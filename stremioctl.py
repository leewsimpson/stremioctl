#!/usr/bin/env python3
"""Control a local Stremio desktop app: search titles, open them, play sources.

Every command prints JSON to stdout; errors go to stderr with exit code 1.
Stdlib only. Run `python stremioctl.py -h` for usage.
"""

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CINEMETA = "https://v3-cinemeta.strem.io"
SERVER = "http://127.0.0.1:11470"  # Stremio's bundled streaming server
SHIM_PORT = 11480
SHIM = f"http://127.0.0.1:{SHIM_PORT}"
TRAILER_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "stremioctl", "trailers")

VIDEO_EXT = re.compile(r"\.(mkv|mp4|avi|webm|mov|wmv|mpg|m4v|ts)$", re.I)
# Stremio 4's player only takes URLs ending in one of these extensions.
PLAYER_EXT = re.compile(r"\.(mkv|avi|mp4|wmv|vp8|mov|mpg|mp3|flac)$", re.I)
HEX40 = re.compile(r"[0-9a-fA-F]{40}")
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "wss://tracker.webtorrent.dev",
]


class CliError(Exception):
    pass


# ---------------------------------------------------------------- HTTP

def http_json(url, data=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": "stremioctl"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def server_up():
    try:
        return http_json(f"{SERVER}/settings", timeout=3)["values"].get("serverVersion", "unknown")
    except (OSError, ValueError, KeyError):
        return None


def require_server():
    if not server_up():
        raise CliError(f"Stremio streaming server not reachable at {SERVER}. Start the Stremio app.")


# ---------------------------------------------------------------- Cinemeta

def cinemeta_search(query, media_type):
    url = f"{CINEMETA}/catalog/{media_type}/top/search={urllib.parse.quote(query)}.json"
    return http_json(url).get("metas", [])


def cinemeta_meta(media_type, media_id):
    return http_json(f"{CINEMETA}/meta/{media_type}/{urllib.parse.quote(media_id)}.json").get("meta") or {}


def year_of(meta):
    m = re.match(r"\d{4}", str(meta.get("releaseInfo") or meta.get("year") or ""))
    return int(m.group()) if m else None


# ---------------------------------------------------------------- YouTube trailers

YOUTUBE = "https://www.youtube.com"


def youtube_search(query):
    """Video ids from a YouTube search results page, in YouTube's order."""
    req = urllib.request.Request(f"{YOUTUBE}/results?search_query={urllib.parse.quote(query)}",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        page = resp.read().decode("utf-8", "replace")
    return list(dict.fromkeys(re.findall(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"', page)))


def yt_dlp(*args):
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else [sys.executable, "-m", "yt_dlp"]
    proc = subprocess.run(cmd + ["--no-warnings", "--no-playlist", *args], capture_output=True, text=True)
    if proc.returncode:
        err = (proc.stderr.strip().splitlines() or ["yt-dlp failed"])[-1]
        if "No module named yt_dlp" in err:
            raise CliError("yt-dlp is not installed: pip install -U yt-dlp")
        raise CliError(f"{err} (an outdated yt-dlp is the usual cause: pip install -U yt-dlp)")
    return proc.stdout.strip()


def download_trailer(yt_id):
    """Download a trailer at up to 1080p into TRAILER_DIR, reusing earlier downloads for a week."""
    path = os.path.join(TRAILER_DIR, yt_id + ".mp4")
    if not os.path.isfile(path):
        os.makedirs(TRAILER_DIR, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        for f in os.listdir(TRAILER_DIR):
            if os.path.getmtime(os.path.join(TRAILER_DIR, f)) < cutoff:
                os.remove(os.path.join(TRAILER_DIR, f))
        # Above 360p YouTube serves video and audio separately; merging them needs ffmpeg.
        fmt = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]" if shutil.which("ffmpeg") else "18/b[ext=mp4]"
        yt_dlp("-q", "-f", fmt, "--merge-output-format", "mp4",
               "-o", os.path.join(TRAILER_DIR, "%(id)s.%(ext)s"), f"{YOUTUBE}/watch?v={yt_id}")
    return path


def youtube_title(yt_id):
    try:
        return http_json(f"{YOUTUBE}/oembed?url={YOUTUBE}/watch?v={yt_id}&format=json", timeout=10).get("title")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- Stremio app

def stremio_exe():
    if sys.platform == "win32":
        import winreg
        try:
            cmd = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, r"stremio\shell\open\command")
            m = re.match(r'"([^"]+)"|(\S+)', cmd)
            path = m.group(1) or m.group(2)
            if os.path.isfile(path):
                return path
        except OSError:
            pass
        guess = os.path.expandvars(r"%LOCALAPPDATA%\Programs\LNV\Stremio-4\stremio.exe")
        if os.path.isfile(guess):
            return guess
    return shutil.which("stremio")


def send_to_app(target):
    """Hand a stremio:// link, magnet or video URL to the running (or new) Stremio instance."""
    exe = stremio_exe()
    if not exe:
        raise CliError("Stremio desktop app not found.")
    subprocess.Popen([exe, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def app_running():
    if sys.platform != "win32":
        return None
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq stremio.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "stremio.exe" in out.lower()


# ---------------------------------------------------------------- redirect shim
# Stremio 4 routes any opened URL containing a 40-hex info hash to a detail page
# instead of the player, and Stremio's own stream URLs contain the hash. The shim
# serves hash-free .mp4-style URLs that 302 to the real stream.

def shim_up():
    try:
        with socket.create_connection(("127.0.0.1", SHIM_PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_shim():
    if shim_up():
        return
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x01000000  # BREAKAWAY_FROM_JOB
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "shim"], creationflags=flags, **kwargs)
    except OSError:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "shim"],
                         creationflags=flags & ~0x01000000, **kwargs)
    for _ in range(30):
        if shim_up():
            return
        time.sleep(0.2)
    raise CliError("Could not start the redirect shim.")


def run_shim():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            target = None
            m = re.match(r"^/t/([0-9a-fA-F]{20})-([0-9a-fA-F]{20})/(\d+)/", path)
            if m:
                target = f"{SERVER}/{m[1]}{m[2]}/{m[3]}"
            m = re.match(r"^/u/([A-Za-z0-9_-]+=*)/", path)
            if m:
                target = base64.urlsafe_b64decode(m[1]).decode()
            if not target:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), Handler).serve_forever()


def safe_name(title, ext):
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "video") + ext


# ---------------------------------------------------------------- torrents

def parse_torrent_source(source):
    """Return (info_hash, trackers) for a magnet link or bare info hash, else None."""
    if source.startswith("magnet:"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(source).query)
        xt = next((x for x in qs.get("xt", []) if x.lower().startswith("urn:btih:")), None)
        if not xt:
            raise CliError("Magnet link has no btih info hash.")
        ih = xt[9:]
        if len(ih) == 32:  # base32 form
            ih = base64.b32decode(ih.upper()).hex()
        return ih.lower(), qs.get("tr", [])
    if re.fullmatch(r"[0-9a-fA-F]{40}", source):
        return source.lower(), []
    return None


def load_torrent(info_hash, trackers, timeout=60):
    sources = [f"dht:{info_hash}"] + [f"tracker:{t}" for t in (trackers or DEFAULT_TRACKERS)]
    http_json(f"{SERVER}/{info_hash}/create",
              {"torrent": {"infoHash": info_hash}, "peerSearch": {"sources": sources, "min": 40, "max": 150}},
              timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = http_json(f"{SERVER}/{info_hash}/stats.json", timeout=10)
        if stats.get("files"):
            return stats
        time.sleep(1)
    raise CliError("Timed out waiting for torrent metadata (no peers?).")


def pick_file(files, file_idx=None, match=None):
    if file_idx is not None:
        if not 0 <= file_idx < len(files):
            raise CliError(f"--file-idx {file_idx} out of range (0-{len(files) - 1}).")
        return file_idx
    candidates = [(i, f) for i, f in enumerate(files) if VIDEO_EXT.search(f["name"])]
    if match:
        rx = re.compile(match, re.I)
        candidates = [(i, f) for i, f in candidates if rx.search(f["path"])]
    if not candidates:
        raise CliError("No video file matched." if match else "Torrent has no video files.")
    return max(candidates, key=lambda c: c[1]["length"])[0]


def describe_files(files):
    return [{"idx": i, "name": f["name"], "size_mb": round(f["length"] / 1048576, 1),
             "video": bool(VIDEO_EXT.search(f["name"]))} for i, f in enumerate(files)]


# ---------------------------------------------------------------- commands

def cmd_search(args):
    types = ["movie", "series"] if args.type == "any" else [args.type]
    results = []
    for t in types:
        for m in cinemeta_search(args.query, t)[: args.limit]:
            results.append({"id": m.get("id"), "type": t, "name": m.get("name"), "year": year_of(m)})
    return {"results": results}


def cmd_info(args):
    meta = cinemeta_meta(args.type, args.id)
    if not meta:
        raise CliError(f"No {args.type} found with id {args.id}.")
    out = {k: meta.get(k) for k in ("id", "type", "name", "releaseInfo", "runtime", "genres",
                                    "director", "cast", "imdbRating", "description")}
    if args.type == "series":
        eps = [v for v in meta.get("videos", []) if (v.get("season") or 0) > 0]
        if args.season is None:
            seasons = {}
            for v in eps:
                seasons[v["season"]] = seasons.get(v["season"], 0) + 1
            out["seasons"] = [{"season": s, "episodes": n} for s, n in sorted(seasons.items())]
        else:
            out["episodes"] = sorted(
                ({"season": v["season"], "episode": v.get("episode") or v.get("number"),
                  "title": v.get("name") or v.get("title"), "released": (v.get("released") or "")[:10]}
                 for v in eps if v["season"] == args.season),
                key=lambda e: e["episode"] or 0)
    return out


def cmd_open(args):
    if args.type == "series" and args.season is not None and args.episode is not None:
        video_id = f"{args.id}:{args.season}:{args.episode}"
    else:
        video_id = args.id
    link = f"stremio:///detail/{args.type}/{args.id}/{video_id}"
    send_to_app(link)
    return {"opened": link}


def cmd_files(args):
    require_server()
    parsed = parse_torrent_source(args.source)
    if not parsed:
        raise CliError("files takes a magnet link or 40-hex info hash.")
    stats = load_torrent(*parsed)
    return {"infoHash": parsed[0], "name": stats.get("name"), "peers": stats.get("peers"),
            "files": describe_files(stats["files"])}


def play_torrent(info_hash, trackers, file_idx=None, match=None, title=None):
    require_server()
    stats = load_torrent(info_hash, trackers)
    idx = pick_file(stats["files"], file_idx, match)
    fname = stats["files"][idx]["name"]
    ext = os.path.splitext(fname)[1] if PLAYER_EXT.search(fname) else ".mp4"
    ensure_shim()
    url = f"{SHIM}/t/{info_hash[:20]}-{info_hash[20:]}/{idx}/{safe_name(title or os.path.splitext(fname)[0], ext)}"
    send_to_app(url)
    return {"playing": fname, "fileIdx": idx, "infoHash": info_hash, "peers": stats.get("peers"),
            "stream": f"{SERVER}/{info_hash}/{idx}"}


def play_url(url, title=None):
    target = url
    if not PLAYER_EXT.search(urllib.parse.urlparse(url).path) or HEX40.search(url):
        ensure_shim()
        token = base64.urlsafe_b64encode(url.encode()).decode()
        target = f"{SHIM}/u/{token}/{safe_name(title or 'video', '.mp4')}"
    send_to_app(target)
    return {"playing": url}


def cmd_play(args):
    parsed = parse_torrent_source(args.source)
    if parsed:
        return play_torrent(*parsed, args.file_idx, args.match, args.title)
    if re.match(r"https?://", args.source):
        return play_url(args.source, args.title)
    raise CliError("play takes a magnet link, 40-hex info hash, or http(s) video URL.")


# ---------------------------------------------------------------- addons
# Stream addons live in a per-user config outside the repo, because configured
# addon URLs often embed private tokens.

CONFIG = os.path.join(os.path.expanduser("~"), ".stremioctl.json")


def load_addons():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("addons", [])
    except FileNotFoundError:
        return []


def save_addons(addons):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump({"addons": addons}, f, indent=2)


def addon_base(url):
    url = url.strip()
    # Stremio install-page links carry the real URL in ?addon=
    wrapped = urllib.parse.parse_qs(url.split("?", 1)[1]).get("addon") if "?addon=" in url else None
    if wrapped:
        url = wrapped[0]
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    return re.sub(r"/manifest\.json$", "", url.rstrip("/"))


def cmd_addons(args):
    addons = load_addons()
    if args.action != "list" and not args.url:
        raise CliError(f"addons {args.action} needs a URL or name.")
    if args.action == "add":
        base = addon_base(args.url)
        manifest = http_json(f"{base}/manifest.json")
        if "stream" not in json.dumps(manifest.get("resources", [])):
            raise CliError(f"{manifest.get('name', base)} does not provide streams.")
        addons = [a for a in addons if a["url"] != base] + [{"name": manifest.get("name", base), "url": base}]
        save_addons(addons)
    elif args.action == "remove":
        addons = [a for a in addons if a["name"].lower() != args.url.lower() and a["url"] != addon_base(args.url)]
        save_addons(addons)
    # Names only: URLs can carry private tokens.
    return {"config": CONFIG, "addons": [a["name"] for a in addons]}


def norm_title(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def resolve_title(args):
    """Turn args.id into an IMDb id and a concrete args.type, searching Cinemeta when it's a title.

    Prefers an exact name match, then Cinemeta's top result; refuses to guess between several exact
    matches (remakes, a film and a series of the same name) unless a year narrows them down."""
    episode_given = args.season is not None or args.episode is not None
    if re.fullmatch(r"tt\d+", args.id):
        if args.type == "any":
            args.type = "series" if episode_given else "movie"
        return
    query, year = args.id.strip(), getattr(args, "year", None)
    m = re.fullmatch(r"(.+?)\s*\(?((?:19|20)\d\d)\)?", query)
    if m and not year:
        query, year = m[1], int(m[2])
    types = ["series"] if episode_given else ["movie", "series"] if args.type == "any" else [args.type]
    hits = [(t, meta) for t in types for meta in cinemeta_search(query, t)[:10]]
    if year:
        hits = [h for h in hits if year_of(h[1]) == year]
    if not hits:
        raise CliError(f"Nothing found for {args.id!r}.")
    # Exact names, plus "Name: Subtitle" (Cinemeta calls the 2021 Dune "Dune: Part One").
    q = norm_title(query)
    pool = [h for h in hits if norm_title(h[1].get("name")) == q
            or (h[1].get("name") or "").lower().startswith(query.lower() + ":")] or hits[:1]
    if len(pool) > 1:
        # Search results carry no popularity, so rank the candidates by their full metadata.
        with ThreadPoolExecutor(8) as ex:
            pops = list(ex.map(lambda h: cinemeta_meta(h[0], h[1]["id"]).get("popularity") or 0, pool[:8]))
        ranked = sorted(zip(pops, pool[:8]), key=lambda p: -p[0])
        if ranked[0][0] < 5 * ranked[1][0]:  # no clear favourite: let the user choose
            options = "; ".join(f"{m.get('name')} ({year_of(m)}, {t}, {m.get('id')})" for _, (t, m) in ranked[:5])
            raise CliError(f"Several titles match {args.id!r}, most popular first: {options}. "
                           "Pass the year or the IMDb id.")
        pool = [ranked[0][1]]
    args.type, args.id, args.name = pool[0][0], pool[0][1]["id"], pool[0][1].get("name")


def video_id_for(args):
    resolve_title(args)
    if args.type == "series":
        if args.season is None or args.episode is None:
            raise CliError(f"{getattr(args, 'name', None) or args.id} is a series: give a season and an episode.")
        return f"{args.id}:{args.season}:{args.episode}"
    return args.id


def fetch_streams(media_type, video_id):
    """Streams from every configured addon, in config order then each addon's own order."""
    addons = load_addons()
    if not addons:
        raise CliError(f"No stream addons configured. Add one with: addons add <manifest url>")
    results = []
    for addon in addons:
        try:
            data = http_json(f"{addon['url']}/stream/{media_type}/{urllib.parse.quote(video_id, safe='')}.json")
        except (OSError, ValueError):
            continue
        for s in data.get("streams", []):
            if s.get("infoHash") or re.match(r"https?://", s.get("url") or ""):
                results.append((addon["name"], s))
    return results


def stream_label(addon_name, s):
    text = " ".join(filter(None, [s.get("name"), s.get("title") or s.get("description")]))
    return {"addon": addon_name, "label": re.sub(r"\s+", " ", text).strip(),
            "kind": "torrent" if s.get("infoHash") else "url", **stream_facts(s)}


# ---------------------------------------------------------------- stream preferences
# Tried in order; the first tier with a match wins, in addon order. Size relaxes
# before resolution. TrueHD is never allowed: it fails to play.

PREFERENCE_TIERS = [
    ("4K, 15-30 GB", lambda f: f["res"] == "4K" and f["size_gb"] and 15 <= f["size_gb"] <= 30),
    ("4K, under 50 GB", lambda f: f["res"] == "4K" and (f["size_gb"] or 0) < 50),
    ("4K, any size", lambda f: f["res"] == "4K"),
    ("any resolution, 15-30 GB", lambda f: f["size_gb"] and 15 <= f["size_gb"] <= 30),
    ("any resolution, under 50 GB", lambda f: (f["size_gb"] or 0) < 50),
    ("any resolution, any size", lambda f: True),
]


def stream_facts(s):
    """Resolution, size and TrueHD flag, from behaviorHints when present, else the label text."""
    hints = s.get("behaviorHints") or {}
    text = " ".join(filter(None, [hints.get("filename"), s.get("name"), s.get("title"), s.get("description")]))
    res = next((r for r, rx in [("4K", r"\b(4k|2160p|uhd)\b"), ("1080p", r"\b1080p\b"), ("720p", r"\b720p\b")]
                if re.search(rx, text, re.I)), None)
    size = hints.get("videoSize")
    if size:
        size_gb = round(size / 1e9, 1)
    else:  # labels read "25 GB" or, for a file in a pack, "6.27 GB/74.5 GB"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|TB)\b", text)
        size_gb = round(float(m[1]) * {"MB": 0.001, "GB": 1, "TB": 1000}[m[2]], 1) if m else None
    return {"res": res, "size_gb": size_gb, "truehd": bool(re.search(r"true-?hd", text, re.I))}


def preferred_rank(streams):
    """(rank, tier) of the stream the playback preferences pick, or (None, None)."""
    facts = [stream_facts(s) for _, s in streams]
    for tier, ok in PREFERENCE_TIERS:
        for i, f in enumerate(facts):
            if not f["truehd"] and ok(f):
                return i, tier
    return None, None


def cmd_streams(args):
    video_id = video_id_for(args)  # resolves args.type too, so call it first
    streams = fetch_streams(args.type, video_id)
    rank, tier = preferred_rank(streams)
    return {"id": args.id, "type": args.type, "preferred": rank, "preferredBecause": tier,
            "streams": [dict(rank=i, **stream_label(a, s)) for i, (a, s) in enumerate(streams[: args.limit])]}


def cmd_watch(args):
    video_id = video_id_for(args)
    streams = fetch_streams(args.type, video_id)
    if not streams:
        raise CliError("No playable streams found from configured addons.")
    if args.rank is None:
        rank, tier = preferred_rank(streams)
        if rank is None:
            raise CliError("Every stream has TrueHD audio, which fails to play. Pick one with --rank.")
    else:
        rank, tier = (args.rank if args.rank < len(streams) else 0), "chosen rank"
    addon_name, s = streams[rank]
    meta = cinemeta_meta(args.type, args.id)
    title = meta.get("name") or args.id
    if args.type == "series":
        title += f" S{args.season:02d}E{args.episode:02d}"
    if s.get("infoHash"):
        trackers = [x[len("tracker:"):] for x in s.get("sources", []) if x.startswith("tracker:")]
        result = play_torrent(s["infoHash"].lower(), trackers, s.get("fileIdx"), None, title)
    else:
        result = play_url(s["url"], title)
    return {"title": title, "id": args.id, "rank": rank, "pickedBecause": tier, "source": stream_label(addon_name, s), **result}


def cmd_trailer(args):
    if re.fullmatch(r"tt\d+", args.title):
        meta = cinemeta_meta(args.type, args.title)
    else:
        hits = cinemeta_search(args.title, args.type)
        meta = cinemeta_meta(args.type, hits[0]["id"]) if hits else {}
    if not meta:
        raise CliError(f"No {args.type} found for {args.title!r}.")
    name, year = meta.get("name"), year_of(meta)
    ids = [t["ytId"] for t in meta.get("trailerStreams") or [] if t.get("ytId")]
    source = "cinemeta"
    if not ids:  # Cinemeta has no trailers for some titles, often recent ones
        ids = youtube_search(" ".join(filter(None, [name, str(year or ""), "official trailer"])))[:5]
        source = "youtube search"
    if not ids:
        raise CliError(f"No trailer found for {name}.")
    yt_id = ids[args.rank if args.rank < len(ids) else 0]
    url = f"{YOUTUBE}/watch?v={yt_id}"
    player = "none" if args.no_open else "stremio" if args.stremio or args.fast else "browser"
    if player == "stremio" and args.fast:  # YouTube's only combined video+audio stream is 360p
        play_url(yt_dlp("-f", "18/b[ext=mp4]", "-g", url).splitlines()[0], f"{name} trailer")
    elif player == "stremio":
        send_to_app(download_trailer(yt_id))  # Stremio plays a local file path directly
    elif player == "browser":
        webbrowser.open(url)
    return {"title": name, "year": year, "id": meta.get("id"), "trailer": youtube_title(yt_id), "url": url,
            "source": source, "alternatives": len(ids) - 1, "player": player}


def cmd_status(args):
    stats = {}
    if server_up():
        try:
            stats = http_json(f"{SERVER}/stats.json", timeout=5)
        except (OSError, ValueError):
            pass
    torrents = [{"infoHash": ih, "name": s.get("name"), "peers": s.get("peers"),
                 "downloadMBps": round((s.get("downloadSpeed") or 0) / 1048576, 2)}
                for ih, s in (stats.items() if isinstance(stats, dict) else [])]
    return {"appRunning": app_running(), "appPath": stremio_exe(), "serverVersion": server_up(),
            "shimRunning": shim_up(), "torrents": torrents}


def main():
    p = argparse.ArgumentParser(prog="stremioctl", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search Cinemeta for movies/series")
    s.add_argument("query")
    s.add_argument("--type", choices=["movie", "series", "any"], default="any")
    s.add_argument("--limit", type=int, default=8, help="max results per type")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("info", help="details for a title; series list seasons, or episodes with --season")
    s.add_argument("id", help="IMDb id, e.g. tt5950044")
    s.add_argument("--type", choices=["movie", "series"], default="movie")
    s.add_argument("--season", type=int)
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("open", help="open a title (or episode) page in the Stremio app")
    s.add_argument("id")
    s.add_argument("--type", choices=["movie", "series"], default="movie")
    s.add_argument("--season", type=int)
    s.add_argument("--episode", type=int)
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("files", help="list the files in a torrent (magnet or info hash)")
    s.add_argument("source")
    s.set_defaults(fn=cmd_files)

    s = sub.add_parser("play", help="play a magnet, info hash, or http(s) video URL in Stremio's player")
    s.add_argument("source")
    s.add_argument("--file-idx", type=int, help="torrent file index (see `files`)")
    s.add_argument("--match", help="regex on torrent file path, e.g. S01E03; largest match wins")
    s.add_argument("--title", help="name shown in the player")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("addons", help="list, add or remove stream addons (stored in ~/.stremioctl.json)")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    s.add_argument("url", nargs="?", help="manifest URL to add, or name/URL to remove")
    s.set_defaults(fn=cmd_addons)

    for name, fn, help_text in [
        ("streams", cmd_streams, "list streams for a title from the configured addons"),
        ("watch", cmd_watch, "play the preferred stream (4K, ~20 GB, no TrueHD) for a title"),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("id", help="IMDb id or title, e.g. tt5950044 or \"breaking bad\"")
        s.add_argument("--type", choices=["movie", "series", "any"], default="any",
                       help="for a title search; --season/--episode imply series")
        s.add_argument("--year", type=int, help="release year, to pick between same-name titles")
        s.add_argument("--season", type=int)
        s.add_argument("--episode", type=int)
        if name == "streams":
            s.add_argument("--limit", type=int, default=15)
        else:
            s.add_argument("--rank", type=int, help="play this stream rank instead of the preferred one")
        s.set_defaults(fn=fn)

    s = sub.add_parser("trailer", help="find a title's YouTube trailer and play it in the browser")
    s.add_argument("title", help="IMDb id or title search, e.g. tt5950044 or \"superman 2025\"")
    s.add_argument("--type", choices=["movie", "series"], default="movie")
    s.add_argument("--rank", type=int, default=0, help="play an alternative trailer instead of the first")
    s.add_argument("--stremio", action="store_true",
                   help="play in Stremio's player instead of the browser (downloads up to 1080p with yt-dlp)")
    s.add_argument("--fast", action="store_true", help="play in Stremio at 360p, streamed with no download")
    s.add_argument("--no-open", action="store_true", help="print the trailer URL without opening it")
    s.set_defaults(fn=cmd_trailer)

    s = sub.add_parser("status", help="app, streaming server, shim and active torrents")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("shim", help="(internal) run the redirect shim in the foreground")
    s.set_defaults(fn=None)

    args = p.parse_args()
    if args.cmd == "shim":
        run_shim()
        return
    try:
        print(json.dumps(args.fn(args), indent=2, ensure_ascii=False))
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        print(f"error: network: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
