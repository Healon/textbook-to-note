#!/usr/bin/env python
"""surya_adapter.py — reference OCR adapter for the Surya rung of the ladder.

Known-compatible version: **surya-ocr 0.22.1**.

`converter/convert.py` never imports an OCR engine. It shells out to whatever
`SURYA_ADAPTER` points at, running under `SURYA_VENV_PY`, so the engine's heavyweight
and frequently-conflicting dependency stack stays in its own venv. This file is the
reference implementation of that boundary; anything obeying the contract below can
replace it (PaddleOCR-VL, a cloud OCR, a stub for tests).

## The contract

    <SURYA_VENV_PY> <this file> p0001.png p0002.png ...     # <= batch_size (20) images

* **stdout carries JSON Lines and nothing else** — one object per input image:

      {"fixture": "p0011.png", "blocks": [{"text": "...", "bbox": [x0, y0, x1, y1]}]}

* `fixture` — the image filename as given. The caller parses the page index out of
  the `pNNNN` stem, so the name must survive round-trip unchanged.
* `blocks[].text` — plain text. Empty/whitespace blocks are dropped by the caller.
* `blocks[].bbox` — `[x0, y0, x1, y1]` in **original image pixels**, top-left origin.
  Required, not decorative: the caller sorts blocks by `(y0, x0)` to rebuild reading
  order. An adapter that omits bbox silently degrades two-column textbook pages into
  interleaved columns — the exact failure the fitz path has a dedicated column sort to
  avoid.
* One line per input image, **in input order**, even when a page yields no blocks — a
  missing line is how the caller detects a dropped page, so a blank page must be
  reported as `"blocks": []` rather than by staying silent.
* All logging goes to **stderr**. Any exception exits non-zero and the caller fails the
  whole batch.

## Why the Python API rather than the `surya_ocr` CLI

Both exist in 0.22. The CLI (`surya.scripts.ocr_text`) loads its own images from a path,
renders its own DPI, and writes `results.json` into a result directory — three pieces of
behaviour we would have to work around, since the caller has already rendered exact
per-page PNGs and wants stdout. The CLI is also a thin wrapper over
`RecognitionPredictor(SuryaInferenceManager())(images, full_page=True)`, which is what
this file calls, so using the API is not reaching past a stable interface into an
unstable one — it is calling the same one the CLI calls, minus the file plumbing.

## Why a 0.17-era adapter breaks on 0.22

Surya >= 0.20 is a ground-up rework: a single ~650M-parameter VLM now does OCR, layout
and table structure together, served behind an OpenAI-compatible inference server
(vllm, or llama.cpp for CPU/Metal). The `surya.ocr` module is gone and
`FoundationPredictor` was replaced by `SuryaInferenceManager`, so an old adapter dies at
import with `No module named 'surya.ocr'`. Output shape changed too: blocks now carry
`html` (math as `<math>`, tables as `<table>`) instead of flat `text`, which is why this
file has an HTML-to-text stage at all.

## Server lifecycle and the memory guard

`SuryaInferenceManager()` attaches to the server at `SURYA_INFERENCE_URL` if that is set,
and otherwise spawns one per process. The caller invokes this adapter once per 20 pages,
so **batch users should start one persistent server and export `SURYA_INFERENCE_URL`** —
otherwise every 20 pages pays a full model load.

If you let it spawn, cap the memory first. llama.cpp's KV cache is sized
`max(16384, SURYA_INFERENCE_PARALLEL * SURYA_INFERENCE_CTX_PER_SLOT)`, and both defaults
are generous (8 slots x 12288 = 98304 tokens). The weights are only ~1.3 GB; the cache is
what produced a reported 48 GB single-page run. See `docs/surya-adapter.md` for a tested
launch command with the caps in it.

## Licensing

Surya's code is Apache-2.0; the model weights are modified OpenRAIL-M (free for personal
use and for organizations under a revenue threshold). Check the upstream terms before
using this rung commercially.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
from html.parser import HTMLParser

# Surya's README recommends feeding the VLM images no wider than ~2048 px (96-192 DPI for
# a US-letter page). convert.py renders at 300 DPI, which is ~2550 px wide, so pages are
# downscaled before inference and every bbox is scaled back to original pixels afterwards
# -- the caller's reading-order sort and any downstream page geometry must stay in the
# coordinate space of the PNG it actually rendered.
MAX_IMAGE_WIDTH = int(os.environ.get("T2N_SURYA_MAX_WIDTH", "2048"))

# Tags after which a line break is meaningful.
# `tr` is deliberately absent: a row break belongs on the CLOSING tag only, or every row
# ends up separated by a blank line instead of a single newline.
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "figcaption", "pre"}
_CELL_TAGS = {"td", "th"}
# Dropped whole: content that is markup bookkeeping rather than page text.
_DROP_TAGS = {"script", "style"}


class _HtmlToText(HTMLParser):
    """Flatten Surya 0.22 block HTML to plain text.

    Deliberately simple and stdlib-only (no bs4 in the adapter's dependency surface):
    - `<br>` and block-level tags become line breaks
    - `<table>` rows become one line per `<tr>`, cells joined by ` | ` -- readable, and
      it keeps the row-to-row separation that a naive strip destroys. It is lossy about
      colspan/rowspan, which is acceptable here because this rung feeds a text corpus:
      the structured-table path is pdfplumber/Docling, and a scanned book has no text
      layer for them to work from anyway.
    - `<math>` keeps its inner text (the LaTeX/AsciiMath body), losing only the wrapper
    - everything else contributes its text content
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._cell_open = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._cell_open = False
            return
        if tag in _CELL_TAGS:
            if self._cell_open:
                self._out.append(" | ")
            self._cell_open = True
            return
        if tag in _BLOCK_TAGS:
            self._out.append("\n")
            self._cell_open = False

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br" and not self._skip_depth:
            self._out.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._out.append("\n")
            self._cell_open = False
        elif tag in _BLOCK_TAGS or tag == "table":
            self._out.append("\n")
            self._cell_open = False

    def handle_data(self, data):
        if not self._skip_depth:
            self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(raw: str) -> str:
    """Surya block HTML -> plain text. Never raises: a block whose HTML is malformed
    degrades to its tag-stripped text rather than failing the page, because losing one
    block's markup is recoverable and losing the whole book is not."""
    if not raw:
        return ""
    try:
        p = _HtmlToText()
        p.feed(raw)
        p.close()
        out = p.text()
    except Exception:
        out = _html.unescape(re.sub(r"<[^>]*>", " ", raw))
    # Normalize whitespace without touching line structure: collapse runs of spaces/tabs,
    # trim each line, and cap blank runs at one so page text stays compact.
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[ \t ]+", " ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def scale_bbox(bbox, inv_scale: float) -> list[float]:
    """Map a bbox measured on the downscaled image back to original-image pixels.

    `inv_scale` is original_width / resized_width (1.0 when no resize happened). Rounded
    to whole pixels: the caller only uses these for a (y0, x0) sort, and sub-pixel noise
    there buys nothing while making outputs harder to diff.
    """
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [round(v * inv_scale) for v in (x0, y0, x1, y1)]


def _load_and_downscale(path: str):
    """Open an image and shrink it to MAX_IMAGE_WIDTH. Returns (image, inv_scale)."""
    from PIL import Image

    img = Image.open(path)
    img = img.convert("RGB")
    w, h = img.size
    if MAX_IMAGE_WIDTH <= 0 or w <= MAX_IMAGE_WIDTH:
        return img, 1.0
    new_w = MAX_IMAGE_WIDTH
    new_h = max(1, round(h * new_w / w))
    print(f"[surya-adapter] downscale {os.path.basename(path)} {w}x{h} -> {new_w}x{new_h}",
          file=sys.stderr)
    return img.resize((new_w, new_h), Image.LANCZOS), w / float(new_w)


def _emit(fixture: str, blocks: list[dict]) -> None:
    sys.stdout.write(json.dumps({"fixture": fixture, "blocks": blocks},
                                ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: surya_adapter.py <image> [<image> ...]", file=sys.stderr)
        return 2

    # Imported here, not at module top, so the pure helpers above stay importable (and
    # unit-testable) from an environment that has no Surya install.
    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor

    images, inv_scales, names = [], [], []
    for path in argv:
        img, inv = _load_and_downscale(path)
        images.append(img)
        inv_scales.append(inv)
        names.append(os.path.basename(path))

    url = os.environ.get("SURYA_INFERENCE_URL")
    print(f"[surya-adapter] {len(images)} page(s); server="
          f"{url or 'spawned per-process (set SURYA_INFERENCE_URL to reuse one)'}",
          file=sys.stderr)

    manager = SuryaInferenceManager()
    predictor = RecognitionPredictor(manager)
    pages = predictor(images, full_page=True)

    if len(pages) != len(images):
        # Positional zip below would silently misalign page text with page numbers, which
        # is worse than failing: the book would look converted and be wrong.
        print(f"[surya-adapter] engine returned {len(pages)} results for {len(images)} "
              f"images — refusing to guess the alignment", file=sys.stderr)
        return 1

    for name, page, inv in zip(names, pages, inv_scales):
        blocks = []
        for b in getattr(page, "blocks", None) or []:
            if getattr(b, "skipped", False):
                continue  # a label Surya deliberately does not OCR (e.g. Picture)
            text = html_to_text(getattr(b, "html", "") or "")
            if not text.strip():
                continue
            try:
                bbox = scale_bbox(b.bbox, inv)
            except Exception:
                # bbox is required for reading order; a block without one would be
                # sorted to the top-left and scramble the page. Drop it loudly instead.
                print(f"[surya-adapter] {name}: block without usable bbox dropped",
                      file=sys.stderr)
                continue
            blocks.append({"text": text, "bbox": bbox})
        _emit(name, blocks)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # non-zero exit == the caller fails the batch, by contract
        import traceback

        traceback.print_exc(file=sys.stderr)
        print(f"[surya-adapter] FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
