"""Lightweight helpers for running the Bangla OCR pipeline.

The original `pipeline.py` module contains an extensive OCR workflow with
multiple external dependencies. For the embedded server we expose a small
surface area that returns HTML for a single image while gracefully falling
back to a simple pytesseract-based implementation when the heavyweight stack
is unavailable.
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path
from typing import Optional

from PIL import Image
import pytesseract

try:  # pragma: no cover - heavy dependencies may be missing
    from . import pipeline as full_pipeline  # type: ignore
    HAS_FULL_PIPELINE = getattr(full_pipeline, "HAS_FULL_PIPELINE", False)
except Exception:  # pragma: no cover
    full_pipeline = None
    HAS_FULL_PIPELINE = False

LOGGER = logging.getLogger("bbocr_server.pipeline")


def _escape_and_wrap(text: str) -> str:
    escaped = html.escape(text.strip())
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    escaped = escaped.replace("\n\n", "\n")
    body = "<br/>".join(line for line in escaped.split("\n") if line)
    return f"<html><body><p>{body}</p></body></html>"


def _fallback_html(image_path: Path, lang: str) -> str:
    image = Image.open(image_path)
    text = pytesseract.image_to_string(image, lang=lang)
    return _escape_and_wrap(text or "")


def render_image_html(image_path: Path, lang: Optional[str] = None) -> str:
    """Return OCR output rendered as HTML.

    Parameters
    ----------
    image_path: Path
        Absolute path to the input image.
    lang: Optional[str]
        Language hint for pytesseract fallback. Defaults to "ben+eng".
    """

    image_path = image_path.expanduser().resolve()
    hint_lang = lang or os.getenv("BB_OCR_LANG", "ben+eng")

    if full_pipeline and getattr(full_pipeline, "HAS_FULL_PIPELINE", False):  # pragma: no cover
        try:
            if hasattr(full_pipeline, "render_image_html"):
                return full_pipeline.render_image_html(str(image_path))
        except Exception as exc:
            LOGGER.error("Full pipeline failed; falling back to pytesseract: %s", exc)

    return _fallback_html(image_path, hint_lang)
