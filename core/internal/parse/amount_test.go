package parse

import "testing"

func TestBest(t *testing.T) {
	tests := []struct {
		name     string
		text     string
		want     int64
		currency string
		found    bool
	}{
		// The bug four upstream PRs tried to fix: uppercase K.
		{"uppercase K", "$10K", 10000, "USD", true},
		{"lowercase k", "$10k", 10000, "USD", true},
		{"decimal K", "$10.5K", 10500, "USD", true},
		{"spaced K", "$10 K", 10000, "USD", true},
		{"thousands separator", "$1,000", 1000, "USD", true},
		{"thousands separator with K", "$1,000K", 1_000_000, "USD", true},
		{"algora label", "💎 Bounty: $2,000", 2000, "USD", true},
		{"bounty word", "$10 Bounty", 10, "USD", true},
		{"currency code prefix", "USD 200", 200, "USD", true},
		{"currency code lowercase", "usd 1500", 1500, "USD", true},
		{"currency code suffix", "200 USD", 200, "USD", true},
		{"currency code suffix with multiplier", "10K EUR", 11000, "EUR", true},
		{"currency code prefix with multiplier", "USD 10K", 10000, "USD", true},
		{"euro symbol", "€50", 55, "EUR", true},
		{"pound symbol", "£30", 38, "GBP", true},
		{"pound with multiplier", "£1.5k", 1905, "GBP", true},
		{"euro with multiplier", "€2K", 2200, "EUR", true},
		{"US dollar prefix", "US$250", 250, "USD", true},
		{"rounding", "bounty $1,234.56", 1235, "USD", true},
		{"range picks the ceiling", "Up to $500, base $100", 500, "USD", true},
		{"range with K", "$10k–$20k", 20000, "USD", true},
		{"max is inclusive", "$1,000,000", 1_000_000, "USD", true},

		// Rejections: the false positives from the upstream issues.
		{"zero", "$0", 0, "", false},
		{"below one dollar", "$0.50", 0, "", false},
		{"above ceiling", "$2M", 0, "", false},
		{"above ceiling without suffix", "$1,000,001", 0, "", false},
		{"issue number is not money", "Issue #1234", 0, "", false},
		{"no amount", "No money here", 0, "", false},
		{"unsupported currency", "¥5000", 0, "", false},
		{"currency without number", "USD bounty", 0, "", false},
		{"word starting with m is not a multiplier", "$50 membership", 50, "USD", true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := Best(tc.text)
			if ok != tc.found {
				t.Fatalf("Best(%q) found = %v, want %v (got %+v)", tc.text, ok, tc.found, got)
			}
			if !tc.found {
				return
			}
			if got.ValueUSD != tc.want {
				t.Errorf("Best(%q).ValueUSD = %d, want %d", tc.text, got.ValueUSD, tc.want)
			}
			if got.Currency != tc.currency {
				t.Errorf("Best(%q).Currency = %q, want %q", tc.text, got.Currency, tc.currency)
			}
		})
	}
}

func TestBestAcrossTexts(t *testing.T) {
	// Title says $200, label says $10K — the label wins.
	got, ok := Best("Fix the parser", "💎 Bounty $10K", "bug")
	if !ok {
		t.Fatal("expected an amount")
	}
	if got.ValueUSD != 10000 {
		t.Fatalf("ValueUSD = %d, want 10000", got.ValueUSD)
	}
}

func TestAllFindsEveryCandidate(t *testing.T) {
	amounts := All("first $10k then $250, and £20")
	if len(amounts) != 3 {
		t.Fatalf("got %d amounts, want 3: %+v", len(amounts), amounts)
	}
	want := map[int64]bool{10000: true, 250: true, 25: true}
	for _, a := range amounts {
		if !want[a.ValueUSD] {
			t.Errorf("unexpected amount %+v", a)
		}
	}
}
