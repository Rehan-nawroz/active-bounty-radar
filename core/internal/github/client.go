// Package github talks to the GitHub REST API.
//
// It adds the things the upstream TypeScript tool lacked: token support,
// pagination past the 50-result cap, and rate-limit aware backoff instead of
// silently returning a truncated list.
package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	// DefaultBaseURL is the public GitHub API.
	DefaultBaseURL = "https://api.github.com"

	apiVersion      = "2022-11-28"
	perPageMax      = 100
	searchResultCap = 1000 // GitHub refuses page*per_page beyond 1000
	defaultUA       = "bounty-core/0.1 (+https://github.com/Rehan-nawroz/active-bounty-radar)"
)

// HTTPError is a non-retryable HTTP failure.
type HTTPError struct {
	Status int
	URL    string
	Body   string
}

func (e *HTTPError) Error() string {
	body := strings.TrimSpace(e.Body)
	if len(body) > 200 {
		body = body[:200] + "…"
	}
	if body == "" {
		return fmt.Sprintf("github: HTTP %d for %s", e.Status, e.URL)
	}
	return fmt.Sprintf("github: HTTP %d for %s: %s", e.Status, e.URL, body)
}

// RateLimitError is returned when GitHub asks us to wait longer than the
// client is willing to block for.
type RateLimitError struct {
	Status  int
	Reset   time.Time
	Message string
}

func (e *RateLimitError) Error() string {
	if e.Reset.IsZero() {
		return fmt.Sprintf("github: rate limited (HTTP %d): %s — set GITHUB_TOKEN for 5000 req/h", e.Status, e.Message)
	}
	return fmt.Sprintf("github: rate limited (HTTP %d) until %s: %s — set GITHUB_TOKEN for 5000 req/h",
		e.Status, e.Reset.Format(time.RFC3339), e.Message)
}

// Issue is the subset of the search result shape we rely on.
type Issue struct {
	Number        int       `json:"number"`
	Title         string    `json:"title"`
	HTMLURL       string    `json:"html_url"`
	RepositoryURL string    `json:"repository_url"`
	State         string    `json:"state"`
	Comments      int       `json:"comments"`
	CreatedAt     time.Time `json:"created_at"`
	Labels        []Label   `json:"labels"`
	PullRequest   *struct{} `json:"pull_request"`
}

// Label is a GitHub label.
type Label struct {
	Name string `json:"name"`
}

type searchResponse struct {
	TotalCount        int     `json:"total_count"`
	IncompleteResults bool    `json:"incomplete_results"`
	Items             []Issue `json:"items"`
}

// RepoDetails is the per-repository context used to judge competition.
type RepoDetails struct {
	Language string
	OpenPRs  int
}

// Client is a GitHub REST client. It is safe for concurrent use.
type Client struct {
	httpClient *http.Client
	baseURL    string
	token      string
	logf       func(string, ...any)
	sleep      func(context.Context, time.Duration) error
	maxRetries int
	maxWait    time.Duration
	userAgent  string
}

// Option configures a Client.
type Option func(*Client)

// New returns a client pointed at the public API unless WithBaseURL is given.
func New(opts ...Option) *Client {
	c := &Client{
		httpClient: &http.Client{Timeout: 30 * time.Second},
		baseURL:    DefaultBaseURL,
		logf:       func(string, ...any) {},
		sleep:      sleepCtx,
		maxRetries: 3,
		maxWait:    75 * time.Second,
		userAgent:  defaultUA,
	}
	for _, opt := range opts {
		opt(c)
	}
	c.baseURL = strings.TrimRight(c.baseURL, "/")
	return c
}

// WithToken authenticates requests (5000 req/h instead of 60 or 10).
func WithToken(token string) Option { return func(c *Client) { c.token = token } }

// WithBaseURL overrides the API root (used by tests and GitHub Enterprise).
func WithBaseURL(base string) Option {
	return func(c *Client) {
		if base != "" {
			c.baseURL = base
		}
	}
}

// WithHTTPClient replaces the underlying transport.
func WithHTTPClient(h *http.Client) Option { return func(c *Client) { c.httpClient = h } }

// WithLogger receives warnings (rate limiting, incomplete results).
func WithLogger(logf func(string, ...any)) Option {
	return func(c *Client) {
		if logf != nil {
			c.logf = logf
		}
	}
}

// WithSleeper replaces the wait function; tests use it to stay instant.
func WithSleeper(f func(context.Context, time.Duration) error) Option {
	return func(c *Client) {
		if f != nil {
			c.sleep = f
		}
	}
}

// WithMaxRetries sets how many times a failed request is retried.
func WithMaxRetries(n int) Option {
	return func(c *Client) {
		if n >= 0 {
			c.maxRetries = n
		}
	}
}

// WithMaxWait caps how long the client blocks on a rate limit before failing.
func WithMaxWait(d time.Duration) Option { return func(c *Client) { c.maxWait = d } }

// HasToken reports whether the client is authenticated.
func (c *Client) HasToken() bool { return c.token != "" }

// SearchIssues runs one search query and follows pagination until limit,
// the result cap, or the last page is reached.
//
// On a mid-way failure the pages fetched so far are returned together with the
// error, so a flaky third page does not throw away the first two.
func (c *Client) SearchIssues(ctx context.Context, query string, limit int) ([]Issue, error) {
	if limit <= 0 || limit > searchResultCap {
		limit = searchResultCap
	}

	var (
		out  []Issue
		page = 1
	)
	for {
		q := url.Values{}
		q.Set("q", query)
		q.Set("per_page", strconv.Itoa(perPageMax))
		q.Set("page", strconv.Itoa(page))
		q.Set("sort", "created")
		q.Set("order", "desc")

		var resp searchResponse
		err := c.get(ctx, "/search/issues", q, &resp)
		if err != nil {
			return out, err
		}
		if resp.IncompleteResults {
			c.logf("GitHub flagged incomplete results for %q; some matches may be missing", query)
		}

		out = append(out, resp.Items...)
		if len(resp.Items) < perPageMax || len(out) >= limit {
			break
		}
		if page*perPageMax >= searchResultCap {
			c.logf("%q hit GitHub's %d-result search cap; narrow the query to see more", query, searchResultCap)
			break
		}
		page++
	}

	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// RepoInfo returns the repository language and its number of open pull
// requests. The PR count is a proxy for how contended a repository is; it costs
// one extra search request and is only used when enrichment is enabled.
func (c *Client) RepoInfo(ctx context.Context, repo string) (RepoDetails, error) {
	var meta struct {
		Language string `json:"language"`
	}
	if err := c.get(ctx, "/repos/"+repo, nil, &meta); err != nil {
		return RepoDetails{}, err
	}

	count, err := c.CountIssues(ctx, "repo:"+repo+" is:pr is:open")
	details := RepoDetails{Language: meta.Language, OpenPRs: count}
	if err != nil {
		// Language alone is still useful.
		return details, fmt.Errorf("open PR count for %s: %w", repo, err)
	}
	return details, nil
}

// CountIssues returns total_count for a search query without fetching results.
func (c *Client) CountIssues(ctx context.Context, query string) (int, error) {
	q := url.Values{}
	q.Set("q", query)
	q.Set("per_page", "1")

	var resp searchResponse
	if err := c.get(ctx, "/search/issues", q, &resp); err != nil {
		return 0, err
	}
	return resp.TotalCount, nil
}

func (c *Client) get(ctx context.Context, path string, q url.Values, out any) error {
	endpoint := c.baseURL + path
	if len(q) > 0 {
		endpoint += "?" + q.Encode()
	}

	var lastErr error
	for attempt := 0; ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return err
		}
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", apiVersion)
		req.Header.Set("User-Agent", c.userAgent)
		if c.token != "" {
			req.Header.Set("Authorization", "Bearer "+c.token)
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			// Trim the URL: it carries percent-encoded search queries that swamp
			// a terminal and add nothing over the path.
			lastErr = transportError(err, path)
			if !retryableTransportError(err) || attempt >= c.maxRetries {
				return lastErr
			}
			if err := c.sleep(ctx, backoff(attempt)); err != nil {
				return err
			}
			continue
		}

		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		_ = resp.Body.Close()

		switch {
		case resp.StatusCode >= 200 && resp.StatusCode < 300:
			if readErr != nil {
				return fmt.Errorf("github: reading %s: %w", endpoint, readErr)
			}
			if out == nil {
				return nil
			}
			if err := json.Unmarshal(body, out); err != nil {
				return fmt.Errorf("github: decoding %s: %w", endpoint, err)
			}
			return nil

		case isRateLimited(resp):
			wait, reset := rateLimitWait(resp)
			msg := errorBody(body)
			if wait > c.maxWait || attempt >= c.maxRetries {
				return &RateLimitError{Status: resp.StatusCode, Reset: reset, Message: msg}
			}
			c.logf("rate limited (HTTP %d); waiting %s before retrying%s",
				resp.StatusCode, wait.Round(time.Second), budgetHint(c.token))
			if err := c.sleep(ctx, wait); err != nil {
				return err
			}
			continue

		case resp.StatusCode >= 500:
			lastErr = &HTTPError{Status: resp.StatusCode, URL: path, Body: string(body)}
			if attempt >= c.maxRetries {
				return lastErr
			}
			c.logf("GitHub returned HTTP %d for %s; retrying", resp.StatusCode, path)
			if err := c.sleep(ctx, backoff(attempt)); err != nil {
				return err
			}
			continue

		default:
			return &HTTPError{Status: resp.StatusCode, URL: path, Body: string(body)}
		}
	}
}

func budgetHint(token string) string {
	if token != "" {
		return ""
	}
	return " — set GITHUB_TOKEN for a 5000 req/h budget"
}

func isRateLimited(resp *http.Response) bool {
	if resp.StatusCode == http.StatusTooManyRequests {
		return true
	}
	if resp.StatusCode != http.StatusForbidden {
		return false
	}
	return resp.Header.Get("x-ratelimit-remaining") == "0" || resp.Header.Get("retry-after") != ""
}

func rateLimitWait(resp *http.Response) (time.Duration, time.Time) {
	if raw := resp.Header.Get("retry-after"); raw != "" {
		if secs, err := strconv.Atoi(raw); err == nil {
			return clampWait(time.Duration(secs) * time.Second), time.Time{}
		}
		if when, err := http.ParseTime(raw); err == nil {
			return clampWait(time.Until(when)), when
		}
	}
	if raw := resp.Header.Get("x-ratelimit-reset"); raw != "" {
		if secs, err := strconv.ParseInt(raw, 10, 64); err == nil {
			reset := time.Unix(secs, 0)
			return clampWait(time.Until(reset)), reset
		}
	}
	return time.Second, time.Time{}
}

func clampWait(d time.Duration) time.Duration {
	const minimum = 500 * time.Millisecond
	if d < minimum {
		return minimum
	}
	return d
}

func backoff(attempt int) time.Duration {
	d := time.Duration(1<<uint(attempt)) * 500 * time.Millisecond
	if d > 10*time.Second {
		d = 10 * time.Second
	}
	return d
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// transportError keeps the failure reason but drops the query string.
// http.Client can wrap the transport error in more than one *url.Error, and
// each layer repeats the full URL, so unwrap them all before formatting.
func transportError(err error, path string) error {
	cause := err
	for i := 0; i < 4; i++ {
		uerr, ok := cause.(*url.Error)
		if !ok || uerr.Err == nil {
			break
		}
		cause = uerr.Err
	}
	return fmt.Errorf("github: %s: %w", path, cause)
}

// retryableTransportError reports whether a transport failure deserves another
// attempt. Connection refusals and DNS failures do not heal within a backoff
// delay, so retrying them only burns the caller's timeout and hides the real
// cause behind a "context deadline exceeded".
func retryableTransportError(err error) bool {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	return false
}

func errorBody(body []byte) string {
	s := strings.TrimSpace(string(body))
	if len(s) > 160 {
		s = s[:160] + "…"
	}
	return s
}
