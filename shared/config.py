"""
Shared configuration for the textbook-to-note converter scripts.

Env-driven, with sensible defaults relative to the repo so it runs out of the
box with no configuration: drop PDFs/EPUBs in ./books, run a script, get
markdown in ./output.

Env overrides:
    BOOKS_DIR           -> source PDFs/EPUBs (default: ./books)
    OUTPUT_DIR          -> markdown output root (default: ./output)
    SURYA_VENV_PY       -> path to a Surya OCR venv's Python interpreter (python.exe on Windows, bin/python on macOS/Linux) (optional)
    SURYA_ADAPTER       -> path to a Surya adapter script (optional)
    INDEXER_SCRIPT      -> path to an external semantic indexer script (optional)
    VAULT_SEARCH_DIR    -> cache/index dir for an external indexer (optional)
    FIGURE_REGISTRY_SCRIPT -> path to a figure-registry generator script (optional)

The OCR/indexer hooks are all optional integrations: if unset (or the path
doesn't exist), the scripts skip that step rather than failing.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BOOKS_DIR = Path(os.environ.get("BOOKS_DIR") or (REPO_ROOT / "books"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR") or (REPO_ROOT / "output"))

# Optional OCR engine (only used as a fallback for scanned/garbled PDFs).
SURYA_VENV_PY = Path(os.environ["SURYA_VENV_PY"]) if os.environ.get("SURYA_VENV_PY") else None
SURYA_ADAPTER = Path(os.environ["SURYA_ADAPTER"]) if os.environ.get("SURYA_ADAPTER") else None

# Optional external semantic-search indexer integration (not part of this repo).
INDEXER_SCRIPT = Path(os.environ["INDEXER_SCRIPT"]) if os.environ.get("INDEXER_SCRIPT") else None
VAULT_SEARCH_DIR = Path(os.environ["VAULT_SEARCH_DIR"]) if os.environ.get("VAULT_SEARCH_DIR") else None
FIGURE_REGISTRY_SCRIPT = Path(os.environ["FIGURE_REGISTRY_SCRIPT"]) if os.environ.get("FIGURE_REGISTRY_SCRIPT") else None

# Folder names to skip entirely during recursive batch conversion (comma-separated).
SKIP_FOLDERS = set(f for f in os.environ.get("CONVERTER_SKIP_FOLDERS", "").split(",") if f)

if __name__ == "__main__":
    for name in ("REPO_ROOT", "BOOKS_DIR", "OUTPUT_DIR", "SURYA_VENV_PY", "SURYA_ADAPTER",
                 "INDEXER_SCRIPT", "VAULT_SEARCH_DIR", "FIGURE_REGISTRY_SCRIPT", "SKIP_FOLDERS"):
        print(f"{name} = {globals()[name]}")
