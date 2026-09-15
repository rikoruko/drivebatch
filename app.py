import os
import io
import re
import json
import uuid
import time
import shutil
import zipfile
import tempfile
import threading
import subprocess
import static_ffmpeg


static_ffmpeg.add_paths()

from flask import (
    Flask, render_template, request, redirect, session, jsonify,
    send_file, Response, stream_with_context
)
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "drivebatch-secret")

def download_drive_file(service, file_id, destination_path):
    request = service.files().get_media(fileId=file_id)

    with open(destination_path, "wb") as f:
        downloader = MediaIoBaseDownload(
            f, request, chunksize=1024 * 1024 * 5
        )  # 5MB chunks
        done = False
        while not done:
            status, done = downloader.next_chunk()

    return destination_path
            
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

VIDEO_MIMES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/webm",
    "video/mpeg",
    "video/ogg",
    "video/3gpp",
    "video/x-flv",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mpeg", ".mpg", ".m4v", ".3gp", ".flv", ".wmv"
}

DOWNLOAD_JOBS = {}
DOWNLOAD_LOCK = threading.Lock()

COMPRESS_JOBS = {}
COMPRESS_LOCK = threading.Lock()


def client_config():
    raw = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")

    if not raw:
        raise RuntimeError(
            "GOOGLE_CLIENT_SECRET_JSON is not configured."
        )

    return json.loads(raw)


def redirect_uri():
    value = os.environ.get("OAUTH_REDIRECT_URI")

    if not value:
        raise RuntimeError(
            "OAUTH_REDIRECT_URI is not configured."
        )

    return value


def make_flow(state=None):
    return Flow.from_client_config(
        client_config(),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
        state=state,
    )


def credentials_from_session():
    data = session.get("google_token")

    if not data:
        return None

    try:
        return Credentials(
            token=data["token"],
            refresh_token=data.get("refresh_token"),
            token_uri=data.get(
                "token_uri",
                "https://oauth2.googleapis.com/token"
            ),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", SCOPES),
        )
    except Exception:
        return None


def credentials_copy():
    data = session.get("google_token")

    if not data:
        return None

    return Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get(
            "token_uri",
            "https://oauth2.googleapis.com/token"
        ),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", SCOPES),
    )


def drive_service():
    credentials = credentials_from_session()

    if not credentials:
        return None

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def extract_folder_id(url):
    patterns = [
        r"/folders/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)

        if match:
            return match.group(1)

    return None


def is_video(file):
    mime = str(
        file.get("mimeType", "")
    ).lower()

    if mime in VIDEO_MIMES:
        return True

    name = str(
        file.get("name", "")
    ).lower()

    extension = os.path.splitext(name)[1]

    return extension in VIDEO_EXTENSIONS


def safe_name(name):
    name = str(name or "video")
    name = name.replace("\x00", "")
    name = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name
    )

    return name.strip() or "video"


def list_children(service, folder_id):
    files = []
    page_token = None

    while True:
        response = service.files().list(
            q=(
                f"'{folder_id}' in parents "
                "and trashed = false"
            ),
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,size,"
                "shortcutDetails)"
            ),
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        files.extend(response.get("files", []))

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return files


def scan_recursive(
    service,
    folder_id,
    path="",
    visited=None,
):
    if visited is None:
        visited = set()

    if folder_id in visited:
        return []

    visited.add(folder_id)

    results = []

    for item in list_children(service, folder_id):

        name = item.get("name", "Untitled")
        mime = item.get("mimeType", "")
        item_id = item.get("id")

        shortcut = item.get("shortcutDetails")

        if shortcut:
            target_id = shortcut.get("targetId")
            target_mime = shortcut.get("targetMimeType")

            if target_id:
                item_id = target_id
                mime = target_mime or mime

        current_path = (
            f"{path}/{name}"
            if path
            else name
        )

        if mime == "application/vnd.google-apps.folder":

            results.extend(
                scan_recursive(
                    service,
                    item_id,
                    current_path,
                    visited,
                )
            )

        elif is_video(item):

            results.append({
                "id": item_id,
                "name": name,
                "mimeType": mime,
                "size": int(item.get("size") or 0),
                "path": current_path,
            })

    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login():
    flow = make_flow()

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    )

    session["oauth_state"] = state

    return redirect(
        authorization_url
    )


@app.route("/oauth2callback")
def oauth_callback():
    try:
        state = session.get("oauth_state")

        flow = make_flow(state=state)

        flow.fetch_token(
            authorization_response=request.url
        )

        credentials = flow.credentials

        session["google_token"] = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
        }

        session.pop(
            "oauth_state",
            None
        )

        return redirect("/")

    except Exception as exc:
        return (
            "Google connection failed: "
            + str(exc),
            500
        )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/api/auth/status")
def auth_status():
    return jsonify({
        "connected": (
            credentials_from_session()
            is not None
        )
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        service = drive_service()

        if not service:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        data = request.get_json(
            silent=True
        ) or {}

        url = str(
            data.get("url", "")
        ).strip()

        if not url:
            return jsonify({
                "error": "Please provide a Google Drive folder link."
            }), 400

        folder_id = extract_folder_id(url)

        if not folder_id:
            return jsonify({
                "error": "Could not find the Drive folder ID."
            }), 400

        videos = scan_recursive(
            service,
            folder_id,
        )

        return jsonify({
            "success": True,
            "videos": videos,
            "count": len(videos),
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


def set_job(job_id, **values):
    with DOWNLOAD_LOCK:
        if job_id in DOWNLOAD_JOBS:
            DOWNLOAD_JOBS[job_id].update(values)


def get_job(job_id):
    with DOWNLOAD_LOCK:
        return DOWNLOAD_JOBS.get(job_id)


def set_compress_job(job_id, **values):
    with COMPRESS_LOCK:
        if job_id in COMPRESS_JOBS:
            COMPRESS_JOBS[job_id].update(values)


def get_compress_job(job_id):
    with COMPRESS_LOCK:
        return COMPRESS_JOBS.get(job_id)


def download_drive_file(
    credentials,
    file_id,
    output_path,
):
    authed_session = AuthorizedSession(credentials)
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    
    with authed_session.get(url, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code} while downloading file {file_id}"
            )

        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)


def compress_video(
    input_path,
    output_path,
    quality,
    progress_callback=None,
):
    quality_map = {
        "360p": 360,
        "720p": 720,
        "1080p": 1080
    }
    height = quality_map.get(quality, 720)
    
    ffmpeg_path = static_ffmpeg.get_ffmpeg()
    if isinstance(ffmpeg_path, tuple):
        ffmpeg_path = ffmpeg_path[0]

    cmd = [
        ffmpeg_path,
        "-nostdin",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        "-i", input_path,
        "-vf", f"scale='min({height},iw)':-2",
        "-c:v", "libx264",
        "-crf", "26",
        "-preset", "fast",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-y",
        output_path
    ]
    
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        for line in process.stdout or ():
            if progress_callback and line.startswith("out_time_ms="):
                try:
                    progress_callback(int(line.split("=", 1)[1]) / 1000000)
                except (TypeError, ValueError):
                    pass

        return process.wait() == 0
    except Exception:
        if "process" in locals() and process.poll() is None:
            process.kill()
            process.wait()
        return False


def zip_worker(
    job_id,
    file_ids,
    credentials,
):
    temp_dir = tempfile.mkdtemp(
        prefix="drivebatch_"
    )

    zip_path = os.path.join(
        temp_dir,
        "DriveBatch.zip"
    )

    set_job(job_id, temp_dir=temp_dir)

    try:
        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        total = len(file_ids)

        set_job(
            job_id,
            status="running",
            total=total,
            completed=0,
            progress=0,
            message="Starting ZIP...",
        )

        used_names = set()

        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:

            for index, file_id in enumerate(
                file_ids,
                start=1
            ):
                job = get_job(job_id)

                if not job:
                    return

                if job.get("cancelled"):
                    set_job(
                        job_id,
                        status="cancelled",
                        message="Download cancelled.",
                    )
                    return

                try:
                    metadata = service.files().get(
                        fileId=file_id,
                        fields="id,name,mimeType,size",
                    ).execute()

                    filename = safe_name(
                        metadata.get(
                            "name",
                            f"video_{index}"
                        )
                    )

                    original_filename = filename
                    counter = 2

                    while filename in used_names:
                        base, ext = os.path.splitext(
                            original_filename
                        )
                        filename = (
                            f"{base} ({counter}){ext}"
                        )
                        counter += 1

                    used_names.add(filename)

                    local_path = os.path.join(
                        temp_dir,
                        f"file_{index}"
                    )

                    set_job(
                        job_id,
                        message=(
                            f"Downloading {filename}..."
                        ),
                    )

                    download_drive_file(
                        credentials,
                        file_id,
                        local_path,
                    )

                    archive.write(
                        local_path,
                        arcname=filename,
                    )

                    try:
                        os.remove(local_path)
                    except Exception:
                        pass

                    progress = int(
                        index / total * 100
                    )

                    set_job(
                        job_id,
                        completed=index,
                        progress=progress,
                        message=(
                            f"{index} / {total} videos"
                        ),
                    )

                except Exception as exc:
                    set_job(
                        job_id,
                        status="error",
                        error=(
                            f"Could not download "
                            f"video {index}: {exc}"
                        ),
                    )
                    return

        set_job(
            job_id,
            status="done",
            progress=100,
            completed=total,
            message="ZIP ready!",
            zip_path=zip_path,
        )

    except Exception as exc:
        set_job(
            job_id,
            status="error",
            error=str(exc),
        )


def compress_worker(
    job_id,
    file_ids,
    credentials,
    quality,
):
    temp_dir = tempfile.mkdtemp(
        prefix="drivebatch_compress_"
    )

    set_compress_job(job_id, temp_dir=temp_dir)

    try:
        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        total = len(file_ids)

        set_compress_job(
            job_id,
            status="running",
            total=total,
            completed=0,
            progress=0,
            message="Starting compression...",
        )

        output_files = []

        for index, file_id in enumerate(
            file_ids,
            start=1
        ):
            job = get_compress_job(job_id)

            if not job:
                return

            if job.get("cancelled"):
                set_compress_job(
                    job_id,
                    status="cancelled",
                    message="Compression cancelled.",
                )
                return

            try:
                metadata = service.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType,size",
                ).execute()

                filename = safe_name(
                    metadata.get(
                        "name",
                        f"video_{index}"
                    )
                )

                if not filename.endswith('.mp4'):
                    filename += '.mp4'

                input_path = os.path.join(
                    temp_dir,
                    f"input_{index}_{filename}"
                )

                output_path = os.path.join(
                    temp_dir,
                    f"output_{index}_{filename}"
                )

                set_compress_job(
                    job_id,
                    message=f"Downloading {filename}...",
                )

                download_drive_file(
                    credentials,
                    file_id,
                    input_path,
                )

                set_compress_job(
                    job_id,
                    message=f"Compressing {filename}...",
                )

                success = compress_video(
                    input_path,
                    output_path,
                    quality,
                    progress_callback=lambda seconds: set_compress_job(
                        job_id,
                        progress=min(
                            99,
                            max(1, int((index - 1) / total * 100)),
                        ),
                        message=(
                            f"Compressing {filename} "
                            f"({int(seconds)}s encoded)..."
                        ),
                    ),
                )

                if not success:
                    raise RuntimeError(
                        f"Failed to compress {filename}"
                    )

                output_files.append(output_path)

                try:
                    os.remove(input_path)
                except Exception:
                    pass

                progress = int(
                    index / total * 100
                )

                set_compress_job(
                    job_id,
                    completed=index,
                    progress=progress,
                    message=f"{index} / {total} videos",
                )

            except Exception as exc:
                set_compress_job(
                    job_id,
                    status="error",
                    error=f"Could not compress video {index}: {exc}",
                )
                return

        is_batch = len(file_ids) > 1

        if is_batch:
            zip_path = os.path.join(
                temp_dir,
                "StreamSaver.zip"
            )

            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:

                for output_file in output_files:
                    archive.write(
                        output_file,
                        arcname=os.path.basename(output_file),
                    )

            set_compress_job(
                job_id,
                status="done",
                progress=100,
                completed=total,
                message="Compression ready!",
                zip_path=zip_path,
                temp_dir=temp_dir,
            )
        else:
            set_compress_job(
                job_id,
                status="done",
                progress=100,
                completed=total,
                message="Compression ready!",
                file_path=output_files[0] if output_files else None,
                temp_dir=temp_dir,
            )

    except Exception as exc:
        set_compress_job(
            job_id,
            status="error",
            error=str(exc),
        )


@app.route("/api/download/start", methods=["POST"])
def start_download():
    try:
        credentials = credentials_copy()

        if not credentials:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        data = request.get_json(
            silent=True
        ) or {}

        file_ids = data.get(
            "file_ids",
            data.get("ids", [])
        )

        if not isinstance(file_ids, list):
            return jsonify({
                "error": "file_ids must be a list."
            }), 400

        file_ids = [
            str(file_id)
            for file_id in file_ids
            if str(file_id).strip()
        ]

        if not file_ids:
            return jsonify({
                "error": "No videos were selected."
            }), 400

        if len(file_ids) > 500:
            return jsonify({
                "error": "You can download up to 500 videos at once."
            }), 400

        job_id = uuid.uuid4().hex

        with DOWNLOAD_LOCK:
            DOWNLOAD_JOBS[job_id] = {
                "status": "queued",
                "total": len(file_ids),
                "completed": 0,
                "progress": 0,
                "message": "Starting ZIP...",
                "error": None,
                "cancelled": False,
                "zip_path": None,
                "temp_dir": None,
                "created_at": time.time(),
            }

        worker = threading.Thread(
            target=zip_worker,
            args=(
                job_id,
                file_ids,
                credentials,
            ),
            daemon=True,
        )

        worker.start()

        return jsonify({
            "success": True,
            "job_id": job_id,
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.route(
    "/api/download/status/<job_id>"
)
def download_status(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": "Download job not found."
        }), 404

    response = {
        "status": job.get("status"),
        "total": job.get("total", 0),
        "completed": job.get("completed", 0),
        "progress": job.get("progress", 0),
        "message": job.get("message"),
    }

    if job.get("error"):
        response["error"] = job["error"]

    if job.get("status") == "done":
        response["ready"] = True

    return jsonify(response)


@app.route(
    "/api/download/file/<job_id>"
)
def download_zip(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": "Download job not found."
        }), 404

    if job.get("status") != "done":
        return jsonify({
            "error": "ZIP is not ready yet."
        }), 409

    path = job.get("zip_path")

    if not path or not os.path.exists(path):
        return jsonify({
            "error": "ZIP file is no longer available."
        }), 404

    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="DriveBatch.zip",
        max_age=0,
    )


@app.route(
    "/api/download/cancel/<job_id>",
    methods=["POST"]
)
def cancel_download(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": "Download job not found."
        }), 404

    set_job(
        job_id,
        cancelled=True,
        status="cancelled",
        message="Download cancelled.",
    )

    return jsonify({
        "success": True
    })


@app.route("/api/compress/start", methods=["POST"])
def start_compress():
    try:
        credentials = credentials_copy()

        if not credentials:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        data = request.get_json(
            silent=True
        ) or {}

        file_ids = data.get(
            "file_ids",
            data.get("ids", [])
        )

        quality = data.get("quality", "720p")

        if quality not in ["360p", "720p", "1080p"]:
            return jsonify({
                "error": "Invalid quality. Choose 360p, 720p, or 1080p."
            }), 400

        if not isinstance(file_ids, list):
            return jsonify({
                "error": "file_ids must be a list."
            }), 400

        file_ids = [
            str(file_id)
            for file_id in file_ids
            if str(file_id).strip()
        ]

        if not file_ids:
            return jsonify({
                "error": "No videos were selected."
            }), 400

        if len(file_ids) > 500:
            return jsonify({
                "error": "You can compress up to 500 videos at once."
            }), 400

        job_id = uuid.uuid4().hex

        with COMPRESS_LOCK:
            COMPRESS_JOBS[job_id] = {
                "status": "queued",
                "total": len(file_ids),
                "completed": 0,
                "progress": 0,
                "message": "Starting compression...",
                "error": None,
                "cancelled": False,
                "file_path": None,
                "zip_path": None,
                "temp_dir": None,
                "created_at": time.time(),
            }

        worker = threading.Thread(
            target=compress_worker,
            args=(
                job_id,
                file_ids,
                credentials,
                quality,
            ),
            daemon=True,
        )

        worker.start()

        return jsonify({
            "success": True,
            "job_id": job_id,
            "is_batch": len(file_ids) > 1,
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.route("/api/compress/status/<job_id>")
def compress_status(job_id):
    job = get_compress_job(job_id)

    if not job:
        return jsonify({
            "error": "Compression job not found."
        }), 404

    response = {
        "status": job.get("status"),
        "total": job.get("total", 0),
        "completed": job.get("completed", 0),
        "progress": job.get("progress", 0),
        "message": job.get("message"),
    }

    if job.get("error"):
        response["error"] = job["error"]

    if job.get("status") == "done":
        response["ready"] = True

    return jsonify(response)


@app.route("/api/compress/file/<job_id>")
def compress_file(job_id):
    job = get_compress_job(job_id)

    if not job:
        return jsonify({
            "error": "Compression job not found."
        }), 404

    if job.get("status") != "done":
        return jsonify({
            "error": "Compression not ready yet."
        }), 409

    zip_path = job.get("zip_path")
    file_path = job.get("file_path")

    if zip_path and os.path.exists(zip_path):
        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name="StreamSaver.zip",
            max_age=0,
        )
    elif file_path and os.path.exists(file_path):
        return send_file(
            file_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=os.path.basename(file_path),
            max_age=0,
        )
    else:
        return jsonify({
            "error": "File no longer available."
        }), 404


def stream_drive_file(file_id, as_attachment=False):
    credentials = credentials_from_session()

    if not credentials:
        return jsonify({
            "error": "Please connect Google Drive first."
        }), 401

    authed_session = AuthorizedSession(credentials)

    meta_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=name,mimeType,size"
    meta_res = authed_session.get(meta_url)

    if meta_res.status_code != 200:
        return jsonify({
            "error": "Could not retrieve video details from Google Drive."
        }), meta_res.status_code

    meta_data = meta_res.json()
    filename = safe_name(meta_data.get("name", "video"))

    media_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    req_headers = {}

    range_header = request.headers.get("Range")
    if range_header:
        req_headers["Range"] = range_header

    drive_res = authed_session.get(
        media_url,
        headers=req_headers,
        stream=True
    )

    if drive_res.status_code not in (200, 206):
        return jsonify({
            "error": f"Failed to download video stream (HTTP {drive_res.status_code})."
        }), drive_res.status_code

    headers = {}
    for header in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
        if header in drive_res.headers:
            headers[header] = drive_res.headers[header]

    if "Accept-Ranges" not in headers:
        headers["Accept-Ranges"] = "bytes"

    disposition = "attachment" if as_attachment else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'

    def generate():
        for chunk in drive_res.iter_content(chunk_size=1024 * 1024):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        status=drive_res.status_code,
        headers=headers,
    )


@app.route("/api/video/<file_id>")
def download_video(file_id):
    try:
        return stream_drive_file(file_id, as_attachment=True)
    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.route("/api/preview/<file_id>")
def preview_video(file_id):
    try:
        return stream_drive_file(file_id, as_attachment=False)
    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.errorhandler(404)
def handle_404(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "API endpoint not found."
        }), 404

    return error


@app.errorhandler(500)
def handle_500(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "Server error."
        }), 500

    return error


def cleanup_jobs():
    while True:
        time.sleep(1800)

        now = time.time()

        with DOWNLOAD_LOCK:
            old_download_jobs = []

            for job_id, job in DOWNLOAD_JOBS.items():
                created = job.get(
                    "created_at",
                    now
                )

                if now - created > 3600:
                    old_download_jobs.append(
                        job_id
                    )

            for job_id in old_download_jobs:
                job = DOWNLOAD_JOBS.pop(
                    job_id,
                    None
                )

                if job:
                    temp_dir = job.get("temp_dir") or (
                        os.path.dirname(job.get("zip_path"))
                        if job.get("zip_path")
                        else None
                    )
                    if temp_dir:
                        try:
                            shutil.rmtree(
                                temp_dir,
                                ignore_errors=True
                            )
                        except Exception:
                            pass

        with COMPRESS_LOCK:
            old_compress_jobs = []

            for job_id, job in COMPRESS_JOBS.items():
                created = job.get(
                    "created_at",
                    now
                )

                if now - created > 3600:
                    old_compress_jobs.append(
                        job_id
                    )

            for job_id in old_compress_jobs:
                job = COMPRESS_JOBS.pop(
                    job_id,
                    None
                )

                if job:
                    temp_dir = job.get("temp_dir")
                    if temp_dir:
                        try:
                            shutil.rmtree(
                                temp_dir,
                                ignore_errors=True
                            )
                        except Exception:
                            pass


def add_created_time():
    with DOWNLOAD_LOCK:
        for job in DOWNLOAD_JOBS.values():
            job.setdefault(
                "created_at",
                time.time()
            )


cleanup_thread = threading.Thread(
    target=cleanup_jobs,
    daemon=True,
)

cleanup_thread.start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
    
