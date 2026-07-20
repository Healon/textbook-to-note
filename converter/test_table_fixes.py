"""test_table_fixes.py — regression guard for the three table-correctness fixes:

  * T2N_TABLE_FRAME_REJECT — discard page-frame pseudo-tables (1-column blobs produced by page
    decoration rectangles), which otherwise ship a real multi-column table collapsed into one
    column, or ordinary prose, as citable-looking markdown.
  * T2N_BOOK_TABLE_CHECK  — warn loudly when a whole book yields no tables at all.
  * page-error counting   — a page that raises during the table pass is counted and reported
    instead of being swallowed by a bare `except Exception`.

Same shape as test_table_merge.py: check()/skip() results, a printed report, exit 0 (all pass/skip)
/ exit 1 (any FAIL). Fixtures are tiny synthetic PDFs built with fitz, so the suite is
self-contained — no real textbook needed.

  python converter/test_table_fixes.py   → exit 0 all pass/skip · exit 1 any FAIL
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


PAGE_W, PAGE_H = 595.0, 842.0
tmp = Path(tempfile.mkdtemp(prefix="t2n_fixes_"))

LOREM = ("Antihistamine and decongestant combinations showed no significant benefit in treating "
         "otitis media with effusion, and no additional studies have been published since to "
         "change this recommendation. Adverse effects include insomnia and hyperactivity. ")


def draw_grid(page, x0, y0, col_ws, row_h, rows):
    """Draw a ruled grid table and fill its cells (top-origin points)."""
    xs = [x0]
    for w in col_ws:
        xs.append(xs[-1] + w)
    ys = [y0 + r * row_h for r in range(len(rows) + 1)]
    for x in xs:
        page.draw_line((x, y0), (x, ys[-1]), width=0.8)
    for y in ys:
        page.draw_line((x0, y), (xs[-1], y), width=0.8)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((xs[c] + 3, ys[r] + row_h - 5), str(cell), fontsize=8)


def add_page_frame(page):
    """The page decoration that fools pdfplumber: a full-page content frame plus the rule under the
    running header. Together they give 3 horizontal and 2 vertical intersecting edges, so
    find_tables() returns ONE table whose bbox is the page body, with rows=2 and columns=1 — the
    exact shape measured on the real pages (wp3-borderless-report.md §5b)."""
    page.draw_rect(fitz.Rect(25.3, 30.0, 570.0, 750.5), width=0.7)  # full-page content frame
    page.draw_line((25.3, 49.1), (570.0, 49.1), width=0.7)          # rule under the running header


def convert(path, out_name, env=None):
    """Convert with a temporary env overlay; returns (markdown, stats)."""
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        out = str(path) + f".{out_name}.md"
        stats = cv.convert_pdf(str(path), out, "test")
        return Path(out).read_text(encoding="utf-8"), stats
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── case 1: page-frame pseudo-table over 2-column prose is rejected ──────────
p1 = tmp / "case1_page_frame.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
add_page_frame(pg)
pg.insert_text((40, 33), "OTITIS MEDIA WITH EFFUSION", fontsize=8)
# A cross-reference, not a caption — this is how a table-free prose page passes the table gate in
# the real corpus (the gate's first branch is a text test for the word "Table"; report §1).
pg.insert_textbox(fitz.Rect(40, 60, 290, 740),
                  "Evidence is summarized in Table 3.1. " + LOREM * 6, fontsize=9)  # left column
pg.insert_textbox(fitz.Rect(310, 60, 570, 740), LOREM * 6, fontsize=9)              # right column
doc.save(str(p1))
doc.close()

md1, st1 = convert(p1, "on")
check("case1: page-frame pseudo-table is not emitted as a table",
      md1.count("**[Table on page") == 0, f"blocks={md1.count('**[Table on page')}")
check("case1: rejection leaves a visible trace comment (loss is never silent)",
      "page-frame pseudo-table rejected on page 1" in md1)
check("case1: rejection is counted in stats",
      st1["rejected_tables"] >= 1, f"rejected={st1['rejected_tables']}")
check("case1: the page prose itself survives the rejection",
      "Antihistamine and decongestant" in md1.replace("\n", " "))

md1_off, st1_off = convert(p1, "off", {"T2N_TABLE_FRAME_REJECT": "0"})
check("case1: kill-switch restores the old behavior (pseudo-table comes back)",
      md1_off.count("**[Table on page") == 1 and st1_off["rejected_tables"] == 0,
      f"blocks={md1_off.count('**[Table on page')}")

# ── case 2: a genuine multi-column table is NOT rejected ─────────────────────
TABLE_ROWS = [
    ["Test", "Sensitivity", "Specificity"],
    ["Neer sign", "88.7%", "30.5%"],
    ["Hawkins test", "91.7%", "44.3%"],
    ["Full can test", "77.0%", "74.0%"],
    ["Drop arm test", "26.9%", "88.4%"],
]
p2 = tmp / "case2_real_table.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
draw_grid(pg, 60, 120, [130, 130, 130], 18, TABLE_ROWS)
doc.save(str(p2))
doc.close()

md2, st2 = convert(p2, "on")
check("case2: genuine multi-column table is kept",
      md2.count("**[Table on page") == 1, f"blocks={md2.count('**[Table on page')}")
check("case2: its column bindings are intact",
      "| Neer sign | 88.7% | 30.5% |" in md2)
check("case2: nothing rejected on a healthy table page",
      st2["rejected_tables"] == 0, f"rejected={st2['rejected_tables']}")

# ── case 2b: page frame AND a real table on the same page ───────────────────
# The decisive case: the frame must be dropped while the real table survives untouched, proving
# the predicate keys on column count rather than on the presence of page decoration.
p2b = tmp / "case2b_frame_plus_table.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
add_page_frame(pg)
draw_grid(pg, 60, 120, [130, 130, 130], 18, TABLE_ROWS)
doc.save(str(p2b))
doc.close()

md2b, st2b = convert(p2b, "on")
check("case2b: the framed page yields exactly one table — the real one",
      md2b.count("**[Table on page") == 1, f"blocks={md2b.count('**[Table on page')}")
check("case2b: real table's column bindings survive alongside a rejected frame",
      "| Neer sign | 88.7% | 30.5% |" in md2b and st2b["rejected_tables"] == 1,
      f"rejected={st2b['rejected_tables']}")
md2b_off, st2b_off = convert(p2b, "off", {"T2N_TABLE_FRAME_REJECT": "0"})
check("case2b: with the fix off, the frame pseudo-table pollutes the page (the bug being fixed)",
      md2b_off.count("**[Table on page") == 2, f"blocks={md2b_off.count('**[Table on page')}")

# ── case 3: genuine 1-column boxed list — locked-in behavior ─────────────────
# A 1-column markdown table encodes no column bindings, and the box text is re-emitted verbatim in
# the page prose, so a LONG boxed list is dropped (its content is not lost) while a SHORT one, which
# cannot be a page-body dump, is left alone. This is the deliberate trade, pinned here.
p3 = tmp / "case3_boxed_list.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
pg.draw_rect(fitz.Rect(60, 100, 520, 700), width=0.8)
pg.draw_line((60, 130), (520, 130), width=0.8)  # rule under the box's own title band
pg.insert_text((66, 122), "TABLE 9-5  Indications for Stopping an Exercise Stress Test", fontsize=9)
pg.insert_textbox(fitz.Rect(66, 136, 514, 694),
                  "Absolute contraindications to exercise testing. " + LOREM * 4, fontsize=9)
doc.save(str(p3))
doc.close()

md3, st3 = convert(p3, "on")
check("case3: long 1-column boxed list is dropped, with a trace",
      md3.count("**[Table on page") == 0 and "page-frame pseudo-table rejected" in md3,
      f"blocks={md3.count('**[Table on page')}")
check("case3: its text is still present in the page prose (no content loss)",
      "Absolute contraindications to exercise testing" in md3.replace("\n", " "))

p3b = tmp / "case3b_short_box.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
draw_grid(pg, 60, 300, [200], 18, [["Red flags"], ["Fever"], ["Weight loss"], ["Night pain"]])
doc.save(str(p3b))
doc.close()

md3b, st3b = convert(p3b, "on")
check("case3b: short 1-column list (no page-body-sized cell, small bbox) is kept",
      st3b["rejected_tables"] == 0, f"rejected={st3b['rejected_tables']}")

# ── case 4: whole-book detector fires when pdfplumber sees 0 pages ───────────
# Reproduces the Zasler/Lanken/NSCA mode: fitz opens the file fine, pdfminer's page-tree walk
# returns nothing, and every table in the book is lost with no error at all.
p4 = tmp / "case4_zero_plumber.pdf"
doc = fitz.open()
for _ in range(3):
    pg = doc.new_page(width=PAGE_W, height=PAGE_H)
    pg.insert_textbox(fitz.Rect(60, 100, 520, 700),
                      "TABLE 4.1 Pretest likelihood of ischemic heart disease\n" * 6, fontsize=9)
doc.save(str(p4))
doc.close()


class _NoPages:
    pages: list = []

    def close(self):
        pass


real_open = cv.pdfplumber.open
cv.pdfplumber.open = lambda *a, **k: _NoPages()
try:
    md4, st4 = convert(p4, "on")
finally:
    cv.pdfplumber.open = real_open

check("case4: detector fires when pdfplumber parses 0 pages but fitz opens the book",
      any("parsed 0 pages" in w for w in st4["warnings"]), f"warnings={st4['warnings']}")
check("case4: the warning is written into the markdown, not just the console",
      "[!warning] Conversion warnings" in md4)
check("case4: per-page failures are counted, not swallowed",
      st4["page_errors"] == 3, f"page_errors={st4['page_errors']}")

# ── case 5: detector does NOT fire on a healthy table-bearing book ───────────
md5, st5 = convert(p2, "healthy")
check("case5: no warning on a book that extracts tables fine",
      st5["warnings"] == [] and "[!warning]" not in md5, f"warnings={st5['warnings']}")
check("case5: no spurious page errors on a healthy book",
      st5["page_errors"] == 0, f"page_errors={st5['page_errors']}")

# ── case 6: caption-based detector needs BOTH zero tables and enough captions ─
p6 = tmp / "case6_captions_no_tables.pdf"
doc = fitz.open()
pg = doc.new_page(width=PAGE_W, height=PAGE_H)
pg.insert_textbox(fitz.Rect(60, 60, 520, 780),
                  "\n".join(f"TABLE 3.{i} Borderless summary of findings" for i in range(1, 15)),
                  fontsize=9)
doc.save(str(p6))
doc.close()

md6, st6 = convert(p6, "on")
check("case6: >=10 captions with 0 tables extracted raises the book-level warning",
      any("0 tables extracted despite" in w for w in st6["warnings"]),
      f"captions={st6['table_captions']} warnings={st6['warnings']}")

md6_off, st6_off = convert(p6, "off", {"T2N_BOOK_TABLE_CHECK": "0"})
check("case6: kill-switch silences the book-level detector",
      st6_off["warnings"] == [] and "[!warning]" not in md6_off, f"warnings={st6_off['warnings']}")

# ── predicate unit checks ───────────────────────────────────────────────────
big = "x" * 600
check("predicate: 1 column + oversized cell → rejected",
      cv.page_frame_reject_reason([[big], ["y"]], (30, 40, 570, 800), PAGE_W, PAGE_H) is not None)
check("predicate: 1 column + page-covering bbox → rejected even with short cells",
      cv.page_frame_reject_reason([["a"], ["b"]], (30, 40, 570, 800), PAGE_W, PAGE_H) is not None)
check("predicate: 1 column, short cells, small bbox → kept",
      cv.page_frame_reject_reason([["a"], ["b"]], (60, 300, 260, 380), PAGE_W, PAGE_H) is None)
check("predicate: multi-column is never rejected, however large the cell or bbox",
      cv.page_frame_reject_reason([[big, big], ["a", "b"]], (30, 40, 570, 800), PAGE_W, PAGE_H) is None)

# ── report ──────────────────────────────────────────────────────────────────
fails = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    mark = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[status]
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and status != "PASS" else ""))
print(f"--- {sum(r[1]=='PASS' for r in results)} pass, "
      f"{len(fails)} fail, {sum(r[1]=='SKIP' for r in results)} skip ---")
sys.exit(1 if fails else 0)
