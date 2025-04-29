from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import json
import uuid
import time
import wave
import subprocess
import tempfile
from vosk import Model, KaldiRecognizer, SetLogLevel
from werkzeug.utils import secure_filename
from pydub import AudioSegment

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configure upload settings
UPLOAD_FOLDER = 'temp_uploads'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm', 'mp3', 'wav', 'm4a'}
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB limit

# Create temporary upload directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Set Vosk logging level (0 for most verbose, higher for less logging)
SetLogLevel(0)

# Load Vosk model lazily to save memory
model = None

def get_model():
    global model
    if model is None:
        model_path = "vosk-model-small-en-us-0.15"  # Change this to your downloaded model path
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Vosk model not found at {model_path}. Please download it from https://alphacephei.com/vosk/models")
        model = Model(model_path)
    return model

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_audio(video_path, output_audio_path):
    """Extract audio from video file using ffmpeg"""
    subprocess.run([
        'ffmpeg', '-i', video_path, '-ar', '16000', '-ac', '1', 
        '-c:a', 'pcm_s16le', '-y', output_audio_path
    ], check=True)

def convert_to_wav(audio_path, output_wav_path):
    """Convert any audio format to WAV with correct parameters for Vosk"""
    audio = AudioSegment.from_file(audio_path)
    audio = audio.set_frame_rate(16000).set_channels(1)
    audio.export(output_wav_path, format="wav")

def transcribe_with_vosk(audio_path):
    """Process audio file with Vosk and return transcription with timestamps"""
    model = get_model()
    
    # Open the audio file
    wf = wave.open(audio_path, "rb")
    
    # Check if the audio has the right format for Vosk
    if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcompfreq() != 16000:
        raise ValueError("Audio file must be 16kHz mono PCM WAV")
    
    # Create recognizer
    rec = KaldiRecognizer(model, wf.getframerate())
    rec.SetWords(True)
    
    # Process the audio in chunks
    results = []
    chunk_size = 4000  # Adjust based on your needs
    
    while True:
        data = wf.readframes(chunk_size)
        if len(data) == 0:
            break
        
        if rec.AcceptWaveform(data):
            part_result = json.loads(rec.Result())
            if 'result' in part_result:
                results.append(part_result)
    
    # Get the final bits of audio
    part_result = json.loads(rec.FinalResult())
    if 'result' in part_result:
        results.append(part_result)
    
    # Process the results to create a transcript and segments
    full_text = ""
    segments = []
    segment_id = 0
    
    for res in results:
        if 'result' not in res:
            continue
        
        words = res['result']
        if not words:
            continue
        
        # Get text for this segment
        segment_text = ' '.join(w['word'] for w in words)
        start_time = words[0]['start']
        end_time = words[-1]['end']
        
        full_text += segment_text + " "
        
        segments.append({
            "id": segment_id,
            "start": start_time,
            "end": end_time,
            "text": segment_text.strip()
        })
        segment_id += 1
    
    return {
        "text": full_text.strip(),
        "segments": segments
    }

@app.route('/ping', methods=['GET'])
def ping():
    """Health check endpoint"""
    return jsonify({"status": "alive", "timestamp": time.time()})

@app.route('/', methods=['GET'])
def home():
    """Simple API info"""
    return jsonify({
        "name": "Vosk Transcription API",
        "endpoints": {
            "/transcribe": "POST - Send file for transcription",
            "/ping": "GET - Health check"
        },
        "file_size_limit": "100MB",
        "supported_formats": list(ALLOWED_EXTENSIONS)
    })

@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    # Check if file is present in request
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    
    # Check if file is empty
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
        
    # Check if file type is allowed
    if not allowed_file(file.filename):
        return jsonify({"error": f"File type not allowed. Supported types: {', '.join(ALLOWED_EXTENSIONS)}"}), 400
    
    try:
        # Create unique filename for the uploaded file
        filename = secure_filename(file.filename)
        unique_filename = f"{str(uuid.uuid4())}_{filename}"
        file_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        
        # Save the file temporarily
        file.save(file_path)
        
        # Create temporary WAV file for processing
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_wav:
            tmp_wav_path = tmp_wav.name
        
        # Process based on file type
        is_video = file.filename.rsplit('.', 1)[1].lower() in {'mp4', 'avi', 'mov', 'mkv', 'webm'}
        
        if is_video:
            # Extract audio from video
            extract_audio(file_path, tmp_wav_path)
        else:
            # Convert audio to proper format for Vosk
            convert_to_wav(file_path, tmp_wav_path)
        
        # Use Vosk to transcribe
        result = transcribe_with_vosk(tmp_wav_path)
        
        # Clean up temporary files
        os.remove(file_path)
        os.remove(tmp_wav_path)
        
        # Return transcription data
        return jsonify({
            "transcription": result["text"],
            "segments": result["segments"]
        })
    
    except Exception as e:
        # Clean up if error occurs
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)
        if 'tmp_wav_path' in locals() and os.path.exists(tmp_wav_path):
            os.remove(tmp_wav_path)
        return jsonify({"error": str(e)}), 500

# Cleanup function to run when app is shutting down
@app.teardown_appcontext
def cleanup_temp_files(error):
    # Remove any leftover files in the temp directory
    if os.path.exists(UPLOAD_FOLDER):
        for f in os.listdir(UPLOAD_FOLDER):
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, f))
            except:
                pass

# Start the application if executed directly
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
