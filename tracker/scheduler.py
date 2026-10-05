"""Capture cycles, the long-running loop, and re-ingesting saved raw captures."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Error as PWError

from . import analysis, capture, dashboard, db, parser, safety
from .config import MIN_INTERVAL_MINUTES
from .parser import Viewer

log = logging.getLogger(__name__)
RAW_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_(\d+)_(\d{3}|dom)\.json$")


def ingest(conn, acfg, item_id: str, taken_at: str, viewers: list[Viewer], source: str) -> int:
    """Store one snapshot and compare it with the previous snapshot of the same item."""
    if source == "dom":  # DOM rows only have usernames: map to known ids so they line up with API snapshots
        viewers = [Viewer(db.user_id_for_username(conn, v.username) or f"u:{v.username}", v.username) for v in viewers]
    db.upsert_story(conn, item_id, first_seen_at=taken_at)
    sid = db.insert_snapshot(conn, item_id, taken_at, source, viewers)
    prev = db.last_snapshot(conn, item_id, before_id=sid)
    if not prev:
        return sid
    res = analysis.compare(prev["user_ids"], [v.user_id for v in viewers], acfg,
                           dom="dom" in (source, prev["source"]),
                           liked_prev=parser.liked_set(zip(prev["user_ids"], prev["extras"])),
                           liked_cur=parser.liked_set((v.user_id, v.extra) for v in viewers))
    db.set_analysis(conn, sid, res.score, res.is_reshuffle)
    db.insert_events(conn, item_id, sid, res.events, taken_at)
    rho = "n/a" if res.score is None else f"{res.score:.2f}"
    if res.is_reshuffle:
        log.info("  item %s: reshuffle detected (rho %s, %.0f%% moved), no events", item_id, rho, res.movers_fraction * 100)
    else:
        log.info("  item %s: %d new viewer(s), %d possible rewatch(es), rho %s",
                 item_id, len(res.new_viewers), len(res.events), rho)
    return sid


def snapshot_once(cfg, conn) -> int:
    """One full capture cycle. Returns the number of live story items."""
    username, items, caps = capture.run_cycle(cfg)
    for it in items:
        db.upsert_story(conn, it.id, it.posted_at, it.expires_at, it.media_type)
    for c in caps:
        ingest(conn, cfg.analysis, c.item_id, c.taken_at, c.viewers, c.source)
    if not items:
        log.info("No live story items.")
    dashboard.render(conn, cfg)
    return len(items)


def _with_retries(fn, cfg):
    """Exponential backoff on transient browser/network errors; SafetyStop always propagates."""
    for attempt in range(cfg.max_retries + 1):
        try:
            return fn()
        except PWError as e:
            if attempt == cfg.max_retries:
                log.error("Cycle failed after %d retries, skipping: %s", cfg.max_retries, str(e).splitlines()[0])
                return None
            wait = 30 * 2 ** attempt
            log.warning("Transient error (%s); retry %d/%d in %ds", str(e).splitlines()[0], attempt + 1, cfg.max_retries, wait)
            time.sleep(wait)
        except safety.SafetyStop:
            raise
        except Exception:  # a bug or an unexpected page: don't hammer, skip this cycle
            log.exception("Cycle failed, skipping")
            return None


def quiet_until(cfg, now: datetime) -> datetime | None:
    """End of the current quiet window (local time), or None if we're outside it."""
    win = cfg.quiet_window()
    if not win:
        return None
    local = now.astimezone(cfg.tz)
    start, end = win
    t = local.time()
    inside = start <= t < end if start <= end else (t >= start or t < end)
    if not inside:
        return None
    end_dt = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return end_dt if end_dt > local else end_dt + timedelta(days=1)


def _sleep(seconds: float, cfg) -> bool:
    """Sleep in short slices so the STOP file is noticed. False if a stop was requested."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if safety.stop_requested(cfg):
            return False
        time.sleep(min(10, left))
    return not safety.stop_requested(cfg)


def run(cfg) -> int:
    conn = db.connect(cfg.db_path)
    log.info("Tracker running. Create %s or press Ctrl+C to stop.", cfg.stop_file)
    while True:
        if safety.stop_requested(cfg):
            log.info("STOP file found, exiting.")
            return 0
        if until := quiet_until(cfg, datetime.now(timezone.utc)):
            until += timedelta(minutes=random.uniform(0, cfg.jitter_minutes))
            log.info("Quiet hours, sleeping until %s", until.strftime("%H:%M"))
            if not _sleep((until - datetime.now(cfg.tz)).total_seconds(), cfg):
                return 0
            continue
        n = _with_retries(lambda: snapshot_once(cfg, conn), cfg)
        base, jit = (cfg.no_story_interval_minutes, cfg.no_story_jitter_minutes) if n == 0 else (cfg.interval_minutes, cfg.jitter_minutes)
        minutes = max(MIN_INTERVAL_MINUTES, base + random.uniform(-jit, jit))
        nxt = datetime.now(cfg.tz) + timedelta(minutes=minutes)
        log.info("Next check at %s (in %.0f min)", nxt.strftime("%H:%M"), minutes)
        if not _sleep(minutes * 60, cfg):
            log.info("STOP file found, exiting.")
            return 0


def reparse(cfg, conn) -> int:
    """Rebuild snapshots and events from data/raw (e.g. after fixing the parser). Stories are kept."""
    groups = defaultdict(list)
    for p in sorted(cfg.raw_dir.glob("*.json")):
        if m := RAW_NAME_RE.match(p.name):
            groups[(m.group(1), m.group(2))].append(p)
    db.clear_snapshots(conn)
    for (ts, item_id), files in sorted(groups.items()):
        payloads = [json.loads(f.read_text("utf-8")) for f in sorted(files)]
        taken_at = payloads[0].get("taken_at") or datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
        api = [pl for pl in payloads if pl.get("source") != "dom"]
        if api:
            viewers = parser.merge_pages([parser.parse_viewers(pl["body"], pl.get("pattern")) for pl in api])
            source = "api"
        else:
            viewers, source = [Viewer("", n) for n in payloads[0]["usernames"]], "dom"
        if viewers:
            ingest(conn, cfg.analysis, item_id, taken_at, viewers, source)
    log.info("Re-parsed %d snapshot(s) from %s", len(groups), cfg.raw_dir)
    dashboard.render(conn, cfg)
    return len(groups)
