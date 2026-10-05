"""End-to-end check of the analysis against a simulated story using Instagram's observed ordering:
likers in a block on top, then non-likers, each block ordered by most recent view."""
import random

from tracker.analysis import compare
from tracker.config import AnalysisConfig

C = AnalysisConfig()


def order(last, liked):
    by_recent = lambda us: sorted(us, key=lambda u: -last[u])
    return by_recent([u for u in last if liked[u]]) + by_recent([u for u in last if not liked[u]])


def simulate(seed, swaps=0, reshuffle_p=0.0, use_likes=True, cycles=40):
    """Returns (true positives, false positives, missed, false reshuffles, real reshuffles, caught reshuffles)."""
    rnd = random.Random(seed)
    last, liked, t = {}, {}, 0.0
    pool = [f"u{i}" for i in range(300)]
    prev = None
    tp = fp = fn = false_rs = real_rs = caught_rs = 0
    for cyc in range(cycles):
        truth, actions = set(), []
        for _ in range(max(0, int(rnd.gauss(12 * 0.85 ** cyc + 1, 2)))):
            if pool:
                actions.append(("new", pool.pop(rnd.randrange(len(pool)))))
        actions += [("re", u) for u in last if rnd.random() < 1.5 / len(last)]
        rnd.shuffle(actions)
        for kind, u in actions:
            t += rnd.random()
            last[u] = t
            if kind == "new":
                liked[u] = rnd.random() < 0.15
            else:
                truth.add(u)
                liked[u] = liked[u] or rnd.random() < 0.15
        snap = order(last, liked)
        likers = {u for u in snap if liked[u]}
        non = [i for i, u in enumerate(snap) if u not in likers]
        shuffled = len(non) > 20 and rnd.random() < reshuffle_p
        if shuffled:
            vals = [snap[i] for i in non]
            rnd.shuffle(vals)
            for i, v in zip(non, vals):
                snap[i] = v
        for _ in range(swaps if not shuffled and len(non) > 2 else 0):
            i = rnd.randrange(len(non) - 1)
            snap[non[i]], snap[non[i + 1]] = snap[non[i + 1]], snap[non[i]]
        if prev:
            res = compare(prev[0], snap, C, liked_prev=prev[1] if use_likes else None,
                          liked_cur=likers if use_likes else None)
            flagged = {e.user_id for e in res.events}
            tp, fp, fn = tp + len(flagged & truth), fp + len(flagged - truth), fn + len(truth - flagged)
            false_rs += res.is_reshuffle and not shuffled
            real_rs += shuffled
            caught_rs += shuffled and res.is_reshuffle
        prev = (snap, likers)
    return tp, fp, fn, false_rs, real_rs, caught_rs


def totals(**kw):
    return [sum(x) for x in zip(*(simulate(s, **kw) for s in range(15)))]


def test_clean_ordering_no_false_flags_and_good_recall():
    tp, fp, fn, false_rs, _, _ = totals()
    assert fp == 0 and false_rs == 0
    assert tp / (tp + fn) > 0.75  # misses are rewatchers already at the top of their block


def test_like_blocks_beat_whole_list():
    tp_blocks, _, fn_blocks, *_ = totals()
    tp_whole, fp_whole, fn_whole, *_ = totals(use_likes=False)
    assert fp_whole == 0
    assert tp_blocks / (tp_blocks + fn_blocks) > 2 * tp_whole / (tp_whole + fn_whole)


def test_noise_and_reshuffles():
    tp, fp, _, _, real_rs, caught_rs = totals(swaps=10, reshuffle_p=0.15)
    assert tp / (tp + fp) > 0.97
    assert real_rs and caught_rs / real_rs > 0.9
