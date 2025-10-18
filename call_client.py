#!/usr/bin/env python3
"""
WebRTC Video Client with Continuous Streaming Audio
- Video: WebRTC (libcamera → H.264)
- Audio OUT: arecord stdout → WebSocket base64 streaming
- Audio IN: WebSocket → aplay stdin streaming
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
import threading

load_dotenv()

# Configuration
SIGNALING_SERVER = os.getenv("SIGNALING_SERVER", "ws://192.168.3.105:8081")
API_SERVER = os.getenv("API_SERVER", "http://192.168.3.105:8081")
STUN_SERVER = os.getenv("STUN_SERVER", "stun://stun.l.google.com:19302")
BT_MAC = os.getenv("BT_MAC", "XX:XX:XX:XX:XX:XX")

# Audio configuration
AUDIO_CHUNK_SIZE = 4096  # Read in 4KB chunks for smooth streaming

# Shared memory for external signals
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

# Action codes
ACTION_REQUEST_CALL = 1
ACTION_STOP_CALL = 2
ACTION_MUTE_UNMUTE = 3

def bluealsa_dev_string(mac, profile):
    """Build bluealsa device string"""
    return f"bluealsa:DEV={mac},PROFILE={profile}"

class BluetoothProfileManager:
    """Minimal manager for bluealsa + BlueZ flows"""
    def __init__(self, bt_mac, settle=1.0):
        self.bt_mac = bt_mac
        self.current_profile = None
        self.settle = settle

    def _run(self, cmd, timeout=5):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"

    def ensure_connected(self):
        """Ensure device is connected via bluetoothctl"""
        rc, out, err = self._run(["bluetoothctl", "info", self.bt_mac])
        if rc != 0 or "Connected: yes" not in out:
            rc, out, err = self._run(["bluetoothctl", "connect", self.bt_mac], timeout=10)
            if rc == 0:
                time.sleep(self.settle)
                return True
            return False
        return True

    def _reconnect(self):
        """Disconnect then connect to force profile negotiation"""
        self._run(["bluetoothctl", "disconnect", self.bt_mac], timeout=5)
        time.sleep(0.5)
        rc, out, err = self._run(["bluetoothctl", "connect", self.bt_mac], timeout=10)
        time.sleep(self.settle)
        return rc == 0

    def switch_to_sco(self):
        """Request reconnect for SCO/HFP"""
        try:
            print("🔄 Requesting reconnect for SCO/HFP...")
            if not self.ensure_connected():
                print("⚠ Device not connected; attempting connect")
            ok = self._reconnect()
            if ok:
                self.current_profile = "sco"
                print("✅ Reconnected; expect SCO devices available")
                return True
            else:
                print("⚠ Failed to reconnect Bluetooth device")
                return False
        except Exception as e:
            print(f"❌ Error switching to SCO: {e}")
            return False

    def switch_to_a2dp(self):
        """Request reconnect for A2DP"""
        try:
            print("🔄 Requesting reconnect for A2DP...")
            ok = self._reconnect()
            if ok:
                self.current_profile = "a2dp"
                print("✅ Reconnected; expect A2DP devices available")
                return True
            else:
                print("⚠ Failed to reconnect Bluetooth device")
                return False
        except Exception as e:
            print(f"❌ Error switching to A2DP: {e}")
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
        self.bt_manager = BluetoothProfileManager(BT_MAC)

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
                if self.loop and self.in_call:
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

    def start_arecord(self):
        """Start arecord subprocess for continuous microphone capture"""
        try:
            self.stop_arecord()
            
            # Use bluealsa device for Bluetooth
            alsa_device = bluealsa_dev_string(BT_MAC, "sco")
            
            cmd = [
                'arecord',
                '-D', alsa_device,
                '-f', 'S16_LE',  # RAW PCM 16-bit signed little-endian
                '-r', '16000',   # 16kHz sample rate (voice quality)
                '-c', '1',       # Mono
                '-t', 'raw',     # RAW format (no WAV header)
            ]
            
            print(f"🎤 Starting arecord: {' '.join(cmd)}")
            self.arecord_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Check if process started successfully
            time.sleep(0.2)
            if self.arecord_process.poll() is not None:
                stderr = self.arecord_process.stderr.read().decode('utf-8', errors='ignore')
                print(f"❌ arecord failed: {stderr}")
                self.arecord_process = None
                return False
            
            print(f"✅ arecord started (PID: {self.arecord_process.pid})")
            return True
            
        except Exception as e:
            print(f"❌ Error starting arecord: {e}")
            traceback.print_exc()
            return False

    def stop_arecord(self):
        """Stop arecord process"""
        if self.arecord_process:
            try:
                print("🛑 Stopping arecord...")
                self.arecord_process.terminate()
                self.arecord_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.arecord_process.kill()
            except Exception as e:
                print(f"⚠️ Error stopping arecord: {e}")
            finally:
                self.arecord_process = None

    def start_aplay(self):
        """Start aplay subprocess for continuous audio playback"""
        try:
            self.stop_aplay()
            
            # Use bluealsa device for Bluetooth
            alsa_device = bluealsa_dev_string(BT_MAC, "sco")
            
            cmd = [
                'aplay',
                '-D', alsa_device,
                '-f', 'S16_LE',  # RAW PCM 16-bit
                '-r', '16000',   # 16kHz (voice quality)
                '-c', '1',       # Mono
                '-t', 'raw',     # RAW format
                '-q'             # Quiet mode
            ]
            
            print(f"🔊 Starting aplay: {' '.join(cmd)}")
            self.aplay_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            time.sleep(0.2)
            if self.aplay_process.poll() is not None:
                stderr = self.aplay_process.stderr.read().decode('utf-8', errors='ignore')
                print(f"❌ aplay failed: {stderr}")
                self.aplay_process = None
                return False
            
            print(f"✅ aplay started (PID: {self.aplay_process.pid})")
            return True
            
        except Exception as e:
            print(f"❌ Error starting aplay: {e}")
            traceback.print_exc()
            return False

    def stop_aplay(self):
        """Stop aplay process"""
        if self.aplay_process:
            try:
                print("🛑 Stopping aplay...")
                if self.aplay_process.stdin:
                    self.aplay_process.stdin.close()
                self.aplay_process.terminate()
                self.aplay_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.aplay_process.kill()
            except Exception as e:
                print(f"⚠️ Error stopping aplay: {e}")
            finally:
                self.aplay_process = None

    async def audio_send_loop(self):
        """Continuously read from arecord and send to operator via WebSocket"""
        try:
            print("🎤 Starting continuous audio send loop...")
            chunk_count = 0
            
            while self.in_call and self.arecord_process:
                if not self.audio_enabled or self.muted:
                    await asyncio.sleep(0.01)
                    continue
                
                try:
                    # Read audio chunk from arecord stdout
                    audio_data = self.arecord_process.stdout.read(AUDIO_CHUNK_SIZE)
                    
                    if not audio_data:
                        print("⚠️ No audio data from arecord, process may have ended")
                        break
                    
                    # Encode to base64 and send via WebSocket
                    audio_b64 = base64.b64encode(audio_data).decode('utf-8')
                    
                    if self.ws and self.connected:
                        await self.ws.send(json.dumps({
                            'type': 'audio_data',
                            'data': audio_b64,
                            'to': self.peer_id
                        }))
                        
                        chunk_count += 1
                        if chunk_count % 100 == 0:
                            print(f"🎤 Sent {chunk_count} audio chunks")
                    
                    # Small delay to prevent overwhelming the WebSocket
                    await asyncio.sleep(0.001)
                    
                except BrokenPipeError:
                    print("⚠️ Broken pipe in audio send")
                    break
                except Exception as e:
                    if self.in_call:
                        print(f"⚠️ Audio send error: {e}")
                    break
                    
        except Exception as e:
            print(f"❌ Audio send loop error: {e}")
            traceback.print_exc()
        finally:
            print(f"🛑 Audio send loop stopped (sent {chunk_count} chunks)")

    async def audio_receive_loop(self):
        """Handle audio data reception (actual writing happens in handle_audio_data)"""
        try:
            print("🔊 Audio receive loop ready...")
            chunk_count = 0
            
            while self.in_call:
                await asyncio.sleep(0.1)
                
        except Exception as e:
            print(f"❌ Audio receive loop error: {e}")
        finally:
            print("🛑 Audio receive loop stopped")

    async def handle_audio_data(self, audio_b64):
        """Handle received audio data from operator and write to aplay"""
        try:
            if not self.in_call:
                return
            
            # Decode base64 audio data
            audio_data = base64.b64decode(audio_b64)
            
            # Check if aplay is still running
            if not self.aplay_process or self.aplay_process.poll() is not None:
                print("⚠️ aplay process died, restarting...")
                if not self.start_aplay():
                    print("❌ Failed to restart aplay")
                    return
            
            # Write to aplay stdin with error handling
            if self.aplay_process and self.aplay_process.stdin:
                try:
                    self.aplay_process.stdin.write(audio_data)
                    self.aplay_process.stdin.flush()
                except BrokenPipeError:
                    print("⚠️ Broken pipe to aplay, restarting...")
                    if self.start_aplay():
                        # Try writing again after restart
                        try:
                            self.aplay_process.stdin.write(audio_data)
                            self.aplay_process.stdin.flush()
                        except Exception:
                            pass
                except Exception as e:
                    print(f"⚠️ Error writing to aplay: {e}")
                    
        except Exception as e:
            if self.in_call:
                print(f"⚠️ Error handling audio data: {e}")

    async def stop_call(self):
        if not self.in_call:
            return
        print("📴 Stopping call...")
        self.in_call = False
        self.peer_id = None
        self.offer_created = False
        
        # Stop video+audio pipeline
        if self.pipe and self.pipeline_playing:
            print("🛑 Stopping pipeline...")
            self.pipe.set_state(Gst.State.NULL)
            self.pipeline_playing = False
            await asyncio.sleep(0.5)
        if self.pipe:
            print("🔄 Cleaning up old pipeline...")
            self.cleanup_pipeline()
        
        # Switch back to A2DP
        self.bt_manager.switch_to_a2dp()
        
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
        """Create WebRTC pipeline with VIDEO and AUDIO"""
        try:
            if self.pipe:
                self.cleanup_pipeline()

            # Use bluealsa device for Bluetooth audio

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

                alsasrc 
                ! queue
                ! audioresample
                ! audioconvert
                ! opusenc bitrate=32000
                ! rtpopuspay pt=97
                ! application/x-rtp,media=audio,encoding-name=OPUS,payload=97
                ! webrtc.
            """

            print("🔄 Creating WebRTC pipeline with VIDEO + AUDIO (Opus)...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            if not self.webrtc:
                print("❌ Failed to get webrtc element")
                return False

            self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)

            # Signals
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)

            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)

            print("✅ WebRTC pipeline with audio created successfully!")
            return True

        except Exception as e:
            print(f"❌ Error creating pipeline: {e}")
            traceback.print_exc()
            return False

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
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(self.delayed_offer_creation(), self.loop)
                elif new == Gst.State.NULL:
                    self.pipeline_playing = False
        elif t == Gst.MessageType.STREAM_START:
            print("🎬 Stream started!")

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
        print(f"\n📞 CALL ACCEPTED by operator: {operator_id}")
        self.peer_id = operator_id
        self.in_call = True
        
        # Switch to SCO/HFP profile for audio
        print("🔄 Switching Bluetooth to SCO/HFP profile...")
        if not self.bt_manager.switch_to_sco():
            print("⚠️ Warning: Failed to trigger SCO profile")
        
        await asyncio.sleep(1.0)
        
        # Create and start pipeline with audio+video
        if not self.pipe:
            print("🔄 Creating pipeline for accepted call...")
            if not self.create_pipeline():
                print("❌ Failed to create pipeline for call")
                await self.stop_call()
                return
        
        print("🎬 Starting audio+video stream...")
        result = self.pipe.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            print("❌ Failed to start pipeline!")
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
                    
                    # Don't log every audio_data message to avoid spam
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
            if not self.create_pipeline():
                print("❌ Failed to create pipeline")
                return
            await self.connect_signaling()
            print("\n" + "="*60)
            print("✅ READY! WebRTC Client with Audio + Video")
            print("="*60)
            print(f"\nBluetooth Device: {BT_MAC}")
            print(f"Audio: WebRTC Opus codec (via alsasrc)")
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
        
        # Switch back to A2DP profile if possible
        try:
            self.bt_manager.switch_to_a2dp()
        except Exception:
            pass
        
        if self.ws:
            try:
                asyncio.create_task(self.ws.close())
            except Exception:
                pass
        
        # Cleanup shared memory
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
    from common import IPC
    ipc=IPC("call_client")
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