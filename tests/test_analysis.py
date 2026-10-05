import pytest

from tracker.analysis import compare, spearman, summarize, top_zone_size
from tracker.config import AnalysisConfig

C = AnalysisConfig()
SCENARIOS = ["stable_one_jump", "jump_with_new_viewers", "push_down", "full_reshuffle", "small_list", "tiny_list", "dom_fallback"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenarios(fixture, name):
    s = fixture("sequences.json")[name]
    res = compare(s["prev"], s["cur"], C, dom=s.get("dom", False))
    assert res.is_reshuffle == s["expect"]["reshuffle"]
    assert [[e.user_id, e.confidence] for e in res.events] == s["expect"]["events"]


def test_new_viewers_never_flagged(fixture):
    s = fixture("sequences.json")["jump_with_new_viewers"]
    res = compare(s["prev"], s["cur"], C)
    assert res.new_viewers == ["x", "y"]
    assert {"x", "y"}.isdisjoint(e.user_id for e in res.events)
    assert res.events[0].jump == 15 and res.events[0].prev_rank == 15 and res.events[0].new_rank == 2


def test_push_down_is_not_movement(fixture):
    s = fixture("sequences.json")["push_down"]
    res = compare(s["prev"], s["cur"], C)
    assert res.score == 1.0 and res.movers_fraction == 0


def test_reshuffle_score_stored():
    res = compare(list("abcdefghij"), list("jihgfedcba"), C)
    assert res.is_reshuffle and res.score is not None and res.events == []


def test_already_in_top_zone_not_flagged():
    prev = list("abcdefghijklmnopqrst")
    cur = ["e"] + [u for u in prev if u != "e"]  # e was #5 (inside top 5) -> #1
    assert compare(prev, cur, C).events == []


def test_many_jumpers_not_high():
    prev = list("abcdefghijklmnopqrst")
    cur = ["p", "q", "r"] + [u for u in prev if u not in "pqr"]
    res = compare(prev, cur, C)
    assert res.events and all(e.confidence != "high" for e in res.events)


def test_helpers():
    assert spearman([0, 1, 2], [0, 1, 2]) == 1.0
    assert spearman([0, 1, 2], [2, 1, 0]) == -1.0
    assert spearman([0, 1], [1, 0]) is None
    assert top_zone_size(20, C) == 5 and top_zone_size(4, C) == 2 and top_zone_size(200, C) == 20


def test_summarize():
    snaps = [("t1", ["a", "b"]), ("t2", ["c", "a", "b"]), ("t3", ["b", "c", "a"])]
    events = [{"user_id": "b", "confidence": "high"}, {"user_id": "b", "confidence": "low"}]
    s = summarize(snaps, events, C)
    assert s["b"]["first_seen"] == "t1" and s["c"]["first_seen"] == "t2"
    assert s["b"]["best_rank"] == 0 and s["b"]["latest_rank"] == 0 and s["a"]["latest_rank"] == 2
    assert (s["b"]["high"], s["b"]["low"]) == (1, 1) and s["b"]["score"] == pytest.approx(1.3)
