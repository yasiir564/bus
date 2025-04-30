from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import re
import os
import tempfile
import logging
import time
import random
import json
from urllib.parse import urlparse
import subprocess
import hashlib
from functools import lru_cache
import shutil
import threading
from datetime import datetime

app = Flask(__name__)
# Configure CORS to allow requests from any origin
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("tiktok_downloader.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_downloader')
os.makedirs(TEMP_DIR, exist_ok=True)

# Set cache size and expiration time (in seconds)
CACHE_SIZE = 200
CACHE_EXPIRATION = 86400  # 24 hours
MAX_CACHE_SIZE_MB = 5000  # 5GB maximum cache size

# List of user agents to rotate - expanded for better undetectability
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.80 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 312.0.0.0.41",
    "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36"
]

# Enhanced TikTok cookies with more variety
TT_COOKIES = [
    "tt_webid_v2=123456789012345678; tt_webid=123456789012345678; ttwid=1%7CAbC123dEf456gHi789jKl%7C1600000000%7Cabcdef0123456789abcdef0123456789; msToken=AbC123dEf456gHi789jKl; s_v_web_id=verify_12345678_abcdefgh_1234_5678_abcd_efghijklmnopqrst; odin_tt=abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789; passport_csrf_token=abcdef0123456789abcdef0123456789; passport_csrf_token_default=abcdef0123456789abcdef0123456789",
    "tt_webid_v2=234567890123456789; tt_webid=234567890123456789; ttwid=1%7CBcD234eFg567hIj890kLm%7C1600100000%7Cbcdefg1234567890abcdef0123456789; msToken=BcD234eFg567hIj890kLm; s_v_web_id=verify_23456789_bcdefghi_2345_6789_bcde_fghijklmnopqrstu; odin_tt=bcdefg1234567890bcdefg1234567890bcdefg1234567890bcdefg1234567890; passport_csrf_token=bcdefg1234567890bcdefg1234567890; passport_csrf_token_default=bcdefg1234567890bcdefg1234567890",
    "tt_webid_v2=345678901234567890; tt_webid=345678901234567890; ttwid=1%7CCdE345fGh678iJk901lMn%7C1600200000%7Ccdefgh2345678901abcdef0123456789; msToken=CdE345fGh678iJk901lMn; s_v_web_id=verify_34567890_cdefghij_3456_7890_cdef_ghijklmnopqrstuv; odin_tt=cdefgh2345678901cdefgh2345678901cdefgh2345678901cdefgh2345678901; passport_csrf_token=cdefgh2345678901cdefgh2345678901; passport_csrf_token_default=cdefgh2345678901cdefgh2345678901",
    "tt_webid_v2=456789012345678901; tt_webid=456789012345678901; ttwid=1%7CdEf456gHi789jKl012mNo%7C1600300000%7Cdefghi3456789012abcdef0123456789; msToken=dEf456gHi789jKl012mNo; s_v_web_id=verify_45678901_defghijk_4567_8901_defg_hijklmnopqrstuvw; odin_tt=defghi3456789012defghi3456789012defghi3456789012defghi3456789012; passport_csrf_token=defghi3456789012defghi3456789012; passport_csrf_token_default=defghi3456789012defghi3456789012",
    "tt_webid_v2=567890123456789012; tt_webid=567890123456789012; ttwid=1%7CEfG567hIj890kLm123nOp%7C1600400000%7Cefghij4567890123abcdef0123456789; msToken=EfG567hIj890kLm123nOp; s_v_web_id=verify_56789012_efghijkl_5678_9012_efgh_ijklmnopqrstuvwx; odin_tt=efghij4567890123efghij4567890123efghij4567890123efghij4567890123; passport_csrf_token=efghij4567890123efghij4567890123; passport_csrf_token_default=efghij4567890123efghij4567890123"
]

# Track active downloads
active_downloads = {}
active_downloads_lock = threading.Lock()

class DownloadStats:
    def __init__(self):
        self.total_downloads = 0
        self.successful_downloads = 0
        self.failed_downloads = 0
        self.last_download_time = None
        self.lock = threading.Lock()
        
    def increment_total(self):
        with self.lock:
            self.total_downloads += 1
            self.last_download_time = datetime.now()
            
    def increment_success(self):
        with self.lock:
            self.successful_downloads += 1
            
    def increment_failed(self):
        with self.lock:
            self.failed_downloads += 1
            
    def get_stats(self):
        with self.lock:
            return {
                "total": self.total_downloads,
                "successful": self.successful_downloads,
                "failed": self.failed_downloads,
                "last_download": self.last_download_time.isoformat() if self.last_download_time else None
            }

stats = DownloadStats()

def get_random_user_agent():
    """Get a random user agent from the list."""
    return random.choice(USER_AGENTS)

def get_random_cookies():
    """Get random cookies for TikTok requests."""
    return random.choice(TT_COOKIES)

def is_valid_tiktok_url(url):
    """Check if the URL is a valid TikTok URL."""
    parsed_url = urlparse(url)
    return parsed_url.netloc in ["www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com"]

def expand_shortened_url(url):
    """Expand a shortened TikTok URL."""
    try:
        headers = {"User-Agent": get_random_user_agent()}
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)
        return response.url
    except Exception as e:
        logger.error(f"Error expanding shortened URL: {e}")
        return url

def extract_video_id(url):
    """Extract the video ID from a TikTok URL."""
    # Handle shortened URLs
    if any(domain in url for domain in ["vm.tiktok.com", "vt.tiktok.com"]):
        url = expand_shortened_url(url)
    
    # Extract video ID from URL
    patterns = [
        r'/video/(\d+)',
        r'tiktok\.com\/@[\w.-]+/video/(\d+)',
        r'v/(\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def get_random_request_headers(referer=None):
    """Generate randomized headers for HTTP requests."""
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Pragma": "no-cache",
        "Cache-Control": "no-cache"
    }
    
    if referer:
        headers["Referer"] = referer
        
    # Always add cookies for better reliability
    headers["Cookie"] = get_random_cookies()
    
    return headers

def generate_cache_key(url):
    """Generate a unique cache key based on URL."""
    return hashlib.md5(url.encode()).hexdigest()

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_scraper(video_id):
    """Download TikTok video without watermark using the scraper method."""
    try:
        # Build the direct video URL
        url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
        headers = get_random_request_headers()
        
        # Add specific headers that help bypass restrictions
        headers.update({
            "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        })
        
        logger.info(f"Fetching TikTok page with scraper method: {url}")
        response = requests.get(
            url, 
            headers=headers, 
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok page. Status: {response.status_code}")
            return None
            
        # Try to find the video data in the page
        # Look for the __UNIVERSAL_DATA_FOR_REHYDRATION__ script
        universal_data_match = re.search(r'window\["UNIVERSAL_DATA_FOR_REHYDRATION"\]\s*=\s*({.+?});', response.text)
        if universal_data_match:
            try:
                universal_data_str = universal_data_match.group(1)
                universal_data = json.loads(universal_data_str)
                
                # Navigate through the structure to find video URL
                if "state" in universal_data and "ItemModule" in universal_data["state"]:
                    item_module = universal_data["state"]["ItemModule"]
                    if video_id in item_module:
                        video_data = item_module[video_id]["video"]
                        video_url = video_data.get("playAddr") or video_data.get("downloadAddr")
                        
                        if video_url:
                            logger.info(f"Found video URL via universal data: {video_url[:60]}...")
                            
                            # Download the video
                            video_headers = get_random_request_headers(referer=url)
                            video_response = requests.get(
                                video_url, 
                                headers=video_headers, 
                                stream=True, 
                                timeout=30
                            )
                            
                            if video_response.status_code != 200:
                                logger.error(f"Failed to download video. Status: {video_response.status_code}")
                                return None
                            
                            # Create a temporary file
                            temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                            
                            # Stream the video to the file
                            with open(temp_file, 'wb') as f:
                                for chunk in video_response.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                            
                            if os.path.getsize(temp_file) < 10000:
                                logger.error(f"Downloaded file is too small: {os.path.getsize(temp_file)} bytes")
                                os.remove(temp_file)
                                return None
                                
                            return temp_file
            except json.JSONDecodeError:
                logger.error("Failed to parse universal data JSON")
        
        # If universal data extraction failed, try regex method as fallback
        patterns = [
            r'"playAddr":"([^"]+)"',
            r'"downloadAddr":"([^"]+)"',
            r'"playUrl":"([^"]+)"',
            r'"contentUrl":"([^"]+)"',
            r'<video[^>]+src="([^"]+)"'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL via regex: {video_url[:60]}...")
                
                # Download the video
                video_headers = get_random_request_headers(referer=url)
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=30
                )
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video. Status: {video_response.status_code}")
                    continue
                
                # Create a temporary file
                temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file
                with open(temp_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(temp_file) < 10000:
                    logger.error(f"Downloaded file is too small: {os.path.getsize(temp_file)} bytes")
                    os.remove(temp_file)
                    continue
                    
                return temp_file
        
        return None
    except Exception as e:
        logger.error(f"Error in scraper method: {e}")
        return None

def get_tiktok_video(url):
    """Download TikTok video without watermark."""
    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from URL: {url}")
        return None, None
    
    logger.info(f"Extracted video ID: {video_id}")
    
    # Check if we already have the video cached
    video_path = os.path.join(TEMP_DIR, f"{video_id}.mp4")
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"Using cached video file: {video_path}")
        return video_path, video_id
    
    # Check if this download is already in progress
    with active_downloads_lock:
        if video_id in active_downloads:
            # Wait for a bit and check if it's completed
            logger.info(f"Download already in progress for video ID: {video_id}, waiting...")
            for _ in range(10):  # Wait for max 5 seconds
                time.sleep(0.5)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    logger.info(f"Downloaded file is now available: {video_path}")
                    return video_path, video_id
            
            # If still not available, consider it a new request
            logger.info(f"Timed out waiting for active download, proceeding with new request")
        
        # Mark this download as in progress
        active_downloads[video_id] = True
    
    try:
        # Use only the scraper method
        logger.info(f"Downloading TikTok video with ID: {video_id}")
        video_path = download_tiktok_video_scraper(video_id)
        
        if video_path:
            logger.info(f"Successfully downloaded video: {video_path}")
            return video_path, video_id
        else:
            logger.error(f"Failed to download video with ID: {video_id}")
            return None, video_id
    finally:
        # Remove the video ID from active downloads
        with active_downloads_lock:
            if video_id in active_downloads:
                del active_downloads[video_id]

def cleanup_old_files():
    """Clean up old temporary files to prevent disk space issues."""
    try:
        current_time = time.time()
        files_to_delete = []
        total_size = 0
        
        # First calculate total size and sort files by age
        files_info = []
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            if os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                file_mtime = os.path.getmtime(file_path)
                files_info.append((file_path, file_size, file_mtime))
                total_size += file_size
        
        # Sort files by modification time (oldest first)
        files_info.sort(key=lambda x: x[2])
        
        # If total size exceeds MAX_CACHE_SIZE_MB, delete oldest files first
        if total_size > MAX_CACHE_SIZE_MB * 1024 * 1024:
            logger.info(f"Cache size ({total_size/(1024*1024):.2f} MB) exceeds limit ({MAX_CACHE_SIZE_MB} MB). Cleaning up...")
            
            for file_path, file_size, file_mtime in files_info:
                os.remove(file_path)
                logger.info(f"Removed file to reduce cache size: {os.path.basename(file_path)}")
                total_size -= file_size
                if total_size <= MAX_CACHE_SIZE_MB * 0.9 * 1024 * 1024:  # Clean until we're under 90% of max
                    break
        
        # Now delete expired files
        for file_path, file_size, file_mtime in files_info:
            if current_time - file_mtime > CACHE_EXPIRATION:
                os.remove(file_path)
                logger.info(f"Removed expired file: {os.path.basename(file_path)}")
    except Exception as e:
        logger.error(f"Error cleaning up old files: {e}")

def validate_input(data):
    """Validate and sanitize incoming request data."""
    if not data or not isinstance(data, dict):
        return False, {"error": "Invalid JSON data"}, 400
        
    url = data.get('url', '').strip()
    if not url:
        return False, {"error": "No URL provided"}, 400
    
    if not is_valid_tiktok_url(url):
        return False, {"error": "Invalid TikTok URL"}, 400
    
    return True, {"url": url}, 200

@app.before_request
def before_request():
    """Add security headers to all responses."""
    # Clean up files occasionally to prevent disk space issues
    if random.random() < 0.05:  # 5% chance to trigger cleanup
        threading.Thread(target=cleanup_old_files).start()

@app.after_request
def add_security_headers(response):
    """Add security headers to response."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With'
    return response

@app.errorhandler(Exception)
def handle_error(e):
    """Global error handler."""
    logger.error(f"Unhandled exception: {str(e)}")
    return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify service is running."""
    return jsonify({
        "status": "ok", 
        "message": "TikTok video downloader service is running",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/download', methods=['POST'])
def download_tiktok():
    """API endpoint to download TikTok videos without watermark."""
    try:
        # Parse and validate input
        data = request.json
        is_valid, result, status_code = validate_input(data)
        
        if not is_valid:
            return jsonify(result), status_code
            
        url = result["url"]
        
        # Update download statistics
        stats.increment_total()
        
        # Try to download the video
        video_path, video_id = get_tiktok_video(url)
        
        if not video_path:
            stats.increment_failed()
            return jsonify({"error": "Failed to download video"}), 500
            
        # Successfully downloaded
        stats.increment_success()
        
        # Set appropriate headers for download
        return send_file(
            video_path, 
            as_attachment=True, 
            download_name=f"tiktok_{video_id}.mp4",
            mimetype="video/mp4"
        )
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """API endpoint to get service statistics."""
    try:
        # Count files by extension
        file_counts = {
            "mp4_files": 0,
            "other_files": 0
        }
        
        total_size = 0
        oldest_file_time = time.time()
        newest_file_time = 0
        
        for f in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, f)
            if not os.path.isfile(file_path):
                continue
                
            # Update file counts
            if f.endswith('.mp4'):
                file_counts["mp4_files"] += 1
            else:
                file_counts["other_files"] += 1
            
            # Update total size
            file_size = os.path.getsize(file_path)
            total_size += file_size
            
            # Update file time stats
            file_time = os.path.getmtime(file_path)
            oldest_file_time = min(oldest_file_time, file_time)
            newest_file_time = max(newest_file_time, file_time)
        
        # Calculate cache age in hours
        cache_age = {
            "oldest_file_hours": round((time.time() - oldest_file_time) / 3600, 2) if oldest_file_time < time.time() else 0,
            "newest_file_hours": round((time.time() - newest_file_time) / 3600, 2) if newest_file_time > 0 else 0
        }
        
        # Get download statistics
        download_stats = stats.get_stats()
        
        # System information
        system_info = {
            "temp_dir": TEMP_DIR,
            "free_space_mb": shutil.disk_usage(TEMP_DIR).free / (1024 * 1024),
            "total_space_mb": shutil.disk_usage(TEMP_DIR).total / (1024 * 1024),
            "active_downloads": len(active_downloads)
        }
        
        return jsonify({
            "status": "ok",
            "stats": {
                "files": file_counts,
                "total_files": sum(file_counts.values()),
                "cache_dir_size_mb": round(total_size / (1024 * 1024), 2),
                "cache_age": cache_age,
                "downloads": download_stats,
                "system": system_info
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({"error": f"Error getting stats: {str(e)}"}), 500

@app.route('/api/clear-cache', methods=['POST'])
def clear_cache():
    """Endpoint to clear the cache (requires admin secret)."""
    try:
        # Simple admin authentication
        admin_secret = request.headers.get('X-Admin-Secret')
        if not admin_secret or admin_secret != os.environ.get('ADMIN_SECRET', 'change_this_secret'):
            return jsonify({"error": "Unauthorized"}), 401
            
        # Clear the cache
        files_removed = 0
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                files_removed += 1
                
        # Clear the function caches
        download_tiktok_video_scraper.cache_clear()
        
        return jsonify({
            "status": "ok",
            "message": f"Cache cleared successfully. Removed {files_removed} files."
        })
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({"error": f"Error clearing cache: {str(e)}"}), 500

if __name__ == '__main__':
    print("TikTok Video Downloader API Server")
    print("----------------------------")
    print("API Endpoints:")
    print("  - POST /api/download: Download TikTok videos without watermark")
    print("  - GET /api/health: Health check endpoint")
    print("  - GET /api/stats: Get service statistics")
    print("  - POST /api/clear-cache: Clear the cache (requires admin authentication)")
    print("\nServer is starting on http://0.0.0.0:8080\n")
    
    # Run the Flask app
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
