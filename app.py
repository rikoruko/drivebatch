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

from flask import Flask, render_template, request, redirect, session, jsonify, send_file
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "drivebatch-secret")

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


def make_flow():
    return Flow.from_client_config(
        client_config(),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
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
                "files(id,name,mimeType,size)"
            ),
            pageSize=1000,
            pageToken=page_token,
        ).execute()

        files.extend(
            response.get("files", [])
        )

        page_token = response.get(
            "nextPageToken"
        )

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

    for item in list_children(
        service,
        folder_id
    ):
        name = item.get(
            "name",
            "Untitled"
        )

        mime = item.get(
            "mimeType",
            ""
        )

        current_path = (
            f"{path}/{name}"
            if path
            else name
        )

        if mime == "application/vnd.google-apps.folder":
            results.extend(
                scan_recursive(
                    service,
                    item["id"],
                    current_path,
                    visited,
                )
            )

        elif is_video(item):
            results.append({
                "id": item["id"],
                "name": name,
                "mimeType": mime,
                "size": int(
                    item.get("size") or 0
                ),
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
        flow = make_flow()

        state = session.get(
            "oauth_state"
        )

        if state:
            flow.state = state

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


def download_drive_file(
    service,
    file_id,
    output_path,
):
    request_obj = service.files().get_media(
        fileId=file_id
    )

    with open(
        output_path,
        "wb"
    ) as output:
        downloader = MediaIoBaseDownload(
            output,
            request_obj,
            chunksize=8 * 1024 * 1024,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()


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
            compression=zipfile.ZIP_STORED,
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
                        service,
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


@app.route("/api/video/<file_id>")
def download_video(file_id):
    try:
        credentials = credentials_from_session()

        if not credentials:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        metadata = service.files().get(
            fileId=file_id,
            fields="name,mimeType,size",
        ).execute()

        filename = safe_name(
            metadata.get(
                "name",
                "video"
            )
        )

        mime = metadata.get(
            "mimeType",
            "application/octet-stream"
        )

        request_obj = service.files().get_media(
            fileId=file_id
        )

        memory = io.BytesIO()

        downloader = MediaIoBaseDownload(
            memory,
            request_obj,
            chunksize=8 * 1024 * 1024,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()

        memory.seek(0)

        return send_file(
            memory,
            mimetype=mime,
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.route("/api/preview/<file_id>")
def preview_video(file_id):
    try:
        credentials = credentials_from_session()

        if not credentials:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        metadata = service.files().get(
            fileId=file_id,
            fields="name,mimeType",
        ).execute()

        mime = metadata.get(
            "mimeType",
            "video/mp4"
        )

        request_obj = service.files().get_media(
            fileId=file_id
        )

        memory = io.BytesIO()

        downloader = MediaIoBaseDownload(
            memory,
            request_obj,
            chunksize=8 * 1024 * 1024,
        )

        done = False

        while not done:
            _, done = downloader.next_chunk()

        memory.seek(0)

        return send_file(
            memory,
            mimetype=mime,
            as_attachment=False,
            download_name=safe_name(
                metadata.get(
                    "name",
                    "video"
                )
            ),
            max_age=0,
        )

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
            old_jobs = []

            for job_id, job in DOWNLOAD_JOBS.items():
                created = job.get(
                    "created_at",
                    now
                )

                if now - created > 3600:
                    old_jobs.append(
                        job_id
                    )

            for job_id in old_jobs:
                job = DOWNLOAD_JOBS.pop(
                    job_id,
                    None
                )

                if job:
                    path = job.get(
                        "zip_path"
                    )

                    if path:
                        try:
                            shutil.rmtree(
                                os.path.dirname(path),
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
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False,
    )