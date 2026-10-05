"""Persistent browser profile at data/profile: manual login once, reuse the saved session after."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from playwright.sync_api import Error as PWError, sync_playwright

log = logging.getLogger(__name__)
HOME = "https://www.instagram.com/"


@contextmanager
def open_context(cfg, headless: bool | None = None):
    """Yield a persistent context; installed Chrome if available, else bundled Chromium. Always closed."""
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(cfg.profile_dir), headless=cfg.headless if headless is None else headless,
                  viewport={"width": 1280, "height": 900}, locale="en-US")
    with sync_playwright() as pw:
        ctx = None
        if cfg.browser_channel:
            try:
                ctx = pw.chromium.launch_persistent_context(channel=cfg.browser_channel, **kwargs)
            except PWError as e:
                log.info("browser channel %r unavailable (%s), using bundled Chromium", cfg.browser_channel, str(e).splitlines()[0])
        if ctx is None:
            ctx = pw.chromium.launch_persistent_context(**kwargs)
        ctx.set_default_timeout(cfg.timeout_seconds * 1000)
        try:
            yield ctx
        finally:
            ctx.close()


def session_user_id(ctx) -> str | None:
    """Logged-in user id from cookies, or None when there's no session."""
    cookies = {c["name"]: c["value"] for c in ctx.cookies(HOME)}
    return cookies.get("ds_user_id") if cookies.get("sessionid") else None


def login(cfg, timeout_s: int = 600) -> bool:
    """Open a headed browser and wait for the user to log in by hand. Never touches the password."""
    with open_context(cfg, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded")
        if session_user_id(ctx):
            log.info("Already logged in (user id %s). Session is saved in %s", session_user_id(ctx), cfg.profile_dir)
            return True
        log.info("Log in in the browser window (2FA is fine). Waiting up to %d minutes...", timeout_s // 60)
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if uid := session_user_id(ctx):
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(3000)  # let Instagram finish writing cookies/storage
                    log.info("Login detected (user id %s). Session saved in %s", uid, cfg.profile_dir)
                    return True
                page.wait_for_timeout(2000)
        except PWError:
            log.error("Browser window was closed before login finished.")
            return False
        log.error("Timed out waiting for login.")
        return False
