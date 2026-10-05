# Instagram Story Rewatch Tracker

Tracks who views **your own** Instagram stories. While a story is live it snapshots the "Seen by" list about every 30 minutes and flags **possible rewatches** from how viewers move in that list. The results go into a single static dashboard (`data/report.html`).

> **Rewatch counts are estimates based on list movement; Instagram doesn't report real rewatches.**

## ⚠️ Account risk: read this first

Automating your logged-in account breaks Instagram's Terms of Use. It can lead to challenges, temporary blocks or, in the worst case, a disabled account. The tool keeps the risk low but can't remove it:

- You log in **once, by hand**. It never asks for, stores or types your password.
- It polls rarely: at least every 15 minutes (hard floor), 30 ± 7 min by default, every 2 h when you have no live story, and not at all during quiet hours.
- It's read-only. It only opens your stories, opens the viewer sheet, scrolls and presses Escape. It never likes, replies, reacts, follows or sends anything.
- It **stops immediately** (exit code 2, with a log entry and a Windows toast) on a logout, login redirect, challenge, checkpoint, "suspicious login", "try again later" or HTTP 429. It never retries through any of these.

**Try it on a secondary account first** for a few days before you point it at your main one.

## Setup (Windows 11, PowerShell)

```powershell
cd D:\Projects\instagram\story-tracker
python -m venv .venv; .\.venv\Scripts\Activate.ps1   # optional
pip install -r requirements.txt
playwright install chromium      # fallback browser; installed Google Chrome is used if present
python -m tracker login          # log in by hand in the window that opens (2FA is fine)
python -m tracker check          # confirms the session and lists your live story items
```

`config.toml` is created from `config.example.toml` on first run. Every setting is described there.

## Commands (`python -m tracker <command>`)

| Command | What it does |
|---|---|
| `login` | Opens a **headed** browser with the persistent profile at `data/profile/` and waits up to 10 min for you to log in. It detects the `sessionid` cookie, prints success and closes. |
| `check` | Opens the saved session headless, confirms you're logged in, and lists your live story items (id, type, posted/expires, viewer count). |
| `snapshot` | Runs one full capture cycle now. It saves raw JSON, writes the DB and regenerates the report. |
| `run` | The long-running loop (see Scheduler). |
| `report` | Regenerates `data/report.html` and prints its path. |
| `export --csv` | Writes `stories`, `viewers`, `snapshots`, `snapshot_viewers` and `events` to `data/export/*.csv` (UTF-8 with BOM, so Excel opens it cleanly). |
| `reparse` | Rebuilds every snapshot and event from `data/raw/`, for example after a parser fix. |

Exit codes: `0` ok, `1` login failed or timed out, `2` safety stop.

## How capture works

1. A persistent Chrome profile is launched (falling back to bundled Chromium) and instagram.com is opened. The tool checks for login/challenge pages and the session cookie.
2. Your live story items come from the same `reels_media` call the web app makes. If that call fails and `username` is set in the config, the tool opens `/stories/<you>/` and reads the reel JSON the page loads.
3. For each item it opens `/stories/<you>/<item id>/`, clicks **Seen by**, and wheel-scrolls the sheet with random 0.8–2.5 s pauses. It stops when the response says there are no more pages, when nothing new has arrived for 5 s, or after `max_scrolls`.
4. A `page.on("response")` listener records Instagram's own JSON responses. A response counts as a viewer list only if **both** of these hold:
   - its URL matches `list_reel_media_viewer`, `story_viewers`, `/graphql` or `/api/v1/media/<id>`
   - its JSON contains a list of user-like objects (username + id), either flat REST `users`, wrapped `{user: …}`, or GraphQL `edges[].node.user`. Generic URLs (GraphQL, media API) also need a viewer-ish JSON path.

   The pattern that matched is logged. Every matched response is saved to `data/raw/{UTC timestamp}_{item id}_{page}.json`.
5. Order is kept exactly as returned (rank 0 = top). Unknown per-viewer fields (`has_liked`, reactions, timestamps…) go into `snapshot_viewers.extra_json`.
6. **DOM fallback:** if no viewer JSON arrives, the tool reads profile links from the open sheet in DOM order. That snapshot is stored with `source="dom"`, any events from it are capped at low confidence, and it's saved as `…_dom.json`.
7. The browser is closed after every cycle.

**Decision:** the tool moves between story items by opening each item's URL rather than clicking "Next". Stories auto-advance, so clicking Next is racy. A direct URL is deterministic and keeps captures keyed to the right item id. Responses are also keyed by the media id found in the request URL or GraphQL variables, so an auto-advance can't misfile them.

## How the analysis works (and its limits)

Each snapshot is compared with the previous snapshot of the **same item** (`tracker/analysis.py`, pure functions).

**Likes split the list into two blocks.** Instagram puts everyone who liked the story at the top, then everyone else. Each block is ordered by most recent view:

- A **non-liker who rewatches** moves to the top of the non-liker block, just under the oldest liker. That can be well below the top of the full list.
- A **liker who rewatches** moves to the top of the whole list.

So when the viewer data has a like flag (`has_liked`), the steps below run **separately on each block**. A non-liker rewatch is caught no matter how many likers sit above it.

**A new like from someone who had already viewed is its own event.** A like happens while viewing, so they must have reopened the story. This is recorded as a high-confidence possible rewatch (marked ♥ on the dashboard). It doesn't depend on ordering, so it's kept even when the pair is a reshuffle. A brand-new viewer who likes is still a first view.

Steps (per block, or on the whole list when there's no like data):

1. **New viewers** (only in the new snapshot) are first views. They never produce events.
2. **Expected drift:** each returning viewer's previous rank is shifted by the number of new viewers now above them, so normal push-down isn't counted as movement. `jump = adjusted_prev_rank − new_rank` (positive = moved up).
3. **Reshuffle check:** Spearman ρ is computed between the two orders over returning viewers. The pair counts as a reshuffle, with the snapshot flagged and **no** events, if either:
   - ρ < 0.6
   - more than 30% of returning viewers moved more than 3 places (and at least 3 did)

   The score is stored either way.
4. **Flag rule:** a possible rewatch needs all of these:
   - jump ≥ max(3, 10% of the list)
   - the viewer lands in the top zone (top 5, or top 10% for lists over 50)
   - the viewer was *not* already in the top zone
5. **Confidence:**
   - **high**: lands in the top 3, jump ≥ 2× the minimum, ρ ≥ 0.85, and at most 2 viewers jumped in that pair
   - **medium**: ρ ≥ 0.7
   - **low**: anything else that passed the flag rule, any DOM-fallback snapshot, or a list too short to judge
6. **Per viewer, per story:** first seen, latest rank, best rank, event counts by confidence, and a **rewatch score** (high 1.0, medium 0.6, low 0.3).

Deviations from a literal reading of the spec, chosen so short lists behave sensibly:

- **ρ leaves out the viewers who passed the jump threshold.** Otherwise a single real jumper tanks ρ: with 8 viewers, a last→first move alone gives ρ = 0.33 and would look like a reshuffle. Real reshuffles are still caught, because they involve many movers.
- **ρ needs at least 5 stable viewers.** Below that it's treated as unknown, and events from those pairs are capped at low.
- **Short lists (≤ 50) use a top zone of min(5, half the list).** With a 4-person list a jump from #4 to #1 can still be detected.

**Known false positives:**

- Instagram's own interaction/closeness ranking on bigger lists. Big reshuffles are caught; small partial re-ranks aren't.
- Someone replying or reacting through DMs. That isn't visible in the viewer data.
- Missing like data. If a snapshot has no like flag (the DOM fallback, or Instagram renaming the field), the whole list is compared as one block. A non-liker rewatch that lands under many likers can then be missed.
- A viewer whose first view and like happen exactly as a snapshot is taken can show up as a like event. This is unlikely with 30-minute polling.
- Instagram A/B tests that change how the list is ordered.
- An early-stopped capture that makes deep-list viewers "vanish" and "reappear".

"First seen" is the time of the first snapshot that contained the viewer, so it's an upper bound on when they actually first viewed.

### Tuning thresholds

All thresholds are in `[analysis]` in `config.toml`. Rules of thumb:

- **Too many flags on a big audience:** raise `reshuffle_threshold` (e.g. 0.7) and `min_jump_frac` (e.g. 0.15), or lower `max_movers_fraction`.
- **Missing obvious rewatches on a small audience:** lower `min_jump_abs` to 2.
- **"High" too generous:** raise `high_min_corr` or `high_jump_factor`.

After changing thresholds, run `python -m tracker reparse` to recompute every event from the raw captures.

## Scheduler (`run`)

- Every cycle checks the session and lists live items. With no live story it sleeps 120 ± 15 min. With a live story it captures every item, then sleeps 30 ± 7 min, never less than 15.
- Nothing runs during quiet hours (default 02:00–07:00 IST).
- Expired items (past `expiring_at`, or no longer returned) are no longer captured.
- The dashboard is regenerated after each cycle.
- Transient browser/network errors back off at 30 s, 60 s, then 120 s. After 3 retries the cycle is skipped.
- **Stop it:** Ctrl+C, or create `data\STOP` (`New-Item data\STOP`), which is checked at least every 10 s. Delete the file before starting again.
- Logs: `data/logs/tracker.log` (rotating, 5 × 1 MB, debug level). The console shows a concise summary.

### Run at Windows startup (Task Scheduler)

Run this from the project folder in PowerShell. Use the venv's `pythonw.exe` if you made one.

```powershell
$dir = (Get-Location).Path
$py  = (Get-Command pythonw).Source        # or "$dir\.venv\Scripts\pythonw.exe"
$action  = New-ScheduledTaskAction -Execute $py -Argument "-m tracker run" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 0
Register-ScheduledTask -TaskName "StoryRewatchTracker" -Action $action -Trigger $trigger -Settings $settings -Description "Instagram story rewatch tracker"
```

Stop, start or remove it:

```powershell
New-Item data\STOP                                   # graceful stop at the next check
Stop-ScheduledTask -TaskName "StoryRewatchTracker"   # hard stop
Start-ScheduledTask -TaskName "StoryRewatchTracker"
Unregister-ScheduledTask -TaskName "StoryRewatchTracker" -Confirm:$false
```

`RestartCount 0` matters: after a safety stop the task must **not** restart on its own.

## Dashboard

`data/report.html` is one self-contained file (inline CSS/JS/data, no network requests). It follows your light/dark preference and works on a phone. It contains:

- header stats and the estimate banner
- an all-stories leaderboard with a 7 / 30 days / all-time filter
- one sortable table per story item (default sort: rewatch score)

Click a viewer to see their rank over time. Reshuffle snapshots are grey and flagged points are ringed by confidence. Their event reasons are listed below the chart.

## Troubleshooting

- **`check` says not logged in, or a safety stop says "login form shown":** run `python -m tracker login` again. Use the same browser each time: with `browser_channel = "chrome"`, don't switch to `""` later, because the profile may not carry over.
- **"'Seen by' opener not found":** Instagram changed the UI. Run with `headless = false` to watch, then update the constants block at the top of `tracker/capture.py` (`SEEN_BY_RE`, `SEEN_BY_CSS`, `VIEW_STORY_RE`).
- **No viewer responses captured (DOM fallback warnings):** the endpoint or shape changed. Open DevTools → Network in a normal browser, open your viewer list, and find the response carrying the viewer users. Add its URL fragment to `URL_PATTERNS` in `tracker/parser.py`, then run `reparse`. `data/logs/tracker.log` logs which pattern matched for every captured page.
- **Likes not detected (no ♥ events, liker block ignored):** look in a raw capture for the per-viewer like field and add its name to `LIKE_KEYS` in `tracker/parser.py`, then run `reparse`.
- **Story listing fails:** set `username` in `config.toml` to enable the page-sniffing fallback, and check `REELS_MEDIA_PATH` / `IG_APP_ID` in `capture.py`.
- **Re-parsing:** every matched response is in `data/raw/` as `{"taken_at", "source", "url", "pattern", "body"}`. Fix `parser.py`, then run `python -m tracker reparse`. It rebuilds snapshots and events (stories and viewers are kept) and re-renders the report.
- **Headless gets challenged more than headed:** set `headless = false`. A window will open briefly each cycle.

## Tests

```powershell
pytest
```

The tests use synthetic fixtures (`tests/fixtures/`) and in-memory SQLite. They need no network and no browser.
