"""test_contract.py — regression guard for the figure-remap contract.

Run after ANY edit to figure_qc_gate.py or figure_remap.py. Asserts the
policy-locked invariants:

  1. fig_id dash normalization is variant-agnostic.
  2. The public entrypoint returns ONLY the contract keys (no engine leakage).
  3. strict (default) HARD-FAILs on a far-off page — never a silent wrong raster.
  4. strict (default) returns match_quality 'exact' on a correct deterministic match.
  5. strict auto-sweeps ±1 neighbor pages — off-by-one input still passes.
  6. The internal gate CLI is guarded (workflow cannot bypass via the script CLI).

The PDF-dependent checks (2-5) need a real digital-native textbook PDF with at
least one figure whose caption you know, plus its exact page number and
fig_id. Point T2N_TEST_PDF at that PDF and set T2N_TEST_FIG_ID / T2N_TEST_PAGE
/ T2N_TEST_CAPTION / T2N_TEST_BOOK to match; if T2N_TEST_PDF is unset, those
checks SKIP (not fail) so the suite is runnable with no fixture at all.

Minimum fixture requirements for the FULL suite (all of 2-5) to actually run
rather than partially skip:
  - the document must have a page at least FAR_OFF_MIN_DIST (3) pages away
    from T2N_TEST_PAGE in either direction (i.e. TEST_PAGE - 3 >= 1, or the
    document has >= TEST_PAGE + 3 pages) — check 3 (far-off hard_fail) SKIPs
    with a message if no such page exists (e.g. a document with < 4 pages
    total when TEST_PAGE is page 1).
  - T2N_TEST_FIG_ID's figure must sit on T2N_TEST_PAGE with an unambiguous,
    deterministically-matchable caption↔raster pair (checks 2, 4, 5 depend on
    this).

  python test_contract.py   → exit 0 all pass/skip · exit 1 any FAIL
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import figure_remap as fr  # noqa: E402
from figure_qc_gate import normalize_fig_id  # noqa: E402

TEST_PDF = os.environ.get("T2N_TEST_PDF")
TEST_BOOK = os.environ.get("T2N_TEST_BOOK", "test_book")
TEST_FIG_ID = os.environ.get("T2N_TEST_FIG_ID", "1.1")
TEST_PAGE = int(os.environ.get("T2N_TEST_PAGE", "1"))
TEST_CAPTION = os.environ.get("T2N_TEST_CAPTION", "")

OUT = HERE / "_test_contract_tmp.jpeg"
CONTRACT_KEYS = {"status", "match_quality", "hard_fail", "file", "fig_id",
                 "reason", "qc_degraded", "qc_skipped"}

# A far-off page must be at least this many pages from TEST_PAGE, else the
# extractor's ±1 neighbor sweep can land back on TEST_PAGE and flip the
# expected hard_fail into a pass (see check 3 below).
FAR_OFF_MIN_DIST = 3


def _far_off_page(pdf_path: str, base_page: int, min_dist: int = FAR_OFF_MIN_DIST) -> int | None:
    """Pick a page >= min_dist away from base_page (1-indexed), clamped to the
    document's actual page count. Returns None if the document is too short
    to place such a page in either direction."""
    import fitz
    doc = fitz.open(pdf_path)
    total = doc.page_count
    doc.close()
    candidate = base_page - min_dist
    if candidate >= 1:
        return candidate
    candidate = base_page + min_dist
    if candidate <= total:
        return candidate
    return None

results = []  # (name, "PASS"|"FAIL"|"SKIP", detail)


def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL", detail))


def skip(name, detail=""):
    results.append((name, "SKIP", detail))


# 1. dash normalization
check("normalize en/em/hyphen/dot → canonical",
      normalize_fig_id("64-5") == normalize_fig_id("64–5")
      == normalize_fig_id("64.5") == "64.5"
      and normalize_fig_id("30 — 44") == "30.44",
      "64-5 / 64–5 / 64.5 / 30 — 44")


# 1b. _validate rejects engine leakage + accepts a clean contract
def _raises(d):
    try:
        fr._validate(d)
        return False
    except RuntimeError:
        return True


_good = {"status": "pass", "match_quality": "exact", "hard_fail": False,
         "file": "x", "fig_id": "1.1", "reason": "",
         "qc_degraded": False, "qc_skipped": []}
check("_validate accepts clean contract", fr._validate(_good) == _good)
check("_validate rejects leaked match_method", _raises({**_good, "match_method": "raw_xref"}))
check("_validate rejects bad status", _raises({**_good, "status": "ok"}))


# 1c. multipanel caption detector (policy routing for composite figures)
check("multipanel detector: (A)(B)(C)(D)(E) caption → True",
      fr._detect_multipanel_caption(
          "Composite: (A) type 1, (B) type 2, (C) type 3, (D) type 4, (E) type 5"))
check("multipanel detector: panel A and panel B → True",
      fr._detect_multipanel_caption("Figure 4.2 panel A and panel B comparison"))
check("multipanel detector: single (A) reference → False (false-positive guard)",
      not fr._detect_multipanel_caption("A single panel showing (A) the anatomy"))
check("multipanel detector: no panel refs → False",
      not fr._detect_multipanel_caption(
          "A radiograph demonstrating the finding in a representative case"))
check("multipanel detector: empty caption → False",
      not fr._detect_multipanel_caption(""))

if TEST_PDF and Path(TEST_PDF).exists():
    # 2 + 4. correct page → pass, exact, contract-only keys
    r = fr.extract(book=TEST_BOOK, fig_id=TEST_FIG_ID,
                   caption=TEST_CAPTION, out=OUT, pdf=TEST_PDF, page=TEST_PAGE)
    check("entrypoint returns ONLY contract keys", set(r.keys()) == CONTRACT_KEYS,
          f"extra={set(r.keys())-CONTRACT_KEYS} missing={CONTRACT_KEYS-set(r.keys())}")
    check("no engine leakage (match_method/attempts/bbox absent)",
          not ({"match_method", "attempts", "bbox"} & set(r.keys())))
    check("correct page → status pass + match_quality exact",
          r.get("status") == "pass" and r.get("match_quality") == "exact",
          f"got status={r.get('status')} quality={r.get('match_quality')}")
    OUT.unlink(missing_ok=True)
    OUT.with_suffix(".geo.jpeg").unlink(missing_ok=True)

    # 3. far-off page → hard-fail (no silent wrong raster; sweep can't help).
    # Must be >= FAR_OFF_MIN_DIST pages away — the extractor auto-sweeps ±1
    # neighbor pages, so a page only a few pages off (on a short document)
    # could land inside that sweep and wrongly pass.
    far_page = _far_off_page(TEST_PDF, TEST_PAGE)
    if far_page is None:
        skip("far-off page → status fail + hard_fail true + no file",
             f"document too short to place a page >= {FAR_OFF_MIN_DIST} "
             f"pages from TEST_PAGE={TEST_PAGE} in either direction")
    else:
        r2 = fr.extract(book=TEST_BOOK, fig_id=TEST_FIG_ID, caption=TEST_CAPTION,
                        out=OUT, pdf=TEST_PDF, page=far_page)
        check("far-off page → status fail + hard_fail true + no file",
              r2.get("status") == "fail" and r2.get("hard_fail") is True
              and r2.get("file") is None,
              f"got status={r2.get('status')} hard_fail={r2.get('hard_fail')} file={r2.get('file')} far_page={far_page}")
        OUT.unlink(missing_ok=True)
        OUT.with_suffix(".geo.jpeg").unlink(missing_ok=True)

    # 5. neighbor sweep: off-by-one page should still pass (sweeps back to TEST_PAGE)
    r3 = fr.extract(book=TEST_BOOK, fig_id=TEST_FIG_ID, caption=TEST_CAPTION,
                    out=OUT, pdf=TEST_PDF, page=TEST_PAGE - 1 if TEST_PAGE > 1 else TEST_PAGE + 1)
    check("off-by-one page → neighbor sweep recovers (status pass)",
          r3.get("status") == "pass" and r3.get("match_quality") == "exact"
          and r3.get("file") is not None,
          f"got status={r3.get('status')} quality={r3.get('match_quality')} file={r3.get('file')}")
    OUT.unlink(missing_ok=True)
    OUT.with_suffix(".geo.jpeg").unlink(missing_ok=True)
else:
    for n in ("entrypoint returns ONLY contract keys", "no engine leakage",
              "correct page → status pass + match_quality exact",
              "far-off page → status fail + hard_fail true + no file",
              "off-by-one page → neighbor sweep recovers (status pass)"):
        skip(n, "T2N_TEST_PDF not set or file missing")

# internal gate CLI is guarded
proc = subprocess.run(
    [sys.executable, str(HERE / "figure_qc_gate.py"), "gate",
     "--book", "x", "--fig-id", "1.1", "--caption", "c", "--out", str(OUT)],
    capture_output=True, text=True,
    env={**os.environ, "FIGURE_REMAP_ALLOW_GATE": ""},
)
check("internal gate CLI guarded (exit 2 + redirect msg)",
      proc.returncode == 2 and "figure_remap.py extract" in proc.stderr,
      f"rc={proc.returncode} stderr={proc.stderr[:80]!r}")

# ─── report ───
fails = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}[status]
    print(f"  {mark} [{status}] {name}" + (f"  ({detail})" if detail and status != 'PASS' else ""))
print(f"--- {sum(r[1]=='PASS' for r in results)} pass, "
      f"{len(fails)} fail, {sum(r[1]=='SKIP' for r in results)} skip ---")
sys.exit(1 if fails else 0)
