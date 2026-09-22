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

def drive_service():
    if GOOGLE_API_KEY:
        return build("drive", "v3", developerKey=GOOGLE_API_KEY, cache_discovery=False)
    return None

def extract_folder_id(url):
    patterns = [r"/folders/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)"]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    return None

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

def scan_recursive(service, folder_id, visited=None, media_type="video"):
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
            if target_id: item_id = target_id

        if mime == "application/vnd.google-apps.folder":
            results.extend(scan_recursive(service, item_id, visited, media_type))
        else:
            # Handles videos, audio/music, and images cleanly
            if media_type == "all" or media_type in mime:
                results.append({
                    "id": item_id,
                    "name": name,
                    "mimeType": mime,
                    "size": int(item.get("size") or 0)
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
        if not service: return jsonify({"error": "Google API access is not configured."}), 401

        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        media_type = str(data.get("media_type", "video")).strip().lower()

        folder_id = extract_folder_id(url)
        if not folder_id: return jsonify({"error": "Could not find the Drive folder ID."}), 400

        items = scan_recursive(service, folder_id, media_type=media_type)

        # Inject public URLs without hitting Google's security gates on the backend
        for item in items:
            item['direct_url'] = f"https://google.com{item['id']}&confirm=t"

        return jsonify({
            "success": True,
            "items": items,
            "count": len(items),
            "media_type": media_type
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
