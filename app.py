#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EthicaDL backend (Flask + yt-dlp)

API:
  POST /api/download     -> start a download job (returns job_id)
  GET  /api/status/<id>  -> progress/status
  GET  /api/file/<id>    -> download finished file (GET/HEAD)
  GET  /healthz          -> health check

Use only for content you own or have explicit permission to download.
"""

import os
import uuid
import time
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify, send_file, send_from_directory, make_response
from yt_dlp import YoutubeDL

app = Flask(__name__, static_folder=".", static_url_path="")

# Where to save files
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Optional: set env FFMPEG_BIN or FFMPEG_DIR to your ffmpeg bin path if ffmpeg isn't on PATH
FFMPEG_PATH = os.environ.get("FFMPEG_BIN") or os.environ.get("FFMPEG_DIR")

# In-memory job store
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# UA presets (match your UI)
UA_PRESETS = {
    "chrome_win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "firefox_linux": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "safari_mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "chrome_android": "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "safari_ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
}
DEFAULT_UA = UA_PRESETS["chrome_win"]

# Resolution → yt-dlp format
FORMAT_MAP = {
    "1": "bv*[height<=4320]+ba/b[height<=4320]",
    "2": "bv*[height<=2160]+ba/b[height<=2160]",
    "3": "bv*[height<=1440]+ba/b[height<=1440]",
    "4": "bv*[height<=1080]+ba/b[height<=1080]",
    "5": "bv*[height<=720]+ba/b[height<=720]",
    "6": "bv*[height<=480]+ba/b[height<=480]",
    "7": "bv*[height<=360]+ba/b[height<=360]",
    "8": "bv*[height<=240]+ba/b[height<=240]",
    "9": "bv*[height<=144]+ba/b[height<=144]",
    "10": "ba/b",  # audio only
}

def normalize_youtube_url(u: str) -> str:
    """Convert youtu.be and shorts URLs to standard watch?v=... form when possible."""
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = p.path or ""
        if "youtu.be" in host:
            vid = path.strip("/").split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        if "youtube.com" in host:
            # shorts -> watch
            if path.startswith("/shorts/"):
                parts = path.strip("/").split("/")
                vid = parts[1] if len(parts) > 1 else ""
                if vid:
                    return f"https://www.youtube.com/watch?v={vid}"
            # keep only v param for watch URLs
            if path.startswith("/watch"):
                qs = parse_qs(p.query)
                if "v" in qs and qs["v"]:
                    return f"https://www.youtube.com/watch?v={qs['v'][0]}"
    except Exception:
        pass
    return u

def build_outtmpl(filename_choice: str, filename_custom: str) -> str:
    """Build output template path based on Auto/Custom choice."""
    default_tpl = str(DOWNLOAD_DIR / "%(title).200B [%(id)s].%(ext)s")
    if filename_choice != "custom" or not (filename_custom or "").strip():
        return default_tpl

    name = filename_custom.strip()
    # If looks like a yt-dlp template, use as-is
    if "%(" in name and ")s" in name:
        return str(DOWNLOAD_DIR / name)
    # If no extension, allow yt-dlp to choose ext dynamically
    if "." not in Path(name).name:
        return str(DOWNLOAD_DIR / (name + ".%(ext)s"))
    # Else fixed filename (with given extension)
    return str(DOWNLOAD_DIR / name)

def find_final_file(video_id: str) -> Path | None:
    """Try to find the final file by [id] pattern in downloads folder."""
    if not video_id:
        return None
    matches = list(DOWNLOAD_DIR.glob(f"*[{video_id}].*"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)

def resolve_final_file(info: dict, job: dict, selection: str, audio_fmt: str, merge: str) -> Path | None:
    """Robustly resolve the final output file path after yt-dlp finishes."""
    video_id = job.get("video_id") or (info.get("id") if isinstance(info, dict) else None)
    start_ts = job.get("start_ts", 0.0)

    # 1) By [id] pattern
    p = find_final_file(video_id)
    if p and p.exists():
        return p

    candidates: list[Path] = []
    # 2) From download hook filename (may be original before post-process)
    hook_fn = job.get("pp_filename") or job.get("filename") or info.get("_filename")
    if hook_fn:
        base = Path(hook_fn)
        # try itself
        candidates.append(base)
        # if audio-only, postprocessor may change extension
        if selection == "10":
            candidates.append(base.with_suffix(f".{audio_fmt}"))
        else:
            # merging may produce mp4/mkv
            if merge and merge != "auto":
                candidates.append(base.with_suffix(f".{merge}"))
            candidates.append(base.with_suffix(".mp4"))
            candidates.append(base.with_suffix(".mkv"))

    # 3) From title + id
    title = (info.get("title") or "").strip() if isinstance(info, dict) else ""
    if title:
        stem = f"{title} [{video_id}]" if video_id else title
        if selection == "10":
            candidates.append(DOWNLOAD_DIR / f"{stem}.{audio_fmt}")
        else:
            # try common video extensions
            for ext in (merge if merge and merge != "auto" else "", info.get("ext",""), "mp4", "mkv", "webm"):
                ext = ext.strip().lower()
                if ext:
                    candidates.append(DOWNLOAD_DIR / f"{stem}.{ext}")

    # Test candidates
    for c in candidates:
        try:
            if c and Path(c).exists():
                return Path(c)
        except Exception:
            continue

    # 4) Fallback: newest file created/modified after the job started (allow small skew)
    try:
        recent = sorted(DOWNLOAD_DIR.glob("*"), key=lambda q: q.stat().st_mtime, reverse=True)
        for q in recent:
            if q.stat().st_mtime >= (start_ts - 5):
                return q
    except Exception:
        pass

    return None

def run_download(job_id: str, payload: dict):
    # 1) Read payload + normalize URL (helps avoid 403 for youtu.be/shorts)
    url_raw = (payload.get("url") or "").strip()
    url = normalize_youtube_url(url_raw)

    selection = payload.get("resolution", "4")
    merge = payload.get("merge", "mp4")
    audio_fmt = payload.get("audioFmt", "m4a")
    ua_choice = payload.get("uaChoice", "")
    ua_custom = (payload.get("uaCustom") or "").strip()
    filename_choice = payload.get("filenameChoice", "")
    filename_custom = payload.get("filenameCustom", "")
    cookies_from = (payload.get("cookiesFrom") or "").strip().lower()

    fmt = FORMAT_MAP.get(selection, FORMAT_MAP["4"])
    outtmpl = build_outtmpl(filename_choice, filename_custom)

    # 2) Build yt-dlp options
    ydl_opts: dict = {
        "noplaylist": True,
        "outtmpl": outtmpl,
        "progress_hooks": [],
        "postprocessor_hooks": [],   # capture final filenames after post-processing
        # Robustness
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }

    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    # Headers and UA
    ydl_opts["http_headers"] = {}
    if ua_choice == "custom" and ua_custom:
        ydl_opts["http_headers"]["User-Agent"] = ua_custom
    elif ua_choice in UA_PRESETS:
        ydl_opts["http_headers"]["User-Agent"] = UA_PRESETS[ua_choice]
    else:
        ydl_opts["http_headers"]["User-Agent"] = DEFAULT_UA

    # Merge format (not for audio-only)
    if merge and merge != "auto" and selection != "10":
        ydl_opts["merge_output_format"] = merge

    # Audio-only postprocess
    if selection == "10":
        ydl_opts["format"] = fmt
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt,
            "preferredquality": "0",
        }]
    else:
        ydl_opts["format"] = fmt

    # YouTube-specific tweaks to reduce 403
    is_youtube = ("youtu.be" in url) or ("youtube.com" in url)
    if is_youtube:
        ydl_opts["http_headers"].setdefault("Referer", "https://www.youtube.com")
        ydl_opts.setdefault("extractor_args", {})
        ydl_opts["extractor_args"]["youtube"] = {"player_client": ["android", "web"]}
        ydl_opts["concurrent_fragment_downloads"] = 1
        ydl_opts["geo_bypass"] = True
        cookies_env = (os.environ.get("COOKIES_BROWSER") or "").strip().lower()
        pick_cookies = cookies_from or cookies_env
        if pick_cookies in {"chrome", "edge", "firefox", "brave"}:
            ydl_opts["cookiesfrombrowser"] = (pick_cookies,)

    # 3) Hooks
    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            status = d.get("status")
            info = d.get("info_dict") or {}
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = round(downloaded / total * 100, 2) if total else None
            if info.get("id"):
                job["video_id"] = info.get("id")
            # keep track of last known filename (before post-process)
            if d.get("filename"):
                job["filename"] = d["filename"]

            if status == "downloading":
                job.update({
                    "state": "downloading",
                    "progress": percent,
                    "eta": d.get("eta"),
                    "speed": d.get("speed"),
                })
            elif status == "finished":
                job.update({
                    "state": "processing",
                    "progress": 100.0,
                })

    def pp_hook(d):
        # postprocessor finished -> capture final filepath if present
        if d.get("status") == "finished":
            info = d.get("info_dict") or {}
            fp = info.get("filepath") or info.get("_filename") or d.get("filename")
            if fp:
                with jobs_lock:
                    job = jobs.get(job_id)
                    if job is not None:
                        job["pp_filename"] = fp

    ydl_opts["progress_hooks"].append(hook)
    ydl_opts["postprocessor_hooks"].append(pp_hook)

    with jobs_lock:
        jobs[job_id].update({"state": "starting", "progress": 0})

    # 4) Run yt-dlp
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Decide final output file robustly
        with jobs_lock:
            job = jobs.get(job_id, {})
            job["merge_ext"] = (merge if merge and merge != "auto" and selection != "10" else "")
        final_path = resolve_final_file(info if isinstance(info, dict) else {}, job, selection, audio_fmt, merge)

        with jobs_lock:
            jobs[job_id].update({
                "state": "finished",
                "ready": bool(final_path),
                "file": str(final_path) if final_path else None,
                "error": None if final_path else "File not found after download",
            })

    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({
                "state": "error",
                "error": str(e),
            })

# ------------------- Flask routes -------------------

@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.post("/api/download")
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "state": "queued",
            "progress": 0,
            "ready": False,
            "file": None,
            "error": None,
            "video_id": None,
            "filename": None,
            "pp_filename": None,
            "start_ts": time.time(),
            "merge_ext": "",
        }

    t = threading.Thread(target=run_download, args=(job_id, data), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.get("/api/status/<job_id>")
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        return jsonify({
            "id": job["id"],
            "state": job["state"],
            "progress": job.get("progress"),
            "eta": job.get("eta"),
            "speed": job.get("speed"),
            "ready": job.get("ready", False),
            "error": job.get("error"),
        })

@app.route("/api/file/<job_id>", methods=["GET", "HEAD"])
def api_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        path = job.get("file")
        if not job.get("ready") or not path:
            return jsonify({"error": "file not ready"}), 409

    p = Path(path)
    if not p.exists():
        return jsonify({"error": "file missing"}), 410

    if request.method == "HEAD":
        # Minimal headers for HEAD checks
        resp = make_response("", 200)
        resp.headers["Content-Length"] = str(p.stat().st_size)
        resp.headers["Content-Type"] = "application/octet-stream"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    resp = send_file(p, as_attachment=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/healthz")
def healthz():
    return {"ok": True}

# ----------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)