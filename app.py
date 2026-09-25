import os
import re
from flask import Flask, request, jsonify, render_template, redirect
import yt_dlp
from googleapiclient.discovery import build
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
    """Extracts Google's pre-rendered resolution streams instantly using yt-dlp and redirects."""
    quality = request.args.get("quality") or request.args.get("cpn") or "original"
    drive_url = f"https://drive.google.com/file/d/{file_id}/view"

    format_selector = 'best'
    if "1080" in quality:
        format_selector = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
    elif "720" in quality:
        format_selector = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
    elif "480" in quality:
        format_selector = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
    elif "360" in quality:
        format_selector = 'bestvideo[height<=360]+bestaudio/best[height<=360]'

        ydl_opts = {
        'format': format_selector,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(drive_url, download=False)
            stream_url = info.get('url')
            
            if not stream_url and 'formats' in info:
                for f in info['formats']:
                    if f.get('url'):
                        stream_url = f.get('url')
                        
            if stream_url:
                return redirect(stream_url)
                
            return redirect(f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}")
    except Exception:
        return redirect(f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}")

@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    return jsonify({"connected": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
    
