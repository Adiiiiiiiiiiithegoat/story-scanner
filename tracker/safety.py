"""Stop-on-anything-weird rules. A SafetyStop is never retried: the loop exits non-zero."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class SafetyStop(Exception):
    """Logged out, challenge, checkpoint or rate limit. Resolve manually in a normal browser."""


class NotLoggedIn(SafetyStop):
    """No saved session at all (nothing was sent to Instagram). Exit code 3: run `login`."""


BAD_PATH_RE = re.compile(r"^/(challenge|checkpoint|accounts/login|accounts/suspended|accounts/disabled|suspended)", re.I)
BAD_JSON_RE = re.compile(
    r"challenge_required|checkpoint_required|login_required|feedback_required|consent_required"
    r"|please wait a few minutes|try again later|rate.?limit|suspicious", re.I)
BAD_TEXT_RE = re.compile(
    r"suspicious login attempt|try again later|we restrict certain activity|help us confirm (that )?it'?s you"
    r"|confirm it'?s you|your account has been (disabled|suspended)|we suspended your account", re.I)


def check_url(url: str) -> str | None:
    parts = urlsplit(url)
    if "instagram.com" in parts.netloc and BAD_PATH_RE.match(parts.path):
        return f"Instagram redirected to {parts.path}"
    return None


def check_json(body, status: int = 200) -> str | None:
    if status == 429:
        return "HTTP 429 (rate limited)"
    objs = body["__multi__"] if isinstance(body, dict) and "__multi__" in body else [body]
    for o in objs:
        if not isinstance(o, dict):
            continue
        msg = " ".join(str(o.get(k) or "") for k in ("message", "error_type", "error", "title")).strip()
        if BAD_JSON_RE.search(msg) or o.get("challenge") or o.get("checkpoint_url") or o.get("spam") is True or o.get("require_login"):
            return f"Instagram response: {msg or sorted(o)[:6]}"
    return None


def check_page(page, login_css: str) -> None:
    """Raise SafetyStop if the current page is a login/challenge/rate-limit page."""
    if reason := check_url(page.url):
        raise SafetyStop(reason)
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        text = ""
    if m := BAD_TEXT_RE.search(text):
        raise SafetyStop(f"page says {m.group(0)!r}")
    if page.locator(login_css).count():
        raise SafetyStop("login form shown: session expired, run `python -m tracker login`")


def stop_requested(cfg) -> bool:
    return cfg.stop_file.exists()


TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode('__TITLE__')) > $null
$x.Item(1).AppendChild($t.CreateTextNode('__MSG__')) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title: str, message: str) -> None:
    """Best-effort Windows toast via PowerShell; never raises."""
    if sys.platform != "win32":
        return
    esc = lambda s: s.replace("'", "''")[:200]
    script = TOAST_PS.replace("__TITLE__", esc(title)).replace("__MSG__", esc(message))
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        log.debug("toast failed: %s", e)
