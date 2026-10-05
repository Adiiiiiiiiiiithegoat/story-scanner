"""Open my stories, open the "Seen by" sheet, and record the JSON Instagram's own web app loads.

Read-only by construction. The only interactions are: navigating to my own story URLs, clicking
"View story" / "Seen by", mouse-wheel scrolling inside the viewer sheet, and pressing Escape.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import parser, safety, session
from .parser import Viewer
from .safety import SafetyStop

log = logging.getLogger(__name__)

# ---- UI / endpoint constants: update these when Instagram changes its web app -------------------
HOME = session.HOME
STORY_URL = HOME + "stories/{username}/{item_id}/"
REELS_MEDIA_PATH = "/api/v1/feed/reels_media/?reel_ids={uid}"  # what the web app calls to load a reel
IG_APP_ID = "936619743392459"  # public instagram.com web app id, sent by the site on every API call
VIEW_STORY_RE = re.compile(r"^\s*View stor(y|ies)\s*$", re.I)  # interstitial button, if shown
SEEN_BY_RE = re.compile(r"^\s*(Seen by|Activity|Viewers)\b", re.I)  # opener of the viewer sheet
SEEN_BY_CSS = "[aria-label*='Seen by' i], [aria-label*='viewers' i]"
LOGIN_FORM_CSS = "input[name='username'], form#loginForm"
PROFILE_HREF_RE = re.compile(r"^/([A-Za-z0-9._]{1,30})/?$")
RESERVED_PATHS = {"explore", "accounts", "direct", "reels", "stories", "p", "reel", "about", "legal", "web", "developer"}
OPENER_WAIT_S = 8          # how long to look for the Seen-by opener
FIRST_RESPONSE_WAIT_S = 8  # how long to wait for the first viewer response after opening the sheet
PAUSE_MS = (800, 2500)     # human-like pause between scrolls
# --------------------------------------------------------------------------------------------------

FETCH_JS = """async ([path, appId]) => {
  const r = await fetch(path, {credentials: 'include', headers: {'X-IG-App-ID': appId, 'X-Requested-With': 'XMLHttpRequest'}});
  return {status: r.status, text: await r.text()};
}"""


@dataclass
class ItemCapture:
    item_id: str
    taken_at: str
    source: str  # "api" or "dom"
    viewers: list[Viewer]


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


class Recorder:
    """page.on('response') handler: saves matched viewer responses and watches for safety signals.

    Exceptions can't escape Playwright event handlers, so problems are parked in stop_reason and
    re-raised by raise_if_stopped() from the main flow.
    """

    def __init__(self, cfg):
        self.raw_dir = cfg.raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.stop_reason: str | None = None
        self.current: str | None = None
        self.wanted: set[str] = set()
        self.started: dict[str, datetime] = {}
        self.pages: dict[str, list[parser.ViewerPage]] = defaultdict(list)
        self.page_no: dict[str, int] = defaultdict(int)

    def begin(self, item_id: str) -> None:
        self.current = item_id
        self.started.setdefault(item_id, datetime.now(timezone.utc))

    def raise_if_stopped(self) -> None:
        if self.stop_reason:
            raise SafetyStop(self.stop_reason)

    def save_raw(self, item_id: str, suffix: str, payload: dict) -> None:
        ts = self.started.get(item_id, datetime.now(timezone.utc))
        payload = {"taken_at": ts.isoformat(timespec="seconds"), **payload}
        path = self.raw_dir / f"{_stamp(ts)}_{item_id}_{suffix}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    def on_response(self, resp) -> None:
        try:
            self._handle(resp)
        except Exception as e:  # never let a handler error kill the capture
            log.debug("response handler error for %s: %s", resp.url, e)

    def _handle(self, resp) -> None:
        url = resp.url
        if "instagram.com" not in url:
            return
        kind = resp.request.resource_type
        if kind == "document" and (reason := safety.check_url(url)):
            self.stop_reason = reason
        if resp.status == 429:
            self.stop_reason = f"HTTP 429 (rate limited) from {url.split('?')[0]}"
        if kind not in ("xhr", "fetch") or 300 <= resp.status < 400:
            return
        try:
            body = parser.loads_lenient(resp.text())
        except Exception:
            return
        if reason := safety.check_json(body, resp.status):
            self.stop_reason = reason
            return
        pattern = parser.match_viewer_response(url, body)
        if not pattern or not self.current:
            return
        item = parser.media_pk_from(url, resp.request.post_data)
        item = item if item in self.wanted else self.current
        page = parser.parse_viewers(body, pattern)
        self.pages[item].append(page)
        n = self.page_no[item]
        self.page_no[item] += 1
        self.save_raw(item, f"{n:03d}", {"source": "api", "url": url, "pattern": pattern, "body": body})
        log.info("  matched %s: page %d, %d viewers%s", pattern, n, len(page.viewers),
                 f" of {page.total}" if page.total else "")


def _settle(page) -> None:
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass


def _check(page, rec: Recorder) -> None:
    rec.raise_if_stopped()
    safety.check_page(page, LOGIN_FORM_CSS)


def _fetch_items(page, rec: Recorder, uid: str, cfg) -> tuple[str | None, list[parser.StoryItem]]:
    res = page.evaluate(FETCH_JS, [REELS_MEDIA_PATH.format(uid=uid), IG_APP_ID])
    rec.raise_if_stopped()
    if res["status"] == 429:
        raise SafetyStop("HTTP 429 (rate limited) while listing stories")
    body = None
    try:
        body = parser.loads_lenient(res["text"])
    except ValueError:
        log.warning("reels_media returned non-JSON (HTTP %s)", res["status"])
    if body is not None:
        if reason := safety.check_json(body, res["status"]):
            raise SafetyStop(reason)
        if found := parser.extract_story_items(body, uid):
            return found
        if isinstance(body, dict) and body.get("status") == "ok" and "reels" in body:
            return cfg.username or None, []  # well-formed answer: no live story
    if not cfg.username:
        raise RuntimeError("Couldn't list stories via the API and no `username` is set in config.toml to fall back on")
    # Fallback: open my stories like a user would and sniff the reel JSON the page loads.
    log.warning("reels_media lookup failed; falling back to opening /stories/%s/", cfg.username)
    sniffed: list = []
    handler = lambda r: sniffed.append(r)
    page.on("response", handler)
    page.goto(f"{HOME}stories/{cfg.username}/", wait_until="domcontentloaded")
    _settle(page)
    _check(page, rec)
    page.remove_listener("response", handler)
    for r in sniffed:
        try:
            if found := parser.extract_story_items(parser.loads_lenient(r.text()), uid):
                return found
        except Exception:
            continue
    return cfg.username, []


def _find_opener(page):
    candidates = [
        lambda: page.get_by_role("button", name=SEEN_BY_RE),
        lambda: page.get_by_role("link", name=SEEN_BY_RE),
        lambda: page.get_by_text(SEEN_BY_RE),
        lambda: page.locator(SEEN_BY_CSS),
    ]
    clicked_view = False
    deadline = time.monotonic() + OPENER_WAIT_S
    while time.monotonic() < deadline:
        for make in candidates:
            loc = make().first
            if loc.count() and loc.is_visible():
                return loc
        view = page.get_by_role("button", name=VIEW_STORY_RE).first
        if not clicked_view and view.count() and view.is_visible():
            view.click()
            clicked_view = True
        page.wait_for_timeout(300)
    return None


def _dom_usernames(scope, own: str | None) -> list[str]:
    hrefs = scope.locator("a[href]").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    names = []
    for h in hrefs:
        m = PROFILE_HREF_RE.match(h or "")
        if m and m.group(1) not in RESERVED_PATHS and m.group(1) != own and m.group(1) not in names:
            names.append(m.group(1))
    return names


def _capture_item(page, rec: Recorder, cfg, username: str, item: parser.StoryItem) -> ItemCapture | None:
    if rec.pages.get(item.id):
        return None  # already captured (the story auto-advanced into it earlier this cycle)
    rec.begin(item.id)
    log.info("Story item %s (%s, %s viewers reported)", item.id, item.media_type, item.viewer_count)
    page.goto(STORY_URL.format(username=username, item_id=item.id), wait_until="domcontentloaded")
    _settle(page)
    _check(page, rec)
    opener = _find_opener(page)
    if opener is None:
        log.warning("  'Seen by' opener not found for item %s; selectors may need updating", item.id)
        return None
    m = re.search(r"/stories/[^/]+/(\d+)", page.url)
    key = m.group(1) if m and m.group(1) in rec.wanted else item.id  # the story may have auto-advanced
    rec.begin(key)
    opener.click()
    dialog = page.get_by_role("dialog").last
    try:
        dialog.wait_for(state="visible", timeout=5000)
    except Exception:
        dialog = page.locator("body")
    deadline = time.monotonic() + FIRST_RESPONSE_WAIT_S
    while not rec.pages.get(key) and time.monotonic() < deadline:
        rec.raise_if_stopped()
        page.wait_for_timeout(250)

    dom_names = [] if rec.pages.get(key) else _dom_usernames(dialog, username)
    box = dialog.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6)
    count = lambda: sum(len(p.viewers) for p in rec.pages.get(key, [])) or len(dom_names)
    last, last_change = count(), time.monotonic()
    for _ in range(cfg.max_scrolls):
        rec.raise_if_stopped()
        pages = rec.pages.get(key)
        if pages and pages[-1].has_more is False:
            break
        page.mouse.wheel(0, random.randint(500, 1100))
        page.wait_for_timeout(random.uniform(*PAUSE_MS))
        if not rec.pages.get(key):
            dom_names += [n for n in _dom_usernames(dialog, username) if n not in dom_names]
        if (c := count()) != last:
            last, last_change = c, time.monotonic()
        elif time.monotonic() - last_change >= cfg.scroll_idle_seconds:
            break
    rec.raise_if_stopped()
    page.keyboard.press("Escape")
    taken_at = rec.started[key].isoformat(timespec="seconds")

    if rec.pages.get(key):
        viewers = parser.merge_pages(rec.pages[key])
        log.info("  captured %d viewers from %d response(s)", len(viewers), len(rec.pages[key]))
        return ItemCapture(key, taken_at, "api", viewers)
    if dom_names:
        log.warning("  no viewer JSON captured for %s; using DOM fallback (%d usernames, lower trust)", key, len(dom_names))
        rec.save_raw(key, "dom", {"source": "dom", "usernames": dom_names})
        return ItemCapture(key, taken_at, "dom", [Viewer("", n) for n in dom_names])
    log.warning("  no viewers captured for %s", key)
    return None


def run_cycle(cfg, only_check: bool = False) -> tuple[str | None, list[parser.StoryItem], list[ItemCapture]]:
    """One browser session: verify login, list live story items, and (unless only_check) capture each."""
    with session.open_context(cfg) as ctx:
        uid = session.session_user_id(ctx)
        if not uid:
            raise SafetyStop("not logged in: run `python -m tracker login`")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        rec = Recorder(cfg)
        page.on("response", rec.on_response)
        page.goto(HOME, wait_until="domcontentloaded")
        _settle(page)
        _check(page, rec)
        if not session.session_user_id(ctx):
            raise SafetyStop("session cookie disappeared: logged out")
        username, items = _fetch_items(page, rec, uid, cfg)
        now = datetime.now(timezone.utc).isoformat()
        items = [i for i in items if i.expires_at > now]
        if only_check or not items:
            return username, items, []
        username = username or cfg.username
        rec.wanted = {i.id for i in items}
        captures = []
        for n, item in enumerate(items):
            if item.viewer_count == 0:
                log.info("Story item %s has no viewers yet, skipping", item.id)
                continue
            if n:
                page.wait_for_timeout(random.uniform(1500, 4000))
            if cap := _capture_item(page, rec, cfg, username, item):
                captures.append(cap)
        return username, items, captures
