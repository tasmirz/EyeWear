# EyeWear — Raspberry Pi Zero 2 client for assistive smart glasses

A compact Raspberry Pi Zero 2 client intended to attach to eyeglasses for people with visual impairment. The device exposes two primary, earbud-button-driven features:

1. OCR (optical character recognition) — capture photos and play spoken results.
2. Live video calling — a prototype, industry-like calling feature for remote help or companion services.

## Controls (via Bluetooth earbuds buttons)

The client listens for button events from Bluetooth earbuds and maps them to actions depending on the current mode. Typical button mappings in the codebase:

- Double tap: OCR / take action (mode dependent)
- Long press: start call / stop OCR (mode dependent)
- Single tap: mute/unmute or pause OCR (mode dependent)

## High-level architecture

- `earbud_input.py` — listens for the Bluetooth input device and converts button events into high-level commands. It writes small integer signals to POSIX shared memory and notifies worker processes via UNIX signals.

- `ocr_process.py` / `pipeline.py` / `ocr_server.py` — OCR subsystem. `ocr_process.py` is the local client process which uses `pipeline.py` for image handling and processing. `ocr_server.py` contains (or coordinates with) the model/server side logic (the repo contains a `bbocr_server` directory with a model and utilities).

- `call_client.py` / `eyewear.py` — calling subsystem that implements the live video call client that talks to a prototype server. `eyewear.py` contains top-level wiring and helpers for running the device features.

- `audio_feedback.py` — plays pre-recorded or synthesized audio feedback (beeps, spoken numbers, status messages) through the Pi's audio interface.

- `earbud_emulator.py` — a helper/test utility to simulate earbud button events when you don't have a Bluetooth headset available.

- `common.py` — shared constants, IPC helpers, enums and simple wrappers used across modules (shared memory names, signal values, mode helpers, etc.).

- `sync` — helper script to sync files or environment on-device; used during development and deployment.

## How signals, shared memory and multiprocessing queues are used

The project uses a small set of IPC primitives to keep the runtime efficient and simple on constrained hardware:

- POSIX shared memory (multiprocessing.shared_memory.SharedMemory):

  - Small fixed-size integer buffers are used to exchange numeric signals between processes (for example: `ocr_signal`, `call_signal`, `ocr_queue_count`, `ocr_queue_images`).
  - Writing to shared memory is used to enqueue a compact command/state code that worker processes read and act upon.

- UNIX signals (signal.SIGUSR1, SIGUSR2, etc.):

  - After writing a value into shared memory, the initiating process sends a UNIX signal (SIGUSR1/SIGUSR2) to the target process to notify it that a new command is available. This keeps latency low and avoids busy-polling.
  - Signal handlers are lightweight; they read the small shared memory integer and trigger worker behavior (for example, start/stop/pause OCR or update playback feedback).

- Multiprocessing queues (where used):
  - Larger data or batches (for example: image buffers or items to process) are passed via multiprocessing queues when a structured handoff is needed.
  - Queues provide a safe way for producers (camera thread/process) to hand off image data to consumers (OCR pipeline/process) while keeping processes isolated.

Why this combination?

- Shared memory is fast and tiny for simple integer commands and counts.
- UNIX signals provide a low-latency notification mechanism so workers wake only when needed.
- Multiprocessing queues are used for bulkier payloads and maintain safe serialization across process boundaries.

## Run / test (on-device)

Install dependencies (example; check `bbocr_server/requirements.txt` and other per-module requirements):

```bash
# run these on Raspbian / Raspberry Pi OS (zsh / bash compatible)
python3 -m pip install -r bbocr_server/requirements.txt
python3 -m pip install evdev
```

Try the earbud input listener (interactive; will scan for input devices):

```bash
python3 earbud_input.py
```

Emulate earbud events for local testing:

```bash
python3 earbud_emulator.py
```

Start the OCR pipeline process (the real command depends on your configuration):

```bash
python3 ocr_process.py &
# or run with your chosen supervisor (systemd, tmux, screen)
```

Start the call client (if configured):

```bash
python3 call_client.py
```

## Design notes and constraints

- The Pi Zero 2 is resource constrained. The architecture keeps compute-heavy work (model inference) either optimized locally in `bbocr_server/models` or offloaded to a more capable server depending on configuration.
- Signals and small shared memory blocks minimize CPU usage and latency for simple control flows.
- Be careful to close shared memory blocks and device file descriptors on shutdown to fully release hardware resources.

## Files of interest

(quick single-line purpose for the files you asked about)

- `ocr_process.py` — Local OCR client process; reacts to shared-memory signals and dispatches images through `pipeline.py`.
- `ocr_server.py` — Server-side / model orchestration for OCR (models and heavy inference code live under `bbocr_server/`).
- `pipeline.py` — Image capture/roi/cropping and pre/post-processing pipeline for OCR.
- `call_client.py` — Live video calling client and call-control logic.
- `audio_feedback.py` — Audio playback manager and list of available feedback sounds.
- `earbud_emulator.py` — Simulates earbud button events for development/testing.
- `earbud_input.py` — Bluetooth earbud button listener; maps button events to high-level actions and signals worker processes.
- `common.py` — Enums, shared memory names, IPC helpers, and small utilities used by multiple processes.
- `eyewear.py` — Top-level app wiring and helpers for starting the system in pre-defined configurations.
- `sync` — Deployment / synchronization helper script.

## Contributing

If you plan to extend or optimize the system:

- Keep the IPC contract (shared memory names and integer codes) stable between modules.
- Avoid blocking expensive work inside signal handlers; handlers should only read the small command and schedule work onto a worker/queue.
- Add unit tests for `pipeline.py` image transforms and small integration tests for the end-to-end OCR flow.

## License & acknowledgements

See individual module headers and the `bbocr_server/LICENSE` for model and code licensing details.

If you want, I can:

- Add quick start/service unit files for systemd to run `ocr_process` and `earbud_input` at boot.
- Create a small diagram showing the IPC flows (signals/shared memory/queues).
