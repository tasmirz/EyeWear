# Original Tasks:
# Connection:
# 1. There is priv, publ keys in ./keys
# 2. request challenge from server, sign with private key, send back
# 2. checks if the public key in mongodb
# 3. server verifies with publ key
# 4. Receive JWT token, server public key
# ===
# 1. Receive image filename (message queue)
# 2. Send image to OCR server, encrypt with server publ key
# 3. Receive playable text (pipe to tts_pipeline.py)
# 4. Ensure piped sequentially

"""
Secure OCR client pipeline.

This client is responsible for:
1. Establishing a WebSocket connection with the OCR server.
2. Performing an RSA challenge–response handshake using keys located in ./keys.
3. Receiving the server issued JWT and server public key.
4. Reading image capture notifications from an inter-process queue.
5. Encrypting outgoing image payloads with a server-provided public key.
6. Forwarding the recognised text to the text-to-speech pipeline in order.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import websockets
from websockets import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - fail fast with a helpful message
    raise RuntimeError(
        "The 'cryptography' package is required to run embedded_base/ocr_client.py"
    ) from exc


def configure_logging(log_level: str) -> logging.Logger:
    """Configure root logging once and return the module logger."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("ocr_client")
    logger.setLevel(level)
    return logger


@dataclass
class Config:
    server_url: str = field(
        default_factory=lambda: os.getenv("OCR_SERVER_URL", "ws://localhost:8765")
    )
    device_id: str = field(
        default_factory=lambda: os.getenv("OCR_DEVICE_ID", "pi-ocr-device")
    )
    keys_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "OCR_KEYS_DIR",
                Path(__file__).resolve().parent / "keys",
            )
        )
    )
    public_key_name: str = field(
        default_factory=lambda: os.getenv("OCR_PUBLIC_KEY_NAME", "device_public.pem")
    )
    private_key_name: str = field(
        default_factory=lambda: os.getenv("OCR_PRIVATE_KEY_NAME", "device_private.pem")
    )
    server_key_name: str = field(
        default_factory=lambda: os.getenv("OCR_SERVER_KEY_NAME", "server_public.pem")
    )
    queue_name: str = field(
        default_factory=lambda: os.getenv("OCR_IMAGE_QUEUE", "/ocr_image_queue")
    )
    queue_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "OCR_QUEUE_DIR",
                Path(__file__).resolve().parent / "queue",
            )
        )
    )
    image_base_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("OCR_IMAGE_BASE_DIR", Path.cwd())
        )
    )
    request_timeout: int = field(
        default_factory=lambda: int(os.getenv("OCR_REQUEST_TIMEOUT", "30"))
    )
    reconnect_delay: int = field(
        default_factory=lambda: int(os.getenv("OCR_RECONNECT_DELAY", "5"))
    )
    tts_script: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "TTS_PIPELINE_SCRIPT",
                Path(__file__).resolve().parent / "tts_pipeline.py",
            )
        )
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("OCR_CLIENT_LOG_LEVEL", "INFO")
    )

    def __post_init__(self) -> None:
        self.keys_dir = self.keys_dir.expanduser().resolve()
        self.queue_dir = self.queue_dir.expanduser().resolve()
        self.image_base_dir = self.image_base_dir.expanduser().resolve()
        self.tts_script = self.tts_script.expanduser().resolve()
        self.log_level = self.log_level.upper()

    @property
    def private_key_path(self) -> Path:
        return self.keys_dir / self.private_key_name

    @property
    def public_key_path(self) -> Path:
        return self.keys_dir / self.public_key_name

    @property
    def server_key_path(self) -> Path:
        return self.keys_dir / self.server_key_name


class KeyManager:
    """Manage the device RSA key pair and remote server public key."""

    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.private_key: rsa.RSAPrivateKey = None  # type: ignore[assignment]
        self.public_key: rsa.RSAPublicKey = None  # type: ignore[assignment]
        self.server_public_key: Optional[rsa.RSAPublicKey] = None
        self._load_or_generate_keys()

    def _load_or_generate_keys(self) -> None:
        self.config.keys_dir.mkdir(parents=True, exist_ok=True)
        if (
            not self.config.private_key_path.exists()
            or not self.config.public_key_path.exists()
        ):
            self.logger.info(
                "Generating RSA key pair for OCR client in %s", self.config.keys_dir
            )
            self._generate_keys()
        self._load_device_keys()
        self._load_server_key()

    def _generate_keys(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.config.private_key_path.write_bytes(private_bytes)
        self.config.public_key_path.write_bytes(public_bytes)
        try:
            os.chmod(self.config.private_key_path, 0o600)
        except PermissionError:
            self.logger.debug(
                "Skipping private key chmod; insufficient permissions on this platform"
            )

    def _load_device_keys(self) -> None:
        self.private_key = serialization.load_pem_private_key(
            self.config.private_key_path.read_bytes(),
            password=None,
        )
        self.public_key = serialization.load_pem_public_key(
            self.config.public_key_path.read_bytes()
        )

    def _load_server_key(self) -> None:
        if self.config.server_key_path.exists():
            try:
                self.server_public_key = serialization.load_pem_public_key(
                    self.config.server_key_path.read_bytes()
                )
                self.logger.debug(
                    "Loaded cached server public key from %s",
                    self.config.server_key_path,
                )
            except ValueError:
                self.logger.warning(
                    "Existing server public key at %s is invalid; ignoring",
                    self.config.server_key_path,
                )

    def set_server_public_key(self, pem: str, persist: bool = True) -> None:
        """Accept a PEM encoded server key and optionally persist it."""
        self.server_public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        if persist:
            self.config.server_key_path.write_text(pem)
            self.logger.debug(
                "Persisted server public key to %s", self.config.server_key_path
            )

    def get_public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    def sign_challenge(self, challenge: str) -> str:
        digest = hashes.Hash(hashes.SHA256())
        digest.update(challenge.encode("utf-8"))
        challenge_hash = digest.finalize()
        signature = self.private_key.sign(
            challenge_hash,
            padding.PKCS1v15(),
            utils.Prehashed(hashes.SHA256()),
        )
        return base64.b64encode(signature).decode("ascii")

    def encrypt_for_server(self, data: bytes) -> Dict[str, str]:
        if not self.server_public_key:
            raise RuntimeError("Server public key unavailable; cannot encrypt payload")
        aes_key = AESGCM.generate_key(bit_length=256)
        aesgcm = AESGCM(aes_key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, data, None)
        encrypted_key = self.server_public_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "encrypted_key": base64.b64encode(encrypted_key).decode("ascii"),
            "enc_alg": "AES-256-GCM",
            "key_alg": "RSA-OAEP",
        }


class ImageQueue:
    """Abstraction over the image filename queue."""

    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._backend = self._select_backend()

    def _select_backend(self):
        try:
            import posix_ipc  # type: ignore

            self.logger.info(
                "Using POSIX message queue backend at %s", self.config.queue_name
            )
            return _PosixQueueBackend(self.config.queue_name, self.logger)
        except (ImportError, OSError):
            self.logger.info(
                "Using filesystem queue backend at %s", self.config.queue_dir
            )
            return _DirectoryQueueBackend(self.config.queue_dir, self.logger)

    async def get(self) -> str:
        return await self._backend.get()

    async def put(self, item: str) -> None:
        await self._backend.put(item)

    def close(self) -> None:
        self._backend.close()


class _PosixQueueBackend:
    """POSIX message queue backend for image filenames."""

    def __init__(self, name: str, logger: logging.Logger) -> None:
        import posix_ipc  # type: ignore

        self._posix = posix_ipc
        try:
            self.queue = posix_ipc.MessageQueue(name)
        except posix_ipc.ExistentialError:
            self.queue = posix_ipc.MessageQueue(
                name,
                flags=posix_ipc.O_CREAT,
                max_messages=64,
                max_message_size=4096,
            )
        self.logger = logger
        self._closed = False

    async def get(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._receive_blocking)

    def _receive_blocking(self) -> str:
        while True:
            try:
                message, _ = self.queue.receive(timeout=1)
                return message.decode("utf-8").strip()
            except self._posix.BusyError:
                continue

    async def put(self, item: str) -> None:
        payload = item.encode("utf-8")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.queue.send(payload))

    def close(self) -> None:
        if not self._closed:
            try:
                self.queue.close()
            except Exception:
                pass
            self._closed = True


class _DirectoryQueueBackend:
    """Filesystem-backed queue for environments without POSIX IPC."""

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        self.path.mkdir(parents=True, exist_ok=True)
        self.poll_interval = 0.5

    async def get(self) -> str:
        while True:
            for candidate in sorted(self.path.glob("*.msg")):
                try:
                    content = candidate.read_text().strip()
                    candidate.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
                if content:
                    return content
            await asyncio.sleep(self.poll_interval)

    async def put(self, item: str) -> None:
        filename = f"{time.time_ns()}_{uuid.uuid4().hex}.msg"
        (self.path / filename).write_text(item)

    def close(self) -> None:
        # No resources to release for the filesystem queue.
        return


class TTSPipeline:
    """Thin wrapper for piping recognised text into tts_pipeline.py."""

    def __init__(self, script_path: Path, logger: logging.Logger) -> None:
        self.script_path = script_path
        self.logger = logger
        self._process: Optional[subprocess.Popen[str]] = None
        self._lock = asyncio.Lock()

    async def speak(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_text, text)

    def _write_text(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return

        if not self.script_path.exists():
            self.logger.warning(
                "TTS pipeline script %s not found; falling back to console output",
                self.script_path,
            )
            self.logger.info("TTS output: %s", cleaned)
            return

        if not self._process or self._process.poll() is not None:
            try:
                self._process = subprocess.Popen(
                    [sys.executable, str(self.script_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except Exception as exc:
                self.logger.error("Failed to launch TTS pipeline: %s", exc)
                self.logger.info("TTS output: %s", cleaned)
                self._process = None
                return

        assert self._process.stdin is not None
        try:
            self._process.stdin.write(cleaned + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self.logger.error("Failed to write to TTS pipeline: %s", exc)
            self.logger.info("TTS output: %s", cleaned)
            self._process = None

    def close(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
            except Exception:
                pass
        self._process = None


class OCRClient:
    """Main OCR client orchestrating authentication, queue handling, and TTS piping."""

    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.key_manager = KeyManager(config, logger)
        self.image_queue = ImageQueue(config, logger)
        self.tts_pipeline = TTSPipeline(config.tts_script, logger)
        self.websocket: Optional[WebSocketClientProtocol] = None
        self.token: Optional[str] = None
        self.recv_task: Optional[asyncio.Task[None]] = None
        self._pending_queue_task: Optional[asyncio.Task[str]] = None
        self._stop_event = asyncio.Event()
        self._connection_active = False
        self.pending_requests: Dict[str, asyncio.Future] = {}

    async def run(self) -> None:
        """Entry point for the client run loop with automatic reconnection."""
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_and_authenticate()
                    await self._consume_queue()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    self.logger.error("OCR client error: %s", exc)
                finally:
                    await self._cleanup_connection()
                    if not self._stop_event.is_set():
                        await asyncio.sleep(self.config.reconnect_delay)
        finally:
            self.image_queue.close()
            self.tts_pipeline.close()

    def stop(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
        if self._pending_queue_task:
            self._pending_queue_task.cancel()

    async def _connect_and_authenticate(self) -> None:
        self.logger.info("Connecting to OCR server at %s", self.config.server_url)
        self.websocket = await websockets.connect(self.config.server_url, max_size=None)
        await self._perform_handshake()
        self._connection_active = True
        self.recv_task = asyncio.create_task(self._recv_loop())

    async def _perform_handshake(self) -> None:
        assert self.websocket is not None
        init_message = {
            "type": "auth_init",
            "device_id": self.config.device_id,
            "public_key": self.key_manager.get_public_key_pem(),
        }
        await self.websocket.send(json.dumps(init_message))
        self.logger.debug("Sent auth_init message")

        challenge_raw = await self.websocket.recv()
        challenge_data = json.loads(challenge_raw)
        if challenge_data.get("type") != "auth_challenge":
            raise RuntimeError(f"Unexpected authentication response: {challenge_data}")

        challenge = challenge_data.get("challenge")
        if not isinstance(challenge, str):
            raise RuntimeError("Authentication challenge missing 'challenge' string")

        signature = self.key_manager.sign_challenge(challenge)
        response = {
            "type": "auth_response",
            "device_id": self.config.device_id,
            "signature": signature,
        }
        await self.websocket.send(json.dumps(response))
        self.logger.debug("Sent auth_response message")

        result_raw = await self.websocket.recv()
        result = json.loads(result_raw)

        if result.get("type") != "auth_success":
            raise RuntimeError(f"Authentication failed: {result}")

        token = result.get("token")
        if not token:
            raise RuntimeError("Authentication succeeded but no JWT token received")
        self.token = token

        server_public_key = result.get("server_public_key")
        if server_public_key:
            self.key_manager.set_server_public_key(server_public_key, persist=True)
            self.logger.info("Received server public key from authentication")
        else:
            self.logger.warning("Authentication success without server public key")
        self.logger.info("Authentication successful for device %s", self.config.device_id)

    async def _consume_queue(self) -> None:
        while self._connection_active and not self._stop_event.is_set():
            self._pending_queue_task = asyncio.create_task(self.image_queue.get())
            try:
                message = await self._pending_queue_task
            except asyncio.CancelledError:
                break
            finally:
                self._pending_queue_task = None

            if not message:
                continue

            await self._handle_image_message(message)

    async def _handle_image_message(self, message: str) -> None:
        path = Path(message).expanduser()
        if not path.is_absolute():
            path = (self.config.image_base_dir / path).resolve()
        else:
            path = path.resolve()

        if not path.exists():
            self.logger.error("Image %s not found; skipping", path)
            return

        try:
            data = path.read_bytes()
        except OSError as exc:
            self.logger.error("Failed to read %s: %s", path, exc)
            return

        if not self.token:
            self.logger.error("Authentication token unavailable; cannot send image")
            return

        try:
            payload = self.key_manager.encrypt_for_server(data)
        except RuntimeError as exc:
            self.logger.error("Cannot encrypt payload: %s", exc)
            return

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_requests[request_id] = future

        message_payload = {
            "type": "ocr_request",
            "device_id": self.config.device_id,
            "token": self.token,
            "request_id": request_id,
            "filename": path.name,
            "payload": payload,
        }

        await self._send_json(message_payload)

        try:
            response = await asyncio.wait_for(
                future, timeout=self.config.request_timeout
            )
        except asyncio.TimeoutError:
            self.logger.error("OCR request %s timed out", request_id)
            future.cancel()
            return
        finally:
            self.pending_requests.pop(request_id, None)

        if response.get("status", "ok") != "ok":
            self.logger.error(
                "OCR server reported error for %s: %s",
                path,
                response.get("error", response),
            )
            return

        recognised_text = response.get("text", "")
        if recognised_text:
            self.logger.info("Received OCR response for %s", path.name)
        else:
            self.logger.warning("OCR response for %s contained no text", path.name)
        await self.tts_pipeline.speak(recognised_text)

    async def _send_json(self, message: Dict[str, object]) -> None:
        if not self.websocket:
            raise RuntimeError("WebSocket connection is not established")
        await self.websocket.send(json.dumps(message))

    async def _recv_loop(self) -> None:
        assert self.websocket is not None
        try:
            async for raw in self.websocket:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    self.logger.error("Received malformed JSON: %s", raw)
                    continue

                msg_type = payload.get("type")
                if msg_type == "ocr_result":
                    request_id = payload.get("request_id")
                    future = self.pending_requests.get(request_id or "")
                    if future and not future.done():
                        future.set_result(payload)
                    else:
                        self.logger.warning(
                            "Received OCR result for unknown request id %s", request_id
                        )
                elif msg_type == "token_refresh":
                    token = payload.get("token")
                    if token:
                        self.token = token
                        self.logger.info("JWT token refreshed")
                elif msg_type == "server_public_key":
                    key_pem = payload.get("value")
                    if key_pem:
                        self.key_manager.set_server_public_key(key_pem, persist=True)
                        self.logger.info("Updated server public key from push message")
                elif msg_type == "error":
                    request_id = payload.get("request_id")
                    future = self.pending_requests.get(request_id or "")
                    if future and not future.done():
                        future.set_result(payload)
                    else:
                        self.logger.error("Server error: %s", payload)
                elif msg_type == "ping":
                    await self._send_json({"type": "pong"})
                else:
                    self.logger.debug("Unhandled server message: %s", payload)
        except ConnectionClosed as exc:
            self.logger.warning("Connection closed: %s", exc)
        except Exception as exc:
            self.logger.error("Receiver loop error: %s", exc)
        finally:
            self._connection_active = False
            if self._pending_queue_task:
                self._pending_queue_task.cancel()
            for future in self.pending_requests.values():
                if not future.done():
                    future.cancel()
            self.pending_requests.clear()

    async def _cleanup_connection(self) -> None:
        if self.recv_task:
            self.recv_task.cancel()
            try:
                await self.recv_task
            except asyncio.CancelledError:
                pass
            self.recv_task = None

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None
        self._connection_active = False


async def _enqueue_image(config: Config, logger: logging.Logger, image: str) -> None:
    queue = ImageQueue(config, logger)
    await queue.put(image)
    queue.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embedded OCR client")
    parser.add_argument(
        "--server-url",
        help="Override the OCR server URL (ws://host:port)",
    )
    parser.add_argument(
        "--device-id",
        help="Override the device identifier used during authentication",
    )
    parser.add_argument(
        "--enqueue",
        metavar="IMAGE_PATH",
        help="Enqueue an image for processing and exit",
    )
    parser.add_argument(
        "--log-level",
        help="Set log level (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    config = Config()
    if args.server_url:
        config.server_url = args.server_url
    if args.device_id:
        config.device_id = args.device_id
    if args.log_level:
        config.log_level = args.log_level.upper()

    logger = configure_logging(config.log_level)

    if args.enqueue:
        await _enqueue_image(config, logger, args.enqueue)
        logger.info("Enqueued %s for OCR processing", args.enqueue)
        return

    client = OCRClient(config, logger)
    loop = asyncio.get_running_loop()
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, client.stop)
            except NotImplementedError:
                # Windows does not allow custom signal handlers in asyncio.
                break
        await client.run()
    finally:
        client.stop()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
