// Package rank turns raw bounties into a ranked list.
//
// Upstream scored competition from comment count alone. We add the repository's
// open PR count as a second signal, because a busy reviewer queue matters as
// much as a long comment thread.
package rank

import (
	"math"
	"sort"
	"strings"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
)

const (
	// Each open PR is weighted as heavily as two comments.
	prWeight     = 2
	lowWeightMax = 1
	medWeightMax = 6
)

// Weight is the combined competition signal.
func Weight(b model.Bounty) int { return b.Comments + prWeight*b.OpenPRs }

// Competition buckets the weight into a human label.
func Competition(b model.Bounty) string {
	switch w := Weight(b); {
	case w <= lowWeightMax:
		return "low"
	case w <= medWeightMax:
		return "medium"
	default:
		return "high"
	}
}

// Score is dollars per unit of competition: a $2,000 bounty with no comments
// outranks a $2,000 bounty with forty.
func Score(b model.Bounty) float64 {
	if b.AmountUSD == nil {
		return 0
	}
	return float64(*b.AmountUSD) / float64(1+Weight(b))
}

// Apply fills in the derived fields.
func Apply(bounties []model.Bounty) {
	for i := range bounties {
		bounties[i].Competition = Competition(bounties[i])
		bounties[i].Score = math.Round(Score(bounties[i])*100) / 100
	}
}

// SortKeys lists the accepted sort orders, best first in the help text.
func SortKeys() []string {
	return []string{"score", "amount", "comments", "newest", "oldest"}
}

// Sort orders bounties in place. Unknown keys fall back to score.
func Sort(bounties []model.Bounty, key string) {
	less := lessFor(strings.ToLower(strings.TrimSpace(key)))
	sort.SliceStable(bounties, func(i, j int) bool { return less(bounties[i], bounties[j]) })
}

func lessFor(key string) func(a, b model.Bounty) bool {
	switch key {
	case "amount":
		return func(a, b model.Bounty) bool {
			if c := compareAmount(a, b); c != 0 {
				return c < 0
			}
			return a.Score > b.Score
		}
	case "comments":
		return func(a, b model.Bounty) bool {
			if wa, wb := Weight(a), Weight(b); wa != wb {
				return wa < wb
			}
			return compareAmount(a, b) < 0
		}
	case "newest":
		return func(a, b model.Bounty) bool {
			if a.CreatedAt != b.CreatedAt {
				return a.CreatedAt > b.CreatedAt
			}
			return a.ID < b.ID
		}
	case "oldest":
		return func(a, b model.Bounty) bool {
			if a.CreatedAt != b.CreatedAt {
				return a.CreatedAt < b.CreatedAt
			}
			return a.ID < b.ID
		}
	default: // "score"
		return func(a, b model.Bounty) bool {
			if a.Score != b.Score {
				return a.Score > b.Score
			}
			if c := compareAmount(a, b); c != 0 {
				return c < 0
			}
			return a.ID < b.ID
		}
	}
}

// compareAmount orders amounts descending with unknown amounts last.
func compareAmount(a, b model.Bounty) int {
	switch {
	case a.AmountUSD == nil && b.AmountUSD == nil:
		return 0
	case a.AmountUSD == nil:
		return 1
	case b.AmountUSD == nil:
		return -1
	case *a.AmountUSD > *b.AmountUSD:
		return -1
	case *a.AmountUSD < *b.AmountUSD:
		return 1
	default:
		return 0
	}
}
