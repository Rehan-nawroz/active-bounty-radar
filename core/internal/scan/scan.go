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

// DefaultSources returns the searches the upstream tool used.
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

// RepoDetails is the per-repository context used to score competition.
type RepoDetails = github.RepoDetails

// Enricher fills in repository language and open PR counts.
type Enricher interface {
	RepoInfo(ctx context.Context, repo string) (RepoDetails, error)
}

// DefaultEnrichMaxRepos bounds how many repositories a single scan enriches.
// Enrichment costs two requests per repository, so an unbounded pass would
// exhaust an unauthenticated budget on the first scan.
const DefaultEnrichMaxRepos = 50

// Options configures a scan.
type Options struct {
	Sources        []Source
	Languages      []string
	Limit          int // 0 means "no truncation" (capped by the API at 1000 per query)
	MinAmount      int64
	MaxComments    int // -1 disables the filter
	Sort           string
	Enricher       Enricher // nil disables enrichment
	EnrichMaxRepos int      // 0 uses DefaultEnrichMaxRepos
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
	for _, f := range failures {
		logf("search failed: %s", f)
	}
	if succeeded == 0 {
		return nil, fmt.Errorf("every search failed: %s", strings.Join(failures, "; "))
	}

	bounties = dedup.Bounties(bounties)
	bounties = filterByAmountAndComments(bounties, opt)

	if opt.Enricher != nil {
		// Cheap ordering first so the enrichment budget goes to the bounties
		// most likely to make the final cut.
		rank.Apply(bounties)
		rank.Sort(bounties, opt.Sort)

		budget := opt.EnrichMaxRepos
		if budget <= 0 {
			budget = DefaultEnrichMaxRepos
		}
		enrich(ctx, opt.Enricher, bounties, budget, logf)
	}

	rank.Apply(bounties)
	rank.Sort(bounties, opt.Sort)

	if opt.Limit > 0 && len(bounties) > opt.Limit {
		bounties = bounties[:opt.Limit]
	}
	return bounties, nil
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
		details RepoDetails
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
