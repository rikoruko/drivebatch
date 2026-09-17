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
from concurrent.futures import ThreadPoolExecutor, as_completed
import static_ffmpeg
import requests


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
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.cloud import storage

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "drivebatch-secret")
app.config["JSON_SORT_KEYS"] = False
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["PREFERRED_URL_SCHEME"] = "https" if os.environ.get("FLASK_ENV") == "production" else "http"

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
            
# Saving generated files to a user-selected Drive folder requires write access.
# Existing read-only sessions must sign in again after this scope changes.
SCOPES = ["https://www.googleapis.com/auth/drive"]
OAUTH_SCOPE_VERSION = 2

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

IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/heic",
    "image/heif",
    "image/tiff",
    "image/svg+xml",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mpeg", ".mpg", ".m4v", ".3gp", ".flv", ".wmv"
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".bmp", ".heic", ".heif", ".tif", ".tiff", ".svg"
}

DOWNLOAD_JOBS = {}
DOWNLOAD_LOCK = threading.Lock()

COMPRESS_JOBS = {}
COMPRESS_LOCK = threading.Lock()
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()


def artifact_bucket():
    if not GCS_BUCKET:
        return None
    return storage.Client().bucket(GCS_BUCKET)


def store_artifact(job_id, path, filename):
    bucket = artifact_bucket()
    if not bucket:
        return None
    object_name = f"jobs/{job_id}/{safe_name(filename)}"
    bucket.blob(object_name).upload_from_filename(path)
    return object_name


def materialize_artifact(job, key, suffix):
    path = job.get(key)
    if path and os.path.exists(path):
        return path, False
    object_name = job.get(f"{key}_object")
    bucket = artifact_bucket()
    if not object_name or not bucket:
        return None, False
    temporary = tempfile.NamedTemporaryFile(
        prefix="drivebatch_artifact_",
        suffix=suffix,
        delete=False,
    )
    temporary.close()
    bucket.blob(object_name).download_to_filename(temporary.name)
    return temporary.name, True


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

    if data.get("scope_version") != OAUTH_SCOPE_VERSION:
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

    if data.get("scope_version") != OAUTH_SCOPE_VERSION:
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


def is_image(file):
    mime = str(
        file.get("mimeType", "")
    ).lower()

    if mime in IMAGE_MIMES:
        return True

    name = str(
        file.get("name", "")
    ).lower()

    extension = os.path.splitext(name)[1]

    return extension in IMAGE_EXTENSIONS


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
    media_type="video",
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
                    media_type,
                )
            )

        elif media_type == "video" and is_video(item):

            results.append({
                "id": item_id,
                "name": name,
                "mimeType": mime,
                "size": int(item.get("size") or 0),
                "path": current_path,
                "type": "video",
            })

        elif media_type == "image" and is_image(item):

            results.append({
                "id": item_id,
                "name": name,
                "mimeType": mime,
                "size": int(item.get("size") or 0),
                "path": current_path,
                "type": "image",
            })

    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


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
            "scope_version": OAUTH_SCOPE_VERSION,
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
        media_type = str(
            data.get("media_type", "video")
        ).strip().lower()

        if media_type not in {"video", "image"}:
            return jsonify({
                "error": "media_type must be either video or image."
            }), 400

        if not url:
            return jsonify({
                "error": "Please provide a Google Drive folder link."
            }), 400

        folder_id = extract_folder_id(url)

        if not folder_id:
            return jsonify({
                "error": "Could not find the Drive folder ID."
            }), 400

        items = scan_recursive(
            service,
            folder_id,
            media_type=media_type,
        )

        return jsonify({
            "success": True,
            "videos": items,
            "images": items if media_type == "image" else [],
            "count": len(items),
            "media_type": media_type,
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
        "480p": 480,
        "720p": 720,
        "1080p": 1080
    }
    height = quality_map.get(quality, 720)
    
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        "-i", input_path,
        "-vf", f"scale='min({height},iw)':-2",
        "-c:v", "libx264",
        "-crf", "26",
        "-preset", "veryfast",
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


def cloudconvert_video(
    input_path,
    output_path,
    quality,
    job_id,
    filename,
    file_index,
    total_files,
):
    api_key = os.environ.get("CLOUDCONVERT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "CLOUDCONVERT_API_KEY is not configured."
        )

    height = {
        "360p": 360,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
    }[quality]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def require_cloudconvert_success(response):
        if response.ok:
            return
        try:
            details = response.json().get("message")
        except ValueError:
            details = None
        if not details:
            details = response.text[:300].strip()
        raise RuntimeError(
            f"CloudConvert API returned {response.status_code}: {details}"
        )

    job_response = requests.post(
        "https://api.cloudconvert.com/v2/jobs",
        headers=headers,
        json={
            "tasks": {
                "upload": {"operation": "import/upload"},
                "convert": {
                    "operation": "convert",
                    "input": "upload",
                    "output_format": "mp4",
                    "video_codec": "x264",
                    "height": height,
                    "preset": "veryfast",
                    "crf": 26,
                },
                "export": {
                    "operation": "export/url",
                    "input": "convert",
                    "inline": False,
                },
            }
        },
        timeout=30,
    )
    require_cloudconvert_success(job_response)
    job = job_response.json()["data"]
    upload_task = next(
        task for task in job["tasks"] if task["name"] == "upload"
    )
    upload_url = upload_task["result"]["form"]["url"]
    upload_parameters = upload_task["result"]["form"]["parameters"]

    with open(input_path, "rb") as source:
        set_compress_job(
            job_id,
            progress=max(
                get_compress_job(job_id).get("progress", 0),
                int((file_index - 1) / total_files * 100),
            ),
            message=(
                f"Video {file_index} / {total_files}: "
                "uploading to CloudConvert..."
            ),
        )
        upload_response = requests.post(
            upload_url,
            data=upload_parameters,
            files={"file": (filename, source, "video/mp4")},
            timeout=600,
        )
    upload_response.raise_for_status()

    job_id_remote = job["id"]
    while True:
        current = requests.get(
            f"https://api.cloudconvert.com/v2/jobs/{job_id_remote}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        require_cloudconvert_success(current)
        data = current.json()["data"]
        tasks = data.get("tasks", [])
        convert_task = next(
            (task for task in tasks if task["name"] == "convert"),
            None,
        )
        percent = int((convert_task or {}).get("percent", 0) or 0)
        overall_progress = min(
            99,
            max(
                1,
                int(
                    ((file_index - 1) + percent / 100)
                    / total_files
                    * 100
                ),
            ),
        )
        current_job = get_compress_job(job_id)
        set_compress_job(
            job_id,
            progress=max(
                current_job.get("progress", 0) if current_job else 0,
                overall_progress,
            ),
            message=(
                f"Video {file_index} / {total_files}: "
                f"CloudConvert {percent}% encoded..."
            ),
        )

        local_job = get_compress_job(job_id)
        if not local_job or local_job.get("cancelled"):
            requests.post(
                f"https://api.cloudconvert.com/v2/jobs/{job_id_remote}/cancel",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            return False

        if data["status"] == "finished":
            export_task = next(
                task for task in tasks if task["name"] == "export"
            )
            output_url = export_task["result"]["files"][0]["url"]
            with requests.get(output_url, stream=True, timeout=600) as download:
                require_cloudconvert_success(download)
                with open(output_path, "wb") as output:
                    for chunk in download.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
            return True

        if data["status"] == "error":
            errors = [
                task.get("message", "CloudConvert task failed.")
                for task in tasks
                if task.get("status") == "error"
            ]
            raise RuntimeError("; ".join(errors))

        time.sleep(2)


def compress_one_video(
    job_id,
    file_id,
    credentials,
    quality,
    file_index,
    total_files,
    temp_dir,
):
    service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    metadata = service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size",
    ).execute()
    filename = safe_name(
        metadata.get("name", f"video_{file_index}")
    )
    if not filename.endswith(".mp4"):
        filename += ".mp4"

    input_path = os.path.join(
        temp_dir,
        f"input_{file_index}_{filename}",
    )
    output_path = os.path.join(
        temp_dir,
        f"output_{file_index}_{filename}",
    )
    set_compress_job(
        job_id,
        message=f"Video {file_index} / {total_files}: downloading {filename}...",
    )
    download_drive_file(credentials, file_id, input_path)
    set_compress_job(
        job_id,
        message=f"Video {file_index} / {total_files}: starting conversion...",
    )
    if not cloudconvert_video(
        input_path,
        output_path,
        quality,
        job_id,
        filename,
        file_index,
        total_files,
    ):
        raise RuntimeError(f"Failed to compress {filename}")
    try:
        os.remove(input_path)
    except OSError:
        pass
    return file_index, output_path


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
                        fields=(
                            "id,name,mimeType,size,"
                            "videoMediaMetadata(durationMillis)"
                        ),
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
                            f"{index} / {total} files"
                        ),
                    )

                except Exception as exc:
                    set_job(
                        job_id,
                        status="error",
                        error=(
                            f"Could not download "
                            f"file {index}: {exc}"
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
            zip_path_object=store_artifact(
                job_id, zip_path, "DriveBatch.zip"
            ),
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

        output_files = [None] * total
        workers = min(3, total)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    compress_one_video,
                    job_id,
                    file_id,
                    credentials,
                    quality,
                    index,
                    total,
                    temp_dir,
                ): index
                for index, file_id in enumerate(file_ids, start=1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    _, output_path = future.result()
                    output_files[index - 1] = output_path
                    completed = sum(
                        output_file is not None
                        for output_file in output_files
                    )
                    set_compress_job(
                        job_id,
                        completed=completed,
                        progress=max(
                            get_compress_job(job_id).get("progress", 0),
                            int(completed / total * 100),
                        ),
                        message=f"{completed} / {total} videos ready",
                    )
                except Exception as exc:
                    set_compress_job(
                        job_id,
                        status="error",
                        error=f"Could not compress video {index}: {exc}",
                    )
                    return

        if any(output_file is None for output_file in output_files):
            raise RuntimeError("One or more videos did not produce an output.")

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
                zip_path_object=store_artifact(
                    job_id, zip_path, "StreamSaver.zip"
                ),
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
                file_path_object=(
                    store_artifact(
                        job_id,
                        output_files[0],
                        os.path.basename(output_files[0]),
                    )
                    if output_files
                    else None
                ),
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
        if not file_ids and data.get("file_id"):
            file_ids = [data["file_id"]]

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
                "quality": "original",
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
        "quality": job.get("quality"),
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

    path, temporary = materialize_artifact(job, "zip_path", ".zip")

    if not path:
        return jsonify({
            "error": "ZIP file is no longer available."
        }), 404
    try:
        return send_file(
            path,
            mimetype="application/zip",
            as_attachment=True,
            download_name="DriveBatch.zip",
            max_age=0,
        )
    finally:
        if temporary:
            threading.Timer(
                60,
                lambda: os.path.exists(path) and os.remove(path),
            ).start()


@app.route("/api/save-to-drive", methods=["POST"])
def save_to_drive():
    try:
        credentials = credentials_copy()

        if not credentials:
            return jsonify({
                "error": "Please connect Google Drive first."
            }), 401

        data = request.get_json(silent=True) or {}

        job_id = str(data.get("job_id", "")).strip()
        job_type = str(data.get("job_type", "download")).strip().lower()
        folder_url = str(data.get("folder_url", "")).strip()
        custom_name = str(data.get("filename", "")).strip() or None

        if not job_id:
            return jsonify({
                "error": "A job id is required."
            }), 400

        if job_type == "download":
            job = get_job(job_id)
        elif job_type == "compress":
            job = get_compress_job(job_id)
        else:
            return jsonify({
                "error": "job_type must be download or compress."
            }), 400

        if not job:
            return jsonify({
                "error": "Job not found."
            }), 404

        target_key = "zip_path" if (
            job.get("zip_path") or job.get("zip_path_object")
        ) else "file_path"
        target_path, temporary = materialize_artifact(
            job,
            target_key,
            ".zip" if target_key == "zip_path" else ".mp4",
        )
        if not target_path:
            return jsonify({
                "error": "The output file is not available to save."
            }), 409

        folder_id = None
        if folder_url:
            folder_id = extract_folder_id(folder_url)
            if not folder_id:
                return jsonify({
                    "error": "Could not find a valid Google Drive folder in that link."
                }), 400

        service = build(
            "drive",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

        filename = custom_name or os.path.basename(target_path)
        metadata = {"name": filename}
        if folder_id:
            metadata["parents"] = [folder_id]

        media = MediaFileUpload(
            target_path,
            resumable=True,
            mimetype="application/zip" if filename.lower().endswith(".zip") else "application/octet-stream",
        )

        result = service.files().create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink",
        ).execute()

        response = {
            "success": True,
            "file_id": result.get("id"),
            "name": result.get("name"),
            "link": result.get("webViewLink"),
        }
        if temporary:
            try:
                os.remove(target_path)
            except OSError:
                pass
        return jsonify(response)

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


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


@app.route("/api/variants/start", methods=["POST"])
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
        if not file_ids and data.get("file_id"):
            file_ids = [data["file_id"]]

        quality = data.get("quality", "720p")

        if quality not in ["360p", "480p", "720p", "1080p"]:
            return jsonify({
                "error": "Invalid quality. Choose 360p, 480p, 720p, or 1080p."
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

        response = {
            "success": True,
            "job_id": job_id,
            "is_batch": len(file_ids) > 1,
            "quality": quality,
        }
        if len(file_ids) == 1:
            response["status_url"] = (
                f"/api/variants/status/{job_id}"
            )
            response["download_url"] = (
                f"/api/variants/download/{job_id}"
            )

        return jsonify(response)

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 500


@app.route("/api/variants/status/<job_id>")
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
        if job.get("file_path"):
            response["download_url"] = (
                f"/api/variants/download/{job_id}"
            )

    return jsonify(response)


@app.route(
    "/api/variants/cancel/<job_id>",
    methods=["POST"],
)
@app.route(
    "/api/compress/cancel/<job_id>",
    methods=["POST"],
)
def cancel_compress(job_id):
    job = get_compress_job(job_id)

    if not job:
        return jsonify({
            "error": "Compression job not found."
        }), 404

    set_compress_job(
        job_id,
        cancelled=True,
        status="cancelled",
        message="Compression cancelled.",
    )

    return jsonify({"success": True})


def stream_local_file(path, filename, mimetype):
    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")
    chunk_number = request.args.get("chunk")
    chunk_size = 25 * 1024 * 1024
    start = 0
    end = file_size - 1
    status = 200

    if not range_header and chunk_number is not None:
        try:
            chunk_index = int(chunk_number)
        except ValueError:
            chunk_index = -1

        if chunk_index < 0:
            return Response(status=416)

        start = chunk_index * chunk_size
        end = min(start + chunk_size - 1, file_size - 1)
        if start >= file_size:
            return Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        status = 206

    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            return Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        requested_start, requested_end = match.groups()
        if requested_start:
            start = int(requested_start)
            if requested_end:
                end = int(requested_end)
        elif requested_end:
            length = int(requested_end)
            start = max(file_size - length, 0)

        if start >= file_size or start > end:
            return Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        end = min(end, file_size - 1)
        status = 206

    content_length = end - start + 1

    def generate():
        with open(path, "rb") as source:
            source.seek(start)
            remaining = content_length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": (
            f'attachment; filename="{safe_name(filename)}"'
        ),
        "Cache-Control": "private, max-age=3600",
    }
    if status == 206:
        headers["Content-Range"] = (
            f"bytes {start}-{end}/{file_size}"
        )

    return Response(
        stream_with_context(generate()),
        status=status,
        mimetype=mimetype,
        headers=headers,
    )


@app.route("/api/variants/download/<job_id>")
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

    zip_path, zip_temporary = materialize_artifact(
        job, "zip_path", ".zip"
    )
    file_path, file_temporary = materialize_artifact(
        job, "file_path", ".mp4"
    )

    if zip_path:
        response = send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name="StreamSaver.zip",
            max_age=0,
        )
        if zip_temporary:
            threading.Timer(
                60,
                lambda: os.path.exists(zip_path) and os.remove(zip_path),
            ).start()
        return response
    elif file_path:
        response = stream_local_file(
            file_path,
            os.path.basename(file_path),
            "video/mp4",
        )
        if file_temporary:
            threading.Timer(
                60,
                lambda: os.path.exists(file_path) and os.remove(file_path),
            ).start()
        return response
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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


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
    
