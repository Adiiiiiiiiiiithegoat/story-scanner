"""Rank-movement scoring. Pure functions over ordered lists of user ids (index 0 = top).

Why this works at all
---------------------
Instagram doesn't expose rewatches. For a small story the "Seen by" list is roughly
reverse-chronological by *latest* view, so someone who watches again tends to pop back up toward
the top. Comparing consecutive snapshots of the same story item, a viewer who jumps up a long
way, into the top of the list, while everyone else keeps their relative order, has plausibly
rewatched. That is all we can say, so every flag is a "possible rewatch" with a confidence level.

Steps per pair (previous snapshot -> current snapshot):
1. New viewers (only in current) are first views. They never produce events.
2. Expected drift: new viewers inserted above someone push them down. Each common viewer's previous
   rank is adjusted by (+ new viewers now above them) and (- vanished viewers that were above them),
   so jump = adjusted_prev_rank - new_rank measures real movement (positive = up).
3. Reshuffle detection: Spearman rho between the two orders over the common viewers, computed
   *without* the viewers whose jump already passes the flag threshold (otherwise a single genuine
   jumper in a short list tanks rho: 8 viewers, last -> first gives rho = 0.33). If rho is below
   `reshuffle_threshold`, or more than `max_movers_fraction` of common viewers moved more than
   `mover_places` (and at least `min_movers_for_reshuffle` did), the pair is a reshuffle: the
   score is stored, the snapshot is flagged and no events are emitted.
4. Flag rule: jump >= max(min_jump_abs, min_jump_frac * list size), lands in the top zone,
   and was not in the top zone in the previous snapshot.
5. Confidence: high = landed in the top `high_top`, jump >= high_jump_factor * min jump,
   rho >= high_min_corr and at most `high_max_up_movers` viewers jumped in this pair;
   medium = rho >= medium_min_corr; low = anything else that passed, a DOM-fallback snapshot,
   or a list too short to judge stability.

Known false positives
---------------------
- Algorithmic re-ranking: on bigger lists Instagram orders viewers by interaction/closeness and
  reshuffles over time. Big reshuffles are caught by step 3; small partial re-ranks are not.
- Interactions: replying, reacting or liking the story can bump someone without a rewatch.
- Instagram A/B tests or format changes can change ordering rules overnight.
- Pagination gaps: if a capture stopped early, people deep in the list "vanish" and "reappear".
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .config import AnalysisConfig

MIN_RHO_VIEWERS = 5


@dataclass
class Event:
    user_id: str
    prev_rank: int
    new_rank: int
    jump: int
    confidence: str
    reason: str


@dataclass
class PairResult:
    score: float | None  # Spearman rho over stable common viewers; None when too few to compute
    is_reshuffle: bool
    movers_fraction: float
    new_viewers: list[str]
    events: list[Event] = field(default_factory=list)


def spearman(a: list[int], b: list[int]) -> float | None:
    """Spearman rho for two rank lists of the same items (no ties). None for fewer than 3 items."""
    n = len(a)
    if n < 3:
        return None
    ra = {v: i for i, v in enumerate(sorted(a))}
    rb = {v: i for i, v in enumerate(sorted(b))}
    d2 = sum((ra[x] - rb[y]) ** 2 for x, y in zip(a, b))
    return 1 - 6 * d2 / (n * (n * n - 1))


def top_zone_size(n: int, c: AnalysisConfig) -> int:
    if n > c.top_zone_frac_above:
        return max(c.top_zone, math.ceil(c.top_zone_frac * n))
    # ponytail: short lists get a zone of half the list so a rewatch is still detectable with 4-9 viewers
    return max(1, min(c.top_zone, n // 2))


def compare(prev: list[str], cur: list[str], c: AnalysisConfig, dom: bool = False) -> PairResult:
    prev_rank = {u: i for i, u in enumerate(prev)}
    cur_rank = {u: i for i, u in enumerate(cur)}
    common = [u for u in cur if u in prev_rank]
    new = [u for u in cur if u not in prev_rank]
    new_pos = sorted(cur_rank[u] for u in new)
    gone_pos = sorted(prev_rank[u] for u in prev if u not in cur_rank)

    min_jump = max(c.min_jump_abs, math.ceil(c.min_jump_frac * len(cur)))
    moves = {}  # user -> (prev_rank, adjusted_prev, new_rank, new_above)
    for u in common:
        above = bisect_left(new_pos, cur_rank[u])
        adj = prev_rank[u] + above - bisect_left(gone_pos, prev_rank[u])
        moves[u] = (prev_rank[u], adj, cur_rank[u], above)
    jump = {u: m[1] - m[2] for u, m in moves.items()}

    candidates = [u for u in common if jump[u] >= min_jump]
    stable = [u for u in common if jump[u] < min_jump]
    # ponytail: rho on fewer than 5 viewers is noise (3 viewers, one move = -0.5), so treat it as unknown
    score = spearman([prev_rank[u] for u in stable], [cur_rank[u] for u in stable]) if len(stable) >= MIN_RHO_VIEWERS else None
    movers = sum(1 for u in common if abs(jump[u]) > c.mover_places)
    movers_fraction = movers / len(common) if common else 0.0
    reshuffle = (score is not None and score < c.reshuffle_threshold) or (
        movers_fraction > c.max_movers_fraction and movers >= c.min_movers_for_reshuffle)
    result = PairResult(score, reshuffle, movers_fraction, new)
    if reshuffle:
        return result

    zone_prev, zone_cur = top_zone_size(len(prev), c), top_zone_size(len(cur), c)
    for u in candidates:
        p, adj, r, above = moves[u]
        if r >= zone_cur or p < zone_prev:
            continue
        rho = "n/a" if score is None else f"{score:.2f}"
        reason = (f"moved #{p + 1} -> #{r + 1} (expected #{adj + 1} after {above} new viewer(s) above); "
                  f"jump {jump[u]} (min {min_jump}); list rho {rho}; {len(candidates)} viewer(s) jumped this round")
        if dom:
            conf, why = "low", "DOM-fallback snapshot (lower trust)"
        elif score is None:
            conf, why = "low", "too few stable viewers to judge list stability"
        elif (r < c.high_top and jump[u] >= c.high_jump_factor * min_jump and score >= c.high_min_corr
              and len(candidates) <= c.high_max_up_movers):
            conf, why = "high", "big jump into the top of an otherwise stable list"
        elif score >= c.medium_min_corr:
            conf, why = "medium", "clear jump, list mostly stable"
        else:
            conf, why = "low", "list only loosely stable"
        result.events.append(Event(u, p, r, jump[u], conf, f"{why}: {reason}"))
    return result


def summarize(snapshots: list[tuple[str, list[str]]], events: list[dict], c: AnalysisConfig) -> dict[str, dict]:
    """Per-viewer summary for one story item.

    snapshots: ordered (taken_at, user_ids) pairs; events: dicts with user_id and confidence.
    first_seen is the first snapshot that contained the viewer (an upper bound on their first view).
    """
    out: dict[str, dict] = {}
    for taken_at, ids in snapshots:
        for r, u in enumerate(ids):
            s = out.setdefault(u, {"first_seen": taken_at, "best_rank": r, "latest_rank": None,
                                   "high": 0, "medium": 0, "low": 0, "score": 0.0})
            s["best_rank"] = min(s["best_rank"], r)
    if snapshots:
        for r, u in enumerate(snapshots[-1][1]):
            out[u]["latest_rank"] = r
    weights = {"high": c.weight_high, "medium": c.weight_medium, "low": c.weight_low}
    for e in events:
        if (s := out.get(e["user_id"])) and e["confidence"] in weights:
            s[e["confidence"]] += 1
            s["score"] += weights[e["confidence"]]
    return out
