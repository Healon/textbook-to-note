"""test_table_merge.py — regression guard for cross-page table merging (T2N_TABLE_MERGE).

Same shape as figures/test_contract.py: a list of check()/skip() results, a printed report,
exit 0 (all pass/skip) / exit 1 (any FAIL). Fixtures are tiny synthetic PDFs built with fitz
(ruled grid tables), so the suite is self-contained — no real textbook needed.

Cases:
  1. true continuation       — a table runs off page 1's bottom and resumes at page 2's top with
                               the same columns → merged into one block, one trace comment.
  2. false-positive lookalike — two same-width tables, but the first sits mid-page (does not run
                               off the bottom) → must NOT merge.
  3. repeated-header case     — the continuation repeats the header row → deduped to one header.
Plus predicate-level unit checks on the geometry gate, and a flag-OFF exact-fallback check.

  python converter/test_table_merge.py   → exit 0 all pass/skip · exit 1 any FAIL
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `shared.config` (repo-root/shared) resolves
sys.path.insert(0, str(HERE))
import convert as cv  # noqa: E402

results = []  # (name, "PASS"|"FAIL"|"SKIP", detail)


def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL", detail))


def skip(name, detail=""):
    results.append((name, "SKIP", detail))


# ── fixture builder ─────────────────────────────────────────────────────────
PAGE_W, PAGE_H = 595.0, 842.0
COLS = [130, 130, 130]  # x0=60 → x-edges 60,190,320,450


def draw_table(page, x0, y0, col_ws, row_h, rows):
    """Draw a ruled grid table at (x0,y0) and fill its cells; return the (x0,top,x1,bottom) bbox.
    Coordinates are top-origin points (shared by fitz and pdfplumber)."""
    xs = [x0]
    for w in col_ws:
        xs.append(xs[-1] + w)
    x1 = xs[-1]
    ys = [y0 + r * row_h for r in range(len(rows) + 1)]
    y1 = ys[-1]
    for x in xs:
        page.draw_line((x, y0), (x, y1), width=0.8)
    for y in ys:
        page.draw_line((x0, y), (x1, y), width=0.8)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((xs[c] + 3, ys[r] + row_h - 5), str(cell), fontsize=8)
    return (x0, y0, x1, y1)


def build_pdf(path, page_specs):
    doc = fitz.open()
    for spec in page_specs:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        if spec.get("header_text"):
            page.insert_text((60, 40), spec["header_text"], fontsize=9)
        for t in spec["tables"]:
            draw_table(page, t["x0"], t["y0"], t["col_ws"], t["row_h"], t["rows"])
    doc.save(str(path))
    doc.close()


def convert(path, merge):
    out = str(path) + (".on.md" if merge else ".off.md")
    os.environ["T2N_TABLE_MERGE"] = "1" if merge else "0"
    cv.convert_pdf(str(path), out, "test")
    return Path(out).read_text(encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="t2n_merge_"))

# ── case 1: true continuation ───────────────────────────────────────────────
p1 = tmp / "case1_continuation.pdf"
build_pdf(p1, [
    {"header_text": "RUNNING HEADER — CHAPTER 3", "tables": [
        # runs off the bottom: y0=690, 6 rows * 18 = 108 → bottom 798 (near page bottom 842)
        {"x0": 60, "y0": 690, "col_ws": COLS, "row_h": 18, "rows": [
            ["Muscle", "Root", "Action"], ["Deltoid", "C5", "Abduction"],
            ["Biceps", "C6", "Flexion"], ["Triceps", "C7", "Extension"],
            ["FDP", "C8", "Flex DIP"], ["Interossei", "T1", "Abduction"],
        ]},
    ]},
    {"header_text": "RUNNING HEADER — CHAPTER 3", "tables": [
        {"x0": 60, "y0": 90, "col_ws": COLS, "row_h": 18, "rows": [
            ["APB", "T1", "Thumb abd"], ["Quadriceps", "L3", "Knee ext"],
            ["Tib ant", "L4", "Dorsiflex"],
        ]},
    ]},
])
md1 = convert(p1, merge=True)
check("case1: continuation merged into a single table block",
      md1.count("**[Table on page") == 1, f"blocks={md1.count('**[Table on page')}")
check("case1: trace comment records the continued page",
      "<!-- table continues from page 2 -->" in md1)
check("case1: rows from both pages present in one table",
      "Deltoid" in md1 and "Tib ant" in md1)
md1_off = convert(p1, merge=False)
check("case1: flag OFF → two separate table blocks (exact-fallback)",
      md1_off.count("**[Table on page") == 2 and "table continues from" not in md1_off,
      f"off blocks={md1_off.count('**[Table on page')}")

# ── case 2: false-positive lookalike (same width, first is mid-page) ─────────
p2 = tmp / "case2_lookalike.pdf"
build_pdf(p2, [
    {"header_text": "SECTION A", "tables": [
        # mid-page: y0=300 → bottom 408, far from page bottom → not a runoff
        {"x0": 60, "y0": 300, "col_ws": COLS, "row_h": 18, "rows": [
            ["Nerve", "Level", "Test"], ["Median", "C6", "Pronation"],
            ["Ulnar", "C8", "Grip"], ["Radial", "C7", "Wrist ext"],
        ]},
    ]},
    {"header_text": "SECTION B", "tables": [
        {"x0": 60, "y0": 90, "col_ws": COLS, "row_h": 18, "rows": [
            ["Artery", "Region", "Branch"], ["Axillary", "Shoulder", "Circumflex"],
            ["Brachial", "Arm", "Profunda"],
        ]},
    ]},
])
md2 = convert(p2, merge=True)
check("case2: unrelated same-width tables NOT merged",
      md2.count("**[Table on page") == 2 and "table continues from" not in md2,
      f"blocks={md2.count('**[Table on page')}")

# ── case 3: repeated header on the continuation ─────────────────────────────
p3 = tmp / "case3_repeated_header.pdf"
HEADER = ["Drug", "Dose", "Route"]
build_pdf(p3, [
    {"header_text": "TABLE OF MEDICATIONS", "tables": [
        {"x0": 60, "y0": 690, "col_ws": COLS, "row_h": 18, "rows": [
            HEADER, ["Baclofen", "10mg", "PO"], ["Tizanidine", "4mg", "PO"],
            ["Dantrolene", "25mg", "PO"], ["Diazepam", "5mg", "PO"], ["Gabapentin", "300mg", "PO"],
        ]},
    ]},
    {"header_text": "TABLE OF MEDICATIONS", "tables": [
        {"x0": 60, "y0": 90, "col_ws": COLS, "row_h": 18, "rows": [
            HEADER, ["Botox", "100U", "IM"], ["Phenol", "5%", "Perineural"],
        ]},
    ]},
])
md3 = convert(p3, merge=True)
check("case3: repeated-header continuation merged",
      md3.count("**[Table on page") == 1 and "table continues from page 2" in md3,
      f"blocks={md3.count('**[Table on page')}")
check("case3: repeated header row deduped (appears once)",
      md3.count("| Drug | Dose | Route |") == 1,
      f"header count={md3.count('| Drug | Dose | Route |')}")
check("case3: all data rows kept across the merge",
      "Baclofen" in md3 and "Botox" in md3 and "Phenol" in md3)

# ── predicate unit checks ───────────────────────────────────────────────────
def _tbl(page_num, y0_top, y1_bottom, x0=60.0, x1=450.0, cols=3, xedges=None):
    return {
        "page": page_num, "rows": [], "md": "x", "col_count": cols,
        "xedges": xedges if xedges is not None else [60.0, 190.0, 320.0],
        "bbox": (x0, y0_top, x1, y1_bottom), "page_w": PAGE_W, "page_h": PAGE_H,
        "heading_above": False, "heading_below": False,
    }

top_tbl = _tbl(1, 690.0, 798.0)  # runs off bottom
bot_tbl = _tbl(2, 90.0, 180.0)   # resumes at top
check("predicate: runoff-bottom + resume-top + aligned cols → continuation",
      cv._is_continuation(top_tbl, bot_tbl))
check("predicate: upper table not near bottom → not a continuation",
      not cv._is_continuation(_tbl(1, 300.0, 408.0), bot_tbl))
check("predicate: lower table not near top → not a continuation",
      not cv._is_continuation(top_tbl, _tbl(2, 400.0, 500.0)))
check("predicate: mismatched column count → not a continuation",
      not cv._is_continuation(top_tbl, _tbl(2, 90.0, 180.0, cols=4, xedges=[60.0, 160.0, 260.0, 360.0])))
check("predicate: intervening heading blocks merge",
      not cv._is_continuation({**top_tbl, "heading_below": True}, bot_tbl))
check("predicate: misaligned x-edges → not a continuation",
      not cv._is_continuation(top_tbl, _tbl(2, 90.0, 180.0, x0=200.0, x1=560.0, xedges=[200.0, 320.0, 440.0])))


# ── no-data-loss: a merge that would fail the content filter falls back ───────
# Two sparse tables each pass the >5% content filter on their own, but their union dips below it.
# The merge must NOT be committed (which would drop the whole table) — both are emitted instead.
def _sparse_tbl(page_num, y0, y1, header, n_empty):
    rows = [list(header)] + [["", "", ""] for _ in range(n_empty)]
    return {
        "page": page_num, "rows": rows, "md": cv._table_to_md(rows), "col_count": 3,
        "xedges": [60.0, 190.0, 320.0], "bbox": (60.0, y0, 450.0, y1),
        "page_w": PAGE_W, "page_h": PAGE_H, "heading_above": False, "heading_below": False,
    }

s1 = _sparse_tbl(1, 690.0, 798.0, ["A", "B", "C"], 15)  # 3/48 ≈ 0.063 alone → kept
s2 = _sparse_tbl(2, 90.0, 180.0, ["A", "B", "C"], 15)   # kept alone; union 3/93 ≈ 0.032 → dropped
check("sparse merge would fail filter → both tables individually valid",
      s1["md"] and s2["md"] and cv._is_continuation(s1, s2))
emit = cv.merge_page_tables([[s1], [s2]])
blocks = [b for page in emit.values() for b in page]
check("sparse merge falls back → no data loss (both tables emitted, unmerged)",
      len(blocks) == 2 and all(b[1] == [] for b in blocks),
      f"blocks={len(blocks)} cont_pages={[b[1] for b in blocks]}")

# ── report ──────────────────────────────────────────────────────────────────
fails = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    mark = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[status]
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and status != "PASS" else ""))
print(f"--- {sum(r[1]=='PASS' for r in results)} pass, "
      f"{len(fails)} fail, {sum(r[1]=='SKIP' for r in results)} skip ---")
sys.exit(1 if fails else 0)
