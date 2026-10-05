import json

import pytest

from tracker import dashboard, db, parser, scheduler
from tracker.config import Config
from tracker.parser import Viewer

ITEM = "3500000000000000001"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def ids(*names):
    return [Viewer(f"id_{n}", n) for n in names]


def test_migrate_idempotent(conn):
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_snapshots_story_time", "idx_snapshot_viewers_user"} <= names


def test_snapshot_roundtrip_and_extra_json(conn, fixture):
    viewers = parser.parse_viewers(fixture("rest_viewers_page1.json")).viewers
    sid = db.insert_snapshot(conn, ITEM, "2026-10-05T10:00:00+00:00", "api", viewers)
    assert db.snapshot_user_ids(conn, sid) == ["1001", "1002", "1003"]
    extra = conn.execute("SELECT extra_json FROM snapshot_viewers WHERE user_id='1001'").fetchone()[0]
    assert json.loads(extra)["has_liked"] is True
    assert conn.execute("SELECT viewer_count FROM snapshots WHERE id=?", (sid,)).fetchone()[0] == 3


def test_username_change_same_user_id(conn):
    db.insert_snapshot(conn, ITEM, "2026-10-05T10:00:00+00:00", "api", [Viewer("77", "old_name", "Full")])
    db.insert_snapshot(conn, ITEM, "2026-10-05T10:30:00+00:00", "api", [Viewer("77", "new_name", "")])
    row = conn.execute("SELECT * FROM viewers WHERE user_id='77'").fetchone()
    assert row["username"] == "new_name" and row["full_name"] == "Full"
    assert row["first_seen_at"].startswith("2026-10-05T10:00") and row["last_seen_at"].startswith("2026-10-05T10:30")
    assert conn.execute("SELECT COUNT(*) FROM viewers").fetchone()[0] == 1
    assert db.user_id_for_username(conn, "new_name") == "77"


def test_ingest_emits_events_and_flags(conn):
    acfg = Config().analysis
    prev = ids(*"abcdefghijklmnopqrst")
    cur = [prev[15]] + prev[:15] + prev[16:]
    scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:00:00+00:00", prev, "api")
    sid = scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:30:00+00:00", cur, "api")
    ev = conn.execute("SELECT * FROM events").fetchall()
    assert [(e["user_id"], e["confidence"], e["snapshot_id"]) for e in ev] == [("id_p", "high", sid)]
    snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (sid,)).fetchone()
    assert snap["is_reshuffle"] == 0 and snap["reshuffle_score"] == 1.0
    # reshuffle: flagged, no new events
    sid3 = scheduler.ingest(conn, acfg, ITEM, "2026-10-05T11:00:00+00:00", list(reversed(cur)), "api")
    assert conn.execute("SELECT is_reshuffle FROM snapshots WHERE id=?", (sid3,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_dom_snapshot_maps_known_usernames_and_is_low(conn):
    acfg = Config().analysis
    prev = ids(*"abcdefghijklmnopqrst")
    scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:00:00+00:00", prev, "api")
    dom = [Viewer("", v.username) for v in [prev[15]] + prev[:15] + prev[16:]]
    scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:30:00+00:00", dom, "dom")
    ev = conn.execute("SELECT user_id, confidence FROM events").fetchall()
    assert [tuple(e) for e in ev] == [("id_p", "low")]


def test_dashboard_renders(conn, tmp_path):
    cfg = Config(data_dir=tmp_path)
    db.upsert_story(conn, ITEM, "2026-10-05T09:00:00+00:00", "2099-01-01T00:00:00+00:00", "image")
    prev = ids(*"abcdefghijklmnopqrst")
    scheduler.ingest(conn, cfg.analysis, ITEM, "2026-10-05T10:00:00+00:00", prev, "api")
    scheduler.ingest(conn, cfg.analysis, ITEM, "2026-10-05T10:30:00+00:00", [prev[15]] + prev[:15] + prev[16:], "api")
    data = dashboard.build(conn, cfg)
    assert data["stats"] == {"active": 1, "viewers": 20, "snapshots": 2, "reshuffles": 0}
    p = next(v for v in data["stories"][0]["viewers"] if v["username"] == "p")
    assert p["high"] == 1 and p["series"] == [15, 0] and p["best_rank"] == 0
    html = dashboard.render(conn, cfg).read_text("utf-8")
    assert "Instagram doesn't report real rewatches" in html
    assert 'src="http' not in html and 'href="http' not in html  # self-contained


def test_reparse_from_raw(conn, tmp_path, fixture):
    cfg = Config(data_dir=tmp_path)
    cfg.raw_dir.mkdir()
    for i, name in enumerate(["rest_viewers_page1.json", "rest_viewers_page2.json"]):
        (cfg.raw_dir / f"20261005T100000Z_{ITEM}_{i:03d}.json").write_text(json.dumps(
            {"taken_at": "2026-10-05T10:00:00+00:00", "source": "api", "pattern": "list_reel_media_viewer", "body": fixture(name)}))
    (cfg.raw_dir / f"20261005T103000Z_{ITEM}_dom.json").write_text(json.dumps(
        {"taken_at": "2026-10-05T10:30:00+00:00", "source": "dom", "usernames": ["erin", "alice", "bob", "carol", "dave"]}))
    assert scheduler.reparse(cfg, conn) == 2
    snaps = db.snapshots_for(conn, ITEM)
    assert [s["source"] for s in snaps] == ["api", "dom"]
    assert db.snapshot_user_ids(conn, snaps[1]["id"])[0] == "1005"  # DOM username mapped back to its id


def test_migrate_v1_to_v2_keeps_events():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.executescript(f"CREATE TABLE schema_version(version INTEGER NOT NULL); INSERT INTO schema_version VALUES (1); {db.MIGRATIONS[0]}")
    c.execute("INSERT INTO events(story_item_id, user_id) VALUES ('s', 'u')")
    assert db.migrate(c) == 2
    assert c.execute("SELECT liked FROM events").fetchone()[0] == 0


def test_ingest_like_data_end_to_end(conn):
    acfg = Config().analysis
    v = lambda n, liked: Viewer(f"id_{n}", n, extra={"has_liked": liked})
    likers, non = list("ABC"), list("abcdefghij")
    prev = [v(n, True) for n in likers] + [v(n, False) for n in non]
    cur = [v("e", True)] + [v(n, True) for n in likers] + [v(n, False) for n in non if n != "e"]
    scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:00:00+00:00", prev, "api")
    scheduler.ingest(conn, acfg, ITEM, "2026-10-05T10:30:00+00:00", cur, "api")
    ev = conn.execute("SELECT user_id, confidence, liked FROM events").fetchall()
    assert [tuple(e) for e in ev] == [("id_e", "high", 1)]


def test_single_instance_lock(tmp_path):
    from tracker import cli
    cfg = Config(data_dir=tmp_path)
    with cli.single_instance(cfg):
        with pytest.raises(cli.AlreadyRunning):
            with cli.single_instance(cfg):
                pass
    with cli.single_instance(cfg):  # released after exit
        pass


def test_restart_waits_for_min_interval(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    c = db.connect(cfg.db_path)
    db.insert_snapshot(c, ITEM, db.utcnow(), "api", ids("a"))
    waits = []
    monkeypatch.setattr(scheduler, "_sleep", lambda s, cfg: waits.append(s) or False)  # False = stop requested
    assert scheduler.run(cfg) == 0
    assert len(waits) == 1 and 14 * 60 < waits[0] <= 15 * 60
