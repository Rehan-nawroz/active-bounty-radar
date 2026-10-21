// Package scan wires search, parsing, dedup, filtering, enrichment and ranking
// into a single scan.
package scan

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/dedup"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/github"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/parse"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/rank"
)

// Source is one GitHub search that discovers bounties.
type Source struct {
	Name  string
	Query string
}

// DefaultSources returns the three discovery conventions: the funded-campaign
// label, a plain bounty label, and the word in the title.
func DefaultSources() []Source {
	return []Source{
		{Name: "algora", Query: `label:"💎 Bounty" is:issue is:open`},
		{Name: "label", Query: `label:bounty is:issue is:open`},
		{Name: "title", Query: `bounty in:title is:issue is:open`},
	}
}

// SourceNames lists the selectable source names, in priority order.
func SourceNames() []string { return []string{"algora", "label", "title"} }

// Searcher is implemented by *github.Client.
type Searcher interface {
	SearchIssues(ctx context.Context, query string, limit int) ([]github.Issue, error)
}

// Enricher fills in repository language and open PR counts.
type Enricher interface {
	RepoInfo(ctx context.Context, repo string) (model.RepoDetails, error)
}

const (
	// DefaultEnrichMaxRepos bounds how many repositories a single authenticated
	// scan enriches. Enrichment costs two requests per repository, so an
	// unbounded pass would exhaust the budget on the first scan.
	DefaultEnrichMaxRepos = 50
	// UnauthenticatedEnrichMaxRepos is the same bound without a token, where the
	// hourly budget is two orders of magnitude smaller.
	UnauthenticatedEnrichMaxRepos = 10
)

// Options configures a scan.
type Options struct {
	Sources     []Source
	Languages   []string
	Limit       int // 0 means "no truncation" (capped by the API at 1000 per query)
	MinAmount   int64
	MaxComments int // -1 disables the filter
	Sort        string
	Enricher    Enricher // nil disables enrichment
	// Authenticated reports whether requests carry a token. It decides the
	// default enrichment budget, which is policy the scanner owns.
	Authenticated bool
	// EnrichMaxRepos overrides the default budget when greater than zero.
	EnrichMaxRepos int
	Logf           func(string, ...any)
}

type querySpec struct {
	label  string
	query  string
	source string
}

// Run performs a full scan. It returns partial results together with a warning
// when only some of the searches fail; it only errors when every search fails.
func Run(ctx context.Context, searcher Searcher, opt Options) ([]model.Bounty, error) {
	logf := opt.Logf
	if logf == nil {
		logf = func(string, ...any) {}
	}
	if opt.Limit < 0 {
		opt.Limit = 0
	}

	// Fetch at least a page per query so ranking has something to choose from
	// even when the caller only wants the top 10.
	fetchLimit := opt.Limit
	if fetchLimit < 100 {
		fetchLimit = 100
	}
	if fetchLimit > 1000 {
		fetchLimit = 1000
	}

	queries := expandQueries(opt)
	if len(queries) == 0 {
		return nil, fmt.Errorf("scan: no sources selected")
	}

	var (
		bounties  []model.Bounty
		failures  []string
		succeeded int
	)
	for _, q := range queries {
		issues, err := searcher.SearchIssues(ctx, q.query, fetchLimit)
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", q.label, err))
			if ctx.Err() != nil {
				return nil, err
			}
		} else {
			succeeded++
		}
		for _, issue := range issues {
			if b, ok := bountyFromIssue(issue, q.source); ok {
				bounties = append(bounties, b)
			}
		}
	}
	if succeeded == 0 {
		// One compact error instead of a per-source warning followed by the
		// same message again: when everything fails, the causes are what matter.
		return nil, everySearchFailed(failures)
	}
	for _, f := range failures {
		logf("search failed: %s", f)
	}

	bounties = dedup.Bounties(bounties)
	bounties = filterByAmountAndComments(bounties, opt)

	if opt.Enricher != nil {
		// Cheap ordering first so the enrichment budget goes to the bounties
		// most likely to make the final cut.
		rank.Apply(bounties)
		rank.Sort(bounties, opt.Sort)

		enrich(ctx, opt.Enricher, bounties, enrichBudget(opt), logf)
	}

	rank.Apply(bounties)
	rank.Sort(bounties, opt.Sort)

	if opt.Limit > 0 && len(bounties) > opt.Limit {
		bounties = bounties[:opt.Limit]
	}
	return bounties, nil
}

// enrichBudget decides how many repositories a scan may enrich. Without a
// token the hourly budget is far smaller, so the cap shrinks with it.
func enrichBudget(opt Options) int {
	if opt.EnrichMaxRepos > 0 {
		return opt.EnrichMaxRepos
	}
	if !opt.Authenticated {
		return UnauthenticatedEnrichMaxRepos
	}
	return DefaultEnrichMaxRepos
}

func everySearchFailed(failures []string) error {
	unique := make([]string, 0, len(failures))
	seen := make(map[string]bool, len(failures))
	for _, failure := range failures {
		// Drop the "source: " prefix so identical causes collapse together.
		cause := failure
		if i := strings.Index(cause, ": "); i >= 0 {
			cause = cause[i+2:]
		}
		if seen[cause] {
			continue
		}
		seen[cause] = true
		unique = append(unique, cause)
	}
	return fmt.Errorf("every search failed: %s", strings.Join(unique, "; "))
}

func expandQueries(opt Options) []querySpec {
	// GitHub ANDs search qualifiers, so multiple languages need one query each.
	languages := opt.Languages
	if len(languages) == 0 {
		languages = []string{""}
	}

	var out []querySpec
	for _, src := range opt.Sources {
		for _, lang := range languages {
			q := src.Query
			label := src.Name
			if lang != "" {
				q += " language:" + lang
				label += "[" + lang + "]"
			}
			out = append(out, querySpec{label: label, query: q, source: src.Name})
		}
	}
	return out
}

func bountyFromIssue(issue github.Issue, source string) (model.Bounty, bool) {
	repo := repoFromURL(issue.RepositoryURL)
	if repo == "" || issue.Number == 0 {
		return model.Bounty{}, false
	}
	if issue.PullRequest != nil {
		return model.Bounty{}, false
	}

	texts := make([]string, 0, len(issue.Labels)+1)
	texts = append(texts, issue.Title)
	for _, label := range issue.Labels {
		texts = append(texts, label.Name)
	}

	b := model.Bounty{
		ID:        repo + "#" + strconv.Itoa(issue.Number),
		Repo:      repo,
		Number:    issue.Number,
		Title:     issue.Title,
		URL:       issue.HTMLURL,
		Source:    source,
		Comments:  issue.Comments,
		CreatedAt: issue.CreatedAt.UTC().Format(time.RFC3339),
	}
	if b.URL == "" {
		b.URL = "https://github.com/" + repo + "/issues/" + strconv.Itoa(issue.Number)
	}
	if amount, ok := parse.Best(texts...); ok {
		value := amount.ValueUSD
		b.AmountUSD = &value
		b.Currency = amount.Currency
	}
	return b, true
}

func repoFromURL(raw string) string {
	const marker = "/repos/"
	i := strings.Index(raw, marker)
	if i < 0 {
		return ""
	}
	rest := strings.Trim(raw[i+len(marker):], "/")
	parts := strings.Split(rest, "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		return ""
	}
	return parts[0] + "/" + parts[1]
}

func filterByAmountAndComments(in []model.Bounty, opt Options) []model.Bounty {
	out := make([]model.Bounty, 0, len(in))
	for _, b := range in {
		if opt.MinAmount > 0 && (b.AmountUSD == nil || *b.AmountUSD < opt.MinAmount) {
			continue
		}
		if opt.MaxComments >= 0 && b.Comments > opt.MaxComments {
			continue
		}
		out = append(out, b)
	}
	return out
}

func enrich(ctx context.Context, enricher Enricher, bounties []model.Bounty, budget int, logf func(string, ...any)) {
	repos := make([]string, 0, budget)
	seen := make(map[string]bool, len(bounties))
	capped := false
	for _, b := range bounties {
		if !seen[b.Repo] {
			if len(repos) >= budget {
				capped = true
				break
			}
			seen[b.Repo] = true
			repos = append(repos, b.Repo)
		}
	}
	if capped {
		logf("enrichment capped at %d repositories; language and open-PR data is partial", budget)
	}
	if len(repos) == 0 {
		return
	}

	type result struct {
		details model.RepoDetails
		err     error
	}

	var (
		mu    sync.Mutex
		wg    sync.WaitGroup
		sem   = make(chan struct{}, 4)
		cache = make(map[string]result, len(repos))
	)
	for _, repo := range repos {
		wg.Add(1)
		go func(repo string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			details, err := enricher.RepoInfo(ctx, repo)
			mu.Lock()
			cache[repo] = result{details: details, err: err}
			mu.Unlock()
		}(repo)
	}
	wg.Wait()

	failed := 0
	for _, repo := range repos {
		if cache[repo].err != nil {
			failed++
		}
	}
	if failed > 0 {
		logf("could not fetch language/open-PR data for %d of %d repositories", failed, len(repos))
	}

	for i := range bounties {
		res, ok := cache[bounties[i].Repo]
		if !ok {
			continue
		}
		if res.details.Language != "" {
			bounties[i].Language = res.details.Language
		}
		bounties[i].OpenPRs = res.details.OpenPRs
	}
}
