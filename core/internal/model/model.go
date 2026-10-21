// Package model holds the types shared by the scanner, the ranking layer and
// the JSON contract consumed by the Python CLI/TUI.
package model

// Bounty is a single payable open-source task found on GitHub.
//
// JSON field names are part of the public contract: the Python CLI forwards
// this payload verbatim in --json mode, so renaming a field is a breaking
// change for users piping into jq.
type Bounty struct {
	ID          string  `json:"id"`          // "<owner>/<repo>#<number>"
	Repo        string  `json:"repo"`        // "<owner>/<repo>"
	Number      int     `json:"number"`      // issue number
	Title       string  `json:"title"`       // issue title
	URL         string  `json:"url"`         // html_url
	AmountUSD   *int64  `json:"amount_usd"`  // nil when no plausible amount was found
	Currency    string  `json:"currency"`    // currency the amount was written in
	Source      string  `json:"source"`      // algora | label | title
	Comments    int     `json:"comments"`    // comment count (competition signal)
	OpenPRs     int     `json:"open_prs"`    // open PRs in the repo (competition signal)
	Language    string  `json:"language"`    // repo language, empty when not enriched
	CreatedAt   string  `json:"created_at"`  // RFC3339, UTC
	Competition string  `json:"competition"` // low | medium | high
	Score       float64 `json:"score"`       // amount per unit of competition
}
