from tracker import parser, safety

VIEWER_URL = "https://www.instagram.com/api/v1/media/3500000000000000001/list_reel_media_viewer/?max_id="


def test_rest_users_array(fixture):
    body = fixture("rest_viewers_page1.json")
    assert parser.match_viewer_response(VIEWER_URL, body) == "list_reel_media_viewer"
    page = parser.parse_viewers(body)
    assert [v.username for v in page.viewers] == ["alice", "bob", "carol"]
    assert page.viewers[0].user_id == "1001" and page.viewers[0].full_name == "Alice A"
    assert page.viewers[0].extra["has_liked"] is True  # merged from the parallel `viewers` list by user id
    assert page.viewers[0].extra["emoji_reaction"] == {"unicode": "x"}
    assert [parser.like_flag(v.extra) for v in page.viewers] == [True, False, False]
    assert page.viewers[1].extra["is_private"] is True  # unknown user fields kept
    assert page.has_more is True and page.total == 5


def test_rest_pagination_merge_keeps_order_and_dedupes(fixture):
    pages = [parser.parse_viewers(fixture("rest_viewers_page1.json")), parser.parse_viewers(fixture("rest_viewers_page2.json"))]
    assert pages[1].has_more is False
    assert [v.username for v in parser.merge_pages(pages)] == ["alice", "bob", "carol", "dave", "erin"]


def test_graphql_edges(fixture):
    body = fixture("graphql_viewers.json")
    assert parser.match_viewer_response("https://www.instagram.com/graphql/query", body) == "graphql"
    page = parser.parse_viewers(body, "graphql")
    assert [(v.user_id, v.username) for v in page.viewers] == [("2001", "zed"), ("2002", "yara")]
    assert page.viewers[0].extra == {"has_liked": True, "reaction": "fire"}
    assert page.has_more is False and page.total == 2


def test_non_viewer_responses_not_matched(fixture):
    reels = fixture("reels_media.json")
    assert parser.match_viewer_response("https://www.instagram.com/api/v1/feed/reels_media/?reel_ids=42", reels) is None
    # GraphQL with a user list that isn't under a viewer-ish key (e.g. suggested users)
    suggested = {"data": {"suggested": [{"pk": "1", "username": "x"}]}}
    assert parser.match_viewer_response("https://www.instagram.com/graphql/query", suggested) is None
    assert parser.match_viewer_response("https://www.instagram.com/api/v1/media/1/likers/", fixture("rest_viewers_page1.json")) is None


def test_extract_story_items(fixture):
    username, items = parser.extract_story_items(fixture("reels_media.json"), "42")
    assert username == "me"
    assert [i.id for i in items] == ["3500000000000000001", "3500000000000000002"]
    assert items[0].media_type == "image" and items[0].viewer_count == 5
    assert items[1].media_type == "video" and items[1].viewer_count == 3
    assert items[1].expires_at > items[1].posted_at  # defaulted to +24h
    assert parser.extract_story_items({"reels": {}, "status": "ok"}, "42") is None


def test_lenient_json_and_media_pk():
    assert parser.loads_lenient('for (;;);{"a": 1}') == {"a": 1}
    assert parser.loads_lenient('{"a": 1}\n{"b": 2}') == {"__multi__": [{"a": 1}, {"b": 2}]}
    assert parser.media_pk_from(VIEWER_URL) == "3500000000000000001"
    assert parser.media_pk_from("https://www.instagram.com/graphql/query",
                                "variables=%7B%22media_id%22%3A%2299%22%7D") == "99"


def test_safety_signals():
    assert safety.check_json({"message": "challenge_required", "status": "fail"})
    assert safety.check_json({"message": "Please wait a few minutes before you try again.", "status": "fail"})
    assert safety.check_json({}, status=429)
    assert safety.check_json({"users": [], "status": "ok"}) is None
    assert safety.check_url("https://www.instagram.com/challenge/abc/")
    assert safety.check_url("https://www.instagram.com/accounts/login/?next=/")
    assert safety.check_url("https://www.instagram.com/stories/me/1/") is None
