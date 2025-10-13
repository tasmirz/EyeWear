import asyncio

if __name__ == '__main__':
	print('empty')
import base64
import json
import cv2
import time
import argparse
import sounddevice as sd
import numpy as np
import websockets

async def send_video(ws, device=0, fps=1):
	cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
	cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
	cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
	if not cap.isOpened():
		raise RuntimeError('Could not open video device')

	interval = 1.0 / fps
	try:
		while True:
			t0 = time.time()
			ret, frame = cap.read()
			if not ret:
				await asyncio.sleep(interval)
				continue

			# encode as JPEG to keep size down
			ret2, jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
			if not ret2:
				await asyncio.sleep(interval)
				continue

			b64 = base64.b64encode(jpg.tobytes()).decode('ascii')
			msg = json.dumps({'type': 'video', 'ts': time.time(), 'jpeg_b64': b64})
			await ws.send(msg)

			delta = time.time() - t0
			await asyncio.sleep(max(0, interval - delta))
	finally:
		cap.release()


class AudioSender:
	def __init__(self, ws, samplerate=16000, channels=1, blocksize=1024):
		self.ws = ws
		self.samplerate = samplerate
		self.channels = channels
		self.blocksize = blocksize

	def callback(self, indata, frames, time_info, status):
		# indata is float32 in range [-1,1]; convert to int16
		pcm16 = (indata * 32767).astype(np.int16)
		b = pcm16.tobytes()
		b64 = base64.b64encode(b).decode('ascii')
		payload = json.dumps({'type': 'audio', 'sr': self.samplerate, 'ch': self.channels, 'pcm_b64': b64})
		# schedule send on event loop
		asyncio.run_coroutine_threadsafe(self.ws.send(payload), asyncio.get_event_loop())


async def send_audio(ws, samplerate=16000, channels=1, blocksize=1024):
	sender = AudioSender(ws, samplerate, channels, blocksize)
	stream = sd.InputStream(samplerate=samplerate, channels=channels, blocksize=blocksize, callback=sender.callback)
	with stream:
		# keep running until cancelled
		while True:
			await asyncio.sleep(1)


async def main(uri, args):
	async with websockets.connect(uri, max_size=2 ** 25) as ws:
		# register as producer
		await ws.send(json.dumps({'op': 'register', 'role': 'producer'}))

		tasks = []
		tasks.append(asyncio.create_task(send_video(ws, device=args.device, fps=args.fps)))
		if args.audio:
			tasks.append(asyncio.create_task(send_audio(ws, samplerate=args.sr, channels=args.channels, blocksize=args.block)))

		await asyncio.gather(*tasks)


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--uri', default='ws://192.168.3.105:8085/ws', help='WebSocket server URI')
	parser.add_argument('--device', type=int, default=0, help='Video device index')
	parser.add_argument('--fps', type=int, default=10, help='Frames per second')
	parser.add_argument('--audio', action='store_true', help='Enable microphone streaming')
	parser.add_argument('--sr', type=int, default=16000, help='Audio sample rate')
	parser.add_argument('--channels', type=int, default=1, help='Audio channels')
	parser.add_argument('--block', type=int, default=1024, help='Audio block size')
	args = parser.parse_args()

	asyncio.run(main(args.uri, args))

