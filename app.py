import os
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "/tmp/fivestar"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track jobs: { job_id: { status, progress, message, files, error } }
jobs = {}

def cleanup_job(job_id, delay=300):
    """Delete job files after delay seconds."""
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
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                pct = int(downloaded / total * 80)
                job["progress"] = pct
                job["message"] = f"Downloading… {pct}%"
        elif d["status"] == "finished":
            job["progress"] = 85
            job["message"] = "Post-processing…"

    # Build yt-dlp options
    if video:
        # Download best video+audio as mp4
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }
    else:
        # Audio only
        if fmt == "mp3":
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(bitrate),
            }]
            ext = "mp3"
        elif fmt == "wav":
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
            }]
            ext = "wav"
        elif fmt == "flac":
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
            }]
            ext = "flac"
        else:
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }]
            ext = "mp3"

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
            "postprocessors": postprocessors,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Collect output files
        entries = info.get("entries", [info]) if "entries" in info else [info]
        for entry in entries:
            title = entry.get("title", "download")
            # Find the actual file on disk
            for fname in os.listdir(out_dir):
                fpath = os.path.join(out_dir, fname)
                if fpath not in [f["path"] for f in completed_files]:
                    size = os.path.getsize(fpath)
                    completed_files.append({
                        "path": fpath,
                        "name": fname,
                        "size": size,
                    })

        job["files"] = completed_files
        job["status"] = "done"
        job["progress"] = 100
        job["message"] = f"Done — {len(completed_files)} file(s) ready"
        cleanup_job(job_id)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
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
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
