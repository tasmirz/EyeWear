from __future__ import annotations

"""
Lightweight text-to-speech pipeline powered by Meta's MMS-TTS (VITS) models.

Features:
- Streams Bangla text to `facebook/mms-tts-ben` (configurable via env/CLI).
- Generates MP3 files for each line of text received on stdin.
- Relies on PyTorch + Hugging Face Transformers; no external TTS engine required.
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
import tempfile
import re
import wave
import numpy as np

try:
    import torch
    from transformers import AutoTokenizer, VitsModel
except ImportError:  # pragma: no cover - runtime dependency check
    torch = None  # type: ignore
    AutoTokenizer = None  # type: ignore
    VitsModel = None  # type: ignore


def configure_logging(level: str) -> logging.Logger:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("tts_pipeline")
    logger.setLevel(numeric)
    return logger


@dataclass
class TTSConfig:
    model_id: str = os.getenv("MMS_TTS_MODEL_ID", "facebook/mms-tts-ben")
    device: str = os.getenv("MMS_TTS_DEVICE", "cpu")
    dtype: Optional[str] = os.getenv("MMS_TTS_DTYPE") or None
    speaker: Optional[str] = os.getenv("MMS_TTS_SPEAKER") or None
    seed: Optional[int] = int(os.getenv("MMS_TTS_SEED", "").strip()) if os.getenv("MMS_TTS_SEED") else None
    log_level: str = os.getenv("TTS_LOG_LEVEL", "INFO")
    output_dir: Path = Path(os.getenv("TTS_OUTPUT_DIR") or Path.cwd()).expanduser().resolve()
    ffmpeg_cmd: str = os.getenv("FFMPEG_CMD", shutil.which("ffmpeg") or "ffmpeg")


class TTSPipeline:
    def __init__(self, config: TTSConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._stop_requested = False
        self._ensure_dependencies()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_dependencies(self) -> None:
        if torch is None or AutoTokenizer is None or VitsModel is None:
            raise RuntimeError(
                "Missing TTS dependencies. Install 'torch', 'transformers>=4.33', and 'accelerate' "
                "to use facebook/mms-tts models."
            )
        if shutil.which(self.config.ffmpeg_cmd) is None and not Path(self.config.ffmpeg_cmd).is_file():
            raise RuntimeError(
                f"'{self.config.ffmpeg_cmd}' not found. Install ffmpeg or set FFMPEG_CMD to a valid executable."
            )
        self._load_model()

    def _resolve_dtype(self) -> Optional[torch.dtype]:
        if not self.config.dtype:
            return None
        mapping = {
            "float32": torch.float32,
            "fp32": torch.float32,
            "float16": torch.float16,
            "fp16": torch.float16,
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
        }
        key = self.config.dtype.lower()
        if key not in mapping:
            self.logger.warning("Unsupported dtype '%s'; falling back to float32.", self.config.dtype)
            return None
        return mapping[key]

    def _load_model(self) -> None:
        assert torch is not None and AutoTokenizer is not None and VitsModel is not None
        dtype = self._resolve_dtype()
        self.device = torch.device(self.config.device)

        self.logger.info("Loading MMS TTS model '%s' on %s", self.config.model_id, self.device)
        self.tokenizer = AutoTokenizer.from_pretrained(self.config.model_id)
        self.model = VitsModel.from_pretrained(self.config.model_id)
        if dtype is not None:
            self.model = self.model.to(dtype=dtype)
        self.model = self.model.to(self.device)
        self.model.eval()

        self.sampling_rate = getattr(self.model.config, "sampling_rate", 16000)
        self.speaker_id: Optional[int] = None

        speaker_map = getattr(self.model.config, "speaker_ids", None)
        if self.config.speaker:
            if isinstance(speaker_map, dict) and self.config.speaker in speaker_map:
                self.speaker_id = speaker_map[self.config.speaker]
            elif self.config.speaker.isdigit():
                self.speaker_id = int(self.config.speaker)
            else:
                self.logger.warning(
                    "Speaker '%s' not recognised; using default speaker.", self.config.speaker
                )
        elif isinstance(speaker_map, dict) and len(speaker_map) == 1:
            self.speaker_id = next(iter(speaker_map.values()))

        if self.config.seed is not None:
            torch.manual_seed(self.config.seed)

    # ------------------------------------------------------------------ signals
    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.logger.info("Received signal %s; shutting down TTS pipeline.", signum)
            self._stop_requested = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                pass

    # ---------------------------------------------------------------- synthesis
    def speak(self, text: str, output_name: Optional[str] = None) -> None:
        text = text.strip()
        if not text:
            return

        safe_base = re.sub(r"[^A-Za-z0-9_-]", "_", (output_name or "").strip())
        if not safe_base:
            safe_base = f"tts_{int(time.time())}"

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        wav_path = Path(tmp_path)

        try:
            self._synth_to_wav(text, wav_path)
            mp3_path = self._wav_to_mp3(wav_path, safe_base)
            self.logger.info("Generated MP3 TTS output: %s", mp3_path)
        finally:
            wav_path.unlink(missing_ok=True)

    def _synth_to_wav(self, text: str, wav_path: Path) -> None:
        assert torch is not None
        inputs = self.tokenizer(text, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        if self.speaker_id is not None:
            inputs["speaker_id"] = torch.tensor([self.speaker_id], device=self.device, dtype=torch.long)

        with torch.no_grad():
            outputs = self.model(**inputs)

        waveform = outputs.waveform.squeeze().cpu().numpy()
        if waveform.ndim == 0:
            waveform = np.expand_dims(waveform, axis=0)
        waveform = np.clip(waveform, -1.0, 1.0)
        pcm16 = (waveform * 32767.0).astype(np.int16)

        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # int16
            wav_file.setframerate(int(self.sampling_rate))
            wav_file.writeframes(pcm16.tobytes())

    def _wav_to_mp3(self, wav_path: Path, base_name: str) -> Path:
        safe_base = re.sub(r"[^A-Za-z0-9_-]", "_", base_name.strip())
        if not safe_base:
            safe_base = f"tts_{int(time.time())}"

        mp3_path = self.config.output_dir / f"{safe_base}.mp3"
        convert_cmd = [
            self.config.ffmpeg_cmd,
            "-y",
            "-f",
            "wav",
            "-i",
            str(wav_path),
            "-ar",
            str(self.sampling_rate),
            "-ac",
            "1",
            str(mp3_path),
        ]
        try:
            subprocess.run(convert_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffmpeg conversion failed (code={exc.returncode})") from exc
        return mp3_path

    # ------------------------------------------------------------------- loop
    def run_stream(self, stream: Iterable[str]) -> None:
        self.logger.info("TTS pipeline ready. Awaiting text on stdin.")
        for raw in stream:
            if self._stop_requested:
                break
            line = raw.strip()
            if not line:
                continue
            output_name: Optional[str] = None
            text = line
            if "|" in line:
                prefix, body = line.split("|", 1)
                body = body.strip()
                if not body:
                    continue
                output_name = prefix.strip() or None
                text = body
            self.logger.debug(
                "Processing TTS request (mp3=%s): %s",
                f"{output_name}.mp3" if output_name else "stdout",
                text,
            )
            try:
                self.speak(text, output_name)
            except Exception as exc:
                self.logger.error("TTS synthesis failed: %s", exc)

        self.logger.info("TTS pipeline exiting.")

    def request_stop(self) -> None:
        self._stop_requested = True

    # ------------------------------------------------------------------ signals
    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.logger.info("Received signal %s; shutting down TTS pipeline.", signum)
            self._stop_requested = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                # Occurs when running in a thread or unsupported environment.
                pass

    # ---------------------------------------------------------------- synthesis
    def speak(self, text: str, output_name: Optional[str] = None) -> None:
        text = text.strip()
        if not text:
            return

        safe_base = re.sub(r"[^A-Za-z0-9_-]", "_", (output_name or "").strip())
        if not safe_base:
            safe_base = f"tts_{int(time.time())}"

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        wav_path = Path(tmp_path)

        try:
            self._synth_to_wav(text, wav_path)
            mp3_path = self._wav_to_mp3(wav_path, safe_base)
            self.logger.info("Generated MP3 TTS output: %s", mp3_path)
        finally:
            wav_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------- loop
    def run_stream(self, stream: Iterable[str]) -> None:
        self.logger.info("TTS pipeline ready. Awaiting text on stdin.")
        for raw in stream:
            if self._stop_requested:
                break
            line = raw.strip()
            if not line:
                continue
            output_name: Optional[str] = None
            text = line
            if "|" in line:
                prefix, body = line.split("|", 1)
                body = body.strip()
                if not body:
                    continue
                output_name = prefix.strip() or None
                text = body
            self.logger.debug(
                "Processing TTS request (mp3=%s): %s",
                f"{output_name}.mp3" if output_name else "stdout",
                text,
            )
            self.speak(text, output_name)

        self.logger.info("TTS pipeline exiting.")

    def request_stop(self) -> None:
        self._stop_requested = True


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequential TTS pipeline powered by facebook/mms-tts.")
    parser.add_argument("--model-id", help="Hugging Face model id (default: env MMS_TTS_MODEL_ID or facebook/mms-tts-ben)")
    parser.add_argument("--device", help="Torch device for inference (default: env MMS_TTS_DEVICE or 'cpu')")
    parser.add_argument("--dtype", help="Torch dtype (float32, float16, bfloat16) (env MMS_TTS_DTYPE)")
    parser.add_argument("--speaker", help="Speaker id/name for multi-speaker models (env MMS_TTS_SPEAKER)")
    parser.add_argument("--seed", type=int, help="Seed to make synthesis deterministic (env MMS_TTS_SEED)")
    parser.add_argument("--output-dir", help="Directory to store generated MP3 files (env TTS_OUTPUT_DIR)")
    parser.add_argument("--ffmpeg-cmd", help="Path to ffmpeg executable (env FFMPEG_CMD)")
    parser.add_argument("--speak", help="Speak the provided text once and exit (generates a temp MP3)")
    parser.add_argument("--log-level", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> TTSConfig:
    cfg = TTSConfig()
    if args.model_id:
        cfg.model_id = args.model_id
    if args.device:
        cfg.device = args.device
    if args.dtype:
        cfg.dtype = args.dtype
    if args.speaker:
        cfg.speaker = args.speaker
    if args.seed is not None:
        cfg.seed = args.seed
    if args.output_dir:
        cfg.output_dir = Path(args.output_dir).expanduser()
    if args.ffmpeg_cmd:
        cfg.ffmpeg_cmd = args.ffmpeg_cmd
    if args.log_level:
        cfg.log_level = args.log_level
    return cfg


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    config = build_config(args)
    logger = configure_logging(config.log_level)

    try:
        pipeline = TTSPipeline(config, logger)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    pipeline.install_signal_handlers()

    if args.speak:
        pipeline.speak(args.speak, output_name="cli_sample")
        return 0

    try:
        pipeline.run_stream(sys.stdin)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        pipeline.request_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
