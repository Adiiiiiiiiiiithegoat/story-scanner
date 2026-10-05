"""Render data/report.html: one self-contained file (inline CSS/JS/data, no external requests)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import analysis, db
from .config import ROOT


def _ts(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso).timestamp() * 1000) if iso else None


def build(conn, cfg) -> dict:
    tz = cfg.tz
    fmt = lambda iso: datetime.fromisoformat(iso).astimezone(tz).strftime("%d %b %H:%M") if iso else ""
    names = db.viewer_names(conn)
    now = db.utcnow()
    stories = []
    for st in db.stories(conn):
        sid_ = st["story_item_id"]
        snaps = db.snapshots_for(conn, sid_)
        if not snaps:
            continue
        orders = [db.snapshot_user_ids(conn, s["id"]) for s in snaps]
        idx = {s["id"]: i for i, s in enumerate(snaps)}
        events = [dict(e) for e in db.events_for(conn, sid_)]
        summary = analysis.summarize([(s["taken_at"], o) for s, o in zip(snaps, orders)], events, cfg.analysis)
        positions = [{u: r for r, u in enumerate(o)} for o in orders]
        by_user: dict[str, list] = {}
        for e in events:
            by_user.setdefault(e["user_id"], []).append({
                "t": fmt(snaps[idx[e["snapshot_id"]]]["taken_at"]), "i": idx[e["snapshot_id"]],
                "prev": e["prev_rank"], "new": e["new_rank"], "jump": e["jump"],
                "confidence": e["confidence"], "reason": e["reason"]})
        viewers = [{
            "user_id": u, "username": names.get(u, u),
            "first_seen": fmt(s["first_seen"]), "first_seen_ts": _ts(s["first_seen"]),
            "latest_rank": s["latest_rank"], "best_rank": s["best_rank"],
            "high": s["high"], "medium": s["medium"], "low": s["low"], "score": round(s["score"], 2),
            "series": [p.get(u) for p in positions], "events": by_user.get(u, []),
        } for u, s in summary.items()]
        posted = st["posted_at"] or st["first_seen_at"]
        stories.append({
            "id": sid_, "media_type": st["media_type"] or "", "posted": fmt(posted), "posted_ts": _ts(posted),
            "expires": fmt(st["expires_at"]), "active": bool(st["expires_at"] and st["expires_at"] > now),
            "snapshots": [{"t": fmt(s["taken_at"]), "ts": _ts(s["taken_at"]), "source": s["source"],
                           "reshuffle": bool(s["is_reshuffle"]), "score": s["reshuffle_score"]} for s in snaps],
            "viewers": viewers,
        })
    return {"generated": datetime.now(timezone.utc).astimezone(tz).strftime("%d %b %Y %H:%M ") + cfg.timezone,
            "stats": db.stats(conn), "names": names, "stories": stories}


def render(conn, cfg) -> Path:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    data_json = json.dumps(build(conn, cfg), ensure_ascii=False).replace("<", "\\u003c")  # safe inside <script>
    html = env.get_template("dashboard.html.j2").render(data_json=data_json)
    cfg.report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.report_path.with_suffix(".tmp")
    tmp.write_text(html, "utf-8")
    tmp.replace(cfg.report_path)
    return cfg.report_path
