import os
import re
import requests
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

def extract_folder_id(url):
    """Extract folder ID from a Google Drive URL."""
    if not url:
        return None
    url = url.strip()
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    if re.match(r'^[a-zA-Z0-9_-]+$', url):
        return url
    return None

def build_drive_service():
    """Build public Google Drive API service using API key."""
    api_key = os.getenv("GOOGLE_DRIVE_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_DRIVE_API_KEY environment variable is not set.")
    return build('drive', 'v3', developerKey=api_key)

def scan_drive_folder(folder_id, media_type="video"):
    """Recursively list files inside a public Google Drive folder."""
    service = build_drive_service()
    
    mime_type_filters = {
        "video": "mimeType contains 'video/'",
        "image": "mimeType contains 'image/'",
        "audio": "mimeType contains 'audio/'"
    }
    file_filter = mime_type_filters.get(media_type, "mimeType contains 'video/'")
    
    items = []
    folders_to_scan = [(folder_id, "")]

    while folders_to_scan:
        current_folder_id, current_path = folders_to_scan.pop(0)
        
        # 1. Fetch sub-folders for recursive scanning (with pagination support)
        page_token = None
        while True:
            folder_res = service.files().list(
                q=f"'{current_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token
            ).execute()
            
            for subfolder in folder_res.get('files', []):
                subpath = f"{current_path}/{subfolder['name']}" if current_path else subfolder['name']
                folders_to_scan.append((subfolder['id'], subpath))
                
            page_token = folder_res.get('nextPageToken')
            if not page_token:
                break

        # 2. Fetch target media files
        file_query = f"'{current_folder_id}' in parents and {file_filter} and trashed = false"
        page_token = None
        while True:
            res = service.files().list(
                q=file_query,
                fields="nextPageToken, files(id, name, size, mimeType)",
                pageToken=page_token
            ).execute()

            for file in res.get('files', []):
                file_id = file['id']
                # Route direct_url through the Flask proxy to prevent browser CORS / NetworkErrors
                direct_url = f"/api/download/{file_id}"
                
                items.append({
                    "id": file_id,
                    "name": file['name'],
                    "size": int(file.get('size', 0)),
                    "path": current_path or "Root",
                    "mimeType": file.get('mimeType', ''),
                    "direct_url": direct_url
                })

            page_token = res.get('nextPageToken')
            if not page_token:
                break

    return items

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/")
def privacy():
    return render_template("privacy.html")

@app.route("/")
def terms():
    return render_template("terms.html")

@app.route("/api/scan", methods=["POST"])
def scan():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    media_type = data.get("media_type", "video")

    if not url:
        return jsonify({"error": "Drive URL is required."}), 400

    folder_id = extract_folder_id(url)
    if not folder_id:
        return jsonify({"error": "Invalid Google Drive folder link."}), 400

    try:
        items = scan_drive_folder(folder_id, media_type)
        key_name = "images" if media_type == "image" else "audio" if media_type == "audio" else "videos"
        return jsonify({key_name: items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download/<file_id>")
def proxy_download(file_id):
    """Proxy Google Drive pre-processed video stream variants (itags) or raw downloads through Flask."""
    quality = request.args.get("cpn")
    
    # Map quality selections to Google Drive web player stream itags
    itag_map = {
        "1080p": "37",
        "720p": "22",
        "360p": "18"
    }
    
    itag = itag_map.get(quality)

    if not itag or quality == "original":
        # Fallback to original raw master download
        target_url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
    else:
        # Target Google Drive's pre-rendered web player stream variant endpoint
        target_url = f"https://drive.google.com/uc?export=view&id={file_id}&itag={itag}"

    try:
        req = requests.get(target_url, stream=True, allow_redirects=True)
        
        def generate():
            for chunk in req.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
                    
        headers = {
            "Content-Type": req.headers.get("Content-Type", "video/mp4"),
            "Content-Disposition": req.headers.get("Content-Disposition", f"attachment; filename=video_{file_id}_{quality or 'original'}.mp4")
        }
        
        return Response(stream_with_context(generate()), headers=headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    return jsonify({"connected": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
