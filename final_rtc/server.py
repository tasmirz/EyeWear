#!/usr/bin/env python3
"""
WebRTC Server for Raspberry Pi with HTTPS support
- Video: Pi Camera -> Web Client (unidirectional)
- Audio: Client -> Pi speakers (receive only for now)
"""

import asyncio
import json
import logging
import ssl
import traceback
import threading
import queue
import os
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import optional dependencies
try:
    from picamera2 import Picamera2
    PICAMERA_AVAILABLE = True
except ImportError:
    logger.warning("picamera2 not available")
    PICAMERA_AVAILABLE = False

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    logger.warning("pyaudio not available")
    PYAUDIO_AVAILABLE = False

# Audio configuration
AUDIO_FORMAT = pyaudio.paInt16 if PYAUDIO_AVAILABLE else None
AUDIO_CHANNELS = 2  # Stereo for compatibility
AUDIO_RATE = 48000
AUDIO_CHUNK = 960


def find_audio_device():
    """Find a working audio output device"""
    if not PYAUDIO_AVAILABLE:
        return None
    
    # Suppress ALSA errors temporarily
    os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
    
    p = pyaudio.PyAudio()
    output_device = None
    
    try:
        default_output = p.get_default_output_device_info()
        logger.info(f"Default output: {default_output['name']}")
        output_device = default_output['index']
    except Exception as e:
        logger.warning(f"No default output device: {e}")
        # Try to find any working output device
        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
                if info['maxOutputChannels'] > 0:
                    logger.info(f"Found output device {i}: {info['name']}")
                    output_device = i
                    break
            except:
                continue
    finally:
        p.terminate()
    
    return output_device


class PiCameraTrack(VideoStreamTrack):
    """Video track from Raspberry Pi Camera - singleton pattern"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, width=640, height=480, fps=15):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
                cls._instance.width = width
                cls._instance.height = height
                cls._instance.fps = fps
            return cls._instance
    
    def __init__(self, width=640, height=480, fps=15):
        if self._initialized:
            return
            
        super().__init__()
        if not PICAMERA_AVAILABLE:
            raise RuntimeError("picamera2 is not available")
        
        try:
            self.picam2 = Picamera2()
            config = self.picam2.create_video_configuration(
                main={"size": (self.width, self.height), "format": "BGR888"},
                controls={"FrameRate": self.fps}
            )
            self.picam2.configure(config)
            self.picam2.start()
            self._initialized = True
            logger.info(f"PiCamera2 initialized: {self.width}x{self.height}@{self.fps}fps")
        except Exception as e:
            logger.error(f"Failed to initialize PiCamera2: {e}")
            raise
    
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        try:
            frame_array = self.picam2.capture_array()
            frame = VideoFrame.from_ndarray(frame_array, format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame
        except Exception as e:
            logger.error(f"Error capturing frame: {e}")
            black_frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            frame = VideoFrame.from_ndarray(black_frame, format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame
    
    def stop(self):
        pass  # Don't stop - shared across connections
    
    @classmethod
    def cleanup(cls):
        """Cleanup method to be called on server shutdown"""
        with cls._lock:
            if cls._instance and cls._instance._initialized:
                try:
                    cls._instance.picam2.stop()
                    logger.info("PiCamera2 stopped")
                except Exception as e:
                    logger.error(f"Error stopping camera: {e}")
                cls._instance = None


class AudioReceiver:
    """Receive and play audio from client"""
    
    def __init__(self, output_device=None):
        if not PYAUDIO_AVAILABLE:
            raise RuntimeError("pyaudio is not available")
        
        self.output_device = output_device
        self.audio_queue = queue.Queue(maxsize=100)
        self.running = True
        self.thread = None
        self.stream = None
        self.pyaudio_instance = None
        
        # Start in a separate thread to isolate PyAudio initialization
        self.thread = threading.Thread(target=self._playback_audio, daemon=True)
        self.thread.start()
    
    def _playback_audio(self):
        """Play audio through speakers in separate thread"""
        try:
            self.pyaudio_instance = pyaudio.PyAudio()
            
            kwargs = {
                'format': AUDIO_FORMAT,
                'channels': AUDIO_CHANNELS,
                'rate': AUDIO_RATE,
                'output': True,
                'frames_per_buffer': AUDIO_CHUNK
            }
            
            if self.output_device is not None:
                kwargs['output_device_index'] = self.output_device
            
            self.stream = self.pyaudio_instance.open(**kwargs)
            logger.info(f"Audio playback started (device: {self.output_device})")
            
            while self.running:
                try:
                    data = self.audio_queue.get(timeout=1)
                    if self.stream and self.running:
                        self.stream.write(data)
                except queue.Empty:
                    continue
                except Exception as e:
                    if self.running:
                        logger.error(f"Audio playback error: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"Failed to initialize audio playback: {e}")
        finally:
            try:
                if self.stream:
                    self.stream.stop_stream()
                    self.stream.close()
            except:
                pass
            try:
                if self.pyaudio_instance:
                    self.pyaudio_instance.terminate()
            except:
                pass
            logger.info("Audio playback stopped")
    
    def add_audio(self, audio_data):
        """Add audio data to playback queue"""
        if not self.audio_queue.full():
            self.audio_queue.put(audio_data)
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)


class WebRTCServer:
    def __init__(self):
        self.pcs = set()
        self.audio_receivers = {}
        self.output_device = find_audio_device()
        
        if self.output_device is not None:
            logger.info(f"✓ Audio output device found: {self.output_device}")
        else:
            logger.warning("⚠ No audio output device found - audio playback disabled")
        
    async def index(self, request):
        """Serve the HTML client"""
        try:
            content = open('client.html', 'r').read()
            return web.Response(content_type='text/html', text=content)
        except FileNotFoundError:
            return web.Response(
                status=404,
                text="client.html not found"
            )
    
    async def offer(self, request):
        """Handle WebRTC offer from client"""
        try:
            params = await request.json()
            offer = RTCSessionDescription(sdp=params['sdp'], type=params['type'])
            
            # Create peer connection
            pc = RTCPeerConnection(
                configuration=RTCConfiguration(
                    iceServers=[RTCIceServer(urls=['stun:stun.l.google.com:19302'])]
                )
            )
            pc_id = id(pc)
            self.pcs.add(pc)
            
            @pc.on('connectionstatechange')
            async def on_connectionstatechange():
                logger.info(f"Connection state: {pc.connectionState}")
                if pc.connectionState in ['failed', 'closed']:
                    await pc.close()
                    self.pcs.discard(pc)
                    if pc_id in self.audio_receivers:
                        try:
                            self.audio_receivers[pc_id].stop()
                        except:
                            pass
                        del self.audio_receivers[pc_id]
            
            # Handle incoming audio track from web client
            @pc.on('track')
            async def on_track(track):
                logger.info(f"✓ Track received: {track.kind}")
                
                if track.kind == 'audio':
                    if not PYAUDIO_AVAILABLE or self.output_device is None:
                        logger.warning("Cannot receive audio - no output device")
                        return
                    
                    try:
                        audio_receiver = AudioReceiver(self.output_device)
                        self.audio_receivers[pc_id] = audio_receiver
                        logger.info("✓ Audio receiver created")
                    except Exception as e:
                        logger.error(f"Failed to create audio receiver: {e}")
                        logger.error(traceback.format_exc())
                        return
                    
                    # Receive audio frames
                    async def receive_audio():
                        try:
                            while True:
                                frame = await track.recv()
                                # Convert to stereo if needed
                                audio_array = frame.to_ndarray()
                                
                                # Ensure stereo
                                if audio_array.shape[0] == 1:
                                    audio_array = np.repeat(audio_array, 2, axis=0)
                                
                                audio_data = audio_array.tobytes()
                                audio_receiver.add_audio(audio_data)
                        except MediaStreamError:
                            logger.info("Audio track ended")
                        except Exception as e:
                            logger.error(f"Audio receive error: {e}")
                    
                    asyncio.create_task(receive_audio())
            
            # Set remote description FIRST
            await pc.setRemoteDescription(offer)
            
            # Log transceivers from offer
            transceivers = pc.getTransceivers()
            logger.info(f"Total transceivers: {len(transceivers)}")
            for t in transceivers:
                logger.info(f"  Transceiver: mid={t.mid}, direction={t.direction}, kind={t.kind if hasattr(t, 'kind') else 'unknown'}")
            
            # Find a video transceiver or add video track
            camera_track = None
            video_transceiver = None
            
            try:
                camera_track = PiCameraTrack()
                
                # Look for an existing video transceiver from the offer
                for t in transceivers:
                    if hasattr(t.sender, 'track') and t.sender.track and t.sender.track.kind == 'video':
                        video_transceiver = t
                        break
                    if hasattr(t.receiver, 'track') and t.receiver.track and t.receiver.track.kind == 'video':
                        video_transceiver = t
                        break
                
                if video_transceiver:
                    # Replace the track on existing transceiver
                    await video_transceiver.sender.replaceTrack(camera_track)
                    logger.info("✓ Video track replaced on existing transceiver")
                else:
                    # Add new video track (creates new transceiver)
                    pc.addTrack(camera_track)
                    logger.info("✓ Video track added as new transceiver")
                    
            except Exception as e:
                logger.error(f"Failed to add video track: {e}")
                logger.error(traceback.format_exc())
                return web.Response(
                    status=500,
                    content_type='application/json',
                    text=json.dumps({'error': f'Camera failed: {str(e)}'})
                )
            
            # Create answer
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            
            response_data = {
                'sdp': pc.localDescription.sdp,
                'type': pc.localDescription.type
            }
            
            logger.info("✓ WebRTC negotiation successful")
            
            return web.Response(
                content_type='application/json',
                text=json.dumps(response_data)
            )
            
        except Exception as e:
            logger.error(f"Error in offer handler: {e}")
            logger.error(traceback.format_exc())
            return web.Response(
                status=500,
                content_type='application/json',
                text=json.dumps({'error': str(e)})
            )
    
    async def on_shutdown(self, app):
        """Cleanup on shutdown"""
        logger.info("Shutting down...")
        
        # Close all peer connections
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros)
        self.pcs.clear()
        
        # Stop all audio receivers
        for receiver in self.audio_receivers.values():
            try:
                receiver.stop()
            except:
                pass
        self.audio_receivers.clear()
        
        # Cleanup camera
        PiCameraTrack.cleanup()
        logger.info("Shutdown complete")


def create_self_signed_cert():
    """Create a self-signed certificate for HTTPS"""
    cert_file = 'cert.pem'
    key_file = 'key.pem'
    
    if os.path.exists(cert_file) and os.path.exists(key_file):
        logger.info("Using existing certificate")
        return cert_file, key_file
    
    try:
        from OpenSSL import crypto
    except ImportError:
        logger.error("pyOpenSSL not installed. Install with: pip install pyopenssl")
        return None, None
    
    logger.info("Generating self-signed certificate...")
    
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)
    
    cert = crypto.X509()
    cert.get_subject().C = "US"
    cert.get_subject().ST = "State"
    cert.get_subject().L = "City"
    cert.get_subject().O = "Organization"
    cert.get_subject().OU = "Organizational Unit"
    cert.get_subject().CN = "localhost"
    cert.set_serial_number(1000)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365*24*60*60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)
    cert.sign(k, 'sha256')
    
    with open(cert_file, "wb") as f:
        f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    
    with open(key_file, "wb") as f:
        f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))
    
    logger.info(f"Certificate created: {cert_file}, {key_file}")
    return cert_file, key_file


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='WebRTC PiCamera Server')
    parser.add_argument('--http', action='store_true', help='Use HTTP instead of HTTPS')
    parser.add_argument('--port', type=int, default=8080, help='Server port (default: 8080)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Server host (default: 0.0.0.0)')
    parser.add_argument('--width', type=int, default=640, help='Video width (default: 640, try 320 for lower CPU)')
    parser.add_argument('--height', type=int, default=480, help='Video height (default: 480, try 240 for lower CPU)')
    parser.add_argument('--fps', type=int, default=15, help='Frame rate (default: 15, try 10 for lower CPU)')
    args = parser.parse_args()
    
    logger.info("="*60)
    logger.info("WebRTC PiCamera Server")
    logger.info(f"Video: {args.width}x{args.height} @ {args.fps}fps")
    logger.info("="*60)
    
    if not PICAMERA_AVAILABLE:
        logger.error("❌ picamera2 not available - cannot start server")
        logger.info("Install with: pip install picamera2")
        return
    
    if not PYAUDIO_AVAILABLE:
        logger.warning("⚠️  pyaudio not available - audio playback disabled")
        logger.info("Install with: pip install pyaudio")
    
    server = WebRTCServer()
    app = web.Application()
    
    # Store video settings in app for access in handlers
    app.video_width = args.width
    app.video_height = args.height
    app.video_fps = args.fps
    
    app.router.add_get('/', server.index)
    app.router.add_post('/offer', server.offer)
    app.on_shutdown.append(server.on_shutdown)
    
    if args.http:
        logger.warning("⚠️  Running in HTTP mode - getUserMedia will only work on localhost!")
        logger.info(f"🚀 Starting server on http://{args.host}:{args.port}")
        web.run_app(app, host=args.host, port=args.port, print=None)
    else:
        cert_file, key_file = create_self_signed_cert()
        if cert_file and key_file:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(cert_file, key_file)
            
            logger.info(f"🚀 Starting server on https://{args.host}:{args.port}")
            logger.info("⚠️  Accept the security warning in your browser for the self-signed certificate")
            web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context, print=None)
        else:
            logger.error("Failed to create certificate. Running in HTTP mode instead.")
            logger.info(f"🚀 Starting server on http://{args.host}:{args.port}")
            web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == '__main__':
    main()