"""End-to-end tests for the `bounty` command.

Each test spawns the CLI as a real process against the stub GitHub server, so
argument handling, stdout/stderr separation, exit codes and SQLite history are
all exercised the way a user hits them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from bounty import render

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def run_cli(binary: Path, *args: str, db: Path, text: bool = True, timeout: int = 90):
    env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    env["BOUNTY_CORE_BIN"] = str(binary)
    env["BOUNTY_DB"] = str(db)
    kwargs = {"capture_output": True, "timeout": timeout, "env": env}
    if text:
        kwargs.update(text=True, encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "bounty", *args], **kwargs)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "history.db"


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_scan_renders_a_table(binary, stub, db):
    proc = run_cli(binary, "scan", "--base-url", stub.url, db=db)

    assert proc.returncode == 0, proc.stderr
    # Amount formatted with a thousands separator, and every bounty marked new.
    assert "$10,000" in proc.stdout
    assert "acme/widget" in proc.stdout
    assert proc.stdout.count("*") == 3
    assert "3 bounties, 3 new" in proc.stderr
    assert db.is_file()


def test_scan_survives_emoji_titles(binary, stub, db):
    # The stub's second title contains an emoji; a legacy Windows code page
    # used to raise UnicodeEncodeError here.
    proc = run_cli(binary, "scan", "--base-url", stub.url, db=db)
    assert proc.returncode == 0, proc.stderr


def test_json_is_passed_through_byte_for_byte(binary, stub, db):
    engine = subprocess.run(
        [str(binary), "scan", "--base-url", stub.url],
        capture_output=True,
        timeout=60,
        env={k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}},
    )
    cli = run_cli(binary, "scan", "--json", "--base-url", stub.url, db=db, text=False)

    assert cli.returncode == 0, cli.stderr.decode("utf-8", "replace")
    assert cli.stdout == engine.stdout
    assert len(json.loads(cli.stdout)) == 3
    assert b"bounties" in cli.stderr  # summary stays on stderr


def test_second_scan_marks_nothing_new(binary, stub, db):
    run_cli(binary, "scan", "--base-url", stub.url, db=db)
    second = run_cli(binary, "scan", "--base-url", stub.url, db=db)

    assert second.returncode == 0, second.stderr
    assert "*" not in second.stdout
    assert "0 new" in second.stderr


def test_new_filter_hides_seen_bounties(binary, stub, db):
    run_cli(binary, "scan", "--base-url", stub.url, db=db)
    fresh = run_cli(binary, "scan", "--base-url", stub.url, "--new", db=db)

    assert fresh.returncode == 0
    assert fresh.stdout.strip() == ""
    assert "no new bounties" in fresh.stderr


def test_usage_errors_exit_two(binary, db):
    for args in (
        ["scan", "--limit", "-1"],
        ["scan", "--min", "-5"],
        ["scan", "--max-comments", "-2"],
        ["scan", "--sort", "bogus"],
        ["scan", "--new", "--json"],
        ["history", "--limit", "0"],
    ):
        proc = run_cli(binary, *args, db=db)
        assert proc.returncode == 2, f"{args} exited {proc.returncode}"
        assert proc.stdout == "", f"{args} wrote to stdout"


def test_early_closed_pipe_exits_quietly(binary, stub, db):
    """`bounty scan | head` must not print a traceback."""
    env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    env["BOUNTY_CORE_BIN"] = str(binary)

    proc = subprocess.Popen(
        [sys.executable, "-m", "bounty", "scan", "--base-url", stub.url, "--db", str(db)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    proc.stdout.close()  # the consumer goes away before output is written
    stderr = proc.stderr.read()
    code = proc.wait(timeout=60)

    assert code == 0, stderr.decode("utf-8", "replace")
    assert b"Traceback" not in stderr
    assert b"Exception ignored" not in stderr


def test_unusable_database_still_shows_results(binary, stub, tmp_path):
    # A directory is not a database file.
    broken = tmp_path / "not-a-db"
    broken.mkdir()

    proc = run_cli(binary, "scan", "--base-url", stub.url, db=broken)

    assert proc.returncode == 0, proc.stderr
    assert "$10,000" in proc.stdout
    assert "could not open the history database" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_new_with_unusable_database_fails(binary, stub, tmp_path):
    broken = tmp_path / "not-a-db"
    broken.mkdir()

    proc = run_cli(binary, "scan", "--base-url", stub.url, "--new", db=broken)

    assert proc.returncode == 1
    assert "--new needs the history database" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_interrupt_exits_130(monkeypatch, capsys):
    """Ctrl+C must match the documented exit code, not print a traceback."""
    from bounty import cli

    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_scan", interrupted)

    assert cli.main(["scan"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_missing_engine_is_reported(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}},
        "BOUNTY_CORE_BIN": str(tmp_path / "nope"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "bounty", "scan"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "BOUNTY_CORE_BIN" in proc.stderr


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------


def test_history_shows_recorded_scans(binary, stub, db):
    run_cli(binary, "scan", "--base-url", stub.url, db=db)
    run_cli(binary, "scan", "--base-url", stub.url, db=db)

    proc = run_cli(binary, "history", "--json", db=db)
    assert proc.returncode == 0, proc.stderr
    scans = json.loads(proc.stdout)
    assert len(scans) == 2
    assert [scan["bounty_count"] for scan in scans] == [3, 3]
    assert [scan["new_count"] for scan in scans] == [0, 3]  # newest first


def test_history_table_renders(binary, stub, db):
    run_cli(binary, "scan", "--base-url", stub.url, db=db)
    proc = run_cli(binary, "history", db=db)

    assert proc.returncode == 0
    assert "Bounties" in proc.stdout
    assert "New" in proc.stdout


def test_history_without_scans_explains_itself(binary, db):
    proc = run_cli(binary, "history", db=db)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "no scans recorded" in proc.stderr


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_table_fits_eighty_columns():
    rows = [
        {
            "id": "acme/widget#1",
            "amount_usd": 400,
            "repo": "acme/a-rather-long-repository-name",
            "title": "Bounty #30 Restaurant POS Interface ($400): scope and paid availability",
            "comments": 0,
            "open_prs": 0,
            "competition": "low",
        }
    ]

    for width in (80, 100, 120):
        buffer = StringIO()
        Console(file=buffer, width=width, highlight=False).print(
            render.bounty_table(rows, {"acme/widget#1"}, width)
        )
        body = buffer.getvalue().splitlines()[2]

        assert len(body) <= width, f"row overflows a {width}-column console"
        assert body.rstrip().endswith("low"), f"competition column cropped at {width}"
        assert body.startswith("*"), f"new marker cropped at {width}"


def test_money_formats_and_blanks():
    assert render.money(10000) == "$10,000"
    assert render.money(None) == "—"


# --------------------------------------------------------------------------
# Documentation stays true
# --------------------------------------------------------------------------


def _section(heading: str) -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index(heading)
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end == -1 else rest[:end]


def test_scan_help_flags_are_documented(binary, db):
    proc = run_cli(binary, "scan", "--help", db=db)
    assert proc.returncode == 0

    help_flags = set(re.findall(r"^\s+(--[a-z-]+)", proc.stdout, flags=re.MULTILINE))
    help_flags.discard("--help")

    engine_flags = set(
        re.findall(r"^\| `(--[a-z-]+)", _section("## Usage"), flags=re.MULTILINE)
    )
    cli_flags = set(
        re.findall(
            r"^\| `(--[a-z-]+)",
            _section("## Command-line front end"),
            flags=re.MULTILINE,
        )
    )

    undocumented = help_flags - (engine_flags | cli_flags)
    assert not undocumented, f"flags accepted but not documented: {sorted(undocumented)}"

    stale = cli_flags - help_flags
    assert not stale, f"documented but not accepted by `bounty scan`: {sorted(stale)}"
