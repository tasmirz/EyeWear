# `bbocr_server/modules/__init__ .py` Reference

## Overview

The package initialiser currently contains no executable code. Its presence marks `bbocr_server/modules/` as a Python package so that other modules (e.g., `pipeline.py`, `pipeline_utils.py`) can perform relative imports such as `from .modules import Recognizer`.

> **Note:** The filename includes an unintended space before `.py` (`__init__ .py`). Python still treats it as a valid module name when referenced with the exact path, but it is unusual. Consider renaming to the canonical `__init__.py` for portability.

## Role in the Project

- Enables package-relative imports for recognisers, detectors, and utilities.
- Serves as the anchor point when distributing the OCR modules as a package.

## Future Improvements

| Action                  | Benefit                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Rename to `__init__.py` | Avoid confusion on case-sensitive filesystems and improve tooling compatibility.                                     |
| Expose exports          | Re-export key classes (e.g., `from .modules import Recognizer`) so consumers can import from `bbocr_server.modules`. |
| Package metadata        | Add `__all__` or version constants if the module is deployed independently.                                          |
