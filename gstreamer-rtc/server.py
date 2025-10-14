#!/usr/bin/env python3
"""
Ultra-optimized WebRTC client using GStreamer with hardware acceleration
This completely avoids aiortc for better performance on Pi Zero 2
"""

import asyncio
import json
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp
import websockets
import sys

# Configuration
SIGNALING_SERVER = "ws://192.168.3.105:8081"
STUN_SERVER = "stun://stun.l.google.com:19302"

class GStreamerWebRTC:
    def __init__(self):
        Gst.init(None)
        self.pipe = None
        self.webrtc = None
        self.ws = None
        self.peer_id = None
        
    def create_pipeline(self):
        """
        Create GStreamer pipeline with hardware-accelerated H.264 encoding
        This uses the Pi's hardware encoder (v4l2h264enc) for minimal CPU usage
        """
        # Pipeline components:
        # 1. libcamerasrc: Capture from Pi camera
        # 2. v4l2h264enc: Hardware H.264 encoding
        # 3. rtph264pay: RTP packetization
        # 4. webrtcbin: WebRTC handling
        # 5. alsasrc/alsasink: Audio I/O
        
        pipeline_str = f"""
            webrtcbin name=webrtc stun-server={STUN_SERVER} bundle-policy=max-bundle
            
            libcamerasrc ! 
            video/x-raw,width=640,height=480,framerate=15/1 ! 
            v4l2h264enc extra-controls="controls,repeat_sequence_header=1" ! 
            video/x-h264,profile=baseline ! 
            rtph264pay config-interval=-1 name=payloader ! 
            application/x-rtp,media=video,encoding-name=H264,payload=96 ! 
            webrtc.sink_0
            
            alsasrc device=hw:1,0 ! 
            audioconvert ! 
            audioresample ! 
            audio/x-raw,rate=16000,channels=1 ! 
            opusenc bitrate=32000 ! 
            rtpopuspay ! 
            application/x-rtp,media=audio,encoding-name=OPUS,payload=97 ! 
            webrtc.sink_1
            
            webrtc. ! 
            rtpopusdepay ! 
            opusdec ! 
            audioconvert ! 
            audioresample ! 
            alsasink device=hw:1,0
        """
        
        print("Creating pipeline...")
        self.pipe = Gst.parse_launch(pipeline_str)
        self.webrtc = self.pipe.get_by_name('webrtc')
        
        # Connect signals
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.on_ice_candidate)
        self.webrtc.connect('pad-added', self.on_pad_added)
        
        # Bus messages
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self.on_bus_message)
        
        print("Pipeline created")
        
    def on_negotiation_needed(self, element):
        """Called when negotiation is needed"""
        print("Negotiation needed")
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)
        
    def on_offer_created(self, promise, element, _):
        """Handle created offer"""
        print("Offer created")
        promise.wait()
        reply = promise.get_reply()
        offer = reply['offer']
        
        promise = Gst.Promise.new()
        element.emit('set-local-description', offer, promise)
        promise.interrupt()
        
        # Send offer via WebSocket
        sdp_text = offer.sdp.as_text()
        asyncio.create_task(self.send_sdp_offer(sdp_text))
        
    async def send_sdp_offer(self, sdp):
        """Send SDP offer to signaling server"""
        if self.ws:
            await self.ws.send(json.dumps({
                'type': 'offer',
                'sdp': sdp
            }))
            print("Offer sent to signaling server")
            
    def on_ice_candidate(self, element, mline_index, candidate):
        """Handle ICE candidates"""
        print(f"ICE candidate: {candidate}")
        asyncio.create_task(self.send_ice_candidate(mline_index, candidate))
        
    async def send_ice_candidate(self, mline_index, candidate):
        """Send ICE candidate to signaling server"""
        if self.ws:
            await self.ws.send(json.dumps({
                'type': 'candidate',
                'candidate': {
                    'candidate': candidate,
                    'sdpMLineIndex': mline_index
                }
            }))
            
    def on_pad_added(self, element, pad):
        """Handle incoming audio pad"""
        if pad.direction != Gst.PadDirection.SRC:
            return
        print(f"Pad added: {pad.get_name()}")
        
    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        t = message.type
        if t == Gst.MessageType.EOS:
            print("End-of-stream")
            self.pipe.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            self.pipe.set_state(Gst.State.NULL)
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipe:
                old, new, pending = message.parse_state_changed()
                print(f"Pipeline state: {old.value_nick} -> {new.value_nick}")
                
    async def handle_answer(self, sdp_text):
        """Handle SDP answer from operator"""
        print("Received answer")
        
        ret, sdp_msg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(bytes(sdp_text.encode()), sdp_msg)
        
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER,
            sdp_msg
        )
        
        promise = Gst.Promise.new()
        self.webrtc.emit('set-remote-description', answer, promise)
        promise.interrupt()
        print("Remote description set")
        
    async def handle_ice_candidate(self, candidate_data):
        """Handle ICE candidate from operator"""
        if candidate_data and 'candidate' in candidate_data:
            candidate = candidate_data['candidate']
            mline_index = candidate_data.get('sdpMLineIndex', 0)
            
            print(f"Adding ICE candidate: {candidate}")
            self.webrtc.emit('add-ice-candidate', mline_index, candidate)
            
    async def connect_signaling(self):
        """Connect to signaling server"""
        print(f"Connecting to {SIGNALING_SERVER}...")
        self.ws = await websockets.connect(SIGNALING_SERVER)
        print("Connected to signaling server")
        
        # Register as Pi
        await self.ws.send(json.dumps({
            'type': 'register',
            'role': 'pi'
        }))
        print("Registered as Pi")
        
    async def handle_signaling(self):
        """Handle signaling messages"""
        try:
            async for message in self.ws:
                data = json.loads(message)
                msg_type = data.get('type')
                
                print(f"Received: {msg_type}")
                
                if msg_type == 'call_request':
                    print(f"Call request from operator: {data.get('operatorId')}")
                    self.peer_id = data.get('operatorId')
                    # Start pipeline which will trigger negotiation
                    self.pipe.set_state(Gst.State.PLAYING)
                    
                elif msg_type == 'answer':
                    await self.handle_answer(data['sdp'])
                    
                elif msg_type == 'candidate':
                    await self.handle_ice_candidate(data.get('candidate'))
                    
                elif msg_type == 'peer_disconnected':
                    print("Peer disconnected")
                    self.pipe.set_state(Gst.State.NULL)
                    
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed")
        except Exception as e:
            print(f"Signaling error: {e}")
            
    async def run(self):
        """Main run loop"""
        try:
            # Create pipeline
            self.create_pipeline()
            
            # Connect to signaling server
            await self.connect_signaling()
            
            # Handle signaling
            await self.handle_signaling()
            
        except KeyboardInterrupt:
            print("\nShutting down...")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()
            
    def cleanup(self):
        """Clean up resources"""
        print("Cleaning up...")
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
        if self.ws:
            asyncio.create_task(self.ws.close())
        print("Cleanup complete")


async def main():
    client = GStreamerWebRTC()
    await client.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)