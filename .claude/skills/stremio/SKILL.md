---
name: stremio
description: Remote control for the Stremio desktop app via stremioctl.py. Use when the user wants to find a movie, series or episode, open it in Stremio, play a video/magnet/torrent in Stremio's player, or check what Stremio is doing.
---

# Stremio remote

`stremioctl.py` (repo root, stdlib Python, JSON output) drives the local Stremio app. Run `python stremioctl.py <cmd> -h` for flags.

## Steps

1. **Resolve the title.** For a plain "play X", skip this step: `watch` and `streams` also take the title itself (`watch "breaking bad" --season 5 --episode 1`) and resolve it, returning the candidates when several titles match. Otherwise, `search "<query>" --type movie|series`. Pick by name and year; "latest"/"newest" means the highest `year`. When two or more results fit equally, show the user a short numbered list and let them choose. For a series episode, `info <id> --type series --season N` lists episodes; map "the next one" or an episode title to a season and episode number. Done when you hold one IMDb id (plus season and episode for a series).

2. **Deliver it** — pick the branch by what the user asked for:
   - **Watch a title** → `watch <id> --type movie|series [--season S --episode E]`. It picks a stream by the user's playback preferences (4K, about 20 GB, no TrueHD) and plays it in Stremio's player. If the user asks for a different stream ("another one", "a smaller one"), run `streams` with the same arguments and replay with `watch --rank N`, never choosing a stream with `"truehd": true`. Done when the command returns `playing`.
   - **Browse a title** (the user wants to see it or choose a stream themselves) → `open` with the same arguments. Done when the command returns `opened`.
   - **A source** (magnet link, 40-hex info hash, or http(s) video URL) → `play "<source>" [--title T]`. For a multi-file torrent, run `files "<source>"` first and pass `--file-idx N` or `--match S01E03`; without either, the largest video file plays. Done when the command returns `playing`.

3. **Report** in one or two lines what is open or playing, including the stream's `source.label` when there is one.

## Addons

`watch` and `streams` read stream addons from `~/.stremioctl.json`, which lives outside the repo because addon URLs often carry private tokens. When they error with "No stream addons configured", ask the user for an addon's manifest URL (in Stremio: Addons → the addon → Configure/Share → copy the Manifest URL) and run `addons add "<url>"`. `addons` lists names only.

## Limits

- Stremio 4 accepts outside links only for pages (detail, search, discover) and for starting playback. Pause, seek, volume and stop are done in the Stremio window, so tell the user that when they ask for them.
- `play` routes torrents through Stremio's own streaming server (`127.0.0.1:11470`) and a small redirect shim on `127.0.0.1:11480`, which it starts automatically. Stremio sends any opened URL that contains an info hash to a detail page, so the shim serves hash-free `.mp4` URLs.
- On any error, run `status`: `serverVersion: null` means the Stremio app isn't running, so ask the user to start it.
