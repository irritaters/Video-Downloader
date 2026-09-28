"""MKS backend: Flask + yt-dlp.

Run locally:  python server.py   ->  http://localhost:5000
Needs ffmpeg (HD video merge + MP3) and Deno (YouTube support) installed.

How downloads work (two steps, friendly to download managers like IDM):
  1. POST /api/prepare      -> starts a background job, returns {"id": ...}
  2. GET  /api/status/<id>  -> poll until state == "ready"
  3. GET  /api/file/<id>    -> the finished file (supports resume / range requests)
Finished files are kept for a short time and then deleted automatically.
NOTE: jobs live in memory, so run ONE worker process (see README).
"""
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Only these sites are accepted (also stops the server being used to fetch random URLs).
ALLOWED_DOMAINS = (
    "youtube.com", "youtu.be",
    "tiktok.com",
    "facebook.com", "fb.watch",
    "x.com", "twitter.com",
)

MAX_PER_MINUTE = 20          # info/prepare requests per IP per minute
MAX_PARALLEL_DOWNLOADS = 3   # downloads running at the same time
MAX_ACTIVE_JOBS = 30         # waiting + running jobs allowed
FILE_TTL = 20 * 60           # seconds a finished file is kept after its last use
MP3_BITRATES = ("128", "192", "256", "320")

slots = threading.Semaphore(MAX_PARALLEL_DOWNLOADS)
jobs = {}
jobs_lock = threading.Lock()
hits = defaultdict(deque)


# ---------------------------------------------------------------- helpers
def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def rate_limited():
    now = time.time()
    q = hits[client_ip()]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= MAX_PER_MINUTE:
        return True
    q.append(now)
    return False


def clean_url(raw):
    raw = (raw or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        return None
    return raw


def error(message, code=400):
    return jsonify({"error": message}), code


def friendly_error(exc):
    """Turn a technical yt-dlp error into a sentence a normal visitor understands."""
    t = str(exc).lower()
    if "ffmpeg" in t or "ffprobe" in t:
        return "The server is missing ffmpeg, which is needed for MP3 and HD video."
    if "not a bot" in t:
        return "The video site is checking this server for robots right now. Please try again later."
    if "429" in t or "too many requests" in t:
        return "The site is limiting requests right now. Please try again in a few minutes."
    if "requested format" in t:
        return "That quality isn't available for this video. Please choose another one."
    if any(k in t for k in ("private", "sign in", "log in", "login", "cookies", "members-only", "members only")):
        return "This video is private or needs a login. Only public videos can be downloaded."
    if any(k in t for k in ("age-restricted", "age restricted", "confirm your age")):
        return "This video is age-restricted, so it can't be downloaded."
    if "no video could be found" in t or "there is no video" in t:
        return "We couldn't find a video in that post."
    if "unsupported url" in t:
        return "That link doesn't point to a video. Copy the link of the video itself."
    if any(k in t for k in ("not available", "unavailable", "removed", "deleted", "does not exist", "404")):
        return "This video is unavailable. It may have been removed or blocked in this country."
    return "We couldn't read that link. Check that the video is public and the link is complete."


# Quality label = what people expect to see (1080p, 720p...), not raw pixel sizes.
HEIGHT_LADDER = (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144)
WIDTH_LADDER = ((7680, 4320), (3840, 2160), (2560, 1440), (1920, 1080),
                (1280, 720), (854, 480), (640, 360), (426, 240), (256, 144))


def snap_height(v):
    for s in HEIGHT_LADDER:
        if v >= s - 8:
            return s
    return None


def snap_width(w):
    for t, q in WIDTH_LADDER:
        if w >= t - 8:
            return q
    return None


def quality_of(width, height):
    """Vertical videos (Shorts, TikTok) are named by their short side; wide videos by height
    (or by width for cinematic crops such as 1920x800, which is still 1080p)."""
    if not width:
        return snap_height(height)
    if width > height:
        return max(filter(None, (snap_height(height), snap_width(width))), default=None)
    return snap_height(width)


# ---------------------------------------------------------------- pages
@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/info")
def info():
    if rate_limited():
        return error("Too many requests. Please wait a minute and try again.", 429)
    url = clean_url((request.get_json(silent=True) or {}).get("url"))
    if not url:
        return error("That link isn't from a supported site. Please paste a link from a supported video site.")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:
        return error(friendly_error(exc))

    formats = data.get("formats") or []
    duration = data.get("duration") or 0

    def size_of(f):
        # Exact size if the site tells us; otherwise estimate from bitrate x length (TikTok, X, Facebook).
        exact = f.get("filesize") or f.get("filesize_approx")
        if exact:
            return exact
        if f.get("tbr") and duration:
            return int(f["tbr"] * 125 * duration)
        return None

    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), default=None)

    def audio_bytes(f):
        if not f:
            return None
        return size_of(f) or (int((f.get("abr") or 0) * 125 * duration) or None)

    groups = {}
    for f in formats:
        if f.get("vcodec") == "none" or not f.get("height"):
            continue
        q = quality_of(f.get("width"), f["height"])
        if q:
            groups.setdefault(q, []).append(f)

    options = []
    for q in sorted(groups, reverse=True)[:8]:
        top = max(f["height"] for f in groups[q])
        # Pick the file the download would most likely use (H.264 first, then MP4, then biggest bitrate).
        pick = max(
            (f for f in groups[q] if f["height"] == top),
            key=lambda f: (str(f.get("vcodec") or "").startswith("avc1"),
                           str(f.get("protocol") or "").startswith("http"),
                           f.get("ext") == "mp4", f.get("fps") or 0, f.get("tbr") or 0),
        )
        size = size_of(pick)
        if size and pick.get("acodec") == "none":        # video-only stream: add the audio part
            extra = audio_bytes(best_audio)
            size = size + extra if extra else None
        options.append({"quality": q, "height": top, "size": size})

    return jsonify({
        "title": data.get("title") or "Untitled video",
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "uploader": data.get("uploader"),
        "options": options,
        "m4a_size": audio_bytes(best_audio),
    })


# ---------------------------------------------------------------- download jobs
# Some sites offer both a watermarked and a clean copy of the same video.
# This filter makes yt-dlp skip the copy the site itself labels as watermarked.
NO_WM = "[format_note!*=?watermarked][format_id!=?download]"


def skip_watermarked(fmt):
    """Add NO_WM after every video selector (b, bv*) in a yt-dlp format string."""
    return re.sub(r"(?<![a-z])(bv\*|b)(?=[\[/+]|$)", lambda m: m.group(1) + NO_WM, fmt)


def build_opts(kind, height, bitrate, outdir, nowm=False):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(outdir, "%(title).80s.%(ext)s"),
    }
    if kind == "video":
        if height:
            # `height` is the exact pixel height of the quality the visitor chose.
            # Prefer H.264 (plays everywhere) but NEVER at the cost of a lower resolution
            # (YouTube 1440p/4K has no H.264, so those fall through to VP9/AV1).
            eq, le = f"[height={height}]", f"[height<={height}]"
            h264 = "[vcodec~='^(avc|h264)']"
            opts["format"] = (
                f"bv*{eq}{h264}+ba[ext=m4a]/b{eq}{h264}/"
                f"bv*{eq}[ext=mp4]+ba[ext=m4a]/bv*{eq}+ba[ext=m4a]/bv*{eq}+ba/b{eq}/"
                f"bv*{le}+ba/b{le}/b"
            )
        else:
            opts["format"] = "bv*[vcodec~='^(avc|h264)']+ba[ext=m4a]/bv*+ba/b"
        if nowm:
            opts["format"] = skip_watermarked(opts["format"])
        opts["merge_output_format"] = "mp4"
    elif kind == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": bitrate or "192"}]
    else:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    return opts


def run_job(job):
    with slots:
        job["state"] = "working"
        tmp = tempfile.mkdtemp(prefix="mks_")
        job["dir"] = tmp
        try:
            opts = build_opts(job["kind"], job["height"], job["bitrate"], tmp, job["nowm"])
            seen = {}

            def on_progress(d):
                # Overall percentage across all streams downloaded so far (video + audio).
                fn = d.get("filename")
                if d.get("status") == "downloading":
                    seen[fn] = (d.get("downloaded_bytes") or 0,
                                d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                elif d.get("status") == "finished":
                    size = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                    seen[fn] = (size, size)
                total = sum(t for _, t in seen.values())
                if total:
                    pct = int(sum(min(dl, t) for dl, t in seen.values()) * 100 / total)
                    # Never go backwards and never show 100 until the file is really ready.
                    job["progress"] = max(job.get("progress", 0), min(pct, 95))

            opts["progress_hooks"] = [on_progress]
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([job["url"]])
            files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl", ".temp"))]
            if not files:
                raise RuntimeError("no file was produced")
            name = max(files, key=lambda f: os.path.getsize(os.path.join(tmp, f)))
            path = os.path.join(tmp, name)
            # Emoji and symbols in titles can make browsers reject the download, so use a clean name.
            base, ext = os.path.splitext(name)
            base = re.sub(r"[^\w\s.()-]", "", base)
            base = re.sub(r"\s+", " ", base).strip(" ._-")[:80] or "video"
            safe = base + ext
            if safe != name:
                os.replace(path, os.path.join(tmp, safe))
                name, path = safe, os.path.join(tmp, safe)
            job.update(path=path, filename=name, size=os.path.getsize(path),
                       touched=time.time(), state="ready")
        except Exception as exc:
            job.update(state="error", error=friendly_error(exc), touched=time.time())
            shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/prepare")
def prepare():
    if rate_limited():
        return error("Too many requests. Please wait a minute and try again.", 429)
    body = request.get_json(silent=True) or {}
    url = clean_url(body.get("url"))
    kind = body.get("type", "video")
    if not url or kind not in ("video", "mp3", "m4a"):
        return error("Invalid request.")

    height = bitrate = None
    if kind == "video" and body.get("height"):
        try:
            height = max(100, min(int(body["height"]), 4400))
        except (TypeError, ValueError):
            return error("Invalid request.")
    if kind == "mp3":
        bitrate = str(body.get("quality", "192"))
        if bitrate not in MP3_BITRATES:
            bitrate = "192"

    nowm = kind == "video" and bool(body.get("nowm"))
    key = (url, kind, height, bitrate, nowm)
    with jobs_lock:
        # Same file already prepared (or being prepared)? Reuse it instead of downloading twice.
        for j in jobs.values():
            if j["key"] == key and (j["state"] in ("queued", "working")
                                    or (j["state"] == "ready" and os.path.exists(j["path"]))):
                return jsonify({"id": j["id"]})
        if sum(1 for j in jobs.values() if j["state"] in ("queued", "working")) >= MAX_ACTIVE_JOBS:
            return error("The server is busy right now. Please try again in a moment.", 503)
        job = {"id": secrets.token_urlsafe(16), "key": key, "url": url, "kind": kind,
               "height": height, "bitrate": bitrate, "nowm": nowm, "state": "queued", "created": time.time(),
               "touched": time.time()}
        jobs[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"id": job["id"]})


@app.get("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job or (job["state"] == "ready" and not os.path.exists(job["path"])):
        return error("This download expired. Please start again.", 404)
    out = {"state": job["state"], "progress": job.get("progress", 0)}
    if job["state"] == "ready":
        out.update(filename=job["filename"], size=job["size"])
    elif job["state"] == "error":
        out["error"] = job["error"]
    return jsonify(out)


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job["state"] != "ready" or not os.path.exists(job["path"]):
        return error("This download expired. Please start again.", 404)
    job["touched"] = time.time()
    # conditional=True gives resume/range support, so download managers work properly.
    return send_file(job["path"], as_attachment=True, download_name=job["filename"], conditional=True)


def cleaner():
    while True:
        time.sleep(60)
        now = time.time()
        with jobs_lock:
            for jid, j in list(jobs.items()):
                if j["state"] == "ready" and now - j["touched"] > FILE_TTL:
                    shutil.rmtree(j.get("dir", ""), ignore_errors=True)
                    del jobs[jid]
                elif j["state"] == "error" and now - j["touched"] > 600:
                    del jobs[jid]


threading.Thread(target=cleaner, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
