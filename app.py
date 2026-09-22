import os
import re
from flask import Flask, render_template, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix
from googleapiclient.discovery import build

app = Flask(__name__)

# Enforce Cross-Origin Isolation required by client-side FFmpeg.wasm
@app.after_request
def add_security_headers(response):
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    response.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    return response

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "drivebatch-secret")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()

# PRESERVED FROM YOUR ORIGINAL REPO: Exact MIME and Extension validation structures
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

AUDIO_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
    "audio/ogg", "audio/flac", "audio/aac", "audio/mp4",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mpeg", ".mpg", ".m4v", ".3gp", ".flv", ".wmv"
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".bmp", ".heic", ".heif", ".tif", ".tiff", ".svg"
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".oga", ".flac", ".aac", ".m4a", ".opus",
}

def drive_service():
    if GOOGLE_API_KEY:
        return build("drive", "v3", developerKey=GOOGLE_API_KEY, cache_discovery=False)
    return None

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

# PRESERVED FROM YOUR ORIGINAL REPO: Strict structural file matching logic
def is_video(file):
    mime = str(file.get("mimeType", "")).lower()
    if mime in VIDEO_MIMES: return True
    name = str(file.get("name", "")).lower()
    return os.path.splitext(name)[1] in VIDEO_EXTENSIONS

def is_image(file):
    mime = str(file.get("mimeType", "")).lower()
    if mime in IMAGE_MIMES: return True
    name = str(file.get("name", "")).lower()
    return os.path.splitext(name)[1] in IMAGE_EXTENSIONS

def is_audio(file):
    mime = str(file.get("mimeType", "")).lower()
    if mime in AUDIO_MIMES: return True
    name = str(file.get("name", "")).lower()
    return os.path.splitext(name)[1] in AUDIO_EXTENSIONS

def list_children(service, folder_id):
    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType,size,shortcutDetails)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token: break
    return files

def scan_recursive(service, folder_id, path="", visited=None, media_type="video"):
    if visited is None: visited = set()
    if folder_id in visited: return []
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

        current_path = f"{path}/{name}" if path else name

        if mime == "application/vnd.google-apps.folder":
            results.extend(scan_recursive(service, item_id, current_path, visited, media_type))
        elif media_type == "video" and is_video(item):
            results.append({
                "id": item_id, "name": name, "mimeType": mime,
                "size": int(item.get("size") or 0), "path": current_path, "type": "video"
            })
        elif media_type == "image" and is_image(item):
            results.append({
                "id": item_id, "name": name, "mimeType": mime,
                "size": int(item.get("size") or 0), "path": current_path, "type": "image"
            })
        elif media_type == "audio" and is_audio(item):
            results.append({
                "id": item_id, "name": name, "mimeType": mime,
                "size": int(item.get("size") or 0), "path": current_path, "type": "audio"
            })
    return results

@app.route("/")
def index(): return render_template("index.html")

@app.route("/privacy")
def privacy(): return render_template("privacy.html")

@app.route("/terms")
def terms(): return render_template("terms.html")

@app.route("/api/auth/status")
def auth_status(): return jsonify({"connected": False, "public_only": True})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        service = drive_service()
        if not service: return jsonify({"error": "Public Google Drive access is not configured."}), 401

        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        media_type = str(data.get("media_type", "video")).strip().lower()

        if media_type not in {"video", "image", "audio"}:
            return jsonify({"error": "media_type must be video, image, or audio."}), 400

        if not url: return jsonify({"error": "Please provide a Google Drive folder link."}), 400

        folder_id = extract_folder_id(url)
        if not folder_id: return jsonify({"error": "Could not find the Drive folder ID."}), 400

        items = scan_recursive(service, folder_id, media_type=media_type)

        # FIXED DIRECT URL GENERATION: Direct handoff bypasses security scan blocks natively
        for item in items:
            item['direct_url'] = f"https://google.com{item['id']}&confirm=t"

        # FIXED MAPPER: Output keys perfectly matched to your template results table expectations
        return jsonify({
            "success": True,
            "items": items,
            "videos": items if media_type == "video" else [],
            "images": items if media_type == "image" else [],
            "audio": items if media_type == "audio" else [],
            "count": len(items),
            "media_type": media_type
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
