import logging
import os
import time
import signal
import struct
import requests
import base64
import threading
import queue
from pathlib import Path
from datetime import datetime
from multiprocessing.shared_memory import SharedMemory
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from picamera2 import Picamera2
from dotenv import load_dotenv

import subprocess
from common import IPC, SoundType
from gtts import gTTS
from common import OCRSignal
from audio_feedback import AudioFeedbackManager

afm = AudioFeedbackManager([
    SoundType.IM_TAKING_DONE,
])

TAGNAME = "ocr_process"
logging.basicConfig(
    format=f"[{TAGNAME}] %(message)s",
    level=logging.INFO
)
logging.info("Starting OCR")


import vlc
class VLCPlayer:
    def __init__(self, media_path: str):
        """Initialize VLC player with a media file path."""
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        media = self.instance.media_new(media_path)
        self.player.set_media(media)

    def play(self):
        """Start or resume playback."""
        self.player.play()

    def pause(self):
        """Pause playback."""
        self.player.pause()

    def resume(self):
        """Resume playback (same as play if paused)."""
        state = self.player.get_state()
        if state == vlc.State.Paused:
            self.player.pause()  # toggle pause to resume
        elif state not in [vlc.State.Playing, vlc.State.Buffering]:
            self.player.play()

    def stop(self):
        """Stop playback."""
        self.player.stop()

ipc = None
shm= None

# Load environment variables
load_dotenv()

class OCRClient:
    def __init__(self):
        # Configuration
        self.server_url = os.getenv('SERVER_URL', 'http://192.168.3.105:8085')
        self.public_key_path = './keys/device_public.pem'
        self.private_key_path = './keys/device_private.pem'
        
        # Queues
        self.image_queue = queue.Queue()
        self.audio_queue = queue.Queue()
        
        # State
        self.jwt_token = None
        self.running = False
        self.paused = False
        
        # Directories
        self.image_dir = Path('./captured_images')
        self.audio_dir = Path('./audio_files')
        self.image_dir.mkdir(exist_ok=True)
        self.audio_dir.mkdir(exist_ok=True)
        
        # Load keys
        self.load_keys()
        
        # Initialize pygame mixer for audio playback
        global player
        
        # Threads
        self.upload_thread = None
        self.playback_thread = None
        
        # IPC setup
        signal.signal(signal.SIGUSR1, self.signal_handler)
        
    def load_keys(self):
        """Load public and private keys"""
        with open(self.public_key_path, 'rb') as f:
            self.public_key = serialization.load_pem_public_key(
                f.read(),
                backend=default_backend()
            )
        
        with open(self.private_key_path, 'rb') as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(),
                password=None,
                backend=default_backend()
            )
    
    def get_public_key_pem(self):
        """Get public key in PEM format as string"""
        pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return pem.decode('utf-8')
    
    def sign_text(self, text):
        """Sign text with private key"""
        signature = self.private_key.sign(
            text.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    
    def authenticate(self):
        """Perform authentication flow"""
        try:
            logging.info("Starting authentication...")
            
            # Step 1: Get challenge
            public_key_pem = self.get_public_key_pem()
            response = requests.post(
                f"{self.server_url}/challenge",
                json={"public_key": public_key_pem},
                timeout=10
            )
            response.raise_for_status()
            
            challenge_data = response.json()
            challenge_jwt = challenge_data.get('jwt')
            challenge_text = challenge_data.get('text')
            
            logging.info(f"Received challenge: {challenge_text}")
            
            # Step 2: Sign the challenge text
            signed_text = self.sign_text(challenge_text)
            
            # Step 3: Send signed challenge
            response = requests.post(
                f"{self.server_url}/auth",
                json={
                    "jwt": challenge_jwt,
                    "signed_text": signed_text
                },
                timeout=10
            )
            response.raise_for_status()
            
            auth_data = response.json()
            self.jwt_token = auth_data.get('jwt')
            
            logging.info("Authentication successful!")
            return True
            
        except Exception as e:
            logging.error(f"Authentication failed: {e}")
            return False
    
    def capture_image(self):
        """Capture image using picamera2 and enqueue"""
            # Generate filename
        try:

            logging.info("Capturing image...")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = self.image_dir / f"_image_{timestamp}.jpg"
        
            #run command,
            filename =  f"/home/pi/EyeWear/captured_images/image_{timestamp}.jpg"
            #subprocess.run(['rpicam-still', '-o', str(filename), '-q', '60', '--autofocus-on-capture', '--timeout', '5000', '--nopreview', '--verbose', '0','--vflip','--hflip'])
            #subprocess.run(['rpicam-still', '-o', str(filename), '-q', '90', '--autofocus-on-capture', '--timeout', '5000', '--nopreview', '--verbose', '0'])
            #subprocess.run(['rpicam-still', '-o', str(filename), '-q', '90',])
            subprocess.run(['rpicam-still', '-o', str(filename), '-q', '90', '--autofocus-mode','continuous', '--timeout', '2000', '--nopreview'])
            #wait for subprocess to complete
            # Capture image
            afm.play(SoundType.IM_TAKING_DONE)
            logging.info(f"Captured image: {filename}")
            
            # Enqueue image path
            self.image_queue.put(str(filename))
            
        except Exception as e:
            logging.error(f"Image capture failed: {e}")
    
    def upload_worker(self):
        """Worker thread to upload images continuously"""
        logging.info("Upload worker started")
        while self.running:
            try:
                # Get image from queue (non-blocking with timeout)
                try:
                    image_path = self.image_queue.get(timeout=1)
                except queue.Empty:
                    continue
                
                logging.info(f"Uploading image: {image_path}")
                
                # Ensure we have valid JWT
                if not self.jwt_token:
                    logging.info("No JWT token, re-authenticating...")
                    if not self.authenticate():
                        # Put image back in queue
                        self.image_queue.put(image_path)
                        time.sleep(5)
                        continue

                # Upload image
                with open(image_path, 'rb') as f:
                    files = {'image': f}
                    headers = {'Authorization': f'Bearer {self.jwt_token}'}

                    response = requests.post(
                        f"{self.server_url}/upload",
                        files=files,
                        headers=headers,
                        timeout=30
                    )

                # Handle response
                # If server returns JSON with uuid (202 Accepted), poll /result/<uuid>
                content_type = response.headers.get('Content-Type', '')
                audio_filename = None

                if 'application/json' in content_type or response.status_code == 202:
                    try:
                        data = response.json()
                    except Exception:
                        # Not a JSON body; treat as error
                        raise requests.exceptions.RequestException('Unexpected non-JSON response')

                    req_uuid = data.get('uuid')
                    if not req_uuid:
                        raise requests.exceptions.RequestException('No uuid in upload response')

                    # Poll for result
                    poll_url = f"{self.server_url}/result/{req_uuid}"
                    max_wait = 120  # seconds
                    wait_interval = 10
                    elapsed = 0.0
                    got_audio = False

                    while elapsed < max_wait and self.running:
                        try:
                            poll_resp = requests.get(poll_url, headers=headers, timeout=30, stream=True)
                        except requests.exceptions.RequestException as e:
                            logging.warning(f"Polling error: {e}")
                            time.sleep(1)
                            elapsed += 1
                            continue

                        if poll_resp.status_code == 202:
                            # still pending
                            time.sleep(wait_interval)
                            elapsed += wait_interval
                            continue
                        elif poll_resp.status_code == 200:
                            # Received plain text response
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            audio_filename = self.audio_dir / f"audio_{timestamp}.mp3"
                            logging.debug(f"Poll response object: {poll_resp}")
                            try:
                                resp_json = poll_resp.json()
                                logging.debug(f"Poll response json: {resp_json}")
                                logging.debug(f"Poll response text: {resp_json.get('text')}")
                            except Exception:
                                resp_json = None

                            tts = gTTS(text=resp_json.get("text") if resp_json else "", lang='bn')
                            tts.save(audio_filename)

                            got_audio = True
                            logging.info(f"Received plain text file: {audio_filename}")
                            break
                        elif poll_resp.status_code == 404:
                            logging.warning(f"Result not found for uuid {req_uuid}")
                            break
                        elif poll_resp.status_code == 401:
                            # JWT expired or invalid: re-auth and retry upload
                            logging.warning("Unauthorized when polling, re-authenticating")
                            self.jwt_token = None
                            break
                        else:
                            logging.warning(f"Unexpected poll response {poll_resp.status_code}: {poll_resp.text}")
                            break

                    if not got_audio:
                        # Put image back in queue for retry or drop
                        logging.warning(f"Failed to get audio for uuid {req_uuid} within timeout")
                        self.image_queue.put(image_path)
                        time.sleep(1)
                        continue
                #
                # If we reached here and have an audio file, delete the image and enqueue audio
                if audio_filename and os.path.exists(audio_filename):
                    try:
                        os.remove(image_path)
                        logging.info(f"Deleted image: {image_path}")
                    except Exception as e:
                        logging.warning(f"Failed to delete image {image_path}: {e}")

                    # Enqueue audio file
                    self.audio_queue.put(str(audio_filename))
                
            except requests.exceptions.RequestException as e:
                logging.error(f"Upload failed: {e}")
                # Put image back in queue
                self.image_queue.put(image_path)
                time.sleep(5)
            except Exception as e:
                logging.exception(f"Upload worker error: {e}")
                time.sleep(1)
    
    def playback_worker(self):
        """Worker thread to play audio files continuously"""
        while self.running:
            try:
                # Get audio file from queue
                try:
                    audio_path = self.audio_queue.get(timeout=1)
                except queue.Empty:
                    continue
                
                # Wait if paused
                while self.paused and self.running:
                    time.sleep(0.5)
                
                if not self.running:
                    break
                
                logging.info(f"Playing audio: {audio_path}")
                
                # Play audio
                #pygame.mixer.music.load(audio_path)
                #pygame.mixer.music.play()
                
                player = VLCPlayer(audio_path)
                player.play()
                
                # Wait for playback to finish
                while player.is_playing() and self.running and not self.paused:
                    time.sleep(0.1)
                
                # Stop if paused
                if self.paused:
                    #pygame.mixer.music.stop()
                    player.stop()
                    # Put audio back in queue
                    self.audio_queue.put(audio_path)
                    continue
                
                # Delete audio file after playback
                if self.running:
                    os.remove(audio_path)
                    logging.info(f"Deleted audio: {audio_path}")
                
            except Exception as e:
                logging.exception(f"Playback worker error: {e}")
                time.sleep(1)
    
    def start(self):
        """Start the OCR client"""
        if self.running:
            logging.info("Client already running")
            return
        
        logging.info("Starting OCR Client...")
        
        # Authenticate
        
        logging.info("OCR Client started successfully")
        self.capture_image()  # Capture initial image
    
    def stop(self):
        """Stop the OCR client"""
        logging.info("Stopping OCR Client...")
        self.running = False
        
        # Wait for threads to finish
        if self.upload_thread:
            self.upload_thread.join(timeout=5)
        if self.playback_thread:
            self.playback_thread.join(timeout=5)
        
        # Stop audio playback
        player.stop()
       # pygame.mixer.music.stop()
        
        
    logging.info("OCR Client stopped")
    
    def toggle_pause(self):
        """Toggle play/pause state"""
        self.paused = not self.paused
        if self.paused:
            logging.info("Playback paused")
            #pygame.mixer.music.pause()
            player.pause()
        else:
            logging.info("Playback resumed")
            #pygame.mixer.music.unpause()
            player.resume()

    def signal_handler(self, signum, frame):
        """Handle signals from shared memory"""
        action_code = struct.unpack('i', shm.buf[:4])[0]
        logging.debug(f"Action code from shared memory: {action_code}")
        
        if action_code == OCRSignal.START_OCR.value:
            logging.info("Starting OCR Client")
            self.start()
        elif action_code == OCRSignal.STOP_OCR.value:
            logging.info("Stopping OCR Client")
            self.stop()
        elif action_code == OCRSignal.NEW_PICTURE.value:
            logging.info("Add another image")
            self.capture_image()
        elif action_code == OCRSignal.PAUSE_OCR.value:
            logging.info("Play/Pause")
            self.toggle_pause()
    
    def run(self):
        """Main run loop"""
        logging.info("OCR Client initialized. Waiting for signals...")
        if not self.authenticate():
            logging.error("Failed to authenticate. Exiting.")
            return
        
        self.running = True
        
        # Start worker threads
        self.upload_thread = threading.Thread(target=self.upload_worker, daemon=True)
        self.playback_thread = threading.Thread(target=self.playback_worker, daemon=True)
        
        self.upload_thread.start()
        self.playback_thread.start()
        try:
            while True:
                signal.pause()  # Wait for signals
        except KeyboardInterrupt:
            logging.info("\nShutting down...")
            self.stop()

if __name__ == "__main__":
    try:
        ipc = IPC("ocr_process")
        try:
            shm = SharedMemory(name="ocr_signal", 
                               create=True, size=4)
        except FileExistsError:
            shm = SharedMemory(name="ocr_signal", create=False, size=4)
            
        logging.info("Shared memory for OCR signals initialized.")
        client = OCRClient()
        client.run()
    except Exception as e:
        logging.exception(f"Fatal error: {e}")
    finally:
        try:
            ipc.cleanup()
            shm.close()

        except:
            pass