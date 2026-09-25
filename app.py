import os
import re
import requests
import yt_dlp
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

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
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
    """Extracts pre-transcoded Google web stream variants using yt-dlp for instant compressed downloads."""
    quality = request.args.get("cpn") or request.args.get("quality")
    drive_page_url = f"https://drive.google.com/file/d/{file_id}/view"

    target_format = None
    stream_url = None

    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(drive_page_url, download=False)
            formats = info.get('formats', [])

        # Parse requested quality height (e.g. "1080p" -> 1080)
        target_height = None
        if quality and quality != "original":
            match = re.search(r'(\d+)', quality)
            if match:
                target_height = int(match.group(1))

        if target_height:
            # Find matching resolution format with a valid CDN URL
            matching = [f for f in formats if f.get('height') == target_height and f.get('url')]
            if matching:
                target_format = matching[0]

        # If no specific resolution match found, pick the standard web stream or fallback
        if not target_format and formats:
            target_format = formats[-1]

        if target_format and target_format.get('url'):
            stream_url = target_format['url']

    except Exception:
        pass  # Fallback gracefully if yt-dlp extraction fails

    # Absolute fallback to standard Drive export link if manifest lookup fails
    if not stream_url:
        stream_url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"

    try:
        upstream_resp = requests.get(stream_url, stream=True, allow_redirects=True)
        
        def generate():
            for chunk in upstream_resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
                    
        ext = target_format.get('ext', 'mp4') if target_format else 'mp4'
        output_filename = f"video_{file_id}_{quality or 'original'}.{ext}"
        
        headers = {
            "Content-Type": upstream_resp.headers.get("Content-Type", "video/mp4"),
            "Content-Disposition": f"attachment; filename={output_filename}"
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
