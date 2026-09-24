#!/usr/bin/env python3
"""MCP server that exposes stremioctl to chat apps (ChatGPT, Claude) so they can drive Stremio.

    python stremio_mcp.py            # streamable HTTP on 127.0.0.1:$MCP_PORT/mcp/$MCP_SECRET
    python stremio_mcp.py --stdio    # stdio, for a local Claude Desktop / Claude Code

Settings come from the environment or a .env file next to this script:
    MCP_SECRET       required for HTTP; the endpoint path, so keep it long and random
    MCP_PORT         default 8766
    MCP_PUBLIC_HOST  the tunnel hostname, e.g. tv.example.com, allowed as a Host header

Needs the `mcp` package (pip install -r requirements.txt); stremioctl.py itself stays stdlib only.
"""

import argparse
import os
import json
import sys
import threading
import time
import urllib.parse
from typing import Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import stremioctl as ctl

HERE = os.path.dirname(os.path.abspath(__file__))

INSTRUCTIONS = """\
Remote control for a Stremio app on a PC connected to the user's TV. Use it when the user wants \
to watch something, find a title, see a trailer, or check what's playing.

- To play something, call `watch` straight away with the title as the user said it, plus season and \
episode for a series. Don't search first: `watch` finds the title itself, and each extra call makes \
the user wait. If it reports several matching titles, ask which one and call again with the year or id.
- Use `search` and `info` only when the user is browsing or asks about a title (what seasons, what \
it's about), not before playing.
- `watch` picks a stream by the user's preferences (4K, about 20 GB, never TrueHD audio). Only pass \
`rank` when the user asks for a different stream; use `list_streams` to show them the options.
- Playback takes several seconds to start. Pausing, seeking and stopping happen in the Stremio window, \
not through these tools.
- Keep replies short: the user is usually speaking to you from the couch."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

mcp = MCPServer(name="stremio", title="Stremio TV", instructions=INSTRUCTIONS, log_level="WARNING")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# Per-thread HTTP timings for the tool call running on that thread (sync tools run in worker threads).
_calls = threading.local()
_http_json = ctl.http_json


def timed_http_json(url, *args, **kwargs):
    start = time.perf_counter()
    try:
        return _http_json(url, *args, **kwargs)
    finally:
        # Host only: addon URLs carry private tokens in their paths.
        host = urllib.parse.urlsplit(url).netloc
        stats = getattr(_calls, "http", None)
        if stats is not None:
            n, secs = stats.get(host, (0, 0.0))
            stats[host] = (n + 1, secs + time.perf_counter() - start)


ctl.http_json = timed_http_json

# Stream addons take 5-7 s per lookup with no caching of their own, and a chat often lists
# streams and then plays one, so keep results for a while.
STREAM_TTL = 15 * 60
_stream_cache = {}
_fetch_streams = ctl.fetch_streams


def cached_fetch_streams(media_type, video_id):
    key = (media_type, video_id)
    hit = _stream_cache.get(key)
    if hit and time.monotonic() - hit[0] < STREAM_TTL:
        return hit[1]
    streams = _fetch_streams(media_type, video_id)
    if streams:
        _stream_cache[key] = (time.monotonic(), streams)
    return streams


ctl.fetch_streams = cached_fetch_streams


def run(fn, **kwargs):
    """Call a stremioctl command function, turning its errors into tool errors, and log its timing."""
    name = fn.__name__.removeprefix("cmd_")
    shown = {k: v for k, v in kwargs.items() if v is not None and v is not False}
    _calls.http = {}
    start = time.perf_counter()
    outcome = "ok"
    try:
        result = fn(argparse.Namespace(**kwargs))
        if name == "watch":
            source = result.get("source") or {}
            outcome = (f"ok: {result['title']} ({result.get('id')}), rank {result.get('rank')}"
                       f" [{source.get('res')}, {source.get('size_gb')} GB, {result.get('pickedBecause')}]")
        return result
    except ctl.CliError as e:
        outcome = f"error: {e}"
        raise ToolError(str(e)) from e
    except OSError as e:
        outcome = f"error: network: {e}"
        raise ToolError(f"network: {e}") from e
    finally:
        total = time.perf_counter() - start
        http = ", ".join(f"{host} {secs:.1f}s" + (f" x{n}" if n > 1 else "")
                         for host, (n, secs) in sorted(_calls.http.items(), key=lambda h: -h[1][1]))
        log(f"TOOL {name} {json.dumps(shown, ensure_ascii=False)} {outcome} in {total:.1f}s"
            + (f" | http: {http}" if http else ""))
        _calls.http = None


@mcp.tool(annotations=READ)
def search(query: str, type: Literal["movie", "series", "any"] = "any", limit: int = 5) -> dict:
    """Search for movies and series by name. Returns IMDb ids, names and years."""
    return run(ctl.cmd_search, query=query, type=type, limit=limit)


@mcp.tool(annotations=READ)
def info(id: str, type: Literal["movie", "series"] = "movie", season: int | None = None) -> dict:
    """Details for a title. For a series, lists its seasons, or a season's episodes when `season` is given."""
    return run(ctl.cmd_info, id=id, type=type, season=season)


@mcp.tool(annotations=ACT)
def watch(title: str, season: int | None = None, episode: int | None = None, year: int | None = None,
          type: Literal["movie", "series", "any"] = "any", rank: int | None = None) -> dict:
    """Play a movie or episode on the TV in one step. `title` is a name as the user said it
    ("breaking bad") or an IMDb id; a season/episode means a series. Picks the stream by the user's
    preferences unless `rank` is given. If several titles match, it returns them instead of playing:
    ask the user which one, then call again with the year or id."""
    return run(ctl.cmd_watch, id=title, type=type, year=year, season=season, episode=episode, rank=rank)


@mcp.tool(annotations=READ)
def list_streams(title: str, season: int | None = None, episode: int | None = None, year: int | None = None,
                 type: Literal["movie", "series", "any"] = "any", limit: int = 10) -> dict:
    """List available streams (resolution, size, TrueHD flag) and which one `watch` would pick.
    `title` is a name or IMDb id, as for `watch`."""
    return run(ctl.cmd_streams, id=title, type=type, year=year, season=season, episode=episode, limit=limit)


@mcp.tool(annotations=ACT)
def open_page(id: str, type: Literal["movie", "series"] = "movie", season: int | None = None,
              episode: int | None = None) -> dict:
    """Show a title's (or episode's) page in Stremio on the TV, without playing it."""
    return run(ctl.cmd_open, id=id, type=type, season=season, episode=episode)


@mcp.tool(annotations=ACT)
def trailer(title: str, type: Literal["movie", "series"] = "movie", rank: int = 0,
            player: Literal["browser", "stremio"] = "browser") -> dict:
    """Play a title's YouTube trailer on the TV. `title` is a name or IMDb id; `rank` picks an alternative.
    `stremio` downloads it at 1080p first, so it takes longer to start."""
    return run(ctl.cmd_trailer, title=title, type=type, rank=rank,
               stremio=player == "stremio", fast=False, no_open=False)


@mcp.tool(annotations=READ)
def status() -> dict:
    """Whether Stremio and its streaming server are running, and the active torrents."""
    return run(ctl.cmd_status)


def load_env(path):
    """Minimal .env reader: KEY=value lines; the real environment wins."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except FileNotFoundError:
        pass


def rpc_summary(body):
    """'tools/call watch ' style label for a JSON-RPC request body, or '' if it isn't one."""
    try:
        msgs = json.loads(body)
    except ValueError:
        return ""
    labels = []
    for m in msgs if isinstance(msgs, list) else [msgs]:
        if isinstance(m, dict) and m.get("method"):
            params = m.get("params") or {}
            # Argument names only, so a caller on a stale tool schema shows up (values stay out of this log).
            tool = f"{params.get('name')}({','.join(params.get('arguments') or {})})" \
                if m["method"] == "tools/call" else None
            labels.append(m["method"] + (f" {tool}" if tool else ""))
        elif isinstance(m, dict):
            labels.append("response")
    return " ".join(labels) + " " if labels else ""


def log_requests(app, secret):
    """ASGI wrapper logging each request to stderr, with the secret path masked."""
    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope["headers"])
        path = scope["path"].replace(secret, "<secret>")
        client = headers.get(b"cf-connecting-ip", b"").decode() or (scope.get("client") or ("?",))[0]
        agent = headers.get(b"user-agent", b"").decode()[:60]
        status, body = [], bytearray()

        async def receive_logged():
            message = await receive()
            if message["type"] == "http.request" and len(body) < 65536:
                body.extend(message.get("body", b""))
            return message

        async def send_logged(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        start = time.perf_counter()
        try:
            await app(scope, receive_logged, send_logged)
        finally:
            log(f"{scope['method']} {path} {rpc_summary(body)}-> {status[0] if status else '?'}"
                f" in {time.perf_counter() - start:.2f}s from {client} [{agent}]")
    return wrapped


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    args = p.parse_args()
    if args.stdio:
        mcp.run("stdio")
        return

    load_env(os.path.join(HERE, ".env"))
    secret = os.environ.get("MCP_SECRET", "")
    if len(secret) < 32:
        sys.exit("MCP_SECRET must be set (in .env) to at least 32 random characters, e.g.\n"
                 "  python -c \"import secrets; print(secrets.token_hex(32))\"")
    port = int(os.environ.get("MCP_PORT", "8766"))
    hosts = ["127.0.0.1:*", "localhost:*"]
    if os.environ.get("MCP_PUBLIC_HOST"):
        hosts.append(os.environ["MCP_PUBLIC_HOST"])
    security = TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=["https://chatgpt.com", "https://claude.ai", "http://127.0.0.1:*", "http://localhost:*"])
    app = log_requests(mcp.streamable_http_app(streamable_http_path=f"/mcp/{secret}", transport_security=security),
                       secret)
    print(f"stremio MCP on http://127.0.0.1:{port}/mcp/{secret[:4]}... (hosts: {', '.join(hosts)})", file=sys.stderr)
    # Bound to localhost only: the internet reaches it through the Cloudflare tunnel.
    # No access log, because every request line would contain the secret.
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
