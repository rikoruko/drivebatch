import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import static_ffmpeg
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from werkzeug.middleware.proxy_fix import ProxyFix

# Ensure static_ffmpeg is ready before importing ffmpeg-python
static_ffmpeg.add_paths()
import ffmpeg


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "drivebatch-dev-secret-key-change-in-prod")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
MAX_JOB_AGE_SECONDS = 3600 * 2


@app.after_request
def add_security_headers(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    response.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    return response


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    cleanup_old_jobs()
    with JOBS_LOCK:
        return JOBS.get(job_id)


def set_job(job_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def cleanup_old_jobs() -> None:
    now = time.time()
    to_delete = []

    with JOBS_LOCK:
        for j_id, job in JOBS.items():
            if now - job.get("created_at", now) > MAX_JOB_AGE_SECONDS:
                to_delete.append(j_id)

        for j_id in to_delete:
            job = JOBS.pop(j_id, None)
            if job and job.get("temp_dir") and os.path.exists(job["temp_dir"]):
                try:
                    shutil.rmtree(job["temp_dir"], ignore_errors=True)
                except Exception:
                    pass


def extract_folder_id(url_or_id: str) -> str:
    if not url_or_id:
        return ""

    url_or_id = url_or_id.strip()

    patterns = [
        r"folders/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]+)$",
    ]

    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)

    return url_or_id


def build_drive_service(credentials_data: Optional[Dict] = None):
    if credentials_data:
        creds = Credentials.from_authorized_user_info(credentials_data, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return build("drive", "v3", developerKey=api_key)

    return build("drive", "v3")


def fetch_files_recursive(service, folder_id: str, mime_prefix: str, parent_path: str = "") -> List[Dict[str, Any]]:
    collected = []
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None

    while True:
        results = service.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            fields="nextPageToken, files(id, name, mimeType, size, thumbnailLink, webViewLink)"
        ).execute()

        files = results.get("files", [])
        for f in files:
            m_type = f.get("mimeType", "")
            if m_type == "application/vnd.google-apps.folder":
                sub_path = f"{parent_path}/{f['name']}" if parent_path else f['name']
                collected.extend(fetch_files_recursive(service, f["id"], mime_prefix, sub_path))
            elif mime_prefix == "*" or m_type.startswith(mime_prefix):
                f["path"] = parent_path or "Google Drive"
                collected.append(f)

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return collected


def download_drive_file(service, file_id: str, destination_path: str) -> bool:
    try:
        drive_request = service.files().get_media(file_id=file_id)
        with open(destination_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, drive_request, chunksize=1024 * 1024 * 5)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception:
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        params = {"key": api_key} if api_key else {}
        response = requests.get(url, params=params, stream=True, timeout=60)
        if response.status_code in (200, 206):
            with open(destination_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024 * 5):
                    if chunk:
                        f.write(chunk)
            return True
        return False


def get_preset_for_quality(quality: str) -> Tuple[str, str, str, str]:
    q = (quality or "720p").lower()

    if q in ["360p"]:
        return "640x360", "30", "600k", "64k"
    elif q in ["480p", "low"]:
        return "854x480", "28", "1000k", "96k"
    elif q in ["1080p", "high"]:
        return "1920x1080", "20", "4000k", "192k"
    else:
        return "1280x720", "23", "2000k", "128k"


def compress_video_ffmpeg(input_path: str, output_path: str, quality: str = "720p") -> bool:
    scale, crf, video_bitrate, audio_bitrate = get_preset_for_quality(quality)

    try:
        (
            ffmpeg.input(input_path)
            .output(
                output_path,
                vf=f"scale={scale}:force_original_aspect_ratio=decrease,pad={scale}:(ow-iw)/2:(oh-ih)/2",
                vcodec="libx264",
                crf=crf,
                preset="fast",
                acodec="aac",
                audio_bitrate=audio_bitrate,
                movflags="+faststart",
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        return True
    except ffmpeg.Error:
        return False


def zip_worker(job_id: str, file_ids: List[str], credentials: Optional[Dict]):
    temp_dir = tempfile.mkdtemp(prefix=f"drivebatch_zip_{job_id}_")
    set_job(job_id, temp_dir=temp_dir, status="processing", message="Initializing Google Drive connection...")

    try:
        service = build_drive_service(credentials)
        downloaded_files = []
        total = len(file_ids)

        for index, file_id in enumerate(file_ids):
            job = get_job(job_id)
            if job and job.get("cancelled"):
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

            set_job(job_id, message=f"Downloading file {index + 1} of {total}...", completed=index, progress=int((index / total) * 90))

            try:
                file_meta = service.files().get(file_id=file_id, fields="name").execute()
                original_name = file_meta.get("name", f"file_{file_id}")
            except Exception:
                original_name = f"file_{file_id}"

            safe_name = re.sub(r'[\\/*?:"<>|]', "_", original_name)
            out_path = os.path.join(temp_dir, safe_name)

            if download_drive_file(service, file_id, out_path):
                downloaded_files.append((out_path, safe_name))

        if not downloaded_files:
            set_job(job_id, status="failed", error="Failed to download selected files.", message="ZIP creation failed.")
            return

        set_job(job_id, message="Creating ZIP archive...", progress=95)
        zip_filename = os.path.join(temp_dir, "DriveBatch.zip")

        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for filepath, arcname in downloaded_files:
                zipf.write(filepath, arcname=arcname)

        set_job(job_id, status="done", completed=total, progress=100, message="ZIP ready!", zip_path=zip_filename)

    except Exception as exc:
        set_job(job_id, status="failed", error=str(exc), message="An error occurred.")


def compress_worker(job_id: str, file_ids: List[str], credentials: Optional[Dict], quality: str):
    temp_dir = tempfile.mkdtemp(prefix=f"drivebatch_comp_{job_id}_")
    set_job(job_id, temp_dir=temp_dir, status="processing", message="Initializing Google Drive connection...")

    try:
        service = build_drive_service(credentials)
        processed_files = []
        total = len(file_ids)

        for index, file_id in enumerate(file_ids):
            job = get_job(job_id)
            if job and job.get("cancelled"):
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

            set_job(job_id, message=f"Compressing file {index + 1} of {total}...", completed=index, progress=int((index / total) * 100))

            try:
                file_meta = service.files().get(file_id=file_id, fields="name").execute()
                original_name = file_meta.get("name", f"video_{file_id}.mp4")
            except Exception:
                original_name = f"video_{file_id}.mp4"

            safe_basename = os.path.splitext(original_name)[0]
            safe_basename = re.sub(r'[\\/*?:"<>|]', "_", safe_basename)

            raw_input_path = os.path.join(temp_dir, f"raw_{file_id}.tmp")
            out_output_path = os.path.join(temp_dir, f"{safe_basename}_{quality}.mp4")

            if download_drive_file(service, file_id, raw_input_path):
                if compress_video_ffmpeg(raw_input_path, out_output_path, quality):
                    processed_files.append(out_output_path)

            if os.path.exists(raw_input_path):
                try:
                    os.remove(raw_input_path)
                except Exception:
                    pass

        if not processed_files:
            set_job(job_id, status="failed", error="Failed to download or compress selected files.", message="Compression failed.")
            return

        if len(processed_files) == 1:
            set_job(job_id, status="done", completed=total, progress=100, message="Compression complete!", file_path=processed_files[0])
        else:
            zip_filename = os.path.join(temp_dir, f"DriveBatch_Compressed_{quality}.zip")
            with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in processed_files:
                    zipf.write(file_path, arcname=os.path.basename(file_path))

            set_job(job_id, status="done", completed=total, progress=100, message="Compression complete!", zip_path=zip_filename)

    except Exception as exc:
        set_job(job_id, status="failed", error=str(exc), message="An error occurred.")


# -------------------------------------------------------------------
# ROUTES
# -------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/api/auth/status")
def auth_status():
    return jsonify({"connected": False, "public_only": True})


@app.route("/api/scan", methods=["POST"])
def scan_folder():
    data = request.get_json() or {}
    url = data.get("url") or data.get("folder_url") or data.get("folder_id")
    media_type = data.get("media_type", "video")

    if not url:
        return jsonify({"error": "Google Drive folder link is required."}), 400

    folder_id = extract_folder_id(url)
    if not folder_id:
        return jsonify({"error": "Invalid Google Drive link."}), 400

    try:
        service = build_drive_service()

        mime_prefix = "video/"
        if media_type == "image":
            mime_prefix = "image/"
        elif media_type == "audio":
            mime_prefix = "audio/"

        items = fetch_files_recursive(service, folder_id, mime_prefix)

        return jsonify({
            "success": True,
            "folder_id": folder_id,
            "videos": items if media_type == "video" else [],
            "images": items if media_type == "image" else [],
            "audio": items if media_type == "audio" else [],
            "count": len(items),
            "media_type": media_type
        })

    except HttpError as err:
        return jsonify({"error": f"Google Drive API Error: {err._get_reason()}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/preview/<file_id>")
@app.route("/api/video/<file_id>")
def stream_single_file(file_id):
    try:
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        service = build_drive_service()
        
        # Try fetching original file name for disposition header
        filename = f"media_{file_id}"
        try:
            file_meta = service.files().get(file_id=file_id, fields="name").execute()
            if file_meta.get("name"):
                filename = re.sub(r'[\\/*?:"<>|]', "_", file_meta["name"])
        except Exception:
            pass

        media_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        req_headers = {}

        range_header = request.headers.get("Range")
        if range_header:
            req_headers["Range"] = range_header

        params = {"key": api_key} if api_key else {}
        drive_res = requests.get(media_url, params=params, headers=req_headers, stream=True, timeout=60)

        if drive_res.status_code not in (200, 206):
            return jsonify({"error": f"Failed to stream file (HTTP {drive_res.status_code})"}), drive_res.status_code

        response_headers = {}
        for header in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
            if header in drive_res.headers:
                response_headers[header] = drive_res.headers[header]

        disposition = "attachment" if request.path.startswith("/api/video/") else "inline"
        response_headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'

        def generate():
            for chunk in drive_res.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), status=drive_res.status_code, headers=response_headers)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ZIP BATCH ENDPOINTS
@app.route("/api/download/start", methods=["POST"])
def start_zip_download():
    data = request.get_json() or {}
    file_ids = [str(fid) for fid in data.get("file_ids", []) if str(fid).strip()]

    if not file_ids:
        return jsonify({"error": "No files selected."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "total": len(file_ids),
            "completed": 0,
            "progress": 0,
            "message": "Starting ZIP creation...",
            "created_at": time.time(),
        }

    threading.Thread(target=zip_worker, args=(job_id, file_ids, None), daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/download/status/<job_id>")
def zip_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@app.route("/api/download/file/<job_id>")
def zip_file(job_id):
    job = get_job(job_id)
    if not job or job.get("status") != "done" or not job.get("zip_path"):
        return jsonify({"error": "File not ready."}), 404

    return send_file(job["zip_path"], mimetype="application/zip", as_attachment=True, download_name="DriveBatch.zip")


@app.route("/api/download/cancel/<job_id>", methods=["POST"])
def zip_cancel(job_id):
    set_job(job_id, cancelled=True, status="cancelled")
    return jsonify({"success": True})


# STREAM SAVER / COMPRESSION ENDPOINTS
@app.route("/api/variants/start", methods=["POST"])
@app.route("/api/compress/start", methods=["POST"])
def start_compress():
    data = request.get_json() or {}
    file_ids = [str(fid) for fid in data.get("file_ids", []) if str(fid).strip()]
    quality = data.get("quality", "720p")

    if not file_ids:
        return jsonify({"error": "No videos selected."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "quality": quality,
            "total": len(file_ids),
            "completed": 0,
            "progress": 0,
            "message": "Starting compression...",
            "created_at": time.time(),
        }

    threading.Thread(target=compress_worker, args=(job_id, file_ids, None, quality), daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/variants/status/<job_id>")
@app.route("/api/compress/status/<job_id>")
def compress_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job)


@app.route("/api/variants/file/<job_id>")
@app.route("/api/variants/download/<job_id>")
@app.route("/api/compress/file/<job_id>")
def compress_file(job_id):
    job = get_job(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "File not ready."}), 404

    path = job.get("zip_path") or job.get("file_path")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File missing."}), 404

    mimetype = "application/zip" if job.get("zip_path") else "video/mp4"
    name = os.path.basename(path)

    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=name)


@app.route("/api/variants/cancel/<job_id>", methods=["POST"])
@app.route("/api/compress/cancel/<job_id>", methods=["POST"])
def compress_cancel(job_id):
    set_job(job_id, cancelled=True, status="cancelled")
    return jsonify({"success": True})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
