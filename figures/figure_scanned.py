"""figure_scanned.py — Scanned-PDF backend for the figure-remap pipeline.

This is the caption-anchor extraction backend. It is invoked by figure_qc_gate.gate()
when _select_backend() picks "caption_anchor" (typically scanned PDFs — e.g. a
reprinted older edition scanned to PDF: 1 raster per page, OCR-overlay text, no
separable figure xrefs).

Algorithm:
  1. fitz finds all caption text bboxes on the page using CAPTION_RE.
  2. Apply Class A OCR substitution normalization (l→1, S→5, O→0) silently.
  3. Build CaptionMatch objects per caption; expand into alias sets.
  4. Look up target fig_id across all caption alias sets:
       - 0 matches → L1_no_caption_match (hard_fail)
       - ≥2 matches → L1_target_matches_multiple_captions (hard_fail)
       - 1 match → proceed
  5. Determine direction (page-global vote in Phase 1; per-column in Phase 2).
  6. Compute figure region = area opposite the caption, bounded by prev/next
     caption in same column, clipped to column bounds.
  7. Render page at 200 dpi (cache by pdf_sha1).
  8. Crop to region; run tighten_within() to trim whitespace + minor text bleed.
  9. Run scanned QC chain (whitespace fill only — fitz text_bleed is meaningless
     for scanned PDFs because OCR overlay covers the whole page).
 10. On hard_fail: write <out>.fail.json with policy_version + observations.

Phase 1 deliverable. L2/L3 guards + per-column direction inference + Class B
ambiguity flagging deferred to Phase 2.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

POLICY_VERSION = "1.2"

# Caption regex — handles dash variants + lowercase 'l' that OCR mangled '1' as
_DASHES = "-‒–—―"
CAPTION_RE = re.compile(
    r"\s*(?:FIG(?:URE)?\.?|Figure|Fig\.?)\s*"
    r"(\d+\s*[" + _DASHES + r"\.]\s*[lISO\d]+[A-Za-z]?)",
    re.IGNORECASE,
)

# Tunable thresholds (Phase 1 only uses a subset)
SCANNED_COVERAGE_MIN = 0.92
SCANNED_TEXT_BLOCK_MAX = 6
ASSIGNABLE_RASTER_MIN = 2
INK_FILL_MIN_FOR_TIGHTEN = 0.15
ROW_THRESH_ANATOMY = 0.025
ROW_THRESH_DEFAULT = 0.10


# ─── Class A OCR substitution normalization ──────────────────────────────────

def _ocr_class_a_normalize(raw: str) -> str:
    """Apply silent, deterministic Class A substitutions.

    These are 1-to-1 with no semantic ambiguity in fig-id context:
      lowercase 'l' / uppercase 'I' → '1'
      uppercase 'S' → '5'  (capital O → '0')
    Class B (B↔8, D↔0 in suffix position) is NOT handled here — that needs
    ambiguity flagging which Phase 2 introduces.
    """
    s = raw
    # Only inside numeric portions: keep trailing letter as-is (it is the
    # sub-letter A/B/C/D). Walk char-by-char with simple positional rule.
    out = []
    for i, ch in enumerate(s):
        if ch in ("l", "I"):
            # Substitute to '1' if neighbours are digits or separators (the
            # OCR confusion only happens inside the numeric id)
            if i + 1 < len(s) and (s[i+1].isdigit() or s[i+1] in "-.–—"):
                out.append("1")
                continue
            if out and (out[-1].isdigit() or out[-1] in "-.–—"):
                out.append("1")
                continue
        if ch == "S":
            if out and (out[-1].isdigit() or out[-1] in "-.–—"):
                out.append("5")
                continue
        if ch == "O":
            if out and (out[-1].isdigit() or out[-1] in "-.–—"):
                out.append("0")
                continue
        out.append(ch)
    return "".join(out)


def normalize_fig_id(raw: str) -> str:
    """Canonicalize a figure id: dash variants → '.', strip whitespace, lowercase.

    Phase 1: applies Class A substitution too so '4-lD' → '4.1d' matches '4-1D'.
    """
    s = _ocr_class_a_normalize(raw.strip())
    for d in _DASHES:
        s = s.replace(d, ".")
    s = re.sub(r"\s+", "", s)
    return s.lower()


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaptionMatch:
    """One caption found on a page, with its alias set and bbox (PDF points)."""
    raw_text: str
    aliases: frozenset[str]
    ambiguity: bool                # Phase 2: True iff Class B token expanded
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class BackendDecision:
    backend: Literal["geometric", "caption_anchor"]
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PageDerived:
    """Single cached page artifact (Phase 1 — Phase 3 will split raw/derived).

    Cache key: (pdf_sha1, page_idx, dpi, policy_version). Cache stored in
    process memory only — cheap enough that disk persistence isn't needed
    for Phase 1.
    """
    pdf_sha1: str
    page_idx: int
    dpi: int
    policy_version: str
    pw_pts: float
    ph_pts: float
    render_path: Path
    captions: tuple[CaptionMatch, ...]
    columns: tuple[tuple[float, float], ...]   # (x0, x1) per column
    backend_decision: BackendDecision


# In-process cache. Workflows usually iterate within one process; no need for
# cross-process persistence in Phase 1.
_PAGE_CACHE: dict[tuple[str, int, int, str], PageDerived] = {}


# ─── PDF SHA + render cache ──────────────────────────────────────────────────

def _pdf_sha1_short(pdf_path: str) -> str:
    """First 12 hex chars of the PDF sha1. Cached per path."""
    h = hashlib.sha1()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _render_cache_dir(out_path: Path) -> Path:
    d = out_path.parent / "_render_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_page(pdf_path: str, page_idx: int, out_path: Path,
                 dpi: int = 200, pdf_sha1: str | None = None) -> Path:
    """Render the page at the given dpi to a cached PNG. Returns its path.

    Cache key: <pdf_sha1[:12]>_pg<N>_<dpi>dpi.png inside _render_cache/.
    """
    import fitz
    sha = pdf_sha1 or _pdf_sha1_short(pdf_path)
    render_path = _render_cache_dir(out_path) / f"{sha}_pg{page_idx}_{dpi}dpi.png"
    if render_path.exists():
        return render_path
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    pix.save(str(render_path))
    doc.close()
    return render_path


# ─── Backend selection (capability-based) ────────────────────────────────────

def _covers_most_of_page(bb, pw, ph, threshold=0.92) -> bool:
    if not bb:
        return False
    area = (bb[2] - bb[0]) * (bb[3] - bb[1])
    return area / (pw * ph) >= threshold


def _select_backend(page) -> BackendDecision:
    """Capability-based backend selection. Operates on raw fitz page.

    Raster dominance is primary; fonts modify confidence ONLY (never flip the
    decision).

    Three structural categories:
      1. Pure scanned: 1 near-full-page raster, no others, sparse text.
         → caption_anchor.
      2. Hybrid scan: near-full-page raster (page background) PLUS smaller
         embedded rasters (digitized figures overlaid on the scan). Common
         in re-published/reprinted scanned textbooks. The smaller rasters
         look 'assignable' but the surrounding OCR text contaminates
         geometric's text_bleed QC.
         → caption_anchor.
      3. Pure born-digital: no near-full-page raster, multiple distinct
         smaller rasters.
         → geometric.
    """
    pw, ph = page.rect.width, page.rect.height
    imgs = list(page.get_image_info(xrefs=True))
    raster_bboxes = [im.get("bbox") for im in imgs if im.get("bbox")]
    has_fullpage_raster = any(_covers_most_of_page(bb, pw, ph) for bb in raster_bboxes)
    assignable = [bb for bb in raster_bboxes
                  if not _covers_most_of_page(bb, pw, ph)]
    fonts = page.get_fonts() if hasattr(page, "get_fonts") else []
    has_fonts = len(fonts) > 0
    text_blocks = [b for b in page.get_text("blocks") if (b[6] if len(b) > 6 else 0) == 0]
    text_block_count = len(text_blocks)

    # Category 2: hybrid-scan (page-bg raster + embedded figures). Route
    # to caption_anchor regardless of assignable count — the OCR-overlay
    # text on the scanned background will fail geometric's text_bleed QC.
    if has_fullpage_raster and len(assignable) > 0:
        return BackendDecision("caption_anchor", 0.92,
                               (f"hybrid_scan",
                                f"assignable={len(assignable)}",
                                f"fonts={len(fonts)}"))
    # Category 3: ≥2 separable rasters, no fullpage background → geometric
    if len(assignable) >= ASSIGNABLE_RASTER_MIN and not has_fullpage_raster:
        return BackendDecision("geometric", 0.95,
                               (f"assignable={len(assignable)}",))
    # Born-digital text page with one inline image and many text blocks →
    # still geometric (fonts MODIFY confidence, don't flip the decision)
    if (text_block_count >= 10 and has_fonts and not has_fullpage_raster):
        return BackendDecision("geometric", 0.75,
                               (f"assignable={len(assignable)}",
                                f"text_blocks={text_block_count}",
                                f"fonts={len(fonts)}"))
    # Category 1: pure scanned (or default fallback)
    return BackendDecision("caption_anchor", 0.9,
                           (f"assignable={len(assignable)}",
                            f"text_blocks={text_block_count}",
                            f"fonts={len(fonts)}"))


# ─── Caption discovery ───────────────────────────────────────────────────────

def _find_captions(page) -> tuple[CaptionMatch, ...]:
    """Primary: walk get_text('dict') for Fig X-Y captions.

    Phase 1: alias set always single element (no Class B expansion). Class B
    arrives in Phase 2 with ambiguity=True flagging.
    """
    captions: list[CaptionMatch] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans)
            m = CAPTION_RE.search(text)
            if not m or m.start() > 10 or len(text) > 200:
                continue
            # Reject inline references like "Anatomy (Fig. 4-2A)" — a real
            # caption line starts with FIG/Figure/Fig (possibly after a small
            # bullet/glyph prefix), not after a word.
            prefix = text[:m.start()]
            if any(c.isalpha() for c in prefix):
                continue
            norm = normalize_fig_id(m.group(1))
            bbox = line.get("bbox") or spans[0].get("bbox")
            if not bbox:
                continue
            captions.append(CaptionMatch(
                raw_text=text[:80].strip(),
                aliases=frozenset({norm}),
                ambiguity=False,
                bbox=tuple(bbox),
            ))
    return tuple(captions)


def _find_captions_recovery(page) -> tuple[CaptionMatch, ...]:
    """Fallback: regex over get_text('blocks') joined text when dict-walk
    finds 0 captions. Mitigates OCR-broken pages where line-level parsing
    misses captions split across spans.
    """
    captions: list[CaptionMatch] = []
    for b in page.get_text("blocks"):
        text = b[4] if len(b) > 4 else ""
        block_type = b[6] if len(b) > 6 else 0
        if block_type != 0:
            continue
        # Each line in the block might contain a caption
        for line in text.splitlines():
            m = CAPTION_RE.search(line)
            if not m or m.start() > 10 or len(line) > 200:
                continue
            norm = normalize_fig_id(m.group(1))
            bbox = tuple(b[:4])
            captions.append(CaptionMatch(
                raw_text=line[:80].strip(),
                aliases=frozenset({norm}),
                ambiguity=False,
                bbox=bbox,
            ))
    return tuple(captions)


# ─── Page analysis (cached) ──────────────────────────────────────────────────

def analyze_page(pdf_path: str, page_idx: int, out_path: Path,
                 dpi: int = 200) -> PageDerived:
    """Return cached PageDerived for (pdf_sha1, page_idx, dpi, policy_version)."""
    sha = _pdf_sha1_short(pdf_path)
    key = (sha, page_idx, dpi, POLICY_VERSION)
    if key in _PAGE_CACHE:
        return _PAGE_CACHE[key]
    import fitz
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    pw, ph = page.rect.width, page.rect.height
    backend = _select_backend(page)
    captions = _find_captions(page)
    if not captions:
        captions = _find_captions_recovery(page)
    doc.close()
    # 2-column split is the common case in textbooks; for full-width pages
    # this still works because one of the columns will simply be empty.
    columns = ((0.0, pw / 2.0), (pw / 2.0, pw))
    render_path = _render_page(pdf_path, page_idx, out_path, dpi=dpi, pdf_sha1=sha)
    derived = PageDerived(
        pdf_sha1=sha, page_idx=page_idx, dpi=dpi,
        policy_version=POLICY_VERSION, pw_pts=pw, ph_pts=ph,
        render_path=render_path, captions=captions, columns=columns,
        backend_decision=backend,
    )
    _PAGE_CACHE[key] = derived
    return derived


# ─── Direction inference ─────────────────────────────────────────────────────

def _infer_direction(captions: tuple[CaptionMatch, ...], ph_pts: float) -> str:
    """Phase 1: page-global direction vote.

    For caption-above books, captions tend to sit in the top half of their
    figure block; for caption-below, the bottom half. Heuristic vote: if the
    median caption Y is in the lower half of the page, captions are likely
    BELOW the figures (common in older reprinted scans). Default 'below' on
    ties.
    """
    if not captions:
        return "below"
    mid = ph_pts / 2.0
    above_count = sum(1 for c in captions if (c.bbox[1] + c.bbox[3]) / 2.0 < mid)
    below_count = len(captions) - above_count
    if below_count >= above_count:
        return "below"
    return "above"


# ─── Region computation ──────────────────────────────────────────────────────

def _compute_region_pts(target_caption: CaptionMatch,
                        all_captions: tuple[CaptionMatch, ...],
                        direction: str,
                        column: tuple[float, float],
                        ph_pts: float) -> tuple[float, float, float, float]:
    """Compute the figure region in PDF points (caption-anchor algorithm).

    direction='below' (caption sits below figure):
      region is ABOVE the target caption, bounded by previous caption above
      in same column (or page top).
    direction='above' (caption sits above figure):
      region is BELOW the target caption, bounded by next caption below
      in same column (or page bottom).
    """
    tx0, ty0, tx1, ty1 = target_caption.bbox
    col_x0, col_x1 = column

    # Captions in same column (mid-x falls inside column)
    same_col = []
    for c in all_captions:
        cx_mid = (c.bbox[0] + c.bbox[2]) / 2.0
        if col_x0 <= cx_mid <= col_x1 and c is not target_caption:
            same_col.append(c)

    if direction == "below":
        # Region above target caption; lower bound = caption top.
        y1 = ty0 - 2.0
        # Upper bound: bottom of nearest caption above in same column
        upper_y = 0.0
        for c in same_col:
            cy_mid = (c.bbox[1] + c.bbox[3]) / 2.0
            if cy_mid < ty0 - 2.0:
                upper_y = max(upper_y, c.bbox[3] + 2.0)
        y0 = upper_y
    else:  # "above"
        # Region below target caption; upper bound = caption bottom.
        y0 = ty1 + 2.0
        lower_y = ph_pts
        for c in same_col:
            cy_mid = (c.bbox[1] + c.bbox[3]) / 2.0
            if cy_mid > ty1 + 2.0:
                lower_y = min(lower_y, c.bbox[1] - 2.0)
        y1 = lower_y

    return (col_x0, y0, col_x1, y1)


# ─── Crop + tighten ──────────────────────────────────────────────────────────

def _tighten_within(crop_pil, row_thresh: float = ROW_THRESH_DEFAULT,
                    pad: int = 10):
    """Find figure body within a crop by ink-density. Trims whitespace + minor
    text bleed. Returns (PIL image, ink_fill_ratio). ink_fill_ratio is the
    fraction of the tight-crop area that is non-whitespace — used by the
    region-tighten ambiguity guard (Phase 2 hardens this; Phase 1 just logs).
    """
    try:
        import numpy as np
    except ImportError:
        return (crop_pil, 1.0)
    arr = np.array(crop_pil.convert("RGB"))
    h, w = arr.shape[:2]
    gray = np.mean(arr, axis=2)
    ink = (gray < 220).astype(np.uint8)
    row_density = ink.mean(axis=1)
    row_mask = row_density > row_thresh
    # Close ≤25 px gaps (label spacing within a figure)
    closed = row_mask.copy()
    last_t = -100
    for i in range(h):
        if row_mask[i]:
            if last_t >= 0 and 0 < (i - last_t - 1) <= 25:
                closed[last_t + 1:i] = True
            last_t = i
    runs = []
    i = 0
    while i < h:
        if closed[i]:
            j = i
            while j < h and closed[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return (crop_pil, 0.0)
    r0, r1 = max(runs, key=lambda r: r[1] - r[0])
    if r1 - r0 < 50:
        return (crop_pil, 0.0)
    # Column tighten within the row band
    band = ink[r0:r1]
    col_density = band.mean(axis=0)
    col_mask = col_density > 0.02
    col_runs = []
    i = 0
    while i < w:
        if col_mask[i]:
            j = i
            while j < w and col_mask[j]:
                j += 1
            col_runs.append((i, j))
            i = j
        else:
            i += 1
    if not col_runs:
        return (crop_pil, 0.0)
    merged = [col_runs[0]]
    for s, e in col_runs[1:]:
        if s - merged[-1][1] < 60:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    c0, c1 = max(merged, key=lambda c: c[1] - c[0])
    r0 = max(0, r0 - pad); r1 = min(h, r1 + pad)
    c0 = max(0, c0 - pad); c1 = min(w, c1 + pad)
    tight = crop_pil.crop((c0, r0, c1, r1))
    # Recompute ink fill on the tight area
    tight_arr = np.array(tight.convert("L"))
    fill = float((tight_arr < 220).mean())
    return (tight, fill)


# ─── QC for scanned crops ────────────────────────────────────────────────────

def _run_scanned_qc(crop_path: Path) -> tuple[bool, list[str]]:
    """QC chain for scanned-mode crops. The fitz text_bleed check is skipped
    because OCR-overlay text covers the whole page (would false-fail every
    crop). whitespace_borders is the primary deterministic check.

    Returns (pass, reasons).
    """
    from figure_qc_gate import qc_whitespace_borders
    reasons = []
    ok, msg = qc_whitespace_borders(crop_path)
    reasons.append(f"whitespace:{msg}")
    return (ok, reasons)


# ─── Debug dump ──────────────────────────────────────────────────────────────

# Schema version for fail.json artifact. Bumped on any structural change so
# downstream consumers (regression scripts, future ML failure-corpus tools)
# can stable-parse. See CALIBRATION.md "fail.json schema" for the contract.
FAIL_JSON_SCHEMA_VERSION = "1.0"


def _write_fail_json(out_path: Path, derived: PageDerived,
                     fig_id_target: str, fail_reason: str,
                     direction: str | None = None) -> None:
    """Write <out>.fail.json with policy_version + observations for debug.

    Schema is documented in CALIBRATION.md and locked at
    FAIL_JSON_SCHEMA_VERSION. Top-level fields are guaranteed stable across
    same-major-version policies.
    """
    fail_path = out_path.with_suffix(out_path.suffix + ".fail.json")
    fail_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": FAIL_JSON_SCHEMA_VERSION,
        "policy_version": derived.policy_version,
        "fig_id_target": fig_id_target,
        "fail_reason": fail_reason,
        "page_idx": derived.page_idx,
        "pdf_sha1": derived.pdf_sha1,
        "backend_selected": derived.backend_decision.backend,
        "backend_confidence": derived.backend_decision.confidence,
        "backend_reasons": list(derived.backend_decision.reasons),
        "captions": [
            {"raw": c.raw_text, "aliases": list(c.aliases),
             "ambiguity": c.ambiguity, "bbox": list(c.bbox)}
            for c in derived.captions
        ],
        "columns": [list(c) for c in derived.columns],
        "direction": direction,
        "render_path": str(derived.render_path),
    }
    fail_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")


# ─── Main entry — scanned extraction ─────────────────────────────────────────

def extract_scanned(pdf_path: str, page_idx: int, fig_id: str, caption: str,
                    out_path: Path, dpi: int = 200) -> dict:
    """Scanned-PDF figure extraction. Returns gate()-compatible dict:
      {pass: bool, file?, match_method, fig_id_normalized, hard_fail?, reason?}

    L1 guards (Phase 1):
      - L1_no_caption_match: target fig_id matches 0 captions on page
      - L1_target_matches_multiple_captions: ≥2 captions claim target fig_id
    L2/L3 deferred to Phase 2 (logged but not gated).
    """
    target_norm = normalize_fig_id(fig_id)
    derived = analyze_page(pdf_path, page_idx, out_path, dpi=dpi)

    # L1 — caption identity match
    matched = [c for c in derived.captions if target_norm in c.aliases]
    if len(matched) == 0:
        _write_fail_json(out_path, derived, target_norm,
                         "L1_no_caption_match")
        return {"pass": False, "hard_fail": True, "match_method": "FAIL",
                "fig_id_normalized": target_norm,
                "reason": f"L1_no_caption_match: target {target_norm} not in any caption alias"}
    if len(matched) > 1:
        _write_fail_json(out_path, derived, target_norm,
                         "L1_target_matches_multiple_captions")
        return {"pass": False, "hard_fail": True, "match_method": "FAIL",
                "fig_id_normalized": target_norm,
                "reason": f"L1_target_matches_multiple_captions: {len(matched)} captions claim {target_norm}"}

    cap = matched[0]
    direction = _infer_direction(derived.captions, derived.ph_pts)

    # Pick column owning the caption
    cap_mid = (cap.bbox[0] + cap.bbox[2]) / 2.0
    column = derived.columns[0] if cap_mid < derived.pw_pts / 2.0 else derived.columns[1]
    # Edge case: if caption spans both columns (rare full-width caption),
    # use full page width
    if cap.bbox[0] < derived.columns[0][1] and cap.bbox[2] > derived.columns[1][0]:
        column = (0.0, derived.pw_pts)

    region_pts = _compute_region_pts(cap, derived.captions, direction,
                                     column, derived.ph_pts)
    # Sanity: region must have positive area
    if region_pts[2] - region_pts[0] < 10 or region_pts[3] - region_pts[1] < 10:
        _write_fail_json(out_path, derived, target_norm,
                         "L2_empty_or_sparse_region", direction=direction)
        return {"pass": False, "hard_fail": True, "match_method": "FAIL",
                "fig_id_normalized": target_norm,
                "reason": "region too thin (caption-anchored area degenerate)"}

    # Render → crop → tighten
    from PIL import Image
    page_img = Image.open(derived.render_path)
    scale = dpi / 72.0
    px_bbox = (int(region_pts[0] * scale), int(region_pts[1] * scale),
               int(region_pts[2] * scale), int(region_pts[3] * scale))
    crop = page_img.crop(px_bbox)

    is_anatomy = bool(re.search(r"\bAnatomy\b", caption, re.IGNORECASE))
    row_thresh = ROW_THRESH_ANATOMY if is_anatomy else ROW_THRESH_DEFAULT
    tight, ink_fill = _tighten_within(crop, row_thresh=row_thresh)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tight.save(out_path)

    # Scanned QC
    qc_ok, qc_reasons = _run_scanned_qc(out_path)
    if not qc_ok:
        try:
            out_path.unlink()
        except Exception:
            pass
        _write_fail_json(out_path, derived, target_norm,
                         "scanned_qc_failed:" + ";".join(qc_reasons),
                         direction=direction)
        return {"pass": False, "hard_fail": True, "match_method": "FAIL",
                "fig_id_normalized": target_norm,
                "reason": f"scanned_qc_failed: {qc_reasons}"}

    return {"pass": True, "file": str(out_path),
            "match_method": "caption_anchor",
            "fig_id_normalized": target_norm,
            "direction": direction, "ink_fill": ink_fill}
