import os
import argparse
import tempfile
import subprocess
from pathlib import Path
import torch
import whisper
from tqdm import tqdm
from flask import Flask, request, jsonify, send_from_directory
import logging
from werkzeug.utils import secure_filename
from flask_cors import CORS  # Import Flask-CORS

app = Flask(__name__)
# Configure CORS to allow file:// origin - this will attempt to allow your local HTML file
CORS(app, resources={r"/api/*": {"origins": ["file:///C:/Users/Administrator/Documents", "null"]}})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'output'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB limit

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# Create necessary directories
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Global variable for the Whisper model
model = None
model_name = "base"  # Default model, can be changed via API

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_model(model_size):
    """Load the Whisper model."""
    global model, model_name
    
    if model is None or model_name != model_size:
        logger.info(f"Loading Whisper model: {model_size}")
        model = whisper.load_model(model_size)
        model_name = model_size
    
    return model

def extract_audio(video_path, audio_path):
    """Extract audio from video using FFmpeg."""
    try:
        cmd = [
            'ffmpeg', '-i', video_path, 
            '-q:a', '0', '-map', 'a', 
            '-c:a', 'libmp3lame', audio_path, 
            '-y'  # Overwrite if exists
        ]
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error extracting audio: {e.stderr.decode() if e.stderr else str(e)}")
        return False

def transcribe_audio(audio_path, model_size="base", language=None):
    """Transcribe audio using Whisper."""
    try:
        model = load_model(model_size)
        
        transcribe_options = {}
        if language:
            transcribe_options["language"] = language
        
        logger.info(f"Transcribing with options: {transcribe_options}")
        result = model.transcribe(audio_path, **transcribe_options)
        
        return result
    except Exception as e:
        logger.error(f"Error during transcription: {str(e)}")
        raise

def format_time(seconds):
    """Convert seconds to SRT time format."""
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    secs = seconds % 60
    millisecs = int((secs - int(secs)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{int(secs):02d},{millisecs:03d}"

def create_srt(transcription, output_srt):
    """Create SRT subtitle file from Whisper transcription."""
    with open(output_srt, 'w', encoding='utf-8') as f:
        for i, segment in enumerate(transcription['segments'], 1):
            start_time = format_time(segment['start'])
            end_time = format_time(segment['end'])
            text = segment['text'].strip()
            
            f.write(f"{i}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")
    
    return output_srt

def add_subtitles_to_video(video_path, srt_path, output_path):
    """Add subtitles to video using FFmpeg."""
    try:
        cmd = [
            'ffmpeg', '-i', video_path, 
            '-i', srt_path, 
            '-c:v', 'copy', '-c:a', 'copy',
            '-c:s', 'mov_text', 
            '-metadata:s:s:0', 'language=eng',
            output_path, 
            '-y'  # Overwrite if exists
        ]
        subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Error adding subtitles: {e.stderr.decode() if e.stderr else str(e)}")
        return False

# Add CORS headers to all API endpoints
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

@app.route('/api/transcribe', methods=['POST', 'OPTIONS'])
def transcribe_video():
    """API endpoint to transcribe a video and generate subtitled version."""
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        return '', 204
        
    # Check if the request has the file
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
        
    if not allowed_file(file.filename):
        return jsonify({'error': f'File type not allowed. Supported formats: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    # Get parameters
    model_size = request.form.get('model', 'base')  # Options: tiny, base, small, medium, large
    language = request.form.get('language', None)   # Optional language code (e.g., 'en', 'fr')
    embed_subtitles = request.form.get('embed', 'true').lower() == 'true'
    
    # Validate model size
    valid_models = ['tiny', 'base', 'small', 'medium', 'large']
    if model_size not in valid_models:
        return jsonify({'error': f'Invalid model size. Choose from: {", ".join(valid_models)}'}), 400
    
    try:
        # Save uploaded file
        filename = secure_filename(file.filename)
        video_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(video_path)
        
        # Extract audio
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_audio:
            audio_path = temp_audio.name
        
        logger.info(f"Extracting audio from {video_path}")
        if not extract_audio(video_path, audio_path):
            return jsonify({'error': 'Failed to extract audio from video'}), 500
        
        # Transcribe audio
        logger.info(f"Transcribing audio with model {model_size}")
        transcription = transcribe_audio(audio_path, model_size, language)
        
        # Create SRT file
        srt_filename = f"{os.path.splitext(filename)[0]}.srt"
        srt_path = os.path.join(app.config['OUTPUT_FOLDER'], srt_filename)
        create_srt(transcription, srt_path)
        
        result = {
            'message': 'Transcription completed',
            'srt_file': srt_filename,
            'download_srt': f"/api/download/{srt_filename}"
        }
        
        # Add subtitles to video if requested
        if embed_subtitles:
            output_filename = f"{os.path.splitext(filename)[0]}_subtitled{os.path.splitext(filename)[1]}"
            output_path = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
            
            logger.info(f"Adding subtitles to video: {output_path}")
            if add_subtitles_to_video(video_path, srt_path, output_path):
                result['subtitled_video'] = output_filename
                result['download_video'] = f"/api/download/{output_filename}"
            else:
                result['warning'] = 'Failed to add subtitles to video, but SRT file was created'
        
        # Clean up the temporary audio file
        try:
            os.unlink(audio_path)
        except:
            pass
            
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error processing video: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    """Download generated files."""
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename, as_attachment=True)

@app.route('/api/models')
def list_models():
    """List available Whisper models."""
    models = {
        'tiny': 'Fastest, lowest accuracy (39M parameters)',
        'base': 'Fast with decent accuracy (74M parameters)',
        'small': 'Balanced speed/accuracy (244M parameters)',
        'medium': 'More accurate, slower (769M parameters)',
        'large': 'Most accurate, slowest (1.5B parameters)'
    }
    return jsonify(models)

@app.route('/')
def index():
    """Simple status endpoint."""
    return jsonify({
        'status': 'running',
        'service': 'Whisper Video Subtitling API',
        'endpoints': {
            '/api/transcribe': 'POST - Upload and transcribe a video',
            '/api/download/<filename>': 'GET - Download a generated file',
            '/api/models': 'GET - List available Whisper models'
        }
    })

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the Whisper Video Subtitling API')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to run the server on')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the server on')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    parser.add_argument('--preload', type=str, choices=['tiny', 'base', 'small', 'medium', 'large'], 
                        help='Preload a specific Whisper model on startup')
    
    args = parser.parse_args()
    
    # Preload model if requested
    if args.preload:
        logger.info(f"Preloading Whisper model: {args.preload}")
        load_model(args.preload)
    
    # Run the Flask app
    app.run(host=args.host, port=args.port, debug=args.debug)
