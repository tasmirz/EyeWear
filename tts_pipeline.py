from __future__ import annotations

"""
Lightweight text-to-speech pipeline optimised for Raspberry Pi devices.

Features:
- Optional Bluetooth headset auto-connect using bluetoothctl.
- Uses the espeak-ng engine by default (available on Raspberry Pi OS).
- Supports directing audio through a specific ALSA device (e.g. Bluetooth sink).
- Reads newline-delimited text from stdin, speaking each entry sequentially.
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
from typing import Iterable, Optional


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
    voice: str = os.getenv("TTS_VOICE", "en")
    rate: int = int(os.getenv("TTS_RATE", "165"))
    volume: int = int(os.getenv("TTS_VOLUME", "150"))  # 0-200 for espeak
    espeak_cmd: str = os.getenv("TTS_ESPEAK_CMD", "espeak")
    aplay_cmd: str = os.getenv("TTS_APLAY_CMD", "aplay")
    audio_device: Optional[str] = os.getenv("TTS_AUDIO_DEVICE") or None
    bluetooth_mac: Optional[str] = os.getenv("TTS_BLUETOOTH_MAC") or None
    bluetooth_retries: int = int(os.getenv("TTS_BLUETOOTH_RETRIES", "3"))
    bluetooth_retry_delay: float = float(os.getenv("TTS_BLUETOOTH_RETRY_DELAY", "2.0"))
    log_level: str = os.getenv("TTS_LOG_LEVEL", "INFO")


class TTSPipeline:
    def __init__(self, config: TTSConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._stop_requested = False
        self._ensure_dependencies()

    def _ensure_dependencies(self) -> None:
        if shutil.which(self.config.espeak_cmd) is None:
            raise RuntimeError(
                f"'{self.config.espeak_cmd}' not found. Install espeak-ng (sudo apt install espeak-ng)."
            )
        if self.config.audio_device and shutil.which(self.config.aplay_cmd) is None:
            raise RuntimeError(
                f"Audio device specified but '{self.config.aplay_cmd}' is missing. Install alsa-utils."
            )
        if self.config.bluetooth_mac and shutil.which("bluetoothctl") is None:
            self.logger.warning(
                "Bluetooth MAC provided but 'bluetoothctl' is not installed; skipping auto-connect."
            )

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

    # -------------------------------------------------------------- bluetooth
    def connect_bluetooth(self) -> None:
        mac = self.config.bluetooth_mac
        if not mac:
            return

        if shutil.which("bluetoothctl") is None:
            return

        self.logger.info("Ensuring Bluetooth headset %s is connected", mac)
        for attempt in range(1, self.config.bluetooth_retries + 1):
            if self._bluetooth_connected(mac):
                self.logger.info("Bluetooth device %s already connected", mac)
                return

            self.logger.info("Attempting Bluetooth connection (%d/%d)", attempt, self.config.bluetooth_retries)
            result = subprocess.run(
                ["bluetoothctl", "connect", mac],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and self._bluetooth_connected(mac):
                self.logger.info("Bluetooth device %s connected successfully", mac)
                return

            self.logger.warning(
                "Bluetooth connection attempt %d failed: %s",
                attempt,
                result.stderr.strip(),
            )
            time.sleep(self.config.bluetooth_retry_delay)

        self.logger.error("Unable to connect to Bluetooth device %s after %d attempts", mac, self.config.bluetooth_retries)

    @staticmethod
    def _bluetooth_connected(mac: str) -> bool:
        result = subprocess.run(
            ["bluetoothctl", "info", mac],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        return "Connected: yes" in result.stdout

    # ---------------------------------------------------------------- playback
    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        if self.config.audio_device:
            self._speak_via_aplay(text)
        else:
            self._speak_direct(text)

    def _espeak_base_args(self) -> list[str]:
        return [
            self.config.espeak_cmd,
            "-v",
            self.config.voice,
            "-s",
            str(self.config.rate),
            f"-a{self.config.volume}",
        ]

    def _speak_direct(self, text: str) -> None:
        cmd = self._espeak_base_args() + ["--stdin"]
        try:
            subprocess.run(
                cmd,
                input=text,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            self.logger.error("espeak command failed (%s): %s", exc.returncode, exc)
        except FileNotFoundError:
            self.logger.error("espeak executable not found at %s", self.config.espeak_cmd)

    def _speak_via_aplay(self, text: str) -> None:
        espeak_cmd = self._espeak_base_args() + ["--stdout"]
        aplay_cmd = [self.config.aplay_cmd, "-q"]
        if self.config.audio_device:
            aplay_cmd.extend(["-D", self.config.audio_device])

        try:
            with subprocess.Popen(
                espeak_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as espeak_proc:
                with subprocess.Popen(
                    aplay_cmd,
                    stdin=espeak_proc.stdout,
                    stderr=subprocess.PIPE,
                ) as aplay_proc:
                    if espeak_proc.stdout:
                        espeak_proc.stdout.close()
                    stderr_espeak = ""
                    try:
                        _, stderr_espeak = espeak_proc.communicate(text)
                    finally:
                        if espeak_proc.stdin:
                            espeak_proc.stdin.close()
                    stderr_aplay = aplay_proc.communicate()[1] or ""

            if espeak_proc.returncode != 0:
                self.logger.error("espeak failed (%s): %s", espeak_proc.returncode, stderr_espeak.strip())
            if aplay_proc.returncode != 0:
                self.logger.error("aplay failed (%s): %s", aplay_proc.returncode, stderr_aplay.strip())
        except FileNotFoundError as exc:
            self.logger.error("Required executable missing: %s", exc)

    # ------------------------------------------------------------------- loop
    def run_stream(self, stream: Iterable[str]) -> None:
        self.logger.info("TTS pipeline ready. Awaiting text on stdin.")
        for raw in stream:
            if self._stop_requested:
                break
            text = raw.strip()
            if not text:
                continue
            self.logger.debug("Speaking text: %s", text)
            self.speak(text)

        self.logger.info("TTS pipeline exiting.")

    def request_stop(self) -> None:
        self._stop_requested = True


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequential TTS pipeline for Raspberry Pi.")
    parser.add_argument("--voice", help="espeak voice variant (default: env TTS_VOICE or 'en')")
    parser.add_argument("--rate", type=int, help="Speech rate words per minute (default: env TTS_RATE or 165)")
    parser.add_argument("--volume", type=int, help="espeak volume 0-200 (default: env TTS_VOLUME or 150)")
    parser.add_argument("--audio-device", help="ALSA device for playback (e.g., bluealsa:DEV=XX:XX:XX:XX:XX:XX,PROFILE=a2dp)")
    parser.add_argument("--bluetooth-mac", help="Bluetooth headset MAC address to auto-connect")
    parser.add_argument("--bluetooth-retries", type=int, help="Bluetooth connection retries (default 3)")
    parser.add_argument("--bluetooth-retry-delay", type=float, help="Seconds between Bluetooth attempts (default 2.0)")
    parser.add_argument("--espeak-cmd", help="Path to espeak/espeak-ng executable")
    parser.add_argument("--aplay-cmd", help="Path to aplay executable for ALSA playback")
    parser.add_argument("--speak", help="Speak the provided text once and exit")
    parser.add_argument("--log-level", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> TTSConfig:
    cfg = TTSConfig()
    if args.voice:
        cfg.voice = args.voice
    if args.rate is not None:
        cfg.rate = args.rate
    if args.volume is not None:
        cfg.volume = args.volume
    if args.audio_device:
        cfg.audio_device = args.audio_device
    if args.bluetooth_mac:
        cfg.bluetooth_mac = args.bluetooth_mac
    if args.bluetooth_retries is not None:
        cfg.bluetooth_retries = args.bluetooth_retries
    if args.bluetooth_retry_delay is not None:
        cfg.bluetooth_retry_delay = args.bluetooth_retry_delay
    if args.espeak_cmd:
        cfg.espeak_cmd = args.espeak_cmd
    if args.aplay_cmd:
        cfg.aplay_cmd = args.aplay_cmd
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
    pipeline.connect_bluetooth()

    if args.speak:
        pipeline.speak(args.speak)
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
