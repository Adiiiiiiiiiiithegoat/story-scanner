"""python -m tracker <login|check|snapshot|run|report|export --csv|reparse>"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import capture, config, dashboard, db, safety, scheduler, session

log = logging.getLogger("tracker")


def setup_logging(cfg) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(cfg.log_dir / "tracker.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stderr:  # pythonw has no console
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    for noisy in ("asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_login(cfg, args):
    return 0 if session.login(cfg) else 1


def cmd_check(cfg, args):
    username, items, _ = capture.run_cycle(cfg, only_check=True)
    log.info("Logged in%s. %d live story item(s).", f" as @{username}" if username else "", len(items))
    for it in items:
        log.info("  %s  %-5s  posted %s  expires %s  viewers %s", it.id, it.media_type, it.posted_at, it.expires_at, it.viewer_count)
    return 0


def cmd_snapshot(cfg, args):
    conn = db.connect(cfg.db_path)
    n = scheduler.snapshot_once(cfg, conn)
    log.info("Done: %d live item(s). Report: %s", n, cfg.report_path)
    return 0


def cmd_run(cfg, args):
    return scheduler.run(cfg)


def cmd_report(cfg, args):
    print(dashboard.render(db.connect(cfg.db_path), cfg))
    return 0


def cmd_export(cfg, args):
    conn = db.connect(cfg.db_path)
    cfg.export_dir.mkdir(parents=True, exist_ok=True)
    for table in ("stories", "viewers", "snapshots", "snapshot_viewers", "events"):
        cur = conn.execute(f"SELECT * FROM {table}")
        path = cfg.export_dir / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur)
        print(path)
    return 0


def cmd_reparse(cfg, args):
    scheduler.reparse(cfg, db.connect(cfg.db_path))
    return 0


COMMANDS = {
    "login": (cmd_login, "open a headed browser and log in manually (once)"),
    "check": (cmd_check, "verify the saved session and list live story items"),
    "snapshot": (cmd_snapshot, "run one capture cycle now"),
    "run": (cmd_run, "long-running polling loop"),
    "report": (cmd_report, "regenerate data/report.html"),
    "export": (cmd_export, "dump tables to data/export/*.csv"),
    "reparse": (cmd_reparse, "rebuild snapshots/events from data/raw"),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tracker", description="Instagram story rewatch tracker (your own stories only).")
    ap.add_argument("--config", help="path to config.toml (default: project folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (_, help_) in COMMANDS.items():
        p = sub.add_parser(name, help=help_)
        if name == "export":
            p.add_argument("--csv", action="store_true", required=True, help="CSV output (the only format)")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    setup_logging(cfg)
    for w in cfg.warnings:
        log.warning(w)
    try:
        return COMMANDS[args.cmd][0](cfg, args) or 0
    except safety.NotLoggedIn as e:
        log.error("%s", e)
        return 3
    except safety.SafetyStop as e:
        log.error("SAFETY STOP: %s", e)
        log.error("Tracker stopped. Open Instagram in a normal browser and resolve this by hand before running again.")
        safety.notify("Story tracker stopped", str(e))
        return 2
    except KeyboardInterrupt:
        log.info("Interrupted, exiting cleanly.")
        return 0
