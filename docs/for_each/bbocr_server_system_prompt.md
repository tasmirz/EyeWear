# `bbocr_server/system_prompt.py` Reference

## Overview

This module defines the `SYSTEM_PROMPT` constant used when calling Gemini for Bangla proofreading. It provides concise guidance to the language model so that OCR output is corrected without translation.

## Content

- The prompt instructs Gemini to act as a Bangla proof-reader:
  - Correct spelling, grammar, and punctuation.
  - Preserve the original meaning and structure.
  - Avoid translating into other languages.
- Encoded as a single multiline Bengali string for clarity and reuse.

## Usage

- Imported in `bbocr_server/server.py` inside `GeminiClient.generate_markdown`.
- Passed as `systemInstruction` to the Gemini API request body.

## Extensibility

| Idea             | Considerations                                                                                                |
| ---------------- | ------------------------------------------------------------------------------------------------------------- |
| Multiple prompts | Export additional constants for different tones (formal/informal).                                            |
| Versioning       | Store prompt variants with metadata (e.g., `SYSTEM_PROMPT_V2`) to experiment with improvements.               |
| Localisation     | Provide English commentary around the Bangla text in docstrings for maintainers unfamiliar with the language. |

## Related Files

- `bbocr_server/server.py` – loads the prompt to configure Gemini requests.
- `bbocr_server/server_pipeline.py` (concept) – would also refer to this constant if completed.
