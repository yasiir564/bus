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
import threading
from datetime import datetime
import subprocess
import hashlib
from functools import lru_cache
import shutil
from urllib.parse import urlparse
import vosk
import wave
import soundfile as sf
import numpy as np

# Check if ffmpeg is installed
try:
    subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
except (FileNotFoundError, subprocess.CalledProcessError):
    print("Error: ffmpeg is not installed or not in PATH. Please install ffmpeg.")
    exit(1)

# Create Flask app
app = Flask(__name__)
# Configure CORS to allow specific origins in production or any in development
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Cloudflare Turnstile Configuration
TURNSTILE_SECRET_KEY = "0x4AAAAAABHoxYr9SKSH_1ZBB4LpXbr_0sQ"  # Replace with your actual secret key
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("tiktok_transcriber.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_transcriber')
os.makedirs(TEMP_DIR, exist_ok=True)

# Create models directory for Vosk
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
os.makedirs(MODELS_DIR, exist_ok=True)

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 312.0.0.0.41",
    "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36"
]

# List of cookies to rotate
TT_COOKIES = [
    "tt_webid_v2=123456789012345678; tt_webid=123456789012345678; ttwid=1%7CAbC123dEf456gHi789jKl%7C1600000000%7Cabcdef0123456789abcdef0123456789; msToken=AbC123dEf456gHi789jKl",
    "tt_webid_v2=234567890123456789; tt_webid=234567890123456789; ttwid=1%7CBcD234eFg567hIj890kLm%7C1600100000%7Cbcdefg1234567890abcdef0123456789; msToken=BcD234eFg567hIj890kLm",
    "tt_webid_v2=345678901234567890; tt_webid=345678901234567890; ttwid=1%7CCdE345fGh678iJk901lMn%7C1600200000%7Ccdefgh2345678901abcdef0123456789; msToken=CdE345fGh678iJk901lMn",
]

# Proxy configuration (optional)
# Format: {"http": "http://user:pass@host:port", "https": "http://user:pass@host:port"}
PROXIES = None

# Download rate limiting
MAX_DOWNLOADS_PER_MINUTE = 20
download_timestamps = []
download_lock = threading.Lock()

# Track active downloads
active_downloads = {}
active_downloads_lock = threading.Lock()

# Available Vosk models (small to large)
VOSK_MODELS = {
    "small": "vosk-model-small-en-us-0.15",
    "medium": "vosk-model-en-us-0.22",
    "large": "vosk-model-en-us-0.42"
}

# Default model to use
DEFAULT_MODEL = "small"

# Model management
model_cache = {}
model_lock = threading.Lock()

class DownloadStats:
    def __init__(self):
        self.total_downloads = 0
        self.successful_downloads = 0
        self.failed_downloads = 0
        self.total_transcriptions = 0
        self.successful_transcriptions = 0
        self.failed_transcriptions = 0
        self.last_download_time = None
        self.last_transcription_time = None
        self.lock = threading.Lock()
        
    def increment_total_download(self):
        with self.lock:
            self.total_downloads += 1
            self.last_download_time = datetime.now()
            
    def increment_successful_download(self):
        with self.lock:
            self.successful_downloads += 1
            
    def increment_failed_download(self):
        with self.lock:
            self.failed_downloads += 1
            
    def increment_total_transcription(self):
        with self.lock:
            self.total_transcriptions += 1
            self.last_transcription_time = datetime.now()
            
    def increment_successful_transcription(self):
        with self.lock:
            self.successful_transcriptions += 1
            
    def increment_failed_transcription(self):
        with self.lock:
            self.failed_transcriptions += 1
            
    def get_stats(self):
        with self.lock:
            return {
                "downloads": {
                    "total": self.total_downloads,
                    "successful": self.successful_downloads,
                    "failed": self.failed_downloads,
                    "last_download": self.last_download_time.isoformat() if self.last_download_time else None
                },
                "transcriptions": {
                    "total": self.total_transcriptions,
                    "successful": self.successful_transcriptions,
                    "failed": self.failed_transcriptions,
                    "last_transcription": self.last_transcription_time.isoformat() if self.last_transcription_time else None
                }
            }

stats = DownloadStats()

def get_random_user_agent():
    """Get a random user agent from the list."""
    return random.choice(USER_AGENTS)

def get_random_cookies():
    """Get random cookies for TikTok requests."""
    return random.choice(TT_COOKIES)

def can_perform_download():
    """Rate limiting for downloads."""
    global download_timestamps
    
    with download_lock:
        current_time = time.time()
        # Remove timestamps older than 60 seconds
        download_timestamps = [ts for ts in download_timestamps if current_time - ts < 60]
        
        if len(download_timestamps) >= MAX_DOWNLOADS_PER_MINUTE:
            return False
        
        download_timestamps.append(current_time)
        return True

def verify_turnstile_token(token, remote_ip=None):
    """Verify Cloudflare Turnstile token."""
    try:
        data = {
            "secret": TURNSTILE_SECRET_KEY,
            "response": token
        }
        
        if remote_ip:
            data["remoteip"] = remote_ip
            
        response = requests.post(TURNSTILE_VERIFY_URL, data=data, timeout=10)
        result = response.json()
        
        if result.get("success"):
            return True, None
        else:
            return False, result.get("error-codes", ["Unknown error"])
    except Exception as e:
        logger.error(f"Turnstile verification error: {e}")
        return False, ["Verification service error"]

def generate_cache_key(url, model_size="small"):
    """Generate a unique cache key based on URL and model size."""
    key = f"{url}_{model_size}"
    return hashlib.md5(key.encode()).hexdigest()

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
        "Pragma": "no-cache",
        "Cache-Control": "no-cache"
    }
    
    if referer:
        headers["Referer"] = referer
        
    # Add cookies to some requests for better undetectability
    if random.random() < 0.7:  # 70% chance to add cookies
        headers["Cookie"] = get_random_cookies()
    
    return headers

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_mobile(video_id):
    """Download TikTok video using the mobile website."""
    try:
        # Direct video URL
        mobile_url = f"https://m.tiktok.com/v/{video_id}"
        
        headers = get_random_request_headers()
        
        logger.info(f"Fetching mobile TikTok page: {mobile_url}")
        response = requests.get(
            mobile_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch mobile TikTok page. Status: {response.status_code}")
            return None
        
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
            return None
        
        # Download the video
        video_headers = get_random_request_headers(referer=mobile_url)
        video_headers.update({
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Range": "bytes=0-"
        })
        
        video_response = requests.get(
            video_url, 
            headers=video_headers, 
            stream=True, 
            timeout=30,
            proxies=PROXIES
        )
        
        if video_response.status_code not in [200, 206]:
            logger.error(f"Failed to download video. Status: {video_response.status_code}")
            return None
        
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
            return None
            
        return temp_file
        
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_web(video_id):
    """Alternative method using web API."""
    try:
        # Build the web URL
        web_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
        
        headers = get_random_request_headers(referer=f"https://www.tiktok.com/video/{video_id}")
        headers.update({
            "Accept": "application/json, text/plain, */*"
        })
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(
            web_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok web API. Status: {response.status_code}")
            return None
        
        try:
            data = response.json()
            
            # Navigate the JSON structure to find the video URL
            if "itemInfo" in data and "itemStruct" in data["itemInfo"]:
                video_data = data["itemInfo"]["itemStruct"]["video"]
                
                if "playAddr" in video_data:
                    video_url = video_data["playAddr"]
                elif "downloadAddr" in video_data:
                    video_url = video_data["downloadAddr"]
                else:
                    logger.error("Could not find video URL in API response")
                    return None
                
                # Download the video
                video_headers = get_random_request_headers(referer=f"https://www.tiktok.com/video/{video_id}")
                video_headers.update({
                    "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                })
                
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=30,
                    proxies=PROXIES
                )
                
                if video_response.status_code != 200:
                    logger.error(f"Failed to download video from API. Status: {video_response.status_code}")
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
            else:
                logger.error("Unexpected API response structure")
                return None
                
        except json.JSONDecodeError:
            logger.error("Failed to parse API response as JSON")
            return None
            
    except Exception as e:
        logger.error(f"Error in web API method: {e}")
        return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_embed(video_id):
    """Try downloading via TikTok's embed functionality."""
    try:
        # Build the embed URL
        embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
        
        headers = get_random_request_headers()
        
        logger.info(f"Fetching TikTok embed page: {embed_url}")
        response = requests.get(
            embed_url, 
            headers=headers, 
            timeout=20,
            proxies=PROXIES
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch TikTok embed page. Status: {response.status_code}")
            return None
        
        # Look for video URL in the embed page
        video_patterns = [
            r'<video[^>]+src="([^"]+)"',
            r'"contentUrl":"([^"]+)"',
            r'"playAddr":"([^"]+)"',
            r'"url":"([^"]+\.mp4[^"]*)"'
        ]
        
        video_url = None
        for pattern in video_patterns:
            matches = re.findall(pattern, response.text)
            if matches:
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                logger.info(f"Found video URL in embed: {video_url[:60]}...")
                break
        
        if not video_url:
            logger.error("No video URL found in the embed page.")
            return None
        
        # Download the video
        video_headers = get_random_request_headers(referer=embed_url)
        video_headers.update({
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
        })
        
        video_response = requests.get(
            video_url, 
            headers=video_headers, 
            stream=True, 
            timeout=30,
            proxies=PROXIES
        )
        
        if video_response.status_code != 200:
            logger.error(f"Failed to download video from embed. Status: {video_response.status_code}")
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
        
    except Exception as e:
        logger.error(f"Error in embed method: {e}")
        return None

@lru_cache(maxsize=CACHE_SIZE)
def download_tiktok_video_scraper(video_id):
    """Try downloading using a more sophisticated approach."""
    try:
        # Build the direct video URL
        url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
        headers = get_random_request_headers()
        
        # Add specific headers that may help bypass restrictions
        headers.update({
            "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118"',
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
            timeout=30,
            proxies=PROXIES
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
                                timeout=30,
                                proxies=PROXIES
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
        
        # If we reached here, try regex method as fallback
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
                    timeout=30,
                    proxies=PROXIES
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

def extract_audio_from_video(video_path, video_id):
    """Extract audio from video file using ffmpeg."""
    try:
        audio_path = os.path.join(TEMP_DIR, f"{video_id}.wav")
        
        # FFmpeg command to extract audio
        cmd = [
            'ffmpeg',
            '-y',  # Overwrite output file without asking
            '-i', video_path,  # Input file
            '-vn',  # No video
            '-ar', '16000',  # Audio sample rate: 16kHz (required by Vosk)
            '-ac', '1',  # Mono audio (required by Vosk)
            '-f', 'wav',  # Force format
            audio_path
        ]
        
        logger.info(f"Extracting audio from video")
        process = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        
        if process.returncode != 0:
            logger.error(f"FFmpeg error: {process.stderr.decode()}")
            return None
        
        logger.info(f"Successfully extracted audio. File size: {os.path.getsize(audio_path)} bytes")
        return audio_path
    except Exception as e:
        logger.error(f"Error extracting audio: {e}")
        return None

def download_vosk_model(model_name):
    """Download and extract a Vosk model if not already present."""
    model_path = os.path.join(MODELS_DIR, model_name)
    
    # If model directory already exists, assume it's already downloaded
    if os.path.exists(model_path) and os.path.isdir(model_path):
        logger.info(f"Model {model_name} already exists at {model_path}")
        return model_path
    
    try:
        # For this example, we're assuming the models are already downloaded
        # In a real application, you would download the model from vosk.org
        logger.error(f"Model {model_name} not found. Please download it from https://alphacephei.com/vosk/models")
        logger.error(f"Download the model and extract it to {model_path}")
        return None
    except Exception as e:
        logger.error(f"Error downloading model: {e}")
        return None

def get_vosk_model(model_size):
    """Get or load the Vosk model, with caching."""
    global model_cache
    
    # Determine model name based on size
    if model_size not in VOSK_MODELS:
        logger.warning(f"Unknown model size: {model_size}. Using default: {DEFAULT_MODEL}")
        model_size = DEFAULT_MODEL
        
    model_name = VOSK_MODELS[model_size]
    
    # Check if model is already in memory
    with model_lock:
        if model_size in model_cache:
            logger.info(f"Using cached model: {model_size}")
            return model_cache[model_size]
    
    # Ensure model is downloaded
    model_path = os.path.join(MODELS_DIR, model_name)
    if not os.path.exists(model_path):
        logger.info(f"Model {model_name} not found locally, downloading...")
        model_path = download_vosk_model(model_name)
        if not model_path:
            logger.error(f"Failed to download model: {model_name}")
            return None
    
    # Load the model
    try:
        logger.info(f"Loading Vosk model: {model_name}")
        model = vosk.Model(model_path)
        
        # Cache the model
        with model_lock:
            model_cache[model_size] = model
            
        logger.info(f"Successfully loaded model: {model_name}")
        return model
    except Exception as e:
        logger.error(f"Error loading Vosk model: {e}")
        return None

def transcribe_audio(audio_path, model_size=DEFAULT_MODEL):
    """Transcribe audio using Vosk."""
    try:
        # Get the model
        model = get_vosk_model(model_size)
        if not model:
            logger.error("Failed to load Vosk model")
            return None
        
        # Open the audio file
        wf = wave.open(audio_path, "rb")
        
        # Check audio format
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
            logger.error("Audio file must be WAV format mono PCM")
            return None
        
        # Create recognizer
        rec = vosk.KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(True)  # Enable word timestamps
        
        # Process the audio
        results = []
        while True:
            data = wf.readframes(4000)  # Read chunks of audio
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                part_result = json.loads(rec.Result())
                if part_result.get("text", "").strip():
                    results.append(part_result)
        
        # Get final result
        final_result = json.loads(rec.FinalResult())
        if final_result.get("text", "").strip():
            results.append(final_result)
        
        # Combine all results
        all_words = []
        full_text = ""
        
        for res in results:
            if "result" in res:
                all_words.extend(res["result"])
            if res.get("text", "").strip():
                full_text += res["text"] + " "
        
        # Format the result with timestamps
        formatted_result = {
            "text": full_text.strip(),
            "words": all_words,
            "segments": []
        }
        
        # Create segments from words (group by time)
        if all_words:
            current_segment = {
                "start": all_words[0]["start"],
                "end": all_words[0]["end"],
                "text": all_words[0]["word"]
            }
            
            for i in range(1, len(all_words)):
                word = all_words[i]
                
                # If less than 1 second gap, extend current segment
                if word["start"] - current_segment["end"] < 1.0:
                    current_segment["end"] = word["end"]
                    current_segment["text"] += " " + word["word"]
                else:
                    # Add the completed segment and start a new one
                    formatted_result["segments"].append(current_segment)
                    current_segment = {
                        "start": word["start"],
                        "end": word["end"],
                        "text": word["word"]
                    }
            
            # Add the last segment
            formatted_result["segments"].append(current_segment)
        
        return formatted_result
        
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return None

def clean_up_files(video_path=None, audio_path=None):
    """Clean up temporary files."""
    try:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            logger.info(f"Removed video file: {video_path}")
            
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
            logger.info(f"Removed audio file: {audio_path}")
    except Exception as e:
        logger.error(f"Error cleaning up files: {e}")

def maintain_cache():
    """Clean old files from the temp directory."""
    try:
        # Get all files in the temp directory
        files = [os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        
        # Sort files by modification time (oldest first)
        files.sort(key=os.path.getmtime)
        
        # Calculate total size
        total_size = sum(os.path.getsize(f) for f in files)
        
        # Remove old files if total size exceeds limit
        while total_size > MAX_CACHE_SIZE_MB * 1024 * 1024 and files:
            file_to_remove = files.pop(0)  # Remove oldest file
            file_size = os.path.getsize(file_to_remove)
            try:
                os.remove(file_to_remove)
                total_size -= file_size
                logger.info(f"Removed old cache file: {file_to_remove}, size: {file_size} bytes")
            except Exception as e:
                logger.error(f"Error removing cache file: {e}")
                
        # Remove files older than expiration time
        current_time = time.time()
        for file_path in files:
            file_mod_time = os.path.getmtime(file_path)
            if current_time - file_mod_time > CACHE_EXPIRATION:
                try:
                    os.remove(file_path)
                    logger.info(f"Removed expired cache file: {file_path}")
                except Exception as e:
                    logger.error(f"Error removing expired file: {e}")
    except Exception as e:
        logger.error(f"Error maintaining cache: {e}")

def download_tiktok_video(video_id):
    """Try all methods to download a TikTok video."""
    stats.increment_total_download()
    
    # Check if we can perform download (rate limiting)
    if not can_perform_download():
        logger.warning("Rate limit reached for downloads")
        stats.increment_failed_download()
        return None, "Rate limit reached. Please try again later."
    
    # Track this download
    download_key = f"download_{video_id}_{int(time.time())}"
    with active_downloads_lock:
        active_downloads[download_key] = {
            "status": "downloading",
            "progress": 0,
            "video_id": video_id,
            "start_time": time.time()
        }
    
    try:
        # Try different methods in order
        methods = [
            ("mobile", download_tiktok_video_mobile),
            ("web", download_tiktok_video_web),
            ("embed", download_tiktok_video_embed),
            ("scraper", download_tiktok_video_scraper)
        ]
        
        for method_name, method_func in methods:
            logger.info(f"Trying download method: {method_name}")
            
            with active_downloads_lock:
                if download_key in active_downloads:
                    active_downloads[download_key]["status"] = f"trying_{method_name}"
                    active_downloads[download_key]["progress"] = 25 * methods.index((method_name, method_func))
            
            video_path = method_func(video_id)
            
            if video_path and os.path.exists(video_path):
                logger.info(f"Successfully downloaded video using {method_name} method")
                
                with active_downloads_lock:
                    if download_key in active_downloads:
                        active_downloads[download_key]["status"] = "completed"
                        active_downloads[download_key]["progress"] = 100
                
                stats.increment_successful_download()
                return video_path, None
        
        # If all methods failed
        logger.error(f"All download methods failed for video ID: {video_id}")
        
        with active_downloads_lock:
            if download_key in active_downloads:
                active_downloads[download_key]["status"] = "failed"
                active_downloads[download_key]["progress"] = 0
        
        stats.increment_failed_download()
        return None, "Failed to download video. TikTok's anti-scraping measures may be active."
        
    except Exception as e:
        logger.error(f"Error in download process: {e}")
        
        with active_downloads_lock:
            if download_key in active_downloads:
                active_downloads[download_key]["status"] = "error"
                active_downloads[download_key]["error"] = str(e)
        
        stats.increment_failed_download()
        return None, f"Error downloading video: {str(e)}"

def process_video(video_id, model_size=DEFAULT_MODEL):
    """Process a TikTok video (download, extract audio, transcribe)."""
    # Generate cache key
    cache_key = generate_cache_key(video_id, model_size)
    
    # Check if we already have the results cached
    cache_file = os.path.join(TEMP_DIR, f"{cache_key}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                cached_result = json.load(f)
            logger.info(f"Using cached transcription result for video ID: {video_id}")
            return cached_result, None
        except Exception as e:
            logger.error(f"Error reading cache file: {e}")
            # Continue with processing if cache read fails
    
    # Track this processing
    process_key = f"process_{video_id}_{int(time.time())}"
    with active_downloads_lock:
        active_downloads[process_key] = {
            "status": "processing",
            "progress": 0,
            "video_id": video_id,
            "start_time": time.time()
        }
    
    try:
        stats.increment_total_transcription()
        
        # Step 1: Download the video
        with active_downloads_lock:
            if process_key in active_downloads:
                active_downloads[process_key]["status"] = "downloading"
                
        video_path, error = download_tiktok_video(video_id)
        if not video_path:
            logger.error(f"Failed to download video: {error}")
            
            with active_downloads_lock:
                if process_key in active_downloads:
                    active_downloads[process_key]["status"] = "failed"
                    active_downloads[process_key]["error"] = error
            
            stats.increment_failed_transcription()
            return None, error
        
        # Step 2: Extract audio
        with active_downloads_lock:
            if process_key in active_downloads:
                active_downloads[process_key]["status"] = "extracting_audio"
                active_downloads[process_key]["progress"] = 40
                
        audio_path = extract_audio_from_video(video_path, video_id)
        if not audio_path:
            logger.error("Failed to extract audio from video")
            clean_up_files(video_path)
            
            with active_downloads_lock:
                if process_key in active_downloads:
                    active_downloads[process_key]["status"] = "failed"
                    active_downloads[process_key]["error"] = "Failed to extract audio"
            
            stats.increment_failed_transcription()
            return None, "Failed to extract audio from video"
        
        # Step 3: Transcribe audio
        with active_downloads_lock:
            if process_key in active_downloads:
                active_downloads[process_key]["status"] = "transcribing"
                active_downloads[process_key]["progress"] = 60
                
        transcription = transcribe_audio(audio_path, model_size)
        if not transcription:
            logger.error("Failed to transcribe audio")
            clean_up_files(video_path, audio_path)
            
            with active_downloads_lock:
                if process_key in active_downloads:
                    active_downloads[process_key]["status"] = "failed"
                    active_downloads[process_key]["error"] = "Failed to transcribe audio"
            
            stats.increment_failed_transcription()
            return None, "Failed to transcribe audio"
        
        # Step 4: Create result
        result = {
            "video_id": video_id,
            "transcription": transcription,
            "model_used": model_size,
            "timestamp": datetime.now().isoformat()
        }
        
        # Cache the result
        try:
            with open(cache_file, 'w') as f:
                json.dump(result, f)
        except Exception as e:
            logger.error(f"Error writing cache file: {e}")
        
        # Clean up
        clean_up_files(video_path, audio_path)
        
        with active_downloads_lock:
            if process_key in active_downloads:
                active_downloads[process_key]["status"] = "completed"
                active_downloads[process_key]["progress"] = 100
        
        stats.increment_successful_transcription()
        return result, None
        
    except Exception as e:
        logger.error(f"Error processing video: {e}")
        
        with active_downloads_lock:
            if process_key in active_downloads:
                active_downloads[process_key]["status"] = "error"
                active_downloads[process_key]["error"] = str(e)
        
        stats.increment_failed_transcription()
        return None, f"Error processing video: {str(e)}"

# API Routes
@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get server statistics."""
    # Run cache maintenance
    maintain_cache()
    
    # Get disk usage
    total_size = 0
    file_count = 0
    for root, dirs, files in os.walk(TEMP_DIR):
        for file in files:
            file_path = os.path.join(root, file)
            if os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                total_size += file_size
                file_count += 1
    
    # Get active downloads
    with active_downloads_lock:
        # Remove completed downloads older than 5 minutes
        current_time = time.time()
        to_remove = []
        for key, download in active_downloads.items():
            if download.get("status") in ["completed", "failed", "error"]:
                if current_time - download.get("start_time", 0) > 300:  # 5 minutes
                    to_remove.append(key)
        
        for key in to_remove:
            active_downloads.pop(key, None)
        
        active_count = len([d for d in active_downloads.values() if d.get("status") not in ["completed", "failed", "error"]])
    
    return jsonify({
        "server": {
            "status": "running",
            "uptime": time.time() - app.start_time if hasattr(app, 'start_time') else 0,
            "cache": {
                "size_bytes": total_size,
                "size_mb": total_size / (1024 * 1024),
                "file_count": file_count,
                "max_size_mb": MAX_CACHE_SIZE_MB
            },
            "active_processes": active_count
        },
        "operations": stats.get_stats()
    })

@app.route('/api/transcribe', methods=['POST'])
def transcribe_video():
    """Transcribe a TikTok video."""
    # Parse request
    try:
        request_data = request.get_json()
        
        # Validate request
        if not request_data:
            return jsonify({"error": "Invalid request data"}), 400
            
        # Get URL or video ID
        url = request_data.get('url')
        video_id = request_data.get('video_id')
        
        # Get CAPTCHA token
        token = request_data.get('token')
        if not token:
            return jsonify({"error": "CAPTCHA verification required"}), 403
            
        # Verify token
        token_valid, token_errors = verify_turnstile_token(token, request.remote_addr)
        if not token_valid:
            return jsonify({"error": "Invalid CAPTCHA token", "details": token_errors}), 403
        
        # Get model size (optional)
        model_size = request_data.get('model_size', DEFAULT_MODEL)
        if model_size not in VOSK_MODELS:
            return jsonify({"error": f"Invalid model size. Choose from: {', '.join(VOSK_MODELS.keys())}"}), 400
        
        # Extract video ID from URL if provided
        if url and not video_id:
            if not is_valid_tiktok_url(url):
                return jsonify({"error": "Invalid TikTok URL"}), 400
                
            video_id = extract_video_id(url)
            
            if not video_id:
                return jsonify({"error": "Could not extract video ID from URL"}), 400
                
        if not video_id:
            return jsonify({"error": "Either URL or video_id is required"}), 400
            
        # Process the video
        result, error = process_video(video_id, model_size)
        
        if error:
            return jsonify({"error": error}), 500
            
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Error in transcribe endpoint: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/check', methods=['POST'])
def check_video():
    """Check status of video processing."""
    try:
        request_data = request.get_json()
        
        # Validate request
        if not request_data:
            return jsonify({"error": "Invalid request data"}), 400
            
        # Get video ID
        video_id = request_data.get('video_id')
        
        if not video_id:
            return jsonify({"error": "video_id is required"}), 400
        
        # Get model size (optional)
        model_size = request_data.get('model_size', DEFAULT_MODEL)
        
        # Check if we have a cached result
        cache_key = generate_cache_key(video_id, model_size)
        cache_file = os.path.join(TEMP_DIR, f"{cache_key}.json")
        
        if os.path.exists(cache_file):
            return jsonify({
                "status": "completed",
                "cached": True
            }), 200
        
        # Check if video is currently being processed
        with active_downloads_lock:
            for key, download in active_downloads.items():
                if download.get("video_id") == video_id and download.get("status") not in ["completed", "failed", "error"]:
                    return jsonify({
                        "status": download.get("status", "processing"),
                        "progress": download.get("progress", 0),
                        "cached": False
                    }), 200
        
        # Not found
        return jsonify({
            "status": "not_found",
            "cached": False
        }), 404
        
    except Exception as e:
        logger.error(f"Error in check endpoint: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/audio/<video_id>', methods=['GET'])
def get_audio(video_id):
    """Get audio file for a video."""
    try:
        # Check if we already have the audio
        audio_path = os.path.join(TEMP_DIR, f"{video_id}.wav")
        
        if not os.path.exists(audio_path):
            # Download and process the video to get audio
            video_path, error = download_tiktok_video(video_id)
            
            if not video_path:
                return jsonify({"error": error or "Failed to download video"}), 500
                
            audio_path = extract_audio_from_video(video_path, video_id)
            
            if not audio_path:
                clean_up_files(video_path)
                return jsonify({"error": "Failed to extract audio"}), 500
                
            # Clean up video file
            clean_up_files(video_path)
        
        return send_file(audio_path, mimetype='audio/wav')
        
    except Exception as e:
        logger.error(f"Error in audio endpoint: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/list_models', methods=['GET'])
def list_models():
    """List available transcription models."""
    return jsonify({
        "available_models": list(VOSK_MODELS.keys()),
        "default_model": DEFAULT_MODEL,
        "models": {
            "small": {
                "name": VOSK_MODELS["small"],
                "description": "Small model (70MB) - Faster but less accurate",
                "size_mb": 70
            },
            "medium": {
                "name": VOSK_MODELS["medium"],
                "description": "Medium model (500MB) - Balance of speed and accuracy",
                "size_mb": 500
            },
            "large": {
                "name": VOSK_MODELS["large"],
                "description": "Large model (1.5GB) - Most accurate but slower",
                "size_mb": 1500
            }
        }
    })

@app.route('/api/status', methods=['GET'])
def get_active_downloads():
    """Get the status of active downloads and processes."""
    with active_downloads_lock:
        active = {k: v for k, v in active_downloads.items() 
                 if v.get("status") not in ["completed", "failed", "error"] or 
                    time.time() - v.get("start_time", 0) < 300}  # Show completed ones for 5 minutes
    
    return jsonify({
        "active_processes": len(active),
        "processes": active
    })

# Start the application
if __name__ == '__main__':
    # Record start time
    app.start_time = time.time()
    
    # Clean up old files on startup
    maintain_cache()
    
    # Start the server
    logger.info("Starting TikTok Transcriber Server")
    app.run(host='0.0.0.0', port=5000, debug=False)
