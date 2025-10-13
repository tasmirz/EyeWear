import asyncio
import json
from aiohttp import web

VIEWERS = set()
PRODUCERS = set()


async def index(request):
	return web.FileResponse('templates/index.html')


async def ws_handler(request):
	ws = web.WebSocketResponse(max_msg_size=2 ** 25)
	await ws.prepare(request)

	# simple registration by query param or first message
	role = request.query.get('role')
	if role == 'producer':
		PRODUCERS.add(ws)
	else:
		VIEWERS.add(ws)

	try:
		async for msg in ws:
			if msg.type == web.WSMsgType.TEXT:
				text = msg.data
				# if a register control message arrives, update role
				try:
					parsed = json.loads(text)
				except Exception:
					parsed = None

				if parsed and parsed.get('op') == 'register':
					new_role = parsed.get('role')
					# move socket between sets if needed
					if new_role == 'producer':
						VIEWERS.discard(ws)
						PRODUCERS.add(ws)
					else:
						PRODUCERS.discard(ws)
						VIEWERS.add(ws)
					continue

				# forward everything from producers to viewers
				if ws in PRODUCERS:
					dead = []
					for v in VIEWERS:
						try:
							await v.send_str(text)
						except Exception:
							dead.append(v)
					for d in dead:
						VIEWERS.discard(d)

			elif msg.type == web.WSMsgType.BINARY:
				# forward binary to viewers unchanged
				if ws in PRODUCERS:
					dead = []
					for v in VIEWERS:
						try:
							await v.send_bytes(msg.data)
						except Exception:
							dead.append(v)
					for d in dead:
						VIEWERS.discard(d)

			elif msg.type == web.WSMsgType.ERROR:
				print('ws connection closed with exception', ws.exception())

	finally:
		VIEWERS.discard(ws)
		PRODUCERS.discard(ws)

	return ws


def create_app():
	app = web.Application()
	app.router.add_get('/', index)
	app.router.add_get('/ws', ws_handler)
	# also serve the template folder statically so assets (if any) work
	app.router.add_static('/templates/', path='templates', show_index=True)
	return app


if __name__ == '__main__':
	app = create_app()
	web.run_app(app, host='0.0.0.0', port=8085)

