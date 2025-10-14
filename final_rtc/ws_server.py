#!/usr/bin/env python3
"""
WebSocket-only streaming server for Raspberry Pi
- Video: Pi Camera -> Web Client (server pushes JPEG frames over WebSocket)
- Audio: Client -> Pi speakers (client sends raw PCM chunks as binary messages)

Protocol (simple):
- Client opens WebSocket to /ws
- Client sends JSON control messages (text):
    {"type":"start","width":640,"height":480,"fps":15}  -> start sending video
    {"type":"stop"} -> stop sending video
    {"type":"bye"}  -> close connection
- Server sends binary video frames: b"V" + JPEG bytes
- Client sends binary audio frames: b"A" + raw PCM bytes (signed 16-bit little-endian, interleaved stereo, 48000 Hz)

Notes and tradeoffs:
- This is not WebRTC. No NAT traversal or media negotiation. Works within LAN or where WebSocket is reachable.
- JPEG over WebSocket is simple and wide-compatible. Latency depends on network and chosen FPS.
- Audio is raw PCM that the client must capture and send in the correct format (16-bit, stereo, 48kHz). The server plays it via PyAudio.

Requirements on Raspberry Pi: picamera2, aiortc removed, pillow optional, pyaudio optional
Install: pip3 install aiohttp picamera2 Pillow pyaudio

"""

import asyncio
import json
import logging
import ssl
import os
import traceback
import threading
import queue
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_pi_stream")

# Optional hardware libs
try:
    from picamera2 import Picamera2
    PICAMERA_AVAILABLE = True
except Exception:
    logger.warning("picamera2 not available")
    PICAMERA_AVAILABLE = False

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except Exception:
    logger.warning("pyaudio not available")
    PYAUDIO_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    logger.warning("Pillow not available; JPEG encoding may fail")
    PIL_AVAILABLE = False

# Audio settings expected from client (server-side fixed)
AUDIO_FORMAT = pyaudio.paInt16 if PYAUDIO_AVAILABLE else None
AUDIO_CHANNELS = 2
AUDIO_RATE = 48000


def find_audio_device():
    if not PYAUDIO_AVAILABLE:
        return None
    p = pyaudio.PyAudio()
    device = None
    try:
        default = p.get_default_output_device_info()
        device = default['index']
    except Exception:
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get('maxOutputChannels', 0) > 0:
                device = i
                break
    finally:
        p.terminate()
    return device


class AudioPlayer:
    def __init__(self, output_device=None):
        if not PYAUDIO_AVAILABLE:
            raise RuntimeError('pyaudio missing')
        self.queue = queue.Queue(maxsize=200)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.output_device = output_device
        self.stream = None
        self.pa = None
        self.thread.start()

    def _run(self):
        try:
            self.pa = pyaudio.PyAudio()
            kwargs = dict(format=AUDIO_FORMAT, channels=AUDIO_CHANNELS, rate=AUDIO_RATE, output=True, frames_per_buffer=1024)
            if self.output_device is not None:
                kwargs['output_device_index'] = self.output_device
            self.stream = self.pa.open(**kwargs)
            logger.info('AudioPlayer started')

            while self.running:
                try:
                    chunk = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    self.stream.write(chunk)
                except Exception:
                    logger.exception('Failed to write audio chunk')
                    break
        except Exception:
            logger.exception('AudioPlayer init failed')
        finally:
            try:
                if self.stream:
                    self.stream.stop_stream()
                    self.stream.close()
            except Exception:
                pass
            try:
                if self.pa:
                    self.pa.terminate()
            except Exception:
                pass
            logger.info('AudioPlayer stopped')

    def add(self, data: bytes):
        if not self.queue.full():
            self.queue.put(data)

    def stop(self):
        self.running = False
        self.thread.join(timeout=2)


class PiCameraJPEG:
    """Simple wrapper producing JPEG frames from picamera2"""
    def __init__(self, width=640, height=480, fps=15):
        if not PICAMERA_AVAILABLE:
            raise RuntimeError('picamera2 missing')
        self.width = width
        self.height = height
        self.fps = fps
        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(main={'size': (self.width, self.height), 'format': 'BGR888'}, controls={'FrameRate': self.fps})
        self.picam2.configure(config)
        self.picam2.start()
        logger.info(f'PiCamera started {self.width}x{self.height}@{self.fps}')

    def capture_jpeg(self) -> bytes:
        arr = self.picam2.capture_array()
        # arr is BGR; convert to RGB
        try:
            import numpy as np
            rgb = arr[:, :, ::-1]
        except Exception:
            rgb = arr
        if PIL_AVAILABLE:
            try:
                from io import BytesIO
                img = Image.fromarray(rgb)
                buf = BytesIO()
                img.save(buf, format='JPEG', quality=75)
                return buf.getvalue()
            except Exception:
                logger.exception('PIL encode failed')
        # Fallback: return raw bytes (not recommended)
        return rgb.tobytes()

    def stop(self):
        try:
            self.picam2.stop()
        except Exception:
            pass


async def websocket_handler(request):
    ws = web.WebSocketResponse(max_msg_size=10_000_000)
    await ws.prepare(request)

    video_task = None
    camera = None
    audio_player = None
    audio_device = find_audio_device()
    if PYAUDIO_AVAILABLE and audio_device is not None:
        try:
            audio_player = AudioPlayer(output_device=audio_device)
        except Exception:
            audio_player = None
            logger.exception('AudioPlayer creation failed')

    async def send_video_loop(width, height, fps):
        nonlocal camera
        try:
            camera = PiCameraJPEG(width=width, height=height, fps=fps)
        except Exception as e:
            await ws.send_json({'type': 'error', 'message': f'camera init failed: {e}'})
            return
        interval = 1.0 / max(1, fps)
        try:
            while True:
                jpeg = camera.capture_jpeg()
                if isinstance(jpeg, bytes):
                    # prefix with 'V' to mark video
                    try:
                        await ws.send_bytes(b'V' + jpeg)
                    except Exception:
                        break
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info('Video task cancelled')
        except Exception:
            logger.exception('Video loop error')
        finally:
            try:
                camera.stop()
            except Exception:
                pass

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    await ws.send_json({'type': 'error', 'message': 'invalid json'})
                    continue
                mtype = data.get('type')
                if mtype == 'start':
                    if video_task and not video_task.done():
                        await ws.send_json({'type': 'info', 'message': 'already streaming'})
                        continue
                    width = int(data.get('width', 640))
                    height = int(data.get('height', 480))
                    fps = int(data.get('fps', 10))
                    video_task = asyncio.create_task(send_video_loop(width, height, fps))
                    await ws.send_json({'type': 'started'})
                elif mtype == 'stop':
                    if video_task:
                        video_task.cancel()
                        video_task = None
                    await ws.send_json({'type': 'stopped'})
                elif mtype == 'bye':
                    break
                else:
                    await ws.send_json({'type': 'error', 'message': 'unsupported command'})

            elif msg.type == WSMsgType.BINARY:
                data = msg.data
                if not data:
                    continue
                tag = data[:1]
                payload = data[1:]
                if tag == b'A':
                    # audio chunk from client
                    if audio_player:
                        audio_player.add(payload)
                else:
                    # unknown binary payload; ignore or log
                    logger.debug('Unknown binary message received')

            elif msg.type == WSMsgType.ERROR:
                logger.error(f'ws connection closed with exception {ws.exception()}')
                break

    except Exception:
        logger.exception('WebSocket handler error')
    finally:
        if video_task:
            video_task.cancel()
            try:
                await video_task
            except Exception:
                pass
        if camera:
            try:
                camera.stop()
            except Exception:
                pass
        if audio_player:
            try:
                audio_player.stop()
            except Exception:
                pass
        await ws.close()
        logger.info('WebSocket closed')

    return ws


async def index(request):
    try:
        content = open('client_ws.html', 'r').read()
        return web.Response(content_type='text/html', text=content)
    except FileNotFoundError:
        return web.Response(status=404, text='client_ws.html not found')


def create_self_signed_cert():
    cert_file = 'cert.pem'
    key_file = 'key.pem'
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file
    try:
        from OpenSSL import crypto
    except Exception:
        logger.warning('pyOpenSSL missing; cannot create cert')
        return None, None
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)
    cert = crypto.X509()
    cert.get_subject().CN = 'localhost'
    cert.set_serial_number(1000)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(365*24*60*60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)
    cert.sign(k, 'sha256')
    with open(cert_file, 'wb') as f:
        f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    with open(key_file, 'wb') as f:
        f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))
    return cert_file, key_file


def main():
    import argparse
    parser = argparse.ArgumentParser(description='WebSocket-only Pi camera server')
    parser.add_argument('--http', action='store_true')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/ws', websocket_handler)

    if args.http:
        logger.info(f'Starting HTTP on {args.host}:{args.port}')
        web.run_app(app, host=args.host, port=args.port)
    else:
        cert, key = create_self_signed_cert()
        if cert and key:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(cert, key)
            logger.info(f'Starting HTTPS on {args.host}:{args.port}')
            web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)
        else:
            logger.warning('Failed to create cert. Falling back to HTTP')
            web.run_app(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
