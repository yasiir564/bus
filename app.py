from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import whisper
import uuid
import time
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configure upload settings
UPLOAD_FOLDER = 'temp_uploads'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm', 'mp3', 'wav', 'm4a'}
MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100MB limit

# Create temporary upload directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Load Whisper model lazily to save memory
model = None

def get_model():
    global model
    if model is None:
        model = whisper.load_model("base")
    return model

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/ping', methods=['GET'])
def ping():
    """Health check endpoint"""
    return jsonify({"status": "alive", "timestamp": time.time()})

@app.route('/', methods=['GET'])
def home():
    """Simple API info"""
    return jsonify({
        "name": "Whisper Transcription API",
        "endpoints": {
            "/transcribe": "POST - Send file for transcription",
            "/ping": "GET - Health check"
        },
        "file_size_limit": "100MB",
        "supported_formats": list(ALLOWED_EXTENSIONS)
    })

@app.route('/transcribe', methods=['POST'])
def transcribe_video():
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
        
        # Use whisper to transcribe
        result = get_model().transcribe(file_path)
        
        # Remove the temporary file after transcription
        os.remove(file_path)
        
        # Return only essential transcription data
        return jsonify({
            "transcription": result["text"],
            "segments": [
                {
                    "id": segment["id"],
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": segment["text"]
                } for segment in result["segments"]
            ]
        })
    
    except Exception as e:
        # Clean up if error occurs
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)
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
