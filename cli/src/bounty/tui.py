"""The ``bounty tui`` dashboard.

A Textual front end over the same engine and history database as ``bounty
scan``: scan once, then filter, sort and read results without spending another
GitHub request. The scan itself is injected, so the dashboard never cares
whether it is talking to the real engine or to a stub.

Keys: ``/`` filter, ``s`` sort, ``m`` minimum amount, ``n`` new only,
``l`` language, ``d`` toggle the detail pane, ``r`` rescan, ``o`` open,
``y`` copy URL, ``q`` quit.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Resize
from textual.widgets import DataTable, Footer, Input, Static

from . import render

MIN_AMOUNTS = (0, 100, 500, 1000, 5000, 10000)
SORT_KEYS = ("score", "amount", "comments", "newest", "oldest")

# Layout, in table cells: (heading, column key, fixed width). The title column
# takes whatever is left, so its width is computed rather than listed.
REPO_WIDTH = 22
COLUMNS = (
    ("", "new", 1),
    ("Amount", "amount", 9),
    ("Repo", "repo", REPO_WIDTH),
    ("Title", "title", None),
    ("Cmt", "cmt", 4),
    ("PRs", "prs", 3),
    ("Comp", "comp", 5),
)
CELL_PADDING = 2  # one cell either side of every column
FIXED_WIDTH = sum(width for _, _, width in COLUMNS if width) + CELL_PADDING * len(COLUMNS)
MIN_TITLE_WIDTH = 18
DETAIL_WIDTH = 36
DETAIL_MIN_WIDTH = 110
SORT_LABELS = {
    "score": "score",
    "amount": "amount",
    "comments": "least contested",
    "newest": "newest",
    "oldest": "oldest",
}


@dataclass(frozen=True)
class ScanResult:
    """One scan, as the dashboard needs it."""

    bounties: list[dict] = field(default_factory=list)
    new_ids: frozenset[str] = frozenset()
    note: str = ""


ScanRunner = Callable[[], ScanResult]


# ---------------------------------------------------------------------------
# Ordering and filtering
# ---------------------------------------------------------------------------


def _created(bounty: dict) -> str:
    return str(bounty.get("created_at") or "")


def _weight(bounty: dict) -> int:
    return int(bounty.get("comments") or 0) + 2 * int(bounty.get("open_prs") or 0)


def sort_bounties(rows: list[dict], key: str) -> list[dict]:
    """Order rows the way the engine's ``--sort`` does.

    The TUI re-sorts locally because changing the order must not cost another
    GitHub request. Ties are broken exactly as in ``core/internal/rank``.
    """
    ordered = sorted(rows, key=lambda b: b.get("id") or "")

    if key == "newest":
        ordered.sort(key=_created, reverse=True)
    elif key == "oldest":
        ordered.sort(key=_created)
    elif key == "comments":
        ordered.sort(key=lambda b: (b.get("amount_usd") is None, -(b.get("amount_usd") or 0)))
        ordered.sort(key=_weight)
    elif key == "amount":
        ordered.sort(key=lambda b: b.get("score") or 0, reverse=True)
        ordered.sort(key=lambda b: (b.get("amount_usd") is None, -(b.get("amount_usd") or 0)))
    else:  # score
        ordered.sort(key=lambda b: (b.get("amount_usd") is None, -(b.get("amount_usd") or 0)))
        ordered.sort(key=lambda b: b.get("score") or 0, reverse=True)
    return ordered


def fit(value: str, width: int) -> Text:
    """Trim a cell to its column, ellipsis included.

    DataTable crops silently, which reads as a rendering bug; an ellipsis makes
    the truncation look deliberate, and Text.truncate measures display cells
    rather than characters, so wide emoji do not overflow the column.
    """
    text = Text(value)
    if text.cell_len > width:
        text.truncate(width, overflow="ellipsis")
    return text


def matches(
    bounty: dict,
    *,
    query: str = "",
    min_amount: int = 0,
    language: str = "",
    new_only: bool = False,
    new_ids: frozenset[str] | set[str] = frozenset(),
) -> bool:
    if new_only and bounty.get("id") not in new_ids:
        return False
    if min_amount and int(bounty.get("amount_usd") or 0) < min_amount:
        return False
    if language and str(bounty.get("language") or "").lower() != language.lower():
        return False
    if query:
        haystack = " ".join(
            str(bounty.get(name) or "") for name in ("repo", "title", "id")
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


# ---------------------------------------------------------------------------
# Entry point for tests and the CLI
# ---------------------------------------------------------------------------


def run(
    scan: ScanRunner,
    *,
    db_label: str = "",
    opener: Callable[[str], object] = webbrowser.open,
    sort_key: str = "score",
) -> None:
    BountyTUI(scan, db_label=db_label, opener=opener, sort_key=sort_key).run()


class BountyTUI(App[None]):
    """Sortable, filterable view of one scan."""

    TITLE = "Bounty Radar"
    # Only the footer docks. Several widgets docked to the same edge overlap
    # in this Textual release, so the top bar, filter and status are all part
    # of the normal vertical flow.
    CSS = """
    Screen { background: $surface; }

    #topbar {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-style: bold;
    }

    #filter {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
    }

    #body { height: 1fr; }

    #table { width: 1fr; height: 1fr; }

    #detail {
        width: 36;
        height: 1fr;
        border-left: solid $primary;
        padding: 0 1;
        background: $surface;
    }

    #detail.hidden { display: none; }

    #detailbody { width: 1fr; height: auto; }

    #status {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("/", "filter", "Filter", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("m", "cycle_min", "Min $", show=True),
        Binding("n", "toggle_new", "New only", show=True),
        Binding("l", "cycle_language", "Language", show=True),
        Binding("d", "toggle_detail", "Detail", show=True),
        Binding("r", "rescan", "Refresh", show=True),
        Binding("o", "open_bounty", "Open", show=True),
        Binding("y", "copy_url", "Copy URL", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "clear_filters", "Clear", show=False),
    ]

    def __init__(
        self,
        scan: ScanRunner,
        *,
        db_label: str = "",
        opener: Callable[[str], object] = webbrowser.open,
        sort_key: str = "score",
    ) -> None:
        super().__init__()
        self.scan = scan
        self.db_label = db_label
        self.opener = opener

        self.bounties: list[dict] = []
        self.new_ids: frozenset[str] = frozenset()
        self.selected: str | None = None

        self.query_text = ""
        self.min_amount = 0
        self.new_only = False
        self.language = ""

        self.sort_key = sort_key if sort_key in SORT_KEYS else "score"
        self.scanning = False
        self.stamp = ""
        self.problem = ""
        self._title_width = 0
        # None means "whatever the window is wide enough for".
        self.detail_override: bool | None = None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(
            Text.assemble(
                (" BOUNTY RADAR", "bold"),
                ("  paid open-source bounties", "dim"),
            ),
            id="topbar",
        )
        yield Input(
            placeholder="filter by title, repository or issue number  (esc clears)",
            id="filter",
        )
        with Horizontal(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail"):
                yield Static(id="detailbody")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_layout(self.size.width)
        self.query_one(DataTable).focus()
        self._refresh_status()
        self._start_scan()

    def on_resize(self, event: Resize) -> None:
        # The title column absorbs whatever the fixed columns leave behind, and
        # the detail pane gives way on narrow terminals. The event carries the
        # new size; self.size is still the old one inside this handler.
        width = event.size.width
        if not self._title_width:
            return
        hidden = self.query_one("#detail").has_class("hidden")
        if (
            (not self._detail_fits(width)) != hidden
            or self._width_for_title(width) != self._title_width
        ):
            self._apply_layout(width)
            self._rebuild()

    def _detail_fits(self, width: int) -> bool:
        if self.detail_override is not None:
            return self.detail_override
        return width >= DETAIL_MIN_WIDTH

    def _width_for_title(self, width: int) -> int:
        pane = 0 if self.query_one("#detail").has_class("hidden") else DETAIL_WIDTH
        return max(MIN_TITLE_WIDTH, width - FIXED_WIDTH - pane)

    def _apply_layout(self, width: int | None = None) -> None:
        width = self.size.width if width is None else width
        self.query_one("#detail").set_class(not self._detail_fits(width), "hidden")
        self._build_columns(width)

    def _build_columns(self, width: int) -> None:
        table = self.query_one(DataTable)
        self._title_width = self._width_for_title(width)
        table.clear(columns=True)
        for heading, key, column_width in COLUMNS:
            table.add_column(
                heading,
                key=key,
                width=self._title_width if column_width is None else column_width,
            )

    # -- scanning ----------------------------------------------------------

    def _start_scan(self) -> None:
        if self.scanning:
            self.notify("a scan is already running", severity="warning")
            return
        self.scanning = True
        self.problem = ""
        self._refresh_status()
        self._scan_worker()

    @work(thread=True, exclusive=True, group="scan")
    def _scan_worker(self) -> None:
        try:
            result = self.scan()
        except Exception as exc:  # noqa: BLE001 - shown in the status bar
            self._deliver(self._scan_failed, exc)
        else:
            self._deliver(self._scan_finished, result)

    def _deliver(self, callback, *args) -> None:
        """Hand a worker result back to the UI thread, if there still is one."""
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            pass  # the dashboard closed while the scan was in flight

    def _scan_finished(self, result: ScanResult) -> None:
        self.bounties = list(result.bounties)
        self.new_ids = frozenset(result.new_ids)
        self.problem = result.note
        self.stamp = datetime.now().strftime("%H:%M:%S")
        self.scanning = False

        known = {b.get("language") or "" for b in self.bounties}
        if self.language and self.language not in known:
            self.language = ""

        self._rebuild()
        if self.new_ids:
            self.notify(f"{len(self.new_ids)} new bounties")

    def _scan_failed(self, exc: Exception) -> None:
        self.scanning = False
        self.problem = str(exc) or exc.__class__.__name__
        self._refresh_status()
        self._refresh_detail()
        self.notify(self.problem, title="scan failed", severity="error", timeout=8)

    # -- the table ---------------------------------------------------------

    def visible(self) -> list[dict]:
        rows = [
            b
            for b in self.bounties
            if matches(
                b,
                query=self.query_text,
                min_amount=self.min_amount,
                language=self.language,
                new_only=self.new_only,
                new_ids=self.new_ids,
            )
        ]
        return sort_bounties(rows, self.sort_key)

    def _cells(self, bounty: dict) -> tuple:
        is_new = bounty.get("id") in self.new_ids
        competition = bounty.get("competition") or "unknown"
        return (
            Text("*", style="bold yellow") if is_new else Text(""),
            Text(render.money(bounty.get("amount_usd")), justify="right"),
            fit(str(bounty.get("repo") or ""), REPO_WIDTH),
            fit(str(bounty.get("title") or ""), self._title_width),
            Text(str(bounty.get("comments") or 0), justify="right"),
            Text(str(bounty.get("open_prs") or 0), justify="right"),
            Text(competition, style=render.COMPETITION_STYLE.get(competition, "white")),
        )

    def _rebuild(self) -> None:
        table = self.query_one(DataTable)
        rows = self.visible()
        ids = [b.get("id") for b in rows]

        previous = self.selected
        table.clear(columns=False)
        for bounty in rows:
            table.add_row(*self._cells(bounty), key=str(bounty.get("id")))

        if not rows:
            self.selected = None
        else:
            index = ids.index(previous) if previous in ids else 0
            self.selected = ids[index]
            table.move_cursor(row=index)

        self._refresh_status(shown=len(rows))
        self._refresh_detail()

    def _selected_bounty(self) -> dict | None:
        for bounty in self.visible():
            if bounty.get("id") == self.selected:
                return bounty
        return None

    def _refresh_detail(self) -> None:
        pane = self.query_one("#detailbody", Static)
        bounty = self._selected_bounty()
        if bounty is None:
            if self.bounties:
                hint = "Nothing matches these filters.\n\nPress escape to clear them."
            elif self.scanning:
                hint = "Scanning GitHub…"
            elif self.problem:
                # The status bar only has one line; the cause belongs here.
                hint = f"Scan failed.\n\n{self.problem}\n\nPress r to try again."
            else:
                hint = "No bounties to show."
            pane.update(Text(hint, style="dim"))
            return
        pane.update(render.detail(bounty, bounty.get("id") in self.new_ids))

    def _refresh_status(self, shown: int | None = None) -> None:
        parts: list[str] = []
        if self.scanning:
            parts.append("scanning…")
        else:
            parts.append(f"{len(self.bounties)} bounties")
            if self.new_ids:
                parts.append(f"{len(self.new_ids)} new")
            if shown is not None and shown != len(self.bounties):
                parts.append(f"{shown} shown")

        if self.min_amount:
            parts.append(f"min ${self.min_amount:,}")
        if self.new_only:
            parts.append("new only")
        if self.language:
            parts.append(self.language)
        if self.query_text:
            parts.append(f"“{self.query_text}”")
        parts.append(f"sort {SORT_LABELS.get(self.sort_key, self.sort_key)}")
        if self.db_label:
            parts.append(self.db_label)
        if self.stamp:
            parts.append(self.stamp)
        if self.problem:
            parts.append(self.problem)

        status = Text(" · ".join(parts), style="red" if self.problem else "")
        self.query_one("#status", Static).update(status)

    # -- events ------------------------------------------------------------

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value.strip()
        self._rebuild()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self.query_one(DataTable).focus()

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.selected = str(event.row_key.value)
            self._refresh_detail()

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        key = event.column_key.value if event.column_key is not None else None
        target = {"amount": "amount", "cmt": "comments"}.get(str(key))
        if target:
            self.sort_key = target
            self._rebuild()

    # -- actions -----------------------------------------------------------

    def action_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_cycle_sort(self) -> None:
        index = (SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)
        self.sort_key = SORT_KEYS[index]
        self._rebuild()

    def action_cycle_min(self) -> None:
        index = (MIN_AMOUNTS.index(self.min_amount) + 1) % len(MIN_AMOUNTS)
        self.min_amount = MIN_AMOUNTS[index]
        self._rebuild()

    def action_toggle_new(self) -> None:
        self.new_only = not self.new_only
        self._rebuild()

    def action_cycle_language(self) -> None:
        languages = sorted({b.get("language") or "" for b in self.bounties})
        choices = [""] + [lang for lang in languages if lang]
        index = choices.index(self.language) if self.language in choices else 0
        self.language = choices[(index + 1) % len(choices)]
        self._rebuild()

    def action_toggle_detail(self) -> None:
        # The first press overrides the automatic choice for this session.
        self.detail_override = not self._detail_fits(self.size.width)
        self._apply_layout()
        self._rebuild()

    def action_rescan(self) -> None:
        self._start_scan()

    def action_open_bounty(self) -> None:
        bounty = self._selected_bounty()
        if bounty is None or not bounty.get("url"):
            self.notify("select a bounty first", severity="warning")
            return
        try:
            self.opener(str(bounty["url"]))
        except Exception as exc:  # noqa: BLE001 - a missing browser is not fatal
            self.notify(f"could not open the browser: {exc}", severity="error")

    def action_copy_url(self) -> None:
        bounty = self._selected_bounty()
        if bounty is None or not bounty.get("url"):
            self.notify("select a bounty first", severity="warning")
            return
        try:
            self.copy_to_clipboard(str(bounty["url"]))
        except Exception as exc:  # noqa: BLE001 - terminal may not support OSC 52
            self.notify(f"clipboard unavailable: {exc}", severity="warning")
            return
        self.notify("URL copied")

    def action_clear_filters(self) -> None:
        self.query_text = ""
        self.min_amount = 0
        self.new_only = False
        self.language = ""
        self.query_one("#filter", Input).value = ""
        self.query_one(DataTable).focus()
        self._rebuild()
