// Package parse extracts currency amounts from GitHub labels and titles.
//
// The upstream tool used a case-sensitive "k" check, so "$10K" was read as
// "$10" (four separate pull requests were opened to fix it). Every pattern
// here is case-insensitive and every candidate is validated against
// MinAmount/MaxAmount, which also kills false positives such as "$0".
package parse

import (
	"math"
	"regexp"
	"strconv"
	"strings"
)

const (
	// MinAmount is the sanity floor: "$0" is not a bounty.
	MinAmount int64 = 1
	// MaxAmount is the sanity ceiling: anything above it is almost certainly an
	// issue number, a year or a stray number rather than a bounty.
	MaxAmount int64 = 1_000_000
)

// RatesToUSD are static, approximate conversion rates. They are deliberately
// not fetched over the network: the engine must stay offline-capable and
// deterministic. Update them by hand if they drift badly.
var RatesToUSD = map[string]float64{
	"USD": 1,
	"EUR": 1.10,
	"GBP": 1.27,
}

var (
	// "$10K", "$10k", "$10.5k", "$1,000", "€50", "£30", "US$250".
	// The trailing multiplier only matches when it stands alone, so "$50
	// membership" stays $50 instead of becoming $50,000.
	symbolRe = regexp.MustCompile(`(?i)(US\$|[$€£])\s*([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s*([kKmM])\b)?`)
	// "USD 200", "eur 1,000", "USD 10K".
	codePrefixRe = regexp.MustCompile(`(?i)\b(USD|EUR|GBP)\s*([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s*([kKmM])\b)?`)
	// "200 USD", "10K EUR".
	codeSuffixRe = regexp.MustCompile(`(?i)\b([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s*([kKmM])\b)?\s*(USD|EUR|GBP)\b`)
)

// Amount is one currency amount found in text, normalised to USD.
type Amount struct {
	ValueUSD int64
	Currency string
	Raw      string
}

// All returns every plausible amount found in text, in no particular order.
func All(text string) []Amount {
	var out []Amount

	for _, m := range symbolRe.FindAllStringSubmatch(text, -1) {
		currency := "USD" // "$" is treated as USD; US$ is matched explicitly.
		switch m[1] {
		case "€":
			currency = "EUR"
		case "£":
			currency = "GBP"
		}
		if a, ok := build(m[2], m[3], currency, m[0]); ok {
			out = append(out, a)
		}
	}
	for _, m := range codePrefixRe.FindAllStringSubmatch(text, -1) {
		if a, ok := build(m[2], m[3], m[1], m[0]); ok {
			out = append(out, a)
		}
	}
	for _, m := range codeSuffixRe.FindAllStringSubmatch(text, -1) {
		if a, ok := build(m[1], m[2], m[3], m[0]); ok {
			out = append(out, a)
		}
	}
	return out
}

// Best returns the largest plausible amount across texts (title, then labels).
//
// Taking the largest rather than the first keeps "base $100, up to $500" from
// silently reporting the floor of the range.
func Best(texts ...string) (Amount, bool) {
	var best Amount
	found := false
	for _, text := range texts {
		for _, a := range All(text) {
			if !found || a.ValueUSD > best.ValueUSD {
				best, found = a, true
			}
		}
	}
	return best, found
}

func build(number, multiplier, currency, raw string) (Amount, bool) {
	currency = strings.ToUpper(currency)
	rate, ok := RatesToUSD[currency]
	if !ok {
		return Amount{}, false
	}
	value, err := strconv.ParseFloat(strings.ReplaceAll(number, ",", ""), 64)
	if err != nil {
		return Amount{}, false
	}
	switch strings.ToLower(multiplier) {
	case "k":
		value *= 1e3
	case "m":
		value *= 1e6
	}
	value *= rate
	if value < float64(MinAmount) || value > float64(MaxAmount) {
		return Amount{}, false
	}
	return Amount{ValueUSD: int64(math.Round(value)), Currency: currency, Raw: strings.TrimSpace(raw)}, true
}
