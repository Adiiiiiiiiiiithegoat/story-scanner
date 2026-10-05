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


# --- like blocks: likers sit on top, each block ordered by latest view -----------------------------
LIKERS = [f"L{i}" for i in range(1, 9)]
NON = [f"n{i}" for i in range(1, 13)]


def test_non_liker_rewatch_under_many_likers():
    prev = LIKERS + NON
    cur = LIKERS + ["n10"] + [u for u in NON if u != "n10"]  # n10 jumps to just under the likers
    assert compare(prev, cur, C).events == []  # without like data it lands at #9: outside the top zone
    res = compare(prev, cur, C, liked_prev=set(LIKERS), liked_cur=set(LIKERS))
    assert [(e.user_id, e.confidence, e.prev_rank, e.new_rank, e.liked) for e in res.events] == [("n10", "high", 17, 8, False)]
    assert "non-liker block" in res.events[0].reason


def test_liker_rewatch_goes_to_top_of_liker_block():
    prev = LIKERS + NON
    cur = ["L5"] + [u for u in LIKERS if u != "L5"] + NON
    res = compare(prev, cur, C, liked_prev=set(LIKERS), liked_cur=set(LIKERS))
    assert [(e.user_id, e.confidence) for e in res.events] == [("L5", "medium")]


def test_new_like_from_existing_viewer_is_a_rewatch():
    prev = LIKERS + NON
    cur = ["n7"] + LIKERS + [u for u in NON if u != "n7"]
    res = compare(prev, cur, C, liked_prev=set(LIKERS), liked_cur=set(LIKERS) | {"n7"})
    assert [(e.user_id, e.confidence, e.liked) for e in res.events] == [("n7", "high", True)]


def test_new_viewer_who_likes_is_a_first_view():
    prev = LIKERS + NON
    cur = ["x"] + LIKERS + NON
    res = compare(prev, cur, C, liked_prev=set(LIKERS), liked_cur=set(LIKERS) | {"x"})
    assert res.events == [] and res.new_viewers == ["x"]


def test_new_like_kept_even_when_reshuffled():
    prev = LIKERS + NON
    rest = [u for u in NON if u != "n7"]
    cur = ["n7"] + LIKERS + rest[::-1]  # non-liker block fully reversed
    res = compare(prev, cur, C, liked_prev=set(LIKERS), liked_cur=set(LIKERS) | {"n7"})
    assert res.is_reshuffle and [(e.user_id, e.liked) for e in res.events] == [("n7", True)]


def test_several_jumpers_are_not_a_reshuffle():
    # 5 deep viewers rewatch: everyone above their old spots shifts down 5, but the list is stable
    prev = [f"v{i:02d}" for i in range(40)]
    jumpers = ["v35", "v30", "v25", "v20", "v15"]
    cur = jumpers + [u for u in prev if u not in jumpers]
    res = compare(prev, cur, C)
    assert not res.is_reshuffle and res.movers_fraction == 0
    assert {e.user_id for e in res.events} == set(jumpers)


def test_rewatch_then_new_viewers_still_in_top_zone():
    prev = list("abcdefghijklmnopqrst")
    cur = list("vwxyz") + ["p"] + [u for u in prev if u != "p"]  # p rewatched, then 5 first views arrived
    res = compare(prev, cur, C)
    assert [(e.user_id, e.new_rank) for e in res.events] == [("p", 5)]
