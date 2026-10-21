package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
)

func fakeServer(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/search/issues" {
			t.Errorf("unexpected path %q", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"total_count": 2,
			"incomplete_results": false,
			"items": [
				{
					"number": 42,
					"title": "Fix parser, $10K bounty",
					"html_url": "https://github.com/acme/widget/issues/42",
					"repository_url": "https://api.github.com/repos/acme/widget",
					"comments": 3,
					"created_at": "2026-01-02T03:04:05Z",
					"labels": [{"name": "💎 Bounty"}]
				},
				{
					"number": 43,
					"title": "Docs typo",
					"html_url": "https://github.com/acme/docs/issues/43",
					"repository_url": "https://api.github.com/repos/acme/docs",
					"comments": 0,
					"created_at": "2026-02-02T03:04:05Z",
					"labels": [{"name": "bounty"}]
				}
			]
		}`))
	}))
}

// TestScanStdoutIsPureJSON covers the upstream --json bug: the human banner
// used to be printed to stdout, so piping into jq failed.
func TestScanStdoutIsPureJSON(t *testing.T) {
	srv := fakeServer(t)
	defer srv.Close()

	t.Setenv("GITHUB_TOKEN", "")
	t.Setenv("GH_TOKEN", "")

	var stdout, stderr bytes.Buffer
	code := run([]string{"scan", "--base-url", srv.URL, "--limit", "5"}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit code = %d, stderr = %s", code, stderr.String())
	}

	var bounties []model.Bounty
	if err := json.Unmarshal(stdout.Bytes(), &bounties); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\nstdout=%s", err, stdout.String())
	}
	if len(bounties) != 2 {
		t.Fatalf("got %d bounties, want 2", len(bounties))
	}

	var found bool
	for _, b := range bounties {
		if b.Number == 42 {
			found = true
			if b.AmountUSD == nil || *b.AmountUSD != 10000 {
				t.Errorf("amount = %v, want 10000 for $10K", b.AmountUSD)
			}
			if b.ID != "acme/widget#42" {
				t.Errorf("id = %q", b.ID)
			}
			if b.Competition != "medium" {
				t.Errorf("competition = %q, want medium", b.Competition)
			}
		}
	}
	if !found {
		t.Fatal("issue 42 missing from output")
	}
}

func TestScanEmptyResultIsAnArray(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"total_count":0,"incomplete_results":false,"items":[]}`))
	}))
	defer srv.Close()

	t.Setenv("GITHUB_TOKEN", "")
	t.Setenv("GH_TOKEN", "")

	var stdout, stderr bytes.Buffer
	if code := run([]string{"scan", "--base-url", srv.URL}, &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %s", code, stderr.String())
	}
	if strings.TrimSpace(stdout.String()) != "[]" {
		t.Fatalf("stdout = %q, want []", stdout.String())
	}
}

func TestScanAcceptsJSONFlagForParity(t *testing.T) {
	srv := fakeServer(t)
	defer srv.Close()

	t.Setenv("GITHUB_TOKEN", "")
	t.Setenv("GH_TOKEN", "")

	var stdout, stderr bytes.Buffer
	if code := run([]string{"scan", "--json", "--pretty", "--base-url", srv.URL}, &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %s", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "[\n  {") {
		t.Fatalf("--pretty did not indent the output: %q", stdout.String())
	}
	if err := json.Unmarshal(stdout.Bytes(), new([]model.Bounty)); err != nil {
		t.Fatalf("stdout is not valid JSON: %v", err)
	}
}

func TestUsageErrors(t *testing.T) {
	tests := []struct {
		name string
		args []string
	}{
		{"no arguments", nil},
		{"unknown command", []string{"frobnicate"}},
		{"unknown source", []string{"scan", "--source", "nope"}},
		{"invalid enrich mode", []string{"scan", "--enrich", "sometimes"}},
		{"unknown flag", []string{"scan", "--nope"}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := run(tc.args, &stdout, &stderr); code != 2 {
				t.Fatalf("exit code = %d, want 2", code)
			}
			if stdout.Len() != 0 {
				t.Errorf("stdout should stay empty on usage errors, got %q", stdout.String())
			}
		})
	}
}

func TestVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"version"}, &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d", code)
	}
	if !strings.Contains(stdout.String(), version) {
		t.Fatalf("stdout = %q", stdout.String())
	}
}
