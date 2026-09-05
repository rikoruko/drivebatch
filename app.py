import io
import os
import re
import zipfile
from tempfile import TemporaryDirectory

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
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
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    ".mpeg", ".mpg", ".ogv", ".3gp", ".flv", ".wmv"
}


def client_config():
    raw = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")

    if raw:
        import json
        return json.loads(raw)

    path = "client_secret.json"
    if os.path.exists(path):
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError("Google OAuth credentials are not configured.")


def redirect_uri():
    configured = os.environ.get("OAUTH_REDIRECT_URI")
    if configured:
        return configured

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/") + "/oauth2callback"

    return "http://localhost:5000/oauth2callback"


def make_flow(state=None):
    flow = Flow.from_client_config(
        client_config(),
        scopes=SCOPES,
        redirect_uri=redirect_uri(),
    )

    if state:
        flow.state = state

    return flow


def drive_service():
    data = session.get("google_credentials")

    if not data:
        return None

    credentials = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def extract_folder_id(value):
    value = (value or "").strip()

    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)

    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", value)
    if match:
        return match.group(1)

    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", value):
        return value

    return None


def is_video(file):
    mime = file.get("mimeType", "")
    name = file.get("name", "").lower()

    if mime in VIDEO_MIMES:
        return True

    return any(name.endswith(ext) for ext in VIDEO_EXTENSIONS)


def format_size(size):
    if not size:
        return "Unknown size"

    size = int(size)

    units = ["B", "KB", "MB", "GB", "TB"]

    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024

    return "Unknown size"


def list_children(service, folder_id):
    results = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,size)",
            pageSize=1000,
            pageToken=page_token,
            orderBy="name",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        results.extend(response.get("files", []))

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return results


def scan_folder(service, folder_id, path=""):
    videos = []

    for item in list_children(service, folder_id):
        item_path = f"{path}/{item['name']}".strip("/")

        if item.get("mimeType") == "application/vnd.google-apps.folder":
            videos.extend(
                scan_folder(service, item["id"], item_path)
            )

        elif is_video(item):
            videos.append({
                "id": item["id"],
                "name": item["name"],
                "path": item_path,
                "size": int(item.get("size", 0) or 0),
                "size_display": format_size(item.get("size")),
            })

    return videos


@app.route("/")
def index():
    return render_template(
        "index.html",
        connected=bool(session.get("google_credentials"))
    )


@app.route("/login")
def login():
    flow = make_flow()

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    session["oauth_state"] = state

    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    state = session.get("oauth_state")

    flow = make_flow(state)

    flow.fetch_token(authorization_response=request.url)

    credentials = flow.credentials

    session["google_credentials"] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }

    session.pop("oauth_state", None)

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/scan", methods=["POST"])
def scan():
    service = drive_service()

    if not service:
        return jsonify({
            "error": "Connect Google Drive first."
        }), 401

    data = request.get_json(silent=True) or {}

    folder_url = data.get("url", "")
    folder_id = extract_folder_id(folder_url)

    if not folder_id:
        return jsonify({
            "error": "Please enter a valid Google Drive folder link."
        }), 400

    try:
        folder = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()

        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            return jsonify({
                "error": "That link does not point to a Google Drive folder."
            }), 400

        videos = scan_folder(service, folder_id)

        return jsonify({
            "folder": folder.get("name", "Google Drive folder"),
            "videos": videos,
            "count": len(videos),
        })

    except Exception as exc:
        return jsonify({
            "error": f"Could not scan this folder: {str(exc)}"
        }), 400


@app.route("/api/download", methods=["POST"])
def download():
    service = drive_service()

    if not service:
        return jsonify({
            "error": "Connect Google Drive first."
        }), 401

    data = request.get_json(silent=True) or {}
    video_ids = data.get("ids", [])

    if not video_ids:
        return jsonify({
            "error": "No videos selected."
        }), 400

    if len(video_ids) > 100:
        return jsonify({
            "error": "Please select no more than 100 videos at once."
        }), 400

    temp_dir = TemporaryDirectory()
    zip_path = os.path.join(temp_dir.name, "drivebatch-videos.zip")

    try:
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED
        ) as archive:

            used_names = set()

            for index, file_id in enumerate(video_ids):
                metadata = service.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType",
                    supportsAllDrives=True,
                ).execute()

                filename = metadata.get("name", f"video-{index + 1}")

                if filename in used_names:
                    base, ext = os.path.splitext(filename)
                    filename = f"{base}-{index + 1}{ext}"

                used_names.add(filename)

                local_path = os.path.join(
                    temp_dir.name,
                    f"video-{index}"
                )

                request_media = service.files().get_media(
                    fileId=file_id,
                    supportsAllDrives=True,
                )

                with open(local_path, "wb") as output:
                    downloader = MediaIoBaseDownload(
                        output,
                        request_media,
                        chunksize=1024 * 1024,
                    )

                    done = False

                    while not done:
                        _, done = downloader.next_chunk()

                archive.write(local_path, arcname=filename)

                try:
                    os.remove(local_path)
                except OSError:
                    pass

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name="drivebatch-videos.zip",
        )

    except Exception as exc:
        temp_dir.cleanup()

        return jsonify({
            "error": f"Download failed: {str(exc)}"
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
              )
