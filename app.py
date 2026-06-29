from flask import Flask, request, jsonify, render_template, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import yt_dlp
import os
import glob
import shutil
import threading
import time
import uuid
import urllib.parse
import logging

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Protect Flask sessions with secret key from environment
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default_fallback_secret_key_change_me')

# ── Rate Limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ── Download Directory ────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Auto-delete downloaded files after this many seconds (default 10 min)
FILE_TTL = int(os.getenv('FILE_TTL_SECONDS', 600))

# ── Cookie helpers ────────────────────────────────────────────────────────────
COOKIES_FILE = os.path.join(BASE_DIR, 'cookies.txt')

def _apply_cookies(ydl_opts: dict) -> dict:
    """
    Priority order:
      1. cookies.txt next to app.py  (best for server / production)
      2. COOKIES_FROM_BROWSER env var  (good for local dev)
      3. No cookies — yt-dlp will try unauthenticated
    Returns a *copy* of ydl_opts with cookie settings applied.
    """
    opts = ydl_opts.copy()

    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts['cookiefile'] = COOKIES_FILE
        logger.info("Using cookies.txt for authentication.")
    else:
        browser = os.getenv('COOKIES_FROM_BROWSER', '').strip().lower()
        if browser:
            # yt-dlp expects a tuple: (browser_name, profile, keyring, container)
            # Only pass the browser name; the rest default to None.
            opts['cookiesfrombrowser'] = (browser, None, None, None)
            logger.info("Extracting cookies from browser: %s", browser)
        else:
            logger.warning(
                "No cookies configured. "
                "Set cookies.txt or COOKIES_FROM_BROWSER in .env to fix age-restricted / bot-blocked videos."
            )

    return opts


def get_ydl_opts(base_opts: dict) -> dict:
    """Build a full yt-dlp options dict with anti-bot headers and cookies."""
    opts = base_opts.copy()

    # ── Realistic browser headers ─────────────────────────────────────────
    opts['http_headers'] = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Sec-Fetch-Mode':  'navigate',
    }

    # ── PO Token (optional — prevents YouTube consent screens) ───────────
    po_token = os.getenv('PO_TOKEN', '').strip()
    if po_token:
        opts.setdefault('extractor_args', {})
        opts['extractor_args']['youtube'] = {'po_token': [po_token]}

    # ── Slow down requests to avoid triggering rate-limits ───────────────
    opts.setdefault('sleep_interval_requests', 1)
    opts.setdefault('sleep_interval',          0)

    # ── Misc robustness flags ─────────────────────────────────────────────
    opts.setdefault('nocheckcertificate', True)
    opts.setdefault('ignoreerrors',       False)

    # ── Apply cookies last so they override nothing ───────────────────────
    opts = _apply_cookies(opts)

    return opts


# ── Cleanup helper ────────────────────────────────────────────────────────────
def _find_actual_file(base_path: str, search_dir: str) -> str | None:
    """
    yt-dlp may change the extension after merging (e.g. .mkv instead of .mp4).
    Walk through common containers, then glob the directory.
    """
    for ext in ('mp4', 'mkv', 'webm', 'm4a', '3gp', 'ogg', 'opus'):
        candidate = f"{base_path}.{ext}"
        if os.path.exists(candidate):
            return candidate

    # Glob fallback: any file in the request-specific dir
    candidates = [f for f in glob.glob(os.path.join(search_dir, '*')) if os.path.isfile(f)]
    if candidates:
        return max(candidates, key=os.path.getsize)

    return None


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/extract', methods=['POST'])
@limiter.limit("10 per minute")
def extract_video():
    """Extract video metadata and available formats."""
    try:
        data = request.get_json(silent=True) or {}
        video_url = (data.get('url') or '').strip()

        if not video_url:
            return jsonify({'error': 'Please provide a valid video URL.'}), 400

        ydl_opts = get_ydl_opts({
            'format':        'best',
            'quiet':         True,
            'no_warnings':   True,
            'skip_download': True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(video_url, download=False)
            except yt_dlp.utils.DownloadError as exc:
                msg = str(exc)
                logger.error("extract_info failed: %s", msg)

                # ── Friendly cookie / auth error messages ─────────────────
                if any(k in msg for k in ('Sign in', 'cookies', 'bot', 'login', 'Login', 'age')):
                    return jsonify({
                        'error': (
                            'YouTube is requiring sign-in or bot-verification for this video. '
                            'Fix: Add a cookies.txt file to the project root OR set '
                            'COOKIES_FROM_BROWSER=chrome (or firefox/edge) in your .env file. '
                            'See README for step-by-step instructions.'
                        )
                    }), 403

                if 'Private video' in msg:
                    return jsonify({'error': 'This video is private.'}), 403

                if 'unavailable' in msg.lower():
                    return jsonify({'error': 'This video is unavailable or has been removed.'}), 404

                return jsonify({'error': f'Could not fetch video info: {msg[:200]}'}), 500

        extractor = (info.get('extractor_key') or info.get('extractor') or '').lower()

        encoded_url = urllib.parse.quote(video_url, safe='')

        # ── Build format list ─────────────────────────────────────────────
        formats = []

        # 1. Server-side merge option (best quality, works on all platforms)
        formats.append({
            'format_id':  'bestvideo+bestaudio/best',
            'resolution': '1080p / 4K (Best Quality)',
            'ext':        'mp4',
            'url':        f'/api/download?url={encoded_url}&format_id=bestvideo%2Bbestaudio%2Fbest',
            'is_direct':  False,
            'filesize':   0,
            'badge':      'High Quality',
        })

        # 2. Direct combined streams (no server merging needed)
        direct_formats = []
        for fmt in info.get('formats', []):
            vcodec = fmt.get('vcodec', 'none') or 'none'
            acodec = fmt.get('acodec', 'none') or 'none'
            has_video = vcodec != 'none'
            has_audio = acodec != 'none'

            if not (has_video and has_audio):
                continue

            resolution = (
                fmt.get('resolution')
                or (f"{fmt.get('height')}p" if fmt.get('height') else None)
                or 'Unknown'
            )

            if 'audio only' in resolution.lower():
                continue

            stream_url = fmt.get('url')
            if not stream_url:
                continue

            direct_formats.append({
                'format_id':  fmt.get('format_id', ''),
                'resolution': f"{resolution} (Fast Direct)",
                'ext':        fmt.get('ext', 'mp4'),
                'url':        stream_url,
                'is_direct':  True,
                'filesize':   fmt.get('filesize') or fmt.get('filesize_approx') or 0,
                'badge':      'Direct',
                '_height':    fmt.get('height') or 0,
            })

        # Sort by height descending, keep top 4
        direct_formats.sort(key=lambda f: f['_height'], reverse=True)
        for f in direct_formats[:4]:
            f.pop('_height', None)
            formats.append(f)

        return jsonify({
            'title':        info.get('title', 'Unknown Video'),
            'author':       info.get('uploader', 'Unknown Creator'),
            'thumbnail':    info.get('thumbnail', ''),
            'duration':     info.get('duration', 0),
            'source':       info.get('extractor_key', 'Unknown'),
            'download_url': info.get('url', ''),
            'formats':      formats,
        })

    except Exception as exc:
        logger.exception("Unhandled error in /api/extract")
        msg = str(exc)
        if 'Incomplete YouTube ID' in msg or 'not a valid URL' in msg:
            return jsonify({'error': 'Invalid URL format. Please check the link and try again.'}), 400
        return jsonify({'error': f'Unexpected error: {msg[:150]}'}), 500


@app.route('/api/download', methods=['GET'])
@limiter.limit("5 per minute")
def download_video():
    """Download, optionally merge, and serve a video file."""
    video_url = (request.args.get('url') or '').strip()
    format_id = (request.args.get('format_id') or 'bestvideo+bestaudio/best').strip()

    if not video_url:
        return jsonify({'error': 'Missing url parameter.'}), 400

    # Unique per-request directory to prevent filename collisions
    req_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(req_dir, exist_ok=True)

    try:
        ydl_opts = get_ydl_opts({
            'format':              format_id,
            'outtmpl':             os.path.join(req_dir, '%(title)s.%(ext)s'),
            'quiet':               True,
            'no_warnings':         True,
            # Merge audio+video into mp4 when ffmpeg is available
            'merge_output_format': 'mp4',
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(video_url, download=True)
            except yt_dlp.utils.DownloadError as exc:
                shutil.rmtree(req_dir, ignore_errors=True)
                msg = str(exc)
                logger.error("download failed: %s", msg)

                if any(k in msg for k in ('Sign in', 'cookies', 'bot', 'login', 'Login', 'age')):
                    return jsonify({
                        'error': (
                            'YouTube requires authentication for this video. '
                            'Please add a cookies.txt file to the project root.'
                        )
                    }), 403

                return jsonify({'error': f'Download failed: {msg[:200]}'}), 500

            # Resolve the actual output path
            template_path = ydl.prepare_filename(info)
            base, _ = os.path.splitext(template_path)

        actual_file = _find_actual_file(base, req_dir)

        if not actual_file or not os.path.exists(actual_file):
            shutil.rmtree(req_dir, ignore_errors=True)
            return jsonify({'error': 'Downloaded file not found on server.'}), 404

        # Schedule cleanup of the request dir after FILE_TTL seconds
        def _cleanup():
            time.sleep(FILE_TTL + 30)
            shutil.rmtree(req_dir, ignore_errors=True)
        threading.Thread(target=_cleanup, daemon=True).start()

        return send_file(
            actual_file,
            as_attachment=True,
            download_name=os.path.basename(actual_file),
        )

    except Exception as exc:
        logger.exception("Unhandled error in /api/download")
        shutil.rmtree(req_dir, ignore_errors=True)
        return jsonify({'error': f'Server error: {str(exc)[:150]}'}), 500


# ── Error handlers ────────────────────────────────────────────────────────────
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({'error': 'Too many requests. Please slow down and try again later.'}), 429


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found.'}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error.'}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    logger.info("Starting server on port %d (debug=%s)", port, debug)
    app.run(debug=debug, port=port)
