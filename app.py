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
import hashlib
import shutil
import threading
from datetime import datetime

app = Flask(__name__)
# Configure CORS to allow requests from any origin
CORS(app)

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
CACHE_EXPIRATION = 86400  # 24 hours
MAX_CACHE_SIZE_MB = 5000  # 5GB maximum cache size

# List of user agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.80 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# Enhanced TikTok cookies
TT_COOKIES = [
    "tt_webid_v2=123456789012345678; tt_webid=123456789012345678; ttwid=1%7CAbC123dEf456gHi789jKl%7C1600000000%7Cabcdef0123456789abcdef0123456789; msToken=AbC123dEf456gHi789jKl;",
    "tt_webid_v2=234567890123456789; tt_webid=234567890123456789; ttwid=1%7CBcD234eFg567hIj890kLm%7C1600100000%7Cbcdefg1234567890abcdef0123456789; msToken=BcD234eFg567hIj890kLm;",
    "tt_webid_v2=345678901234567890; tt_webid=345678901234567890; ttwid=1%7CCdE345fGh678iJk901lMn%7C1600200000%7Ccdefgh2345678901abcdef0123456789; msToken=CdE345fGh678iJk901lMn;",
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,video/mp4,*/*;q=0.8",
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
        "Cache-Control": "no-cache",
        "Range": "bytes=0-"  # Add Range header to ensure full video content
    }
    
    if referer:
        headers["Referer"] = referer
        
    # Always add cookies for better reliability
    headers["Cookie"] = get_random_cookies()
    
    return headers

def validate_video_file(file_path):
    """Check if the downloaded file is a valid MP4 video."""
    try:
        file_size = os.path.getsize(file_path)
        
        # Check minimum file size (10KB)
        if file_size < 10240:
            logger.warning(f"File too small ({file_size} bytes), likely invalid")
            return False
            
        # Check file header for MP4 signature
        with open(file_path, 'rb') as f:
            header = f.read(12)
            # Most MP4 files start with 'ftyp' at byte 4
            if b'ftyp' not in header:
                logger.warning(f"File doesn't have MP4 signature: {header}")
                return False
                
        return True
    except Exception as e:
        logger.error(f"Error validating video file: {e}")
        return False

def download_tiktok_video(video_id):
    """Download TikTok video without watermark."""
    try:
        # Build the TikTok video URL
        url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
        headers = get_random_request_headers()
        
        logger.info(f"Fetching TikTok page: {url}")
        response = requests.get(
            url, 
            headers=headers, 
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok page. Status: {response.status_code}")
            return None

        # Create a temporary file path
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
            
        # Method 1: Extract video URL from page content
        video_url = None
        
        # Look for video URL in the page HTML - focusing on high quality MP4 sources
        patterns = [
            # Match unescaped URLs
            r'"playAddr":"([^"]+)"',
            r'"playAddr_h264":"([^"]+)"',
            r'"playUrl":"([^"]+)"', 
            r'"videoUrl":"([^"]+)"',
            r'"downloadAddr":"([^"]+)"',
            # Match with h264/h265 specific URLs
            r'"h264PlayAddr":"([^"]+)"',
            r'"h265PlayAddr":"([^"]+)"',
            # Video element sources
            r'<video[^>]+src="([^"]+)"'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                for match in matches:
                    candidate_url = match.replace('\\u002F', '/').replace('\\', '')
                    # Filter out audio-only URLs
                    if 'music' not in candidate_url.lower() and 'audio' not in candidate_url.lower():
                        video_url = candidate_url
                        logger.info(f"Found video URL: {video_url[:60]}...")
                        break
                if video_url:
                    break
        
        # If we found a video URL, download it
        if video_url:
            # Ensure it's a full URL
            if video_url.startswith('//'):
                video_url = 'https:' + video_url
                
            # Download the video with special headers for video content
            video_headers = get_random_request_headers(referer=url)
            video_headers['Accept'] = 'video/mp4,video/*,*/*;q=0.8'
            
            try:
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=30,
                    verify=False  # Sometimes needed for CDN URLs
                )
                
                if video_response.status_code == 200:
                    # Check content type
                    content_type = video_response.headers.get('Content-Type', '')
                    logger.info(f"Content-Type of response: {content_type}")
                    
                    if 'video' in content_type or 'octet-stream' in content_type:
                        # Stream the video to the file
                        with open(temp_file, 'wb') as f:
                            for chunk in video_response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        
                        # Validate the video file
                        if validate_video_file(temp_file):
                            logger.info(f"Successfully downloaded video to {temp_file}")
                            return temp_file
                        else:
                            logger.warning(f"Downloaded file is not a valid video, removing: {temp_file}")
                            if os.path.exists(temp_file):
                                os.remove(temp_file)
                    else:
                        logger.warning(f"Response is not video content: {content_type}")
                else:
                    logger.error(f"Failed to download video from URL. Status: {video_response.status_code}")
            except Exception as e:
                logger.error(f"Error downloading from URL {video_url}: {e}")
        
        # Method 2: Try additional extraction strategies
        # Look for data in JSON structures within the page
        json_pattern = r'<script id="SIGI_STATE" type="application/json">(.*?)</script>'
        json_match = re.search(json_pattern, response.text, re.DOTALL)
        
        if json_match:
            try:
                json_data = json.loads(json_match.group(1))
                
                # Navigate through possible JSON paths to find video URL
                if "ItemModule" in json_data and video_id in json_data["ItemModule"]:
                    video_data = json_data["ItemModule"][video_id]
                    if "video" in video_data:
                        video_url = video_data["video"].get("playAddr") or video_data["video"].get("downloadAddr")
                        
                        if video_url:
                            logger.info(f"Found video URL in JSON data: {video_url[:60]}...")
                            
                            # Download using the same approach as above
                            video_headers = get_random_request_headers(referer=url)
                            video_headers['Accept'] = 'video/mp4,video/*,*/*;q=0.8'
                            
                            try:
                                video_response = requests.get(
                                    video_url, 
                                    headers=video_headers, 
                                    stream=True, 
                                    timeout=30,
                                    verify=False
                                )
                                
                                if video_response.status_code == 200:
                                    with open(temp_file, 'wb') as f:
                                        for chunk in video_response.iter_content(chunk_size=8192):
                                            if chunk:
                                                f.write(chunk)
                                    
                                    if validate_video_file(temp_file):
                                        return temp_file
                                    else:
                                        if os.path.exists(temp_file):
                                            os.remove(temp_file)
                            except Exception as e:
                                logger.error(f"Error downloading from JSON URL: {e}")
            except json.JSONDecodeError:
                logger.error("Failed to parse JSON data from page")
        
        # Method 3: Use a fallback approach with direct API
        # This is a more direct approach if the above methods fail
        try:
            api_url = f"https://api16-normal-useast5.us.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}"
            api_headers = get_random_request_headers()
            api_headers["Accept"] = "application/json"
            
            api_response = requests.get(api_url, headers=api_headers, timeout=30)
            if api_response.status_code == 200:
                try:
                    api_data = api_response.json()
                    
                    # Extract video URL from API response
                    if "aweme_list" in api_data and len(api_data["aweme_list"]) > 0:
                        aweme = api_data["aweme_list"][0]
                        if "video" in aweme and "play_addr" in aweme["video"]:
                            video_urls = aweme["video"]["play_addr"].get("url_list", [])
                            if video_urls:
                                video_url = video_urls[0]
                                logger.info(f"Found video URL from API: {video_url[:60]}...")
                                
                                # Download using the same approach as above
                                video_headers = get_random_request_headers()
                                video_response = requests.get(
                                    video_url, 
                                    headers=video_headers, 
                                    stream=True, 
                                    timeout=30
                                )
                                
                                if video_response.status_code == 200:
                                    with open(temp_file, 'wb') as f:
                                        for chunk in video_response.iter_content(chunk_size=8192):
                                            if chunk:
                                                f.write(chunk)
                                    
                                    if validate_video_file(temp_file):
                                        return temp_file
                                    else:
                                        if os.path.exists(temp_file):
                                            os.remove(temp_file)
                except json.JSONDecodeError:
                    logger.error("Failed to parse API response JSON")
        except Exception as e:
            logger.error(f"Error using API fallback: {e}")
        
        # If we reached here, we couldn't download the video
        logger.error(f"All methods failed to download video with ID: {video_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None

def process_tiktok_url(url):
    """Process a TikTok URL and download the video."""
    # Extract video ID from URL
    video_id = extract_video_id(url)
    if not video_id:
        logger.error(f"Could not extract video ID from URL: {url}")
        return None, None
    
    logger.info(f"Extracted video ID: {video_id}")
    
    # Check if we already have the video cached
    video_path = os.path.join(TEMP_DIR, f"{video_id}.mp4")
    if os.path.exists(video_path) and validate_video_file(video_path):
        logger.info(f"Using cached video file: {video_path}")
        return video_path, video_id
    
    # Check if this download is already in progress
    with active_downloads_lock:
        if video_id in active_downloads:
            # Wait for a bit and check if it's completed
            logger.info(f"Download already in progress for video ID: {video_id}, waiting...")
            for _ in range(10):  # Wait for max 5 seconds
                time.sleep(0.5)
                if os.path.exists(video_path) and validate_video_file(video_path):
                    logger.info(f"Downloaded file is now available: {video_path}")
                    return video_path, video_id
            
            # If still not available, consider it a new request
            logger.info(f"Timed out waiting for active download, proceeding with new request")
        
        # Mark this download as in progress
        active_downloads[video_id] = True
    
    try:
        # Download the video
        logger.info(f"Downloading TikTok video with ID: {video_id}")
        video_path = download_tiktok_video(video_id)
        
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

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify service is running."""
    return jsonify({
        "status": "ok", 
        "message": "TikTok video downloader service is running",
        "version": "1.1.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/download', methods=['POST'])
def download_tiktok():
    """API endpoint to download TikTok videos without watermark."""
    try:
        # Parse input
        data = request.json
        if not data or 'url' not in data:
            return jsonify({"error": "No URL provided"}), 400
            
        url = data['url'].strip()
        if not url:
            return jsonify({"error": "Empty URL provided"}), 400
            
        if not is_valid_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        # Update download statistics
        stats.increment_total()
        
        # Attempt to run cleanup in the background occasionally
        if random.random() < 0.05:  # 5% chance
            threading.Thread(target=cleanup_old_files).start()
        
        # Process the URL and download the video
        video_path, video_id = process_tiktok_url(url)
        
        if not video_path or not os.path.exists(video_path):
            stats.increment_failed()
            return jsonify({"error": "Failed to download video"}), 500
            
        # Successfully downloaded
        stats.increment_success()
        
        # Set appropriate headers for download
        return send_file(
            video_path, 
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"tiktok_{video_id}.mp4"
        )
        
    except Exception as e:
        logger.error(f"Error processing download request: {str(e)}")
        return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """API endpoint to get service statistics."""
    try:
        # Count files by extension
        file_counts = {"mp4_files": 0, "other_files": 0}
        total_size = 0
        
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
            total_size += os.path.getsize(file_path)
        
        # Get download statistics
        download_stats = stats.get_stats()
        
        # System information
        system_info = {
            "temp_dir": TEMP_DIR,
            "free_space_mb": shutil.disk_usage(TEMP_DIR).free / (1024 * 1024),
            "active_downloads": len(active_downloads)
        }
        
        return jsonify({
            "status": "ok",
            "stats": {
                "files": file_counts,
                "total_files": sum(file_counts.values()),
                "cache_dir_size_mb": round(total_size / (1024 * 1024), 2),
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
                
        return jsonify({
            "status": "ok",
            "message": f"Cache cleared successfully. Removed {files_removed} files."
        })
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({"error": f"Error clearing cache: {str(e)}"}), 500

# Add CORS headers to all responses
@app.after_request
def add_cors_headers(response):
    """Add CORS headers to response."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With, X-Admin-Secret'
    return response

@app.route('/api/version', methods=['GET'])
def get_version():
    """Return API version information."""
    return jsonify({
        "version": "1.1.0",
        "api": "TikTok Downloader API",
        "endpoints": [
            {"path": "/api/download", "method": "POST", "description": "Download TikTok video without watermark"},
            {"path": "/api/health", "method": "GET", "description": "Health check endpoint"},
            {"path": "/api/stats", "method": "GET", "description": "Get service statistics"},
            {"path": "/api/clear-cache", "method": "POST", "description": "Clear the cache (admin only)"},
            {"path": "/api/version", "method": "GET", "description": "Get API version information"}
        ]
    })

if __name__ == '__main__':
    print("TikTok Video Downloader API Server")
    print("----------------------------")
    print("API Endpoints:")
    print("  - POST /api/download: Download TikTok videos without watermark")
    print("  - GET /api/health: Health check endpoint")
    print("  - GET /api/stats: Get service statistics")
    print("  - POST /api/clear-cache: Clear the cache (requires admin authentication)")
    print("  - GET /api/version: Get API version information")
    print("\nServer is starting on http://0.0.0.0:8080\n")
    
    # Run the Flask app
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
