import os
import re
import json
import time
import logging
import hashlib
import requests
import uuid
import threading
import tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from functools import wraps, lru_cache
import whisper

app = Flask(__name__)
# Configure CORS to allow requests from specific origins
CORS(app, resources={r"/*": {"origins": ["https://g-bus.vercel.app", "http://localhost:3000"]}})

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "audio/")
CACHE_EXPIRY = 86400  # 24 hours (in seconds)
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB max file size

# Create directories if they don't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiktok_transcriber')

# Cache storage
video_cache = {}  # For TikTok video info
transcript_cache = {}  # For transcriptions
cache_lock = threading.Lock()

# Load Whisper model - using a smaller model for faster performance on Render
# Options: "tiny", "base", "small", "medium", "large"
logger.info("Loading Whisper model...")
model = whisper.load_model("tiny")
logger.info("Whisper model loaded")

# Helper function for logging
def log_message(message):
    if isinstance(message, (dict, list, tuple)):
        logger.info(json.dumps(message))
    else:
        logger.info(message)

# Cache functions for TikTok videos
def get_from_cache(cache_dict, key):
    with cache_lock:
        if key in cache_dict and cache_dict[key]['expires'] > time.time():
            log_message(f'Cache hit for key: {key}')
            return cache_dict[key]['data']
    return None

def set_in_cache(cache_dict, key, data, expiration=CACHE_EXPIRY):
    with cache_lock:
        cache_dict[key] = {
            'data': data,
            'expires': time.time() + expiration
        }
    log_message(f'Cache set for key: {key}')
    return True

# Extract TikTok video ID from URL
def extract_tiktok_id(url):
    # Normalize URL
    normalized_url = url
    normalized_url = normalized_url.replace('m.tiktok.com', 'www.tiktok.com')
    normalized_url = normalized_url.replace('vm.tiktok.com', 'www.tiktok.com')
    
    # Regular expressions to match different TikTok URL formats
    patterns = [
        r'tiktok\.com\/@[\w\.]+\/video\/(\d+)',  # Standard format
        r'tiktok\.com\/t\/(\w+)',                # Short URL format
        r'v[mt]\.tiktok\.com\/(\w+)',            # Very short URL format
        r'tiktok\.com\/.*[?&]item_id=(\d+)',     # Query parameter format
    ]
    
    # First try with normalized URL
    for pattern in patterns:
        match = re.search(pattern, normalized_url)
        if match:
            return match.group(1)
    
    # For short URLs - follow redirect
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url or len(url.split('/')[3:]) < 2:
        return 'follow_redirect'
    
    return None

# Follow redirects to get final URL
def follow_tiktok_redirects(url):
    log_message(f'Following redirects for: {url}')
    
    try:
        response = requests.head(url, allow_redirects=True, 
                               headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'},
                               timeout=10)
        final_url = response.url
        log_message(f'Redirect resolved to: {final_url}')
        return final_url
    except Exception as e:
        log_message(f'Error following redirect: {str(e)}')
        return url

# Try to get TikTok video using TikWM API
def fetch_from_tikwm(url):
    log_message(f'Trying TikWM API service for: {url}')
    
    api_url = 'https://www.tikwm.com/api/'
    
    try:
        response = requests.post(
            api_url,
            data={
                'url': url,
                'hd': 1
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message(f'Error: TikWM API request failed with status: {response.status_code}')
            return None
        
        data = response.json()
        
        if not data.get('data') or data.get('code') != 0:
            log_message(f'TikWM API returned error: {data}')
            return None
        
        video_data = data['data']
        
        return {
            'video_url': video_data['play'],
            'cover_url': video_data['cover'],
            'author': video_data['author']['unique_id'],
            'desc': video_data['title'],
            'video_id': video_data['id'],
            'likes': video_data.get('digg_count', 0),
            'comments': video_data.get('comment_count', 0),
            'shares': video_data.get('share_count', 0),
            'plays': video_data.get('play_count', 0),
            'method': 'tikwm'
        }
    except Exception as e:
        log_message(f'Error using TikWM API: {str(e)}')
        return None

# Try to get TikTok video using SSSTik API
def fetch_from_ssstik(url):
    log_message(f'Trying SSSTik API service for: {url}')
    
    session = requests.Session()
    
    try:
        # First request to get cookies and token
        response = session.get(
            'https://ssstik.io/en',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to access SSSTik service')
            return None
        
        html = response.text
        
        # Extract the tt token
        tt_match = re.search(r'name="tt"[\s]+value="([^"]+)"', html)
        if not tt_match:
            log_message('Failed to extract token from SSSTik')
            return None
        
        tt_token = tt_match.group(1)
        
        # Make the API request
        response = session.post(
            'https://ssstik.io/abc?url=dl',
            data={
                'id': url,
                'locale': 'en',
                'tt': tt_token
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Origin': 'https://ssstik.io',
                'Referer': 'https://ssstik.io/en',
                'X-Requested-With': 'XMLHttpRequest'
            },
            timeout=30
        )
        
        if response.status_code != 200:
            log_message('Failed to get a response from SSSTik API')
            return None
        
        response_text = response.text
        
        # Parse the HTML response to extract the download link
        video_match = re.search(r'<a href="([^"]+)"[^>]+>Download server 1', response_text)
        if not video_match:
            log_message('Failed to extract download link from SSSTik response')
            return None
        
        video_url = video_match.group(1).replace('&amp;', '&')
        
        # Extract username if available
        author = 'Unknown'
        user_match = re.search(r'<div class="maintext">@([^<]+)</div>', response_text)
        if user_match:
            author = user_match.group(1)
        
        # Extract description/title if available
        desc = ''
        desc_match = re.search(r'<p[^>]+class="maintext">([^<]+)</p>', response_text)
        if desc_match:
            desc = desc_match.group(1)
        
        return {
            'video_url': video_url,
            'author': author,
            'desc': desc,
            'video_id': hashlib.md5(url.encode()).hexdigest(),
            'cover_url': '',
            'likes': 0,
            'comments': 0,
            'shares': 0,
            'plays': 0,
            'method': 'ssstik'
        }
    except Exception as e:
        log_message(f'Error using SSSTik service: {str(e)}')
        return None

# Functions for file handling
def sanitize_filename(name):
    """Remove any path info and sanitize the file name"""
    name = os.path.basename(name)
    name = name.replace(' ', '_')
    name = re.sub(r'[^A-Za-z0-9_\-\.]', '', name)
    return name

def generate_unique_filename(original_name):
    """Generate a unique filename based on the original name"""
    filename, extension = os.path.splitext(original_name)
    unique_id = uuid.uuid4().hex[:10]
    return f"{sanitize_filename(filename)}_{unique_id}{extension}"

def cleanup_expired_files():
    """Remove files that haven't been accessed for CACHE_EXPIRY seconds"""
    current_time = time.time()
    with cache_lock:
        # Clean video cache
        expired_keys = [k for k, v in video_cache.items() if current_time > v["expires"]]
        for key in expired_keys:
            if 'file_path' in video_cache[key]['data']:
                try:
                    file_path = video_cache[key]['data']['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Removed expired video file: {file_path}")
                except Exception as e:
                    logger.error(f"Error removing file {file_path}: {str(e)}")
            del video_cache[key]
        
        # Clean transcript cache
        expired_keys = [k for k, v in transcript_cache.items() if current_time > v["expires"]]
        for key in expired_keys:
            del transcript_cache[key]

def start_cleanup_thread():
    """Start a background thread to periodically clean up expired files"""
    def cleanup_task():
        while True:
            cleanup_expired_files()
            time.sleep(300)  # Run every 5 minutes

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

def download_tiktok_video(url):
    """Download a TikTok video and return the local file path"""
    # Normalize URL for short links
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url or len(url.split('/')[3:]) < 2:
        url = follow_tiktok_redirects(url)
    
    # Try to get video info
    result = fetch_from_tikwm(url)
    if not result:
        result = fetch_from_ssstik(url)
    
    if not result or not result.get('video_url'):
        raise Exception("Failed to extract video URL from TikTok link")
    
    video_url = result['video_url']
    author = result['author']
    
    # Generate a nice filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video
    log_message(f"Downloading video from {video_url} to {file_path}")
    response = requests.get(video_url, stream=True, timeout=30)
    
    if response.status_code != 200:
        raise Exception(f"Failed to download video: HTTP {response.status_code}")
    
    with open(file_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    
    return {
        'file_path': file_path,
        'author': author,
        'desc': result['desc'],
        'video_id': result['video_id'],
        'filename': filename
    }

def transcribe_audio_file(file_path):
    """Transcribe audio using Whisper directly from a video file"""
    log_message(f"Transcribing directly from file: {file_path}")
    
    try:
        # Transcribe with Whisper - directly from the video file
        # Whisper can handle both audio and video files
        result = model.transcribe(file_path)
        
        # Create a segments array with timestamps
        segments = []
        for segment in result.get("segments", []):
            segments.append({
                "start": segment.get("start", 0),
                "end": segment.get("end", 0),
                "text": segment.get("text", "")
            })
        
        transcription = {
            "text": result["text"],
            "segments": segments,
            "language": result.get("language", "")
        }
        
        return transcription
    
    except Exception as e:
        log_message(f"Transcription error: {str(e)}")
        raise Exception(f"Failed to transcribe file: {str(e)}")

# Root route handler
@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'status': 'running', 
        'message': 'TikTok Transcription API is running',
        'endpoints': {
            '/api/transcribe': 'POST - Transcribe TikTok video from URL',
            '/status': 'GET - Check service status',
            '/healthz': 'GET - Simple health check'
        }
    })

# Routes
@app.route('/api/transcribe', methods=['POST'])
def transcribe_tiktok():
    """Endpoint that takes a TikTok URL and returns a transcription"""
    if not request.is_json:
        return jsonify({'success': False, 'error': 'Request must be in JSON format'}), 400
    
    try:
        data = request.get_json()
        
        if not data or not data.get('url'):
            return jsonify({'success': False, 'error': 'TikTok URL is required'}), 400
        
        tiktok_url = data['url'].strip()
        url_hash = hashlib.md5(tiktok_url.encode()).hexdigest()
        
        # Check for cached result
        cached_result = get_from_cache(transcript_cache, url_hash)
        if cached_result:
            return jsonify(cached_result)
        
        try:
            # 1. Download the TikTok video
            log_message(f"Starting download for URL: {tiktok_url}")
            video_info = download_tiktok_video(tiktok_url)
            video_path = video_info['file_path']
            log_message(f"Download complete: {video_path}")
            
            # 2. Transcribe directly from the video file
            # We skip the audio extraction step that used to use FFmpeg
            log_message("Starting transcription")
            transcription = transcribe_audio_file(video_path)
            log_message("Transcription complete")
            
            # 3. Create result
            result = {
                'success': True,
                'transcription': transcription['text'],
                'segments': transcription['segments'],
                'language': transcription['language'],
                'author': video_info['author'],
                'title': video_info['desc'],
                'video_id': video_info['video_id'],
                'cached': False
            }
            
            # 4. Add to cache
            set_in_cache(transcript_cache, url_hash, result)
            
            # 5. Keep the video file for the cache period
            video_info['file_path'] = video_path
            set_in_cache(video_cache, url_hash, video_info)
            
            return jsonify(result)
        
        except Exception as e:
            log_message(f"Error processing TikTok transcription: {str(e)}")
            import traceback
            error_details = f"{str(e)}\n{traceback.format_exc()}"
            return jsonify({'success': False, 'error': str(e)}), 500
    
    except Exception as e:
        log_message(f"Unexpected error in request handling: {str(e)}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/status', methods=['GET'])
def status():
    """Status endpoint for health checks"""
    with cache_lock:
        video_cache_count = len(video_cache)
        transcript_cache_count = len(transcript_cache)
    
    return jsonify({
        'status': 'running',
        'whisper_model': 'tiny',
        'video_cache_count': video_cache_count,
        'transcript_cache_count': transcript_cache_count,
        'cache_expiry_seconds': CACHE_EXPIRY
    })

@app.route('/healthz', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        'status': 'ok',
        'message': 'Service is healthy'
    })

@app.route('/clear-cache', methods=['POST'])
def clear_cache():
    """Admin endpoint to manually clear all caches"""
    try:
        with cache_lock:
            global video_cache, transcript_cache
            
            # Remove video files
            for key, data in video_cache.items():
                try:
                    file_path = data['data'].get('file_path')
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as e:
                    log_message(f"Error removing file: {str(e)}")
            
            video_cache = {}
            transcript_cache = {}
        
        return jsonify({'success': True, 'message': 'All caches cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Manual transcription endpoint (file upload fallback)
@app.route('/api/transcribe-file', methods=['POST'])
def transcribe_file():
    """Endpoint that takes an audio file and returns a transcription"""
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    # Create a temporary file to save the uploaded file
    temp_file = tempfile.NamedTemporaryFile(delete=False)
    file.save(temp_file.name)
    temp_file.close()
    
    try:
        # Transcribe the file using Whisper
        result = model.transcribe(temp_file.name)
        
        # Create segments array
        segments = []
        for segment in result.get("segments", []):
            segments.append({
                "start": segment.get("start", 0),
                "end": segment.get("end", 0),
                "text": segment.get("text", "")
            })
        
        # Clean up the temporary file
        os.unlink(temp_file.name)
        
        return jsonify({
            "success": True,
            "transcription": result["text"],
            "segments": segments,
            "language": result.get("language", "")
        })
    
    except Exception as e:
        # Clean up the temporary file in case of error
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

if __name__ == '__main__':
    # Start the cleanup thread
    start_cleanup_thread()
    logger.info("Started cache cleanup thread")
    
    # Start the Flask app
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting TikTok Transcription API on port {port}...")
    print("Available endpoints:")
    print("  - POST /api/transcribe - Transcribe TikTok video from URL")
    print("  - POST /api/transcribe-file - Transcribe uploaded audio file")
    print("  - GET /status - Check service status")
    print("  - GET /healthz - Simple health check")
    app.run(host='0.0.0.0', port=port, debug=False)
