// Package dedup collapses the same issue returned by several searches.
package dedup

import "github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"

// sourcePriority decides which discovery source wins when an issue is found
// more than once: the Algora label is the most specific signal.
var sourcePriority = map[string]int{
	"algora": 3,
	"label":  2,
	"title":  1,
}

// Bounties removes duplicates by (repo, number) — the upstream tool deduped by
// URL string, which breaks as soon as the same issue comes back with a
// different anchor or casing.
//
// Merging keeps the best of both records: the highest-priority source, the
// largest amount, and the highest competition counts.
func Bounties(in []model.Bounty) []model.Bounty {
	index := make(map[string]int, len(in))
	out := make([]model.Bounty, 0, len(in))

	for _, b := range in {
		i, seen := index[b.ID]
		if !seen {
			index[b.ID] = len(out)
			out = append(out, b)
			continue
		}
		out[i] = merge(out[i], b)
	}
	return out
}

func merge(a, b model.Bounty) model.Bounty {
	if sourcePriority[b.Source] > sourcePriority[a.Source] {
		a.Source = b.Source
	}
	if b.AmountUSD != nil && (a.AmountUSD == nil || *b.AmountUSD > *a.AmountUSD) {
		a.AmountUSD = b.AmountUSD
		a.Currency = b.Currency
	}
	if b.Comments > a.Comments {
		a.Comments = b.Comments
	}
	if b.OpenPRs > a.OpenPRs {
		a.OpenPRs = b.OpenPRs
	}
	if a.Title == "" {
		a.Title = b.Title
	}
	if a.URL == "" {
		a.URL = b.URL
	}
	if a.Language == "" {
		a.Language = b.Language
	}
	if a.CreatedAt == "" || (b.CreatedAt != "" && b.CreatedAt < a.CreatedAt) {
		a.CreatedAt = b.CreatedAt
	}
	return a
}
