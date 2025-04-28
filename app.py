from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os
import tempfile
import logging
import json
import hashlib
import random
from urllib.parse import urlparse

# Create Flask app
app = Flask(__name__)
# Configure CORS to allow any origin
CORS(app)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_fetcher')
os.makedirs(TEMP_DIR, exist_ok=True)

# List of user agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"
]

def get_random_user_agent():
    """Get a random user agent from the list."""
    return random.choice(USER_AGENTS)

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

def download_tiktok_video(video_id):
    """Download TikTok video using the mobile website."""
    try:
        # Direct video URL
        mobile_url = f"https://m.tiktok.com/v/{video_id}"
        
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }
        
        logger.info(f"Fetching mobile TikTok page: {mobile_url}")
        response = requests.get(mobile_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch mobile TikTok page. Status: {response.status_code}")
            return None, None
        
        # Patterns to extract video URLs
        video_patterns = [
            r'"playAddr":"([^"]+)"',
            r'"downloadAddr":"([^"]+)"',
            r'"playUrl":"([^"]+)"',
            r'"videoUrl":"([^"]+)"',
            r'(https://[^"\']+\.mp4[^"\'\s]*)'
        ]
        
        video_url = None
        for pattern in video_patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                # Use the first match
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL: {video_url[:60]}...")
                break
        
        if not video_url:
            logger.error("No video URL found in the page.")
            return None, "No video URL found"
        
        # Extract title if available
        title_pattern = r'"desc":"([^"]+)"'
        title_match = re.search(title_pattern, response.text)
        title = title_match.group(1).replace('\\u002F', '/').replace('\\', '') if title_match else "TikTok Video"
        
        # Download the video
        video_headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": mobile_url,
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Range": "bytes=0-"
        }
        
        video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
        
        if video_response.status_code not in [200, 206]:
            logger.error(f"Failed to download video. Status: {video_response.status_code}")
            return None, f"Failed to download video. Status: {video_response.status_code}"
        
        # Create a temporary file
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file
        total_size = 0
        with open(temp_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
        
        logger.info(f"Downloaded video file size: {total_size} bytes")
        
        if total_size < 10000:  # If file is too small, likely an error
            logger.error(f"Downloaded file is too small: {total_size} bytes")
            os.remove(temp_file)
            return None, "Downloaded file is too small"
            
        return temp_file, title
        
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None, str(e)

def try_alternative_download_method(video_id):
    """Try alternative method using web API."""
    try:
        # Build the web URL
        web_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
        
        headers = {
            "User-Agent": get_random_user_agent(),
            "Referer": f"https://www.tiktok.com/video/{video_id}",
            "Accept": "application/json, text/plain, */*"
        }
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(web_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok web API. Status: {response.status_code}")
            return None, None
        
        try:
            data = response.json()
            
            # Navigate the JSON structure to find the video URL
            if "itemInfo" in data and "itemStruct" in data["itemInfo"]:
                video_data = data["itemInfo"]["itemStruct"]["video"]
                title = data["itemInfo"]["itemStruct"].get("desc", "TikTok Video")
                
                if "playAddr" in video_data:
                    video_url = video_data["playAddr"]
                elif "downloadAddr" in video_data:
                    video_url = video_data["downloadAddr"]
                else:
                    logger.error("Could not find video URL in API response")
                    return None, "Could not find video URL in API response"
                
                # Download the video
                video_headers = {
                    "User-Agent": get_random_user_agent(),
                    "Referer": f"https://www.tiktok.com/video/{video_id}",
                    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                }
                
                video_response = requests.get(video_url, headers=video_headers, stream=True, timeout=30)
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video from API. Status: {video_response.status_code}")
                    return None, f"Failed to download video from API. Status: {video_response.status_code}"
                
                # Create a temporary file
                temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file
                total_size = 0
                with open(temp_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            total_size += len(chunk)
                
                if total_size < 10000:  # If file is too small, likely an error
                    logger.error(f"Downloaded file is too small: {total_size} bytes")
                    os.remove(temp_file)
                    return None, "Downloaded file is too small"
                    
                return temp_file, title
            else:
                logger.error("Unexpected API response structure")
                return None, "Unexpected API response structure"
                
        except json.JSONDecodeError:
            logger.error("Failed to parse API response as JSON")
            return None, "Failed to parse API response as JSON"
            
    except Exception as e:
        logger.error(f"Error in web API method: {e}")
        return None, str(e)

def fetch_tiktok_video(video_id):
    """Try different methods to download a TikTok video."""
    # Try primary method first
    video_path, title = download_tiktok_video(video_id)
    
    # If primary method fails, try alternative
    if not video_path:
        logger.info("Primary download method failed, trying alternative...")
        video_path, title = try_alternative_download_method(video_id)
    
    if not video_path:
        return None, "Failed to download video using all available methods", None
    
    # Get video info
    video_info = {
        "video_id": video_id,
        "title": title,
        "file_path": video_path,
        "file_size": os.path.getsize(video_path)
    }
    
    return video_path, None, video_info

# API Routes
@app.route('/api/fetch', methods=['POST'])
def fetch_video():
    """Fetch a TikTok video."""
    try:
        request_data = request.get_json()
        
        # Validate request
        if not request_data:
            return jsonify({"error": "Invalid request data"}), 400
            
        # Get URL or video ID
        url = request_data.get('url')
        video_id = request_data.get('video_id')
        
        # Extract video ID from URL if provided
        if url and not video_id:
            if not is_valid_tiktok_url(url):
                return jsonify({"error": "Invalid TikTok URL"}), 400
                
            video_id = extract_video_id(url)
            
            if not video_id:
                return jsonify({"error": "Could not extract video ID from URL"}), 400
                
        if not video_id:
            return jsonify({"error": "Either URL or video_id is required"}), 400
            
        # Fetch the video
        video_path, error, video_info = fetch_tiktok_video(video_id)
        
        if error:
            return jsonify({"error": error}), 500
        
        # Return video information
        result = {
            "success": True,
            "video_id": video_id,
            "title": video_info.get("title", "TikTok Video"),
            "file_size": video_info.get("file_size", 0),
            "download_url": f"/api/download/{video_id}",
        }
            
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Error in fetch endpoint: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/download/<video_id>', methods=['GET'])
def download_video(video_id):
    """Download the fetched video."""
    try:
        # Check if video exists in temp directory
        video_path = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        if not os.path.exists(video_path):
            video_path, error, _ = fetch_tiktok_video(video_id)
            
            if error:
                return jsonify({"error": error}), 500
        
        # Set headers for file download
        headers = {
            "Content-Disposition": f"attachment; filename=tiktok_{video_id}.mp4"
        }
        
        # Return the file
        return open(video_path, 'rb').read(), 200, {
            'Content-Type': 'video/mp4',
            'Content-Disposition': f'attachment; filename=tiktok_{video_id}.mp4'
        }
        
    except Exception as e:
        logger.error(f"Error in download endpoint: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "message": "TikTok Fetcher API is running"
    })

# Start the application
if __name__ == '__main__':
    logger.info("Starting TikTok Fetcher Server")
    app.run(host='0.0.0.0', port=5000, debug=True)
