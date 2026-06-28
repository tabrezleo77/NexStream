from flask import Flask, request, jsonify, render_template, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import yt_dlp
import os
import urllib.parse

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Protect Flask sessions with secret key from environment
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default_fallback_secret_key')

# Setup Rate Limiting (Protects API routes from scraping/denial of service)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["100 per day", "30 per hour"],
    storage_uri="memory://"
)

# Create a downloads directory next to app.py
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Helper to automatically load cookies if cookies.txt or COOKIES_FROM_BROWSER is present
def get_ydl_opts(base_opts):
    ydl_opts = base_opts.copy()
    
    # Check if cookies.txt exists in the root directory (recommended for server deployment)
    cookies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
    if os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    else:
        # Check if a browser is configured to extract cookies from (recommended for local development)
        cookies_browser = os.getenv('COOKIES_FROM_BROWSER')
        if cookies_browser:
            ydl_opts['cookiesfrombrowser'] = (cookies_browser,)
            
    return ydl_opts

@app.route('/')
def index():
    return render_template('index.html')

# API Route to extract video metadata
# Restricted to 10 extractions per minute per IP address
@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract_video():
    try:
        data = request.get_json()
        video_url = data.get('url')
        
        if not video_url:
            return jsonify({'error': 'Please provide a valid video URL.'}), 400

        # Extract basic info
        ydl_opts = get_ydl_opts({
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            is_youtube = 'youtube' in info.get('extractor_key', '').lower() or 'youtube' in info.get('extractor', '').lower()

            formats = []

            # 1. Add High-Quality Server Merging Option (using /api/download)
            encoded_url = urllib.parse.quote(video_url)
            formats.append({
                'format_id': 'bestvideo+bestaudio/best',
                'resolution': '1080p / 4K (Best Quality)',
                'ext': 'mp4',
                'url': f'/api/download?url={encoded_url}&format_id=bestvideo+bestaudio/best',
                'is_direct': False,
                'filesize': 0,
                'badge': 'High Quality'
            })

            # 2. Extract direct streams
            for fmt in info.get('formats', []):
                vcodec = fmt.get('vcodec')
                acodec = fmt.get('acodec')
                
                # Check for combined video + audio
                if is_youtube:
                    is_combined = vcodec and vcodec != 'none' and acodec and acodec != 'none'
                else:
                    is_combined = vcodec != 'none' and acodec != 'none'

                if fmt.get('url') and is_combined:
                    resolution = fmt.get('resolution') or (f"{fmt.get('height')}p" if fmt.get('height') else "Unknown")
                    
                    if "audio only" in resolution.lower():
                        continue

                    formats.append({
                        'format_id': fmt.get('format_id'),
                        'resolution': f"{resolution} (Fast Direct)",
                        'ext': fmt.get('ext', 'mp4'),
                        'url': fmt.get('url'),
                        'is_direct': True,
                        'filesize': fmt.get('filesize') or fmt.get('filesize_approx') or 0,
                        'badge': 'Direct'
                    })

            # Reverse to put highest direct qualities at the top
            direct_formats = [f for f in formats if f['is_direct']]
            direct_formats.reverse()
            
            # Keep top 4 direct formats + 1 server format
            final_formats = [formats[0]] + direct_formats[:4]

            response_data = {
                'title': info.get('title', 'Unknown Video'),
                'author': info.get('uploader', 'Unknown Creator'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'source': info.get('extractor_key', 'Unknown'),
                'download_url': info.get('url'),
                'formats': final_formats
            }
            return jsonify(response_data)
            
    except Exception as e:
        error_msg = str(e)
        if "Incomplete YouTube ID" in error_msg or "not a valid URL" in error_msg:
            return jsonify({'error': 'Invalid URL format. Please check the link and try again.'}), 400
        return jsonify({'error': f'Failed to parse URL: {error_msg[:100]}...'}), 500

# Backend Download and Merge Route
# Restricted to 5 downloads per minute per IP to protect bandwidth
@app.route('/api/download', methods=['GET'])
@limiter.limit("5 per minute")
def download_video():
    video_url = request.args.get('url')
    format_id = request.args.get('format_id', 'bestvideo+bestaudio/best')

    if not video_url:
        return "Video URL parameter is missing", 400

    try:
        ydl_opts = get_ydl_opts({
            'format': format_id,
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            filename = ydl.prepare_filename(info)
            
            base, _ = os.path.splitext(filename)
            actual_file = None
            for ext in ['mp4', 'mkv', 'webm', '3gp']:
                test_path = f"{base}.{ext}"
                if os.path.exists(test_path):
                    actual_file = test_path
                    break

            if not actual_file or not os.path.exists(actual_file):
                actual_file = filename

            if os.path.exists(actual_file):
                return send_file(actual_file, as_attachment=True)
            else:
                return "Failed to find the downloaded file on server.", 404

    except Exception as e:
        return f"Error during server processing: {str(e)}", 500

# Error handler for rate limit exceedance
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        'error': 'Too many requests. Please slow down and try again later.'
    }), 429

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, port=port)
