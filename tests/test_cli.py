"""Integration tests for the bounty-core CLI.

These drive the built binary the way a user does: real argv, real process, real
stdout/stderr. A local stub stands in for api.github.com so the tests are fast
and never consume GitHub's rate limit.

Run with:
    uv venv .venv
    uv pip install --python .venv -r requirements-dev.txt
    .venv/Scripts/python -m pytest tests -q      # Windows
    .venv/bin/python -m pytest tests -q          # macOS / Linux
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

# Three bounties: a $10K one, a $200 one written with a currency code, and one
# with no amount at all. Amounts deliberately use the forms that trip naive
# parsers.
ITEMS = [
    {
        "number": 1,
        "title": "[ Bounty $10K ] Fix the widget parser",
        "html_url": "https://github.com/acme/widget/issues/1",
        "repository_url": "https://api.github.com/repos/acme/widget",
        "comments": 2,
        "created_at": "2026-05-01T00:00:00Z",
        "labels": [{"name": "\U0001f48e Bounty"}],
    },
    {
        "number": 2,
        "title": "Bounty: USD 200 for docs",
        "html_url": "https://github.com/acme/docs/issues/2",
        "repository_url": "https://api.github.com/repos/acme/docs",
        "comments": 0,
        "created_at": "2026-05-02T00:00:00Z",
        "labels": [{"name": "bounty"}],
    },
    {
        "number": 3,
        "title": "Update the README",
        "html_url": "https://github.com/acme/docs/issues/3",
        "repository_url": "https://api.github.com/repos/acme/docs",
        "comments": 12,
        "created_at": "2026-05-03T00:00:00Z",
        "labels": [{"name": "bounty"}],
    },
]


class StubGitHub:
    """A tiny stand-in for the GitHub search and repo APIs."""

    def __init__(self, items=None):
        self.items = ITEMS if items is None else items
        self.requests: list[str] = []
        self.script: list[tuple[int, dict, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                outer.requests.append(self.path)

                if outer.script:
                    status, headers, body = outer.script.pop(0)
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if body:
                        self.wfile.write(body.encode())
                    return

                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]

                if parsed.path == "/search/issues":
                    payload = {
                        "total_count": len(outer.matching(query)),
                        "incomplete_results": False,
                        "items": outer.matching(query),
                    }
                elif parsed.path.startswith("/repos/"):
                    payload = {"language": "Go"}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # silence request logging
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def matching(self, query: str) -> list[dict]:
        if "\U0001f48e Bounty" in query:
            return [self.items[0]]
        if "in:title" in query or "label:bounty" in query:
            return self.items[1:]
        return []

    def fail_next(self, status: int, headers=None, body: str = ""):
        self.script.append((status, headers or {}, body))

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _binary_path() -> Path:
    override = os.environ.get("BOUNTY_CORE_BIN")
    if override:
        return Path(override)

    name = "bounty-core.exe" if os.name == "nt" else "bounty-core"
    built = ROOT / "bin" / name
    if built.exists():
        return built

    go = os.environ.get("GO") or shutil.which("go")
    if not go:
        pytest.fail(
            "bounty-core binary not found and no Go toolchain on PATH; "
            "build it with: cd core && go build -o ../bin/bounty-core ."
        )

    built.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([go, "build", "-o", str(built), "."], cwd=ROOT / "core", check=True)
    return built


@pytest.fixture(scope="session")
def binary() -> Path:
    return _binary_path()


@pytest.fixture
def stub():
    with StubGitHub() as server:
        yield server


@pytest.fixture(scope="session")
def jq() -> str:
    found = os.environ.get("JQ") or shutil.which("jq")
    if not found:
        pytest.skip("jq is not installed (set JQ=/path/to/jq to run these tests)")
    return found


def scan(binary: Path, *args: str, token: str | None = None, timeout: int = 60):
    env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    if token:
        env["GITHUB_TOKEN"] = token
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=env,
    )


def run_json(binary: Path, *args: str, token: str | None = None) -> list[dict]:
    proc = scan(binary, *args, token=token)
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# Discovery and parsing
# --------------------------------------------------------------------------


def test_scan_emits_pure_json(binary, stub):
    proc = scan(binary, "scan", "--base-url", stub.url, "--limit", "10")

    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)  # stdout must be JSON and nothing else
    assert len(rows) == 3
    assert proc.stderr == "", f"quiet scan should not write to stderr: {proc.stderr!r}"

    by_number = {row["number"]: row for row in rows}
    assert by_number[1]["amount_usd"] == 10000, "the $10K bug"
    assert by_number[1]["source"] == "algora"
    assert by_number[2]["amount_usd"] == 200
    assert by_number[3]["amount_usd"] is None


def test_empty_result_is_an_array(binary):
    with StubGitHub(items=[]) as empty:
        proc = scan(binary, "scan", "--base-url", empty.url)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "[]"


def test_filters_apply(binary, stub):
    rows = run_json(binary, "scan", "--base-url", stub.url, "--min", "500")
    assert {row["number"] for row in rows} == {1}

    # Issue 1 has 2 comments, issue 3 has 12: only the quiet $200 one survives.
    rows = run_json(binary, "scan", "--base-url", stub.url, "--max-comments", "1")
    assert {row["number"] for row in rows} == {2}


def test_limit_truncates(binary, stub):
    rows = run_json(binary, "scan", "--base-url", stub.url, "--limit", "2")
    assert len(rows) == 2


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_usage_errors_exit_two(binary):
    cases = [
        [],
        ["frobnicate"],
        ["scan", "--sort", "bogus"],
        ["scan", "--limit", "-5"],
        ["scan", "--max-comments", "-7"],
        ["scan", "--timeout", "0s"],
        ["scan", "--source", "nope"],
    ]
    for args in cases:
        proc = scan(binary, *args)
        assert proc.returncode == 2, f"{args} exited {proc.returncode}"
        assert proc.stdout == "", f"{args} wrote to stdout"
        assert proc.stderr, f"{args} produced no explanation"


def test_every_search_failing_is_compact(binary):
    proc = scan(binary, "scan", "--base-url", "http://127.0.0.1:9", "--timeout", "10s")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "every search failed" in proc.stderr
    # The same cause from three sources must be reported once.
    assert proc.stderr.count("127.0.0.1:9") == 1, proc.stderr
    assert "context deadline exceeded" not in proc.stderr


def test_partial_failure_still_exits_zero(binary, stub):
    stub.fail_next(422, body='{"message":"Validation Failed"}')
    proc = scan(binary, "scan", "--base-url", stub.url)
    rows = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert len(rows) == 2
    assert "search failed" in proc.stderr


def test_rate_limit_is_retried(binary, stub):
    stub.fail_next(429, {"Retry-After": "1"})
    proc = scan(binary, "scan", "--base-url", stub.url, "--timeout", "30s")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)
    assert "rate limited" in proc.stderr


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------


def test_token_enables_enrichment(binary, stub):
    rows = run_json(binary, "scan", "--base-url", stub.url, token="test-token")
    assert all(row["language"] == "Go" for row in rows), rows


def test_tokenless_enrichment_note(binary, stub):
    proc = scan(binary, "scan", "--base-url", stub.url, "--enrich", "on")
    assert proc.returncode == 0
    assert "without GITHUB_TOKEN" in proc.stderr


# --------------------------------------------------------------------------
# Documentation stays true
# --------------------------------------------------------------------------


def documented_flags() -> set[str]:
    text = README.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `(--[a-z-]+)", text, flags=re.MULTILINE))


def documented_fields() -> list[str]:
    text = README.read_text(encoding="utf-8")
    table = text.split("## Output format", 1)[1]
    return re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.MULTILINE)


def test_every_help_flag_is_documented(binary):
    proc = scan(binary, "--help")
    assert proc.returncode == 0
    help_flags = set(re.findall(r"^  (--[a-z-]+)", proc.stdout, flags=re.MULTILINE))
    assert help_flags == documented_flags(), (
        f"help only: {help_flags - documented_flags()}; "
        f"README only: {documented_flags() - help_flags}"
    )


def test_every_documented_field_is_emitted(binary, stub):
    rows = run_json(binary, "scan", "--base-url", stub.url)
    fields = documented_fields()
    assert fields, "no field rows parsed out of the README"
    missing = [field for field in fields if field not in rows[0]]
    assert not missing, f"documented but missing from output: {missing}"


# --------------------------------------------------------------------------
# The README's jq examples
# --------------------------------------------------------------------------


def test_readme_jq_length(binary, stub, jq):
    data = json.dumps(run_json(binary, "scan", "--base-url", stub.url))
    out = subprocess.run([jq, "length"], input=data, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "3"


def test_readme_jq_shortlist(binary, stub, jq):
    program = '.[] | select(.amount_usd >= 1000) | "\\(.amount_usd)\\t\\(.competition)\\t\\(.url)"'
    data = json.dumps(run_json(binary, "scan", "--base-url", stub.url))
    out = subprocess.run([jq, "-r", program], input=data, capture_output=True, text=True, check=True)

    lines = [line for line in out.stdout.splitlines() if line]
    # 2 comments → weight 2 → "medium" competition.
    assert lines == ["10000\tmedium\thttps://github.com/acme/widget/issues/1"], lines


def test_readme_jq_sort_by(binary, stub, jq):
    program = 'sort_by(-(.amount_usd // 0))[] | "\\(.amount_usd // "?")\\t\\(.repo) — \\(.title)"'
    data = json.dumps(run_json(binary, "scan", "--base-url", stub.url))
    out = subprocess.run([jq, "-r", program], input=data, capture_output=True, text=True, check=True)

    lines = [line for line in out.stdout.splitlines() if line]
    assert len(lines) == 3
    assert lines[0].startswith("10000\t"), lines
    assert lines[-1].startswith("?\t"), lines
