# Embedded OCR Pipeline (Raspberry Pi)

This folder hosts a minimal end-to-end pipeline for capturing images, sending
them to an OCR service, routing the recognised text through Gemini for polishing, and (optionally) speaking the refined
output via a local text-to-speech (TTS) handler. It is designed for resource-constrained devices such
as Raspberry Pi Zero 2.

---

## Architecture Overview

- **`bbocr_server/server.py`**  
  Flask API handling RSA challenge–response authentication, receiving image uploads,
  rendering Bangla OCR to HTML (via `pipeline_utils.py` with the full pipeline when
  available, otherwise a pytesseract fallback), and requesting a Gemini summary.

- **`ocr_client.py`**  
  Command-line client that watches an image queue, authenticates with the server,
  uploads images, and speaks Gemini-refined text when TTS is enabled.

- **`tts_pipeline.py`**  
  Lightweight TTS helper using Meta's MMS-TTS (`facebook/mms-tts-ben` by default); ingests Gemini-refined text and emits MP3 files (one per utterance).

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
   sudo apt install python3 python3-pip ffmpeg \
       libblas-dev liblapack-dev libatlas-base-dev tesseract-ocr tesseract-ocr-ben
   ```

   _Install `ffmpeg` for MP3 conversion. For TTS, install PyTorch/Transformers:_  
   `pip3 install torch --index-url https://download.pytorch.org/whl/cpu`  
   `pip3 install transformers accelerate`

2. **MMS-TTS Configuration**

   `tts_pipeline.py` loads `facebook/mms-tts-ben` from Hugging Face. Set optional overrides before launching the client:

   ```bash
   export MMS_TTS_MODEL_ID="facebook/mms-tts-ben"   # change to another MMS voice if desired
   export MMS_TTS_DEVICE="cpu"                      # or 'cuda'
   export MMS_TTS_DTYPE="float32"                   # fp16/bf16 supported on compatible hardware
   export MMS_TTS_SPEAKER="speaker_id_or_name"      # optional for multi-speaker checkpoints
   export TTS_OUTPUT_DIR="/path/to/store/mp3s"      # optional output directory (defaults to cwd)
   ```

3. **(Recommended) Isolated Virtual Environment**

   Because this project depends on specific versions of `torch`, `transformers`, and other packages that may conflict with globally installed tools, create a dedicated virtual environment before installing requirements:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate         # on Windows use: .venv\Scripts\activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

   When you are done working on the project, run `deactivate` to leave the environment.

4. **Python Dependencies**

   ```bash
   pip3 install flask flask-cors requests pyjwt cryptography pillow pytesseract
   pip3 install python-multipart  # only needed if the server runs under Uvicorn/FastAPI
   ```

5. **Environment Variables**
   Copy `.env.local` (or create a new `.env`) under `embedded_base/bbocr_server`
   providing at least:

   ```*
   GEMINI_API_KEY="your_google_genai_key"
   GEMINI_MODEL="gemini-2.0-flash"  # or preferred model
   GEMINI_RETRIES=3                 # optional: retry Gemini calls on 5xx
   GEMINI_RETRY_DELAY=1.5           # optional: seconds between retries
   ```

   The server automatically loads `.env` and `.env.local` on startup.

---

## Usage Guide

| Step                 | Command                                                                                                                                               | Notes                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1. Start server      | `cd embedded_base/bbocr_server`<br>`export $(grep -v '^#' .env.local \| xargs)`<br>`SERVER_PORT=8080 python3 server.py > /tmp/bbocr_flask.log 2>&1 &` | Launches the Flask OCR service (loads Bangla models if available). Use another port if 8080 is busy.                       |
| 2. Health check      | `curl -s http://127.0.0.1:8080/health`                                                                                                                | Confirms the server is listening.                                                                                          |
| 3a. Queue image      | `python3 embedded_base/ocr_client.py --enqueue embedded_base/test_images/sample.jpg`                                                                  | Adds an image to the filesystem/POSIX queue. Repeat per image.                                                             |
| 3b. Quick single-run | `python3 embedded_base/ocr_client.py --process-image embedded_base/test_images/sample.jpg --no-tts`                                                   | Skips the queue; authenticates, uploads once, prints OCR + Gemini response.                                                |
| 4. Continuous client | `python3 embedded_base/ocr_client.py --no-tts --log-level INFO`                                                                                       | Consumes the queue, uploads images, prints raw OCR text.                                                                   |
| 5. Review Gemini     | `tail -f /tmp/bbocr_flask.log`                                                                                                                        | Gemini Markdown summaries logged after each OCR result.                                                                    |
| 6. Optional TTS      | `python3 embedded_base/ocr_client.py --log-level INFO`                                                                                                | Enables Bangla TTS with Gemini-refined text; ensure MMS env vars (`MMS_TTS_MODEL_ID`, etc.) are set if you need overrides. |

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

If the requested port is already in use, the server logs a warning and falls back to the next available port (set `SERVER_PORT_AUTO=0` to disable this). To free the original port manually, either kill the existing process:

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
3. Receive Gemini Markdown + `refined_text` (logged by the server and included in the JSON response).

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

To generate MP3s for Gemini-refined Bangla text (skips audio if Gemini is disabled or returns nothing):

```bash
python3 embedded_base/ocr_client.py --log-level INFO
```

Ensure `tts_pipeline.py` can load the MMS-TTS checkpoint (`facebook/mms-tts-ben` by default) and that PyTorch/Transformers are installed. MP3 files are stored in `TTS_OUTPUT_DIR` (defaults to the client working directory).

---

## Troubleshooting Tips

| Issue                           | Resolution                                                                                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `403 Forbidden` on client       | Confirm the client uses HTTP REST (`ocr_client.py`) and the server is running.                                                                                  |
| `Signature verification failed` | Ensure both server and client use the updated `cryptography` version and that the device public key is registered (see `bbocr_server/authorized_devices.json`). |
| Gemini errors in log            | Check that `GEMINI_API_KEY`/`GEMINI_MODEL` are set and valid; inspect `/tmp/bbocr_flask.log` for HTTP codes from the Gemini API.                                |
| Bengali OCR accuracy is poor    | Verify `bnocr.onnx` and `best.pt` are present; without them the server uses the pytesseract fallback (requires `tesseract-ocr-ben`).                            |
| Bluetooth/TTS silent            | Confirm the MMS model downloads succeed (check Hugging Face cache), `ffmpeg` is installed, and Gemini is returning `refined_text` in the OCR response.          |

---

## Next Steps

- Integrate the queue with your image capture pipeline (`take_image.py`).
- Expand error handling or logging as needed for production.
- When satisfied with OCR/Gemini behavior, enable the TTS pipeline for live audio feedback.

This README focuses solely on the `embedded_base` workflow; see other project folders for additional integrations or legacy clients.
