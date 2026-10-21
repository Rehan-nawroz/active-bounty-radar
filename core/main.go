// Command bounty-core is the scan engine behind the bounty CLI.
//
// It only ever writes JSON to stdout. Every diagnostic — warnings, rate-limit
// notices, progress — goes to stderr, which is what makes "bounty-core scan |
// jq" safe. The upstream tool printed its banner on stdout and broke --json.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/github"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/model"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/rank"
	"github.com/Rehan-nawroz/active-bounty-radar/core/internal/scan"
)

const version = "0.1.0"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		usage(stderr)
		return 2
	}

	switch args[0] {
	case "scan":
		return runScan(args[1:], stdout, stderr)
	case "version", "--version", "-v":
		fmt.Fprintf(stdout, "bounty-core %s\n", version)
		return 0
	case "help", "--help", "-h":
		usage(stdout)
		return 0
	default:
		fmt.Fprintf(stderr, "bounty-core: unknown command %q\n\n", args[0])
		usage(stderr)
		return 2
	}
}

func runScan(args []string, stdout, stderr io.Writer) int {
	logf := func(format string, a ...any) {
		fmt.Fprintf(stderr, "warning: "+format+"\n", a...)
	}

	fs := flag.NewFlagSet("scan", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() { usage(stderr) }

	var (
		languageList stringList
		sourceList   stringList
	)
	fs.Var(&languageList, "lang", "comma separated GitHub languages (e.g. go,rust); repeatable")
	fs.Var(&sourceList, "source", "discovery sources: algora, label, title; repeatable")

	var (
		minAmount   = fs.Int64("min", 0, "only keep bounties worth at least this many USD")
		maxComments = fs.Int("max-comments", -1, "only keep bounties with at most this many comments (-1 disables)")
		limit       = fs.Int("limit", 100, "maximum number of bounties to return (0 = no truncation)")
		sortKey     = fs.String("sort", "score", "sort order: score, amount, comments, newest, oldest")
		pretty      = fs.Bool("pretty", false, "indent the JSON output")
		enrichMode  = fs.String("enrich", "auto", "repository enrichment: auto, on, off")
		baseURL     = fs.String("base-url", "", "override the GitHub API root (testing)")
		timeout     = fs.Duration("timeout", 120*time.Second, "overall scan timeout")
		maxRetries  = fs.Int("max-retries", 3, "retries per request for rate limits and 5xx responses")
		verbose     = fs.Bool("verbose", false, "print progress information to stderr")
		_           = fs.Bool("json", true, "no-op: JSON is the only output format")
	)

	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "bounty-core: unexpected argument %q\n", fs.Arg(0))
		return 2
	}

	if err := validateFlags(*limit, *maxComments, *minAmount, *timeout, *sortKey); err != nil {
		fmt.Fprintf(stderr, "bounty-core: %v\n", err)
		return 2
	}

	selected, err := selectSources(sourceList)
	if err != nil {
		fmt.Fprintf(stderr, "bounty-core: %v\n", err)
		return 2
	}

	enrich, err := resolveEnrichment(*enrichMode, token())
	if err != nil {
		fmt.Fprintf(stderr, "bounty-core: %v\n", err)
		return 2
	}

	client := github.New(
		github.WithToken(token()),
		github.WithBaseURL(*baseURL),
		github.WithMaxRetries(*maxRetries),
		github.WithLogger(logf),
	)

	opt := scan.Options{
		Sources:     selected,
		Languages:   dedupeStrings(languageList),
		Limit:       *limit,
		MinAmount:   *minAmount,
		MaxComments: *maxComments,
		Sort:        strings.ToLower(strings.TrimSpace(*sortKey)),
		Logf:        logf,
	}
	if enrich {
		opt.Enricher = client
		opt.EnrichMaxRepos = 10 // two requests per repo without a token
		if client.HasToken() {
			opt.EnrichMaxRepos = scan.DefaultEnrichMaxRepos
		} else {
			fmt.Fprintln(stderr, "note: enrichment without GITHUB_TOKEN is limited to 60 requests/hour; only 10 repositories will be enriched")
		}
	}
	if *verbose {
		languages := len(opt.Languages)
		if languages == 0 {
			languages = 1
		}
		fmt.Fprintf(stderr, "debug: %d %s (%d %s × %d %s), limit=%d, enrich=%v\n",
			len(selected)*languages, plural(len(selected)*languages, "query", "queries"),
			len(selected), plural(len(selected), "source", "sources"),
			languages, plural(languages, "language", "languages"),
			*limit, enrich)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()

	started := time.Now()
	bounties, err := scan.Run(ctx, client, opt)
	if err != nil {
		switch {
		case errors.Is(err, context.DeadlineExceeded):
			fmt.Fprintf(stderr, "bounty-core: scan timed out after %s (raise --timeout or lower --limit)\n", *timeout)
		case errors.Is(err, context.Canceled):
			fmt.Fprintln(stderr, "bounty-core: interrupted")
			return 130
		default:
			fmt.Fprintf(stderr, "bounty-core: %v\n", err)
		}
		return 1
	}

	if err := writeJSON(stdout, bounties, *pretty); err != nil {
		fmt.Fprintf(stderr, "bounty-core: %v\n", err)
		return 1
	}
	if *verbose {
		fmt.Fprintf(stderr, "debug: %d bounties in %s\n", len(bounties), time.Since(started).Round(time.Millisecond))
	}
	return 0
}

func writeJSON(w io.Writer, bounties []model.Bounty, pretty bool) error {
	if bounties == nil {
		bounties = []model.Bounty{}
	}
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	if pretty {
		enc.SetIndent("", "  ")
	}
	return enc.Encode(bounties)
}

// stringList is a repeatable comma separated flag. Plain string flags silently
// keep only the last value, so "--lang go --lang rust" would quietly scan Go
// repositories for rust.
type stringList []string

func (s *stringList) String() string { return strings.Join(*s, ",") }

func (s *stringList) Set(value string) error {
	for _, part := range strings.Split(value, ",") {
		if part = strings.TrimSpace(part); part != "" {
			*s = append(*s, part)
		}
	}
	return nil
}

func selectSources(names []string) ([]scan.Source, error) {
	if len(names) == 0 {
		return scan.DefaultSources(), nil
	}

	available := make(map[string]scan.Source)
	for _, s := range scan.DefaultSources() {
		available[s.Name] = s
	}

	seen := make(map[string]bool, len(names))
	out := make([]scan.Source, 0, len(names))
	for _, name := range names {
		key := strings.ToLower(name)
		if seen[key] {
			continue
		}
		src, ok := available[key]
		if !ok {
			return nil, fmt.Errorf("unknown source %q (available: %s)", name, strings.Join(scan.SourceNames(), ", "))
		}
		seen[key] = true
		out = append(out, src)
	}
	return out, nil
}

func validateFlags(limit, maxComments int, minAmount int64, timeout time.Duration, sortKey string) error {
	if limit < 0 {
		return fmt.Errorf("--limit must be 0 or greater, got %d (0 means no truncation)", limit)
	}
	if maxComments < -1 {
		return fmt.Errorf("--max-comments must be -1 (disabled) or greater, got %d", maxComments)
	}
	if minAmount < 0 {
		return fmt.Errorf("--min must be 0 or greater, got %d", minAmount)
	}
	if timeout <= 0 {
		return fmt.Errorf("--timeout must be greater than zero, got %s", timeout)
	}

	sortKey = strings.ToLower(strings.TrimSpace(sortKey))
	for _, key := range rank.SortKeys() {
		if sortKey == key {
			return nil
		}
	}
	return fmt.Errorf("--sort must be one of: %s, got %q", strings.Join(rank.SortKeys(), ", "), sortKey)
}

func resolveEnrichment(mode, tok string) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(mode)) {
	case "auto", "":
		return tok != "", nil
	case "on", "true", "yes":
		return true, nil
	case "off", "false", "no":
		return false, nil
	default:
		return false, fmt.Errorf("invalid --enrich value %q (auto, on, off)", mode)
	}
}

func token() string {
	if t := strings.TrimSpace(os.Getenv("GITHUB_TOKEN")); t != "" {
		return t
	}
	return strings.TrimSpace(os.Getenv("GH_TOKEN"))
}

func plural(n int, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

func dedupeStrings(in []string) []string {
	seen := make(map[string]bool, len(in))
	out := make([]string, 0, len(in))
	for _, value := range in {
		if seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	return out
}

func usage(w io.Writer) {
	fmt.Fprint(w, `bounty-core — find paid open-source bounties on GitHub

Usage:
  bounty-core scan [flags]

Flags:
  --min N            only keep bounties worth at least N USD (default 0)
  --max-comments N   only keep bounties with at most N comments (default -1)
  --lang LIST        comma separated GitHub languages (e.g. go,rust); repeatable
  --limit N          maximum bounties returned, 0 = no truncation (default 100)
  --source LIST      discovery sources: algora, label, title; repeatable (default all)
  --sort KEY         score, amount, comments, newest, oldest (default score)
  --pretty           indent JSON output
  --enrich MODE      repository language + open PR data: auto, on, off (default auto)
  --timeout DUR      overall timeout (default 120s)
  --max-retries N    retries per request (default 3)
  --verbose          progress information on stderr
  --json             no-op; JSON is the only output format

Environment:
  GITHUB_TOKEN       raises the rate limit to 5000 requests/hour and enables
                     automatic repository enrichment (auto mode)

Output:
  A JSON array on stdout. Diagnostics always go to stderr, so
  "bounty-core scan | jq" is safe:
    bounty-core scan --limit 20 | jq '.[] | select(.amount_usd >= 1000) | .url'

Exit codes: 0 success, 1 runtime error, 2 usage error, 130 interrupted.
`)
}
