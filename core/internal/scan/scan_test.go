package scan

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/github"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
)

type fakeSearcher struct {
	results map[string][]github.Issue
	errs    map[string]error
	calls   []string
}

func (f *fakeSearcher) SearchIssues(_ context.Context, query string, _ int) ([]github.Issue, error) {
	f.calls = append(f.calls, query)
	for prefix, err := range f.errs {
		if strings.Contains(query, prefix) {
			return nil, err
		}
	}
	for prefix, issues := range f.results {
		if strings.Contains(query, prefix) {
			return issues, nil
		}
	}
	return nil, nil
}

type fakeEnricher struct {
	details map[string]model.RepoDetails
	fail    map[string]bool
}

func (f *fakeEnricher) RepoInfo(_ context.Context, repo string) (model.RepoDetails, error) {
	if f.fail[repo] {
		return model.RepoDetails{Language: "Go"}, errors.New("boom")
	}
	return f.details[repo], nil
}

func issue(number int, title string, comments int, labels ...string) github.Issue {
	is := github.Issue{
		Number:        number,
		Title:         title,
		HTMLURL:       "https://github.com/acme/widget/issues/" + strconv.Itoa(number),
		RepositoryURL: "https://api.github.com/repos/acme/widget",
		Comments:      comments,
		CreatedAt:     time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC),
	}
	for _, l := range labels {
		is.Labels = append(is.Labels, github.Label{Name: l})
	}
	return is
}

func TestRunHandlesTheAmountBugAndDedup(t *testing.T) {
	searcher := &fakeSearcher{results: map[string][]github.Issue{
		`label:"💎 Bounty"`: {
			issue(7, "$10K reward for the parser", 2, "💎 Bounty"),
			issue(8, "no amount here", 0, "💎 Bounty"),
		},
		"bounty in:title": {
			issue(7, "$10K reward for the parser", 5, "bounty"), // duplicate, richer
			issue(9, "Bounty: USD 200", 1),
		},
	}}

	got, err := Run(context.Background(), searcher, Options{
		Sources:     DefaultSources(),
		Limit:       10,
		MaxComments: -1,
		Sort:        "score",
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("got %d bounties, want 3 (dedup failed): %+v", len(got), got)
	}

	byID := map[string]struct {
		amount   int64
		comments int
		source   string
	}{}
	for _, b := range got {
		var amount int64
		if b.AmountUSD != nil {
			amount = *b.AmountUSD
		}
		byID[b.ID] = struct {
			amount   int64
			comments int
			source   string
		}{amount, b.Comments, b.Source}
	}

	seven := byID["acme/widget#7"]
	if seven.amount != 10000 {
		t.Errorf("issue 7 amount = %d, want 10000 ($10K bug)", seven.amount)
	}
	if seven.comments != 5 {
		t.Errorf("issue 7 comments = %d, want 5 (merged)", seven.comments)
	}
	if seven.source != "algora" {
		t.Errorf("issue 7 source = %q, want algora", seven.source)
	}
	if byID["acme/widget#9"].amount != 200 {
		t.Errorf("issue 9 amount = %d, want 200", byID["acme/widget#9"].amount)
	}
	if byID["acme/widget#8"].amount != 0 {
		t.Errorf("issue 8 should have no amount")
	}
}

func TestRunFiltersAndLimits(t *testing.T) {
	searcher := &fakeSearcher{results: map[string][]github.Issue{
		"bounty in:title": {
			issue(1, "Bounty $5,000", 1),
			issue(2, "Bounty $100", 0),
			issue(3, "Bounty $9,000", 40), // too many comments
			issue(4, "Bounty without amount", 0),
		},
	}}

	got, err := Run(context.Background(), searcher, Options{
		Sources:     []Source{{Name: "title", Query: "bounty in:title"}},
		Limit:       10,
		MinAmount:   1000,
		MaxComments: 10,
		Sort:        "amount",
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d bounties, want 1: %+v", len(got), got)
	}
	if got[0].Number != 1 {
		t.Fatalf("kept issue %d, want 1", got[0].Number)
	}

	limited, err := Run(context.Background(), searcher, Options{
		Sources:     []Source{{Name: "title", Query: "bounty in:title"}},
		Limit:       2,
		MaxComments: -1,
		Sort:        "amount",
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(limited) != 2 {
		t.Fatalf("got %d bounties, want 2", len(limited))
	}
	if limited[0].Number != 3 {
		t.Fatalf("sorted by amount, want issue 3 first, got %d", limited[0].Number)
	}
}

func TestRunSkipsPullRequests(t *testing.T) {
	pr := issue(10, "Bounty $500", 0)
	pr.PullRequest = &struct{}{}

	searcher := &fakeSearcher{results: map[string][]github.Issue{
		"bounty in:title": {pr, issue(11, "Bounty $500", 0)},
	}}
	got, err := Run(context.Background(), searcher, Options{
		Sources:     []Source{{Name: "title", Query: "bounty in:title"}},
		Limit:       10,
		MaxComments: -1,
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(got) != 1 || got[0].Number != 11 {
		t.Fatalf("pull requests must be skipped, got %+v", got)
	}
}

func TestRunEnrichesRepositories(t *testing.T) {
	searcher := &fakeSearcher{results: map[string][]github.Issue{
		"bounty in:title": {issue(1, "Bounty $1,000", 1)},
	}}
	enricher := &fakeEnricher{details: map[string]model.RepoDetails{
		"acme/widget": {Language: "Go", OpenPRs: 4},
	}}

	got, err := Run(context.Background(), searcher, Options{
		Sources:     []Source{{Name: "title", Query: "bounty in:title"}},
		Limit:       10,
		MaxComments: -1,
		Enricher:    enricher,
		Logf:        func(string, ...any) {},
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if got[0].Language != "Go" {
		t.Errorf("language = %q, want Go", got[0].Language)
	}
	if got[0].OpenPRs != 4 {
		t.Errorf("open PRs = %d, want 4", got[0].OpenPRs)
	}
	if got[0].Competition != "high" {
		t.Errorf("competition = %q, want high (1 comment + 4 PRs = weight 9)", got[0].Competition)
	}
}

func TestRunSurvivesPartialSearchFailures(t *testing.T) {
	searcher := &fakeSearcher{
		results: map[string][]github.Issue{
			"bounty in:title": {issue(1, "Bounty $500", 0)},
		},
		errs: map[string]error{
			`label:"💎 Bounty"`: errors.New("rate limited"),
		},
	}
	var warnings []string

	got, err := Run(context.Background(), searcher, Options{
		Sources:     DefaultSources(),
		Limit:       10,
		MaxComments: -1,
		Logf:        func(format string, a ...any) { warnings = append(warnings, strings.TrimSpace(format)) },
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d bounties, want 1", len(got))
	}
	if len(warnings) != 1 {
		t.Fatalf("warnings = %v, want 1", warnings)
	}
}

func TestRunFailsWhenEverySearchFails(t *testing.T) {
	// "is:issue" is present in every default query, so all of them fail.
	searcher := &fakeSearcher{errs: map[string]error{"is:issue": errors.New("nope")}}
	var warnings []string
	_, err := Run(context.Background(), searcher, Options{
		Sources:     DefaultSources(),
		Limit:       10,
		MaxComments: -1,
		Logf:        func(format string, a ...any) { warnings = append(warnings, format) },
	})
	if err == nil {
		t.Fatal("expected an error when every search fails")
	}
	// The same cause from three sources must appear once, not three times, and
	// must not also be duplicated as per-source warnings.
	if got := strings.Count(err.Error(), "nope"); got != 1 {
		t.Errorf("cause appears %d times in %q, want 1", got, err.Error())
	}
	if len(warnings) != 0 {
		t.Errorf("warnings = %v, want none when every search fails", warnings)
	}
}

func TestEnrichBudgetFollowsAuthentication(t *testing.T) {
	tests := []struct {
		name string
		opt  Options
		want int
	}{
		{"tokenless scan", Options{}, UnauthenticatedEnrichMaxRepos},
		{"authenticated scan", Options{Authenticated: true}, DefaultEnrichMaxRepos},
		{"explicit override", Options{Authenticated: true, EnrichMaxRepos: 3}, 3},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := enrichBudget(tc.opt); got != tc.want {
				t.Fatalf("enrichBudget() = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestRunExpandsLanguagesIntoQueries(t *testing.T) {
	searcher := &fakeSearcher{}
	_, err := Run(context.Background(), searcher, Options{
		Sources:     []Source{{Name: "title", Query: "bounty in:title"}},
		Languages:   []string{"go", "rust"},
		Limit:       10,
		MaxComments: -1,
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if len(searcher.calls) != 2 {
		t.Fatalf("calls = %v, want 2 language-specific queries", searcher.calls)
	}
	for _, q := range searcher.calls {
		if !strings.Contains(q, "language:") {
			t.Errorf("query %q is missing the language qualifier", q)
		}
	}
}
