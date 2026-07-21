"""docling_tables.py — Docling as a swappable table source for convert.py's `_table_to_md()`.

NOT wired into convert.py by this change. This is a standalone module the dispatcher will
import and call; convert.py itself is untouched.

## Why a persistent worker process

Measured in `_t2n-mineru/docling-page-selection-report.md` (§B/C/G): Docling's model cold
start is ~4.2 s; warm per-page cost is ~0.2-1.3 s depending on page/table complexity. The
corpus run is 250+ books / ~126,000 pages, of which roughly 30% are table candidates. Paying
cold start per page — or even once per book — would add hours. So table extraction runs as a
**persistent worker process** inside the Docling venv, holding ONE warm `DocumentConverter`
for the entire batch, reused across many different PDFs (not just many pages of one book —
this was verified empirically, not assumed; see the module-level NOTE near
`DoclingTableWorker` and `test_docling_tables.py`'s worker-reuse test).

The parent (this module, imported into the *normal* env — no Docling/torch installed there)
talks to the worker over stdin/stdout: **one JSON request per line in, one JSON response per
line out.** The worker's real stdout file descriptor is reserved for protocol responses only;
everything else (Docling/torch/RapidOCR banners, tqdm progress) is redirected to stderr at the
OS file-descriptor level (see `_worker_main`) so a stray `print()` — Python-level or a native
extension writing straight to fd 1 — can never corrupt the JSON stream.

## Public API (parent side, normal env)

    with DoclingTableWorker() as w:                       # spawns once, reused for the batch
        tables = w.tables_for_page(pdf_path, page_no)      # -> list[DoclingTable], never raises
        for t in tables:
            flags = qc_flags(t, pdf_path)                   # list[str], flag-only, never empty->reject

`tables_for_page()` NEVER raises and NEVER blocks the caller's book conversion: on worker
unavailability, timeout, crash, or repeated failure it returns `[]` so the caller falls back
to pdfplumber, exactly as if Docling had found no tables on that page.

## Env override

Same optional-integration pattern as `SURYA_VENV_PY` in `shared/config.py` (that file is not
touched by this change, so the var is read locally here instead):

    DOCLING_VENV_PY -> path to the Docling venv's python.exe (Windows) / bin/python (POSIX).
                        If unset or the path doesn't exist, `docling_available()` is False and
                        `DoclingTableWorker` degrades to always returning `[]` — never raises,
                        never blocks a run that doesn't have the venv configured.

## QC gate

Five pure, flag-only functions (never reject, never auto-fix — same philosophy as the figure
QC gate and PLAN.md's WP3 spec):
    content_retention_check  -- §I of the report: cross-check Docling's cells against fitz's
                                 own text stream inside the same bbox. Requires a text layer;
                                 degrades cleanly (returns None, not a false flag) on scanned
                                 pages.
    ragged_row, empty_first_cell, run_together, multi_value  -- the four PLAN.md WP3 checks,
                                 ported to operate on Docling's row/col structure instead of
                                 pdfplumber's.
    single_column             -- direct `num_cols == 1`, cheaper than the old geometry
                                 heuristic since Docling already computes this.

`qc_flags(table, pdf_path)` runs all five and returns the non-None reasons; the caller (not
this module) composes them into a trace marker — `format_trace_marker()` below reproduces
PLAN.md's exact wording as a convenience, not because this module writes markdown itself.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# === Env override (mirrors SURYA_VENV_PY in shared/config.py; not added there since this
# change must not touch shared/config.py or convert.py) ===
DOCLING_VENV_PY = Path(os.environ["DOCLING_VENV_PY"]) if os.environ.get("DOCLING_VENV_PY") else None


def docling_available() -> bool:
    return bool(DOCLING_VENV_PY and DOCLING_VENV_PY.exists())


# === Wire protocol data model (parent side) ================================================

@dataclass
class DoclingTable:
    """One table, as reported by the worker. `rows` is the list[list[str]] shape
    `_table_to_md()` consumes directly (header row is `rows[0]`); `cells` is the raw per-cell
    record list (text/spans/offsets/header flags) for QC checks and any future use that needs
    more than flattened rows."""
    page_no: int
    shape: tuple[int, int]          # (num_rows, num_cols)
    bbox: dict                       # {"l","t","r","b","coord_origin"} -- PDF points, BOTTOMLEFT
    rows: list[list[str]]
    cells: list[dict]

    @classmethod
    def from_dict(cls, d: dict) -> "DoclingTable":
        return cls(
            page_no=d["page_no"],
            shape=tuple(d["shape"]),
            bbox=d["bbox"],
            rows=d["rows"],
            cells=d.get("cells", []),
        )

    def header_row_index(self) -> int | None:
        """Index of the row whose cells are ALL flagged `column_header` by TableFormer, or None
        if no such clean header row exists. Empirically (report §H) this was always row 0 on
        every table sampled, but this does not derive that from the label alone -- it checks
        the actual per-cell flags, since a future table/edition could differ."""
        if not self.cells:
            return None
        by_row: dict[int, list[dict]] = {}
        for c in self.cells:
            by_row.setdefault(c["start_row"], []).append(c)
        for row_idx in sorted(by_row):
            row_cells = by_row[row_idx]
            if row_cells and all(c.get("column_header") for c in row_cells):
                return row_idx
        return None


class _WorkerError(Exception):
    """Internal: any failure talking to the worker subprocess. Always caught inside
    DoclingTableWorker -- never escapes to the caller."""


# === Parent-side worker manager ==============================================================

class DoclingTableWorker:
    """Manages one persistent Docling worker subprocess for the whole batch.

    Design choices called out explicitly (per the dispatcher's ask to flag open judgment
    calls):
      - request_timeout default 60s: measured warm/cold latency across ~50 real conversions
        this session ranged 0.15s-4.9s (docling-page-selection-report.md §B/C/G); 60s is a
        wide safety margin so the timeout only fires on a genuine hang, not a slow-but-real
        page. Conservative on purpose -- a false timeout kills and restarts a perfectly good
        worker, paying a fresh 4s cold start.
      - max_consecutive_restarts default 3: after 3 restarts IN A ROW with no successful
        request in between, the worker disables itself for the rest of the process rather than
        thrashing (repeatedly paying cold start against something that's reliably broken --
        e.g. a bad venv install). A single successful request resets the counter.
      - single outstanding request at a time (no pipelining): matches how convert.py's
        per-page loop will actually call this (one page at a time); keeps the protocol and the
        failure model simple. If a future caller needs concurrency, wrap multiple
        DoclingTableWorker instances rather than extending this one to pipeline.
    """

    DEFAULT_REQUEST_TIMEOUT = 60.0
    DEFAULT_MAX_RESTARTS = 3

    def __init__(
        self,
        venv_py: Path | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_consecutive_restarts: int = DEFAULT_MAX_RESTARTS,
        cmd_override: list[str] | None = None,
    ):
        """`cmd_override` replaces the spawn command entirely (e.g. `[sys.executable, "-c",
        "..."]`) -- for tests only, so the worker-management logic (spawn/timeout/restart/
        disable) can be exercised without a GPU or a Docling install. Production callers
        should leave it as None."""
        self.venv_py = venv_py or DOCLING_VENV_PY
        self.request_timeout = request_timeout
        self.max_consecutive_restarts = max_consecutive_restarts
        self._cmd_override = cmd_override
        self._skip_availability_check = cmd_override is not None

        self._proc: subprocess.Popen | None = None
        self._out_queue: "queue.Queue[str | None]" = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._lock = threading.Lock()
        self._req_id = 0
        self._consecutive_restarts = 0
        self._disabled = False
        self.last_error: str | None = None

    # -- context manager --------------------------------------------------------------------
    def __enter__(self) -> "DoclingTableWorker":
        try:
            self._ensure_started()
        except _WorkerError as e:
            # Never raise out of __enter__: a batch run must be able to do
            # `with DoclingTableWorker() as w:` unconditionally and just get [] back later.
            self.last_error = str(e)
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- public API ---------------------------------------------------------------------------
    def tables_for_page(self, pdf_path: str, page_no: int) -> list[DoclingTable]:
        """Never raises. Returns [] on: worker unavailable/disabled, spawn failure, timeout,
        crash, or a malformed response -- the caller should treat [] exactly like "Docling
        found no tables on this page" and fall back to pdfplumber.

        Note on restart timing: a request that hits a dead/dying worker is NOT retried against
        a freshly-spawned replacement within this same call -- it returns [] for THIS page and
        the restart happens for the NEXT call. Deliberate: retrying in-call would double the
        worst-case latency for one unlucky page and adds a retry-storm edge case, for a benefit
        (recovering one page's tables instead of falling back to pdfplumber for it) that does
        not seem worth the complexity given deaths should be rare. Losing Docling's table for
        one page to a pdfplumber fallback is cheap; the worker itself recovers immediately
        after."""
        with self._lock:
            if self._disabled:
                return []
            if not self._skip_availability_check and not docling_available():
                self._disabled = True
                self.last_error = f"DOCLING_VENV_PY not set or missing: {DOCLING_VENV_PY}"
                return []
            # Detect a worker that died between calls (e.g. an unhandled crash after it already
            # answered the previous request) BEFORE trying to use it. This must go through the
            # same _handle_failure/restart-cap accounting as an in-flight failure below -- if
            # _ensure_started() were left to silently respawn a process it merely *notices* is
            # dead, a worker that reliably dies right after each response would be respawned
            # forever without ever tripping max_consecutive_restarts.
            if self._proc is not None and self._proc.poll() is not None:
                self._handle_failure(_WorkerError(f"worker exited (code {self._proc.poll()}) since last call"))
                if self._disabled:
                    return []
            try:
                self._ensure_started()
                self._req_id += 1
                resp = self._send_request({
                    "id": self._req_id, "cmd": "convert_page",
                    "pdf_path": str(pdf_path), "page_no": int(page_no),
                })
            except _WorkerError as e:
                self._handle_failure(e)
                return []

            if not resp.get("ok"):
                # A well-formed "this page failed" response (Docling raised inside .convert())
                # is NOT a worker death -- the worker is still alive and answered correctly.
                # Reset the restart counter (it proved it's healthy) and just report no tables.
                self._consecutive_restarts = 0
                self.last_error = resp.get("error")
                return []

            self._consecutive_restarts = 0
            try:
                return [DoclingTable.from_dict(t) for t in resp.get("tables", [])]
            except Exception as e:
                self.last_error = f"malformed table payload: {e}"
                return []

    def status(self) -> dict:
        return {
            "available": docling_available() or self._skip_availability_check,
            "running": self._proc is not None and self._proc.poll() is None,
            "disabled": self._disabled,
            "consecutive_restarts": self._consecutive_restarts,
            "last_error": self.last_error,
            "stderr_tail": list(self._stderr_tail),
        }

    def close(self) -> None:
        with self._lock:
            self._kill()

    # -- internals ------------------------------------------------------------------------------
    def _spawn_cmd(self) -> list[str]:
        if self._cmd_override is not None:
            return self._cmd_override
        return [str(self.venv_py), str(Path(__file__).resolve()), "--worker"]

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._disabled:
            raise _WorkerError("worker disabled after repeated restart failures")
        cmd = self._spawn_cmd()
        try:
            # bytes mode (no text=True) deliberately -- Windows subprocess pipes default to
            # the system codepage (e.g. cp950), not UTF-8; encode/decode explicitly everywhere
            # instead of trusting the platform default (same gotcha convert.py's Surya
            # integration already documents around its own subprocess.run call).
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as e:
            raise _WorkerError(f"failed to spawn worker ({cmd}): {e}")

        self._out_queue = queue.Queue()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _reader_loop(self) -> None:
        proc = self._proc
        try:
            for raw in proc.stdout:
                self._out_queue.put(raw.decode("utf-8", errors="replace").strip())
        except Exception:
            pass
        finally:
            self._out_queue.put(None)  # EOF/death sentinel

    def _drain_stderr(self) -> None:
        """Keep the child's stderr pipe from filling up and blocking it; keep a short rolling
        tail for diagnostics (surfaced via status())."""
        proc = self._proc
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-20]
        except Exception:
            pass

    def _send_request(self, req: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise _WorkerError("worker not running")
        try:
            self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise _WorkerError(f"write to worker failed: {e}")

        try:
            raw = self._out_queue.get(timeout=self.request_timeout)
        except queue.Empty:
            raise _WorkerError(
                f"timed out after {self.request_timeout}s waiting for response id={req['id']}"
            )
        if raw is None:
            raise _WorkerError("worker stdout closed (process died)")
        try:
            resp = json.loads(raw)
        except Exception as e:
            raise _WorkerError(f"malformed response line {raw[:200]!r}: {e}")
        if resp.get("id") != req["id"]:
            raise _WorkerError(f"response id mismatch: sent {req['id']}, got {resp.get('id')}")
        return resp

    def _handle_failure(self, err: _WorkerError) -> None:
        self.last_error = str(err)
        self._kill()
        self._consecutive_restarts += 1
        if self._consecutive_restarts > self.max_consecutive_restarts:
            self._disabled = True

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# === Worker-side entrypoint (runs INSIDE the Docling venv only) ==============================
# Everything below this point imports Docling/torch and must never be imported/executed by the
# parent process -- it is only reached via `python docling_tables.py --worker`, launched with
# DOCLING_VENV_PY as the interpreter. `import fitz` (used by content_retention_check, parent
# side) is deliberately NOT at module top-level for the same reason: the Docling venv this
# session does not have PyMuPDF installed, and this one file is imported by both sides.

def _worker_main() -> None:
    # Redirect fd 1 (stdout) to fd 2 (stderr) at the OS level -- not just `sys.stdout =
    # sys.stderr` -- because native extensions (torch/RapidOCR C++ code) can write straight to
    # file descriptor 1, bypassing Python's sys.stdout object entirely. A plain Python-level
    # redirect would not catch that and would silently corrupt the JSON protocol stream.
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr  # also cover anything checking sys.stdout by identity
    protocol_out = os.fdopen(protocol_fd, "wb", buffering=0)

    def respond(obj: dict) -> None:
        protocol_out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.document_converter import DocumentConverter, PdfFormatOption

    device_name = os.environ.get("DOCLING_DEVICE", "cuda").lower()
    device = AcceleratorDevice.CUDA if device_name == "cuda" else AcceleratorDevice.CPU
    acc = AcceleratorOptions(num_threads=8, device=device)
    po = PdfPipelineOptions()
    po.do_ocr = True
    po.do_table_structure = True
    po.table_structure_options.do_cell_matching = True
    po.table_structure_options.mode = TableFormerMode.ACCURATE
    po.accelerator_options = acc
    # ONE converter for the process lifetime -- this is the entire point of the worker.
    # Verified empirically (not assumed) that reusing it across DIFFERENT PDFs stays warm:
    # see test_docling_tables.py's cross-book reuse test and docling-page-selection-report.md
    # §B/C/G, where a single converter instance converted 19 pages across 6 different books in
    # one process with only the first call paying the ~4.2s cold-start cost.
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=po)})

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue  # no usable id to respond with; parent will time out and restart
        req_id = req.get("id")
        try:
            if req.get("cmd") != "convert_page":
                respond({"id": req_id, "ok": False, "error": f"unknown cmd {req.get('cmd')!r}"})
                continue
            pdf_path = req["pdf_path"]
            page_no = int(req["page_no"])
            t0 = time.time()
            result = converter.convert(pdf_path, page_range=(page_no, page_no))
            tables = [_table_item_to_wire(t) for t in result.document.tables]
            respond({"id": req_id, "ok": True, "tables": tables, "secs": round(time.time() - t0, 3)})
        except Exception as e:
            respond({"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"})


def _table_item_to_wire(table_item) -> dict:
    prov = table_item.prov[0]
    bbox = prov.bbox
    cells = [_cell_to_dict(c) for c in table_item.data.table_cells]
    num_rows, num_cols = table_item.data.num_rows, table_item.data.num_cols
    return {
        "page_no": prov.page_no,
        "shape": [num_rows, num_cols],
        "bbox": {"l": bbox.l, "t": bbox.t, "r": bbox.r, "b": bbox.b, "coord_origin": str(bbox.coord_origin)},
        "rows": _grid_from_cells(num_rows, num_cols, cells),
        "cells": cells,
    }


def _cell_to_dict(c) -> dict:
    """Serialize one TableCell for the wire.

    `bbox` is Optional on docling's model (`Union[BoundingBox, None]`, default None), so it is
    emitted as None when absent rather than assumed present -- content_retention_check falls
    back to whole-table-bbox clipping when no cell carries one. Measured on docling 2.113.0
    (Malanga p93/p94): populated for 32/32 and 11/11 cells.

    ==Cell bbox origin is TOPLEFT -- NOT the BOTTOMLEFT the TABLE bbox uses.== Verified the same
    way the table-bbox mapping was: A/B-ing both readings against known cell text on Malanga
    p93/p94 -- unflipped extracted exactly the cell's own text ('Test', 'Varus stress test',
    "The patient's arm is placed in 20 degrees of flexion..."), while the flipped control
    extracted the empty string for every cell, ruling out coincidence. `coord_origin` is
    therefore carried per cell and read at use time, never assumed.
    """
    b = c.bbox
    return {
        "text": c.text, "row_span": c.row_span, "col_span": c.col_span,
        "start_row": c.start_row_offset_idx, "end_row": c.end_row_offset_idx,
        "start_col": c.start_col_offset_idx, "end_col": c.end_col_offset_idx,
        "column_header": c.column_header, "row_header": c.row_header, "row_section": c.row_section,
        "bbox": None if b is None else
                {"l": b.l, "t": b.t, "r": b.r, "b": b.b, "coord_origin": str(b.coord_origin)},
    }


def _grid_from_cells(num_rows: int, num_cols: int, cells: list[dict]) -> list[list[str]]:
    """Pure adapter: raw per-cell records -> a full span-filled grid of strings, matching
    `_table_to_md()`'s expected `list[list[str]]` shape (row 0 = header).

    Reimplemented here (rather than calling docling_core's own `TableData.grid` property)
    specifically so this logic is unit-testable without a Docling install/GPU -- see
    `test_docling_tables.py`. Verified byte-identical to docling_core's own `.grid` + its own
    `export_to_markdown()` on a real nested-header table (Cyriax_p143,
    docling-page-selection-report.md §H): a cell's text is duplicated into EVERY (row,col)
    position its span covers, never left blank -- this mirrors docling_core's
    `table_data.py:130-140` construction exactly, not a design choice made here.
    """
    grid = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    for c in cells:
        for i in range(max(0, c["start_row"]), min(num_rows, c["end_row"])):
            for j in range(max(0, c["start_col"]), min(num_cols, c["end_col"])):
                grid[i][j] = c["text"]
    return grid


# === QC gate — pure functions, FLAG-ONLY (never reject, never auto-fix) ======================
# Uniform signature `(table, pdf_path=None, doc=None) -> str | None` so a caller can loop over
# QC_CHECKS uniformly; only content_retention_check uses pdf_path/doc.

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"}

def _normalize_tokens_ordered(text: str) -> list:
    text = text.casefold()
    for lig, rep in _LIGATURES.items():
        text = text.replace(lig, rep)
    text = unicodedata.normalize("NFKD", text)
    return re.findall(r"[a-z0-9]+", text)


def _normalize_tokens(text: str) -> set:
    return set(_normalize_tokens_ordered(text))


def _adjacent_token_pairs(table: DoclingTable) -> set:
    """Every (token, next_token) pair occurring WITHIN a single cell.

    Deliberately within-cell, not across the flattened row string: joining cells with a space
    would manufacture adjacencies at every cell boundary, and those spurious pairs are exactly
    what a suppression rule must not be able to fire on. Built from `cells` (one entry per real
    cell) rather than `rows`, because `rows` is the span-filled grid -- a spanning cell's text
    is duplicated into every position it covers, which would also manufacture pairs.
    """
    texts = [c.get("text") for c in table.cells] if table.cells else \
            [c for row in table.rows for c in row]
    pairs = set()
    for txt in texts:
        toks = _normalize_tokens_ordered(txt or "")
        pairs.update(zip(toks, toks[1:]))
    return pairs


def content_retention_check(
    table: DoclingTable, pdf_path: str | None = None, doc=None
) -> str | None:
    """§I of docling-page-selection-report.md.

    The TABLE bbox (`table.bbox`) is `CoordOrigin.BOTTOMLEFT` (PDF-native, y=0 at page bottom)
    — verified empirically against a real table (Zasler p470): flipping via
    `fitz.Rect(l, page_h - t, r, page_h - b)` extracted exactly the table's own text and a
    rendered crop of that rect was visually exactly the table grid; the unflipped control
    extracted an unrelated paragraph from elsewhere on the page, ruling out coincidence. If a
    future Docling version reports a different `coord_origin`, this function does NOT guess a
    flip for it — it skips the check rather than risk clipping the wrong page region.
    ==Per-CELL bboxes use the opposite origin (TOPLEFT)== and are handled separately in
    `_cell_rects`; do not assume one origin covers both.

    What the check compares against is the reconstructed CELL GRID, not the table bbox — see
    `_clip_text`. The bbox frames captions, footnotes and source legends that were never table
    content, and reporting those as "missing cell content" was the check's dominant failure
    mode (it flagged 46 of 77 tables on Malanga 2e — measured before/after in report §J).

    Degrades cleanly on scanned pages: if fitz finds 0 chars in the bbox (no text layer here,
    or at all), returns None -- "not checked", not "verified clean". Do not read a None return
    as proof the table is correct on a scanned book.

    Perf: pass an already-open `doc` (a fitz Document, e.g. the one convert_pdf() already holds
    open for the whole book) to avoid reopening/re-parsing the PDF's xref on every call — a
    large book can have hundreds of tables, so `pdf_path`-per-call would reopen the same file
    that many times. `pdf_path` remains as a fallback (opens+closes its own handle) so the
    function stays usable/testable standalone, without a GPU or a live book-conversion loop. If
    both are given, `doc` wins.

    False-positive suppression -- ONE mechanism, deliberately narrow:
      Superscript glue (validated on ACSM_p150, report §I): fitz glues a footnote or reference
      marker directly onto the preceding word with no space ("HDL-C"+"b" -> fused token "cb",
      "Bennett"+"48" -> "bennett48", "1987"+"26" -> "198726"), while Docling's cell text keeps
      the space. Suppressed when the fused token can be split into two pieces that appear
      ADJACENT, in that order, inside a single Docling cell.

      Two properties do the safety work. Splitting is tried at EVERY position, not just before
      the last character -- the old last-char-only rule handled a 1-character marker ("cb") but
      not the 2- and 3-digit reference numbers that dominate a medical textbook, mis-splitting
      "awake44" as "awake4"+"4" and leaving it flagged. And the two pieces must be adjacent
      WITHIN ONE CELL, not merely present somewhere in the table: an every-split-point rule
      resting on mere membership would suppress "100" whenever "10" and "0" both appeared
      anywhere, which is precisely the kind of broad rule that hides real content loss.
      Adjacency also matches the actual defect -- the marker is glued to the word it follows.
      Every suppressed token is still named in the returned reason string — never silently
      dropped from view.

    REMOVED 2026-07-21 -- a second suppression mechanism ("fitz drops ligature glyphs entirely,
    e.g. flexion->exion") was added here on the strength of docling-bench40.md §F7 and has been
    deleted as unfounded. fitz does NOT drop ligatures: it returns the precomposed codepoint
    (U+FB01 etc.), which `_normalize_tokens_ordered` already expands, so the case could never
    reach the suppressor -- measured over all 37 tables of the 46-page benchmark, it fired zero
    times. §F7 itself was an artifact of that benchmark's own tokenizer skipping U+FB01 and of
    `clip=` slicing words at the bbox edge; its author confirmed this and is amending the
    report. Do not re-add it without new evidence: it was a BROAD suppression resting on a false
    premise, i.e. exactly the kind of rule that hides real content loss. The real, measured
    defect runs the other way -- Docling corrupts ligatures ("specific"->"speciic") while fitz
    is correct -- which is why fitz can serve as the repair oracle (see repair_ligatures).
    """
    b = table.bbox
    if b.get("coord_origin") != "CoordOrigin.BOTTOMLEFT":
        return None

    if doc is not None:
        fitz_text = _clip_text(table, doc, b)
    elif pdf_path is not None:
        import fitz  # local import -- see module-level note; not available in the Docling venv
        try:
            d = fitz.open(pdf_path)
        except Exception:
            return None
        try:
            fitz_text = _clip_text(table, d, b)
        finally:
            try:
                d.close()
            except Exception:
                pass
    else:
        return None

    if fitz_text is None:
        return None  # out-of-range page or unreadable -- degrade silently, don't flag

    fitz_tokens = _normalize_tokens(fitz_text)
    if not fitz_tokens:
        return None  # scanned page / no text layer in this bbox -- nothing to check against

    docling_tokens = _normalize_tokens(" ".join(cell for row in table.rows for cell in row))
    missing = fitz_tokens - docling_tokens

    suppressed: dict = {}  # token -> human-readable reason

    # Footnote/reference-superscript glue -- the only suppression mechanism here; see the
    # docstring for why the former ligature-drop mechanisms were removed.
    pairs = _adjacent_token_pairs(table)
    for tok in missing:
        if len(tok) < 2:
            continue
        for i in range(1, len(tok)):
            base, tail = tok[:i], tok[i:]
            if (base, tail) in pairs:
                suppressed[tok] = (f"superscript-glue artifact, matches Docling's adjacent "
                                   f"'{base}'+'{tail}'")
                break

    real_missing = sorted(t for t in missing if t not in suppressed)
    if not real_missing:
        return None

    reason = f"content-retention: {len(real_missing)} token(s) in source not found in cells: {real_missing}"
    if suppressed:
        detail = "; ".join(f"'{t}' ({why})" for t, why in sorted(suppressed.items()))
        reason += f" (suppressed {len(suppressed)} likely artifact(s): {detail})"
    return reason


# Cell bboxes bound the glyphs tightly; a word whose centre sits a hair outside a band edge
# because of rounding should still count as inside. 1pt at textbook body size (~9-10pt) is far
# below one line's height, so this cannot pull in the NEXT line's text.
_CELL_PAD = 1.0


def _cell_rects(table: DoclingTable, page_h: float, pad: float = _CELL_PAD) -> list | None:
    """Per-cell rects as (cell_dict, fitz.Rect) in fitz-native top-left coords, or None if no
    cell carries a usable bbox (older worker output, or an origin we refuse to guess at).

    ==Cell bbox origin is TOPLEFT, unlike the TABLE bbox, which is BOTTOMLEFT== -- so cell
    rects are NOT flipped and table rects are. Not an assumption: A/B-ing both readings on
    Malanga p93/p94 returned the cell's exact text unflipped ('Test', 'Varus stress test', "The
    patient's arm is placed in 20 degrees of flexion...") and the empty string flipped, for
    every cell tested. `coord_origin` is still read per cell rather than hardcoded, and an
    origin that is neither known value makes the whole function bail to None (caller falls back
    to whole-table clipping) rather than guess a flip -- same policy the table bbox uses.
    """
    import fitz  # local import -- see module-level note; not available in the Docling venv
    out = []
    for c in table.cells:
        b = c.get("bbox")
        if not b:
            continue
        origin = b.get("coord_origin")
        if origin == "CoordOrigin.TOPLEFT":
            r = fitz.Rect(b["l"], b["t"], b["r"], b["b"])
        elif origin == "CoordOrigin.BOTTOMLEFT":
            r = fitz.Rect(b["l"], page_h - b["t"], b["r"], page_h - b["b"])
        else:
            return None
        r.normalize()
        if r.is_empty or r.is_infinite:
            continue
        out.append((c, fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)))
    return out or None


def _cell_hull_text(page, cell_rects: list) -> str:
    """Text of the words whose centre lies inside the bounding rect of every cell bbox -- the
    region the table's cells actually occupy.

    ==Deliberately the HULL, not a tighter fit.== Two tighter variants were implemented and
    measured on all 66 Docling tables of Malanga 2e, and both were rejected for going blind on
    real content loss:
      - union of the individual cell rects (word must fall inside SOME cell): 2 tables flagged,
        but a cell Docling dropped entirely contributes no rect, so the clip deletes exactly the
        region whose content went missing. Missed the p8 defect (the whole "Ulnar Collateral
        Test" row is absent from Docling's cells) -- the check went silent on a real drop.
      - per-row/per-column bands: 3 tables flagged, and still blind, because a line Docling
        dropped falls in the GAP BETWEEN two row bands. Missed p36 (Docling truncates a cell at
        "Examiner grasps patient's head under", losing "occiput and chin and applies axial
        traction force") and p46 (loses "Sensitivity: 52.6% Specificity: 82.4%" -- clinical data).
    The hull keeps those interior regions covered while still dropping what the table BBOX
    wrongly framed: the caption above the header and the footnote/source legend below the last
    row both lie outside the cells' extent. That is the whole false-positive class this fixes.

    Words are selected by CENTRE-INSIDE rather than by handing the rect to `get_text(clip=)`,
    because `clip=` slices a word that straddles the boundary and yields a fragment token --
    the artifact that produced the bogus §F7 ligature finding. A whole word is either in or out.
    """
    import fitz  # local import -- see module-level note; not available in the Docling venv
    hull = fitz.Rect(cell_rects[0][1])
    for _, r in cell_rects[1:]:
        hull |= r
    return " ".join(w[4] for w in page.get_text("words")
                    if hull.contains(((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)))


def _clip_text(table: DoclingTable, doc, b: dict) -> str | None:
    """Extract the fitz text that is actually TABLE CONTENT, from an already-open Document.
    Returns None on any failure (out-of-range page, closed/corrupt doc) so the caller degrades
    silently.

    Clips to the hull of the cell bboxes when they are available, falling back to the whole
    table bbox when they are not. The table bbox frames whatever the box happens to enclose --
    caption, footnote, source legend -- text that was never table content and correctly appears
    in no cell, which the retention check then reported as "missing". Clipping to the cells'
    own extent removes that false-positive class at the source instead of trying to explain it
    away with token-level suppression rules afterwards.
    """
    import fitz  # local import -- see module-level note; not available in the Docling venv
    try:
        if not (0 <= table.page_no - 1 < doc.page_count):
            return None
        page = doc[table.page_no - 1]
        H = page.rect.height
        cr = _cell_rects(table, H)
        if cr is None:
            rect = fitz.Rect(b["l"], H - b["t"], b["r"], H - b["b"])
            return page.get_text("text", clip=rect)
        return _cell_hull_text(page, cr)
    except Exception:
        return None


def ragged_row(table: DoclingTable, pdf_path: str | None = None, doc=None) -> str | None:
    """PLAN.md WP3 (a): any row whose non-empty cell count differs from the modal row width."""
    if len(table.rows) < 2:
        return None
    counts = [sum(1 for c in row if c and c.strip()) for row in table.rows]
    if not counts:
        return None
    mode_count, _ = Counter(counts).most_common(1)[0]
    bad = [i for i, c in enumerate(counts) if c != mode_count]
    if not bad:
        return None
    return f"ragged_row: {len(bad)} row(s) differ from modal non-empty-cell count {mode_count}: rows {bad}"


def empty_first_cell(table: DoclingTable, pdf_path: str | None = None, doc=None) -> str | None:
    """PLAN.md WP3 (b): a data row with an empty row-label cell while later cells hold values
    (catches column absorption, e.g. a row's label silently merging into the previous row's)."""
    header_idx = table.header_row_index()
    start = (header_idx + 1) if header_idx is not None else 1
    bad = []
    for i in range(start, len(table.rows)):
        row = table.rows[i]
        if not row:
            continue
        first = (row[0] or "").strip()
        if not first and any((c or "").strip() for c in row[1:]):
            bad.append(i)
    if not bad:
        return None
    return f"empty_first_cell: {len(bad)} data row(s) with empty row-label but values elsewhere: rows {bad}"


# Lowercase->uppercase with no space ("acetabulumHip"), or a letter immediately followed by an
# unambiguous bullet glyph with no space ("text•Nextitem"). Deliberately excludes a bare hyphen
# from the bullet class -- too many false positives on legitimate hyphenated words
# ("self-report", "21-24") -- so a plain "-" boundary is NOT flagged by this check; only glyphs
# that are unambiguously bullets are. Open design choice, noted per the dispatcher's ask.
_RUN_TOGETHER_RE = re.compile(r"[a-z][A-Z]|[A-Za-z][•▪‣○–—](?=[A-Za-z])")


def run_together(table: DoclingTable, pdf_path: str | None = None, doc=None) -> str | None:
    """PLAN.md WP3 (c): a cell whose text has a lowercase→uppercase or letter→dash-bullet
    boundary with no separating whitespace (catches "acetabulumHip joint")."""
    hits = []
    for ri, row in enumerate(table.rows):
        for ci, cell in enumerate(row):
            if cell and _RUN_TOGETHER_RE.search(cell):
                hits.append([ri, ci])
    if not hits:
        return None
    return f"run_together: {len(hits)} cell(s) with a no-space case/bullet boundary: {hits[:10]}"


# A "numeric token" includes simple ranges ("21-24") as ONE token, matching PLAN.md's own
# example pair ("71.6 78" and "21-24 26-100" are both given as bad-cell examples) -- a bare
# number and a number-range are both single data points; the defect is >1 of either glued
# together with whitespace, not the presence of a dash.
_NUM_TOKEN_RE = re.compile(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?%?$")


def multi_value(table: DoclingTable, pdf_path: str | None = None, doc=None) -> str | None:
    """PLAN.md WP3 (d): a numeric cell holding >1 whitespace-separated numeric token where
    sibling cells (same column) hold exactly 1 (catches "71.6 78" and "21-24 26-100").
    Requires >=2 sibling single-token cells in the column before flagging anything in it, so a
    column whose NORMAL format is multi-number (e.g. always "mean (range)") is not flagged --
    the defect is deviation from the column's own established pattern, not multi-number-ness
    itself."""
    if not table.rows:
        return None
    ncols = max((len(r) for r in table.rows), default=0)
    hits = []
    for ci in range(ncols):
        col = [(ri, row[ci]) for ri, row in enumerate(table.rows) if ci < len(row) and (row[ci] or "").strip()]
        single_rows = {ri for ri, v in col if len(v.split()) == 1 and _NUM_TOKEN_RE.match(v.strip())}
        if len(single_rows) < 2:
            continue
        for ri, v in col:
            if ri in single_rows:
                continue
            toks = v.split()
            numeric_toks = [t for t in toks if _NUM_TOKEN_RE.match(t.strip(",;"))]
            if len(numeric_toks) > 1:
                hits.append([ri, ci, v])
    if not hits:
        return None
    return f"multi_value: {len(hits)} cell(s) hold >1 numeric token where sibling cells hold 1: {hits[:10]}"


def single_column(table: DoclingTable, pdf_path: str | None = None, doc=None) -> str | None:
    """Direct `num_cols == 1` -- a genuine improvement over the old pdfplumber-era geometry
    heuristic, since Docling already computes this rather than inferring it after the fact."""
    if table.shape[1] == 1:
        return f"single_column: table collapsed to 1 column ({table.shape[0]} rows)"
    return None


QC_CHECKS = [content_retention_check, ragged_row, empty_first_cell, run_together, multi_value, single_column]


def qc_flags(table: DoclingTable, pdf_path: str | None = None, doc=None) -> list[str]:
    """Run every QC check and return the non-None reasons, in QC_CHECKS order. A check that
    raises is reported as its own flag rather than propagating -- one buggy check must not take
    down the whole page's QC pass. Pass `doc` (an already-open fitz Document, e.g. the one
    convert_pdf() already holds for the whole book) to avoid content_retention_check reopening
    the PDF per table -- see its docstring. `pdf_path` remains a fallback for standalone use."""
    flags = []
    for fn in QC_CHECKS:
        try:
            r = fn(table, pdf_path, doc)
        except Exception as e:
            r = f"{fn.__name__}: check raised {type(e).__name__}: {e}"
        if r:
            flags.append(r)
    return flags


def format_trace_marker(page_no: int, flags: list[str]) -> str:
    """Convenience formatter reproducing PLAN.md's WP3 trace-marker convention verbatim:
        <!-- ⚠️ table structure unverified: <reason> — verify against PDF page N before citing -->
    Provided so an integration can reuse the exact format instead of re-deriving it; this
    module does not write markdown itself and this function is not called by anything here."""
    reason = "; ".join(flags)
    return f"<!-- ⚠️ table structure unverified: {reason} — verify against PDF page {page_no} before citing -->"


# === Ligature repair (docling-bench40.md §F5) ==============================================
# NOTE ON _LIG_EXPANSIONS vs the deleted _LIGATURE_PAIRS: same seven strings, opposite purpose.
# The deleted constant fed a SUPPRESSOR that silently excused missing tokens on a premise that
# turned out to be false. These feed a REPAIR that is gated by an oracle -- a rewrite happens
# only when the current spelling is absent from the source page AND the proposed one is present
# there. Do not merge or "simplify" the two ideas back together.


def ligature_repair_enabled() -> bool:
    """Kill-switch. Default ON: every repair is gated on the source PDF's own text layer
    (see `repair_ligatures`), so it cannot invent text, and it is a strict no-op when
    Docling's token already matches the source."""
    return os.environ.get("T2N_LIGATURE_REPAIR", "1").strip().lower() not in ("0", "false", "no", "off")


# Ligature expansions involved in the two measured Docling defects. BOTH originate in the
# source PDF's precomposed ligature codepoints (U+FB00-FB06); fitz decodes those correctly
# (either as the codepoint, which `_normalize_tokens_ordered` expands, or already as ASCII),
# Docling does not:
#   F5a "spaced"  — Beneﬁts -> "Bene fi ts"  (expanded, spurious spaces around the expansion)
#   F5b "dropped" — speciﬁc -> "speciic"     (leading 'f' of the expansion deleted; reads as a
#                                             plain typo, which is why it is the dangerous one)
# Measured on ACSM GETP 12e (F5b) and AAP Pediatric CPG 18e (F5a); see docling-bench40.md §F5.
# Longest-first so "ffi"/"ffl" win over "ff"/"fi" when both could apply.
_LIG_EXPANSIONS = ("ffi", "ffl", "ff", "fi", "fl", "ft", "st")

# F5a: word, whitespace, a bare ligature expansion, whitespace, word.
_SPACED_LIG_RE = re.compile(
    r"(?<![^\W\d_])([^\W\d_]+)\s+(" + "|".join(_LIG_EXPANSIONS) + r")\s+([^\W\d_]+)(?![^\W\d_])"
)


def _page_text(doc, page_no: int) -> str | None:
    try:
        if not (0 <= page_no - 1 < doc.page_count):
            return None
        return doc[page_no - 1].get_text("text")
    except Exception:
        return None


def _source_tokens(table, pdf_path=None, doc=None) -> set | None:
    """Casefolded, ligature-expanded token set of the source page's own text.

    WHOLE PAGE, deliberately NOT the table bbox. `content_retention_check` clips to the bbox
    because it measures *retention* (did this table keep its own content). This function asks a
    different question — "is this spelling attested in the source document?" — and for that the
    bbox is actively harmful: fitz's `clip=` keeps only glyphs whose box falls inside the rect,
    so any word straddling the table border is returned SLICED ("THRESHOLD"->"THRES",
    "NUMBER"->"UMBER", "109"->"09"; measured on Zasler p290). A sliced oracle both blocks
    legitimate repairs and manufactures false "not in source" suspects.

    Page-level cannot cause a wrong repair: a repair candidate must still be a real word
    attested on that page, and the only edit ever attempted is reinserting a ligature's dropped
    leading character. Returns None when there is no text layer at all (scanned page) —
    callers must treat None as "no oracle", never as "clean"."""
    if doc is not None:
        text = _page_text(doc, table.page_no)
    elif pdf_path is not None:
        import fitz
        try:
            d = fitz.open(pdf_path)
        except Exception:
            return None
        try:
            text = _page_text(d, table.page_no)
        finally:
            try:
                d.close()
            except Exception:
                pass
    else:
        return None
    if text is None:
        return None
    toks = set(_normalize_tokens_ordered(text))
    return toks or None


def _attested(tok: str, src: set) -> bool:
    """Is `tok` present in the source token set, compared through the SAME normalization
    pipeline that built that set? Comparing a raw casefolded token against NFKD-normalized
    source tokens is an asymmetry bug: "Sjögren" casefolds to "sjögren" but the source side
    decomposes and splits it into {"sjo","gren"}, so a correct token looks unattested."""
    parts = _normalize_tokens_ordered(tok)
    return bool(parts) and all(p in src for p in parts)


def _repair_token_f5b(tok: str, src: set) -> str | None:
    """F5b: Docling deleted the leading 'f' of a ligature expansion inside `tok`.
    Reinsert it and accept ONLY if the result exists in the source text.

    `corrupt(exp) == exp[1:]` uniformly for the measured cases (fi->i, fl->l, ff->f), so the
    search is over the expansion's own tail, not arbitrary edits."""
    low = tok.casefold()
    if _attested(tok, src):
        return None                      # Docling already agrees with the source -> never touch
    for exp in _LIG_EXPANSIONS:
        tail = exp[1:]
        start = 0
        while True:
            i = low.find(tail, start)
            if i < 0:
                break
            cand = tok[:i] + exp[0] + tok[i:]        # reinsert the dropped leading char
            if _attested(cand, src) and len(cand) > len(tok):
                return cand
            start = i + 1
    return None


def _ligature_shaped(tok: str) -> bool:
    """Could `tok` plausibly be an F5b corruption at all — i.e. does it contain a ligature
    expansion's tail at a position where reinserting the dropped leading char would change it?
    Narrows the flag-only suspect set to genuinely ligature-shaped tokens instead of every
    Docling token the source page happens not to attest (which is dominated by unrelated
    artifacts: superscript-glue, small-caps mangling)."""
    low = tok.casefold()
    return any(exp[1:] in low for exp in _LIG_EXPANSIONS)


def repair_ligatures(table, pdf_path=None, doc=None):
    """Repair the F5a/F5b ligature defects in `table.rows`, using the source PDF's own text
    layer inside the table bbox as the oracle.

    Returns `(rows, repairs, suspects)`:
      rows     — a NEW list[list[str]]; `table` is not mutated.
      repairs  — list of dicts {row, col, before, after, variant} — one per changed token.
      suspects — alphabetic tokens (len>=5) Docling emitted that do NOT appear in the source
                 text and that no ligature reinsertion could resolve. FLAG-ONLY: these are the
                 "no fitz counterpart -> do not guess" cases required by the spec.

    THE GUARD (this is the whole safety argument, and it is enforced here, not merely
    intended): a token is only ever rewritten when (a) its current form is ABSENT from the
    source token set and (b) the proposed form is PRESENT in it. So:
      - a book where Docling is already correct is a strict no-op (condition (a) fails);
      - no repair can introduce a string the source page does not contain (condition (b));
      - repairs only ever ADD characters, never shorten or resequence.
    Disabled (or no text layer) => returns the rows unchanged with empty repairs.
    """
    rows = [list(r) for r in table.rows]
    if not ligature_repair_enabled():
        return rows, [], []
    src = _source_tokens(table, pdf_path, doc)
    if src is None:
        return rows, [], []              # scanned page / no oracle -> never guess

    repairs, suspects = [], []
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            if not cell:
                continue
            original = cell

            # --- F5a: join "Bene fi ts" -> "Benefits", oracle-gated ---
            def _join(m):
                cand = m.group(1) + m.group(2) + m.group(3)
                if _attested(cand, src):
                    repairs.append({"row": ri, "col": ci, "before": m.group(0),
                                    "after": cand, "variant": "F5a-spaced"})
                    return cand
                return m.group(0)
            cell = _SPACED_LIG_RE.sub(_join, cell)

            # --- F5b: "speciic" -> "specific", oracle-gated ---
            out = []
            for piece in re.split(r"(\W+)", cell):
                if piece and piece[0].isalpha() and len(piece) >= 4:
                    fixed = _repair_token_f5b(piece, src)
                    if fixed:
                        repairs.append({"row": ri, "col": ci, "before": piece,
                                        "after": fixed, "variant": "F5b-dropped"})
                        piece = fixed
                    elif (piece.isalpha() and len(piece) >= 5
                          and not _attested(piece, src)
                          and _ligature_shaped(piece)):
                        suspects.append({"row": ri, "col": ci, "token": piece})
                out.append(piece)
            cell = "".join(out)

            if cell != original:
                rows[ri][ci] = cell
    return rows, repairs, suspects


def format_repair_marker(page_no: int, repairs: list) -> str:
    """House-style trace marker, mirroring `format_trace_marker()`. Emitted only when at least
    one repair was made, so an untouched cell is distinguishable from a repaired one."""
    if not repairs:
        return ""
    from collections import Counter
    pairs = Counter(f"{r['before']}->{r['after']}" for r in repairs)
    detail = ", ".join(f"{k}" + (f" x{v}" if v > 1 else "") for k, v in pairs.most_common(6))
    more = "" if len(pairs) <= 6 else f", +{len(pairs) - 6} more"
    return (f"<!-- 🔤 ligature repaired: {len(repairs)} token(s) in {len({(r['row'], r['col']) for r in repairs})} "
            f"cell(s) ({detail}{more}) — verified against the PDF text layer, page {page_no} -->")


def ligature_suspect_check(table, pdf_path=None, doc=None) -> str | None:
    """QC check (flag-only, QC_CHECKS-compatible signature): Docling emitted alphabetic tokens
    the source page does not contain and that ligature reinsertion could not explain."""
    _, _, suspects = repair_ligatures(table, pdf_path, doc)
    if not suspects:
        return None
    toks = sorted({s["token"] for s in suspects})
    return (f"ligature_suspect: {len(toks)} token(s) absent from the source text layer and not "
            f"explained by a ligature repair: {toks[:10]}")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker_main()
    else:
        print(__doc__)
