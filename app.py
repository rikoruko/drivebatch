import io
import os
import re
import uuid
import zipfile
import threading
import time
import json
from tempfile import TemporaryDirectory

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    Response,
)

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

VIDEO_MIMES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/mpeg",
    "video/ogg",
    "video/3gpp",
    "video/x-flv",
    "video/x-ms-wmv",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".mkv",
    ".mpeg",
    ".mpg",
    ".ogv",
    ".3gp",
    ".flv",
    ".wmv",
}

DOWNLOAD_JOBS = {}
JOBS_LOCK = threading.Lock()


def create_job():
    job_id = uuid.uuid4().hex

    with JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = {
            "status": "starting",
            "progress": 0,
            "current": 0,
            "total": 0,
            "current_name": "",
            "message": "Starting download...",
            "path": None,
            "temp_dir": None,
            "error": None,
            "cancel_requested": False,
            "created": time.time(),
        }

    return job_id


def update_job(job_id, **values):
    with JOBS_LOCK:
        if job_id in DOWNLOAD_JOBS:
            DOWNLOAD_JOBS[job_id].update(values)


def get_job(job_id):
    with JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)

        if not job:
            return None

        return dict(job)


def cleanup_old_jobs():
    now = time.time()
    old_jobs = []

    with JOBS_LOCK:
        for job_id, job in list(DOWNLOAD_JOBS.items()):
            if now - job.get("created", now) > 3600:
                old_jobs.append(
                    DOWNLOAD_JOBS.pop(job_id)
                )

    for job in old_jobs:
        temp_dir = job.get("temp_dir")

        if temp_dir:
            try:
                temp_dir.cleanup()
            except Exception:
                pass


def is_cancel_requested(job_id):
    with JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)

        if not job:
            return True

        return bool(
            job.get("cancel_requested")
        )


def client_config():
    raw = os.environ.get(
        "GOOGLE_CLIENT_SECRET_JSON"
    )

    if raw:
        return json.loads(raw)

    path = "client_secret.json"

    if os.path.exists(path):
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    raise RuntimeError(
        "Google OAuth credentials are not configured."
    )


def redirect_uri():
    configured = os.environ.get(
        "OAUTH_REDIRECT_URI"
    )

    if configured:
        return configured

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if render_url:
        return (
            render_url.rstrip("/")
            + "/oauth2callback"
        )

    return (
        "http://localhost:5000/"
        "oauth2callback"
    )


def make_flow(state=None):
    flow = Flow.from_client_config(
        client_config(),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
    )

    if state:
        flow.state = state

    return flow


def build_drive_service(credentials_data):
    if not credentials_data:
        return None

    credentials = Credentials(
        token=credentials_data.get("token"),
        refresh_token=credentials_data.get(
            "refresh_token"
        ),
        token_uri=credentials_data.get(
            "token_uri"
        ),
        client_id=credentials_data.get(
            "client_id"
        ),
        client_secret=credentials_data.get(
            "client_secret"
        ),
        scopes=credentials_data.get(
            "scopes"
        ),
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def drive_service():
    return build_drive_service(
        session.get("google_credentials")
    )


def extract_folder_id(value):
    value = (value or "").strip()

    match = re.search(
        r"/folders/([a-zA-Z0-9_-]+)",
        value,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"[?&]id=([a-zA-Z0-9_-]+)",
        value,
    )

    if match:
        return match.group(1)

    if re.fullmatch(
        r"[a-zA-Z0-9_-]{10,}",
        value,
    ):
        return value

    return None


def is_video(file):
    mime = file.get("mimeType", "")
    name = file.get("name", "").lower()

    if mime in VIDEO_MIMES:
        return True

    return any(
        name.endswith(ext)
        for ext in VIDEO_EXTENSIONS
    )


def format_size(size):
    if not size:
        return "Unknown size"

    value = float(int(size))

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"

        if unit == units[-1]:
            return f"{value:.1f} {unit}"

        value /= 1024

    return "Unknown size"


def list_children(service, folder_id):
    results = []
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
                "videoMediaMetadata)"
            ),
            pageSize=1000,
            pageToken=page_token,
            orderBy="name",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        results.extend(
            response.get("files", [])
        )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return results


def scan_folder(
    service,
    folder_id,
    path="",
):
    videos = []

    for item in list_children(
        service,
        folder_id,
    ):
        item_path = (
            f"{path}/{item['name']}"
            .strip("/")
        )

        if (
            item.get("mimeType")
            == "application/vnd.google-apps.folder"
        ):
            videos.extend(
                scan_folder(
                    service,
                    item["id"],
                    item_path,
                )
            )

        elif is_video(item):
            videos.append({
                "id": item["id"],
                "name": item["name"],
                "path": item_path,
                "size": int(
                    item.get("size", 0) or 0
                ),
                "size_display": format_size(
                    item.get("size")
                ),
                "mimeType": item.get(
                    "mimeType",
                    "",
                ),
            })

    return videos


@app.route("/")
def index():
    return render_template(
        "index.html",
        connected=bool(
            session.get(
                "google_credentials"
            )
        ),
    )


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
def oauth2callback():
    state = session.get(
        "oauth_state"
    )

    flow = make_flow(state)

    flow.fetch_token(
        authorization_response=request.url
    )

    credentials = flow.credentials

    session["google_credentials"] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }

    session.pop(
        "oauth_state",
        None,
    )

    return redirect(
        url_for("index")
    )


@app.route("/logout")
def logout():
    session.clear()

    return redirect(
        url_for("index")
    )


@app.route(
    "/api/scan",
    methods=["POST"],
)
def scan():
    service = drive_service()

    if not service:
        return jsonify({
            "error": (
                "Connect Google Drive first."
            )
        }), 401

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    folder_id = extract_folder_id(
        data.get("url", "")
    )

    if not folder_id:
        return jsonify({
            "error": (
                "Please enter a valid "
                "Google Drive folder link."
            )
        }), 400

    try:
        folder = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()

        if (
            folder.get("mimeType")
            != "application/vnd.google-apps.folder"
        ):
            return jsonify({
                "error": (
                    "That link does not point "
                    "to a Google Drive folder."
                )
            }), 400

        videos = scan_folder(
            service,
            folder_id,
        )

        total_size = sum(
            video.get("size", 0)
            for video in videos
        )

        return jsonify({
            "folder": folder.get(
                "name",
                "Google Drive folder",
            ),
            "videos": videos,
            "count": len(videos),
            "total_size": total_size,
            "total_size_display": format_size(
                total_size
            ),
        })

    except Exception as exc:
        return jsonify({
            "error": (
                "Could not scan this folder: "
                + str(exc)
            )
        }), 400


def build_zip_job(
    job_id,
    video_ids,
    credentials_data,
):
    temp_dir = TemporaryDirectory()

    try:
        service = build_drive_service(
            credentials_data
        )

        if not service:
            update_job(
                job_id,
                status="error",
                error=(
                    "Google Drive connection "
                    "expired. Please reconnect."
                ),
                message=(
                    "Drive connection expired."
                ),
            )

            temp_dir.cleanup()
            return

        zip_path = os.path.join(
            temp_dir.name,
            "drivebatch-videos.zip",
        )

        total = len(video_ids)

        update_job(
            job_id,
            status="downloading",
            total=total,
            current=0,
            progress=0,
            message="Starting...",
        )

        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:

            used_names = set()

            for index, file_id in enumerate(
                video_ids
            ):

                if is_cancel_requested(
                    job_id
                ):
                    update_job(
                        job_id,
                        status="cancelled",
                        message=(
                            "Download cancelled."
                        ),
                    )

                    temp_dir.cleanup()
                    return

                metadata = service.files().get(
                    fileId=file_id,
                    fields=(
                        "id,name,mimeType"
                    ),
                    supportsAllDrives=True,
                ).execute()

                filename = metadata.get(
                    "name",
                    f"video-{index + 1}",
                )

                if filename in used_names:
                    base, ext = os.path.splitext(
                        filename
                    )

                    filename = (
                        f"{base}-"
                        f"{index + 1}"
                        f"{ext}"
                    )

                used_names.add(filename)

                local_path = os.path.join(
                    temp_dir.name,
                    f"video-{index}",
                )

                update_job(
                    job_id,
                    status="downloading",
                    current=index,
                    total=total,
                    progress=int(
                        (index / total) * 90
                    ),
                    current_name=filename,
                    message=(
                        f"Downloading "
                        f"{index + 1} "
                        f"of {total}"
                    ),
                )

                request_media = (
                    service.files().get_media(
                        fileId=file_id,
                        supportsAllDrives=True,
                    )
                )

                with open(
                    local_path,
                    "wb",
                ) as output:

                    downloader = (
                        MediaIoBaseDownload(
                            output,
                            request_media,
                            chunksize=4 * 1024 * 1024,
                        )
                    )

                    done = False

                    while not done:

                        if is_cancel_requested(
                            job_id
                        ):
                            update_job(
                                job_id,
                                status="cancelled",
                                message=(
                                    "Download "
                                    "cancelled."
                                ),
                            )

                            temp_dir.cleanup()
                            return

                        status, done = (
                            downloader.next_chunk()
                        )

                        if status:
                            file_progress = int(
                                status.progress()
                                * 100
                            )

                            overall = int(
                                (
                                    index
                                    + status.progress()
                                )
                                / total
                                * 90
                            )

                            update_job(
                                job_id,
                                progress=min(
                                    overall,
                                    90,
                                ),
                                current=index,
                                total=total,
                                current_name=filename,
                                message=(
                                    f"Downloading "
                                    f"{index + 1} "
                                    f"of {total} "
                                    f"({file_progress}%)"
                                ),
                            )

                if is_cancel_requested(
                    job_id
                ):
                    update_job(
                        job_id,
                        status="cancelled",
                        message=(
                            "Download cancelled."
                        ),
                    )

                    temp_dir.cleanup()
                    return

                archive.write(
                    local_path,
                    arcname=filename,
                )

                try:
                    os.remove(local_path)
                except OSError:
                    pass

                update_job(
                    job_id,
                    progress=int(
                        ((index + 1) / total)
                        * 90
                    ),
                    current=index + 1,
                    total=total,
                    current_name=filename,
                    message=(
                        f"Added "
                        f"{index + 1} "
                        f"of {total}"
                    ),
                )

        update_job(
            job_id,
            status="ready",
            progress=100,
            current=total,
            total=total,
            current_name="",
            message="Your ZIP is ready!",
            path=zip_path,
            temp_dir=temp_dir,
        )

    except Exception as exc:

        try:
            temp_dir.cleanup()
        except Exception:
            pass

        update_job(
            job_id,
            status="error",
            error=str(exc),
            message="Download failed.",
        )


@app.route(
    "/api/download/start",
    methods=["POST"],
)
def start_download():
    cleanup_old_jobs()

    credentials_data = session.get(
        "google_credentials"
    )

    if not credentials_data:
        return jsonify({
            "error": (
                "Connect Google Drive first."
            )
        }), 401

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    video_ids = data.get(
        "ids",
        [],
    )

    if not isinstance(
        video_ids,
        list,
    ):
        return jsonify({
            "error": (
                "Invalid video selection."
            )
        }), 400

    if not video_ids:
        return jsonify({
            "error": (
                "No videos selected."
            )
        }), 400

    if len(video_ids) > 100:
        return jsonify({
            "error": (
                "Please select no more "
                "than 100 videos at once."
            )
        }), 400

    job_id = create_job()

    thread = threading.Thread(
        target=build_zip_job,
        args=(
            job_id,
            video_ids,
            credentials_data,
        ),
        daemon=True,
    )

    thread.start()

    return jsonify({
        "job_id": job_id
    })


@app.route(
    "/api/download/status/<job_id>"
)
def download_status(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": (
                "Download job not found."
            )
        }), 404

    return jsonify({
        "status": job.get(
            "status"
        ),
        "progress": job.get(
            "progress",
            0,
        ),
        "current": job.get(
            "current",
            0,
        ),
        "total": job.get(
            "total",
            0,
        ),
        "current_name": job.get(
            "current_name",
            "",
        ),
        "message": job.get(
            "message",
            "",
        ),
        "error": job.get(
            "error"
        ),
        "ready": (
            job.get("status")
            == "ready"
        ),
    })


@app.route(
    "/api/download/file/<job_id>"
)
def download_file(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "error": (
                "Download job not found."
            )
        }), 404

    if job.get("status") != "ready":
        return jsonify({
    