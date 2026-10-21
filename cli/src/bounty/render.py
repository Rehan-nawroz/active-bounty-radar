"""Human-readable tables."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

COMPETITION_STYLE = {"low": "green", "medium": "yellow", "high": "red"}


def money(amount: int | None) -> str:
    return "—" if amount is None else f"${amount:,.0f}"


def bounty_table(rows: list[dict], new_ids: frozenset[str] | set[str], width: int = 100) -> Table:
    """Build the results table for a console of the given width.

    Rich crops anything that overflows the console, which silently eats the
    right-hand columns on an 80-column terminal. Everything except the title
    occupies 62 cells at its widest (marker, amount, repo, comment/PR/contention
    columns and their padding), so the title gets the remainder.
    """
    title_width = max(12, min(52, width - 62))

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("", width=1)
    table.add_column("Amount", justify="right", no_wrap=True, min_width=7)
    # One line per bounty: wrapping titles turns a full scan into an unreadable
    # tower of words, so every text column truncates instead.
    table.add_column("Repo", no_wrap=True, overflow="ellipsis", max_width=26)
    table.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=title_width)
    table.add_column("Cmt", justify="right", no_wrap=True)
    table.add_column("PRs", justify="right", no_wrap=True)
    table.add_column("Comp", no_wrap=True)

    for row in rows:
        is_new = row["id"] in new_ids
        competition = row.get("competition") or "unknown"
        style = COMPETITION_STYLE.get(competition, "white")

        table.add_row(
            # ASCII on purpose: Windows consoles still default to a legacy code
            # page, where a star glyph either crashes or renders as mojibake.
            "*" if is_new else "",
            money(row.get("amount_usd")),
            row.get("repo") or "",
            row.get("title") or "",
            str(row.get("comments", 0)),
            str(row.get("open_prs", 0)),
            f"[{style}]{competition}[/]",
            style="bold" if is_new else None,
        )
    return table


def _stamp(raw: str | None) -> str:
    if not raw:
        return "unknown"
    return str(raw).replace("T", " ")[:16]


def detail(bounty: dict, is_new: bool = False) -> Group:
    """Everything known about one bounty, for the dashboard's side pane."""
    competition = bounty.get("competition") or "unknown"
    style = COMPETITION_STYLE.get(competition, "white")

    heading = Text()
    if is_new:
        heading.append("NEW  ", style="bold yellow")
    heading.append(str(bounty.get("id") or "?"), style="bold")

    title = Text(bounty.get("title") or "(untitled)", style="bold white")

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("Amount", money(bounty.get("amount_usd")))
    grid.add_row("Competition", Text(competition, style=style))
    score = bounty.get("score")
    grid.add_row("Score", "—" if score is None else f"{score:,.0f}")
    grid.add_row("Comments", str(bounty.get("comments") or 0))
    grid.add_row("Open PRs", str(bounty.get("open_prs") or 0))
    grid.add_row("Language", bounty.get("language") or "unknown")
    grid.add_row("Source", bounty.get("source") or "unknown")
    grid.add_row("Opened", _stamp(bounty.get("created_at")))

    # The full URL rarely fits a side pane; the issue path is the part that
    # identifies it, and ``y`` copies the whole thing.
    url = str(bounty.get("url") or "")
    shown = url.removeprefix("https://github.com/") if url else "no link"
    hint = Text("o open · y copy URL", style="dim")
    return Group(
        heading,
        title,
        Text(""),
        grid,
        Text(""),
        Text(shown, style="underline cyan"),
        hint,
    )


def scans_table(scans: list[dict]) -> Table:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("When", no_wrap=True)
    table.add_column("Bounties", justify="right", no_wrap=True)
    table.add_column("New", justify="right", no_wrap=True)

    for scan in scans:
        ran_at = str(scan.get("ran_at", "")).replace("T", " ")[:19]
        table.add_row(ran_at, str(scan["bounty_count"]), str(scan["new_count"]))
    return table
