package dedup

import (
	"testing"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
)

func amount(v int64) *int64 { return &v }

func TestBountiesMergesDuplicates(t *testing.T) {
	in := []model.Bounty{
		{ID: "acme/widget#7", Repo: "acme/widget", Number: 7, Title: "Ship it", Source: "title", AmountUSD: nil, Comments: 1},
		{ID: "acme/widget#7", Repo: "acme/widget", Number: 7, Title: "Ship it", Source: "algora", AmountUSD: amount(10000), Currency: "USD", Comments: 4, OpenPRs: 2},
		{ID: "other/thing#1", Repo: "other/thing", Number: 1, Title: "Other", Source: "label"},
	}

	got := Bounties(in)
	if len(got) != 2 {
		t.Fatalf("got %d bounties, want 2", len(got))
	}
	if got[0].Source != "algora" {
		t.Errorf("source = %q, want algora (highest priority wins)", got[0].Source)
	}
	if got[0].AmountUSD == nil || *got[0].AmountUSD != 10000 {
		t.Errorf("amount = %v, want 10000", got[0].AmountUSD)
	}
	if got[0].Comments != 4 {
		t.Errorf("comments = %d, want 4", got[0].Comments)
	}
	if got[0].OpenPRs != 2 {
		t.Errorf("open PRs = %d, want 2", got[0].OpenPRs)
	}
}

func TestBountiesKeepsLargerAmount(t *testing.T) {
	in := []model.Bounty{
		{ID: "a/b#1", Source: "algora", AmountUSD: amount(500), Currency: "USD"},
		{ID: "a/b#1", Source: "title", AmountUSD: amount(2000), Currency: "USD"},
	}
	got := Bounties(in)
	if len(got) != 1 {
		t.Fatalf("got %d bounties, want 1", len(got))
	}
	if got[0].AmountUSD == nil || *got[0].AmountUSD != 2000 {
		t.Fatalf("amount = %v, want 2000", got[0].AmountUSD)
	}
}

func TestBountiesKeepsDistinctIssuesInSameRepo(t *testing.T) {
	in := []model.Bounty{
		{ID: "a/b#1", Source: "algora"},
		{ID: "a/b#2", Source: "algora"},
	}
	if got := Bounties(in); len(got) != 2 {
		t.Fatalf("got %d bounties, want 2", len(got))
	}
}
