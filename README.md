# Embedded OCR Pipeline (Raspberry Pi)

This folder hosts a minimal end-to-end pipeline for capturing images, sending
them to an OCR service, and forwarding the recognised text to Gemini and (optionally) a local
text-to-speech (TTS) handler. It is designed for resource-constrained devices such
as Raspberry Pi Zero 2.

---

## Architecture Overview

- **`bbocr_server/server.py`**  
  Flask API handling RSA challenge–response authentication, receiving image uploads,
  rendering Bangla OCR to HTML (via `pipeline_utils.py` with the full pipeline when
  available, otherwise a pytesseract fallback), and requesting a Gemini summary.

- **`ocr_client.py`**  
  Command-line client that watches an image queue, authenticates with the server,
  uploads images, and pipes recognised text to TTS.

- **`tts_pipeline.py`**  
  Lightweight TTS helper (defaults to the Bangla `bn` voice in `espeak-ng`) and
  can route audio through ALSA/BlueZ when a Bluetooth headset is available.

- **Other Helpers**  
  `take_image.py` captures camera stills; queue backends (POSIX message queue or filesystem) connect producers and the OCR client.

---

## Prerequisites

### Bangla OCR Assets

The high-accuracy pipeline expects the Bangla OCR models that ship with the
larger project:

- `bnocr.onnx`
- `best.pt`

Place them on the device and update the paths in `bbocr_server/pipeline.py`
if they differ from the defaults.  
If these files are missing, the server automatically falls back to
`pytesseract`; accuracy on Bangla will be noticeably lower.

1. **System Packages (Raspberry Pi OS / Debian)**

   ```bash
   sudo apt update
   sudo apt install python3 python3-pip espeak-ng alsa-utils bluetooth \
       libblas-dev liblapack-dev libatlas-base-dev tesseract-ocr tesseract-ocr-ben
   ```

   _Install `espeak-ng` to access the Bangla (`bn`) voice used by the TTS pipeline; `alsa-utils` and `bluetooth` are only needed if you plan to play audio._

2. **Python Dependencies**

   ```bash
   pip3 install flask flask-cors requests pyjwt cryptography pillow pytesseract
   pip3 install python-multipart  # only needed if the server runs under Uvicorn/FastAPI
   ```

3. **Environment Variables**
   Copy `.env.local` (or create a new `.env`) under `embedded_base/bbocr_server`
   providing at least:

   ```*
   GEMINI_AI_API_KEY="your_google_genai_key"
   GEMINI_AI_MODEL="gemini-2.0-flash"  # or preferred model
   ```

   The server automatically loads `.env` and `.env.local` on startup.

---

## Usage Guide

| Step                 | Command                                                                                                                                               | Notes                                                                                                |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 1. Start server      | `cd embedded_base/bbocr_server`<br>`export $(grep -v '^#' .env.local \| xargs)`<br>`SERVER_PORT=8080 python3 server.py > /tmp/bbocr_flask.log 2>&1 &` | Launches the Flask OCR service (loads Bangla models if available). Use another port if 8080 is busy. |
| 2. Health check      | `curl -s http://127.0.0.1:8080/health`                                                                                                                | Confirms the server is listening.                                                                    |
| 3a. Queue image      | `python3 embedded_base/ocr_client.py --enqueue embedded_base/test_images/sample.jpg`                                                                  | Adds an image to the filesystem/POSIX queue. Repeat per image.                                       |
| 3b. Quick single-run | `python3 embedded_base/ocr_client.py --process-image embedded_base/test_images/sample.jpg --no-tts`                                                   | Skips the queue; authenticates, uploads once, prints OCR + Gemini response.                          |
| 4. Continuous client | `python3 embedded_base/ocr_client.py --no-tts --log-level INFO`                                                                                       | Consumes the queue, uploads images, prints raw OCR text.                                             |
| 5. Review Gemini     | `tail -f /tmp/bbocr_flask.log`                                                                                                                        | Gemini Markdown summaries logged after each OCR result.                                              |
| 6. Optional TTS      | `python3 embedded_base/ocr_client.py --log-level INFO`                                                                                                | Enables Bangla TTS; configure `TTS_BLUETOOTH_MAC`/`TTS_AUDIO_DEVICE` as needed.                      |

### 1. Start the OCR Server

```bash
cd embedded_base/bbocr_server
export $(grep -v '^#' .env.local | xargs)  # loads Gemini variables
SERVER_PORT=8080 python3 server.py > /tmp/bbocr_flask.log 2>&1 &
```

Verify it is running:

```bash
curl -s http://127.0.0.1:8080/health
```

If the port is already in use, either kill the existing process:

```bash
lsof -i tcp:8080
kill <PID>
```

or start the server on a different port (`SERVER_PORT=8081`).

### 2. Queue Test Images

Place images inside `embedded_base/test_images` (or any path accessible to the device),
then enqueue each file:

```bash
python3 embedded_base/ocr_client.py --enqueue embedded_base/test_images/sample.jpg
```

Repeat for every image you want processed.

> **Quick check:** to process a single image immediately (without using the queue), run  
> `python3 embedded_base/ocr_client.py --process-image embedded_base/test_images/sample.jpg --no-tts`

If you rely on the fallback OCR, set `BB_OCR_LANG=ben+eng` (or your preferred
language combo) before starting the server to steer pytesseract.

### 3. Run the OCR Client (without TTS)

```bash
python3 embedded_base/ocr_client.py --no-tts --log-level INFO
```

The client will:

1. Authenticate with the server (RSA challenge/response).
2. Pull queued filenames, upload them via REST, and print the OCR text.
3. Receive Gemini Markdown refinements (logged by the server and included in the JSON response).

Increase verbosity with `--log-level DEBUG` if you want to inspect full payloads.
Press `Ctrl+C` to stop once the queue is empty.

### 4. Inspect Gemini Output

Tail the server log to review Gemini summaries:

```bash
tail -f /tmp/bbocr_flask.log
```

Look for lines mentioning Gemini. The server now sends the Bangla HTML output to
Gemini and logs the returned Markdown after spelling/grammar corrections.

### 5. (Optional) Test TTS

To pipe recognised text into audio:

```bash
python3 embedded_base/ocr_client.py --log-level INFO
```

Ensure `tts_pipeline.py` can find `espeak-ng` (Bangla voice `bn` is used by default); set Bluetooth variables if you want audio on a headset:

```bash
export TTS_BLUETOOTH_MAC="AA:BB:CC:DD:EE:FF"
export TTS_AUDIO_DEVICE="bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"
```

---

## Troubleshooting Tips

| Issue                           | Resolution                                                                                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `403 Forbidden` on client       | Confirm the client uses HTTP REST (`ocr_client.py`) and the server is running.                                                                                  |
| `Signature verification failed` | Ensure both server and client use the updated `cryptography` version and that the device public key is registered (see `bbocr_server/authorized_devices.json`). |
| Gemini errors in log            | Check that `GEMINI_AI_API_KEY`/`GEMINI_AI_MODEL` are set and valid; inspect `/tmp/bbocr_flask.log` for HTTP codes from the Gemini API.                          |
| Bengali OCR accuracy is poor    | Verify `bnocr.onnx` and `best.pt` are present; without them the server uses the pytesseract fallback (requires `tesseract-ocr-ben`).                            |
| Bluetooth/TTS silent            | Confirm `espeak-ng` is installed, the headset is paired, and ALSA device name is correct.                                                                       |

---

## Next Steps

- Integrate the queue with your image capture pipeline (`take_image.py`).
- Expand error handling or logging as needed for production.
- When satisfied with OCR/Gemini behavior, enable the TTS pipeline for live audio feedback.

This README focuses solely on the `embedded_base` workflow; see other project folders for additional integrations or legacy clients.
