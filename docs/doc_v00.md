# Eyewear Embedded Base – Unified Technical Guide

This document provides an end-to-end view of the Eyewear embedded stack. It consolidates all module‑level notes into cohesive subsystems so you can understand how capture, networking, OCR, and speech synthesis cooperate.

---

## 1. System Overview

```mermaid
flowchart LR
    button["Bluetooth Headset\n(earbud_input.py)"] --> trigger[take_image.py\nCapture]
    trigger --> queue["Image Queue\n(mp_queue.py / filesystem)"]
    queue --> client["OCR Client\n(ocr_client.py)"]
    client --> server["OCR Server\n(bbocr_server/server.py)"]
    server --> pipeline["OCR Pipeline\n(pipeline_utils.py / pipeline.py)"]
    pipeline --> server
    server --> gemini["Gemini Post-process\n(system_prompt.py)"]
    server --> client
    client --> tts["TTS Pipeline\n(tts_pipeline.py)"]
    tts --> user["Audio Output"]
```

Supporting libraries (e.g., `common.py`, `bbocr_server/modules/…`) provide shared utilities, model download helpers, and abstract interfaces to keep components decoupled.

---

## 2. Device Interaction Suite

### 2.1 `earbud_input.py`

Listens to Bluetooth headset media buttons via `evdev` so a wearer can trigger image capture or queue management.

```mermaid
flowchart TD
    start(["Launch script"]) --> scan["find_bluetooth_device()"]
    scan --> found{Media-key device?}
    found -- yes --> grab["device.grab() (optional)"]
    found -- no --> manual["Prompt for manual selection"]
    manual --> grab
    grab --> loop["read_button_events(device)"]
    loop --> action{EV_KEY press?}
    action -- yes --> map["Map to PLAY/NEXT/etc\nand log/trigger callback"]
    action -- no --> loop
```

Key points:

- Prints recognised media keys; extend the mapping to enqueue jobs or call REST endpoints.
- Grabbing the device prevents other apps from receiving events; disable if coexistence is needed.
- Suggestion: wire button handlers to call `take_image.py` or push paths via `mp_queue.py`.

### 2.2 `take_image.py`

Captures still frames with Picamera2, applying autofocus hinting before storage.

```mermaid
sequenceDiagram
    participant User
    participant Script
    participant Cam as Picamera2

    User->>Script: python3 take_image.py [timestamp]
    Script->>Cam: configure(still, 1920×1080)
    Script->>Cam: start()
    Script->>Cam: set_controls(AfMode=0)
    Script->>Cam: set_controls(LensPosition=425)
    Script->>Script: sleep(3s for stability)
    Script->>Cam: capture_file(image_<ts>.jpg)
    Cam-->>Script: saved JPEG
    Script->>Cam: stop()
    Script->>User: print path
```

- Override timestamp via CLI or let the script auto-generate one.
- Integrate with the queue by adding a post-capture call to the OCR client’s enqueue helper.

### 2.3 `common.py`

Shared utility for PID-file bookkeeping and basic tagged logging.

| Class    | Purpose                                                                                       |
| -------- | --------------------------------------------------------------------------------------------- |
| `IPC`    | Creates `.pid/<name>.pid`, ensures singleton behaviour, and exposes `read_pid`/`send_signal`. |
| `Logger` | Minimal wrapper around `logging.basicConfig` to streamline CLI logging.                       |

```mermaid
flowchart TD
    start["Script start"] --> init["IPC(name)"]
    init --> exists{PID exists?}
    exists -- yes --> abort["Log + exit(1)"]
    exists -- no --> write["create_pidfile()"]
    write --> run["Run script logic"]
    run --> signal["send_signal/read_pid as needed"]
```

- PID files reside under `.pid/` relative to the CWD; align service runs with that assumption.
- Only uses standard library modules, keeping footprint tiny.

---

## 3. Queuing & Interprocess Communication

### `bbocr_server/mp_queue.py`

Exposes a `multiprocessing.Queue` over a manager server so separate processes can enqueue filenames.

```mermaid
flowchart TD
    start([CLI]) --> mode{--start-server?}
    mode -- yes --> srv["QueueManager.get_server().serve_forever()"]
    mode -- no --> hasItems{Items provided?}
    hasItems -- no --> err[Log error & exit 1]
    hasItems -- yes --> connect["QueueManager.connect()"]
    connect --> enqueue["queue.put(item) for each input"]
```

CLI samples:

- `python3 mp_queue.py --start-server`
- `python3 mp_queue.py img1.jpg img2.jpg`

Use this when capture scripts run in separate processes or machines but need to feed the main OCR client pipeline.

---

## 4. Client Runtime (Embedded Device)

### 4.1 `ocr_client.py`

Central controller on the embedded device. It dequeues image paths, authenticates against the server, uploads images, and voices results.

```mermaid
sequenceDiagram
    participant Q as ImageQueue
    participant C as OCRClient
    participant S as Server
    participant T as TTSPipeline

    loop Images
        Q->>C: next path
        C->>S: /get_challenge (public key)
        S-->>C: challenge + JWT
        C->>S: /auth (token + signature)
        S-->>C: bearer token (fingerprint)
        C->>S: /ocr (multipart upload)
        S-->>C: {text, html, markdown}
        C->>T: speak(text)
    end
```

Core building blocks:

| Component    | Description                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------- |
| `Config`     | Aggregates env vars/CLI flags: queue backend choice, key paths, server URL, logging, TTS options. |
| `KeyManager` | Generates or loads RSA keys, signs challenges, stores server public key.                          |
| `ImageQueue` | Chooses between POSIX message queue or filesystem directory backend.                              |
| `TTSSink`    | Launches `tts_pipeline.py`, writes recognised text to its stdin, restarts on failure.             |

Operational notes:

- Maintains bearer tokens, refreshing automatically before expiry.
- Accepts CLI utilities like `--enqueue`, `--process-image`, and `--no-tts`.
- Logs queue read and upload issues, backing off per `retry_delay`.

### 4.2 `tts_pipeline.py`

Provides speech output for recognised text, with optional Bluetooth headset auto-connect.

```mermaid
flowchart TD
    start(["python3 tts_pipeline.py ..."]) --> parse["parse_args()"]
    parse --> config["build_config()"]
    config --> init["TTSPipeline(config)"]
    init --> deps["_ensure_dependencies()"]
    init --> sig["install_signal_handlers()"]
    sig --> speak{--speak text?}
    speak -- yes --> once["pipeline.speak(text)"]
    speak -- no --> bt["connect_bluetooth()"]
    bt --> stream["run_stream(stdin)"]
    stream --> speakLoop["for line -> speak()"]
```

- Supports direct `espeak-ng` playback or piping audio through ALSA (`aplay`) when a specific device is set.
- Gracefully handles SIGINT/SIGTERM, flushing remaining text before exit.
- Provides environment variables (`TTS_VOICE`, `TTS_RATE`, `TTS_BLUETOOTH_MAC`, etc.) for quick tuning.

---

## 5. Server Runtime

### 5.1 `bbocr_server/server.py`

Flask application that authenticates devices, queues OCR jobs, invokes the pipeline, and optionally calls Gemini to polish text.

#### Architectural pieces

| Component          | Role                                                                                              |
| ------------------ | ------------------------------------------------------------------------------------------------- |
| `ServerConfig`     | Loads environment configuration for Mongo, keys, JWTs, queue timeouts, Gemini keys, etc.          |
| `ServerKeyManager` | Ensures RSA key pair is present and exposes PEM strings.                                          |
| `DeviceRepository` | Looks up authorised public keys via MongoDB or fallback JSON/PEM files (`authorized_keys`).       |
| `AuthService`      | Issues challenges, verifies signatures, mints bearer tokens keyed by public-key fingerprint.      |
| `OCRPipeline`      | Runs a background worker with multiprocessing queues, calling `pipeline_utils.render_image_html`. |
| `GeminiClient`     | Submits HTML to Gemini for Bangla proofreading, using `system_prompt.py`.                         |

#### Authentication flow

```mermaid
sequenceDiagram
    participant Device
    participant Auth
    participant Repo
    participant Keys

    Device->>Auth: POST /get_challenge {public_key}
    Auth->>Repo: find_by_public_key(public_key)
    Repo-->>Auth: DeviceRecord(fingerprint)
    Auth-->>Device: {challenge, challenge_token}
    Device->>Auth: POST /auth {challenge_token, signed_challenge}
    Auth->>Auth: verify JWT integrity & expiry
    Auth->>Repo: re-validate public key
    Auth->>Auth: verify RSA signature
    Auth-->>Device: {token, key_fingerprint, expires_in, server_public_key}
```

Tokens carry `key_fingerprint` rather than legacy device IDs, letting key rotation drive identity.

#### OCR request handling

```mermaid
flowchart TD
    upload[/POST /ocr/] --> verify["_extract_token + verify_access_token()"]
    verify --> store["Save temp file under tmp_dir"]
    store --> submit["OCRPipeline.submit()"]
    submit --> worker["(Worker process)"]
    worker --> html["render_image_html()"]
    worker --> respQueue["response_queue.put()"]
    respQueue --> submit
    submit --> cleanup["Delete temp file"]
    cleanup --> gemini["GeminiClient.generate_markdown()"]
    gemini --> reply["Return JSON {text, html, markdown, key_fingerprint}"]
```

Deployment guidelines:

- Supply PEM files for each allowed device under `authorized_keys/` or JSON fallback.
- Set a strong `JWT_SECRET` and configure optionally for MongoDB lookups.
- Gemini integration is optional; when disabled the server still returns HTML/text.

### 5.2 `bbocr_server/pipeline_utils.py`

Facade over the heavyweight OCR pipeline, guaranteeing HTML output even when dependencies are missing.

- Checks if `pipeline.py` (full stack) is available; if not, falls back to Tesseract.
- `_escape_and_wrap` sanitises results and wraps them in HTML tags.
- `BB_OCR_LANG` environment variable defaults to `ben+eng` for fallback language hints.

```mermaid
flowchart TD
    start["render_image_html(path, lang)"] --> resolve["Resolve path\n& language"]
    resolve --> full{Full pipeline available?}
    full -- yes --> tryFull["full_pipeline.render_image_html()"]
    tryFull -- success --> returnHTML
    tryFull -- exception --> log["Log + fallback"]
    full -- no --> fallback
    fallback["pytesseract.image_to_string()"] --> wrap["_escape_and_wrap()"]
    wrap --> returnHTML[HTML string]
```

### 5.3 `bbocr_server/pipeline.py`

The full OCR workflow coupling YOLO layout analysis, Paddle DBNet detection, and ApsisNet recognition.

| Stage          | Key Functions                                                                                                        |
| -------------- | -------------------------------------------------------------------------------------------------------------------- |
| Configuration  | `parse_arguments`, `build_option`                                                                                    |
| Model loading  | `load_yolo`, `load_model`, `load_model2`, `load_bocr`                                                                |
| Pre-processing | `padWordImage`, `correctPadding`, `word_horizontal_dilation`, `line_horizontal_dilation`, `line_vertical_dilation`   |
| Detection      | `dla_predict`, `run_yolo_model`, `crop_all_text_box`, `single_image_layout`                                          |
| Recognition    | `recognize_word`, `word_predict`, `word_batch_predict`                                                               |
| Reconstruction | `generate_html`, `reconstruct`, `merge_image_arrays`, plus numerous `viz_*` helpers for debugging graphical overlays |

```mermaid
flowchart TD
    input[Input image] --> models[Load YOLO + Paddle DBNet + ApsisNet]
    models --> layout[YoloDLA regions]
    layout --> detect[DBNet line/word boxes]
    detect --> crops["get_crops()"]
    crops --> recognise[ApsisNet infer -> text]
    recognise --> align[Map text back to layout]
    align --> html["generate_html()"]
    html --> output[Persist or return artifacts]
```

Outputs include pickled ROI datasets, word-level boxes, and HTML/Markdown reconstructions. These artifacts aid debugging and allow incremental processing.

### 5.4 `bbocr_server/system_prompt.py`

Defines the Bangla proofreading prompt used when invoking Gemini. It emphasises:

- correcting spelling, grammar, punctuation,
- preserving Bangla phrasing,
- avoiding translation into other languages.

### 5.5 `bbocr_server/server_pipeline.py` (Concept Stage)

Sketches an alternative server that would:

- authenticate clients,
- push jobs into Redis,
- call `pipeline.py`,
- send results through a Gemini API.

The implementation currently consists of constants/imports and a docstring describing the intended flow.

---

## 6. OCR Model Modules (`bbocr_server/modules/…`)

These Python packages encapsulate model interfaces and shared utilities used by both the full and fallback OCR pipelines.

### 6.1 Abstract Interfaces – `modules.py`

Defines contracts for detectors, recognisers, and layout analysers.

```mermaid
classDiagram
    class Recognizer {
        +infer(...) *
    }
    class Detector {
        +get_word_boxes(...) *
        +get_line_boxes(...) *
        +get_crops(...) *
    }
    class LayoutAnalyzer {
        +get_rois(...) *
    }

    ApsisNet --|> Recognizer
    PaddleDBNet --|> Detector
    YoloDLA --|> LayoutAnalyzer
```

Implementations must provide these methods so the pipeline can swap models without refactoring.

### 6.2 Utility Helpers – `utils.py`

Shared helpers for logging, directory preparation, and Google Drive downloads.

```mermaid
flowchart TD
    start["Model initialisation"] --> ensure["create_dir(base, ext)"]
    ensure --> exists{Weights exist?}
    exists -- no --> download["gdown.download(id, path)"]
    exists -- yes --> ready[Return path]
    download --> ready
```

- `LOG_INFO`: coloured console logging.
- `create_dir`: ensures model directories exist (`~/.bengali_ai_ocr/...`).
- `download`: fetches files via `gdown`.
- `dotdict`: attribute-style dictionary access for convenience.

### 6.3 ApsisNet Recogniser – `apsisnet.py`

ONNX-based Bangla word recogniser with optional Unicode normalisation.

```mermaid
flowchart TD
    start["Crops list"] --> pad["correctPadding(img)"]
    pad --> norm["img / 255.0"]
    norm --> expand["np.expand_dims(img, axis=0)"]
    expand --> batch["Stack batch tensors"]
    batch --> ort["self.model.run(...)"]
    ort --> decode["Argmax sequence until 'sep'"]
    decode --> normalise["Normalizer().normalize()"]
    normalise --> output["Decoded texts"]
```

- Downloads ONNX weights into `~/.bengali_ai_ocr/bnocr.onnx` if missing.
- Vocabulary includes Bangla characters plus special tokens (`blank`, `sep`, `pad`).
- Batch size auto-adjusts based on available crops.

### 6.4 Paddle DBNet Detector – `paddledbnet.py`

Detects text lines and words using PaddleOCR. Handles model downloads, GPU setup, and crop extraction.

```mermaid
flowchart TD
    init["PaddleDBNet.__init__"] --> runtime["Configure fd.RuntimeOption"]
    runtime --> dirs["create_dir(~/.bengali_ai_ocr/, line/word)"]
    dirs --> dl["maybe_download(...tar)"]
    dl --> load1["load_model(line)"]
    dl --> load2["load_model(word)"]
```

Key features:

- Configurable thresholds (`det_db_thresh`, `det_db_box_thresh`, etc.).
- Extracts rotated crops using perspective transforms.
- Supports GPU acceleration via FastDeploy.

### 6.5 YOLO Layout Analyzer – `yolodla.py`

Ultralytics YOLO model that segments document structure (paragraphs, tables, images, etc.).

```mermaid
flowchart TD
    init["YoloDLA.__init__"] --> weights["get_model_weights()"]
    weights --> exists{best.pt available?}
    exists -- no --> download["gdown.download(..., best.pt)"]
    exists -- yes --> load
    download --> load
    load --> model["YOLO(weight_path)"]
```

- Returns normalised coordinates and metadata for each detected region to guide cropping and recognition.
- Currently carries a local definition of `LayoutAnalyzer` for quick testing; align with `modules.py` for production use.

### 6.6 Module Initialiser – `__init__ .py`

Empty placeholder ensuring `bbocr_server/modules/` behaves as a package. The filename contains a stray space (`__init__ .py`); rename to `__init__.py` for tooling compatibility.

---

## 7. Future Considerations & Tips

- **Key Management**: Maintain PEM files for each device in `bbocr_server/authorized_keys`, and rotate keys by fingerprint rather than device IDs.
- **Queue Choices**: POSIX queues offer low latency; filesystem queues are useful for portability or debugging. `mp_queue.py` bridges remote producers.
- **Monitoring**: Use logs from `ocr_client`, server, and TTS modules to detect fallback scenarios (e.g., Gemini disabled, pipeline fallback to Tesseract).
- **Extensibility**: Implement new recognisers/detectors with the ABCs in `modules.py`, then plug them into `pipeline.py` or `pipeline_utils.py`.
- **Documentation**: This unified guide replaces individual markdown files; update it when components evolve to keep architectural knowledge centralised.
