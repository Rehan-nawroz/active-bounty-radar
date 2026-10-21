package rank

import (
	"testing"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
)

func amount(v int64) *int64 { return &v }

func TestCompetition(t *testing.T) {
	tests := []struct {
		name string
		b    model.Bounty
		want string
	}{
		{"untouched", model.Bounty{}, "low"},
		{"one comment", model.Bounty{Comments: 1}, "low"},
		{"three comments", model.Bounty{Comments: 3}, "medium"},
		{"one open PR counts as two comments", model.Bounty{OpenPRs: 1}, "medium"},
		{"many comments", model.Bounty{Comments: 20}, "high"},
		{"busy queue", model.Bounty{Comments: 1, OpenPRs: 4}, "high"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Competition(tc.b); got != tc.want {
				t.Fatalf("Competition = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestScorePrefersLessContention(t *testing.T) {
	quiet := model.Bounty{AmountUSD: amount(1000), Comments: 0}
	busy := model.Bounty{AmountUSD: amount(1000), Comments: 9}

	if Score(quiet) <= Score(busy) {
		t.Fatalf("quiet score %v should beat busy score %v", Score(quiet), Score(busy))
	}
	if Score(model.Bounty{}) != 0 {
		t.Fatalf("bounty without amount should score 0")
	}
}

func TestSortOrders(t *testing.T) {
	rich := model.Bounty{ID: "a/rich#1", AmountUSD: amount(5000), Comments: 0, CreatedAt: "2026-01-01T00:00:00Z"}
	busy := model.Bounty{ID: "a/busy#2", AmountUSD: amount(5000), Comments: 8, CreatedAt: "2026-03-01T00:00:00Z"}
	unknown := model.Bounty{ID: "a/unknown#3", CreatedAt: "2026-02-01T00:00:00Z"}

	all := []model.Bounty{unknown, busy, rich}
	Apply(all)

	ranked := append([]model.Bounty(nil), all...)
	Sort(ranked, "score")
	if ranked[0].ID != rich.ID || ranked[2].ID != unknown.ID {
		t.Fatalf("score sort = %v", ids(ranked))
	}

	byAmount := append([]model.Bounty(nil), all...)
	Sort(byAmount, "amount")
	if byAmount[2].ID != unknown.ID {
		t.Fatalf("unknown amount should sort last: %v", ids(byAmount))
	}

	byComments := append([]model.Bounty(nil), all...)
	Sort(byComments, "comments")
	if byComments[0].ID != rich.ID {
		t.Fatalf("comments sort should put the quietest first: %v", ids(byComments))
	}

	byNewest := append([]model.Bounty(nil), all...)
	Sort(byNewest, "newest")
	if byNewest[0].ID != busy.ID {
		t.Fatalf("newest sort = %v", ids(byNewest))
	}

	byOldest := append([]model.Bounty(nil), all...)
	Sort(byOldest, "oldest")
	if byOldest[0].ID != rich.ID {
		t.Fatalf("oldest sort = %v", ids(byOldest))
	}
}

func ids(bs []model.Bounty) []string {
	out := make([]string, len(bs))
	for i, b := range bs {
		out[i] = b.ID
	}
	return out
}
