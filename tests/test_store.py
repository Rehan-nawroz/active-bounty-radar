"""Unit tests for the scan history store."""

from __future__ import annotations

from datetime import datetime, timezone

from bounty import store

BOUNTY = {
    "id": "acme/widget#1",
    "repo": "acme/widget",
    "number": 1,
    "title": "Fix the widget parser",
    "url": "https://github.com/acme/widget/issues/1",
    "amount_usd": 10000,
    "currency": "USD",
    "source": "algora",
    "comments": 2,
    "open_prs": 0,
    "language": "Go",
    "created_at": "2026-05-01T00:00:00Z",
    "competition": "medium",
    "score": 3333.33,
}

LATER = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER_STILL = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def connect(tmp_path):
    return store.connect(tmp_path / "history.db")


def test_first_scan_marks_everything_new(tmp_path):
    conn = connect(tmp_path)
    outcome = store.record_scan(conn, [BOUNTY], LATER)

    assert outcome.total == 1
    assert outcome.new_ids == {"acme/widget#1"}
    assert store.known_ids(conn) == {"acme/widget#1"}


def test_rescan_marks_nothing_new(tmp_path):
    conn = connect(tmp_path)
    store.record_scan(conn, [BOUNTY], LATER)
    outcome = store.record_scan(conn, [BOUNTY], LATER_STILL)

    assert outcome.new_ids == frozenset()


def test_rescan_updates_fields_but_keeps_first_seen(tmp_path):
    conn = connect(tmp_path)
    store.record_scan(conn, [BOUNTY], LATER)

    changed = dict(BOUNTY, comments=9, competition="high")
    store.record_scan(conn, [changed], LATER_STILL)

    row = conn.execute("SELECT * FROM bounties WHERE id = ?", (BOUNTY["id"],)).fetchone()
    assert row["comments"] == 9
    assert row["competition"] == "high"
    assert row["first_seen_at"] == LATER.isoformat(timespec="seconds")
    assert row["last_seen_at"] == LATER_STILL.isoformat(timespec="seconds")


def test_history_rows_record_totals(tmp_path):
    conn = connect(tmp_path)
    second = dict(BOUNTY, id="acme/docs#2", number=2, title="Docs")

    store.record_scan(conn, [BOUNTY], LATER)
    store.record_scan(conn, [BOUNTY, second], LATER_STILL)

    scans = store.recent_scans(conn)
    assert [scan["bounty_count"] for scan in scans] == [2, 1]
    assert [scan["new_count"] for scan in scans] == [1, 1]


def test_recent_scans_respects_limit_and_order(tmp_path):
    conn = connect(tmp_path)
    for day in range(1, 4):
        store.record_scan(conn, [BOUNTY], datetime(2026, 6, day, tzinfo=timezone.utc))

    scans = store.recent_scans(conn, limit=2)
    assert len(scans) == 2
    assert scans[0]["ran_at"] > scans[1]["ran_at"]


def test_database_file_and_directory_are_created(tmp_path):
    path = tmp_path / "nested" / "dir" / "history.db"
    conn = store.connect(path)
    store.record_scan(conn, [BOUNTY], LATER)
    conn.close()

    assert path.is_file()


def test_default_db_path_honours_environment(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("BOUNTY_DB", str(target))
    assert store.default_db_path() == target
