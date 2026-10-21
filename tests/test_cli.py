"""Integration tests for the bounty-core engine.

These drive the built binary the way a user does: real argv, real process, real
stdout/stderr. The stub GitHub server comes from conftest.py.

Run with:
    .venv/Scripts/python -m pytest tests -q      # Windows
    .venv/bin/python -m pytest tests -q          # macOS / Linux
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def scan(binary: Path, *args: str, token: str | None = None, timeout: int = 60):
    import os

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


def test_empty_result_is_an_array(binary, stub_factory):
    empty = stub_factory(items=[])
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


def engine_section() -> str:
    """The engine's flag table only, so the CLI table cannot confuse parity."""
    text = README.read_text(encoding="utf-8")
    start = text.index("## Usage")
    end = text.index("## Output format")
    return text[start:end]


def documented_flags() -> set[str]:
    return set(re.findall(r"^\| `(--[a-z-]+)", engine_section(), flags=re.MULTILINE))


def documented_fields() -> list[str]:
    """The output-format table only, so later tables cannot look like fields."""
    text = README.read_text(encoding="utf-8")
    table = text.split("## Output format", 1)[1].split("\n## ", 1)[0]
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
    out = subprocess.run([jq, "length"], input=data, capture_output=True, text=True, encoding="utf-8", check=True)
    assert out.stdout.strip() == "3"


def test_readme_jq_shortlist(binary, stub, jq):
    program = '.[] | select(.amount_usd >= 1000) | "\\(.amount_usd)\\t\\(.competition)\\t\\(.url)"'
    data = json.dumps(run_json(binary, "scan", "--base-url", stub.url))
    out = subprocess.run([jq, "-r", program], input=data, capture_output=True, text=True, encoding="utf-8", check=True)

    lines = [line for line in out.stdout.splitlines() if line]
    # 2 comments → weight 2 → "medium" competition.
    assert lines == ["10000\tmedium\thttps://github.com/acme/widget/issues/1"], lines


def test_readme_jq_sort_by(binary, stub, jq):
    program = 'sort_by(-(.amount_usd // 0))[] | "\\(.amount_usd // "?")\\t\\(.repo) — \\(.title)"'
    data = json.dumps(run_json(binary, "scan", "--base-url", stub.url))
    out = subprocess.run([jq, "-r", program], input=data, capture_output=True, text=True, encoding="utf-8", check=True)

    lines = [line for line in out.stdout.splitlines() if line]
    assert len(lines) == 3
    assert lines[0].startswith("10000\t"), lines
    assert lines[-1].startswith("?\t"), lines
