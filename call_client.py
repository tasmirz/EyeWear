#!/usr/bin/env python3
"""
WebRTC Video Client with Authentication and Shared Memory IPC
"""

import os
import asyncio
import base64
import json
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import websockets
import sys
import signal
import struct
import requests
from multiprocessing.shared_memory import SharedMemory
import traceback
from common import IPC

from dotenv import load_dotenv

load_dotenv()
# Configuration
SIGNALING_SERVER = os.getenv("SIGNALING_SERVER", "ws://192.168.3.105:8081")
API_SERVER = os.getenv("API_SERVER", "http://192.168.3.105:8081")
STUN_SERVER = os.getenv("STUN_SERVER", "stun://stun.l.google.com:19302")

ipc = None

# Shared memory for IPC
SHM_SIZE = 4
SHM_NAME = "call_signal"

shm = None

# read pem from keys/device_public.pem
with open("keys/device_public.pem", "r") as f:
    PUBLIC_KEY = f.read().strip()
    # Remove header/footer if present
    if PUBLIC_KEY.startswith("-----BEGIN PUBLIC KEY-----"):
        PUBLIC_KEY = "\n".join(PUBLIC_KEY.split("\n")[1:-1]).strip()
    
# read pem from keys/device_private.pem
with open("keys/device_private.pem", "r") as f:
    PRIVATE_KEY = f.read().strip()

# Action codes
ACTION_REQUEST_CALL = 1
ACTION_STOP_CALL = 2
ACTION_MUTE_UNMUTE = 3


class WebRTCClient:
    def __init__(self):
        Gst.init(None)
        self.pipe = None
        self.webrtc = None
        self.ws = None
        self.loop = None
        self.peer_id = None
        self.connected = False
        self.offer_created = False
        self.auth_token = None
        self.in_call = False
        self.muted = False
        self.pipeline_playing = False
        
        # Shared memory
        self.setup_shared_memory()
        self.setup_signal_handlers()
        
    def setup_shared_memory(self):
        global shm
        """Setup shared memory for IPC"""
        try:
            # Try to create or open existing shared memory
            try:
                shm = SharedMemory(name=SHM_NAME, create=True, size=SHM_SIZE)
                print(f"✅ Created shared memory: {SHM_NAME}")
            except FileExistsError:
                shm = SharedMemory(name=SHM_NAME, create=False, size=SHM_SIZE)
                print(f"✅ Opened existing shared memory: {SHM_NAME}")
            
            # Initialize to 0
            struct.pack_into('i', shm.buf, 0, 0)
            
        except Exception as e:
            print(f"❌ Error setting up shared memory: {e}")
            
    def setup_signal_handlers(self):
        """Setup signal handlers for IPC"""
        signal.signal(signal.SIGUSR1, self.signal_handler)
        print("✅ Signal handlers registered (SIGUSR1)")
        
    def signal_handler(self, signum, frame):
        """Handle signals from other processes"""
        if not shm:
            return
            
        try:
            action_code = struct.unpack('i', shm.buf[:4])[0]
            print(f"\n🔔 Signal received! Action code: {action_code}")
            
            if action_code == ACTION_REQUEST_CALL:
                print("📞 Request Call signal received")
                if self.loop and not self.in_call:
                    asyncio.run_coroutine_threadsafe(self.request_call(), self.loop)
                    
            elif action_code == ACTION_STOP_CALL:
                print("📴 Stop Call signal received")
                if self.loop and self.in_call:
                    asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
                    
            elif action_code == ACTION_MUTE_UNMUTE:
                print("🔇 Mute/Unmute signal received")
                if self.loop and self.in_call:
                    asyncio.run_coroutine_threadsafe(self.toggle_mute(), self.loop)
            
            # Reset signal
            struct.pack_into('i', shm.buf, 0, 0)
            
        except Exception as e:
            print(f"❌ Error handling signal: {e}")
            
    async def authenticate(self):
        """Authenticate with the server using public key challenge"""
        try:
            print("🔐 Starting authentication...")
            
            # Step 1: Get challenge
            response = requests.post(
                f"{API_SERVER}/api/challenge",
                json={"publicKey": PUBLIC_KEY},
                timeout=10
            )
            
            if response.status_code != 200:
                print(f"❌ Challenge failed: {response.json()}")
                return False
                
            challenge_data = response.json()
            challenge_token = challenge_data['challengeToken']
            challenge_text = challenge_data['challengeText']
            
            print(f"✅ Received challenge")
            print(f"   Token: {challenge_token}")
            print(f"   Text: {challenge_text}")
            
            # Step 2: Sign challenge
            signed_challenge = self.sign_challenge(challenge_text)
            
            # Step 3: Verify signature and get final token
            response = requests.post(
                f"{API_SERVER}/api/auth",
                json={
                    "challengeToken": challenge_token,
                    "signedChallenge": signed_challenge
                },
                timeout=10
            )
            
            if response.status_code != 200:
                print(f"❌ Authentication failed: {response.json()}")
                return False
                
            auth_data = response.json()
            self.auth_token = auth_data['token']
            
            print("✅ Authentication successful!")
            return True
            
        except Exception as e:
            print(f"❌ Authentication error: {e}")
            return False
            
    def sign_challenge(self, challenge_text):
        """Sign the challenge text with private key"""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        
        private_key = serialization.load_pem_private_key(
            PRIVATE_KEY.encode(),
            password=None,
            backend=default_backend()
        )
        signature = private_key.sign(
            challenge_text.encode(),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

    async def stop_call(self):
        """Stop current call and reset for next call"""
        if not self.in_call:
            return
            
        print("📴 Stopping call...")
        self.in_call = False
        self.peer_id = None
        self.offer_created = False
        
        # Properly stop and reset pipeline
        if self.pipe and self.pipeline_playing:
            print("🛑 Stopping pipeline...")
            self.pipe.set_state(Gst.State.NULL)
            self.pipeline_playing = False
            await asyncio.sleep(1)  # Allow time for pipeline to fully stop
            
        # Recreate the pipeline for next call
        if self.pipe:
            print("🔄 Cleaning up old pipeline...")
            self.cleanup_pipeline()
            
        # Create fresh pipeline for next call
        print("🔄 Creating fresh pipeline for next call...")
        if not self.create_pipeline():
            print("❌ Failed to recreate pipeline")
            return False
            
        if self.ws and self.connected:
            try:
                await self.ws.send(json.dumps({
                    'type': 'call_ended'
                }))
                print("✅ Call ended notification sent")
            except Exception as e:
                print(f"⚠️ Error sending call_ended: {e}")
                
        return True
        
    def cleanup_pipeline(self):
        """Clean up pipeline resources"""
        if self.pipe:
            # Remove signal handlers first
            if self.webrtc:
                try:
                    self.webrtc.disconnect_by_func(self.on_negotiation_needed)
                    self.webrtc.disconnect_by_func(self.on_ice_candidate)
                except:
                    pass
                    
            # Remove bus watch
            bus = self.pipe.get_bus()
            bus.remove_signal_watch()
            
            # Set state to NULL
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
            self.webrtc = None
            
    def create_pipeline(self):
        """Create optimized camera pipeline - called fresh for each call"""
        try:
            # Clean up any existing pipeline first
            if self.pipe:
                self.cleanup_pipeline()
                
            pipeline_str = f"""
                webrtcbin name=webrtc 
                stun-server={STUN_SERVER} 
                bundle-policy=max-bundle
                latency=100
                
                libcamerasrc 
                ! video/x-raw,width=640,height=480,framerate=30/1 
                ! queue max-size-buffers=1 leaky=downstream 
                ! videoconvert 
                ! queue max-size-buffers=1 leaky=downstream
                ! x264enc 
                   speed-preset=ultrafast 
                   tune=zerolatency 
                   bitrate=512 
                   key-int-max=30 
                   byte-stream=true 
                   threads=4
                ! video/x-h264,profile=constrained-baseline,level=(string)3.1
                ! queue max-size-buffers=1 leaky=downstream
                ! h264parse config-interval=1
                ! rtph264pay 
                   config-interval=1 
                   pt=96 
                   mtu=1200
                ! application/x-rtp,media=video,encoding-name=H264,payload=96
                ! webrtc.
            """
            
            print("🔄 Creating fresh pipeline...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            
            if not self.webrtc:
                return self.create_fallback_pipeline()
            
            # Set WebRTC properties
            self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            
            # Connect signals - IMPORTANT: Do this fresh each time
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
            
            # Setup bus monitoring
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)
            
            print("✅ Fresh pipeline created successfully!")
            return True
            
        except Exception as e:
            print(f"❌ Error creating pipeline: {e}")
            traceback.print_exc()
            return self.create_fallback_pipeline()
    
    def create_fallback_pipeline(self):
        """Fallback test pattern pipeline"""
        try:
            pipeline_str = f"""
                webrtcbin name=webrtc stun-server={STUN_SERVER}
                videotestsrc pattern=ball is-live=true 
                ! video/x-raw,width=640,height=480,framerate=15/1 
                ! videoconvert 
                ! x264enc speed-preset=ultrafast tune=zerolatency 
                ! video/x-h264,profile=baseline 
                ! h264parse 
                ! rtph264pay config-interval=1 pt=96 
                ! application/x-rtp,media=video,encoding-name=H264,payload=96 
                ! webrtc.
            """
            
            print("Creating fallback pipeline...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
            
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)
            
            print("✓ Fallback pipeline created!")
            return True
            
        except Exception as e:
            print(f"❌ Failed to create fallback pipeline: {e}")
            return False
            
    async def request_call(self):
        """Request a call - adds to queue"""
        if not self.connected or not self.ws:
            print("❌ Not connected to signaling server")
            return
            
        try:
            await self.ws.send(json.dumps({
                'type': 'request_call'
            }))
            print("📞 Call request sent to queue")
            
        except Exception as e:
            print(f"❌ Error requesting call: {e}")
                    
    async def toggle_mute(self):
        """Toggle mute state"""
        self.muted = not self.muted
        print(f"🔇 Audio {'MUTED' if self.muted else 'UNMUTED'}")
        
    async def force_create_offer(self):
        """Force create and send an offer"""
        if self.offer_created or not self.in_call:
            print("⚠️ Offer already created or not in call, skipping...")
            return
            
        print("🚀 Creating WebRTC offer...")
        self.offer_created = True
        
        try:
            promise = Gst.Promise.new_with_change_func(self.on_offer_created, self.webrtc, None)
            self.webrtc.emit('create-offer', None, promise)
        except Exception as e:
            print(f"❌ Error creating offer: {e}")
            self.offer_created = False
        
    def on_negotiation_needed(self, element):
        """Called when negotiation is needed"""
        print("🎯 Negotiation needed")
        if self.loop and not self.offer_created and self.in_call:
            asyncio.run_coroutine_threadsafe(self.force_create_offer(), self.loop)
        
    def on_offer_created(self, promise, element, _):
        """Handle created offer"""
        try:
            if not self.in_call:
                print("⚠️ Not in call, ignoring offer creation")
                return
                
            reply = promise.get_reply()
            if not reply:
                print("❌ No reply from offer creation")
                self.offer_created = False
                return
                
            offer = reply.get_value('offer')
            if not offer:
                print("❌ No offer in reply!")
                self.offer_created = False
                return
            
            sdp_text = offer.sdp.as_text()
            
            promise = Gst.Promise.new()
            element.emit('set-local-description', offer, promise)
            promise.interrupt()
            
            if self.loop and self.connected and self.peer_id and self.in_call:
                asyncio.run_coroutine_threadsafe(self.send_sdp_offer(sdp_text), self.loop)
                
        except Exception as e:
            print(f"❌ Error in on_offer_created: {e}")
            traceback.print_exc()
            self.offer_created = False
        
    async def send_sdp_offer(self, sdp):
        """Send SDP offer to signaling server"""
        if self.ws and self.connected and self.peer_id:
            try:
                message = {
                    'type': 'offer',
                    'sdp': sdp,
                    'to': self.peer_id
                }
                await self.ws.send(json.dumps(message))
                print("✅ Offer sent!")
            except Exception as e:
                print(f"❌ Error sending offer: {e}")
        
    def on_ice_candidate(self, element, mline_index, candidate):
        """Handle ICE candidates"""
        if self.loop and self.connected and self.peer_id and self.in_call:
            asyncio.run_coroutine_threadsafe(
                self.send_ice_candidate(mline_index, candidate), 
                self.loop
            )
        
    async def send_ice_candidate(self, mline_index, candidate):
        """Send ICE candidate to signaling server"""
        if self.ws and self.connected and self.peer_id:
            try:
                message = {
                    'type': 'candidate',
                    'candidate': {
                        'candidate': candidate,
                        'sdpMLineIndex': mline_index
                    },
                    'to': self.peer_id
                }
                await self.ws.send(json.dumps(message))
            except Exception as e:
                print(f"❌ Error sending ICE candidate: {e}")
        
    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        t = message.type
        if t == Gst.MessageType.EOS:
            print("📺 End-of-stream")
            if self.loop and self.in_call:
                asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
                
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"❌ Pipeline ERROR: {err}")
            if debug:
                print(f"Debug: {debug}")
            if self.loop and self.in_call:
                asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
                
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"⚠ Pipeline WARNING: {warn}")
            
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipe:
                old, new, pending = message.parse_state_changed()
                print(f"🔧 Pipeline: {old.value_nick} → {new.value_nick}")
                
                if new == Gst.State.PLAYING:
                    self.pipeline_playing = True
                    if self.peer_id and not self.offer_created and self.in_call:
                        print("🎬 Pipeline PLAYING - forcing offer in 2 seconds...")
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(self.delayed_offer_creation(), self.loop)
                elif new == Gst.State.NULL:
                    self.pipeline_playing = False
                    
        elif t == Gst.MessageType.STREAM_START:
            print("🎬 Stream started!")
                
    async def delayed_offer_creation(self):
        """Create offer after delay"""
        await asyncio.sleep(2)
        if self.in_call and not self.offer_created:
            await self.force_create_offer()
    
    async def handle_answer(self, sdp_text):
        """Handle SDP answer from operator"""
        print("📥 Received answer from operator")
        
        try:
            ret, sdp_msg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp_text.encode()), sdp_msg)
            
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER,
                sdp_msg
            )
            
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
            print("✅ Remote description set")
            
        except Exception as e:
            print(f"❌ Error handling answer: {e}")
            traceback.print_exc()
        
    async def handle_ice_candidate(self, candidate_data):
        """Handle ICE candidate from operator"""
        try:
            if candidate_data and 'candidate' in candidate_data:
                candidate = candidate_data['candidate']
                mline_index = candidate_data.get('sdpMLineIndex', 0)
                
                self.webrtc.emit('add-ice-candidate', mline_index, candidate)
        except Exception as e:
            print(f"❌ Error handling ICE candidate: {e}")
            
    async def handle_call_accepted(self, operator_id):
        """Handle call acceptance - start fresh pipeline"""
        print(f"\n📞 CALL ACCEPTED by operator: {operator_id}")
        self.peer_id = operator_id
        self.in_call = True
        
        # Make sure we have a fresh pipeline
        if not self.pipe:
            print("🔄 Creating pipeline for accepted call...")
            if not self.create_pipeline():
                print("❌ Failed to create pipeline for call")
                return
                
        # Start pipeline
        print("🎬 Starting video stream...")
        result = self.pipe.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            print("❌ Failed to start pipeline!")
            await self.stop_call()
        else:
            print("✅ Pipeline started successfully")
            self.pipeline_playing = True
            
    async def connect_signaling(self):
        """Connect to signaling server"""
        print(f"🔗 Connecting to {SIGNALING_SERVER}...")
        try:
            self.ws = await websockets.connect(
                SIGNALING_SERVER, 
                ping_interval=20, 
                ping_timeout=30
            )
            self.connected = True
            print("✅ Connected to signaling server")
            
            # Authenticate WebSocket connection
            await self.ws.send(json.dumps({
                'type': 'authenticate',
                'token': self.auth_token
            }))
            print("✅ WebSocket authenticated")
            
        except Exception as e:
            print(f"❌ Connection error: {e}")
            raise
        
    async def handle_signaling(self):
        """Handle signaling messages"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get('type')
                    
                    print(f"📥 Received: {msg_type}")
                    
                    if msg_type == 'authenticated':
                        device_id = data.get('deviceId')
                        print(f"✅ Authenticated as device: {device_id}")
                        
                    elif msg_type == 'call_queued':
                        position = data.get('position')
                        print(f"📞 Call added to queue (position: {position})")
                        
                    elif msg_type == 'call_accepted':
                        operator_id = data.get('operatorId')
                        await self.handle_call_accepted(operator_id)
                        
                    elif msg_type == 'answer':
                        if self.in_call:
                            await self.handle_answer(data['sdp'])
                        else:
                            print("⚠️ Received answer but not in call")
                        
                    elif msg_type == 'candidate':
                        if self.in_call and self.webrtc:
                            await self.handle_ice_candidate(data.get('candidate'))
                        else:
                            print("⚠️ Received ICE candidate but not in call")
                            
                    elif msg_type == 'peer_disconnected':
                        print("\n📴 Operator disconnected")
                        await self.stop_call()
                        
                    elif msg_type == 'error':
                        print(f"❌ Server error: {data.get('message')}")
                        
                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error: {e}")
                except Exception as e:
                    print(f"❌ Error processing message: {e}")
                    traceback.print_exc()
                    
        except websockets.exceptions.ConnectionClosed:
            print("❌ WebSocket connection closed")
            self.connected = False
        except Exception as e:
            print(f"❌ Signaling error: {e}")
            self.connected = False
            
    async def run(self):
        """Main run loop"""
        try:
            self.loop = asyncio.get_running_loop()
            
            # Authenticate with server
            if not await self.authenticate():
                print("❌ Authentication failed - exiting")
                return
            
            # Create initial pipeline
            if not self.create_pipeline():
                print("❌ Failed to create pipeline")
                return
            
            # Connect to signaling server
            await self.connect_signaling()
            
            print("\n" + "="*60)
            print("✅ READY! WebRTC Client Running")
            print("="*60)
            print("\nTo request a call, send SIGUSR1 signal:")
            print(f"  kill -SIGUSR1 {os.getpid()}")
            print("\nOr write to shared memory:")
            print(f"  Action codes: 1=Request Call, 2=Stop Call, 3=Mute/Unmute")
            print("="*60 + "\n")
            
            # Handle signaling
            await self.handle_signaling()
            
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down...")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            traceback.print_exc()
        finally:
            self.cleanup()
            
    def cleanup(self):
        """Clean up all resources"""
        print("🧹 Cleaning up...")
        self.connected = False
        self.in_call = False
        self.offer_created = False
        self.peer_id = None
        
        # Clean up pipeline
        self.cleanup_pipeline()
            
        if self.ws:
            asyncio.create_task(self.ws.close())
            
        print("✅ Cleanup complete")


async def main():
    import os
    global ipc
    ipc = IPC("call_client")
    
    # Print process ID for signal sending
    print(f"Process ID: {os.getpid()}")
    
    client = WebRTCClient()
    await client.run()


if __name__ == "__main__":
    import os
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Exiting...")
        if shm:
            shm.unlink()
            shm.close()
        if ipc:
            ipc.cleanup()
        sys.exit(0)