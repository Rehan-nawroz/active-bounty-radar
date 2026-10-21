"""The ``bounty`` command.

Human mode renders a table on stdout; ``--json`` writes the engine's bytes
through unchanged so pipes into jq keep working. Everything advisory — engine
warnings, summary lines — goes to stderr.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sqlite3
import sys
from pathlib import Path

from rich.console import Console

from . import __version__, core_bridge, render, store, tui

SORT_KEYS = ("score", "amount", "comments", "newest", "oldest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bounty",
        description="Find and rank paid open-source bounties on GitHub.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="find and rank open bounties")
    _add_scan_flags(scan)
    scan.add_argument("--json", action="store_true",
                      help="print the engine's JSON unchanged")
    scan.add_argument("--new", action="store_true",
                      help="only bounties not seen in a previous scan")
    _add_local_flags(scan)

    dashboard = sub.add_parser("tui", help="full-screen dashboard: filter, sort, rescan")
    _add_scan_flags(dashboard, progress=False)
    _add_local_flags(dashboard)

    history = sub.add_parser("history", help="show past scans")
    history.add_argument("--limit", type=int, default=20, metavar="N",
                         help="how many scans to show (default 20)")
    history.add_argument("--json", action="store_true",
                         help="print the history as JSON")
    history.add_argument("--db", default=None, metavar="PATH",
                         help="history database location")

    return parser


def _add_scan_flags(parser: argparse.ArgumentParser, *, progress: bool = True) -> None:
    """The engine flags both human front ends accept."""
    parser.add_argument("--min", type=int, default=0, metavar="N",
                        help="only bounties worth at least N USD (default 0)")
    parser.add_argument("--max-comments", type=int, default=-1, metavar="N",
                        help="only bounties with at most N comments (-1 disables)")
    parser.add_argument("--lang", action="append", default=[], metavar="LANG",
                        help="restrict to a GitHub language; repeatable")
    parser.add_argument("--limit", type=int, default=100, metavar="N",
                        help="maximum bounties returned (default 100)")
    parser.add_argument("--source", action="append", default=[], metavar="SRC",
                        help="algora, label, title; repeatable (default all)")
    parser.add_argument("--sort", default="score", metavar="KEY",
                        help="score, amount, comments, newest, oldest (default score)")
    parser.add_argument("--enrich", default="auto", choices=("auto", "on", "off"),
                        help="repository language and open-PR data (default auto)")
    parser.add_argument("--timeout", default="120s", metavar="DUR",
                        help="overall scan timeout (default 120s)")
    parser.add_argument("--base-url", default=None, help=argparse.SUPPRESS)
    if progress:
        parser.add_argument("--verbose", action="store_true",
                            help="engine progress on stderr")


def _add_local_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="history database location")
    parser.add_argument("--core", default=None, metavar="PATH",
                        help="path to the bounty-core engine")


def _pipe_closed_exit(exc: OSError) -> int:
    """Exit quietly when the consumer closed the pipe, as in ``| head``.

    A traceback here is pure noise for the user, and on Windows the closed pipe
    surfaces as EINVAL rather than EPIPE. Anything else is re-raised.
    """
    if not isinstance(exc, BrokenPipeError) and exc.errno not in (errno.EPIPE, errno.EINVAL):
        raise exc

    # Point stdout at the null device so the interpreter's final flush does not
    # print "Exception ignored" on the way out.
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def _tune_streams() -> None:
    """Make stdout/stderr tolerate non-ASCII titles.

    Windows consoles default to a legacy code page (cp1252, cp437). GitHub
    titles routinely contain emoji, and without this a table render dies with
    UnicodeEncodeError instead of printing the bounty.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _tune_streams()
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        # The engine already reports the interrupt; do not add a traceback.
        print("bounty: interrupted", file=sys.stderr)
        return 130


def _dispatch(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "tui":
        return cmd_tui(args)
    if args.command == "history":
        return cmd_history(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


def _core_args(args: argparse.Namespace) -> list[str]:
    """Translate CLI flags into engine flags.

    Every value is passed explicitly so the CLI's defaults, not the engine's,
    decide what a scan does.
    """
    core_args = [
        "--limit", str(args.limit),
        "--sort", args.sort,
        "--min", str(args.min),
        "--max-comments", str(args.max_comments),
        "--enrich", args.enrich,
        "--timeout", args.timeout,
    ]
    for language in args.lang:
        core_args += ["--lang", language]
    for source in args.source:
        core_args += ["--source", source]
    if getattr(args, "verbose", False):
        core_args.append("--verbose")
    if args.base_url:
        core_args += ["--base-url", args.base_url]
    return core_args


def _validation_error(args: argparse.Namespace) -> str | None:
    if args.limit < 0:
        return "--limit must be 0 or greater"
    if args.min < 0:
        return "--min must be 0 or greater"
    if args.max_comments < -1:
        return "--max-comments must be -1 or greater"
    if args.sort not in SORT_KEYS:
        return f"--sort must be one of: {', '.join(SORT_KEYS)}"
    if getattr(args, "new", False) and getattr(args, "json", False):
        return "--new cannot be combined with --json; --json prints the engine output unchanged"
    return None


def _db_path(raw: str | None) -> Path:
    return Path(raw).expanduser() if raw else store.default_db_path()


def _open_store(raw: str | None) -> tuple[sqlite3.Connection | None, Path]:
    """Open the history database, reporting a bad path instead of crashing."""
    db_path = _db_path(raw)
    try:
        return store.connect(db_path), db_path
    except (sqlite3.Error, OSError) as exc:
        print(f"bounty: could not open the history database at {db_path}: {exc}",
              file=sys.stderr)
        return None, db_path


def cmd_scan(args: argparse.Namespace) -> int:
    problem = _validation_error(args)
    if problem:
        print(f"bounty: {problem}", file=sys.stderr)
        return 2

    try:
        core = core_bridge.find_core(args.core)
        raw = core_bridge.run_scan(core, _core_args(args))
    except core_bridge.BridgeError as exc:
        print(f"bounty: {exc}", file=sys.stderr)
        return exc.returncode

    try:
        bounties = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"bounty: the engine returned invalid JSON: {exc}", file=sys.stderr)
        return 1

    conn, db_path = _open_store(args.db)
    outcome = store.ScanOutcome(frozenset(), len(bounties))
    saved = False

    if conn is not None:
        try:
            outcome = store.record_scan(conn, bounties)
            saved = True
        except (sqlite3.Error, OSError) as exc:
            # History is a convenience: a broken database must not cost the user
            # the scan they just waited for.
            print(f"bounty: could not save this scan to {db_path}: {exc}", file=sys.stderr)
        finally:
            conn.close()

    if args.new and not saved:
        print("bounty: --new needs the history database, which could not be opened",
              file=sys.stderr)
        return 1

    summary = f"{len(bounties)} bounties"
    if saved:
        summary += f", {len(outcome.new_ids)} new"

    if args.json:
        try:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
        except OSError as exc:
            return _pipe_closed_exit(exc)
        print(f"bounty: {summary}", file=sys.stderr)
        return 0

    visible = [b for b in bounties if b["id"] in outcome.new_ids] if args.new else bounties
    if not visible:
        print(
            "bounty: no new bounties since the last scan" if args.new
            else "bounty: no bounties matched",
            file=sys.stderr,
        )
        return 0

    console = Console(highlight=False)
    try:
        console.print(render.bounty_table(visible, outcome.new_ids, console.width))
    except OSError as exc:
        return _pipe_closed_exit(exc)
    if saved:
        detail = f"{len(visible)} bounties, {len(outcome.new_ids)} new · {db_path}"
    else:
        detail = f"{len(visible)} bounties (history not saved)"
    print(f"bounty: {detail}", file=sys.stderr)
    return 0


def _tui_scan(args: argparse.Namespace, core: Path) -> tui.ScanRunner:
    """Build the dashboard's scan callable: engine first, then history.

    Each refresh is a real scan, so it is also recorded and its new bounties
    are reported — the dashboard is a view of history, not a way around it.
    """
    db_path = _db_path(args.db)
    core_args = _core_args(args)

    def scan() -> tui.ScanResult:
        output = core_bridge.run_scan_quiet(core, core_args)
        try:
            bounties = json.loads(output.stdout)
        except json.JSONDecodeError as exc:
            raise core_bridge.BridgeError(
                f"the engine returned invalid JSON: {exc}"
            ) from exc

        # The engine's warnings are captured in this mode; surface them in the
        # status bar rather than letting them print over the dashboard.
        notes = [output.stderr.strip()] if output.stderr.strip() else []

        try:
            conn = store.connect(db_path)
        except (sqlite3.Error, OSError) as exc:
            notes.append(f"history not saved: {exc}")
            return tui.ScanResult(bounties, frozenset(), " · ".join(notes))

        try:
            outcome = store.record_scan(conn, bounties)
        except (sqlite3.Error, OSError) as exc:
            notes.append(f"history not saved: {exc}")
            return tui.ScanResult(bounties, frozenset(), " · ".join(notes))
        finally:
            conn.close()

        return tui.ScanResult(bounties, outcome.new_ids, " · ".join(notes))

    return scan


def _has_tty() -> bool:
    return sys.stdout.isatty()


def cmd_tui(args: argparse.Namespace) -> int:
    problem = _validation_error(args)
    if problem:
        print(f"bounty: {problem}", file=sys.stderr)
        return 2

    if not _has_tty():
        print(
            "bounty: `bounty tui` needs an interactive terminal; "
            "use `bounty scan` for pipes and scripts",
            file=sys.stderr,
        )
        return 2

    try:
        core = core_bridge.find_core(args.core)
    except core_bridge.BridgeError as exc:
        print(f"bounty: {exc}", file=sys.stderr)
        return exc.returncode

    tui.run(
        _tui_scan(args, core),
        db_label=str(_db_path(args.db)),
        sort_key=args.sort,
    )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    if args.limit < 1:
        print("bounty: --limit must be 1 or greater", file=sys.stderr)
        return 2

    conn, db_path = _open_store(args.db)
    if conn is None:
        return 1
    try:
        scans = store.recent_scans(conn, args.limit)
    finally:
        conn.close()

    if args.json:
        try:
            json.dump(scans, sys.stdout, indent=2)
            sys.stdout.write("\n")
        except OSError as exc:
            return _pipe_closed_exit(exc)
        return 0

    if not scans:
        print(f"bounty: no scans recorded yet in {db_path} — run `bounty scan` first",
              file=sys.stderr)
        return 0

    try:
        Console(highlight=False).print(render.scans_table(scans))
    except OSError as exc:
        return _pipe_closed_exit(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
