"""Turn raw captured Instagram JSON into normalized, ordered viewer lists. Pure functions, no I/O.

Instagram changes endpoints and payload shapes, so nothing here assumes one format. A response
counts as a viewer list when its URL matches a known pattern AND its JSON contains a list whose
elements look like users (have a username and a user id), either directly (REST `users: [...]`),
wrapped (`viewers: [{user: {...}, ...}]`) or as GraphQL edges (`edges: [{node: {user: {...}}}]`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

URL_PATTERNS = [
    ("list_reel_media_viewer", re.compile(r"list_reel_media_viewer", re.I)),
    ("story_viewers", re.compile(r"story_viewers", re.I)),
    ("graphql", re.compile(r"/graphql", re.I)),
    ("media_api", re.compile(r"/api/v1/media/\d+", re.I)),
]
EXCLUDE_URL_RE = re.compile(r"likers|/comments|/info/|reels_media|reels_tray", re.I)
VIEWER_PATH_RE = re.compile(r"viewer", re.I)
ITEMS_PATH_RE = re.compile(r"\.items(\[|$)")  # story-item arrays carry the owner as `user`, not viewers
CORE_KEYS = {"pk", "pk_id", "id", "username", "full_name", "strong_id__"}
LIKE_KEYS = ("has_liked", "liked", "has_liked_story", "is_liked")  # per-viewer like flag; update if Instagram renames it


@dataclass
class Viewer:
    user_id: str
    username: str
    full_name: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ViewerPage:
    viewers: list[Viewer]
    has_more: bool | None  # None = response didn't say
    total: int | None


@dataclass
class StoryItem:
    id: str
    posted_at: str
    expires_at: str
    media_type: str
    viewer_count: int | None


def loads_lenient(text: str):
    """json.loads that tolerates the `for (;;);` guard and newline-delimited GraphQL chunks."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        parts = []
        for line in text.splitlines():
            try:
                parts.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if not parts:
            raise ValueError("not JSON")
        return {"__multi__": parts}


def _as_viewer(el) -> Viewer | None:
    if not isinstance(el, dict):
        return None
    wrapper = el["node"] if isinstance(el.get("node"), dict) else el
    user = wrapper["user"] if isinstance(wrapper.get("user"), dict) else wrapper
    uid = user.get("pk") or user.get("pk_id") or user.get("id")
    name = user.get("username")
    if uid in (None, "") or not isinstance(name, str) or not name:
        return None
    extra = {k: v for k, v in user.items() if k not in CORE_KEYS}
    if wrapper is not user:  # per-viewer flags (has_liked, reaction, timestamp...) live on the wrapper
        extra.update({k: v for k, v in wrapper.items() if k != "user"})
    return Viewer(str(uid), name, user.get("full_name") or "", extra)


def find_user_lists(obj, path: str = "$") -> list[tuple[str, list]]:
    """Every non-empty list in `obj` whose elements all look like users, with its JSON path."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += find_user_lists(v, f"{path}.{k}")
    elif isinstance(obj, list):
        if obj and all(_as_viewer(el) for el in obj):
            out.append((path, obj))
        else:
            for i, v in enumerate(obj):
                out += find_user_lists(v, f"{path}[{i}]")
    return out


def _viewer_lists(body, url_pattern: str | None = None) -> list[tuple[str, list]]:
    lists = [pl for pl in find_user_lists(body) if not ITEMS_PATH_RE.search(pl[0])]
    if url_pattern in ("graphql", "media_api"):  # generic URLs: also require a viewer-ish JSON path
        lists = [pl for pl in lists if VIEWER_PATH_RE.search(pl[0]) or pl[0].endswith(".users")]
    return lists


def match_viewer_response(url: str, body) -> str | None:
    """Name of the URL pattern if this response is a viewer list, else None."""
    if EXCLUDE_URL_RE.search(url):
        return None
    name = next((n for n, rx in URL_PATTERNS if rx.search(url)), None)
    return name if name and _viewer_lists(body, name) else None


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _has_more(body) -> bool | None:
    found: dict[str, list] = {}
    for k, v in _walk(body):
        if k in ("has_next_page", "next_max_id", "more_available"):
            found.setdefault(k, []).append(v)
    for k in ("has_next_page", "next_max_id", "more_available"):  # most authoritative first
        if k in found:
            return any(bool(v) for v in found[k])
    return None


def parse_viewers(body, url_pattern: str | None = None) -> ViewerPage:
    """Viewers in the exact order returned (index 0 = top of the list)."""
    lists = _viewer_lists(body, url_pattern)
    total = next((v for k, v in _walk(body) if k in ("total_viewer_count", "user_count") and isinstance(v, int)), None)
    if not lists:
        return ViewerPage([], _has_more(body), total)
    _, best = max(lists, key=lambda pl: (bool(VIEWER_PATH_RE.search(pl[0]) or pl[0].endswith(".users")), len(pl[1])))
    viewers = [_as_viewer(el) for el in best]
    _merge_side_info(body, viewers)
    return ViewerPage(viewers, _has_more(body), total)


def _merge_side_info(body, viewers: list[Viewer]) -> None:
    """REST viewer responses carry likes/reactions/replies in a parallel `viewers: [{user: {pk}, has_liked, ...}]`
    list whose users have no username. Merge those per-viewer fields into extra, matched by user id."""
    by_id = {v.user_id: v for v in viewers}
    stack = [body]
    while stack:
        d = stack.pop()
        if isinstance(d, dict):
            stack.extend(d.values())
        elif isinstance(d, list):
            for el in d:
                user = el.get("user") if isinstance(el, dict) else None
                uid = str(user.get("pk") or user.get("id") or "") if isinstance(user, dict) else ""
                if uid in by_id:
                    by_id[uid].extra.update({k: v for k, v in el.items() if k != "user"})
                else:
                    stack.append(el)


def merge_pages(pages: list[ViewerPage]) -> list[Viewer]:
    """Concatenate pages in order, keeping each user's first (highest) position."""
    seen, out = set(), []
    for p in pages:
        for v in p.viewers:
            if v.user_id not in seen:
                seen.add(v.user_id)
                out.append(v)
    return out


def like_flag(extra: dict | None) -> bool | None:
    """Whether this viewer liked the story; None if the entry carries no like field."""
    for k in LIKE_KEYS:
        if k in (extra or {}):
            return bool(extra[k])
    return None


def liked_set(entries) -> set[str] | None:
    """User ids who liked, from (user_id, extra) pairs; None when no entry has like data at all."""
    flags = [(u, like_flag(extra)) for u, extra in entries]
    if all(f is None for _, f in flags):
        return None
    return {u for u, f in flags if f}


def media_pk_from(url: str, post_data: str | None = None) -> str | None:
    """The story item (media pk) a viewer request is about, from the URL or GraphQL variables."""
    if m := re.search(r"/media/(\d+)", url):
        return m.group(1)
    if post_data and (m := re.search(r'"?media_id"?\s*[:=]\s*"?(\d+)', unquote(post_data))):
        return m.group(1)
    return None


def _iso(epoch) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds")


def extract_story_items(body, owner_id: str) -> tuple[str | None, list[StoryItem]] | None:
    """(owner username, live story items) from any reel-shaped JSON, or None if no reel for owner_id."""
    stack = [body]
    while stack:
        d = stack.pop()
        if isinstance(d, list):
            stack.extend(d)
            continue
        if not isinstance(d, dict):
            continue
        user = d.get("user") or d.get("owner")
        if isinstance(d.get("items"), list) and isinstance(user, dict) and str(user.get("pk") or user.get("id")) == str(owner_id):
            items = []
            for it in d["items"]:
                if not isinstance(it, dict) or not it.get("taken_at"):
                    continue
                pk = str(it.get("pk") or str(it.get("id", "")).split("_")[0])
                expires = it.get("expiring_at") or int(it["taken_at"]) + 86400
                items.append(StoryItem(
                    id=pk,
                    posted_at=_iso(it["taken_at"]),
                    expires_at=_iso(expires),
                    media_type={1: "image", 2: "video"}.get(it.get("media_type"), str(it.get("media_type"))),
                    viewer_count=it.get("total_viewer_count", it.get("viewer_count")),
                ))
            return user.get("username"), items
        stack.extend(d.values())
    return None
