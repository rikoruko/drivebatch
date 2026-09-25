import os
import re
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "drivebatch-dev-secret-key-change-in-prod")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@app.after_request
def add_security_headers(response):
    """
    Required security headers to enable SharedArrayBuffer in modern browsers.
    Unlocks multi-threaded WASM performance for client-side operations.
    """
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    response.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    return response


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
    api_key = os.environ.get("GOOGLE_API_KEY", "")

    while True:
        results = service.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            # Requested videoMediaMetadata and webContentLink for stream processing
            fields="nextPageToken, files(id, name, mimeType, size, thumbnailLink, webViewLink, webContentLink, videoMediaMetadata)"
        ).execute()

        files = results.get("files", [])
        for f in files:
            m_type = f.get("mimeType", "")
            if m_type == "application/vnd.google-apps.folder":
                sub_path = f"{parent_path}/{f['name']}" if parent_path else f['name']
                collected.extend(fetch_files_recursive(service, f["id"], mime_prefix, sub_path))
            elif mime_prefix == "*" or m_type.startswith(mime_prefix):
                f["path"] = parent_path or "Google Drive"
                
                file_id = f.get("id")
                
                # Original master binary endpoint
                if api_key:
                    f["direct_url"] = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
                else:
                    f["direct_url"] = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

                # Google Drive pre-processed video preview stream endpoint
                f["google_stream_url"] = f"https://drive.google.com/videoplayback?id={file_id}"

                collected.append(f)

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    return collected


# -------------------------------------------------------------------
# PAGES
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


# -------------------------------------------------------------------
# ZERO-EGRESS METADATA API
# -------------------------------------------------------------------

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"connected": False, "public_only": True})


@app.route("/api/scan", methods=["POST"])
def scan_folder():
    """
    Scans Google Drive folder hierarchy and returns metadata directly to the client.
    No binary data passes through this server.
    """
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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "zero_egress": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
