package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// fakeIssue builds one search result item.
func fakeIssue(number int, title string) map[string]any {
	return map[string]any{
		"number":         number,
		"title":          title,
		"html_url":       fmt.Sprintf("https://github.com/acme/widget/issues/%d", number),
		"repository_url": "https://api.github.com/repos/acme/widget",
		"state":          "open",
		"comments":       number % 5,
		"created_at":     "2026-01-02T03:04:05Z",
		"labels":         []map[string]string{{"name": "bounty"}},
	}
}

func writeSearch(t *testing.T, w http.ResponseWriter, items []map[string]any, incomplete bool) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(map[string]any{
		"total_count":        len(items),
		"incomplete_results": incomplete,
		"items":              items,
	}); err != nil {
		t.Fatalf("encode: %v", err)
	}
}

func TestSearchIssuesPaginates(t *testing.T) {
	var calls int32
	var sawAuth, sawUA string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		sawAuth = r.Header.Get("Authorization")
		sawUA = r.Header.Get("User-Agent")

		if got := r.URL.Query().Get("per_page"); got != "100" {
			t.Errorf("per_page = %q, want 100", got)
		}
		page := r.URL.Query().Get("page")
		switch page {
		case "1":
			items := make([]map[string]any, 100)
			for i := range items {
				items[i] = fakeIssue(i+1, fmt.Sprintf("issue %d", i+1))
			}
			writeSearch(t, w, items, false)
		case "2":
			writeSearch(t, w, []map[string]any{fakeIssue(101, "last one"), fakeIssue(102, "and another")}, false)
		default:
			t.Errorf("unexpected page %q", page)
			writeSearch(t, w, nil, false)
		}
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithToken("tok-123"), WithSleeper(noSleep))
	issues, err := client.SearchIssues(context.Background(), "label:bounty", 500)
	if err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if len(issues) != 102 {
		t.Fatalf("got %d issues, want 102", len(issues))
	}
	if got := atomic.LoadInt32(&calls); got != 2 {
		t.Fatalf("got %d requests, want 2", got)
	}
	if sawAuth != "Bearer tok-123" {
		t.Errorf("Authorization = %q", sawAuth)
	}
	if !strings.HasPrefix(sawUA, "bounty-core") {
		t.Errorf("User-Agent = %q", sawUA)
	}
}

func TestSearchIssuesStopsAtLimit(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		items := make([]map[string]any, 100)
		for i := range items {
			items[i] = fakeIssue(i+1, "x")
		}
		writeSearch(t, w, items, false)
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep))
	issues, err := client.SearchIssues(context.Background(), "label:bounty", 40)
	if err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if len(issues) != 40 {
		t.Fatalf("got %d issues, want 40", len(issues))
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("got %d requests, want 1", got)
	}
}

func TestSearchIssuesRetriesOnRateLimit(t *testing.T) {
	var calls int32
	var slept []time.Duration

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) == 1 {
			w.Header().Set("Retry-After", "2")
			w.Header().Set("x-ratelimit-remaining", "0")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		writeSearch(t, w, []map[string]any{fakeIssue(1, "$10K reward")}, false)
	}))
	defer srv.Close()

	client := New(
		WithBaseURL(srv.URL),
		WithSleeper(func(_ context.Context, d time.Duration) error {
			slept = append(slept, d)
			return nil
		}),
	)
	issues, err := client.SearchIssues(context.Background(), "label:bounty", 10)
	if err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if len(issues) != 1 {
		t.Fatalf("got %d issues, want 1", len(issues))
	}
	if len(slept) != 1 || slept[0] != 2*time.Second {
		t.Fatalf("slept = %v, want [2s]", slept)
	}
}

func TestSearchIssuesGivesUpWhenWaitTooLong(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep), WithMaxWait(10*time.Second))
	_, err := client.SearchIssues(context.Background(), "label:bounty", 10)

	var rl *RateLimitError
	if !errors.As(err, &rl) {
		t.Fatalf("error = %v, want *RateLimitError", err)
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("got %d requests, want 1 (no pointless retries)", got)
	}
}

func TestSearchIssuesRetriesServerErrors(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) == 1 {
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte("bad gateway"))
			return
		}
		writeSearch(t, w, []map[string]any{fakeIssue(1, "ok")}, false)
	}))
	defer srv.Close()

	var slept []time.Duration
	client := New(WithBaseURL(srv.URL), WithSleeper(func(_ context.Context, d time.Duration) error {
		slept = append(slept, d)
		return nil
	}))
	issues, err := client.SearchIssues(context.Background(), "label:bounty", 10)
	if err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if len(issues) != 1 {
		t.Fatalf("got %d issues, want 1", len(issues))
	}
	if len(slept) != 1 || slept[0] != 500*time.Millisecond {
		t.Fatalf("slept = %v, want [500ms]", slept)
	}
}

func TestSearchIssuesDoesNotRetryClientErrors(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"message":"Validation Failed"}`))
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep))
	_, err := client.SearchIssues(context.Background(), "label:nope", 10)

	var httpErr *HTTPError
	if !errors.As(err, &httpErr) {
		t.Fatalf("error = %v, want *HTTPError", err)
	}
	if httpErr.Status != http.StatusUnprocessableEntity {
		t.Errorf("status = %d", httpErr.Status)
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("got %d requests, want 1", got)
	}
}

func TestSearchIssuesWithoutTokenSendsNoAuthorization(t *testing.T) {
	var auth atomic.Value
	auth.Store("unset")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth.Store(r.Header.Get("Authorization"))
		writeSearch(t, w, nil, false)
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep))
	if _, err := client.SearchIssues(context.Background(), "label:bounty", 10); err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if got := auth.Load().(string); got != "" {
		t.Fatalf("Authorization header = %q, want empty", got)
	}
}

func TestSearchIssuesWarnsOnIncompleteResults(t *testing.T) {
	var warnings []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeSearch(t, w, []map[string]any{fakeIssue(1, "x")}, true)
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep), WithLogger(func(format string, a ...any) {
		warnings = append(warnings, fmt.Sprintf(format, a...))
	}))
	if _, err := client.SearchIssues(context.Background(), "label:bounty", 10); err != nil {
		t.Fatalf("SearchIssues: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "incomplete") {
		t.Fatalf("warnings = %v", warnings)
	}
}

func TestRepoInfo(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/acme/widget":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"language":"Go","open_issues_count":12}`))
		case r.URL.Path == "/search/issues":
			if q := r.URL.Query().Get("q"); q != "repo:acme/widget is:pr is:open" {
				t.Errorf("q = %q", q)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"total_count":3,"incomplete_results":false,"items":[]}`))
		default:
			t.Errorf("unexpected path %q", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	client := New(WithBaseURL(srv.URL), WithSleeper(noSleep))
	details, err := client.RepoInfo(context.Background(), "acme/widget")
	if err != nil {
		t.Fatalf("RepoInfo: %v", err)
	}
	if details.Language != "Go" {
		t.Errorf("language = %q, want Go", details.Language)
	}
	if details.OpenPRs != 3 {
		t.Errorf("open PRs = %d, want 3", details.OpenPRs)
	}
}

func noSleep(context.Context, time.Duration) error { return nil }
