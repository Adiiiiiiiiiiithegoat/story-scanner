# Story Scanner: plan

## Tracker
- [x] One-time manual login with a persistent browser profile
- [x] Captures the "Seen by" list from Instagram's own JSON, with a DOM fallback
- [x] Rewatch detection that knows likers sit above non-likers
- [x] Safety stops on logout, challenge or 429, plus an OS-level run lock
- [x] Static dashboard report and CSV export
- [x] start.bat launcher

## Dashboard and scheduling
- [ ] Commit the uncommitted dashboard columns, likes column and scan-interval rework
- [ ] Register the StoryRewatchTracker Task Scheduler entry so it survives reboots
- [ ] Tune the [analysis] settings after a few days of real data

## Multi-device and multi-account
- [ ] Track a second Instagram account (the spam account) from the home PC
- [ ] Host only the dashboard, behind a password with an account picker, for the office laptop
