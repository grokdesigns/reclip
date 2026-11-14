import os
import re
import sys
import time
import uuid
import glob
import hmac
import json
import socket
import ipaddress
import subprocess
import threading
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_file, render_template, Response

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Optional password (HTTP Basic auth, any username). Strongly recommended
# whenever ReClip is reachable from anything other than localhost.
PASSWORD = os.environ.get("RECLIP_PASSWORD", "")
# Extra hostnames the UI may be served under (comma-separated). IP literals,
# localhost and *.local are always allowed; anything else is rejected to
# block DNS-rebinding attacks from websites you visit.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("RECLIP_ALLOWED_HOSTS", "").split(",") if h.strip()}
# Let yt-dlp fetch from private/loopback addresses (off by default to prevent SSRF)
ALLOW_PRIVATE_URLS = os.environ.get("RECLIP_ALLOW_PRIVATE_URLS") == "1"
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("RECLIP_MAX_CONCURRENT", 3))
MAX_PENDING_JOBS = int(os.environ.get("RECLIP_MAX_PENDING", 20))
MAX_PLAYLIST_ITEMS = int(os.environ.get("RECLIP_MAX_PLAYLIST", 100))
# Seconds to keep finished files before deleting them (0 keeps them forever)
FILE_TTL = int(os.environ.get("RECLIP_FILE_TTL", 3600))

FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

jobs = {}
jobs_lock = threading.Lock()
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

if not PASSWORD:
    print("WARNING: RECLIP_PASSWORD is not set — anyone who can reach this port can use ReClip.",
          file=sys.stderr)


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def ytdlp_error(stderr):
    """Return yt-dlp's own ERROR line (useful to the UI) and nothing else."""
    for line in reversed(stderr.strip().split("\n")):
        if line.startswith("ERROR:"):
            return line[:300]
    return "yt-dlp failed"


def check_url(url):
    """Return an error message if ``url`` isn't a public http(s) URL."""
    if len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        return "Invalid URL"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "Invalid URL"
    if parts.scheme not in ("http", "https") or not host:
        return "Only http(s) URLs are supported"
    if ALLOW_PRIVATE_URLS:
        return None
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return "Could not resolve host"
    for addr in addrs:
        if not ipaddress.ip_address(addr.split("%")[0]).is_global:
            return "URLs pointing to private or local addresses are not allowed"
    return None


def host_allowed(host_header):
    host = host_header.lower()
    if host.startswith("["):
        host = host[1:host.find("]")]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if host in ALLOWED_HOSTS or host == "localhost" or host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@app.before_request
def guard():
    if not host_allowed(request.host):
        return jsonify({"error": "Host not allowed; add it to RECLIP_ALLOWED_HOSTS"}), 403
    if PASSWORD:
        auth = request.authorization
        supplied = (auth.password or "") if auth else ""
        if not hmac.compare_digest(supplied.encode(), PASSWORD.encode()):
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="ReClip", charset="UTF-8"'})


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def request_url():
    """Pull and validate ``url`` from the JSON body: returns (url, error)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, "Expected a JSON body"
    url = str(data.get("url") or "").strip()
    if not url:
        return None, "No URL provided"
    return url, check_url(url)


def cleanup_expired():
    """Delete finished jobs and their files once they're older than FILE_TTL."""
    if FILE_TTL <= 0:
        return
    now = time.time()
    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if job["status"] in ("done", "error") and now - job.get("finished_at", now) > FILE_TTL:
                del jobs[job_id]
        active = {job_id for job_id, job in jobs.items() if job["status"] in ("queued", "downloading")}
        kept = {job["file"] for job in jobs.values() if job.get("file")}
    # Also catches leftovers from previous runs, whose jobs are no longer in memory
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        if path in kept or os.path.basename(path).split(".")[0] in active:
            continue
        try:
            if now - os.path.getmtime(path) > FILE_TTL:
                os.remove(path)
        except OSError:
            pass


# Codecs iOS Photos/Messages accept inside an MP4. YouTube's higher resolutions
# are VP9/AV1 with Opus audio, which play in VLC but can't be saved to Photos.
APPLE_VCODECS = {"h264", "hevc"}
APPLE_ACODECS = {"aac", "alac", "mp3"}


def is_apple_vcodec(vcodec):
    return (vcodec or "").startswith(("avc1", "avc3", "hvc1", "hev1"))


def probe_codecs(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
         "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    streams = json.loads(result.stdout or "{}").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return video, audio


def make_apple_compatible(path):
    """Re-encode only the streams iOS can't import, then rewrite in place."""
    video, audio = probe_codecs(path)
    vcodec = video.get("codec_name")
    acodec = audio.get("codec_name")
    video_ok = not video or (vcodec in APPLE_VCODECS and video.get("pix_fmt") in (None, "yuv420p", "yuvj420p"))
    audio_ok = not audio or acodec in APPLE_ACODECS
    # HEVC must carry the hvc1 tag or iOS refuses it, so remux even when codecs are fine
    if video_ok and audio_ok and vcodec != "hevc":
        return

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0:v:0?", "-map", "0:a:0?"]
    if video_ok:
        cmd += ["-c:v", "copy"] + (["-tag:v", "hvc1"] if vcodec == "hevc" else [])
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if audio_ok else ["-c:a", "aac", "-b:a", "192k"]
    tmp = path + ".apple.mp4"
    cmd += ["-movflags", "+faststart", tmp]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip().split("\n")[-1] or "ffmpeg conversion failed")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    try:
        with download_slots:
            job["status"] = "downloading"
            _download(job, job_id, url, format_choice, format_id)
    finally:
        job["finished_at"] = time.time()


def _download(job, job_id, url, format_choice, format_id):
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/best",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "--merge-output-format", "mp4"]

    # "--" stops yt-dlp from parsing the URL as an option (argument injection)
    cmd += ["--", url]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = ytdlp_error(result.stderr)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        if chosen.endswith(".mp4"):
            make_apple_compatible(chosen)

        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|' and c.isprintable()).strip()[:100].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
        job["status"] = "done"
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out"
    except Exception:
        app.logger.exception("Download %s failed", job_id)
        job["status"] = "error"
        job["error"] = "Download failed"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    url, error = request_url()
    if error:
        return jsonify({"error": error}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", "--", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": ytdlp_error(result.stderr)}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options — keep best format per resolution, preferring
        # H.264/HEVC so the result saves to iOS Photos without re-encoding
        def rank(f):
            return (is_apple_vcodec(f.get("vcodec")), f.get("tbr") or 0)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none" and FORMAT_ID_RE.match(str(f.get("format_id", ""))):
                if height not in best_by_height or rank(f) > rank(best_by_height[height]):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{int(height)}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        thumbnail = info.get("thumbnail") or ""
        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": thumbnail if thumbnail.startswith(("http://", "https://")) else "",
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception:
        app.logger.exception("Info lookup failed")
        return jsonify({"error": "Could not read video info"}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    url, error = request_url()
    if error:
        return jsonify({"error": error}), 400

    cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", str(MAX_PLAYLIST_ITEMS), "-J", "--", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": ytdlp_error(result.stderr)}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries") or []
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls[:MAX_PLAYLIST_ITEMS]})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception:
        app.logger.exception("Playlist lookup failed")
        return jsonify({"error": "Could not read playlist info"}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    url, error = request_url()
    if error:
        return jsonify({"error": error}), 400

    data = request.get_json(silent=True)
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = str(data.get("title") or "")[:300]

    if format_choice not in ("video", "audio"):
        return jsonify({"error": "Invalid format"}), 400
    if format_id is not None and not FORMAT_ID_RE.match(str(format_id)):
        return jsonify({"error": "Invalid format_id"}), 400

    cleanup_expired()

    job_id = uuid.uuid4().hex
    with jobs_lock:
        pending = sum(1 for job in jobs.values() if job["status"] in ("queued", "downloading"))
        if pending >= MAX_PENDING_JOBS:
            return jsonify({"error": "Too many downloads in progress — try again shortly"}), 429
        jobs[job_id] = {"status": "queued", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not os.path.exists(job["file"]):
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
