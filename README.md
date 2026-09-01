# youtube-dashboard

A self-hosted YouTube subscription tracker. Subscribe to channels by URL/handle/ID, and it polls their RSS feeds in the background so you get a simple "new videos" dashboard without needing a YouTube API key.

## What this is

`app/main.py` is a FastAPI app. On startup it starts a background scheduler thread (`app/scheduler.py`) that polls every subscribed channel's RSS feed every `POLL_INTERVAL` seconds (default 600s / 10 min), fetching new videos and storing them in a SQLite database (`youtube_tracker.db`).

- `app/rss.py` — resolves a channel URL/handle/ID to a raw channel ID, and parses YouTube's Atom RSS feeds. Prefers the `UULF` (long-form only, Shorts-excluded) feed and falls back to the full uploads feed if that's empty.
- `app/models.py` / `app/database.py` — SQLAlchemy models (`Channel`, `Video`) and session setup. `Video` carries `playback_seconds` / `duration_seconds` / `playback_updated_at` for resume-on-reopen; `Channel` carries `last_poll_ok` / `last_poll_error` for feed health.
- `app/crud.py` — database read/write helpers used by routes and the scheduler.
- `app/routes.py` — the dashboard page (`/`) plus a small JSON API for subscribing/unsubscribing, forcing a poll, bulk/single watched + bookmarked toggles, saving playback position, and a `/api/stats` summary.
- `app/templates/index.html` — the dashboard UI. Single self-contained file: inline CSS (no CDN), inline SVG icons, self-hosted Orbitron font. Shares the suite's "Cybertron HUD" look with `home-dashboard` / `screen-time-dashboard` (same CSS-variable vocabulary, the shared `logo.svg`, a light/dark toggle). Mobile-first layout — the channel sidebar becomes a slide-in drawer on narrow screens.
- `app/database.py` — also runs `run_lightweight_migrations()` on boot: idempotent `ALTER TABLE … ADD COLUMN` for columns added after first release (cheap and safe on SQLite), so no migration tool is needed.
- `fix-titles.py` — one-off script to re-fetch and fix channel titles that were saved incorrectly (e.g. as "Videos").

### Dashboard features

- **Feed Vitals** strip: subscriptions, unwatched (the "inbox" number), resume (part-watched), saved, new-in-7-days, last poll (relative time, refreshed live), and feed health (count of feeds whose last poll failed). Backed by `GET /api/stats`, re-polled client-side every 60s.
- Filter by channel, by status (all / unwatched / **resume** / saved), by **time range** (24h / 7d / 30d / all), and **sort** newest ↔ oldest.
- Fuzzy text search over video title/description.
- Per-channel **unwatched badge** in the sidebar, plus a warning marker on any feed whose last poll errored or has gone stale (older than 3× the poll interval). Hover for the error text.
- **Mark all watched** — clears the inbox for the current channel (or every feed).
- Grid / list view toggle (persisted). Relative timestamps with the absolute date on hover.
- **Resume playback** — the player reports its position back to the server (`POST /api/videos/{id}/progress`) every 10s while playing and on pause / close / end / tab-hide (`navigator.sendBeacon`). Reopening a video restarts where you left off (`playerVars.start`); a thin progress bar on the thumbnail shows how far in you are. Playing a video to the end clears its resume point and auto-marks it watched. In-progress videos are exempt from the per-channel pruner so they aren't deleted before you finish.
- **Cinema player** — tapping a video loads it through the **YouTube IFrame Player API** (needed to read/restore position), requests the Fullscreen API on the player, and (where the browser supports it — Android Chrome/Firefox) locks orientation to landscape, so it goes straight to a full-screen landscape player with almost no chrome. Leaving fullscreen (back / swipe / the close button) tears the player down and unlocks orientation. Desktop and iOS Safari, which reject `screen.orientation.lock`, fall back to a centered 16:9 player. No navigation to youtube.com.
- **Keyboard shortcuts** (press `?`): `j`/`k` navigate, `o` open, `w` watched, `b` saved, `/` search, `r` refresh, `g` grid/list.

## Requirements

- Python 3
- `tmux` (optional, used by `run.bash` for a detached background run)

## Setup

```bash
bash setup.bash
```

Creates a venv named `dashboard` and installs `requirements.txt` into it. Skips creation if a venv already exists in the directory.

## Configuration

Optional `.env` file (or environment variables) in the project root:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./youtube_tracker.db` | SQLAlchemy database URL |
| `POLL_INTERVAL` | `600` | Seconds between RSS polls |
| `KEEP_PER_CHANNEL` | `2` | How many recent videos to retain per channel. Each poll adds at most this many fresh videos per channel and the pruner trims back down to it; bookmarked videos are always kept on top. Raise it for a longer tail. |
| `BASE_PATH` | `""` | External mount prefix (e.g. `/youtube-dashboard`) — see [Tailnet routing](#tailnet-routing) |

## Run

```bash
bash run.bash
```

If `tmux` is available, this starts the app in a detached session named `youtube_tracker`; otherwise it runs in the foreground. Either way it calls `python3 run.py`, which starts uvicorn bound to `127.0.0.1:8000` (loopback only; the tailnet reaches it through `tailscale serve`). The reloader is off — this is a long-lived service, not a dev run.

Attach to the tmux session: `tmux attach -t youtube_tracker` (detach with `Ctrl-b` then `d`).

To stop it, kill the tmux session (`tmux kill-session -t youtube_tracker`) or the `uvicorn`/`python3 run.py` process if running in the foreground.

## Use

Open `http://localhost:8000`. Subscribe to a channel by pasting its URL, `@handle`, or raw `UC...` ID into the subscribe form. New videos show up after the next poll (or immediately via the "poll now" API endpoints below).

Also reachable tailnet-wide at `https://groot.tail088f09.ts.net/youtube-dashboard`, mounted alongside Nextcloud and camera-server on the same hostname via `tailscale serve` (see [Tailnet routing](#tailnet-routing)).

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/channels` | Subscribe to a channel (accepts URL/handle/ID) |
| `DELETE` | `/api/channels/{channel_id}` | Unsubscribe |
| `PATCH` | `/api/channels/{channel_id}` | Rename a channel's stored title |
| `POST` | `/api/channels/{channel_id}/poll` | Force-poll a single channel now |
| `POST` | `/api/channels/poll` | Force-poll all channels now |
| `PUT` | `/api/videos/{video_id}` | Update a single video's watched/bookmarked status |
| `POST` | `/api/videos/{video_id}/progress` | Save playback position for resume-on-reopen. Body: `{"seconds": 123.4, "duration": 600}`. Auto-marks watched near the end. |
| `POST` | `/api/videos/mark-watched` | Bulk-mark unwatched videos watched. Body: `{"channel_id": "UC…"}` to scope to one channel, or `{}` / `{"channel_id": null}` for every feed |
| `GET` | `/api/stats` | Summary numbers for the Feed Vitals strip |
| `GET` | `/manifest.webmanifest` | PWA manifest (prefix-aware) |

## Files

| File | Purpose |
|---|---|
| `run.py` | Entrypoint, starts uvicorn |
| `run.bash` | Activates the venv and starts the app (tmux if available) |
| `setup.bash` | Creates the `dashboard` venv and installs dependencies |
| `quick-push.sh` | Convenience `git add -A && git commit && git push` |
| `fix-titles.py` | One-off maintenance script to re-fetch channel titles |
| `app/static/fonts/orbitron.woff2` | Vendored display font (copy of the one in `home-dashboard`) |
| `dashboard/` | Virtual environment (not portable — recreate with `setup.bash`) |
| `youtube_tracker.db` | SQLite database (created on first run) |

## Notes

- No YouTube API key required — everything is read from public RSS feeds, so subscription counts and features are limited to what the feed exposes (recent uploads only, not full channel history).
- The scheduler also prunes stored videos down to `KEEP_PER_CHANNEL` (default 2) most recent per channel on startup and removes any misclassified Shorts, since the `UULF` feed's Shorts filtering isn't perfect. Bookmarked videos and videos with a saved resume point are always kept.
- Icons are inline SVG and the Orbitron font is vendored at `app/static/fonts/`. The page's only cross-origin requests are to YouTube: thumbnail images, the embed player, and the IFrame Player API script (`youtube.com/iframe_api`, loaded for resume support).

## Tailnet routing

`run.bash` sets `BASE_PATH=/youtube-dashboard` when launching the app. `app/routes.py` passes it into every template render as `base_path`, and `app/templates/index.html` prefixes all its hardcoded links, form actions, the favicon/manifest/font references, and its `fetch()`/`location.href` calls with it (via a `BASE_PATH` JS constant). The `/manifest.webmanifest` route also builds its `start_url`/`scope`/icon paths from `BASE_PATH`. This is needed because the browser resolves absolute paths like `/static/...` or `/api/...` against the domain root, not wherever the page itself was mounted — without the prefix those requests would miss this app entirely and hit whatever else is mounted at `/`. Running the app directly (`python3 run.py`, no `BASE_PATH` set) is unaffected and serves normally at the root.

The tailnet-wide mount is configured once on the host via:

```bash
tailscale serve --bg --set-path=/youtube-dashboard 8000
```

`tailscale serve` strips the `/youtube-dashboard` prefix before forwarding to this app, so the app's own routes and API don't need to know about the prefix — only the browser-facing URLs generated in the template do, which is what `BASE_PATH` handles.
