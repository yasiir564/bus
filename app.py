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
import gc
import resource
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from functools import wraps, lru_cache
import whisper

# Set resource limits
# Limit virtual memory to 1.5GB (adjust as needed for your server)
resource.setrlimit(resource.RLIMIT_AS, (int(1.5 * 1024 * 1024 * 1024), -1))

app = Flask(__name__)
# Configure CORS to allow requests from specific origins
CORS(app, resources={r"/*": {"origins": ["https://g-bus.vercel.app", "http://localhost:3000"]}})

# Configuration
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(CURRENT_DIR, "downloads/")
CACHE_DIR = os.path.join(CURRENT_DIR, "cache/")
CACHE_EXPIRY = 86400  # 24 hours (in seconds)
MAX_FILE_SIZE = 50 * 1024 * 1024  # Reduced to 50MB max file size
MAX_REQUEST_QUEUE = 3  # Maximum number of concurrent processing requests
REQUEST_TIMEOUT = 60  # Timeout for external API requests in seconds
WHISPER_MODEL_SIZE = "tiny"  # Use smallest model for memory efficiency

# Create directories if they don't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tiktok_transcriber')

# Request queue with semaphore for concurrency control
request_semaphore = threading.Semaphore(MAX_REQUEST_QUEUE)

# Whisper model loading - only when needed, not at startup
whisper_model = None
model_lock = threading.Lock()

def get_whisper_model():
    """Lazy loading of Whisper model to save memory"""
    global whisper_model
    with model_lock:
        if whisper_model is None:
            logger.info(f"Loading Whisper {WHISPER_MODEL_SIZE} model...")
            whisper_model = whisper.load_model(WHISPER_MODEL_SIZE)
            logger.info("Whisper model loaded")
    return whisper_model

# Helper function for logging
def log_message(message):
    if isinstance(message, (dict, list, tuple)):
        logger.info(json.dumps(message))
    else:
        logger.info(message)

# Disk-based caching functions
def get_cache_filename(key):
    """Generate a filename for the cache entry"""
    return os.path.join(CACHE_DIR, f"{key}.json")

def get_from_cache(key):
    """Get data from disk cache"""
    cache_file = get_cache_filename(key)
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                cache_entry = json.load(f)
                
            if cache_entry.get('expires', 0) > time.time():
                log_message(f'Cache hit for key: {key}')
                return cache_entry.get('data')
            else:
                # Clean up expired cache entry
                try:
                    os.remove(cache_file)
                except:
                    pass
        except Exception as e:
            log_message(f"Error reading cache: {str(e)}")
    
    return None

def set_in_cache(key, data, expiration=CACHE_EXPIRY):
    """Store data in disk cache"""
    cache_file = get_cache_filename(key)
    
    try:
        with open(cache_file, 'w') as f:
            json.dump({
                'data': data,
                'expires': time.time() + expiration
            }, f)
        log_message(f'Cache set for key: {key}')
        return True
    except Exception as e:
        log_message(f"Error writing cache: {str(e)}")
        return False

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
        response = requests.head(
            url, 
            allow_redirects=True, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'},
            timeout=REQUEST_TIMEOUT
        )
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
                'hd': 0  # Use lower quality to save bandwidth
            },
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            },
            timeout=REQUEST_TIMEOUT
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
            timeout=REQUEST_TIMEOUT
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
            timeout=REQUEST_TIMEOUT
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
    unique_id = uuid.uuid4().hex[:8]
    return f"{sanitize_filename(filename)}_{unique_id}{extension}"

def cleanup_files(older_than=3600):
    """Clean up files older than the specified time"""
    now = time.time()
    try:
        # Clean uploads directory
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > older_than:
                try:
                    os.remove(file_path)
                    logger.info(f"Removed old file: {file_path}")
                except Exception as e:
                    logger.error(f"Error removing file {file_path}: {str(e)}")
        
        # Clean expired cache entries
        for filename in os.listdir(CACHE_DIR):
            if not filename.endswith('.json'):
                continue
                
            file_path = os.path.join(CACHE_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    with open(file_path, 'r') as f:
                        cache_entry = json.load(f)
                    
                    if cache_entry.get('expires', 0) < now:
                        os.remove(file_path)
                        logger.info(f"Removed expired cache: {file_path}")
                except Exception as e:
                    # If we can't read it, it's probably corrupt
                    try:
                        os.remove(file_path)
                        logger.info(f"Removed corrupt cache: {file_path}")
                    except:
                        pass
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")

def start_cleanup_thread():
    """Start a background thread to periodically clean up expired files"""
    def cleanup_task():
        while True:
            cleanup_files()
            # Force garbage collection to free memory
            gc.collect()
            time.sleep(300)  # Run every 5 minutes

    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

def download_tiktok_video(url):
    """Download a TikTok video and return the local file path with streaming"""
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
    
    # Generate a filename based on the author and a unique ID
    filename = f"{sanitize_filename(author)}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Download the video with streaming to reduce memory usage
    log_message(f"Downloading video from {video_url}")
    
    total_size = 0
    try:
        with requests.get(video_url, stream=True, timeout=REQUEST_TIMEOUT) as response:
            if response.status_code != 200:
                raise Exception(f"Failed to download video: HTTP {response.status_code}")
            
            # Check content length if available
            content_length = response.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_FILE_SIZE:
                raise Exception(f"Video file too large: {int(content_length) // (1024*1024)}MB (max: {MAX_FILE_SIZE // (1024*1024)}MB)")
            
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        size = len(chunk)
                        total_size += size
                        if total_size > MAX_FILE_SIZE:
                            # Close and delete the partial file
                            f.close()
                            os.remove(file_path)
                            raise Exception(f"Video file too large: >{MAX_FILE_SIZE // (1024*1024)}MB")
                        f.write(chunk)
        
        return {
            'file_path': file_path,
            'author': author,
            'desc': result['desc'],
            'video_id': result['video_id'],
            'filename': filename,
            'size': total_size
        }
    except Exception as e:
        # Clean up partial file if download failed
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        raise e

def transcribe_audio_file(file_path):
    """Transcribe audio using Whisper directly from a video file"""
    log_message(f"Transcribing directly from file: {file_path}")
    
    try:
        # Get model instance
        model = get_whisper_model()
        
        # Use memory-optimized options
        result = model.transcribe(
            file_path,
            # Use fp16 precision for memory efficiency
            fp16=False,  
            # Disable language detection to save memory
            language="en"
        )
        
        # Create a segments array with timestamps
        segments = []
        for segment in result.get("segments", []):
            segments.append({
                "start": segment.get("start", 0),
                "end": segment.get("end", 0),
                "text": segment.get("text", "")
            })
        
        return {
            "text": result["text"],
            "segments": segments,
            "language": result.get("language", "en")
        }
    
    except Exception as e:
        log_message(f"Transcription error: {str(e)}")
        raise Exception(f"Failed to transcribe file: {str(e)}")
    finally:
        # Force garbage collection after transcription
        gc.collect()

# Routes
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

@app.route('/api/transcribe', methods=['POST'])
def transcribe_tiktok():
    """Endpoint that takes a TikTok URL and returns a transcription"""
    # Check if we've reached the maximum number of concurrent requests
    if not request_semaphore.acquire(blocking=False):
        return jsonify({
            'success': False, 
            'error': 'Server is busy. Please try again later.'
        }), 429
    
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Request must be in JSON format'}), 400
        
        data = request.get_json()
        
        if not data or not data.get('url'):
            return jsonify({'success': False, 'error': 'TikTok URL is required'}), 400
        
        tiktok_url = data['url'].strip()
        url_hash = hashlib.md5(tiktok_url.encode()).hexdigest()
        
        # Check for cached result
        cached_result = get_from_cache(url_hash)
        if cached_result:
            cached_result['cached'] = True
            return jsonify(cached_result)
        
        try:
            # 1. Download the TikTok video
            log_message(f"Starting download for URL: {tiktok_url}")
            video_info = download_tiktok_video(tiktok_url)
            video_path = video_info['file_path']
            log_message(f"Download complete: {video_path}")
            
            # 2. Transcribe directly from the video file
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
            set_in_cache(url_hash, result)
            
            # 5. Clean up video file
            try:
                os.remove(video_path)
                log_message(f"Removed video file: {video_path}")
            except Exception as e:
                log_message(f"Warning: Could not delete video file: {str(e)}")
            
            return jsonify(result)
        
        except Exception as e:
            log_message(f"Error processing TikTok transcription: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    
    except Exception as e:
        log_message(f"Unexpected error in request handling: {str(e)}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
    
    finally:
        # Release the semaphore to allow another request
        request_semaphore.release()
        # Force garbage collection
        gc.collect()

@app.route('/status', methods=['GET'])
def status():
    """Status endpoint for health checks"""
    # Get free memory info
    try:
        import psutil
        mem = psutil.virtual_memory()
        mem_info = {
            'total': mem.total / (1024 * 1024),
            'available': mem.available / (1024 * 1024),
            'percent': mem.percent
        }
    except:
        mem_info = {'error': 'psutil not available'}
    
    # Count cache files
    try:
        cache_count = len([f for f in os.listdir(CACHE_DIR) if f.endswith('.json')])
    except:
        cache_count = -1
    
    return jsonify({
        'status': 'running',
        'whisper_model': WHISPER_MODEL_SIZE,
        'model_loaded': whisper_model is not None,
        'memory': mem_info,
        'cache_count': cache_count,
        'available_threads': request_semaphore._value,
        'max_concurrency': MAX_REQUEST_QUEUE
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
        # Clean up all files
        for filename in os.listdir(CACHE_DIR):
            file_path = os.path.join(CACHE_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    log_message(f"Error removing cache file: {str(e)}")
        
        # Clean up all downloads
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    log_message(f"Error removing download file: {str(e)}")
        
        # Force garbage collection
        gc.collect()
        
        return jsonify({'success': True, 'message': 'All caches cleared successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/transcribe-file', methods=['POST'])
def transcribe_file():
    """Endpoint that takes an audio file and returns a transcription"""
    # Check if we've reached the maximum number of concurrent requests
    if not request_semaphore.acquire(blocking=False):
        return jsonify({
            'success': False, 
            'error': 'Server is busy. Please try again later.'
        }), 429
    
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        # Check file size before saving
        content = file.read(MAX_FILE_SIZE + 1)
        if len(content) > MAX_FILE_SIZE:
            return jsonify({
                "success": False,
                "error": f"File too large. Maximum size is {MAX_FILE_SIZE/(1024*1024)}MB"
            }), 413
        
        # Create a temporary file and write the content
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_file.write(content)
        temp_file.close()
        
        try:
            # Transcribe with Whisper
            transcription = transcribe_audio_file(temp_file.name)
            
            # Clean up the temporary file
            os.unlink(temp_file.name)
            
            return jsonify({
                "success": True,
                "transcription": transcription["text"],
                "segments": transcription["segments"],
                "language": transcription["language"]
            })
        
        except Exception as e:
            # Clean up the temporary file in case of error
            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)
            
            return jsonify({
                "success": False,
                "error": str(e)
            }), 500
    
    except Exception as e:
        log_message(f"Error in file upload handler: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500
    
    finally:
        # Release the semaphore to allow another request
        request_semaphore.release()
        # Force garbage collection
        gc.collect()

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
    
    # Memory-friendly server settings
    app.run(
        host='0.0.0.0', 
        port=port, 
        debug=False,
        threaded=True,
        processes=1  # Single process to avoid memory duplication
    )
