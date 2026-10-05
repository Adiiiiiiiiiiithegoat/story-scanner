"""Load config.toml (created from config.example.toml on first run) into plain dataclasses."""
from __future__ import annotations

import shutil
import tomllib
from dataclasses import dataclass, field, fields
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MIN_INTERVAL_MINUTES = 15  # hard floor, deliberately not configurable


@dataclass
class AnalysisConfig:
    reshuffle_threshold: float = 0.6
    max_movers_fraction: float = 0.30
    mover_places: int = 3
    min_movers_for_reshuffle: int = 3
    min_jump_abs: int = 3
    min_jump_frac: float = 0.10
    top_zone: int = 5
    top_zone_frac: float = 0.10
    top_zone_frac_above: int = 50
    high_top: int = 3
    high_jump_factor: float = 2.0
    high_min_corr: float = 0.85
    high_max_up_movers: int = 2
    medium_min_corr: float = 0.7
    weight_high: float = 1.0
    weight_medium: float = 0.6
    weight_low: float = 0.3


@dataclass
class Config:
    data_dir: Path = ROOT / "data"
    timezone: str = "Asia/Kolkata"
    username: str = ""
    interval_minutes: float = 30
    jitter_minutes: float = 7
    no_story_interval_minutes: float = 120
    no_story_jitter_minutes: float = 15
    quiet_hours: list = field(default_factory=lambda: ["02:00", "07:00"])
    max_retries: int = 3
    headless: bool = True
    browser_channel: str = "chrome"
    max_scrolls: int = 40
    scroll_idle_seconds: float = 5
    timeout_seconds: float = 30
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    warnings: list = field(default_factory=list)

    tz = property(lambda self: ZoneInfo(self.timezone))
    profile_dir = property(lambda self: self.data_dir / "profile")
    db_path = property(lambda self: self.data_dir / "tracker.db")
    raw_dir = property(lambda self: self.data_dir / "raw")
    log_dir = property(lambda self: self.data_dir / "logs")
    report_path = property(lambda self: self.data_dir / "report.html")
    export_dir = property(lambda self: self.data_dir / "export")
    stop_file = property(lambda self: self.data_dir / "STOP")

    def quiet_window(self) -> tuple[dtime, dtime] | None:
        if not self.quiet_hours:
            return None
        start, end = (dtime.fromisoformat(t) for t in self.quiet_hours)
        return start, end

    def validate(self) -> "Config":
        if self.interval_minutes < MIN_INTERVAL_MINUTES:
            self.warnings.append(f"interval_minutes={self.interval_minutes} raised to the {MIN_INTERVAL_MINUTES} min floor")
            self.interval_minutes = MIN_INTERVAL_MINUTES
        if self.no_story_interval_minutes < MIN_INTERVAL_MINUTES:
            self.no_story_interval_minutes = MIN_INTERVAL_MINUTES
        if self.jitter_minutes < 0 or self.no_story_jitter_minutes < 0:
            raise ValueError("jitter must be >= 0")
        if self.quiet_hours and len(self.quiet_hours) != 2:
            raise ValueError('quiet_hours must be [] or ["HH:MM", "HH:MM"]')
        self.quiet_window()  # raises on bad HH:MM
        self.tz  # raises on unknown timezone
        if self.max_scrolls < 1 or self.max_retries < 0:
            raise ValueError("max_scrolls must be >= 1 and max_retries >= 0")
        a = self.analysis
        for name in ("max_movers_fraction", "min_jump_frac", "top_zone_frac", "high_min_corr", "medium_min_corr"):
            if not 0 <= getattr(a, name) <= 1:
                raise ValueError(f"analysis.{name} must be between 0 and 1")
        if not -1 <= a.reshuffle_threshold <= 1:
            raise ValueError("analysis.reshuffle_threshold must be between -1 and 1")
        return self


def _pick(cls, values: dict, where: str) -> dict:
    known = {f.name for f in fields(cls)} - {"analysis", "warnings"}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown config keys in {where}: {', '.join(sorted(unknown))}")
    return values


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.toml"
    created = False
    if not path.exists():
        shutil.copy(ROOT / "config.example.toml", path)
        created = True
    raw = tomllib.loads(path.read_text("utf-8"))
    analysis = raw.pop("analysis", {})
    flat: dict = {}
    for k, v in raw.items():  # [schedule] / [browser] tables are just grouping
        flat.update(v) if isinstance(v, dict) else flat.__setitem__(k, v)
    _pick(Config, flat, path.name)
    if "data_dir" in flat:
        flat["data_dir"] = (ROOT / flat["data_dir"]).resolve()
    cfg = Config(**flat, analysis=AnalysisConfig(**_pick(AnalysisConfig, analysis, "[analysis]")))
    if created:
        cfg.warnings.append(f"created {path} from config.example.toml")
    return cfg.validate()
