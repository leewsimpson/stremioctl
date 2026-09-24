---
name: trailer
description: Find and play a movie or series trailer from YouTube via stremioctl.py. Use when the user asks to watch, see, find or play a trailer (or "preview") for a title.
---

# Trailer

`stremioctl.py trailer` (repo root, stdlib Python, JSON output) finds a title's YouTube trailer and opens it in the default browser, where it autoplays, or in Stremio's player.

## Steps

1. **Play it.** `python stremioctl.py trailer "<title and year if known>" [--type series]`. It takes a title search or an IMDb id and uses the first Cinemeta match, so include the year when the title is ambiguous ("superman 2025" vs "superman 1978"). If you're unsure which title the user means, resolve the IMDb id first with the stremio skill's `search` and pass the id. Add `--stremio` when the user wants it in Stremio ("on the TV", "in Stremio"): it downloads the trailer at up to 1080p with yt-dlp (about 10 seconds) and plays the file in Stremio. Done when the command returns `player` as `browser` or `stremio`.

2. **Report** in one line: the `trailer` video title and `url`.

## Follow-ups

- **Wrong video or "another trailer"** → rerun with `--rank N` (1 up to `alternatives`).
- **Just the link** → add `--no-open`.
- **Stremio, but instantly** → `--fast` streams at 360p with no download.
- **yt-dlp errors (403, extraction failed)** → the fix is almost always `pip install -U yt-dlp`, because YouTube changes often. Retry after updating. Stremio's own YouTube playback is broken for the same reason, which is why this goes through yt-dlp.
- Downloads are cached in `%LOCALAPPDATA%\stremioctl\trailers` and removed after a week.
- Trailers come from Cinemeta when it has them (`source: cinemeta`); otherwise from a YouTube search for "<name> <year> official trailer" (`source: youtube search`). Search results can be fan edits or reactions, so check the returned `trailer` title looks official and try `--rank` if not.
