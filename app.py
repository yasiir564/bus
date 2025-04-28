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
from vosk import Model, KaldiRecognizer, SetLogLevel
import wave
import sys

# Check if ffmpeg is installed
try:
    subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
except (FileNotFoundError, subprocess.CalledProcessError):
    print("Error: ffmpeg is not installed or not in PATH. Please install ffmpeg.")
    exit(1)

# Check if Vosk model exists, if not download it
VOSK_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vosk-model-en-us-0.22")
if not os.path.exists(VOSK_MODEL_PATH):
    print("Vosk model not found. Downloading model...")
    try:
        import urllib.request
        import zipfile
        
        model_url = "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip"
        zip_path = os.path.join(tempfile.gettempdir(), "vosk-model.zip")
        
        # Download the model
        print("Downloading Vosk model...")
        urllib.request.urlretrieve(model_url, zip_path)
        
        # Extract the model
        print("Extracting Vosk model...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(os.path.dirname(os.path.abspath(__file__)))
        
        os.remove(zip_path)
        print("Vosk model downloaded and extracted successfully.")
    except Exception as e:
        print(f"Error downloading Vosk model: {e}")
        print("Please download the model manually from https://alphacephei.com/vosk/models")
        exit(1)

app = Flask(__name__)
# Configure CORS to allow specific origins in production or any in development
CORS(app, resources={r"/api/*": {"origins": "*"}})

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

# Set Vosk log level
SetLogLevel(-1)  # -1 to disable Vosk logs

# Configure temp directory for file storage
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'tiktok_transcriber')
os.makedirs(TEMP_DIR, exist_ok=True)

# Set cache size and expiration time (in seconds)
CACHE_SIZE = 200
CACHE_EXPIRATION = 86400  # 24 hours
MAX_CACHE_SIZE_MB = 5000  # 5GB maximum cache size

# List of user agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/119.0.6045.109 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.80 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
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

# Track active transcriptions
active_transcriptions = {}
active_transcriptions_lock = threading.Lock()

# Cache for transcriptions
transcription_cache = {}
transcription_cache_expiry = {}
transcription_cache_lock = threading.Lock()

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
        
    def increment_total_downloads(self):
        with self.lock:
            self.total_downloads += 1
            self.last_download_time = datetime.now()
            
    def increment_success_downloads(self):
        with self.lock:
            self.successful_downloads += 1
            
    def increment_failed_downloads(self):
        with self.lock:
            self.failed_downloads += 1
            
    def increment_total_transcriptions(self):
        with self.lock:
            self.total_transcriptions += 1
            self.last_transcription_time = datetime.now()
            
    def increment_success_transcriptions(self):
        with self.lock:
            self.successful_transcriptions += 1
            
    def increment_failed_transcriptions(self):
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
    """Extract audio from video using ffmpeg."""
    try:
        audio_path = os.path.join(TEMP_DIR, f"{video_id}.wav")
        
        # FFmpeg command to extract audio
        cmd = [
            'ffmpeg',
            '-y',  # Overwrite output file without asking
            '-i', video_path,  # Input file
            '-vn',  # No video
            '-ar', '16000',  # Audio sample rate: 16kHz (required by Vosk)
            '-ac', '1',  # Audio channels: mono
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

def transcribe_audio(audio_path, video_id):
    """Transcribe audio using Vosk API."""
    try:
        # Check if audio file exists
        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {audio_path}")
            return None
        
        # Check if audio file is valid
        if os.path.getsize(audio_path) < 1000:  # Too small to be valid
            logger.error(f"Audio file too small: {os.path.getsize(audio_path)} bytes")
            return None
        
        logger.info(f"Loading Vosk model from {VOSK_MODEL_PATH}")
        model = Model(VOSK_MODEL_PATH)
        
        logger.info(f"Opening audio file: {audio_path}")
        wf = wave.open(audio_path, "rb")
        
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
            logger.error("Audio file must be WAV format mono PCM")
            return None
        
        logger.info(f"Creating recognizer with sample rate {wf.getframerate()}")
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(True)  # Include timestamps
        
        results = []
        while True:
            data = wf.readframes(4000)  # Read 4000 frames at a time
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                part_result = json.loads(rec.Result())
                if part_result.get("text", "").strip():
                    results.append(part_result)
        
        # Get final result
        part_result = json.loads(rec.FinalResult())
        if part_result.get("text", "").strip():
            results.append(part_result)
        
        # Process results to create combined transcript with timestamps
        transcript = []
        for res in results:
            text = res.get("text", "").strip()
            if text:
                if "result" in res:
                   # Get timestamp of first and last word
                    start_time = res["result"][0]["start"]
                    end_time = res["result"][-1]["end"]
                    transcript.append({
                        "text": text,
                        "start": start_time,
                        "end": end_time
                    })
                else:
                    # No timestamps available
                    transcript.append({
                        "text": text,
                        "start": None,
                        "end": None
                    })
        
        if not transcript:
            logger.warning("No transcript generated")
            return {"transcript": [], "full_text": ""}
        
        # Combine all text for full transcript
        full_text = " ".join([item["text"] for item in transcript])
        
        return {
            "transcript": transcript,
            "full_text": full_text
        }
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return None

def download_and_transcribe(url):
    """Main function to download and transcribe TikTok video."""
    try:
        # Extract video ID from URL
        video_id = extract_video_id(url)
        if not video_id:
            return {"error": "Invalid TikTok URL. Could not extract video ID."}, 400
        
        # Create a unique job ID for this transcription
        job_id = hashlib.md5(f"{video_id}_{time.time()}".encode()).hexdigest()
        
        # Check if transcription is in cache
        with transcription_cache_lock:
            if video_id in transcription_cache:
                # Check if cache is still valid
                if time.time() - transcription_cache_expiry.get(video_id, 0) < CACHE_EXPIRATION:
                    logger.info(f"Using cached transcription for video {video_id}")
                    return {"job_id": job_id, "status": "completed", "result": transcription_cache[video_id]}
        
        # Track this download/transcription job
        with active_downloads_lock:
            active_downloads[job_id] = {
                "video_id": video_id,
                "url": url,
                "status": "downloading",
                "start_time": time.time()
            }
        
        # Check rate limiting
        if not can_perform_download():
            with active_downloads_lock:
                active_downloads[job_id]["status"] = "rate_limited"
            return {"job_id": job_id, "status": "rate_limited", "message": "Rate limit exceeded. Try again later."}, 429
        
        # Start download in a separate thread
        thread = threading.Thread(target=process_download_and_transcribe, args=(url, video_id, job_id))
        thread.daemon = True
        thread.start()
        
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "Download and transcription started"
        }
    except Exception as e:
        logger.error(f"Error in download_and_transcribe: {e}")
        return {"error": str(e)}, 500

def process_download_and_transcribe(url, video_id, job_id):
    """Process download and transcription in background."""
    try:
        stats.increment_total_downloads()
        
        logger.info(f"Starting download for video {video_id}")
        
        # Try different download methods
        video_path = None
        methods = [
            download_tiktok_video_mobile,
            download_tiktok_video_web,
            download_tiktok_video_embed,
            download_tiktok_video_scraper
        ]
        
        for method in methods:
            if video_path:
                break
                
            with active_downloads_lock:
                active_downloads[job_id]["status"] = f"downloading_method_{methods.index(method) + 1}"
                
            video_path = method(video_id)
            
            if video_path:
                logger.info(f"Successfully downloaded video using method {methods.index(method) + 1}")
                break
        
        if not video_path:
            logger.error(f"All download methods failed for video {video_id}")
            with active_downloads_lock:
                active_downloads[job_id]["status"] = "download_failed"
                active_downloads[job_id]["end_time"] = time.time()
            stats.increment_failed_downloads()
            return
        
        stats.increment_success_downloads()
        
        # Extract audio
        with active_downloads_lock:
            active_downloads[job_id]["status"] = "extracting_audio"
            
        audio_path = extract_audio_from_video(video_path, video_id)
        
        if not audio_path:
            logger.error(f"Failed to extract audio for video {video_id}")
            with active_downloads_lock:
                active_downloads[job_id]["status"] = "audio_extraction_failed"
                active_downloads[job_id]["end_time"] = time.time()
            return
            
        # Transcribe audio
        with active_downloads_lock:
            active_downloads[job_id]["status"] = "transcribing"
            
        with active_transcriptions_lock:
            active_transcriptions[job_id] = {
                "video_id": video_id,
                "status": "processing",
                "start_time": time.time()
            }
        
        stats.increment_total_transcriptions()
        transcription_result = transcribe_audio(audio_path, video_id)
        
        if not transcription_result:
            logger.error(f"Failed to transcribe audio for video {video_id}")
            with active_downloads_lock:
                active_downloads[job_id]["status"] = "transcription_failed"
                active_downloads[job_id]["end_time"] = time.time()
            with active_transcriptions_lock:
                active_transcriptions[job_id]["status"] = "failed"
                active_transcriptions[job_id]["end_time"] = time.time()
            stats.increment_failed_transcriptions()
            return
            
        stats.increment_success_transcriptions()
        
        # Cache the result
        with transcription_cache_lock:
            transcription_cache[video_id] = transcription_result
            transcription_cache_expiry[video_id] = time.time()
            
            # Clean up old cache entries if needed
            if len(transcription_cache) > CACHE_SIZE:
                # Remove oldest entries
                oldest_key = min(transcription_cache_expiry.keys(), key=lambda k: transcription_cache_expiry[k])
                del transcription_cache[oldest_key]
                del transcription_cache_expiry[oldest_key]
        
        # Update job status
        with active_downloads_lock:
            active_downloads[job_id]["status"] = "completed"
            active_downloads[job_id]["end_time"] = time.time()
            active_downloads[job_id]["result"] = transcription_result
            
        with active_transcriptions_lock:
            active_transcriptions[job_id]["status"] = "completed"
            active_transcriptions[job_id]["end_time"] = time.time()
            active_transcriptions[job_id]["result"] = transcription_result
            
        # Clean up files
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception as e:
            logger.error(f"Error cleaning up files: {e}")
            
    except Exception as e:
        logger.error(f"Error in process_download_and_transcribe: {e}")
        with active_downloads_lock:
            active_downloads[job_id]["status"] = "error"
            active_downloads[job_id]["error"] = str(e)
            active_downloads[job_id]["end_time"] = time.time()

# API Routes
@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "timestamp": time.time(),
        "cache_size": len(transcription_cache),
        "stats": stats.get_stats()
    })

@app.route('/api/transcribe', methods=['POST'])
def transcribe_endpoint():
    """Endpoint to transcribe a TikTok video."""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({"error": "Missing URL parameter"}), 400
            
        url = data['url']
        
        if not is_valid_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
            
        result = download_and_transcribe(url)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in transcribe endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get the status of a transcription job."""
    try:
        with active_downloads_lock:
            if job_id in active_downloads:
                job_info = active_downloads[job_id].copy()
                
                # Don't return everything for in-progress jobs
                if job_info.get("status") != "completed":
                    if "result" in job_info:
                        del job_info["result"]
                
                return jsonify({
                    "job_id": job_id,
                    "status": job_info.get("status", "unknown"),
                    "video_id": job_info.get("video_id"),
                    "start_time": job_info.get("start_time"),
                    "end_time": job_info.get("end_time", None),
                    "result": job_info.get("result", None)
                })
        
        return jsonify({"error": "Job not found"}), 404
    except Exception as e:
        logger.error(f"Error in get_job_status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    """Clear the transcription cache."""
    try:
        with transcription_cache_lock:
            transcription_cache.clear()
            transcription_cache_expiry.clear()
            
        # Also clean up temp directory
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except Exception as e:
                logger.error(f"Error cleaning up file {file_path}: {e}")
                
        return jsonify({"status": "success", "message": "Cache cleared"})
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({"error": str(e)}), 500

# Clean up expired cache periodically
def clean_cache_task():
    """Clean up expired cache entries periodically."""
    while True:
        try:
            # Sleep first to allow the server to start up
            time.sleep(3600)  # Run once per hour
            
            logger.info("Running cache cleanup task")
            current_time = time.time()
            
            # Clean up transcription cache
            with transcription_cache_lock:
                expired_keys = []
                for key, timestamp in transcription_cache_expiry.items():
                    if current_time - timestamp > CACHE_EXPIRATION:
                        expired_keys.append(key)
                
                for key in expired_keys:
                    del transcription_cache[key]
                    del transcription_cache_expiry[key]
                    
                logger.info(f"Removed {len(expired_keys)} expired cache entries")
            
            # Clean up temp files
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        file_mtime = os.path.getmtime(file_path)
                        if current_time - file_mtime > CACHE_EXPIRATION:
                            os.remove(file_path)
                            logger.info(f"Removed expired temporary file: {filename}")
                except Exception as e:
                    logger.error(f"Error cleaning up file {file_path}: {e}")
                    
            # Check temp directory size
            total_size = sum(os.path.getsize(os.path.join(TEMP_DIR, f)) for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f)))
            if total_size > MAX_CACHE_SIZE_MB * 1024 * 1024:
                logger.warning(f"Temp directory size exceeds {MAX_CACHE_SIZE_MB}MB, cleaning up")
                
                # Get list of files sorted by modification time (oldest first)
                files = [(os.path.getmtime(os.path.join(TEMP_DIR, f)), f) 
                         for f in os.listdir(TEMP_DIR) 
                         if os.path.isfile(os.path.join(TEMP_DIR, f))]
                files.sort()
                
                # Remove files until we're below the limit
                for mtime, filename in files:
                    file_path = os.path.join(TEMP_DIR, filename)
                    try:
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        total_size -= file_size
                        logger.info(f"Removed {filename} to free space")
                        
                        if total_size < MAX_CACHE_SIZE_MB * 0.8 * 1024 * 1024:  # Aim for 80% of max
                            break
                    except Exception as e:
                        logger.error(f"Error removing file {file_path}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in clean_cache_task: {e}")

# Start cache cleanup task
cleanup_thread = threading.Thread(target=clean_cache_task)
cleanup_thread.daemon = True
cleanup_thread.start()

if __name__ == '__main__':
    logger.info("Starting TikTok Transcriber API")
    app.run(host='0.0.0.0', port=5000, debug=False)
