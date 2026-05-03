import os
import uuid
import threading
import time
import base64
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "/tmp/fivestar"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIE_FILE = "/tmp/youtube_cookies.txt"

def prepare_cookie_file():
    """
    Render-safe cookie support:
    - Put your base64 cookies text into the Render environment variable YOUTUBE_COOKIES_B64
    - This writes it to /tmp/youtube_cookies.txt at runtime
    - Do NOT commit cookies.txt to GitHub
    """
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not cookies_b64:
        return False

    try:
        cookie_text = base64.b64decode(cookies_b64).decode("utf-8", errors="replace")
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_text)
        return True
    except Exception as e:
        print("Could not decode YOUTUBE_COOKIES_B64:", e)
        return False

COOKIES_READY = prepare_cookie_file()

jobs = {}

def cleanup_job(job_id, delay=300):
    def _clean():
        time.sleep(delay)
        job = jobs.get(job_id, {})
        for f in job.get("files", []):
            try:
                os.remove(f["path"])
            except Exception:
                pass
        jobs.pop(job_id, None)
    threading.Thread(target=_clean, daemon=True).start()

def run_download(job_id, url, fmt, bitrate, video):
    job = jobs[job_id]
    out_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)

    completed_files = []

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                pct = int(downloaded / total * 80)
                job["progress"] = pct
                job["message"] = f"Downloading… {pct}%"
        elif d.get("status") == "finished":
            job["progress"] = 85
            job["message"] = "Post-processing…"

    common_opts = {
        "outtmpl": os.path.join(out_dir, "%(title).180B.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]
            }
        },
    }

    if os.path.exists(COOKIE_FILE):
        common_opts["cookiefile"] = COOKIE_FILE

    if video:
        ydl_opts = {
            **common_opts,
            "format": "best",
        }
        attempts = [
            {"format": "best"},
            {"format": "bestvideo*+bestaudio/best"},
            {},
        ]
    else:
        if fmt not in {"mp3", "wav", "flac"}:
            fmt = "mp3"

        postprocessor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
        }
        if fmt == "mp3":
            postprocessor["preferredquality"] = str(bitrate or 128)

        ydl_opts = {
            **common_opts,
            "format": "bestaudio/best",
            "postprocessors": [postprocessor],
        }
        attempts = [
            {"format": "bestaudio/best"},
            {"format": "best"},
            {},
        ]

    try:
        last_error = None
        info = None

        for override in attempts:
            try:
                opts = dict(ydl_opts)
                if "format" in override:
                    opts["format"] = override["format"]
                else:
                    opts.pop("format", None)

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break

            except Exception as e:
                last_error = e
                msg = str(e)
                if (
                    "Requested format is not available" not in msg
                    and "HTTP Error 403" not in msg
                    and "Sign in to confirm" not in msg
                ):
                    raise

        if info is None:
            raise last_error

        for fname in os.listdir(out_dir):
            fpath = os.path.join(out_dir, fname)
            if os.path.isfile(fpath):
                completed_files.append({
                    "path": fpath,
                    "name": fname,
                    "size": os.path.getsize(fpath),
                })

        job["files"] = completed_files
        job["status"] = "done"
        job["progress"] = 100
        job["message"] = f"Done — {len(completed_files)} file(s) ready"
        cleanup_job(job_id)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        if not os.path.exists(COOKIE_FILE):
            job["message"] = f"Error: {str(e)}. Render has no YOUTUBE_COOKIES_B64 environment variable yet."
        else:
            job["message"] = f"Error: {str(e)}"

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    bitrate = data.get("bitrate", 128)
    video = data.get("video", False)

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "message": "Starting…",
        "files": [],
        "error": None,
    }

    t = threading.Thread(target=run_download, args=(job_id, url, fmt, bitrate, video), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "files": [{"name": f["name"], "size": f["size"]} for f in job["files"]],
        "error": job["error"],
    })

@app.route("/api/file/<job_id>/<filename>")
def serve_file(job_id, filename):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    for f in job["files"]:
        if f["name"] == filename:
            return send_file(f["path"], as_attachment=True, download_name=filename)
    return jsonify({"error": "File not found"}), 404

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": "render-cookies",
        "cookies_found": os.path.exists(COOKIE_FILE),
        "has_env_var": bool(os.environ.get("YOUTUBE_COOKIES_B64", "").strip()),
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
