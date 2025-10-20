#!/usr/bin/env python3
"""
WebRTC Video Client with Bluez-ALSA Bluetooth Audio
- Switches BT from A2DP → HFP/HSP for calls
- Video: WebRTC (libcamera → H.264)
- Audio: WebRTC (Opus bidirectional via bluez-alsa)
"""

import os
import asyncio
import base64
import json
import subprocess
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
from dotenv import load_dotenv
import time
#from audio_feedback import AudioFeedback
from common import SoundType, IPC, CallSignal

load_dotenv()

# Configuration
SIGNALING_SERVER = os.getenv("SIGNALING_SERVER", "ws://192.168.3.105:8081")
API_SERVER = os.getenv("API_SERVER", "http://192.168.3.105:8081")
STUN_SERVER = os.getenv("STUN_SERVER", "stun://stun.l.google.com:19302")
BT_MAC = os.getenv("BT_MAC", "XX:XX:XX:XX:XX:XX")

#af = AudioFeedback(SoundType.CALLING)

# Shared memory
SHM_SIZE = 4
SHM_NAME = "call_signal"
shm = None

# Load keys
with open("keys/device_public.pem", "r") as f:
    PUBLIC_KEY = f.read().strip()
    if PUBLIC_KEY.startswith("-----BEGIN PUBLIC KEY-----"):
        PUBLIC_KEY = "\n".join(PUBLIC_KEY.split("\n")[1:-1]).strip()
with open("keys/device_private.pem", "r") as f:
    PRIVATE_KEY = f.read().strip()

ACTION_REQUEST_CALL = CallSignal.START_CALL.value
ACTION_STOP_CALL = CallSignal.END_CALL.value
ACTION_MUTE_UNMUTE = CallSignal.MUTE_CALL.value


def get_bluealsa_devices():
    """List available bluez-alsa devices"""
    try:
        print("\n📋 Listing bluez-alsa PCM devices...")
        # Try bluealsa-cli first
        result = subprocess.run(['bluealsa-cli', 'list-pcms'], 
                              capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("⚠️ bluealsa-cli not available or failed")
        
        # List via arecord
        result = subprocess.run(['arecord', '-L'], 
                              capture_output=True, text=True, timeout=2)
        devices = []
        for line in result.stdout.split('\n'):
            if 'bluealsa' in line.lower():
                devices.append(line.strip())
                if BT_MAC in line or 'sco' in line.lower():
                    print(f"  ✓ {line.strip()}")
        
        # List via aplay
        result = subprocess.run(['aplay', '-L'], 
                              capture_output=True, text=True, timeout=2)
        for line in result.stdout.split('\n'):
            if 'bluealsa' in line.lower() and (BT_MAC in line or 'sco' in line.lower()):
                if line.strip() not in devices:
                    print(f"  ✓ {line.strip()}")
                    devices.append(line.strip())
        
        if devices:
            print(f"✅ Found {len(devices)} bluez-alsa device(s)")
        else:
            print("⚠️ No bluez-alsa devices found")
        return devices
    except Exception as e:
        print(f"⚠️ Error listing bluez-alsa devices: {e}")
        return []


def switch_bt_profile_bluealsa(mac, profile):
    """Switch Bluetooth profile for bluez-alsa using bluetoothctl"""
    try:
        print(f"🔄 Attempting to switch {mac} to {profile.upper()} profile...")
        
        # Disconnect first
        subprocess.run(['bluetoothctl', 'disconnect', mac], 
                      capture_output=True, timeout=5)
        time.sleep(0.5)
        
        # Reconnect (bluez will auto-select profile based on usage)
        subprocess.run(['bluetoothctl', 'connect', mac], 
                      capture_output=True, timeout=5)
        time.sleep(1.5)
        
        print(f"✅ Reconnected to {mac}")
        return True
        
    except Exception as e:
        print(f"❌ Failed to switch BT profile: {e}")
        return False


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
        self.bt_in_sco = False

        self.setup_shared_memory()
        self.setup_signal_handlers()

    def setup_shared_memory(self):
        global shm
        try:
            try:
                shm = SharedMemory(name=SHM_NAME, create=True, size=SHM_SIZE)
                print(f"✅ Created shared memory: {SHM_NAME}")
            except FileExistsError:
                shm = SharedMemory(name=SHM_NAME, create=False, size=SHM_SIZE)
                print(f"✅ Opened existing shared memory: {SHM_NAME}")
            struct.pack_into('i', shm.buf, 0, 0)
        except Exception as e:
            print(f"❌ Error setting up shared memory: {e}")

    def setup_signal_handlers(self):
        signal.signal(signal.SIGUSR1, self.signal_handler)
        print("✅ Signal handlers registered (SIGUSR1)")

    def signal_handler(self, signum, frame):
        if not shm:
            return
        try:
            action_code = struct.unpack('i', shm.buf[:4])[0]
            print(f"\n🔔 Signal received! Action code: {action_code}")
            if action_code == ACTION_REQUEST_CALL:
                if self.loop and not self.in_call:
                    asyncio.run_coroutine_threadsafe(self.request_call(), self.loop)
            elif action_code == ACTION_STOP_CALL:
                if self.loop:
                    asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
            elif action_code == ACTION_MUTE_UNMUTE:
                if self.loop and self.in_call:
                    asyncio.run_coroutine_threadsafe(self.toggle_mute(), self.loop)
            struct.pack_into('i', shm.buf, 0, 0)
        except Exception as e:
            print(f"❌ Error handling signal: {e}")

    async def authenticate(self):
        try:
            print("🔐 Starting authentication...")
            response = requests.post(
                f"{API_SERVER}/api/challenge",
                json={"publicKey": PUBLIC_KEY},
                timeout=10
            )
            if response.status_code != 200:
                print(f"❌ Challenge failed: {response.text}")
                return False
            challenge_data = response.json()
            challenge_token = challenge_data['challengeToken']
            challenge_text = challenge_data['challengeText']
            signed_challenge = self.sign_challenge(challenge_text)
            response = requests.post(
                f"{API_SERVER}/api/auth",
                json={"challengeToken": challenge_token, "signedChallenge": signed_challenge},
                timeout=10
            )
            if response.status_code != 200:
                print(f"❌ Authentication failed: {response.text}")
                return False
            auth_data = response.json()
            self.auth_token = auth_data['token']
            print("✅ Authentication successful!")
            return True
        except Exception as e:
            print(f"❌ Authentication error: {e}")
            return False

    def sign_challenge(self, challenge_text):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        private_key = serialization.load_pem_private_key(
            PRIVATE_KEY.encode(), password=None, backend=default_backend()
        )
        signature = private_key.sign(
            challenge_text.encode(),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

    async def stop_call(self):
        if not self.in_call:
            return
        print("📴 Stopping call...")
        self.in_call = False
        self.peer_id = None
        self.offer_created = False
        
        # Stop pipeline
        if self.pipe and self.pipeline_playing:
            print("🛑 Stopping pipeline...")
            self.pipe.set_state(Gst.State.NULL)
            self.pipeline_playing = False
            await asyncio.sleep(0.5)
        if self.pipe:
            print("🔄 Cleaning up old pipeline...")
            self.cleanup_pipeline()
        
        # Switch back to A2DP for music
        if self.bt_in_sco:
            print("🔄 Switching back to A2DP...")
            switch_bt_profile_bluealsa(BT_MAC, 'a2dp')
            self.bt_in_sco = False
        
        print("🔄 Creating fresh pipeline for next call...")
        if not self.create_pipeline():
            print("❌ Failed to recreate pipeline")
            return False
        
        if self.ws and self.connected:
            try:
                await self.ws.send(json.dumps({'type': 'call_ended'}))
                print("✅ Call ended notification sent")
            except Exception as e:
                print(f"⚠️ Error sending call_ended: {e}")
        return True

    def cleanup_pipeline(self):
        if self.pipe:
            if self.webrtc:
                try:
                    self.webrtc.disconnect_by_func(self.on_negotiation_needed)
                    self.webrtc.disconnect_by_func(self.on_ice_candidate)
                except Exception:
                    pass
            bus = self.pipe.get_bus()
            try:
                bus.remove_signal_watch()
            except Exception:
                pass
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
            self.webrtc = None

    def create_pipeline(self):
        """Create WebRTC pipeline with VIDEO and AUDIO via bluez-alsa"""
        try:
            if self.pipe:
                self.cleanup_pipeline()

            # Build bluez-alsa device strings
            if self.in_call and self.bt_in_sco:
                # Build the ALSA device string for bluez-alsa
                bt_device = f"bluealsa:DEV={BT_MAC},PROFILE=sco"
                
                # For GStreamer, we need to properly escape the device parameter
                # Note: We DON'T use quotes in the variable, they're added in the pipeline string
                audio_src = f"alsasrc device={bt_device}"
                audio_sink = f"alsasink device={bt_device}"
                
                print(f"🎧 Using bluez-alsa SCO device: {bt_device}")
                
                # Test if source device is accessible
                print("🧪 Testing microphone access...")
                test_result = subprocess.run(
                    ['timeout', '3', 'arecord', '-D', bt_device, '-d', '1', '-f', 'S16_LE', '-r', '8000', '-c', '1', '/dev/null'],
                    capture_output=True,
                    timeout=5
                )
                if test_result.returncode != 0:
                    print(f"⚠️ Warning: Could not test record from microphone")
                    stderr = test_result.stderr.decode() if test_result.stderr else "no error output"
                    print(f"   stderr: {stderr}")
                    
                    # If device is busy, try to wait and retry
                    if "Device or resource busy" in stderr:
                        print("   Device is busy, waiting 2 seconds...")
                        time.sleep(2)
                    elif "No such file or directory" in stderr:
                        print("   ❌ Device doesn't exist - bluealsa might not be ready")
                        print("   Falling back to default audio")
                        audio_src = "alsasrc"
                        audio_sink = "alsasink"
                else:
                    print(f"✅ Bluetooth SCO microphone is accessible")
            else:
                # When not in call, don't create pipeline
                print("⚠️ Not in call mode - should not create pipeline now")
                return False

            pipeline_str = f"""
                webrtcbin name=webrtc 
                  stun-server={STUN_SERVER}
                  bundle-policy=max-bundle
                  latency=100

                libcamerasrc af-mode=2
                ! video/x-raw,width=640,height=480,framerate=30/1
                ! queue max-size-buffers=1 leaky=downstream
                ! videoconvert
                ! queue max-size-buffers=1 leaky=downstream
                ! x264enc speed-preset=ultrafast tune=zerolatency bitrate=512 key-int-max=30 byte-stream=true threads=4
                ! video/x-h264,profile=constrained-baseline,level=(string)3.1
                ! queue max-size-buffers=1 leaky=downstream
                ! h264parse config-interval=1
                ! rtph264pay config-interval=1 pt=96 mtu=1200
                ! application/x-rtp,media=video,encoding-name=H264,payload=96
                ! webrtc.

                {audio_src}
                ! audio/x-raw,rate=8000,channels=1,format=S16LE
                ! queue max-size-buffers=10 leaky=downstream
                ! audioresample
                ! audioconvert
                ! opusenc bitrate=16000 frame-size=20
                ! rtpopuspay pt=97
                ! application/x-rtp,media=audio,encoding-name=OPUS,payload=97
                ! webrtc.
                
                webrtc. 
                ! application/x-rtp,media=audio,encoding-name=OPUS,payload=97
                ! rtpopusdepay
                ! opusdec
                ! audioconvert
                ! audioresample
                ! audio/x-raw,rate=8000,channels=1,format=S16LE
                ! queue max-size-buffers=10 leaky=downstream
                ! {audio_sink} sync=false buffer-time=100000 latency-time=20000
            """
            
            print(f"\n🔍 Pipeline preview:")
            print(f"   Audio source: {audio_src}")
            print(f"   Audio sink: {audio_sink}")

            print("🔄 Creating WebRTC pipeline with VIDEO + AUDIO (Opus via bluez-alsa)...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            if not self.webrtc:
                print("❌ Failed to get webrtc element")
                return False

            self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)

            # Signals
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
            self.webrtc.connect('pad-added', self.on_incoming_stream)
            
            # Monitor all pads for debugging
            def on_pad_added_debug(element, pad):
                pad_name = pad.get_name()
                direction = "SRC" if pad.get_direction() == Gst.PadDirection.SRC else "SINK"
                caps = pad.get_current_caps()
                caps_str = caps.to_string()[:80] if caps else "no caps yet"
                print(f"🔌 WebRTC pad: {pad_name} ({direction}) - {caps_str}")
                
            self.webrtc.connect('pad-added', on_pad_added_debug)
            
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)

            print("✅ WebRTC pipeline created successfully!")
            return True

        except Exception as e:
            print(f"❌ Error creating pipeline: {e}")
            traceback.print_exc()
            return False

    def on_incoming_stream(self, element, pad):
        """Handle incoming audio/video stream from browser"""
        caps = pad.get_current_caps()
        if caps:
            caps_str = caps.to_string()
            print(f"📥 Incoming stream: {caps_str[:100]}...")
            if 'audio' in caps_str:
                print("🎧 ✅ AUDIO stream from browser detected!")
            elif 'video' in caps_str:
                print("📹 Video stream from browser")
        else:
            print("⚠️ Incoming pad but no caps available yet")

    async def request_call(self):
        if not self.connected or not self.ws:
            print("❌ Not connected to signaling server")
            return
        try:
            await self.ws.send(json.dumps({'type': 'request_call'}))
            print("📞 Call request sent to queue")
        except Exception as e:
            print(f"❌ Error requesting call: {e}")

    async def toggle_mute(self):
        """Toggle microphone mute"""
        self.muted = not self.muted
        print(f"🔇 Microphone {'MUTED' if self.muted else 'UNMUTED'}")
        
        if self.ws and self.connected and self.peer_id:
            try:
                await self.ws.send(json.dumps({
                    'type': 'mute_status',
                    'muted': self.muted,
                    'to': self.peer_id
                }))
            except Exception as e:
                print(f"⚠️ Error sending mute status: {e}")

    async def force_create_offer(self):
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
        print("🎯 Negotiation needed")
        if self.loop and not self.offer_created and self.in_call:
            asyncio.run_coroutine_threadsafe(self.force_create_offer(), self.loop)

    def on_offer_created(self, promise, element, _):
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
            p = Gst.Promise.new()
            element.emit('set-local-description', offer, p)
            p.interrupt()
            if self.loop and self.connected and self.peer_id and self.in_call:
                asyncio.run_coroutine_threadsafe(self.send_sdp_offer(sdp_text), self.loop)
        except Exception as e:
            print(f"❌ Error in on_offer_created: {e}")
            traceback.print_exc()
            self.offer_created = False

    async def send_sdp_offer(self, sdp):
        if self.ws and self.connected and self.peer_id:
            try:
                message = {'type': 'offer', 'sdp': sdp, 'to': self.peer_id}
                await self.ws.send(json.dumps(message))
                print("✅ Offer sent!")
            except Exception as e:
                print(f"❌ Error sending offer: {e}")

    def on_ice_candidate(self, element, mline_index, candidate):
        if self.loop and self.connected and self.peer_id and self.in_call:
            asyncio.run_coroutine_threadsafe(
                self.send_ice_candidate(mline_index, candidate),
                self.loop
            )

    async def send_ice_candidate(self, mline_index, candidate):
        if self.ws and self.connected and self.peer_id:
            try:
                message = {
                    'type': 'candidate',
                    'candidate': {'candidate': candidate, 'sdpMLineIndex': mline_index},
                    'to': self.peer_id
                }
                await self.ws.send(json.dumps(message))
            except Exception as e:
                print(f"❌ Error sending ICE candidate: {e}")

    def on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("📺 End-of-stream")
            if self.loop and self.in_call:
                asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"❌ Pipeline ERROR: {err}")
            if debug:
                print(f"   Debug: {debug}")
            if self.loop and self.in_call:
                asyncio.run_coroutine_threadsafe(self.stop_call(), self.loop)
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"⚠ Pipeline WARNING: {warn}")
            if debug:
                print(f"   Debug: {debug}")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipe:
                old, new, pending = message.parse_state_changed()
                print(f"🔧 Pipeline: {old.value_nick} → {new.value_nick}")
                if new == Gst.State.PLAYING:
                    self.pipeline_playing = True
                    if self.peer_id and not self.offer_created and self.in_call:
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(self.delayed_offer_creation(), self.loop)
                elif new == Gst.State.NULL:
                    self.pipeline_playing = False
        elif t == Gst.MessageType.STREAM_START:
            src_name = message.src.get_name() if message.src else "unknown"
            print(f"🎬 Stream started on: {src_name}")

    async def delayed_offer_creation(self):
        await asyncio.sleep(2)
        if self.in_call and not self.offer_created:
            await self.force_create_offer()

    async def handle_answer(self, sdp_text):
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
        try:
            if candidate_data and 'candidate' in candidate_data:
                candidate = candidate_data['candidate']
                mline_index = candidate_data.get('sdpMLineIndex', 0)
                self.webrtc.emit('add-ice-candidate', mline_index, candidate)
        except Exception as e:
            print(f"❌ Error handling ICE candidate: {e}")

    async def handle_call_accepted(self, operator_id):
        #af.stop()
        print(f"\n📞 CALL ACCEPTED by operator: {operator_id}")
        self.peer_id = operator_id
        self.in_call = True

        # Switch to SCO for bidirectional audio
        print("🔄 Switching Bluetooth to SCO mode...")
        if switch_bt_profile_bluealsa(BT_MAC, 'sco'):
            self.bt_in_sco = True
            print("✅ Bluetooth in SCO mode")
        else:
            print("⚠️ Could not switch to SCO, using default audio")
            self.bt_in_sco = False

        # List available bluez-alsa devices
        print("\n🔍 Checking available audio devices:")
        get_bluealsa_devices()
        
        # Check if bluealsa service is running
        print("\n🔍 Checking bluealsa service status:")
        try:
            result = subprocess.run(['systemctl', 'is-active', 'bluealsa'],
                                  capture_output=True, text=True, timeout=2)
            if result.stdout.strip() == 'active':
                print("✅ bluealsa service is active")
            else:
                print(f"⚠️ bluealsa service status: {result.stdout.strip()}")
        except Exception as e:
            print(f"⚠️ Could not check bluealsa service: {e}")
        
        # Check for processes using ALSA
        print("\n🔍 Checking for processes using audio devices:")
        try:
            result = subprocess.run(['fuser', '-v', '/dev/snd/*'],
                                  capture_output=True, text=True, timeout=2)
            if result.stdout:
                print(f"   Processes using audio: {result.stdout}")
            else:
                print("   No conflicts detected")
        except Exception:
            pass

        await asyncio.sleep(2.0)  # Give BT more time to stabilize

        # Create pipeline with bluez-alsa device
        if not self.create_pipeline():
            print("❌ Failed to create pipeline for call")
            await self.stop_call()
            return

        print("🎬 Starting WebRTC audio+video stream...")
        result = self.pipe.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            print("❌ Failed to start pipeline!")
            print("   Trying to get more error details...")
            
            # Try to get the last error from the bus
            bus = self.pipe.get_bus()
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg:
                err, debug = msg.parse_error()
                print(f"   Error: {err}")
                if debug:
                    print(f"   Debug: {debug}")
            
            # Check which element failed
            print("\n   🔍 Checking pipeline elements:")
            it = self.pipe.iterate_elements()
            while True:
                result_iter, elem = it.next()
                if result_iter != Gst.IteratorResult.OK:
                    break
                if elem:
                    state = elem.get_state(0)
                    print(f"      {elem.get_name()}: {state[1].value_nick}")
            
            await self.stop_call()
        else:
            print("✅ Pipeline started - Audio + Video via WebRTC (Opus + H.264)")
            self.pipeline_playing = True

    async def connect_signaling(self):
        print(f"🔗 Connecting to {SIGNALING_SERVER}...")
        try:
            self.ws = await websockets.connect(
                SIGNALING_SERVER,
                ping_interval=20,
                ping_timeout=30
            )
            self.connected = True
            print("✅ Connected to signaling server")
            await self.ws.send(json.dumps({'type': 'authenticate', 'token': self.auth_token}))
            print("✅ WebSocket authenticated")
        except Exception as e:
            print(f"❌ Connection error: {e}")
            raise

    async def handle_signaling(self):
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get('type')
                    
                    if msg_type != 'audio_data':
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
                    elif msg_type == 'peer_disconnected':
                        print("\n📴 Operator disconnected")
                        await self.stop_call()
                    elif msg_type == 'call_ended':
                        print("\n📴 Call ended by operator")
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
        try:
            self.loop = asyncio.get_running_loop()
            if not await self.authenticate():
                print("❌ Authentication failed - exiting")
                return
            
            # List available bluez-alsa devices at startup
            print("\n🔍 Checking bluez-alsa devices...")
            get_bluealsa_devices()
            
            if not self.create_pipeline():
                print("❌ Failed to create pipeline")
                return
            await self.connect_signaling()
            print("\n" + "="*60)
            print("✅ READY! WebRTC Client with Bluez-ALSA Audio + Video")
            print("="*60)
            print(f"\nBluetooth Device: {BT_MAC}")
            print(f"Audio System: bluez-alsa (SCO profile for calls)")
            print(f"Video: WebRTC H.264")
            print("\nTo request a call, signal with SIGUSR1:")
            print(f"  kill -SIGUSR1 {os.getpid()}")
            print("="*60 + "\n")
            await self.handle_signaling()
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down...")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            traceback.print_exc()
        finally:
            self.cleanup()

    def cleanup(self):
        print("🧹 Cleaning up...")
        self.connected = False
        self.in_call = False
        self.offer_created = False
        self.peer_id = None
        
        self.cleanup_pipeline()
        
        # Restore A2DP
        if self.bt_in_sco:
            switch_bt_profile_bluealsa(BT_MAC, 'a2dp')
            self.bt_in_sco = False
        
        if self.ws:
            try:
                asyncio.create_task(self.ws.close())
            except Exception:
                pass
        
        try:
            global shm
            if shm:
                try:
                    shm.close()
                except Exception:
                    pass
                try:
                    shm.unlink()
                except Exception:
                    pass
                shm = None
        except Exception:
            pass
        print("✅ Cleanup complete")


async def main():
    print(f"Process ID: {os.getpid()}")
    client = WebRTCClient()
    await client.run()


if __name__ == "__main__":
    ipc = IPC("call_client")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Exiting...")
        try:
            if shm:
                shm.unlink()
                shm.close()
        except Exception:
            pass
        sys.exit(0)