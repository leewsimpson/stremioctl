# Control Stremio from your phone

This guide sets up voice and chat control of Stremio. You talk to ChatGPT or Claude on your phone, and the PC connected to your TV plays what you asked for.

```text
Phone (ChatGPT or Claude app)
  │  "play Breaking Bad season 5 episode 1"
  ▼
Chat app's servers ── HTTPS + MCP ──► https://tv.example.com/mcp/<secret>
                                          │  Cloudflare Tunnel (outbound from your PC,
                                          │  no port forwarding)
                                          ▼
                                   127.0.0.1:8766  stremio_mcp.py
                                          │
                                          ▼
                                   stremioctl.py ──► Stremio on the TV
```

The chat app does the understanding. `stremio_mcp.py` is a thin MCP server over `stremioctl.py`, so the phone and the command line behave the same.

## What you need

- **The CLI working first.** Follow the [quick start](../README.md#quick-start): an addon is added and `python stremioctl.py watch "<something>"` plays.
- **Python 3.10 or newer**, for the MCP server; the CLI alone needs only 3.9.
- **A domain on Cloudflare** (the free plan is fine), and [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) installed.
- **ChatGPT with plugins** (Work chats, developer mode) and/or **Claude** with custom connectors.

## 1. Install the MCP server

From the repo root:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 2. Configure `.env`

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"    # paste this as MCP_SECRET
```

| Setting | Meaning |
|---|---|
| `MCP_SECRET` | Part of the endpoint URL, and effectively its password. At least 32 random characters. |
| `MCP_PORT` | Local port for the server. Default is `8766`; change it if something else uses that port. |
| `MCP_PUBLIC_HOST` | Your tunnel hostname, e.g. `tv.example.com`. Requests addressed to any other host are rejected. |
| `TUNNEL_NAME` | The Cloudflare tunnel's name. Default is `stremio-tv`. |

`.env` is gitignored. Never commit it.

## 3. Create the Cloudflare tunnel

Run these once:

```powershell
cloudflared tunnel login                  # pick your domain in the browser
cloudflared tunnel create stremio-tv      # prints the tunnel UUID and writes its credentials file
```

If `login` says a `cert.pem` already exists and that certificate is for the same domain, skip the login.

Create `%USERPROFILE%\.cloudflared\stremio-tv.yml` with your UUID, username, hostname and port:

```yaml
tunnel: <UUID>
credentials-file: C:\Users\<you>\.cloudflared\<UUID>.json
ingress:
  - hostname: tv.example.com
    service: http://localhost:8766
  - service: http_status:404
```

Then point the hostname at the tunnel. Pass the config file explicitly:

```powershell
cloudflared --config $HOME\.cloudflared\stremio-tv.yml tunnel route dns <UUID> tv.example.com
```

> **Why `--config`?** If you already run another tunnel, its `~/.cloudflared/config.yml` pins that tunnel, and `cloudflared` uses it over the name you pass. Without `--config`, `route dns` would point your hostname at the *other* tunnel. `start-tv.ps1` always passes `--config` for the same reason.

## 4. Start it

```powershell
.\start-tv.ps1          # starts the MCP server and the tunnel in the background (skips any already running)
.\start-tv.ps1 -Url     # prints the connector URL
```

The URL looks like `https://tv.example.com/mcp/<secret>`. Anyone who has it can play things on your TV, so treat it like a password.

To check it from outside, open `https://tv.example.com/` in a browser. You should get a 404 page from the server, not a Cloudflare error.

## 5. Connect a chat app

### ChatGPT

1. **Settings → Security and login**: turn on **Developer mode**.
2. **ChatGPT Plugins → +**: paste the connector URL and choose **no authentication**. If ChatGPT asks whether the long value in the URL is an access key or a public identifier, answer **access key**.
3. Install the plugin from your personal plugins directory.
4. On the home page, switch from **Chat** to **Work**, start a new chat, type `@`, and pick the plugin.

Add the plugin yourself as above; don't ask ChatGPT to build it for you. A chat that builds it runs in a sandbox, which fails with "the endpoint did not respond to a connection probe".

### Claude

On claude.ai, go to **Settings → Connectors → Add custom connector** and paste the URL. It then works in the Claude apps, including on iOS.

### Try it

- "Play Dune Part Two."
- "Play Breaking Bad season 5 episode 1."
- "Show me the trailer for Severance."
- "What's playing?" or "Is Stremio running?"
- "Play a different stream." The chat app lists the streams first, then plays the one you choose.

## 6. Start it at logon

`start-tv.ps1` must run in your desktop session, not as a Windows service, because it opens the Stremio window. Register it to run at logon:

```powershell
$action  = New-ScheduledTaskAction -Execute powershell.exe `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PWD\start-tv.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName 'Stremio TV' -Action $action -Trigger $trigger
```

If Windows refuses, run it from an administrator PowerShell. To remove it: `Unregister-ScheduledTask 'Stremio TV'`.

## Tools the chat app gets

| Tool | Does |
|---|---|
| `watch(title, season?, episode?, year?, rank?)` | Finds the title and plays the preferred stream, in one call. If the title is ambiguous, it returns the candidates instead. |
| `list_streams(title, ...)` | Lists the streams with resolution, size and TrueHD flag, and which one `watch` would pick. |
| `search(query)`, `info(id, season?)` | Browse titles, seasons and episodes. |
| `open_page(id, ...)` | Shows the title's page in Stremio without playing. |
| `trailer(title, player?)` | Plays a YouTube trailer, in the browser (default) or in Stremio. |
| `status()` | Whether Stremio and its streaming server are running. |

The server also sends instructions telling the model to call `watch` directly rather than searching first, and to keep replies short. Addon management and raw `play` of arbitrary URLs are deliberately not exposed.

**After changing tools**, which means their names, parameters or descriptions: chat apps cache the tool list. In ChatGPT, refresh the plugin, or remove and re-add it, then start a **new** chat. A caller on the stale list shows up in the log as a tool called with the old argument names.

## Security

- The server binds to `127.0.0.1` only. The internet reaches it solely through the tunnel.
- The endpoint path contains `MCP_SECRET`. Any other path returns 404.
- Requests addressed to a host other than `MCP_PUBLIC_HOST` or localhost are rejected, which protects against DNS rebinding.
- Nothing that writes configuration is exposed: no addon management, and no playing arbitrary URLs.
- The server never logs the secret, and it logs addon hosts without their token-bearing paths.

If the URL leaks, put a new `MCP_SECRET` in `.env`, restart the server, and update the URL in your chat apps.

## Logs and speed

`start-tv.ps1` logs to `%LOCALAPPDATA%\stremioctl\`:

- `mcp.log` has one line per HTTP request and one `TOOL` line per tool call:

  ```text
  21:46:02 TOOL watch {"id": "Practical Magic 2", "type": "movie", "rank": 1} ok: Practical Magic 2 (tt32588798), rank 1 [1080p, 6.8 GB, chosen rank] in 1.1s | http: aiostreams.elfhosted.com 1.0s, v3-cinemeta.strem.io 0.1s x2
  21:46:02 POST /mcp/<secret> tools/call watch(title,type,rank) -> 200 in 1.12s from 23.101.217.186 [openai-mcp/1.0.0 (Codex)]
  ```

  Each request line shows the JSON-RPC method, the argument names, the duration and the client. Each `TOOL` line shows the arguments, what was picked, the total time, and the time spent on each outside host.
- `tunnel.log` is `cloudflared`'s output.

**Where the time goes.** Title lookups take about 0.1 s. Stream addons usually take 1–7 s per lookup, so the server caches stream lists for 15 minutes. Everything else, meaning the gaps between requests, is the chat app thinking. In ChatGPT's Work mode that can be 10 s or more per call, which is why `watch` does the whole job in one call.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `start-tv.ps1` says "MCP server already listening", but nothing works | Another program owns that port. On one machine a Windows service held 3000. Check with `Get-NetTCPConnection -LocalPort 8766`, and change `MCP_PORT` in `.env` and in `stremio-tv.yml`. |
| The public URL gives a Cloudflare 502 | The server isn't running, or the tunnel's `service:` port doesn't match `MCP_PORT`. |
| The hostname reaches a different service | `route dns` used another tunnel's `config.yml`. Rerun it with `--config` and `--overwrite-dns`. |
| ChatGPT: "did not respond to a connection probe" | ChatGPT was building the plugin itself, in its sandbox. Add it yourself through **ChatGPT Plugins**. |
| A tool call fails with "Field required" | The chat app has a stale tool list. Refresh the plugin and start a new chat. |
| "Several titles match …" | Working as intended. Say which one, e.g. with the year. |
| "Every stream has TrueHD audio" | Nothing playable matched. Ask for the stream list and choose a rank. |
| Torrent sources fail with "streaming server not reachable" | Start the Stremio app. Its streaming server runs only while the app is open. |
