"""
OCR client for the embedded pipeline.

Workflow
--------
1. Load/generate RSA keys from ./keys.
2. POST /get_challenge with the public key, sign the challenge, obtain JWT.
3. Watch the image queue (POSIX message queue if available, otherwise a simple
   directory) for filenames to process.
4. Upload images to /ocr, receive Gemini refinements, and forward the refined text to the TTS pipeline.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import time
from unittest import case
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from multiprocessing.managers import BaseManager

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


def _html_to_text(html_content: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        import re

        text = re.sub(r"<[^>]+>", " ", html_content)
        return " ".join(text.split())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class PipelineRequestManager(BaseManager):
    pass


class PipelineResultManager(BaseManager):
    pass


PipelineRequestManager.register("get_queue")
PipelineResultManager.register("out_queue")


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
    pipeline_host: str = field(default_factory=lambda: os.getenv("PIPELINE_HOST", "127.0.0.1"))
    pipeline_port: str = field(default_factory=lambda: os.getenv("PIPELINE_PORT", "50000"))
    pipeline_authkey: str = field(default_factory=lambda: os.getenv("PIPELINE_AUTHKEY", "abcf"))
    pipeline_out_host: str = field(default_factory=lambda: os.getenv("PIPELINE_OUT_HOST", "127.0.0.1"))
    pipeline_out_port: str = field(default_factory=lambda: os.getenv("PIPELINE_OUT_PORT", "50001"))
    pipeline_out_authkey: str = field(default_factory=lambda: os.getenv("PIPELINE_OUT_AUTHKEY", "abcfe"))
    pipeline_base_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "PIPELINE_WORKDIR",
                Path(__file__).resolve().parents[1] / "bbocr_server",
            )
        )
    )
    pipeline_drop_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "PIPELINE_DROP_DIR",
                Path(__file__).resolve().parents[1] / "bbocr_server" / "tmp" / "client_drop",
            )
        )
    )
    pipeline_timeout: float = field(default_factory=lambda: float(os.getenv("PIPELINE_TIMEOUT", "120")))

    def __post_init__(self) -> None:
        self.server_base_url = self.server_base_url.rstrip("/")
        self.keys_dir = self.keys_dir.expanduser().resolve()
        self.queue_dir = self.queue_dir.expanduser().resolve()
        self.image_base_dir = self.image_base_dir.expanduser().resolve()
        self.tts_script = self.tts_script.expanduser().resolve()
        self.log_level = self.log_level.upper()
        self.pipeline_base_dir = self.pipeline_base_dir.expanduser().resolve()
        self.pipeline_drop_dir = self.pipeline_drop_dir.expanduser().resolve()
        try:
            self.pipeline_port = int(self.pipeline_port)
        except ValueError:
            self.pipeline_port = 50000
        try:
            self.pipeline_out_port = int(self.pipeline_out_port)
        except ValueError:
            self.pipeline_out_port = 50001

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
        self.output_dir = Path(os.getenv("TTS_OUTPUT_DIR") or Path.cwd()).expanduser().resolve()
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("Unable to create TTS output directory %s: %s", self.output_dir, exc)

    def speak(self, text: str, output_name: Optional[str] = None) -> None:
        clean = text.strip()
        if not clean:
            return

        if not self.enabled:
            self.logger.info("TTS disabled. Text: %s", clean)
            return

        if not self.script_path.exists():
            self.logger.warning("TTS script %s not found. Text: %s", self.script_path, clean)
            return

        mp3_path: Optional[Path] = None
        wait_dir = self.output_dir
        env = None
        if output_name:
            mp3_path = wait_dir / f"{output_name}.mp3"
            try:
                mp3_path.unlink(missing_ok=True)
            except OSError:
                pass

        if self._process is None or self._process.poll() is not None:
            try:
                env = os.environ.copy()
                if "TTS_OUTPUT_DIR" in env:
                    try:
                        wait_dir = Path(env["TTS_OUTPUT_DIR"]).expanduser().resolve()
                    except Exception:
                        wait_dir = self.output_dir
                else:
                    env["TTS_OUTPUT_DIR"] = str(wait_dir)
                try:
                    wait_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    self.logger.warning("Unable to ensure TTS output directory %s: %s", wait_dir, exc)
                if mp3_path is not None:
                    mp3_path = wait_dir / mp3_path.name
                self._process = subprocess.Popen(
                    [sys.executable, str(self.script_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=env,
                )
                self.output_dir = wait_dir
            except Exception as exc:
                self.logger.error("Failed to launch TTS pipeline: %s", exc)
                self._process = None
                self.logger.info("TTS fallback output: %s", clean)
                return

        assert self._process.stdin is not None
        try:
            line = clean if output_name is None else f"{output_name}|{clean}"
            self._process.stdin.write(line + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self.logger.error("Writing to TTS pipeline failed: %s", exc)
            self._process = None
            self.logger.info("TTS fallback output: %s", clean)
            return

        if mp3_path:
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if mp3_path.exists():
                    self.logger.info("TTS MP3 generated: %s", mp3_path)
                    break
                time.sleep(0.1)
            else:
                self.logger.warning("Expected TTS MP3 %s but it was not created.", mp3_path)

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
        if self._pipeline_request_manager is not None:
            try:
                self._pipeline_request_manager.shutdown()
            except Exception:
                pass
        if self._pipeline_result_manager is not None:
            try:
                self._pipeline_result_manager.shutdown()
            except Exception:
                pass
        self._pipeline_request_manager = None
        self._pipeline_result_manager = None
        self._pipeline_request_queue = None
        self._pipeline_result_queue = None

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

        pipeline_payload = self._process_via_pipeline(image_path)
        if pipeline_payload is not None:
            self._handle_ocr_payload(image_path, pipeline_payload)
            return

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

        self._handle_ocr_payload(image_path, payload)

    def _handle_ocr_payload(self, image_path: Path, payload: Dict[str, Any]) -> None:
        if payload.get("status") != "ok":
            self.logger.error("OCR server error: %s", payload)
            return

        text = (payload.get("text") or "").strip()
        refined_text = (payload.get("refined_text") or "").strip()
        markdown = (payload.get("markdown") or "").strip()

        if text:
            self.logger.info("OCR result (%s): %s", image_path.name, text)
        else:
            self.logger.warning("OCR result for %s contained no text.", image_path.name)

        if refined_text:
            self.logger.info("Gemini refined text (%s): %s", image_path.name, refined_text)
            self.tts.speak(refined_text, output_name=image_path.stem)
        else:
            if markdown:
                self.logger.info("Gemini markdown (%s): %s", image_path.name, markdown)
            self.logger.warning("Skipping TTS for %s because Gemini refined text is unavailable.", image_path.name)

    # --------------------------- pipeline queue helpers ---------------------

    def _ensure_pipeline_connections(self) -> None:
        if self._pipeline_request_queue and self._pipeline_result_queue:
            return

        class _RequestManager(BaseManager):
            pass

        class _ResultManager(BaseManager):
            pass

        _RequestManager.register("get_queue")
        _ResultManager.register("out_queue")

        req_manager = PipelineRequestManager(
            address=(self.config.pipeline_host, self.config.pipeline_port),
            authkey=self.config.pipeline_authkey.encode("utf-8"),
        )
        res_manager = PipelineResultManager(
            address=(self.config.pipeline_out_host, self.config.pipeline_out_port),
            authkey=self.config.pipeline_out_authkey.encode("utf-8"),
        )

        req_manager.connect()
        res_manager.connect()

        self._pipeline_request_manager = req_manager
        self._pipeline_result_manager = res_manager
        self._pipeline_request_queue = req_manager.get_queue()
        self._pipeline_result_queue = res_manager.out_queue()

    def _resolve_pipeline_path(self, path_str: str) -> Path:
        path = Path(path_str)
        if not path.is_absolute():
            path = (self.config.pipeline_base_dir / path).resolve()
        return path

    def _process_via_pipeline(self, image_path: Path) -> Optional[Dict[str, Any]]:
        try:
            self._ensure_pipeline_connections()
        except Exception as exc:
            self.logger.debug("Pipeline unavailable: %s", exc)
            return None

        drop_dir = self.config.pipeline_drop_dir
        drop_dir.mkdir(parents=True, exist_ok=True)

        job_key = uuid.uuid4().hex
        staged_path = drop_dir / f"{job_key}_{image_path.name}"
        try:
            shutil.copy2(image_path, staged_path)
        except OSError as exc:
            self.logger.error("Failed to stage %s for pipeline: %s", image_path, exc)
            return None

        try:
            self._pipeline_request_queue.put(str(staged_path))
        except Exception as exc:
            self.logger.error("Failed to enqueue %s: %s", staged_path, exc)
            staged_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            return None

        deadline = time.time() + float(self.config.pipeline_timeout)

        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("Pipeline timed out waiting for result")
                try:
                    result_path = self._pipeline_result_queue.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError("Pipeline timed out waiting for result")
                except Exception as exc:
                    self.logger.debug("Pipeline result queue error: %s", exc)
                    self._pipeline_result_queue = None
                    self._pipeline_result_manager = None
                    self._ensure_pipeline_connections()
                    continue

                if isinstance(result_path, bytes):
                    result_path = result_path.decode("utf-8", errors="ignore")

                html_path = self._resolve_pipeline_path(result_path)
                if html_path.stem == staged_path.stem:
                    if not html_path.exists():
                        raise RuntimeError(f"Pipeline output missing: {html_path}")
                    html_content = html_path.read_text(encoding="utf-8")
                    return {
                        "status": "ok",
                        "html": html_content,
                        "text": _html_to_text(html_content),
                        "markdown": None,
                        "refined_text": None,
                    }
                # Not our job, ignore and keep waiting
                continue
        except TimeoutError as exc:
            self.logger.error("Pipeline timeout for %s: %s", image_path, exc)
        except Exception as exc:
            self.logger.error("Pipeline processing failed for %s: %s", image_path, exc)
        finally:
            staged_path.unlink(missing_ok=True)  # type: ignore[arg-type]

        return None


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

from common import IPC
from multiprocessing.shared_memory import SharedMemory
import struct

shm = SharedMemory(name="ocr_signal", create=True, size=4)

def signal_handler(signum, frame):
    action_code = struct.unpack('i', shm.buf[:4])[0]
    print(f"Action code from shared memory: {action_code}")
    if action_code == 1:
        print("Starting OCR Client")
        # Add code to start OCR Client
    elif action_code == 2:
        print("Stopping OCR Client")
        # Add code to stop OCR Client
    elif action_code == 3:
        print("Add another image")
        # Add code to add another image
    elif action_code == 4:
        print("Play/Pause")
        # Add code to get status of OCR Client
 
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
        signal.signal(signal.SIGUSR1, signal_handler)
        ipc = IPC("ocr_client")
        # wait for signals to trigger actions
        print("OCR Client is running and waiting for signals...")
        while True:
            signal.pause()
        # Authenticate
        #config = Config()
        ##config.load_from_env()
        #logger = configure_logging(config.log_level)
        #client = OCRClient(config, logger)
        #client.ensure_authenticated()

    except Exception as e:
        print(f"Error: {e}")
    finally:
        shm.close()
        shm.unlink()
        ipc.cleanup()
    # args = parse_args()
    # config = Config()
    # if args.server_url:
    #     config.server_base_url = args.server_url
    # if args.device_id:
    #     config.device_id = args.device_id
    # if args.log_level:
    #     config.log_level = args.log_level.upper()
    # if args.no_tts:
    #     config.tts_enabled = False

    # logger = configure_logging(config.log_level)

    # if args.enqueue:
    #     enqueue_image(config, logger, args.enqueue)
    #     if not args.process_image:
    #         return

    # client = OCRClient(config, logger)
    # client.install_signal_handlers()

    # if args.process_image:
    #     try:
    #         client.ensure_authenticated()
    #         client._process_image(args.process_image)
    #     finally:
    #         client.close()
    #     return

    # try:
    #     client.run()
    # finally:
    #     client.close()


if __name__ == "__main__":
    main()
