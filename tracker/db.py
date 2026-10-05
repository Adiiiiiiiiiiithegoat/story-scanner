"""SQLite schema, migrations, inserts and queries. Timestamps are ISO 8601 UTC strings."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = [
    # v1
    """
    CREATE TABLE stories(
        story_item_id TEXT PRIMARY KEY, posted_at TEXT, expires_at TEXT, first_seen_at TEXT, media_type TEXT);
    CREATE TABLE snapshots(
        id INTEGER PRIMARY KEY, story_item_id TEXT NOT NULL, taken_at TEXT NOT NULL, viewer_count INTEGER,
        source TEXT NOT NULL, reshuffle_score REAL, is_reshuffle INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE snapshot_viewers(
        snapshot_id INTEGER NOT NULL, user_id TEXT NOT NULL, username TEXT, rank INTEGER NOT NULL, extra_json TEXT,
        PRIMARY KEY(snapshot_id, user_id));
    CREATE TABLE viewers(
        user_id TEXT PRIMARY KEY, username TEXT, full_name TEXT, first_seen_at TEXT, last_seen_at TEXT);
    CREATE TABLE events(
        id INTEGER PRIMARY KEY, story_item_id TEXT, user_id TEXT, snapshot_id INTEGER, prev_rank INTEGER,
        new_rank INTEGER, jump INTEGER, confidence TEXT, reason TEXT, created_at TEXT);
    CREATE INDEX idx_snapshots_story_time ON snapshots(story_item_id, taken_at);
    CREATE INDEX idx_snapshot_viewers_user ON snapshot_viewers(user_id);
    CREATE INDEX idx_events_story ON events(story_item_id);
    CREATE INDEX idx_events_user ON events(user_id);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    version = row[0] if row else 0
    if not row:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        conn.commit()
    for v, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {sql}; UPDATE schema_version SET version = {v}; COMMIT;")
        version = v
    return version


def upsert_story(conn, item_id, posted_at=None, expires_at=None, media_type=None, first_seen_at=None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO stories VALUES (?,?,?,?,?)
               ON CONFLICT(story_item_id) DO UPDATE SET
                 posted_at = COALESCE(excluded.posted_at, stories.posted_at),
                 expires_at = COALESCE(excluded.expires_at, stories.expires_at),
                 media_type = COALESCE(excluded.media_type, stories.media_type)""",
            (item_id, posted_at, expires_at, first_seen_at or utcnow(), media_type))


def insert_snapshot(conn, item_id: str, taken_at: str, source: str, viewers) -> int:
    """viewers: ordered parser.Viewer list. Also upserts the viewers table (keeps usernames current)."""
    with conn:
        sid = conn.execute(
            "INSERT INTO snapshots(story_item_id, taken_at, viewer_count, source) VALUES (?,?,?,?)",
            (item_id, taken_at, len(viewers), source)).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO snapshot_viewers VALUES (?,?,?,?,?)",
            [(sid, v.user_id, v.username, rank, json.dumps(v.extra, ensure_ascii=False) if v.extra else None)
             for rank, v in enumerate(viewers)])
        conn.executemany(
            """INSERT INTO viewers VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 full_name = COALESCE(NULLIF(excluded.full_name, ''), viewers.full_name),
                 last_seen_at = excluded.last_seen_at""",
            [(v.user_id, v.username, v.full_name, taken_at, taken_at) for v in viewers])
    return sid


def last_snapshot(conn, item_id: str, before_id: int | None = None) -> dict | None:
    row = conn.execute(
        "SELECT id, source FROM snapshots WHERE story_item_id = ? AND id < ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (item_id, before_id if before_id is not None else 2**62)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "source": row["source"], "user_ids": snapshot_user_ids(conn, row["id"])}


def snapshot_user_ids(conn, sid: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT user_id FROM snapshot_viewers WHERE snapshot_id = ? ORDER BY rank", (sid,))]


def set_analysis(conn, sid: int, score: float | None, is_reshuffle: bool) -> None:
    with conn:
        conn.execute("UPDATE snapshots SET reshuffle_score = ?, is_reshuffle = ? WHERE id = ?", (score, int(is_reshuffle), sid))


def insert_events(conn, item_id: str, sid: int, events, created_at: str) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO events(story_item_id, user_id, snapshot_id, prev_rank, new_rank, jump, confidence, reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(item_id, e.user_id, sid, e.prev_rank, e.new_rank, e.jump, e.confidence, e.reason, created_at) for e in events])


def user_id_for_username(conn, username: str) -> str | None:
    row = conn.execute("SELECT user_id FROM viewers WHERE username = ? ORDER BY last_seen_at DESC LIMIT 1", (username,)).fetchone()
    return row[0] if row else None


def clear_snapshots(conn) -> None:
    with conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM snapshot_viewers")
        conn.execute("DELETE FROM snapshots")


def stories(conn):
    return conn.execute("SELECT * FROM stories ORDER BY COALESCE(posted_at, first_seen_at) DESC").fetchall()


def snapshots_for(conn, item_id: str):
    return conn.execute("SELECT * FROM snapshots WHERE story_item_id = ? ORDER BY taken_at, id", (item_id,)).fetchall()


def events_for(conn, item_id: str):
    return conn.execute("SELECT * FROM events WHERE story_item_id = ? ORDER BY snapshot_id, new_rank", (item_id,)).fetchall()


def viewer_names(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT user_id, username FROM viewers")}


def stats(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "active": q("SELECT COUNT(*) FROM stories WHERE expires_at > ?", utcnow()),
        "viewers": q("SELECT COUNT(*) FROM viewers"),
        "snapshots": q("SELECT COUNT(*) FROM snapshots"),
        "reshuffles": q("SELECT COUNT(*) FROM snapshots WHERE is_reshuffle = 1"),
    }
