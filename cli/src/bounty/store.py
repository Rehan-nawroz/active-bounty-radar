"""Scan history in SQLite.

The engine is stateless: every scan is a fresh view of GitHub. This module
remembers what a previous scan saw so the CLI can highlight new bounties and
answer "what changed since last time?".
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bounties (
    id            TEXT PRIMARY KEY,
    repo          TEXT NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    amount_usd    INTEGER,
    currency      TEXT,
    source        TEXT,
    comments      INTEGER,
    open_prs      INTEGER,
    language      TEXT,
    created_at    TEXT,
    competition   TEXT,
    score         REAL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS bounties_repo ON bounties (repo);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at       TEXT NOT NULL,
    bounty_count INTEGER NOT NULL,
    new_count    INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ScanOutcome:
    """What recording a scan learned."""

    new_ids: frozenset[str]
    total: int


def default_db_path() -> Path:
    """Per-user data directory, overridable with BOUNTY_DB."""
    override = os.environ.get("BOUNTY_DB")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")

    return base / "bounty-radar" / "history.db"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_scan(
    conn: sqlite3.Connection,
    bounties: list[dict],
    ran_at: datetime | None = None,
) -> ScanOutcome:
    """Store a scan, returning which bounties had never been seen before."""
    stamp = (ran_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    new_ids: set[str] = set()

    with conn:
        for bounty in bounties:
            known = conn.execute(
                "SELECT 1 FROM bounties WHERE id = ?", (bounty["id"],)
            ).fetchone()

            if known is None:
                new_ids.add(bounty["id"])
                conn.execute(
                    """
                    INSERT INTO bounties (
                        id, repo, number, title, url, amount_usd, currency, source,
                        comments, open_prs, language, created_at, competition, score,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bounty["id"],
                        bounty.get("repo"),
                        bounty.get("number"),
                        bounty.get("title"),
                        bounty.get("url"),
                        bounty.get("amount_usd"),
                        bounty.get("currency"),
                        bounty.get("source"),
                        bounty.get("comments"),
                        bounty.get("open_prs"),
                        bounty.get("language"),
                        bounty.get("created_at"),
                        bounty.get("competition"),
                        bounty.get("score"),
                        stamp,
                        stamp,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE bounties SET
                        title = ?, url = ?, amount_usd = ?, currency = ?, source = ?,
                        comments = ?, open_prs = ?, language = ?, competition = ?,
                        score = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        bounty.get("title"),
                        bounty.get("url"),
                        bounty.get("amount_usd"),
                        bounty.get("currency"),
                        bounty.get("source"),
                        bounty.get("comments"),
                        bounty.get("open_prs"),
                        bounty.get("language"),
                        bounty.get("competition"),
                        bounty.get("score"),
                        stamp,
                        bounty["id"],
                    ),
                )

        conn.execute(
            "INSERT INTO scans (ran_at, bounty_count, new_count) VALUES (?, ?, ?)",
            (stamp, len(bounties), len(new_ids)),
        )

    return ScanOutcome(new_ids=frozenset(new_ids), total=len(bounties))


def recent_scans(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ran_at, bounty_count, new_count FROM scans ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["id"] for row in conn.execute("SELECT id FROM bounties")}
