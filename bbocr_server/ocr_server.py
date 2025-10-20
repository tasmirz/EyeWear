import os
import secrets
import tempfile
import threading
import multiprocessing as mp
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from multiprocessing.managers import BaseManager

from matplotlib.pyplot import text

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from pymongo import MongoClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature
import base64

# Load environment variables
load_dotenv()
from flask import Flask, Response
import json


app = Flask(__name__)
CORS(app)

# Configuration
SECRET_KEY = os.getenv('SECRET_KEY', secrets.token_hex(32))
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
DB_NAME = os.getenv('DB_NAME', 'ocr_system')
CHECK_PUBLIC_KEY = os.getenv('CHECK_PUBLIC_KEY', 'true').lower() == 'true'
JWT_EXPIRY_HOURS = int(os.getenv('JWT_EXPIRY_HOURS', '24'))

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
devices_collection = db['devices']

# OCR Pipeline Queue Manager
class QueueManager(BaseManager):
    pass

# Global variables for pipeline
ocr_input_queue = None
ocr_output_queue = None

_ocr_output_queue = mp.Queue()
def _get_out_queue():
    return _ocr_output_queue

QueueManager.register("get_queue")
QueueManager.register("out_queue",callable=_get_out_queue)

output_results = {}  # Store results {filename: audio_path}
output_lock = threading.Lock()

# New mappings: pending requests and results keyed by uuid
pending_requests = {}  # uuid -> { 'public_key': str, 'image': str, 'temp_dir': str, 'timestamp': datetime }
results_by_uuid = {}   # uuid -> audio_path
request_lock = threading.Lock()

output_manager = QueueManager(address=("127.0.0.1", 50001), authkey=b"abcfe")
output_manager.start()

def init_pipeline_connection():
    """Initialize connection to OCR pipeline"""
    global ocr_input_queue, ocr_output_queue
    
    try:
        # Connect to input queue
        input_manager = QueueManager(address=("127.0.0.1", 50000), authkey=b"abcf")
        input_manager.connect()
        ocr_input_queue = input_manager.get_queue()
        print("Connected to OCR input queue")
        
        # Connect to output queue

        ocr_output_queue = output_manager.out_queue()
        print("Connected to OCR output queue")
        
        # Start output consumer thread
        consumer_thread = threading.Thread(target=output_consumer, daemon=True)
        consumer_thread.start()
        
        return True
    except Exception as e:
        print(f"Failed to connect to OCR pipeline: {e}")
        return False

def output_consumer():
    """Consume outputs from OCR pipeline"""
    global ocr_output_queue, output_results
    
    while True:
        try:
            output = ocr_output_queue.get()
            print(f"Received OCR output: {output}")
            
            # finished_job = {
            #     'uuid': uuid,
            #     'public_key': public_key,
            #     'input_file': file_path,
            #     'output_html': html_file_path,
            #     'output_speech': speech_file,
            #     'runtime': runtime,
            # }


            # output should be a dict with 'input_file' and 'output_file'
            if isinstance(output, dict):
                input_file = output.get('input_file')
                output_speech = output.get('output_speech')
                output_html = output.get('output_html')
                # delete input file, output_html if exists
                # try:
                #     if input_file and os.path.exists(input_file):
                #         os.remove(input_file)
                #     if output_html and os.path.exists(output_html):
                #         os.remove(output_html)
                # except Exception as e:
                #     print(f"Error deleting temp files: {e}")


                # Try to determine uuid. Prefer explicit uuid key if provided by pipeline.
                uuid_key = output.get('uuid')
                if not uuid_key and input_file:
                    try:
                        uuid_key = Path(input_file).stem
                    except Exception:
                        uuid_key = None

                # Store both by input path (backwards compatibility) and by uuid when possible
                if input_file and output_speech:
                    with output_lock:
                        output_results[input_file] = output_speech

                if uuid_key and output_speech:
                    with request_lock:
                        results_by_uuid[uuid_key] = output_speech
                        # Optionally log
                        print(f"Mapped result for uuid {uuid_key} -> {output_speech}")
        except (EOFError, KeyboardInterrupt):
            break
        except Exception as e:
            print(f"Error in output consumer: {e}")

def get_public_key_from_pem(pem_string):
    """Load public key from PEM string"""
    try:
        public_key = serialization.load_pem_public_key(
            pem_string.encode('utf-8'),
            backend=default_backend()
        )
        return public_key
    except Exception as e:
        print(f"Error loading public key: {e}")
        return None

def verify_signature(public_key, text, signature_b64):
    """Verify signature with public key"""
    try:
        signature = base64.b64decode(signature_b64)
        public_key.verify(
            signature,
            text.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        print(f"Error verifying signature: {e}")
        return False

def get_public_key_fingerprint(public_key_pem):
    """Generate fingerprint for public key"""
    import hashlib
    return hashlib.sha256(public_key_pem.encode('utf-8')).hexdigest()

def is_device_authorized(public_key_pem):
    """Check if device public key is authorized in MongoDB"""
    if not CHECK_PUBLIC_KEY:
        return True
    
    fingerprint = get_public_key_fingerprint(public_key_pem)
    device = devices_collection.find_one({"fingerprint": fingerprint})
    
    if device and device.get('authorized', False):
        return True
    
    # Store unauthorized device for future approval
    if not device:
        devices_collection.insert_one({
            "fingerprint": fingerprint,
            "public_key": public_key_pem,
            "authorized": False,
            "first_seen": datetime.utcnow(),
            "last_seen": datetime.utcnow()
        })
    else:
        devices_collection.update_one(
            {"fingerprint": fingerprint},
            {"$set": {"last_seen": datetime.utcnow()}}
        )
    
    return False

@app.route('/challenge', methods=['POST'])
def challenge():
    """Step 1: Generate challenge for device"""
    try:
        data = request.get_json()
        public_key_pem = data.get('public_key')
        
        if not public_key_pem:
            return jsonify({"error": "Public key is required"}), 400
        
        # Validate public key format
        public_key = get_public_key_from_pem(public_key_pem)
        if not public_key:
            return jsonify({"error": "Invalid public key format"}), 400
        
        # Check if device is authorized (if enabled)
        if not is_device_authorized(public_key_pem):
            return jsonify({"error": "Device not authorized"}), 403
        
        # Generate challenge text
        challenge_text = secrets.token_urlsafe(32)
        
        # Create challenge JWT
        challenge_jwt = jwt.encode({
            'public_key': public_key_pem,
            'challenge': challenge_text,
            'exp': datetime.utcnow() + timedelta(minutes=5)
        }, SECRET_KEY, algorithm='HS256')
        
        return jsonify({
            'jwt': challenge_jwt,
            'text': challenge_text
        }), 200
        
    except Exception as e:
        print(f"Challenge error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/auth', methods=['POST'])
def authenticate():
    """Step 2: Verify signed challenge and issue final JWT"""
    try:
        data = request.get_json()
        challenge_jwt = data.get('jwt')
        signed_text = data.get('signed_text')
        
        if not challenge_jwt or not signed_text:
            return jsonify({"error": "JWT and signed text are required"}), 400
        
        # Decode challenge JWT
        try:
            challenge_data = jwt.decode(challenge_jwt, SECRET_KEY, algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Challenge expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid challenge token"}), 401
        
        public_key_pem = challenge_data.get('public_key')
        challenge_text = challenge_data.get('challenge')
        
        # Load public key
        public_key = get_public_key_from_pem(public_key_pem)
        if not public_key:
            return jsonify({"error": "Invalid public key"}), 400
        
        # Verify signature
        if not verify_signature(public_key, challenge_text, signed_text):
            return jsonify({"error": "Invalid signature"}), 401
        
        # Generate final JWT
        fingerprint = get_public_key_fingerprint(public_key_pem)
        final_jwt = jwt.encode({
            'device_fingerprint': fingerprint,
            'public_key': public_key_pem,
            'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS)
        }, SECRET_KEY, algorithm='HS256')
        
        return jsonify({'jwt': final_jwt}), 200
        
    except Exception as e:
        print(f"Auth error: {e}")
        return jsonify({"error": "Internal server error"}), 500

def verify_jwt_token(token):
    """Verify JWT token and return payload"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# replace with redis later,
# a concurrency-safe queue

@app.route('/upload', methods=['POST'])
def upload_image():
    """Upload image, process through OCR, return audio"""
    try:
        print("Received upload request")
        # Verify JWT
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing or invalid authorization header"}), 401
        
        token = auth_header.split(' ')[1]
        payload = verify_jwt_token(token)
        
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        
        # Check if image file is present
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        
        image_file = request.files['image']
        if image_file.filename == '':
            return jsonify({"error": "Empty filename"}), 400
        
        # Create temp directory for this request
        temp_dir = tempfile.mkdtemp()

        try:
            # Generate uuid for this request
            req_uuid = str(uuid.uuid4())

            # Save image to temp file named <uuid>.jpg
            temp_image_path = os.path.join(temp_dir, f"{req_uuid}.jpg")
            image_file.save(temp_image_path)
            print(f"Saved image to: {temp_image_path}")


            # rotate image 180 deg
            import cv2
            image=cv2.imread(temp_image_path)
            rotated_image = cv2.rotate(image, cv2.ROTATE_180)
            cv2.imwrite(temp_image_path, rotated_image)
            
            # Enqueue job dict to OCR pipeline
            if ocr_input_queue is None:
                return jsonify({"error": "OCR pipeline not available"}), 503

            job = {
                'uuid': req_uuid,
                'input_file': temp_image_path,
                'public_key': payload.get('public_key'),
            }

            # record pending request
            with request_lock:
                pending_requests[req_uuid] = {
                    'public_key': payload.get('public_key'),
                    'image': temp_image_path,
                    'temp_dir': temp_dir,
                    'timestamp': datetime.utcnow()
                }

            ocr_input_queue.put(job)
            print(f"Enqueued job uuid={req_uuid} -> {temp_image_path}")

            # Immediately return uuid and the public_key in the JWT payload back to client
            return jsonify({'uuid': req_uuid, 'public_key': payload.get('public_key')}), 202

        except Exception as e:
            # Cleanup on error
            # try:
            #     if os.path.exists(temp_image_path):
            #         os.remove(temp_image_path)
            #     if os.path.exists(temp_dir):
            #         os.rmdir(temp_dir)
            # except:
            #     pass
            # raise e
            pass
        
    except Exception as e:
        print(f"Upload error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "pipeline_connected": ocr_input_queue is not None,
        "check_public_key": CHECK_PUBLIC_KEY
    }), 200

@app.route('/devices', methods=['GET'])
def list_devices():
    """List all devices (for admin)"""
    try:
        devices = list(devices_collection.find({}, {'_id': 0}))
        return jsonify({"devices": devices}), 200
    except Exception as e:
        print(f"Error listing devices: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/devices/authorize', methods=['POST'])
def authorize_device():
    """Authorize a device (for admin)"""
    try:
        data = request.get_json()
        fingerprint = data.get('fingerprint')
        authorized = data.get('authorized', True)
        
        if not fingerprint:
            return jsonify({"error": "Fingerprint is required"}), 400
        
        result = devices_collection.update_one(
            {"fingerprint": fingerprint},
            {"$set": {"authorized": authorized, "updated": datetime.utcnow()}}
        )
        
        if result.modified_count == 0:
            return jsonify({"error": "Device not found"}), 404
        
        return jsonify({"success": True}), 200
        
    except Exception as e:
        print(f"Error authorizing device: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/result/<uuid_key>', methods=['GET'])
def get_result(uuid_key):
    """Client polls with UUID to check if wav is ready. Returns wav if available."""
    try:
        # Verify JWT
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing or invalid authorization header"}), 401

        token = auth_header.split(' ')[1]
        payload = verify_jwt_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        # Confirm requester public_key matches the stored public_key for this uuid
        requester_pk = payload.get('public_key')

        with request_lock:
            pending = pending_requests.get(uuid_key)
            res_path = results_by_uuid.get(uuid_key)

        if not pending and not res_path:
            return jsonify({"status": "not_found"}), 404

        # If result not ready yet
        if not res_path:
            return jsonify({"status": "pending"}), 202

        # Ensure public key matches the one who submitted
        stored_pk = pending.get('public_key') if pending else None
        if stored_pk and requester_pk and stored_pk != requester_pk:
            return jsonify({"error": "Public key mismatch"}), 403

        # Send file
        if not os.path.exists(res_path):
            return jsonify({"error": "Audio file missing"}), 500

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # send  as plain text, open the file as text and send , NOT file
        with open(res_path, 'r', encoding='utf-8') as f:
            text_ = f.read()
        data = {
            "status": "completed",
            "text": text_
        }
        response = Response(json.dumps(data, ensure_ascii=False),
                        mimetype="application/json")

        # Cleanup after sending
        @response.call_on_close
        def cleanup_result():
            try:
                # Remove audio file
                if os.path.exists(res_path):
                    #os.remove(res_path)
                    print(f"Deleted audio file: {res_path}")

                # Remove temp image and dir if present
                with request_lock:
                    pending_local = pending_requests.pop(uuid_key, None)
                    results_by_uuid.pop(uuid_key, None)

                if pending_local:
                    img = pending_local.get('image')
                    td = pending_local.get('temp_dir')
                    try:
                        if img and os.path.exists(img):
                            #os.remove(img)
                            print(f"Deleted temp image: {img}")
                        if td and os.path.exists(td):
                            #os.rmdir(td)
                            pass
                    except Exception as e:
                        print(f"Cleanup result error: {e}")
            except Exception as e:
                print(f"Result cleanup outer error: {e}")

        return response

    except Exception as e:
        print(f"Get result error: {e}")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    # Initialize pipeline connection
    if not init_pipeline_connection():
        print("Warning: OCR pipeline not connected. Upload endpoint will not work.")
    
    # Create indexes
    devices_collection.create_index("fingerprint", unique=True)
    
    # Run Flask app
    port = int(os.getenv('PORT', 8085))
    host = os.getenv('HOST', '0.0.0.0')
    
    print(f"Starting server on {host}:{port}")
    print(f"Public key checking: {'ENABLED' if CHECK_PUBLIC_KEY else 'DISABLED'}")
    
    app.run(host=host, port=port, debug=False, threaded=True)