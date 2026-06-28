from flask import Flask, request, jsonify, render_template, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import yt_dlp
import os
import urllib.parse
import shutil

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

@app.route('/')
def index():
    return render_template('index.html')

# API Route to extract video metadata
# Restricted to 10 extractions per minute per IP address
@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract_video():
    try:
        data = request.get_json() or {}
        video_url = data.get('url')
        
        if not video_url:
            return jsonify({'error': 'Please provide a valid video URL.'}), 400

        # Configure yt-dlp options for metadata extraction
        ydl_opts = {
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract video info without downloading
            info = ydl.extract_info(video_url, download=False)
            
            # Detect platform
            source = info.get('extractor_key', 'Unknown').upper()
            if 'instagram' in video_url.lower() or 'instagram' in source.lower():
                source = 'INSTAGRAM'
            elif 'youtube' in video_url.lower() or 'youtu.be' in video_url.lower() or 'youtube' in source.lower():
                source = 'YOUTUBE'

            formats = []
            
            # Construct server merge option (downloads & merges on server side)
            encoded_url = urllib.parse.quote(video_url)
            
            # Check if ffmpeg is available
            ffmpeg_available = shutil.which('ffmpeg') is not None
            
            # If ffmpeg is missing, server can't merge bestvideo+bestaudio.
            # We fallback to 'best' (pre-merged best quality video and audio stream)
            server_format_id = 'bestvideo+bestaudio/best' if ffmpeg_available else 'best'
            resolution_label = '1080p / 4K (Best Quality)' if ffmpeg_available else 'Best Quality (Pre-merged)'
            
            formats.append({
                'format_id': server_format_id,
                'resolution': resolution_label,
                'ext': 'mp4',
                # This points to our backend downloader which will download and serve
                'url': f'/api/download?url={encoded_url}&format_id={server_format_id}',
                'is_direct': False,
                'filesize': 0,
                'badge': 'Server'
            })

            # Process direct formats
            direct_formats = []
            for fmt in info.get('formats', []):
                # We need a direct stream url
                url = fmt.get('url')
                if not url:
                    continue
                
                # Check if it has both video and audio stream to avoid direct silent downloads
                vcodec = fmt.get('vcodec')
                acodec = fmt.get('acodec')
                is_combined = vcodec and vcodec != 'none' and acodec and acodec != 'none'
                
                if is_combined:
                    resolution = fmt.get('resolution') or (f"{fmt.get('height')}p" if fmt.get('height') else "Unknown")
                    
                    # Avoid showing generic "audio-only" or raw sizes
                    if "audio only" in resolution.lower():
                        continue
                    
                    direct_formats.append({
                        'format_id': fmt.get('format_id'),
                        'resolution': resolution,
                        'ext': fmt.get('ext', 'mp4'),
                        'url': url,
                        'is_direct': True,
                        'filesize': fmt.get('filesize') or fmt.get('filesize_approx') or 0,
                        'badge': 'Direct'
                    })

            # Reverse to put highest direct qualities at the top (typically 720p, 360p)
            direct_formats.reverse()
            
            # Combine formats (server first, then direct)
            formats.extend(direct_formats)

            # Clean and prepare metadata response
            response_data = {
                'title': info.get('title', 'Unknown Video'),
                'author': info.get('uploader') or info.get('uploader_id') or 'Unknown Creator',
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'source': source,
                'download_url': info.get('url'), # Best mixed format
                'formats': formats[:8] # Send top 8 formats
            }
            return jsonify(response_data)
            
    except Exception as e:
        error_msg = str(e)
        if "Incomplete YouTube ID" in error_msg or "not a valid URL" in error_msg or "Unsupported URL" in error_msg:
            return jsonify({'error': 'Invalid URL format. Please check the link and try again.'}), 400
        return jsonify({'error': f'Failed to parse URL: {error_msg[:100]}...'}), 500

# Backend Download and Merge Route
# Restricted to 5 downloads per minute per IP to protect bandwidth
@app.route('/api/download', methods=['GET'])
@limiter.limit("5 per minute")
def download_video():
    video_url = request.args.get('url')
    format_id = request.args.get('format_id', 'best')
    if not video_url:
        return "Video URL parameter is missing", 400
    try:
        # Check if ffmpeg is available
        ffmpeg_available = shutil.which('ffmpeg') is not None
        
        # If requested format_id needs merging but ffmpeg is missing, fallback to 'best'
        actual_format = format_id
        if 'bestvideo+bestaudio' in format_id and not ffmpeg_available:
            actual_format = 'best'

        # Configuration for downloading
        ydl_opts = {
            'format': actual_format,
            # Saves inside the local downloads directory
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Download
            info = ydl.extract_info(video_url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Since yt-dlp might change extension (e.g. to mkv or mp4), let's locate the actual file
            base, _ = os.path.splitext(filename)
            actual_file = None
            for ext in ['mp4', 'mkv', 'webm', '3gp', 'mov']:
                test_path = f"{base}.{ext}"
                if os.path.exists(test_path):
                    actual_file = test_path
                    break
            if not actual_file or not os.path.exists(actual_file):
                actual_file = filename
            if os.path.exists(actual_file):
                # Send the merged/downloaded file to user's browser
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
