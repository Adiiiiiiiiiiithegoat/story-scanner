"""Rank-movement scoring. Pure functions over ordered lists of user ids (index 0 = top).

Why this works at all
---------------------
Instagram doesn't expose rewatches. For a small story the "Seen by" list is roughly
reverse-chronological by *latest* view, so someone who watches again tends to pop back up toward
the top. Comparing consecutive snapshots of the same story item, a viewer who jumps up a long
way, into the top of the list, while everyone else keeps their relative order, has plausibly
rewatched. That is all we can say, so every flag is a "possible rewatch" with a confidence level.

Steps per pair (previous snapshot -> current snapshot), run separately on the liker and the
non-liker block when like data is available:
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
- Interactions: replies and reactions sent as DMs aren't visible in the viewer data.
- Likes are handled, not a false positive: Instagram puts likers in a block at the top (each block
  ordered by latest view). See compare(). If the viewer data has no like flag, the whole list is
  compared as one block and a non-liker rewatch that lands under many likers can be missed.
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
    liked: bool = False  # event comes from a new like (an existing viewer reopened the story and liked it)


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


def _compare_block(prev: list[str], cur: list[str], c: AnalysisConfig, dom: bool = False, block: str = "") -> PairResult:
    """Rank-movement comparison of one ordered list (the whole list, or one like-block of it)."""
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
    where = f" within the {block} block" if block else ""
    for u in candidates:
        p, adj, r, above = moves[u]
        if r >= zone_cur or p < zone_prev:
            continue
        rho = "n/a" if score is None else f"{score:.2f}"
        reason = (f"moved #{p + 1} -> #{r + 1}{where} (expected #{adj + 1} after {above} new viewer(s) above); "
                  f"jump {jump[u]} (min {min_jump}); rho {rho}; {len(candidates)} viewer(s) jumped this round")
        if dom:
            conf, why = "low", "DOM-fallback snapshot (lower trust)"
        elif score is None:
            conf, why = "low", "too few stable viewers to judge list stability"
        elif (r < c.high_top and jump[u] >= c.high_jump_factor * min_jump and score >= c.high_min_corr
              and len(candidates) <= c.high_max_up_movers):
            conf, why = "high", "big jump to the top of an otherwise stable list"
        elif score >= c.medium_min_corr:
            conf, why = "medium", "clear jump, list mostly stable"
        else:
            conf, why = "low", "list only loosely stable"
        result.events.append(Event(u, p, r, jump[u], conf, f"{why}: {reason}"))
    return result


def compare(prev: list[str], cur: list[str], c: AnalysisConfig, dom: bool = False,
            liked_prev: set[str] | None = None, liked_cur: set[str] | None = None) -> PairResult:
    """Compare two snapshots. liked_prev/liked_cur: user ids who liked the story in each snapshot,
    or None when that snapshot carries no like data (then the whole list is compared as one block).

    With like data the list is two blocks, each ordered by most recent view: likers on top, then
    non-likers. They're compared separately, so a non-liker who rewatches (lands at the top of the
    non-liker block, just under the likers) is caught however many likers there are. An existing
    viewer who newly liked must have reopened the story, so that's its own high-confidence event,
    independent of ordering (kept even when the pair is a reshuffle).
    """
    if liked_prev is None or liked_cur is None:
        return _compare_block(prev, cur, c, dom)
    prev_rank = {u: i for i, u in enumerate(prev)}
    cur_rank = {u: i for i, u in enumerate(cur)}
    blocks = [
        _compare_block([u for u in prev if u in liked_prev], [u for u in cur if u in liked_cur], c, dom, "liker"),
        _compare_block([u for u in prev if u not in liked_prev], [u for u in cur if u not in liked_cur], c, dom, "non-liker"),
    ]
    scores = [b.score for b in blocks if b.score is not None]
    result = PairResult(min(scores) if scores else None, any(b.is_reshuffle for b in blocks),
                        max(b.movers_fraction for b in blocks), [u for u in cur if u not in prev_rank])
    if not result.is_reshuffle:
        for e in (e for b in blocks for e in b.events):  # report full-list ranks, keep block reasoning
            e.prev_rank, e.new_rank = prev_rank[e.user_id], cur_rank[e.user_id]
            result.events.append(e)
    for u in cur:
        if u in prev_rank and u in liked_cur and u not in liked_prev:
            result.events.append(Event(
                u, prev_rank[u], cur_rank[u], prev_rank[u] - cur_rank[u], "high",
                f"liked the story since the last snapshot; a like happens while viewing, so they reopened it: "
                f"moved #{prev_rank[u] + 1} -> #{cur_rank[u] + 1} into the liker block", liked=True))
    result.events.sort(key=lambda e: e.new_rank)
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
