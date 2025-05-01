import os
import re
import json
import uuid
import requests
import tempfile
import logging
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import urllib.parse
from urllib.parse import urlparse
import moviepy.editor as mp
import cv2
import numpy as np
from PIL import Image
import io

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for all domains

# Create temp directory for storing files
TEMP_DIR = tempfile.gettempdir()
os.makedirs(os.path.join(TEMP_DIR, "tiktok_downloads"), exist_ok=True)

# User agent to simulate browser
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"


def validate_tiktok_url(url):
    """Validate if the URL is a TikTok URL"""
    parsed = urlparse(url)
    return any(domain in parsed.netloc for domain in ["tiktok.com", "vm.tiktok.com", "www.tiktok.com"])


def get_clean_tiktok_url(url):
    """Convert short URLs to standard format and ensure it's clean"""
    if "vm.tiktok.com" in url or "/v/" in url:
        # Follow redirects for short URLs
        response = requests.head(url, allow_redirects=True)
        url = response.url
    
    # Remove query parameters if present
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    
    return clean_url


def extract_tiktok_id(url):
    """Extract the TikTok ID from the URL"""
    match = re.search(r'/video/(\d+)', url)
    if match:
        return match.group(1)
    return None


def get_tiktok_metadata(url):
    """Get the metadata of the TikTok post"""
    try:
        clean_url = get_clean_tiktok_url(url)
        tiktok_id = extract_tiktok_id(clean_url)
        
        if not tiktok_id:
            return None
        
        # First try the API approach
        api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={tiktok_id}"
        headers = {
            "User-Agent": USER_AGENT
        }
        
        response = requests.get(api_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            
            # Check if we have the required data
            if "aweme_list" in data and len(data["aweme_list"]) > 0:
                item = data["aweme_list"][0]
                
                # Determine content type (video or image)
                content_type = "video"
                download_url = None
                
                # Check for video
                if "video" in item and "play_addr" in item["video"]:
                    download_url = item["video"]["play_addr"]["url_list"][0]
                
                # Check for image/slideshow
                if not download_url and "image_post_info" in item:
                    content_type = "image"
                    if "images" in item["image_post_info"]:
                        # For slideshows, we'll handle the first image for now
                        download_url = item["image_post_info"]["images"][0]["display_image"]["url_list"][0]
                
                if download_url:
                    return {
                        "id": tiktok_id,
                        "content_type": content_type,
                        "download_url": download_url
                    }
        
        # Fallback to webpage scraping if API doesn't work
        # In a production environment, you'd use a proper HTML parser here
        headers = {
            "User-Agent": USER_AGENT
        }
        response = requests.get(clean_url, headers=headers)
        if response.status_code == 200:
            html_content = response.text
            
            # Extract the video URL from the HTML
            # This is a simplified approach, might need updates as TikTok changes
            video_match = re.search(r'"playAddr":"([^"]+)"', html_content)
            if video_match:
                video_url = video_match.group(1).replace('\\u002F', '/').replace('\\', '')
                return {
                    "id": tiktok_id,
                    "content_type": "video",
                    "download_url": video_url
                }
            
            # Check for image content
            image_match = re.search(r'"imageUrl":"([^"]+)"', html_content)
            if image_match:
                image_url = image_match.group(1).replace('\\u002F', '/').replace('\\', '')
                return {
                    "id": tiktok_id,
                    "content_type": "image",
                    "download_url": image_url
                }
                
        return None
    
    except Exception as e:
        logger.error(f"Error in get_tiktok_metadata: {e}")
        return None


def download_content(url, file_path):
    """Download content from the URL to the specified file path"""
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()
        
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return True
    except Exception as e:
        logger.error(f"Error downloading content: {e}")
        return False


def remove_watermark_from_video(input_path, output_path):
    """Remove watermark from a video file"""
    try:
        # Load video
        video = mp.VideoFileClip(input_path)
        
        # Get video dimensions
        width, height = video.size
        
        # Create a mask to remove the watermark
        # TikTok watermarks are usually in the bottom right corner
        # This is a simplified approach - for better results, you'd need
        # more sophisticated watermark detection and removal
        
        # Process the video with a watermark mask
        def remove_watermark(frame):
            # Convert frame to numpy array
            img = np.array(frame)
            
            # Define watermark area (bottom right corner)
            # Adjust these values based on typical TikTok watermark position
            watermark_height = int(height * 0.15)  # 15% of the video height
            watermark_width = int(width * 0.25)    # 25% of the video width
            
            # Create a mask for the bottom right corner
            y_start = height - watermark_height
            x_start = width - watermark_width
            
            # Use inpainting to remove the watermark
            # This is a simplified approach - for better results you would need
            # more sophisticated watermark detection
            mask = np.zeros((height, width), dtype=np.uint8)
            mask[y_start:height, x_start:width] = 255
            
            # Apply inpainting
            img_inpainted = cv2.inpaint(
                cv2.cvtColor(img, cv2.COLOR_RGB2BGR),
                mask,
                3,  # Inpainting radius
                cv2.INPAINT_TELEA  # Algorithm choice
            )
            
            return cv2.cvtColor(img_inpainted, cv2.COLOR_BGR2RGB)
        
        # Apply watermark removal to each frame
        processed_video = video.fl_image(remove_watermark)
        
        # Write the processed video to the output path
        processed_video.write_videofile(output_path, codec='libx264', audio_codec='aac')
        
        # Close the video files
        video.close()
        processed_video.close()
        
        return True
    except Exception as e:
        logger.error(f"Error removing watermark from video: {e}")
        return False


def remove_watermark_from_image(input_path, output_path):
    """Remove watermark from an image file"""
    try:
        # Read the image
        img = cv2.imread(input_path)
        if img is None:
            logger.error(f"Failed to read image: {input_path}")
            return False
        
        height, width = img.shape[:2]
        
        # Define watermark area (bottom right corner)
        # Adjust these values based on typical TikTok watermark position
        watermark_height = int(height * 0.15)  # 15% of the image height
        watermark_width = int(width * 0.25)    # 25% of the image width
        
        # Create a mask for the bottom right corner
        mask = np.zeros((height, width), dtype=np.uint8)
        y_start = height - watermark_height
        x_start = width - watermark_width
        mask[y_start:height, x_start:width] = 255
        
        # Apply inpainting to remove the watermark
        img_inpainted = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
        
        # Save the processed image
        cv2.imwrite(output_path, img_inpainted)
        
        return True
    except Exception as e:
        logger.error(f"Error removing watermark from image: {e}")
        return False


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "ok"})


@app.route('/api/download', methods=['POST'])
def download_tiktok():
    """API endpoint to download and process TikTok content"""
    try:
        data = request.json
        if not data or 'url' not in data:
            return jsonify({"error": "No URL provided"}), 400
        
        url = data['url']
        
        # Validate URL
        if not validate_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        # Get metadata
        metadata = get_tiktok_metadata(url)
        if not metadata:
            return jsonify({"error": "Failed to extract TikTok metadata"}), 400
        
        # Generate unique filenames
        unique_id = str(uuid.uuid4())
        input_file = os.path.join(TEMP_DIR, "tiktok_downloads", f"input_{unique_id}")
        output_file = os.path.join(TEMP_DIR, "tiktok_downloads", f"output_{unique_id}")
        
        # Add appropriate extension based on content type
        if metadata["content_type"] == "video":
            input_file += ".mp4"
            output_file += ".mp4"
        else:
            input_file += ".jpg"
            output_file += ".jpg"
        
        # Download the content
        if not download_content(metadata["download_url"], input_file):
            return jsonify({"error": "Failed to download content"}), 500
        
        # Remove watermark
        success = False
        if metadata["content_type"] == "video":
            success = remove_watermark_from_video(input_file, output_file)
        else:
            success = remove_watermark_from_image(input_file, output_file)
        
        if not success:
            return jsonify({"error": "Failed to remove watermark"}), 500
        
        # Return the processed file
        return send_file(
            output_file, 
            as_attachment=True,
            download_name=f"tiktok_{metadata['id']}.{output_file.split('.')[-1]}"
        )
    
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/info', methods=['POST'])
def get_info():
    """API endpoint to get information about a TikTok post without downloading"""
    try:
        data = request.json
        if not data or 'url' not in data:
            return jsonify({"error": "No URL provided"}), 400
        
        url = data['url']
        
        # Validate URL
        if not validate_tiktok_url(url):
            return jsonify({"error": "Invalid TikTok URL"}), 400
        
        # Get metadata
        metadata = get_tiktok_metadata(url)
        if not metadata:
            return jsonify({"error": "Failed to extract TikTok metadata"}), 400
        
        return jsonify({
            "id": metadata["id"],
            "content_type": metadata["content_type"]
        })
    
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Get port from environment variable or use default
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
