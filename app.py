from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import tempfile
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

# Initialize Whisper model (using "base" for lightweight usage)
# Options: "tiny", "base", "small", "medium", "large"
model = whisper.load_model("base")

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/ping', methods=['GET'])
def ping():
    """Health check endpoint"""
    return jsonify({"status": "alive", "timestamp": time.time()})

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
        result = model.transcribe(file_path)
        
        # Remove the temporary file after transcription
        os.remove(file_path)
        
        # Return the transcription
        return jsonify({
            "transcription": result["text"],
            "segments": result["segments"]
        })
    
    except Exception as e:
        # Clean up if error occurs
        if os.path.exists(file_path):
            os.remove(file_path)
        return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    """Simple home page with instructions"""
    return """
    <html>
        <head>
            <title>Whisper Video Transcription API</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
                h1 { color: #333; }
                code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; }
            </style>
        </head>
        <body>
            <h1>Whisper Video Transcription API</h1>
            <p>This is a simple API for transcribing videos using OpenAI's Whisper model.</p>
            <h2>How to use:</h2>
            <p>Send a POST request to <code>/transcribe</code> with a video file in the <code>file</code> field.</p>
            <h3>Testing with cURL:</h3>
            <pre>curl -F "file=@your_video.mp4" https://your-app-url.com/transcribe</pre>
            <h3>File size limit:</h3>
            <p>Maximum file size: 100MB</p>
            <h3>Supported file types:</h3>
            <p>Video: mp4, avi, mov, mkv, webm</p>
            <p>Audio: mp3, wav, m4a</p>
        </body>
    </html>
    """

# Start the application if executed directly
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
