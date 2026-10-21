"""The dashboard, driven headlessly the way Textual tests do.

The TUI is exercised through its real widget tree — keys, focus, filters and
the scan worker — rather than by calling its methods directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from textual.widgets import DataTable, Input, Static

from bounty import cli, core_bridge, render, tui

ROOT = Path(__file__).resolve().parents[1]


def bounty(
    number: int,
    *,
    amount: int | None,
    repo: str = "acme/widget",
    comments: int = 0,
    open_prs: int = 0,
    language: str = "Go",
    title: str | None = None,
    created_at: str = "2026-05-01T00:00:00Z",
    score: float | None = None,
) -> dict:
    weight = comments + 2 * open_prs
    return {
        "id": f"{repo}#{number}",
        "repo": repo,
        "number": number,
        "title": title or f"Bounty #{number}",
        "url": f"https://github.com/{repo}/issues/{number}",
        "amount_usd": amount,
        "currency": "USD",
        "source": "algora",
        "comments": comments,
        "open_prs": open_prs,
        "language": language,
        "created_at": created_at,
        "competition": "low",
        "score": score if score is not None else (amount or 0) / (1 + weight),
    }


ROWS = [
    bounty(1, amount=10_000, comments=2, title="[ Bounty $10K ] Fix the widget parser"),
    bounty(2, amount=200, repo="acme/docs", title="Bounty: USD 200 for docs"),
    bounty(3, amount=None, comments=9, title="Update the README"),
]


def result(rows=None, new_ids=None, note: str = "") -> tui.ScanResult:
    rows = list(ROWS if rows is None else rows)
    if new_ids is None:
        new_ids = {row["id"] for row in rows}
    return tui.ScanResult(rows, frozenset(new_ids), note)


def renderable(widget) -> str:
    buffer = StringIO()
    Console(file=buffer, width=80, color_system=None).print(widget.content)
    return buffer.getvalue()


def status(app) -> str:
    return renderable(app.query_one("#status", Static))


async def settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def test_dashboard_lists_the_scan():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_lists_the_scan(app))


async def _lists_the_scan(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        table = app.query_one(DataTable)
        assert table.row_count == 3
        assert "3 bounties" in status(app)
        assert "3 new" in status(app)
        assert app.selected == "acme/widget#1"
        assert "$10,000" in renderable(app.query_one("#detailbody", Static))


def test_scan_failure_is_reported_without_crashing():
    def boom():
        raise RuntimeError("every search failed: rate limited")

    app = tui.BountyTUI(boom)
    asyncio.run(_failure(app))


async def _failure(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        assert "every search failed" in status(app)
        assert app.query_one(DataTable).row_count == 0
        detail = renderable(app.query_one("#detailbody", Static))
        assert "Scan failed" in detail
        assert "every search failed" in detail


def test_refresh_runs_another_scan():
    calls: list[int] = []

    def scan() -> tui.ScanResult:
        calls.append(1)
        # The second scan finds the same bounties, so none are new any more.
        return result(new_ids=set() if len(calls) > 1 else None)

    app = tui.BountyTUI(scan)
    asyncio.run(_refresh(app, calls))


async def _refresh(app, calls) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert len(calls) == 1
        assert "3 new" in status(app)

        await pilot.press("r")
        await settle(app, pilot)

        assert len(calls) == 2
        assert "new" not in status(app)


def test_engine_warnings_land_in_the_status_bar():
    app = tui.BountyTUI(lambda: result(note="search failed: title"))
    asyncio.run(_warnings(app))


async def _warnings(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert "search failed: title" in status(app)


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


def test_filter_narrows_the_table_and_escape_clears_it():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_filtering(app))


async def _filtering(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        await pilot.press("/")
        await pilot.press(*"docs")
        await pilot.pause()

        table = app.query_one(DataTable)
        assert table.row_count == 1
        assert "1 shown" in status(app)
        assert app.selected == "acme/docs#2"

        await pilot.press("escape")
        await pilot.pause()

        assert table.row_count == 3
        assert app.query_one("#filter", Input).value == ""


def test_new_only_and_minimum_amount_filters():
    app = tui.BountyTUI(lambda: result(new_ids={"acme/docs#2"}))
    asyncio.run(_filters(app))


async def _filters(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one(DataTable)

        await pilot.press("n")
        assert table.row_count == 1
        assert "new only" in status(app)

        await pilot.press("n")
        await pilot.press("m")  # $100 floor: the unknown-amount bounty drops
        assert table.row_count == 2
        assert "min $100" in status(app)


def test_resize_applies_the_new_terminal_size():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_resizing(app))


async def _resizing(app) -> None:
    async with app.run_test(size=(120, 32)) as pilot:
        await settle(app, pilot)
        detail = app.query_one("#detail")
        table = app.query_one(DataTable)

        assert not detail.has_class("hidden")
        assert app._title_width == 26  # 120 - 58 fixed - 36 pane
        assert table.row_count == 3

        await pilot.resize_terminal(80, 24)
        await pilot.pause()

        # The pane yields and the title column absorbs its columns.
        assert detail.has_class("hidden")
        assert app._title_width == 22  # 80 - 58 fixed
        assert table.row_count == 3
        assert len(table.columns) == 7

        await pilot.resize_terminal(120, 32)
        await pilot.pause()

        assert not detail.has_class("hidden")
        assert app._title_width == 26
        assert table.row_count == 3


def test_sort_flag_seeds_the_dashboard():
    contested = bounty(1, amount=10_000, comments=9, title="Contested but rich")
    quiet = bounty(2, amount=5_000, comments=0, title="Cheap but quiet")
    # Amount order is #1 then #2; score order is the reverse.
    rows = [contested, quiet]

    app = tui.BountyTUI(lambda: tui.ScanResult(rows, frozenset(), ""), sort_key="amount")
    asyncio.run(_seeded_sort(app, rows))


async def _seeded_sort(app, rows) -> None:
    async with app.run_test(size=(120, 32)) as pilot:
        await settle(app, pilot)

        assert app.sort_key == "amount"
        assert [b["id"] for b in app.visible()] == [
            "acme/widget#1",
            "acme/widget#2",
        ]


def test_unknown_sort_key_falls_back_to_score():
    app = tui.BountyTUI(lambda: result(), sort_key="bogus")
    assert app.sort_key == "score"


def test_cmd_tui_passes_the_flags_to_the_dashboard(monkeypatch, tmp_path):
    captured: dict = {}

    monkeypatch.setattr(cli, "_has_tty", lambda: True)
    monkeypatch.setattr(core_bridge, "find_core", lambda explicit=None: tmp_path / "fake-core")
    monkeypatch.setattr(tui, "run", lambda scan, **kwargs: captured.update(kwargs))

    args = cli.build_parser().parse_args(
        ["tui", "--sort", "amount", "--db", str(tmp_path / "history.db")]
    )

    assert cli.cmd_tui(args) == 0
    assert captured["sort_key"] == "amount"
    assert str(captured["db_label"]).endswith("history.db")


def test_sort_cycles_and_reorders():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_sorting(app))


async def _sorting(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        assert [row["id"] for row in app.visible()][0] == "acme/widget#1"

        await pilot.press("s")  # score -> amount
        assert app.sort_key == "amount"
        assert [row["id"] for row in app.visible()] == [
            "acme/widget#1",
            "acme/docs#2",
            "acme/widget#3",
        ]

        await pilot.press("s")  # amount -> comments (least contested first)
        assert app.sort_key == "comments"
        assert app.visible()[0]["id"] == "acme/docs#2"


def test_language_cycle_only_offers_languages_present():
    rows = [
        bounty(1, amount=100, language="Go"),
        bounty(2, amount=50, language="Rust"),
        bounty(3, amount=None, language=""),
    ]
    app = tui.BountyTUI(lambda: result(rows))
    asyncio.run(_languages(app))


async def _languages(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one(DataTable)

        await pilot.press("l")
        assert app.language == "Go"
        assert table.row_count == 1

        await pilot.press("l")
        assert app.language == "Rust"

        await pilot.press("l")
        assert app.language == ""
        assert table.row_count == 3


def test_selection_updates_the_detail_pane():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_selection(app))


async def _selection(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        detail = app.query_one("#detailbody", Static)

        await pilot.press("down")
        await pilot.pause()

        assert app.selected == "acme/docs#2"
        assert "acme/docs#2" in renderable(detail)
        assert "USD 200 for docs" in renderable(detail)


def test_open_uses_the_selected_bounty_url():
    opened: list[str] = []
    app = tui.BountyTUI(lambda: result(), opener=opened.append)
    asyncio.run(_opening(app, opened))


async def _opening(app, opened) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        await pilot.press("o")
        await pilot.pause()

        assert opened == ["https://github.com/acme/widget/issues/1"]


def test_copy_does_not_crash_a_terminal_without_osc52():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_copying(app))


async def _copying(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("y")
        await pilot.pause()
        assert app.is_running


def test_detail_pane_can_be_hidden():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_hide_detail(app))


async def _hide_detail(app) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        pane = app.query_one("#detail")

        await pilot.press("d")
        assert pane.has_class("hidden")

        await pilot.press("d")
        assert not pane.has_class("hidden")


def test_scanning_on_a_small_terminal_keeps_every_column():
    app = tui.BountyTUI(lambda: result())
    asyncio.run(_small(app))


async def _small(app) -> None:
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)

        table = app.query_one(DataTable)
        assert len(table.columns) == 7
        title = table.columns["title"]
        assert 18 <= title.width < 80


# ---------------------------------------------------------------------------
# Ordering, filtering, rendering (no app needed)
# ---------------------------------------------------------------------------


def test_sort_orders_match_the_engine():
    newest_first = [
        bounty(1, amount=100, created_at="2026-05-01T00:00:00Z"),
        bounty(2, amount=50, created_at="2026-05-03T00:00:00Z"),
        bounty(3, amount=None, created_at="2026-05-02T00:00:00Z"),
    ]

    assert [b["id"] for b in tui.sort_bounties(newest_first, "amount")] == [
        "acme/widget#1",
        "acme/widget#2",
        "acme/widget#3",
    ]
    assert [b["id"] for b in tui.sort_bounties(newest_first, "newest")] == [
        "acme/widget#2",
        "acme/widget#3",
        "acme/widget#1",
    ]
    assert [b["id"] for b in tui.sort_bounties(newest_first, "oldest")] == [
        "acme/widget#1",
        "acme/widget#3",
        "acme/widget#2",
    ]

    contested = [
        bounty(1, amount=100, comments=9),
        bounty(2, amount=100, comments=0, open_prs=1),
        bounty(3, amount=100, comments=2),
    ]
    # weight = comments + 2 x open PRs: #2 and #3 tie at 2, and equal weights
    # fall back to the amount (equal here) and then the id.
    assert [b["id"] for b in tui.sort_bounties(contested, "comments")] == [
        "acme/widget#2",
        "acme/widget#3",
        "acme/widget#1",
    ]


def test_cells_are_trimmed_with_an_ellipsis():
    assert tui.fit("x" * 30, 10).plain == "xxxxxxxxx…"
    assert tui.fit("short", 10).plain == "short"
    # Wide characters count as two cells, so trimming stays inside the column.
    assert tui.fit("\U0001f48e" * 10, 6).cell_len <= 6


def test_detail_pane_shows_the_issue_path():
    buffer = StringIO()
    Console(file=buffer, width=40, color_system=None).print(render.detail(ROWS[0], True))
    body = buffer.getvalue()

    assert "$10,000" in body
    assert "acme/widget/issues/1" in body
    assert "https://" not in body


def test_matches_covers_every_filter():
    row = bounty(7, amount=1500, repo="acme/rocket", language="Rust")

    assert tui.matches(row)
    assert tui.matches(row, query="ROCKET")
    assert not tui.matches(row, query="docs")
    assert tui.matches(row, min_amount=1500)
    assert not tui.matches(row, min_amount=1501)
    assert tui.matches(row, language="rust")
    assert not tui.matches(row, language="Go")
    assert not tui.matches(row, new_only=True, new_ids={"other#1"})
    assert tui.matches(row, new_only=True, new_ids={"acme/rocket#7"})


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def run_cli(*args: str, env: dict | None = None, timeout: int = 60):
    environment = {k: v for k, v in os.environ.items() if k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "bounty", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=timeout,
    )


def test_tui_needs_a_terminal():
    proc = run_cli("tui")

    assert proc.returncode == 2
    assert "needs an interactive terminal" in proc.stderr
    assert proc.stdout == ""


def test_tui_help_lists_the_scan_flags():
    proc = run_cli("tui", "--help")

    assert proc.returncode == 0
    assert "--min" in proc.stdout
    assert "--db" in proc.stdout
    assert "--json" not in proc.stdout  # nothing to pipe in a dashboard


def test_tui_scan_runner_uses_the_engine_and_records_history(binary, stub, tmp_path):
    db = tmp_path / "history.db"
    args = cli.build_parser().parse_args(
        ["tui", "--base-url", stub.url, "--db", str(db)]
    )

    scan = cli._tui_scan(args, binary)
    first = scan()

    assert [b["id"] for b in first.bounties] == [
        "acme/widget#1",
        "acme/docs#2",
        "acme/docs#3",
    ]
    assert len(first.new_ids) == 3
    assert first.note == ""
    assert db.is_file()

    second = scan()
    assert second.new_ids == frozenset()
    assert json.loads(json.dumps(second.bounties)) == second.bounties


def test_dashboard_renders_a_real_engine_scan(binary, stub, tmp_path):
    db = tmp_path / "history.db"
    args = cli.build_parser().parse_args(
        ["tui", "--base-url", stub.url, "--db", str(db)]
    )
    app = tui.BountyTUI(cli._tui_scan(args, binary), db_label=str(db))

    asyncio.run(_real_scan(app, db))


async def _real_scan(app, db) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)

        assert app.query_one(DataTable).row_count == 3
        assert "3 new" in status(app)
        assert "$10,000" in renderable(app.query_one("#detailbody", Static))
        assert db.is_file()


def test_tui_scan_runner_survives_an_unusable_database(binary, stub, tmp_path):
    broken = tmp_path / "not-a-db"
    broken.mkdir()
    args = cli.build_parser().parse_args(["tui", "--base-url", stub.url, "--db", str(broken)])

    outcome = cli._tui_scan(args, binary)()

    assert len(outcome.bounties) == 3
    assert outcome.new_ids == frozenset()
    assert "history not saved" in outcome.note
