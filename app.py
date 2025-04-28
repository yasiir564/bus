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
import threading
from datetime import datetime
from vosk import Model, KaldiRecognizer, SetLogLevel
import wave
import gc
import sys

# Check if ffmpeg is installed
try:
    subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
except (FileNotFoundError, subprocess.CalledProcessError):
    print("Error: ffmpeg is not installed or not in PATH. Please install ffmpeg.")
    exit(1)

# Configure to use the small Vosk model instead of the larger one
VOSK_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vosk-model-small-en-us-0.15")
if not os.path.exists(VOSK_MODEL_PATH):
    print("Small Vosk model not found. Downloading model...")
    try:
        import urllib.request
        import zipfile
        
        # Use the smaller model instead of the 0.22 version
        model_url = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
        zip_path = os.path.join(tempfile.gettempdir(), "vosk-model-small.zip")
        
        # Download the model
        print("Downloading small Vosk model...")
        urllib.request.urlretrieve(model_url, zip_path)
        
        # Extract the model
        print("Extracting Vosk model...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(os.path.dirname(os.path.abspath(__file__)))
        
        os.remove(zip_path)
        print("Small Vosk model downloaded and extracted successfully.")
    except Exception as e:
        print(f"Error downloading Vosk model: {e}")
        print("Please download the model manually from https://alphacephei.com/vosk/models")
        exit(1)

app = Flask(__name__)
# Configure CORS to allow specific origins in production or any in development
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Set up logging with rotation to avoid huge log files
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            "tiktok_transcriber.log", 
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3
        ),
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
CACHE_SIZE = 100  # Reduced from 200
CACHE_EXPIRATION = 43200  # 12 hours instead of 24
MAX_CACHE_SIZE_MB = 1000  # 1GB maximum cache size instead of 5GB

# List of user agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
]

# List of cookies to rotate
TT_COOKIES = [
    "tt_webid_v2=123456789012345678; tt_webid=123456789012345678; ttwid=1%7CAbC123dEf456gHi789jKl%7C1600000000%7Cabcdef0123456789abcdef0123456789; msToken=AbC123dEf456gHi789jKl",
    "tt_webid_v2=234567890123456789; tt_webid=234567890123456789; ttwid=1%7CBcD234eFg567hIj890kLm%7C1600100000%7Cbcdefg1234567890abcdef0123456789; msToken=BcD234eFg567hIj890kLm"
]

# Proxy configuration (optional)
PROXIES = None

# Download rate limiting
MAX_DOWNLOADS_PER_MINUTE = 10  # Reduced from 20
download_timestamps = []
download_lock = threading.Lock()

# Track active downloads
active_downloads = {}
active_downloads_lock = threading.Lock()

# Track active transcriptions
active_transcriptions = {}
active_transcriptions_lock = threading.Lock()

# Cache for transcriptions - using a simpler structure to reduce memory
transcription_cache = {}
transcription_cache_expiry = {}
transcription_cache_lock = threading.Lock()

# Create a single shared Vosk model instance instead of loading it every time
vosk_model = None
vosk_model_lock = threading.Lock()

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
        response = requests.head(url, allow_redirects=True, timeout=5, headers=headers)
        return response.url
    except Exception as e:
        logger.error(f"Error expanding shortened URL: {e}")
        return url

def extract_video_id(url):
    """Extract the video ID from a TikTok URL."""
    # Handle shortened URLs
    if any(domain in url for domain in ["vm.tiktok.com", "vt.tiktok.com"]):
        url = expand_shortened_url(url)
    
    # Extract video ID from URL using regex
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
        "Connection": "keep-alive",
    }
    
    if referer:
        headers["Referer"] = referer
    
    # Add cookies to some requests for better undetectability
    if random.random() < 0.7:  # 70% chance to add cookies
        headers["Cookie"] = get_random_cookies()
    
    return headers

# Optimized download function with smaller LRU cache size
@lru_cache(maxsize=50)  # Reduced from 200
def download_tiktok_video(video_id):
    """Attempt to download TikTok video using the most efficient method first"""
    methods = [
        download_tiktok_video_mobile,
        download_tiktok_video_embed,
        download_tiktok_video_web,
        download_tiktok_video_scraper
    ]
    
    for method in methods:
        try:
            video_path = method(video_id)
            if video_path and os.path.exists(video_path) and os.path.getsize(video_path) > 10000:
                return video_path
        except Exception as e:
            logger.error(f"Error in {method.__name__}: {e}")
    
    return None

@lru_cache(maxsize=50)  # Reduced cache size
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
            timeout=10,  # Reduced timeout
            proxies=PROXIES
        )
        
        if response.status_code != 200:
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
                video_url = matches[0]
                video_url = video_url.replace('\\u002F', '/').replace('\\', '')
                break
        
        if not video_url:
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
            timeout=15,  # Reduced timeout
            proxies=PROXIES
        )
        
        if video_response.status_code not in [200, 206]:
            return None
        
        # Create a temporary file
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file using smaller chunks
        total_size = 0
        with open(temp_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=4096):  # Smaller chunks
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
        
        if total_size < 10000:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            return None
            
        return temp_file
        
    except Exception as e:
        logger.error(f"Error downloading video (mobile): {e}")
        return None

@lru_cache(maxsize=25)  # Reduced cache size
def download_tiktok_video_web(video_id):
    """Alternative method using web API."""
    try:
        web_url = f"https://www.tiktok.com/api/item/detail/?itemId={video_id}"
        
        headers = get_random_request_headers(referer=f"https://www.tiktok.com/video/{video_id}")
        headers.update({
            "Accept": "application/json, text/plain, */*"
        })
        
        logger.info(f"Fetching TikTok web API: {web_url}")
        response = requests.get(
            web_url, 
            headers=headers, 
            timeout=10,  # Reduced timeout
            proxies=PROXIES
        )
        
        if response.status_code != 200:
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
                    timeout=15,
                    proxies=PROXIES
                )
                
                if video_response.status_code != 200:
                    return None
                
                # Create a temporary file
                temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file with smaller chunks
                with open(temp_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=4096):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(temp_file) < 10000:
                    os.remove(temp_file)
                    return None
                    
                return temp_file
            else:
                return None
                
        except json.JSONDecodeError:
            return None
            
    except Exception as e:
        logger.error(f"Error in web API method: {e}")
        return None

@lru_cache(maxsize=25)
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
            timeout=10,  # Reduced timeout
            proxies=PROXIES
        )
        
        if response.status_code != 200:
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
                break
        
        if not video_url:
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
            timeout=15,
            proxies=PROXIES
        )
        
        if video_response.status_code != 200:
            return None
        
        # Create a temporary file
        temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
        
        # Stream the video to the file with smaller chunks
        with open(temp_file, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)
        
        if os.path.getsize(temp_file) < 10000:
            os.remove(temp_file)
            return None
            
        return temp_file
        
    except Exception as e:
        logger.error(f"Error in embed method: {e}")
        return None

# Smallest cache for the most resource-intensive method
@lru_cache(maxsize=10)
def download_tiktok_video_scraper(video_id):
    """Try downloading using a more sophisticated approach."""
    try:
        # Build the direct video URL
        url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
        headers = get_random_request_headers()
        
        logger.info(f"Fetching TikTok page with scraper method: {url}")
        response = requests.get(
            url, 
            headers=headers, 
            timeout=15,
            proxies=PROXIES
        )
        
        if response.status_code != 200:
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
                            # Download the video
                            video_headers = get_random_request_headers(referer=url)
                            video_response = requests.get(
                                video_url, 
                                headers=video_headers, 
                                stream=True, 
                                timeout=15,
                                proxies=PROXIES
                            )
                            
                            if video_response.status_code != 200:
                                return None
                            
                            # Create a temporary file
                            temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                            
                            # Stream the video to the file
                            with open(temp_file, 'wb') as f:
                                for chunk in video_response.iter_content(chunk_size=4096):
                                    if chunk:
                                        f.write(chunk)
                            
                            if os.path.getsize(temp_file) < 10000:
                                os.remove(temp_file)
                                return None
                                
                            return temp_file
            except json.JSONDecodeError:
                pass
        
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
                
                # Download the video
                video_headers = get_random_request_headers(referer=url)
                video_response = requests.get(
                    video_url, 
                    headers=video_headers, 
                    stream=True, 
                    timeout=15,
                    proxies=PROXIES
                )
                
                if video_response.status_code != 200:
                    continue
                
                # Create a temporary file
                temp_file = os.path.join(TEMP_DIR, f"{video_id}.mp4")
                
                # Stream the video to the file
                with open(temp_file, 'wb') as f:
                    for chunk in video_response.iter_content(chunk_size=4096):
                        if chunk:
                            f.write(chunk)
                
                if os.path.getsize(temp_file) < 10000:
                    os.remove(temp_file)
                    continue
                    
                return temp_file
        
        return None
        
    except Exception as e:
        logger.error(f"Error in scraper method: {e}")
        return None

def extract_audio_from_video(video_path, video_id):
    """Extract audio from video using ffmpeg with optimized settings."""
    try:
        audio_path = os.path.join(TEMP_DIR, f"{video_id}.wav")
        
        # Optimized FFmpeg command:
        # - Lower audio quality (8kHz sample rate is sufficient for speech recognition)
        # - Use mono audio
        # - Use PCM_S16LE codec which is well supported and efficient
        cmd = [
            'ffmpeg',
            '-y',
            '-i', video_path,
            '-vn',
            '-ar', '8000',  # Lower sample rate to 8kHz - sufficient for speech recognition and smaller file
            '-ac', '1',  # Mono audio
            '-acodec', 'pcm_s16le',  # Simple PCM codec
            '-f', 'wav',
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

def load_vosk_model():
    """Lazy load the Vosk model once and reuse it."""
    global vosk_model
    
    with vosk_model_lock:
        if vosk_model is None:
            logger.info(f"Loading Vosk model from {VOSK_MODEL_PATH}")
            vosk_model = Model(VOSK_MODEL_PATH)
    
    return vosk_model

def transcribe_audio(audio_path, video_id):
    """Transcribe audio using Vosk API with memory optimizations."""
    try:
        # Check if audio file exists and is valid
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            logger.error(f"Audio file invalid or too small: {audio_path}")
            return None
        
        # Load model only once
        model = load_vosk_model()
        
        # Open audio file
        wf = wave.open(audio_path, "rb")
        
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
            logger.error("Audio file must be WAV format mono PCM")
            wf.close()
            return None
        
        # Create recognizer
        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(True)
        
        # Use smaller chunks for processing to reduce memory usage
        results = []
        chunk_size = 2000  # Smaller chunk size (was 4000)
        
        while True:
            data = wf.readframes(chunk_size)
            if len(data) == 0:
                break
                
            if rec.AcceptWaveform(data):
                part_result = json.loads(rec.Result())
                if part_result.get("text", "").strip():
                    results.append(part_result)
            
            # Periodically force garbage collection to free memory
            if random.random() < 0.1:  # 10% chance each chunk
                gc.collect()
        
        # Get final result
        part_result = json.loads(rec.FinalResult())
        if part_result.get("text", "").strip():
            results.append(part_result)
        
        # Close the wave file
        wf.close()
        
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
        
        # Force garbage collection after processing
        gc.collect()
        
        return {
            "transcript": transcript,
            "full_text": full_text
        }
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return None
    finally:
        # Make sure wave file is closed in case of error
        try:
            if 'wf' in locals() and wf:
                wf.close()
        except:
            pass

def download_and_transcribe(url):
    """Main function to download and transcribe TikTok video."""
    try:
        # Extract video ID from URL
        video_id = extract_video_id(url)
        if not video_id:
            return {"error": "Invalid TikTok URL. Could not extract video ID."}, 400
        
        # Create a unique job ID
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
            return {"job_id": job_id, "status": "rate_limited", "message": "Too many requests. Please try again later."}, 429
        
        # Start transcription in separate thread
        threading.Thread(
            target=process_transcription_job,
            args=(job_id, video_id, url),
            daemon=True
        ).start()
        
        return {"job_id": job_id, "status": "processing", "message": "Processing your request"}, 202
    except Exception as e:
        logger.error(f"Error in download_and_transcribe: {e}")
        return {"error": "Server error while processing your request", "details": str(e)}, 500

def process_transcription_job(job_id, video_id, url):
    """Process the transcription job in a separate thread."""
    try:
        stats.increment_total_downloads()
        
        # Update status
        with active_downloads_lock:
            if job_id in active_downloads:
                active_downloads[job_id]["status"] = "downloading"
        
        # Download the video
        logger.info(f"Downloading TikTok video: {url}")
        video_path = download_tiktok_video(video_id)
        
        if not video_path or not os.path.exists(video_path):
            logger.error(f"Failed to download video: {url}")
            with active_downloads_lock:
                if job_id in active_downloads:
                    active_downloads[job_id]["status"] = "download_failed"
                    active_downloads[job_id]["end_time"] = time.time()
            stats.increment_failed_downloads()
            return
        
        stats.increment_success_downloads()
        
        # Update status
        with active_downloads_lock:
            if job_id in active_downloads:
                active_downloads[job_id]["status"] = "extracting_audio"
        
        # Extract audio
        logger.info(f"Extracting audio from video: {video_path}")
        audio_path = extract_audio_from_video(video_path, video_id)
        
        if not audio_path or not os.path.exists(audio_path):
            logger.error("Failed to extract audio")
            with active_downloads_lock:
                if job_id in active_downloads:
                    active_downloads[job_id]["status"] = "audio_extraction_failed"
                    active_downloads[job_id]["end_time"] = time.time()
            return
        
        # Update status
        with active_downloads_lock:
            if job_id in active_downloads:
                active_downloads[job_id]["status"] = "transcribing"
        
        with active_transcriptions_lock:
            active_transcriptions[job_id] = {
                "video_id": video_id,
                "status": "transcribing",
                "start_time": time.time()
            }
        
        stats.increment_total_transcriptions()
        
        # Transcribe audio
        logger.info(f"Transcribing audio: {audio_path}")
        transcript_data = transcribe_audio(audio_path, video_id)
        
        if not transcript_data:
            logger.error("Failed to transcribe audio")
            with active_transcriptions_lock:
                if job_id in active_transcriptions:
                    active_transcriptions[job_id]["status"] = "transcription_failed"
                    active_transcriptions[job_id]["end_time"] = time.time()
            stats.increment_failed_transcriptions()
            return
        
        stats.increment_success_transcriptions()
        
        # Store result in cache
        with transcription_cache_lock:
            # Check if cache is too large and remove oldest entries if necessary
            while len(transcription_cache) > CACHE_SIZE:
                oldest_key = min(transcription_cache_expiry, key=transcription_cache_expiry.get)
                del transcription_cache[oldest_key]
                del transcription_cache_expiry[oldest_key]
            
            # Check total cache size in MB
            cache_size_mb = sum(sys.getsizeof(json.dumps(v)) for v in transcription_cache.values()) / (1024 * 1024)
            
            if cache_size_mb > MAX_CACHE_SIZE_MB:
                # Remove oldest entries until cache is under size limit
                keys_sorted_by_time = sorted(transcription_cache_expiry, key=transcription_cache_expiry.get)
                
                for key in keys_sorted_by_time:
                    del transcription_cache[key]
                    del transcription_cache_expiry[key]
                    
                    cache_size_mb = sum(sys.getsizeof(json.dumps(v)) for v in transcription_cache.values()) / (1024 * 1024)
                    if cache_size_mb <= MAX_CACHE_SIZE_MB:
                        break
            
            # Store new result
            transcription_cache[video_id] = transcript_data
            transcription_cache_expiry[video_id] = time.time()
        
        # Update status
        with active_transcriptions_lock:
            if job_id in active_transcriptions:
                active_transcriptions[job_id]["status"] = "completed"
                active_transcriptions[job_id]["end_time"] = time.time()
        
        with active_downloads_lock:
            if job_id in active_downloads:
                active_downloads[job_id]["status"] = "completed"
                active_downloads[job_id]["end_time"] = time.time()
        
        logger.info(f"Successfully transcribed video {video_id}")
        
        # Clean up temporary files with some delay to prevent issues
        threading.Timer(60, cleanup_files, args=[video_path, audio_path]).start()
        
    except Exception as e:
        logger.error(f"Error in process_transcription_job: {e}")
        with active_downloads_lock:
            if job_id in active_downloads:
                active_downloads[job_id]["status"] = "error"
                active_downloads[job_id]["error"] = str(e)
                active_downloads[job_id]["end_time"] = time.time()
        
        with active_transcriptions_lock:
            if job_id in active_transcriptions:
                active_transcriptions[job_id]["status"] = "error"
                active_transcriptions[job_id]["error"] = str(e)
                active_transcriptions[job_id]["end_time"] = time.time()

def cleanup_files(video_path, audio_path):
    """Clean up temporary files."""
    try:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
    except Exception as e:
        logger.error(f"Error cleaning up files: {e}")

def clean_temp_directory():
    """Clean up old temporary files."""
    try:
        current_time = time.time()
        for filename in os.listdir(TEMP_DIR):
            filepath = os.path.join(TEMP_DIR, filename)
            file_mod_time = os.path.getmtime(filepath)
            
            # Delete files older than 24 hours
            if current_time - file_mod_time > 86400:
                try:
                    os.remove(filepath)
                    logger.info(f"Deleted old temporary file: {filepath}")
                except Exception as e:
                    logger.error(f"Error deleting temporary file {filepath}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning temp directory: {e}")

# Set up API endpoints
@app.route('/api/transcribe', methods=['POST'])
def transcribe_route():
    """Endpoint to transcribe a TikTok video."""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({"error": "Missing URL parameter"}), 400
        
        url = data['url']
        if not is_valid_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        result, status_code = download_and_transcribe(url)
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Error in transcribe route: {e}")
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/job/<job_id>', methods=['GET'])
def job_status_route(job_id):
    """Endpoint to check the status of a transcription job."""
    try:
        # Check if job is in downloads
        with active_downloads_lock:
            if job_id in active_downloads:
                job_info = active_downloads[job_id].copy()
                
                # Check if job is completed or has transcription data
                with transcription_cache_lock:
                    video_id = job_info.get("video_id")
                    if video_id and video_id in transcription_cache:
                        job_info["result"] = transcription_cache[video_id]
                
                return jsonify(job_info), 200
        
        # Check if job is in transcriptions
        with active_transcriptions_lock:
            if job_id in active_transcriptions:
                job_info = active_transcriptions[job_id].copy()
                
                # Check if job is completed
                if job_info.get("status") == "completed":
                    with transcription_cache_lock:
                        video_id = job_info.get("video_id")
                        if video_id and video_id in transcription_cache:
                            job_info["result"] = transcription_cache[video_id]
                
                return jsonify(job_info), 200
        
        return jsonify({"error": "Job not found"}), 404
    except Exception as e:
        logger.error(f"Error in job status route: {e}")
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def stats_route():
    """Endpoint to get server statistics."""
    try:
        # Get cache statistics
        with transcription_cache_lock:
            cache_size = len(transcription_cache)
            cache_size_mb = sum(sys.getsizeof(json.dumps(v)) for v in transcription_cache.values()) / (1024 * 1024)
        
        # Get active jobs
        with active_downloads_lock:
            active_download_count = len(active_downloads)
        
        with active_transcriptions_lock:
            active_transcription_count = len(active_transcriptions)
        
        # Get memory usage
        import psutil
        process = psutil.Process(os.getpid())
        memory_usage_mb = process.memory_info().rss / (1024 * 1024)
        
        # Get server stats
        server_stats = {
            "server_time": datetime.now().isoformat(),
            "uptime": time.time() - process.create_time(),
            "memory_usage_mb": memory_usage_mb,
            "cache": {
                "size": cache_size,
                "size_mb": cache_size_mb,
                "max_size": CACHE_SIZE,
                "max_size_mb": MAX_CACHE_SIZE_MB
            },
            "active_jobs": {
                "downloads": active_download_count,
                "transcriptions": active_transcription_count
            }
        }
        
        # Add download and transcription stats
        server_stats.update(stats.get_stats())
        
        return jsonify(server_stats), 200
    except Exception as e:
        logger.error(f"Error in stats route: {e}")
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "ok"}), 200

# Background task for cleanup
def start_cleanup_scheduler():
    """Start a background thread to clean up temporary files periodically."""
    def cleanup_task():
        while True:
            try:
                clean_temp_directory()
                time.sleep(3600)  # Run every hour
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
                time.sleep(3600)  # Sleep and try again
    
    cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()

# Initialize cache cleanup job
start_cleanup_scheduler()

# Main entry point
if __name__ == '__main__':
    try:
        # Import psutil if not already imported
        import psutil
    except ImportError:
        print("Warning: psutil not installed. Memory stats will not be available.")
        print("Install with: pip install psutil")
    
    # Start the server
    port = int(os.environ.get("PORT", 5000))
    
    print(f"Starting TikTok Transcriber API on port {port}")
    print(f"Using Vosk model: {VOSK_MODEL_PATH}")
    print(f"Temporary directory: {TEMP_DIR}")
    
    app.run(host='0.0.0.0', port=port, threaded=True)
