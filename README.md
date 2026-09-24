# stremioctl

Control the Stremio desktop app from a terminal, from an AI agent, or by voice from your phone.

- **`stremioctl.py`** is a command-line remote for Stremio. It finds titles, picks a good stream, and plays it in Stremio's own player. It uses only the standard library and prints JSON, so scripts and agents can use it too.
- **`stremio_mcp.py`** exposes the same commands as an [MCP](https://modelcontextprotocol.io) server. Add it to ChatGPT or Claude, say "play Severance season 2 episode 3" on your phone, and it plays on the PC connected to your TV. See **[docs/remote-control.md](docs/remote-control.md)**.
- **Claude Code skills** in `.claude/skills/` (`stremio`, `trailer`) let an agent drive the CLI. `.agents/` is a symlink to `.claude/` for tools that look there.

Tested with Stremio 4.4 on Windows.

## Quick start

You need Python 3.9 or newer and the [Stremio desktop app](https://www.stremio.com/downloads), running.

```powershell
git clone https://github.com/leewsimpson/stremioctl.git
cd stremioctl
```

1. **Add a stream addon.** Stremio's catalog tells `stremioctl` what exists; a stream addon supplies the streams. In Stremio, open Addons → your stream addon → Configure (or Share) and copy its manifest URL, then:

   ```powershell
   python stremioctl.py addons add "https://your-addon.example/.../manifest.json"
   ```

   Addons are saved in `~/.stremioctl.json`, outside the repo, because configured addon URLs usually contain private tokens.

2. **Play something:**

   ```powershell
   python stremioctl.py watch "dune part two"
   python stremioctl.py watch "breaking bad" --season 1 --episode 3
   ```

3. **Check what's happening:** `python stremioctl.py status`

## Commands

Every command prints JSON. Run `python stremioctl.py <command> -h` for all flags.

| Command | What it does |
|---|---|
| `search "superman" [--type movie\|series]` | Search Cinemeta, Stremio's catalog. Returns IMDb ids and years. |
| `info <id> [--type series] [--season N]` | Details for a title; for a series, its seasons, or a season's episodes. |
| `watch <title or id> [--season S --episode E] [--year Y] [--rank N]` | Play the preferred stream. |
| `streams <title or id> [...]` | List streams with resolution, size and TrueHD flag, and which one `watch` would pick. |
| `open <id> [--type series --season S --episode E]` | Show the title's page in Stremio, to pick a stream yourself. |
| `play <magnet \| info hash \| url> [--match S01E03 \| --file-idx N]` | Play a source directly in Stremio's player. |
| `files <magnet \| info hash>` | List the files in a torrent. |
| `trailer "superman 2025" [--stremio \| --fast] [--rank N]` | Play a YouTube trailer, in the browser or in Stremio. |
| `addons [list \| add <url> \| remove <name>]` | Manage stream addons. |
| `status` | Whether the app, streaming server and shim are running, and the active torrents. |

### How `watch` chooses

**The title.** `watch` and `streams` take a name or an IMDb id. A name resolves as follows:

1. Exact name matches win. So do `Name: Subtitle` titles, because Cinemeta calls the 2021 film "Dune: Part One".
2. Among several matches, the clearly most popular one wins, so "severance" means the Apple TV series.
3. If no match is clearly ahead, the command lists the candidates instead of guessing. Pass `--year` or the IMDb id to choose.
4. A season or episode means a series.

**The stream.** It takes the first stream, in addon order, that matches the built-in preferences:

- **4K**
- **15–30 GB.** About 20 GB is plenty; 50 GB+ remuxes are skipped.
- **No TrueHD audio**, because it fails to play in Stremio. DD+, DD, AAC and Atmos over DD+ are fine.

If nothing matches, it relaxes size first, then resolution. It never relaxes TrueHD.

These preferences are hard-coded, not settings: they live in `PREFERENCE_TIERS` in `stremioctl.py`, tuned for one setup (a 4K TV and fast disk), so they may not match yours. Until they are configurable — see [Possible improvements](#possible-improvements) — you can override them per play with `watch --rank N`, or edit the tiers in the source.

### Trailers

`trailer` opens a title's YouTube trailer in your default browser, from Cinemeta's trailer list or, failing that, a YouTube search. `--stremio` plays it in Stremio's player instead. Stremio's built-in YouTube playback no longer works, so this uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`pip install yt-dlp`) to download it at up to 1080p into `%LOCALAPPDATA%\stremioctl\trailers`; with ffmpeg installed it can merge separate video and audio. `--fast` streams at 360p with no download.

## How it works

- **Search and metadata** come from Cinemeta (`v3-cinemeta.strem.io`).
- **Pages** open through `stremio:///detail/...` links, which the Stremio app registers.
- **Torrents** stream through Stremio's bundled streaming server on `127.0.0.1:11470`.
- **Playback** starts by handing the app a video URL. Stremio 4 sends any URL that contains an info hash to a detail page instead of the player, so playback goes through a small redirect shim on `127.0.0.1:11480`. The shim serves hash-free `.mp4` URLs that redirect to the real stream. It starts automatically, keeps running in the background, and shows up in `status`.

Pause, seek and stop aren't available from outside the app; use the Stremio window for those.

## Possible improvements

Ideas, not commitments.

- **Configurable playback preferences.** The rules `watch` uses to pick a stream — 4K, 15–30 GB, never TrueHD — are hard-coded in `PREFERENCE_TIERS` in `stremioctl.py`, so they reflect one viewer's tastes. They could become a `preferences` section in `~/.stremioctl.json` (preferred resolution, size range, whether TrueHD is banned), keeping today's values as the defaults, plus flags like `--res` and `--max-size` on `watch`.
- **Playback control.** `status` can see the app, but pause, seek and stop still need the Stremio window. If the app's embedded player can be driven from outside, the CLI and the MCP server could expose it.
- **Cross-platform support.** The code already has non-Windows fallbacks (finding the app with `shutil.which`, trailers under `~/.cache` when `LOCALAPPDATA` is absent), but only Windows is tested. Verifying macOS and Linux would let the "Tested with Stremio 4.4 on Windows" caveat go.
