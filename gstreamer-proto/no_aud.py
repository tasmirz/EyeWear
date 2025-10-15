#!/usr/bin/env python3
"""
WebRTC Video Client - Optimized for stable video
"""

import asyncio
import json
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import websockets
import sys
import signal

# Configuration
SIGNALING_SERVER = "ws://192.168.3.105:8081"
STUN_SERVER = "stun://stun.l.google.com:19302"

class GStreamerWebRTCVideoOnly:
    def __init__(self):
        Gst.init(None)
        self.pipe = None
        self.webrtc = None
        self.ws = None
        self.loop = None
        self.peer_id = None
        self.connected = False
        self.offer_created = False
        
    def create_pipeline(self):
        """Create optimized camera pipeline"""
        try:
            # Optimized pipeline for stable video
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
            
            print("Creating optimized pipeline...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            
            if not self.webrtc:
                print("ERROR: Could not find webrtcbin!")
                return False
            
            # Configure webrtcbin for better performance
            self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            
            # Connect signals
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
            
            # Bus messages
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)
            
            print("✓ Optimized pipeline created successfully!")
            return True
            
        except Exception as e:
            print(f"Error creating optimized pipeline: {e}")
            # Fallback to simpler pipeline
            return self.create_simple_pipeline()
    
    def create_simple_pipeline(self):
        """Simple fallback pipeline"""
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
            
            print("Creating simple fallback pipeline...")
            self.pipe = Gst.parse_launch(pipeline_str)
            self.webrtc = self.pipe.get_by_name('webrtc')
            
            # Connect signals
            self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
            
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)
            
            print("✓ Simple pipeline created successfully!")
            return True
            
        except Exception as e:
            print(f"Error creating simple pipeline: {e}")
            return False
    
    async def force_create_offer(self):
        """Force create and send an offer"""
        if self.offer_created:
            print("Offer already created, skipping...")
            return
            
        print("🚀 FORCE CREATING WEBRTC OFFER...")
        self.offer_created = True
        
        # Create the offer
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, self.webrtc, None)
        self.webrtc.emit('create-offer', None, promise)
        
    def on_negotiation_needed(self, element):
        """Called when negotiation is needed"""
        print("🎯 on-negotiation-needed signal received!")
        if self.loop and not self.offer_created:
            asyncio.run_coroutine_threadsafe(self.force_create_offer(), self.loop)
        
    def on_offer_created(self, promise, element, _):
        """Handle created offer"""
        try:
            print("✅ Offer created successfully")
            reply = promise.get_reply()
            offer = reply.get_value('offer')
            
            if not offer:
                print("ERROR: No offer in reply!")
                return
            
            sdp_text = offer.sdp.as_text()
            print(f"📄 Offer SDP length: {len(sdp_text)} bytes")
            
            # Set local description
            promise = Gst.Promise.new()
            element.emit('set-local-description', offer, promise)
            promise.interrupt()
            print("✅ Local description set")
            
            # Send offer via WebSocket
            if self.loop and self.connected and self.peer_id:
                asyncio.run_coroutine_threadsafe(self.send_sdp_offer(sdp_text), self.loop)
            else:
                print("❌ Cannot send offer - not properly connected")
                
        except Exception as e:
            print(f"❌ Error in on_offer_created: {e}")
            import traceback
            traceback.print_exc()
        
    async def send_sdp_offer(self, sdp):
        """Send SDP offer to signaling server"""
        if self.ws and self.connected and self.peer_id:
            try:
                message = {
                    'type': 'offer',
                    'sdp': sdp,
                    'to': self.peer_id
                }
                print("📤 Sending offer to signaling server...")
                await self.ws.send(json.dumps(message))
                print("✅ Offer sent to signaling server!")
            except Exception as e:
                print(f"❌ Error sending offer: {e}")
        
    def on_ice_candidate(self, element, mline_index, candidate):
        """Handle ICE candidates"""
        print(f"🧊 ICE candidate [{mline_index}]: {candidate[:50]}...")
        if self.loop and self.connected and self.peer_id:
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
                print(f"📤 Sent ICE candidate [{mline_index}]")
            except Exception as e:
                print(f"❌ Error sending ICE candidate: {e}")
        
    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        t = message.type
        if t == Gst.MessageType.EOS:
            print("End-of-stream")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"❌ ERROR: {err}")
            if debug:
                print(f"Debug: {debug}")
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"⚠ WARNING: {warn}")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipe:
                old, new, pending = message.parse_state_changed()
                print(f"🔧 Pipeline: {old.value_nick} → {new.value_nick}")
                
                # When pipeline goes to PLAYING, force offer creation
                if new == Gst.State.PLAYING and self.peer_id and not self.offer_created:
                    print("🎬 Pipeline is PLAYING - forcing offer creation in 1 second...")
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(self.delayed_offer_creation(), self.loop)
                    
        elif t == Gst.MessageType.STREAM_START:
            print("🎬 Stream started!")
        elif t == Gst.MessageType.QOS:
            # Handle quality of service messages
            pass
                
    async def delayed_offer_creation(self):
        """Create offer after a delay using asyncio"""
        await asyncio.sleep(2)
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
            import traceback
            traceback.print_exc()
        
    async def handle_ice_candidate(self, candidate_data):
        """Handle ICE candidate from operator"""
        try:
            if candidate_data and 'candidate' in candidate_data:
                candidate = candidate_data['candidate']
                mline_index = candidate_data.get('sdpMLineIndex', 0)
                
                print(f"🧊 Adding remote ICE candidate [{mline_index}]")
                self.webrtc.emit('add-ice-candidate', mline_index, candidate)
        except Exception as e:
            print(f"❌ Error handling ICE candidate: {e}")
            
    async def connect_signaling(self):
        """Connect to signaling server"""
        print(f"🔗 Connecting to {SIGNALING_SERVER}...")
        try:
            self.ws = await websockets.connect(SIGNALING_SERVER, ping_interval=20, ping_timeout=30)
            self.connected = True
            print("✅ Connected to signaling server")
            
            # Register as Pi
            await self.ws.send(json.dumps({
                'type': 'register',
                'role': 'pi'
            }))
            print("✅ Registered as Pi")
            
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
                    
                    if msg_type == 'call_request':
                        operator_id = data.get('operatorId')
                        print(f"\n📞 CALL REQUEST from operator: {operator_id}")
                        self.peer_id = operator_id
                        
                        # Start pipeline
                        print("🎬 Starting video stream...")
                        result = self.pipe.set_state(Gst.State.PLAYING)
                        if result == Gst.StateChangeReturn.FAILURE:
                            print("❌ ERROR: Failed to start pipeline!")
                        else:
                            print("✅ Pipeline started")
                            # Force offer creation after a short delay
                            print("⏰ Scheduling forced offer creation in 3 seconds...")
                            await asyncio.sleep(3)
                            await self.force_create_offer()
                            
                    elif msg_type == 'answer':
                        await self.handle_answer(data['sdp'])
                        
                    elif msg_type == 'candidate':
                        await self.handle_ice_candidate(data.get('candidate'))
                        
                    elif msg_type == 'peer_disconnected':
                        print("\n📴 Operator disconnected")
                        self.cleanup()
                        
                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error: {e}")
                except Exception as e:
                    print(f"❌ Error processing message: {e}")
                    
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

            # Create pipeline first
            if not self.create_pipeline():
                print("❌ ERROR: Failed to create pipeline")
                return
            
            # Connect to signaling server
            await self.connect_signaling()
            
            print("\n✅ READY! Waiting for operator to connect...")
            print("   (Open the web interface and click 'Connect')\n")
            
            # Handle signaling
            await self.handle_signaling()
            
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down...")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()
            
    def cleanup(self):
        """Clean up resources"""
        print("🧹 Cleaning up...")
        self.connected = False
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
        if self.ws:
            asyncio.create_task(self.ws.close())
        print("✅ Cleanup complete")


async def main():
    # Initialize GStreamer
    Gst.init(None)
    
    client = GStreamerWebRTCVideoOnly()
    await client.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Exiting...")
        sys.exit(0)