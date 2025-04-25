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
from flask import Flask, request, jsonify
from flask_cors import CORS
import whisper

app = Flask(__name__)
# Configure CORS to allow requests from specific origins
CORS(app, resources={r"/*": {"origins": ["https://g-bus.vercel.app", "http://localhost:3000"]}})

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
CACHE_EXPIRY = 3600  # 1 hour (reduced from 24 hours)
MAX_CACHE_ITEMS = 20  # Limit cache size
REQUEST_TIMEOUT = 15  # Reduced timeout

# Create directory if it doesn't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Configure minimal logging
logging.basicConfig(level=logging.WARNING,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiktok_transcriber')

# Cache storage with LRU mechanism
video_cache = {}  # For TikTok video info
transcript_cache = {}  # For transcriptions
cache_lock = threading.Lock()

# Load the smallest Whisper model
logger.info("Loading Whisper model...")
model = whisper.load_model("tiny.en")  # Using tiny.en for English-only to reduce memory
logger.info("Whisper model loaded")

# Cache functions
def get_from_cache(cache_dict, key):
    with cache_lock:
        if key in cache_dict and cache_dict[key]['expires'] > time.time():
            return cache_dict[key]['data']
    return None

def set_in_cache(cache_dict, key, data, expiration=CACHE_EXPIRY):
    with cache_lock:
        # Enforce cache size limit - remove oldest items if needed
        if len(cache_dict) >= MAX_CACHE_ITEMS:
            # Find and remove oldest items
            oldest_key = min(cache_dict.keys(), key=lambda k: cache_dict[k]['expires'])
            if 'file_path' in cache_dict[oldest_key]['data']:
                try:
                    file_path = cache_dict[oldest_key]['data']['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
            del cache_dict[oldest_key]
            
        cache_dict[key] = {
            'data': data,
            'expires': time.time() + expiration
        }
    return True

# Extract TikTok video ID from URL
def extract_tiktok_id(url):
    # Normalize URL
    normalized_url = url.replace('m.tiktok.com', 'www.tiktok.com').replace('vm.tiktok.com', 'www.tiktok.com')
    
    # Regular expressions to match different TikTok URL formats
    patterns = [
        r'tiktok\.com\/@[\w\.]+\/video\/(\d+)',  # Standard format
        r'tiktok\.com\/t\/(\w+)',                # Short URL format
        r'v[mt]\.tiktok\.com\/(\w+)',            # Very short URL format
        r'tiktok\.com\/.*[?&]item_id=(\d+)',     # Query parameter format
    ]
    
    # Try with normalized URL
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
    try:
        response = requests.head(url, allow_redirects=True, 
                               headers={'User-Agent': 'Mozilla/5.0'}, # Simplified user agent
                               timeout=REQUEST_TIMEOUT)
        return response.url
    except Exception as e:
        logger.warning(f'Error following redirect: {str(e)}')
        return url

# Try to get TikTok video using TikWM API (primary method)
def fetch_from_tikwm(url):    
    api_url = 'https://www.tikwm.com/api/'
    
    try:
        response = requests.post(
            api_url,
            data={'url': url, 'hd': 0},  # Use lower quality to save bandwidth
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=REQUEST_TIMEOUT
        )
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        
        if not data.get('data') or data.get('code') != 0:
            return None
        
        video_data = data['data']
        
        return {
            'video_url': video_data['play'],
            'author': video_data['author']['unique_id'],
            'desc': video_data['title'],
            'video_id': video_data['id'],
            'method': 'tikwm'
        }
    except Exception as e:
        logger.warning(f'Error using TikWM API: {str(e)}')
        return None

# Functions for file handling
def sanitize_filename(name):
    """Remove any path info and sanitize the file name"""
    name = os.path.basename(name)
    name = name.replace(' ', '_')
    name = re.sub(r'[^A-Za-z0-9_\-\.]', '', name)
    return name

def download_tiktok_video(url):
    """Download a TikTok video and return the local file path"""
    # Normalize URL for short links
    if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url or len(url.split('/')[3:]) < 2:
        url = follow_tiktok_redirects(url)
    
    # Try to get video info
    result = fetch_from_tikwm(url)
    
    if not result or not result.get('video_url'):
        raise Exception("Failed to extract video URL from TikTok link")
    
    video_url = result['video_url']
    author = result['author']
    
    # Generate a filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video with streaming to minimize memory usage
    response = requests.get(video_url, stream=True, timeout=REQUEST_TIMEOUT)
    
    if response.status_code != 200:
        raise Exception(f"Failed to download video: HTTP {response.status_code}")
    
    # Use smaller chunk size and direct file writing to reduce memory usage
    with open(file_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=4096):  # Smaller chunks
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
    """Transcribe audio using Whisper directly from a video file with optimized settings"""
    try:
        # Use more efficient Whisper options
        result = model.transcribe(
            file_path,
            fp16=False,  # Avoid GPU requirements
            language='en',  # Specify language if known
            task='transcribe',
            verbose=False
        )
        
        # Create a simplified segments array with timestamps
        segments = [
            {
                "start": segment.get("start", 0),
                "end": segment.get("end", 0),
                "text": segment.get("text", "")
            }
            for segment in result.get("segments", [])
        ]
        
        return {
            "text": result["text"],
            "segments": segments,
            "language": result.get("language", "")
        }
    
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        raise Exception(f"Failed to transcribe file: {str(e)}")

def cleanup_expired_files():
    """Remove files that haven't been accessed for CACHE_EXPIRY seconds"""
    current_time = time.time()
    with cache_lock:
        # Clean video cache
        expired_keys = [k for k, v in list(video_cache.items()) if current_time > v["expires"]]
        for key in expired_keys:
            if 'file_path' in video_cache[key]['data']:
                try:
                    file_path = video_cache[key]['data']['file_path']
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
            del video_cache[key]
        
        # Clean transcript cache
        expired_keys = [k for k, v in list(transcript_cache.items()) if current_time > v["expires"]]
        for key in expired_keys:
            del transcript_cache[key]

def start_cleanup_thread():
    """Start a background thread to periodically clean up expired files"""
    def cleanup_task():
        while True:
            try:
                cleanup_expired_files()
            except Exception:
                pass  # Don't let cleanup errors crash the thread
            time.sleep(600)  # Run every 10 minutes (increased from 5)

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

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
            video_info = download_tiktok_video(tiktok_url)
            video_path = video_info['file_path']
            
            # 2. Transcribe directly from the video file
            transcription = transcribe_audio_file(video_path)
            
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
            return jsonify({'success': False, 'error': str(e)}), 500
    
    except Exception as e:
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/status', methods=['GET'])
def status():
    """Status endpoint for health checks"""
    with cache_lock:
        video_cache_count = len(video_cache)
        transcript_cache_count = len(transcript_cache)
    
    return jsonify({
        'status': 'running',
        'whisper_model': 'tiny.en',
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
            for key, data in list(video_cache.items()):
                try:
                    file_path = data['data'].get('file_path')
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
            
            video_cache = {}
            transcript_cache = {}
        
        return jsonify({'success': True, 'message': 'All caches cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    # Start the cleanup thread
    start_cleanup_thread()
    
    # Start the Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
