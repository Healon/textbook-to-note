"""test_docling_tables.py — regression guard for docling_tables.py.

Same shape as test_table_fixes.py / test_table_merge.py: check()/skip() results, a printed
report, exit 0 (all pass/skip) / exit 1 (any FAIL). Runs WITHOUT a GPU and WITHOUT a Docling
install: the worker-management tests spawn a tiny fake worker (plain `sys.executable -c
"..."`) instead of the real Docling subprocess, via `DoclingTableWorker(cmd_override=...)`;
the QC-check tests build `DoclingTable` fixtures directly from plain data; the adapter test
calls `_grid_from_cells` directly with synthetic cell dicts. `content_retention_check`'s
fixtures use tiny real PDFs built with fitz (available in this env, same as convert.py's own
tests) -- no Docling needed, since the check's whole point is reading the SOURCE pdf, not
Docling's.

  python converter/test_docling_tables.py   -> exit 0 all pass/skip · exit 1 any FAIL
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import docling_tables as dt  # noqa: E402

results = []  # (name, "PASS"|"FAIL"|"SKIP", detail)


def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL", detail))


def skip(name, detail=""):
    results.append((name, "SKIP", detail))


tmp = Path(tempfile.mkdtemp(prefix="t2n_docling_tables_"))

# ============================================================================================
# Adapter: span-filling + header-row placement (pure, no Docling/GPU needed)
# ============================================================================================

# Mirrors the real Cyriax_p143 nested-header case from docling-page-selection-report.md §H:
# a header cell spanning 2 columns (col_span=2, start_col=1,end_col=3).
_span_cells = [
    {"text": "History", "start_row": 0, "end_row": 1, "start_col": 0, "end_col": 1},
    {"text": "NerveRootSigns", "start_row": 0, "end_row": 1, "start_col": 1, "end_col": 3},
    {"text": "Cord Signs", "start_row": 0, "end_row": 1, "start_col": 3, "end_col": 4},
    {"text": "Always", "start_row": 1, "end_row": 2, "start_col": 0, "end_col": 1},
    {"text": "Sometimes1", "start_row": 1, "end_row": 2, "start_col": 1, "end_col": 2},
    {"text": "Sometimes2", "start_row": 1, "end_row": 2, "start_col": 2, "end_col": 3},
    {"text": "Rare", "start_row": 1, "end_row": 2, "start_col": 3, "end_col": 4},
]
_grid = dt._grid_from_cells(2, 4, _span_cells)
check("adapter: span cell duplicated into every covered grid position",
      _grid[0] == ["History", "NerveRootSigns", "NerveRootSigns", "Cord Signs"], f"{_grid[0]}")
check("adapter: unspanned data row unaffected",
      _grid[1] == ["Always", "Sometimes1", "Sometimes2", "Rare"], f"{_grid[1]}")

# empty/no-coverage cells stay ""
_grid_gap = dt._grid_from_cells(2, 2, [{"text": "only", "start_row": 0, "end_row": 1, "start_col": 0, "end_col": 1}])
check("adapter: uncovered grid position stays empty string",
      _grid_gap == [["only", ""], ["", ""]], f"{_grid_gap}")

# header_row_index: clean header row 0
_hdr_cells = [
    {"start_row": 0, "start_col": 0, "column_header": True},
    {"start_row": 0, "start_col": 1, "column_header": True},
    {"start_row": 1, "start_col": 0, "column_header": False},
    {"start_row": 1, "start_col": 1, "column_header": False},
]
t_hdr = dt.DoclingTable(page_no=1, shape=(2, 2), bbox={}, rows=[["a", "b"], ["c", "d"]], cells=_hdr_cells)
check("header_row_index: finds row 0 when all its cells are column_header", t_hdr.header_row_index() == 0)

_no_hdr_cells = [
    {"start_row": 0, "start_col": 0, "column_header": True},
    {"start_row": 0, "start_col": 1, "column_header": False},  # mixed -> not a clean header row
]
t_no_hdr = dt.DoclingTable(page_no=1, shape=(1, 2), bbox={}, rows=[["a", "b"]], cells=_no_hdr_cells)
check("header_row_index: None when no row is uniformly column_header", t_no_hdr.header_row_index() is None)

# ============================================================================================
# QC checks: firing and not-firing (pure functions, synthetic DoclingTable fixtures)
# ============================================================================================

def mk(rows, shape=None, cells=None, page_no=1, bbox=None):
    shape = shape or (len(rows), max((len(r) for r in rows), default=0))
    return dt.DoclingTable(page_no=page_no, shape=shape, bbox=bbox or {}, rows=rows, cells=cells or [])


# -- ragged_row --
t = mk([["A", "B", "C"], ["1", "2", "3"], ["4", "", ""]])
check("ragged_row: fires on a row with fewer non-empty cells than the modal width",
      dt.ragged_row(t) is not None and "rows [2]" in dt.ragged_row(t))
t = mk([["A", "B"], ["1", "2"], ["3", "4"]])
check("ragged_row: does not fire when every row's non-empty count matches", dt.ragged_row(t) is None)

# -- empty_first_cell --
t = mk([["H1", "H2"], ["", "x"]])
check("empty_first_cell: fires on a data row with empty label but a later value",
      dt.empty_first_cell(t) is not None)
t = mk([["H1", "H2"], ["a", "x"]])
check("empty_first_cell: does not fire when the row label is present", dt.empty_first_cell(t) is None)
t = mk([["H1", "H2"], ["", ""]])
check("empty_first_cell: does not fire on a wholly empty row (nothing to absorb)", dt.empty_first_cell(t) is None)

# -- run_together --
t = mk([["Region"], ["acetabulumHip joint"]])
check("run_together: fires on a lowercase->uppercase boundary with no space (PLAN.md's own example)",
      dt.run_together(t) is not None)
t = mk([["Region"], ["Hip joint"]])
check("run_together: does not fire on normally-spaced text", dt.run_together(t) is None)
t = mk([["Region"], ["self-report"]])
check("run_together: a bare hyphen is NOT treated as a bullet boundary (documented design choice)",
      dt.run_together(t) is None)

# -- multi_value --
t = mk([["Test", "Val"], ["A", "71.6"], ["B", "78"], ["C", "71.6 78"]])
r = dt.multi_value(t)
check("multi_value: fires on a cell with 2 numeric tokens where siblings hold 1 (PLAN.md's own example)",
      r is not None and "[3, 1" in r, f"{r}")
t = mk([["Test", "Val"], ["A", "21-24"], ["B", "26-100"], ["C", "21-24 26-100"]])
check("multi_value: numeric RANGES count as single tokens too (PLAN.md's other example)",
      dt.multi_value(t) is not None)
t = mk([["Test", "Val"], ["A", "71.6"], ["B", "78"], ["C", "80"]])
check("multi_value: does not fire when every sibling cell is single-valued", dt.multi_value(t) is None)
t = mk([["Test", "Val"], ["A", "71.6 78"], ["B", "80 82"], ["C", "90 91"]])
check("multi_value: does not fire when multi-number IS the column's established pattern",
      dt.multi_value(t) is None)

# -- single_column --
t = mk([["a"], ["b"], ["c"]], shape=(3, 1))
check("single_column: fires when num_cols == 1", dt.single_column(t) is not None)
t = mk([["a", "b"], ["c", "d"]], shape=(2, 2))
check("single_column: does not fire on a real multi-column table", dt.single_column(t) is None)

# ============================================================================================
# content_retention_check: coordinate mapping, degradation, false-positive suppression
# ============================================================================================
PAGE_W, PAGE_H = 595.0, 842.0


def bottomleft_bbox(top_left_rect: fitz.Rect) -> dict:
    """Convert a normal fitz (top-left-origin) rect into the BOTTOMLEFT-origin dict shape
    content_retention_check expects from the real worker -- mirrors the empirical mapping
    verified in docling-page-selection-report.md §I: fitz_rect = Rect(l, H-t, r, H-b)."""
    return {"l": top_left_rect.x0, "r": top_left_rect.x1,
            "t": PAGE_H - top_left_rect.y0, "b": PAGE_H - top_left_rect.y1,
            "coord_origin": "CoordOrigin.BOTTOMLEFT"}


def make_pdf(name: str, text: str | None, rect=fitz.Rect(50, 50, 300, 150)) -> Path:
    p = tmp / name
    doc = fitz.open()
    pg = doc.new_page(width=PAGE_W, height=PAGE_H)
    if text is not None:
        pg.insert_textbox(rect, text, fontsize=10)
    doc.save(str(p))
    doc.close()
    return p


clip_rect = fitz.Rect(50, 50, 300, 150)

# clean: source text and Docling's cells agree
p_clean = make_pdf("retention_clean.pdf", "First Author Year Country", clip_rect)
t_clean = mk([["First Author", "Year", "Country"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
check("content_retention: clean when fitz text and Docling cells agree",
      dt.content_retention_check(t_clean, str(p_clean)) is None)

# real loss: "Titubation" is on the page but absent from Docling's cells (Sivan-style row-label drop)
p_loss = make_pdf("retention_loss.pdf", "Titubation Present", clip_rect)
t_loss = mk([["Present"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
r = dt.content_retention_check(t_loss, str(p_loss))
check("content_retention: flags a genuinely missing token", r is not None and "titubation" in r, f"{r}")

# scanned-page degradation: no text at all inside the bbox -> None (not-checked), never a false flag
p_scan = make_pdf("retention_scanned.pdf", None, clip_rect)
t_scan = mk([["anything", "goes", "here"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
check("content_retention: degrades to None (not-checked) on a page with no text in the bbox, never flags",
      dt.content_retention_check(t_scan, str(p_scan)) is None)

# false-positive suppression: "HDL-Cb" (glued superscript) vs Docling's "HDL-C b" (space kept)
p_supp = make_pdf("retention_suppress.pdf", "HDL-Cb", clip_rect)
t_supp = mk([["HDL-C b"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
check("content_retention: suppresses the footnote-superscript-glue artifact (validated on real ACSM_p150 case)",
      dt.content_retention_check(t_supp, str(p_supp)) is None)

# mixed case: a real loss ALONGSIDE a suppressible artifact -- both must be handled correctly,
# the real one surfaced, the suppressed one named (not silently dropped from view)
p_mixed = make_pdf("retention_mixed.pdf", "HDL-Cb Titubation", clip_rect)
t_mixed = mk([["HDL-C b"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
r = dt.content_retention_check(t_mixed, str(p_mixed))
check("content_retention: mixed case flags the real loss...", r is not None and "titubation" in r, f"{r}")
check("content_retention: ...and does NOT flag the suppressed artifact as part of the real-loss list",
      r is not None and "'cb'" not in r.split("(suppressed")[0], f"{r}")
check("content_retention: ...and names what it suppressed, rather than hiding it",
      r is not None and "suppressed" in r and "cb" in r, f"{r}")

# REMOVED 2026-07-21: two tests here asserted that a "fitz drops ligature glyphs entirely"
# artifact ("exion" vs Docling "flexion", "Bene"+"ts" vs "Benefits") gets suppressed. That
# suppression has been deleted from docling_tables.py as unfounded -- fitz returns the
# precomposed ligature codepoint, which token normalisation already expands, so the mechanism
# does not exist and the suppressor never fired on real data. See content_retention_check's
# docstring. The test below is kept and is now the load-bearing one: it pins the property that
# actually matters, that a real content loss is NOT explained away.

# a genuinely truncated cell must NOT be suppressed just because it looks partial
p_lig3 = make_pdf("retention_not_lig.pdf", "elephant", clip_rect)
t_lig3 = mk([["eleph"]], page_no=1, bbox=bottomleft_bbox(clip_rect))  # genuinely truncated, not a ligature drop
r = dt.content_retention_check(t_lig3, str(p_lig3))
check("content_retention: a genuine truncation (not a ligature pattern) is NOT suppressed",
      r is not None and "elephant" in r, f"{r}")

# FIX 2: an already-open fitz.Document (doc=) is accepted and gives the same result as pdf_path,
# without the function opening its own handle
doc_open = fitz.open(str(p_clean))
r_doc = dt.content_retention_check(t_clean, doc=doc_open)
doc_open.close()
check("content_retention: accepts an already-open doc= and matches the pdf_path= result",
      r_doc is None)

doc_open2 = fitz.open(str(p_loss))
r_doc2 = dt.content_retention_check(t_loss, doc=doc_open2)
doc_open2.close()
check("content_retention: doc= still catches a real loss, not just the happy path",
      r_doc2 is not None and "titubation" in r_doc2, f"{r_doc2}")

# non-BOTTOMLEFT coord_origin: skip rather than guess a flip
t_unknown_origin = mk([["x"]], page_no=1, bbox={"l": 0, "t": 10, "r": 10, "b": 0, "coord_origin": "CoordOrigin.TOPLEFT"})
check("content_retention: skips (returns None) on an unexpected coord_origin instead of guessing",
      dt.content_retention_check(t_unknown_origin, str(p_clean)) is None)

# no pdf_path given (e.g. called generically via qc_flags with pdf_path=None) -> None, not a crash
check("content_retention: returns None (not a crash) when pdf_path is None",
      dt.content_retention_check(t_clean, None) is None)

# ============================================================================================
# content_retention_check: clipping to the CELL grid rather than the table bbox
# ============================================================================================
# The table bbox frames caption/footnote text that was never table content; clipping to the
# cells' own extent is what removes that false-positive class. Verified on real data (report
# §J): 46/66 -> 21/66 tables flagged on Malanga 2e, with the 21 sampled and confirmed genuine.


def cell(text, row, col, rect: fitz.Rect, origin="CoordOrigin.TOPLEFT", page_h=PAGE_H):
    """One wire-format cell. Cell bboxes are TOPLEFT on real docling output (measured on
    docling 2.113.0), so the default builds them unflipped; pass origin=BOTTOMLEFT to build
    the flipped form and check it maps to the same page region."""
    if origin == "CoordOrigin.BOTTOMLEFT":
        bb = {"l": rect.x0, "r": rect.x1, "t": page_h - rect.y0, "b": page_h - rect.y1,
              "coord_origin": origin}
    else:
        bb = {"l": rect.x0, "t": rect.y0, "r": rect.x1, "b": rect.y1, "coord_origin": origin}
    return {"text": text, "row_span": 1, "col_span": 1,
            "start_row": row, "end_row": row + 1, "start_col": col, "end_col": col + 1,
            "column_header": row == 0, "row_header": False, "row_section": False, "bbox": bb}


# A page whose table bbox also encloses a caption above the grid and a footnote below it --
# the real layout that produced the false positives.
p_spill = tmp / "retention_spill.pdf"
_d = fitz.open()
_pg = _d.new_page(width=PAGE_W, height=PAGE_H)
_pg.insert_text((60, 60), "Table 9.1 Caption Words Here", fontsize=9)   # caption, above cells
_pg.insert_text((60, 100), "Alpha", fontsize=9)                        # r0c0
_pg.insert_text((200, 100), "Beta", fontsize=9)                        # r0c1
_pg.insert_text((60, 130), "Gamma", fontsize=9)                        # r1c0
_pg.insert_text((200, 130), "Delta", fontsize=9)                       # r1c1
_pg.insert_text((60, 175), "Footnote legend citations", fontsize=9)    # footnote, below cells
_d.save(str(p_spill)); _d.close()

_spill_cells = [
    cell("Alpha", 0, 0, fitz.Rect(58, 90, 130, 105)),
    cell("Beta", 0, 1, fitz.Rect(198, 90, 270, 105)),
    cell("Gamma", 1, 0, fitz.Rect(58, 120, 130, 135)),
    cell("Delta", 1, 1, fitz.Rect(198, 120, 270, 135)),
]
# table bbox deliberately spans caption..footnote, exactly as Docling reports it
_spill_bbox = bottomleft_bbox(fitz.Rect(50, 45, 400, 190))
t_spill = mk([["Alpha", "Beta"], ["Gamma", "Delta"]], cells=_spill_cells, bbox=_spill_bbox)

r = dt.content_retention_check(t_spill, str(p_spill))
check("content_retention: caption+footnote inside the TABLE bbox but outside the cells are "
      "not reported as missing cell content", r is None, f"{r}")

# ...and the control: the same table WITHOUT cell bboxes still uses the table bbox, so the
# caption/footnote DO get flagged. Pins that the fix is the cell clipping, not something else.
t_spill_nobbox = mk([["Alpha", "Beta"], ["Gamma", "Delta"]],
                    cells=[dict(c, bbox=None) for c in _spill_cells], bbox=_spill_bbox)
r_nb = dt.content_retention_check(t_spill_nobbox, str(p_spill))
check("content_retention: falls back to the table bbox when no cell carries one (old behaviour "
      "preserved for older worker output)", r_nb is not None and "caption" in r_nb, f"{r_nb}")

# LOAD-BEARING: a cell Docling dropped ENTIRELY must still be caught. This is the regression
# guard for the blind spot that killed the two tighter clipping variants -- clipping to the
# individual cell rects (or to per-row bands) deletes exactly the region whose content went
# missing, so the check goes silent on a real drop. Measured on real pages before this guard
# existed: the cell-union variant missed Malanga p8 (a whole dropped row) and the row-band
# variant missed p36/p46 (a truncated cell losing "Sensitivity: 52.6% Specificity: 82.4%").
t_dropped = mk([["Alpha", "Beta"], ["Gamma", ""]],
               cells=[c for c in _spill_cells if c["text"] != "Delta"], bbox=_spill_bbox)
r_drop = dt.content_retention_check(t_dropped, str(p_spill))
check("content_retention: STILL flags a cell whose content Docling dropped entirely (the clip "
      "must not delete the region the loss happened in)",
      r_drop is not None and "delta" in r_drop, f"{r_drop}")

# cell bboxes in the other origin must map to the same region, not be silently mis-clipped
t_spill_bl = mk([["Alpha", "Beta"], ["Gamma", "Delta"]], bbox=_spill_bbox,
                cells=[cell(c["text"], c["start_row"], c["start_col"],
                            fitz.Rect(c["bbox"]["l"], c["bbox"]["t"], c["bbox"]["r"], c["bbox"]["b"]),
                            origin="CoordOrigin.BOTTOMLEFT") for c in _spill_cells])
check("content_retention: BOTTOMLEFT cell bboxes are flipped to the same region as TOPLEFT ones",
      dt.content_retention_check(t_spill_bl, str(p_spill)) is None,
      f"{dt.content_retention_check(t_spill_bl, str(p_spill))}")

# an unrecognised cell origin must fall back to the table bbox, never guess a flip
t_spill_unknown = mk([["Alpha", "Beta"], ["Gamma", "Delta"]], bbox=_spill_bbox,
                     cells=[dict(c, bbox=dict(c["bbox"], coord_origin="CoordOrigin.WEIRD"))
                            for c in _spill_cells])
check("content_retention: an unknown cell coord_origin falls back to table-bbox clipping "
      "instead of guessing a flip",
      dt.content_retention_check(t_spill_unknown, str(p_spill)) is not None)

# ============================================================================================
# Superscript-glue suppression: every split point, but only on WITHIN-CELL adjacency
# ============================================================================================
# 2- and 3-digit reference markers dominate a medical textbook; the old last-character-only
# rule could not split them ("awake44" -> "awake4"+"4") and left them flagged.
p_ref = make_pdf("retention_ref.pdf", "awake44 Bennett48", clip_rect)
t_ref = mk([["awake 44", "Bennett 48"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
r = dt.content_retention_check(t_ref, str(p_ref))
check("content_retention: suppresses a multi-digit superscript marker ('awake44' = Docling's "
      "adjacent 'awake'+'44')", r is None, f"{r}")

# SAFETY: both halves present but NOT adjacent -> must NOT be suppressed. Without this the
# every-split-point rule would explain away "100" whenever "10" and "0" appear anywhere.
p_nonadj = make_pdf("retention_nonadj.pdf", "100", clip_rect)
t_nonadj = mk([["10 apples", "0 pears"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
r = dt.content_retention_check(t_nonadj, str(p_nonadj))
check("content_retention: does NOT suppress '100' just because '10' and '0' both exist "
      "somewhere in the table (adjacency is required)",
      r is not None and "100" in r, f"{r}")

# SAFETY: adjacency must be WITHIN one cell -- a pair straddling a cell boundary is an artifact
# of flattening the grid, not evidence of a glued superscript.
p_cross = make_pdf("retention_cross.pdf", "alphabeta", clip_rect)
t_cross = mk([["alpha", "beta"]], page_no=1, bbox=bottomleft_bbox(clip_rect),
             cells=[dict(cell("alpha", 0, 0, fitz.Rect(0, 0, 1, 1)), bbox=None),
                    dict(cell("beta", 0, 1, fitz.Rect(0, 0, 1, 1)), bbox=None)])
r = dt.content_retention_check(t_cross, str(p_cross))
check("content_retention: does NOT suppress across a cell boundary ('alpha'|'beta' in two "
      "different cells does not explain a fused 'alphabeta')",
      r is not None and "alphabeta" in r, f"{r}")

# the suppressed token is still named, never silently dropped
p_named = make_pdf("retention_named.pdf", "awake44 Titubation", clip_rect)
t_named = mk([["awake 44"]], page_no=1, bbox=bottomleft_bbox(clip_rect))
r = dt.content_retention_check(t_named, str(p_named))
check("content_retention: a multi-digit suppression is still named in the reason string",
      r is not None and "titubation" in r and "awake44" in r, f"{r}")

# _adjacent_token_pairs builds from cells (not the span-filled grid), so a merged cell's
# duplicated text cannot manufacture a pair
_pairs = dt._adjacent_token_pairs(mk([["a b", "a b"]], cells=[
    {"text": "a b", "start_row": 0, "end_row": 1, "start_col": 0, "end_col": 2,
     "row_span": 1, "col_span": 2, "column_header": False, "row_header": False,
     "row_section": False, "bbox": None}]))
check("_adjacent_token_pairs: a span-duplicated cell does not manufacture a ('b','a') pair",
      ("a", "b") in _pairs and ("b", "a") not in _pairs, f"{sorted(_pairs)}")

# qc_flags composes all checks and never raises even if one is handed garbage
t_garbage_bbox = mk([["a"]], page_no=999, bbox={"l": 0, "t": 10, "r": 10, "b": 0, "coord_origin": "CoordOrigin.BOTTOMLEFT"})
try:
    flags = dt.qc_flags(t_garbage_bbox, str(p_clean))  # page_no 999 doesn't exist in a 1-page pdf
    check("qc_flags: never raises even with an out-of-range page_no", True)
except Exception as e:
    check("qc_flags: never raises even with an out-of-range page_no", False, f"{type(e).__name__}: {e}")

check("format_trace_marker: reproduces PLAN.md's exact wording",
      dt.format_trace_marker(42, ["reason one"]) ==
      "<!-- ⚠️ table structure unverified: reason one — verify against PDF page 42 before citing -->")

# ============================================================================================
# Worker process management: reuse, death/restart, disable-after-cap, per-request timeout.
# No GPU, no Docling install -- a tiny fake worker subprocess stands in via cmd_override.
# ============================================================================================

FAKE_ECHO = r"""
import sys, json
for raw in sys.stdin.buffer:
    line = raw.decode("utf-8").strip()
    if not line:
        continue
    req = json.loads(line)
    pg = req.get("page_no", 1)
    table = {"page_no": pg, "shape": [2, 2],
             "bbox": {"l": 0, "t": 10, "r": 10, "b": 0, "coord_origin": "CoordOrigin.BOTTOMLEFT"},
             "rows": [["h1", "h2"], ["v1", "v2"]], "cells": []}
    resp = {"id": req["id"], "ok": True, "tables": [table], "secs": 0.01}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""

FAKE_ERROR_RESPONSE = r"""
import sys, json
for raw in sys.stdin.buffer:
    line = raw.decode("utf-8").strip()
    if not line:
        continue
    req = json.loads(line)
    resp = {"id": req["id"], "ok": False, "error": "simulated per-page failure"}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""

FAKE_DIES_AFTER_ONE = r"""
import sys, json
for raw in sys.stdin.buffer:
    line = raw.decode("utf-8").strip()
    if not line:
        continue
    req = json.loads(line)
    pg = req.get("page_no", 1)
    table = {"page_no": pg, "shape": [2, 2],
             "bbox": {"l": 0, "t": 10, "r": 10, "b": 0, "coord_origin": "CoordOrigin.BOTTOMLEFT"},
             "rows": [["h1", "h2"], ["v1", "v2"]], "cells": []}
    resp = {"id": req["id"], "ok": True, "tables": [table], "secs": 0.01}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
    sys.exit(0)
"""

FAKE_DIES_IMMEDIATELY = "import sys; sys.exit(1)"

FAKE_HANGS = r"""
import sys, time
for raw in sys.stdin.buffer:
    line = raw.decode("utf-8").strip()
    if not line:
        continue
    time.sleep(30)
"""


def worker_with(code: str, **kw) -> dt.DoclingTableWorker:
    return dt.DoclingTableWorker(cmd_override=[sys.executable, "-c", code], **kw)


# -- basic request/response + cross-"book" reuse (different pdf_path per call, one process) --
with worker_with(FAKE_ECHO) as w:
    r1 = w.tables_for_page("C:/fake/bookA.pdf", 10)
    r2 = w.tables_for_page("C:/fake/bookB.pdf", 20)   # different "book", same process
    r3 = w.tables_for_page("C:/fake/bookA.pdf", 30)   # back to book A
    check("worker: round-trips a real table payload through the subprocess pipe",
          len(r1) == 1 and isinstance(r1[0], dt.DoclingTable) and r1[0].rows == [["h1", "h2"], ["v1", "v2"]])
    check("worker: single process answers requests for a DIFFERENT pdf_path without restarting",
          len(r2) == 1 and r2[0].page_no == 20)
    check("worker: and keeps answering for the original pdf_path afterwards",
          len(r3) == 1 and r3[0].page_no == 30)
    check("worker: status() reports running + not disabled + 0 consecutive restarts",
          w.status()["running"] and not w.status()["disabled"] and w.status()["consecutive_restarts"] == 0)

# -- a well-formed ok:false response is NOT treated as worker death --
with worker_with(FAKE_ERROR_RESPONSE) as w:
    r = w.tables_for_page("C:/fake/book.pdf", 1)
    check("worker: a clean ok:false page-level error returns [] without killing the worker",
          r == [] and w.status()["running"] and not w.status()["disabled"])
    check("worker: last_error surfaces the worker's own error message",
          w.last_error == "simulated per-page failure")

# -- death mid-batch triggers restart and the worker recovers on the call AFTER the one that
# hit the dead process (no in-call retry -- see tables_for_page's docstring for why) --
with worker_with(FAKE_DIES_AFTER_ONE, request_timeout=10) as w:
    r1 = w.tables_for_page("C:/fake/book.pdf", 1)   # succeeds, then the fake process exits
    r2 = w.tables_for_page("C:/fake/book.pdf", 2)   # hits the now-dead process -> [] + triggers restart
    r3 = w.tables_for_page("C:/fake/book.pdf", 3)   # fresh respawned process -> succeeds again
    check("worker: first call succeeds before the (simulated) crash", len(r1) == 1)
    check("worker: the call that hits the dead process returns [] (not retried in-call)", r2 == [])
    check("worker: the NEXT call after that succeeds against the auto-restarted process", len(r3) == 1)
    check("worker: consecutive_restarts resets to 0 after a successful call post-restart",
          w.status()["consecutive_restarts"] == 0)

# -- disables after max_consecutive_restarts, stops thrashing --
with worker_with(FAKE_DIES_IMMEDIATELY, max_consecutive_restarts=2, request_timeout=10) as w:
    outcomes = [w.tables_for_page("C:/fake/book.pdf", i) for i in range(5)]
    check("worker: every call returns [] (never raises) even though the worker never comes up",
          all(o == [] for o in outcomes))
    check("worker: disables itself after exceeding max_consecutive_restarts",
          w.status()["disabled"] is True)
    check("worker: stops incrementing the restart counter once disabled (no thrashing)",
          w.status()["consecutive_restarts"] == 3,  # max(2) + 1 = 3rd failure trips disable
          f"{w.status()['consecutive_restarts']}")

# -- per-request timeout: a hung worker must not hang the caller --
with worker_with(FAKE_HANGS, request_timeout=1.0) as w:
    t0 = time.time()
    r = w.tables_for_page("C:/fake/book.pdf", 1)
    dt_elapsed = time.time() - t0
    check("worker: a hung request returns [] instead of blocking forever", r == [])
    check("worker: returns within the configured timeout, not the full 30s hang",
          dt_elapsed < 5.0, f"elapsed={dt_elapsed:.1f}s")
    check("worker: last_error mentions the timeout", w.last_error is not None and "timed out" in w.last_error)

# -- unavailable worker (no venv configured, no override) never raises, just returns [] --
old_venv = dt.DOCLING_VENV_PY
dt.DOCLING_VENV_PY = None
try:
    w = dt.DoclingTableWorker()
    with w:
        r = w.tables_for_page("C:/fake/book.pdf", 1)
    check("worker: with no DOCLING_VENV_PY configured, tables_for_page returns [] without raising", r == [])
finally:
    dt.DOCLING_VENV_PY = old_venv

old_venv2 = dt.DOCLING_VENV_PY
dt.DOCLING_VENV_PY = None
try:
    check("docling_available(): False when DOCLING_VENV_PY unset", dt.docling_available() is False)
finally:
    dt.DOCLING_VENV_PY = old_venv2

# ============================================================================================
# ligature repair (F5a spaced / F5b dropped) -- docling-bench40.md §F5
# Oracle-gated: a token is rewritten ONLY when its current form is absent from the source
# page's text layer AND the proposed form is present. No GPU / no Docling install needed.
# ============================================================================================

lig_rect = fitz.Rect(50, 50, 400, 150)
lig_bbox = bottomleft_bbox(lig_rect)

# -- F5a: Docling expanded the ligature but left spurious spaces --
pdf_a = make_pdf("lig_f5a.pdf", "Benefits outweigh harms", rect=lig_rect)
t = mk([["Assessment"], ["Bene fi ts outweigh harms"]], page_no=1, bbox=lig_bbox)
rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_a))
check("ligature F5a: 'Bene fi ts' -> 'Benefits' when the source page attests it",
      rows[1][0] == "Benefits outweigh harms", f"{rows[1][0]!r}")
check("ligature F5a: reports one repair, tagged with the variant",
      len(reps) == 1 and reps[0]["variant"] == "F5a-spaced" and reps[0]["after"] == "Benefits",
      f"{reps}")
check("ligature F5a: does not mutate the input table",
      t.rows[1][0] == "Bene fi ts outweigh harms")

# -- F5b: Docling dropped the ligature's leading 'f' (reads as a plain typo) --
pdf_b = make_pdf("lig_f5b.pdf", "specific inflammation findings", rect=lig_rect)
t = mk([["Finding"], ["speciic inlammation"]], page_no=1, bbox=lig_bbox)
rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_b))
check("ligature F5b: 'speciic' -> 'specific' and 'inlammation' -> 'inflammation'",
      rows[1][0] == "specific inflammation", f"{rows[1][0]!r}")
check("ligature F5b: both repairs tagged F5b-dropped",
      len(reps) == 2 and all(r["variant"] == "F5b-dropped" for r in reps), f"{reps}")

# -- the boundary that matters: no source counterpart => DO NOT GUESS, leave it alone --
pdf_c = make_pdf("lig_nocounterpart.pdf", "widget gadget sprocket", rect=lig_rect)
t = mk([["Finding"], ["speciic"]], page_no=1, bbox=lig_bbox)
rows, reps, sus = dt.repair_ligatures(t, pdf_path=str(pdf_c))
check("ligature: a token with NO source counterpart is left untouched (never guessed)",
      rows[1][0] == "speciic" and reps == [], f"rows={rows[1][0]!r} reps={reps}")
check("ligature: the unrepairable token is reported as a suspect instead of being rewritten",
      any(s["token"] == "speciic" for s in sus), f"{sus}")

# -- no-op when Docling already agrees with the source (protects books Docling gets right) --
pdf_d = make_pdf("lig_already_ok.pdf", "anulus ﬁbrosus outer ﬁbers", rect=lig_rect)
t = mk([["Ligament"], ["anulus fibrosus (outer fibers)"]], page_no=1, bbox=lig_bbox)
rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_d))
check("ligature: strict no-op when Docling's token already matches the source's ligature codepoint",
      rows[1][0] == "anulus fibrosus (outer fibers)" and reps == [], f"rows={rows[1][0]!r} reps={reps}")

# -- kill-switch OFF control --
os.environ["T2N_LIGATURE_REPAIR"] = "0"
try:
    t = mk([["Finding"], ["speciic"]], page_no=1, bbox=lig_bbox)
    rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_b))
    check("ligature: T2N_LIGATURE_REPAIR=0 disables repair entirely (rows returned unchanged)",
          rows[1][0] == "speciic" and reps == [], f"rows={rows[1][0]!r} reps={reps}")
finally:
    os.environ.pop("T2N_LIGATURE_REPAIR", None)
check("ligature: kill-switch defaults ON when the var is unset", dt.ligature_repair_enabled() is True)

# -- scanned page (no text layer) => no oracle => never guess, never crash --
pdf_scan = make_pdf("lig_scanned.pdf", None, rect=lig_rect)
t = mk([["Finding"], ["speciic"]], page_no=1, bbox=lig_bbox)
rows, reps, sus = dt.repair_ligatures(t, pdf_path=str(pdf_scan))
check("ligature: no text layer => no repair and no crash (degrades like content_retention_check)",
      rows[1][0] == "speciic" and reps == [] and sus == [], f"rows={rows[1][0]!r}")

# -- trace marker: present iff something was repaired --
t = mk([["Finding"], ["speciic"]], page_no=1, bbox=lig_bbox)
rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_b))
marker = dt.format_repair_marker(t.page_no, reps)
check("ligature: a repaired table gets a trace marker naming the substitution and the page",
      "speciic" in marker and "specific" in marker and "page 1" in marker, f"{marker!r}")
check("ligature: an untouched table gets no marker at all", dt.format_repair_marker(1, []) == "")

# -- a page_no with no such page (out-of-range) is a missing oracle, not a crash --
t = mk([["Finding"], ["speciic"]], page_no=99, bbox=lig_bbox)
rows, reps, _sus = dt.repair_ligatures(t, pdf_path=str(pdf_b))
check("ligature: out-of-range page_no degrades to no-repair rather than raising",
      rows[1][0] == "speciic" and reps == [])


# ── report ──────────────────────────────────────────────────────────────────
fails = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    mark = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[status]
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and status != "PASS" else ""))
print(f"--- {sum(r[1]=='PASS' for r in results)} pass, "
      f"{len(fails)} fail, {sum(r[1]=='SKIP' for r in results)} skip ---")
sys.exit(1 if fails else 0)
