"""
OCR client for the embedded pipeline.

Workflow
--------
1. Load/generate RSA keys from ./keys.
2. POST /get_challenge with the public key, sign the challenge, obtain JWT.
3. Watch the image queue (POSIX message queue if available, otherwise a simple
   directory) for filenames to process.
4. Upload images to /ocr and forward recognised text to the TTS pipeline.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover - runtime misconfiguration
    raise RuntimeError("The 'requests' package is required. Install it via `pip install requests`.") from exc

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError as exc:  # pragma: no cover - runtime misconfiguration
    raise RuntimeError("The 'cryptography' package is required. Install it via `pip install cryptography`.") from exc


# -----------------------------------------------------------------------------
# Logging & configuration helpers
# -----------------------------------------------------------------------------


def configure_logging(level: str) -> logging.Logger:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("ocr_client")
    logger.setLevel(numeric_level)
    return logger


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    server_base_url: str = field(default_factory=lambda: os.getenv("OCR_SERVER_URL", "http://127.0.0.1:8080"))
    keys_dir: Path = field(default_factory=lambda: Path(os.getenv("OCR_KEYS_DIR", Path(__file__).parent / "keys")))
    public_key_name: str = field(default_factory=lambda: os.getenv("OCR_PUBLIC_KEY_NAME", "device_public.pem"))
    private_key_name: str = field(default_factory=lambda: os.getenv("OCR_PRIVATE_KEY_NAME", "device_private.pem"))
    server_key_name: str = field(default_factory=lambda: os.getenv("OCR_SERVER_KEY_NAME", "server_public.pem"))
    queue_name: str = field(default_factory=lambda: os.getenv("OCR_IMAGE_QUEUE", "/ocr_image_queue"))
    queue_dir: Path = field(default_factory=lambda: Path(os.getenv("OCR_QUEUE_DIR", Path(__file__).parent / "queue")))
    image_base_dir: Path = field(default_factory=lambda: Path(os.getenv("OCR_IMAGE_BASE_DIR", Path.cwd())))
    request_timeout: int = field(default_factory=lambda: int(os.getenv("OCR_REQUEST_TIMEOUT", "30")))
    retry_delay: float = field(default_factory=lambda: float(os.getenv("OCR_RETRY_DELAY", "5")))
    tts_enabled: bool = field(default_factory=lambda: _env_bool("OCR_TTS_ENABLED", True))
    tts_script: Path = field(
        default_factory=lambda: Path(os.getenv("TTS_PIPELINE_SCRIPT", Path(__file__).parent / "tts_pipeline.py"))
    )
    log_level: str = field(default_factory=lambda: os.getenv("OCR_CLIENT_LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        self.server_base_url = self.server_base_url.rstrip("/")
        self.keys_dir = self.keys_dir.expanduser().resolve()
        self.queue_dir = self.queue_dir.expanduser().resolve()
        self.image_base_dir = self.image_base_dir.expanduser().resolve()
        self.tts_script = self.tts_script.expanduser().resolve()
        self.log_level = self.log_level.upper()

    @property
    def get_challenge_url(self) -> str:
        return f"{self.server_base_url}/get_challenge"

    @property
    def auth_url(self) -> str:
        return f"{self.server_base_url}/auth"

    @property
    def ocr_url(self) -> str:
        return f"{self.server_base_url}/ocr"

    @property
    def private_key_path(self) -> Path:
        return self.keys_dir / self.private_key_name

    @property
    def public_key_path(self) -> Path:
        return self.keys_dir / self.public_key_name

    @property
    def server_key_path(self) -> Path:
        return self.keys_dir / self.server_key_name


# -----------------------------------------------------------------------------
# Key management
# -----------------------------------------------------------------------------


class KeyManager:
    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.private_key: rsa.RSAPrivateKey
        self.public_key: rsa.RSAPublicKey
        self.server_public_key: Optional[str] = None
        self._ensure_keys()

    def _ensure_keys(self) -> None:
        self.config.keys_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.private_key_path.exists() or not self.config.public_key_path.exists():
            self.logger.info("Generating device RSA key pair in %s", self.config.keys_dir)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.config.private_key_path.write_bytes(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            self.config.public_key_path.write_bytes(
                key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            try:
                os.chmod(self.config.private_key_path, 0o600)
            except PermissionError:
                self.logger.debug("Skipping chmod on private key (insufficient permissions)")

        self.private_key = serialization.load_pem_private_key(self.config.private_key_path.read_bytes(), password=None)
        self.public_key = serialization.load_pem_public_key(self.config.public_key_path.read_bytes())

        if self.config.server_key_path.exists():
            try:
                self.server_public_key = self.config.server_key_path.read_text()
            except OSError:
                self.server_public_key = None

    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    def sign_challenge(self, challenge: str) -> str:
        signature = self.private_key.sign(
            challenge.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def cache_server_public_key(self, pem: str) -> None:
        self.server_public_key = pem
        try:
            self.config.server_key_path.write_text(pem)
        except OSError as exc:
            self.logger.warning("Unable to persist server public key: %s", exc)


# -----------------------------------------------------------------------------
# Queue backends
# -----------------------------------------------------------------------------


class ImageQueue:
    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._backend = self._choose_backend()

    def _choose_backend(self):
        try:
            import posix_ipc  # type: ignore

            self.logger.info("Using POSIX message queue backend (%s)", self.config.queue_name)
            return _PosixQueueBackend(self.config.queue_name)
        except (ImportError, OSError):
            self.logger.info("Using filesystem queue backend (%s)", self.config.queue_dir)
            return _DirectoryQueueBackend(self.config.queue_dir)

    def get(self) -> str:
        return self._backend.get()

    def put(self, value: str) -> None:
        self._backend.put(value)

    def close(self) -> None:
        self._backend.close()


class _PosixQueueBackend:
    def __init__(self, name: str) -> None:
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
        self._closed = False

    def get(self) -> str:
        while True:
            try:
                message, _priority = self.queue.receive(timeout=1)
                text = message.decode("utf-8").strip()
                if text:
                    return text
            except self._posix.BusyError:
                continue

    def put(self, value: str) -> None:
        self.queue.send(value.encode("utf-8"))

    def close(self) -> None:
        if not self._closed:
            try:
                self.queue.close()
            except Exception:
                pass
            self._closed = True


class _DirectoryQueueBackend:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)

    def get(self) -> str:
        while True:
            for candidate in sorted(self.path.glob("*.msg")):
                try:
                    text = candidate.read_text().strip()
                    candidate.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
                if text:
                    return text
            time.sleep(0.5)

    def put(self, value: str) -> None:
        filename = f"{time.time_ns()}_{uuid.uuid4().hex}.msg"
        (self.path / filename).write_text(value.strip())

    def close(self) -> None:
        return


# -----------------------------------------------------------------------------
# TTS integration
# -----------------------------------------------------------------------------


class TTSSink:
    def __init__(self, script_path: Path, logger: logging.Logger, enabled: bool) -> None:
        self.script_path = script_path
        self.logger = logger
        self.enabled = enabled
        self._process: Optional[subprocess.Popen[str]] = None

    def speak(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            return

        if not self.enabled:
            self.logger.info("TTS disabled. Text: %s", clean)
            return

        if not self.script_path.exists():
            self.logger.warning("TTS script %s not found. Text: %s", self.script_path, clean)
            return

        if self._process is None or self._process.poll() is not None:
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
                self._process = None
                self.logger.info("TTS fallback output: %s", clean)
                return

        assert self._process.stdin is not None
        try:
            self._process.stdin.write(clean + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self.logger.error("Writing to TTS pipeline failed: %s", exc)
            self._process = None
            self.logger.info("TTS fallback output: %s", clean)

    def close(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
            except Exception:
                pass
        self._process = None


# -----------------------------------------------------------------------------
# OCR client implementation
# -----------------------------------------------------------------------------


class OCRClient:
    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.keys = KeyManager(config, logger)
        self.queue = ImageQueue(config, logger)
        self.tts = TTSSink(config.tts_script, logger, config.tts_enabled)
        self.session = requests.Session()
        self.token: Optional[str] = None
        self.token_expiry: float = 0.0
        self.key_fingerprint: Optional[str] = None
        self._stopped = False

    # --------------------------- lifecycle ---------------------------------
    def install_signal_handlers(self) -> None:
        def _stop_handler(signum, _frame):
            self.logger.info("Received signal %s; shutting down.", signum)
            self.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop_handler)
            except ValueError:
                # Raised when running in an environment that forbids signal handlers.
                pass

    def request_stop(self) -> None:
        self._stopped = True

    def close(self) -> None:
        self.queue.close()
        self.tts.close()
        self.session.close()

    # --------------------------- authentication -----------------------------
    def ensure_authenticated(self) -> None:
        now = time.time()
        if self.token and now < (self.token_expiry - 10):
            return

        self.logger.info("Authenticating with OCR server at %s", self.config.server_base_url)
        response = self.session.post(
            self.config.get_challenge_url,
            json={"public_key": self.keys.public_key_pem()},
            timeout=self.config.request_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Challenge request failed: {response.status_code} {response.text}")
        challenge_payload = response.json()
        challenge = challenge_payload.get("challenge")
        challenge_token = challenge_payload.get("challenge_token")
        if not challenge or not challenge_token:
            raise RuntimeError("Invalid challenge payload from server")

        signature = self.keys.sign_challenge(challenge)
        auth_response = self.session.post(
            self.config.auth_url,
            json={"challenge_token": challenge_token, "signed_challenge": signature},
            timeout=self.config.request_timeout,
        )
        if auth_response.status_code != 200:
            raise RuntimeError(f"Authentication failed: {auth_response.status_code} {auth_response.text}")

        data = auth_response.json()
        self.token = data.get("token")
        expires_in = data.get("expires_in", self.config.request_timeout)
        if not self.token:
            raise RuntimeError("Authentication response missing token")

        self.token_expiry = time.time() + float(expires_in)
        self.key_fingerprint = data.get("key_fingerprint")
        server_public_key = data.get("server_public_key")
        if server_public_key:
            self.keys.cache_server_public_key(server_public_key)

        if self.key_fingerprint:
            self.logger.info(
                "Authentication successful as key %s. Token expires in %.0f seconds.",
                self.key_fingerprint[:12],
                float(expires_in),
            )
        else:
            self.logger.info("Authentication successful. Token expires in %.0f seconds.", float(expires_in))

    # --------------------------- OCR processing -----------------------------
    def run(self) -> None:
        self.logger.info("OCR client started. Waiting for images...")
        while not self._stopped:
            try:
                image_ref = self.queue.get()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                self.logger.error("Queue read failed: %s", exc)
                time.sleep(self.config.retry_delay)
                continue

            if not image_ref:
                continue

            try:
                self.ensure_authenticated()
            except Exception as exc:
                self.logger.error("Authentication error: %s", exc)
                time.sleep(self.config.retry_delay)
                continue

            self._process_image(image_ref)

        self.logger.info("OCR client stopped.")

    def _process_image(self, image_ref: str) -> None:
        image_path = Path(image_ref).expanduser()
        if not image_path.is_absolute():
            image_path = (self.config.image_base_dir / image_path).resolve()

        if not image_path.exists():
            self.logger.error("Image %s not found; skipping.", image_path)
            return

        self.logger.info("Submitting %s for OCR", image_path)
        try:
            with image_path.open("rb") as fh:
                files = {"file": (image_path.name, fh, "application/octet-stream")}
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                data = {"jwt_token": self.token or ""}
                response = self.session.post(
                    self.config.ocr_url,
                    data=data,
                    files=files,
                    timeout=self.config.request_timeout,
                )
        except Exception as exc:
            self.logger.error("Failed to upload %s: %s", image_path, exc)
            return

        if response.status_code == 401:
            self.logger.warning("Access token rejected. Re-authenticating...")
            self.token = None
            try:
                self.ensure_authenticated()
            except Exception as auth_exc:
                self.logger.error("Re-authentication failed: %s", auth_exc)
                return
            self._process_image(str(image_path))
            return

        if response.status_code != 200:
            self.logger.error("OCR request failed (%s): %s", response.status_code, response.text)
            return

        try:
            payload = response.json()
        except ValueError:
            self.logger.error("OCR server returned non-JSON response")
            return

        if payload.get("status") != "ok":
            self.logger.error("OCR server error: %s", payload)
            return

        text = payload.get("text", "").strip()
        if text:
            self.logger.info("OCR result (%s): %s", image_path.name, text)
            self.tts.speak(text)
        else:
            self.logger.warning("OCR result for %s contained no text.", image_path.name)


# -----------------------------------------------------------------------------
# CLI helpers
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embedded OCR client")
    parser.add_argument("--server-url", help="HTTP base URL of the OCR server (default env OCR_SERVER_URL)")
    parser.add_argument("--enqueue", metavar="IMAGE_PATH", help="Enqueue an image path and exit")
    parser.add_argument(
        "--process-image",
        metavar="IMAGE_PATH",
        help="Immediately process a single image (skips queue)",
    )
    parser.add_argument("--log-level", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--no-tts", action="store_true", help="Disable text-to-speech output")
    return parser.parse_args()


def enqueue_image(config: Config, logger: logging.Logger, image_path: str) -> None:
    queue = ImageQueue(config, logger)
    queue.put(image_path)
    queue.close()
    logger.info("Queued %s for OCR", image_path)


def main() -> None:
    args = parse_args()
    config = Config()
    if args.server_url:
        config.server_base_url = args.server_url
    if args.log_level:
        config.log_level = args.log_level.upper()
    if args.no_tts:
        config.tts_enabled = False

    logger = configure_logging(config.log_level)

    if args.enqueue:
        enqueue_image(config, logger, args.enqueue)
        if not args.process_image:
            return

    client = OCRClient(config, logger)
    client.install_signal_handlers()

    if args.process_image:
        try:
            client.ensure_authenticated()
            client._process_image(args.process_image)
        finally:
            client.close()
        return

    try:
        client.run()
    finally:
        client.close()


if __name__ == "__main__":
    main()
