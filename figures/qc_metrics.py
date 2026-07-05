"""Per-book QC metrics + verdict logic for figure-remap skill.

Audit philosophy: each book has its own format quirks. We don't try to
make extract_figures.py universal — instead we make audit cross-check
strong enough to catch under-extraction every time, then a per-book
override fixes that book. Three independent ground-truth signals:

  A. PDF-side: scan_image_pages + line-start FIGURE/Table marker count
  B. MD-side : inline `Fig. X.Y` references (separator-tolerant)
  C. Type-mix: figures vs tables ratio in detected captions

Any signal red-flagging → red verdict + diagnostic_dump showing 3 missed
pages so a human can read the format and write the per-book fix.
"""
import re
from collections import Counter
from pathlib import Path


# Verdict thresholds (tunable from playbook insights)
GREEN_REF_RATIO_MIN = 0.80   # caption_count / md_unique_fig_ids
GREEN_NO_IMAGE_MAX = 0.10    # fraction of captions with no image
GREEN_GAP_MAX = 0.10         # fraction of fig_id sequence gaps (5–10% is normal in medical textbooks where tables/sub-figs interleave)
GREEN_PDF_COVERAGE_MIN = 0.70  # caption_count / pdf_fig_marker_count
GREEN_IMG_PAGE_COVERAGE_MIN = 0.60  # pages_with_caption / pdf_image_pages

YELLOW_REF_RATIO_MIN = 0.50
YELLOW_NO_IMAGE_MAX = 0.25
YELLOW_PDF_COVERAGE_MIN = 0.40
YELLOW_IMG_PAGE_COVERAGE_MIN = 0.35

# Books with naturally low figure count (text-heavy, mostly tables) where
# absolute caption_count thresholds matter more than ratios. Marked red if
# we extracted < EXTRACT_FLOOR_FOR_LOWFIG figures AND PDF has > MIN_IMG_PAGES
# image pages — i.e. the book has images but parser didn't catch them.
EXTRACT_FLOOR_LOWFIG = 5
MIN_IMG_PAGES_TO_CARE = 30

# Separator class — same as extract_figures.py
_SEP = r"[\-\.–—]"
_MD_FIG_RE = re.compile(rf"\bFig(?:ure)?\.?\s+(\d+{_SEP}\d+)", re.I)
_MD_REF_TAG_RE = re.compile(rf"<!--\s*REF:\s*Fig(?:ure)?\.?\s*(\d+{_SEP}\d+)", re.I)
# Liberal line-start figure marker — many variants seen in textbooks:
#   "FIGURE 5-42." / "Figure 32.1" / "Fig. 3-2 ..." / "Fig 1" / "FIG. 12 ..."
#   "圖 5-3" / "圖 1" / "Plate 12" / "PLATE 1"
#   "Figure 1A" / "Fig 1a" (letter-suffixed)
# Captures fig_id: digits optionally followed by separator+digits, optionally with letter.
_PDF_FIG_LINE_RE = re.compile(
    rf"(?im)^\s*(?:FIGURE|Figure|figure|Fig|FIG|圖|Plate|PLATE|Photo|Photograph)\.?\s+(\d+(?:{_SEP}\d+)?[A-Za-z]?)\b"
)
_PDF_TBL_LINE_RE = re.compile(
    rf"(?im)^\s*(?:TABLE|Table|table|Tab|TAB|表)\.?\s+(\d+(?:{_SEP}\d+)?[A-Za-z]?)\b"
)


def _norm_fig_id(s: str) -> str:
    """Normalize fig_id for cross-source comparison (en-dash → hyphen, dot → hyphen)."""
    return re.sub(r"[–—\.]", "-", s).strip("-")


def parse_chapter_md_refs(md_dir: Path) -> set:
    """Scan all chapter md files for unique normalized fig_id references.

    Matches both `<!-- REF: Fig X.Y -->` tags and inline `Fig.\\s+X.Y`
    mentions, with separator-tolerant regex (hyphen, dot, en-dash, em-dash).
    """
    fig_ids = set()
    if not md_dir.is_dir():
        return fig_ids
    for md in md_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _MD_REF_TAG_RE.finditer(text):
            fig_ids.add(_norm_fig_id(m.group(1)))
        for m in _MD_FIG_RE.finditer(text):
            fig_ids.add(_norm_fig_id(m.group(1)))
    return fig_ids


def pdf_audit(pdf_path: str) -> dict:
    """Independent PDF-side scan — does NOT use extract_figures.py parser.

    Counts ground-truth signals that any caption parser should agree with:
      - image_pages: pages with raster images
      - fig_marker_pages: pages where a line starts with FIGURE/Figure/Fig./FIG.
      - table_marker_pages: pages where a line starts with TABLE/Table

    Discrepancy between these counts and parser output → parser bug.
    """
    try:
        import fitz
    except ImportError:
        return {"_error": "fitz not available"}
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {"_error": f"fitz open failed: {e}"}

    image_pages = set()  # raster
    vector_pages = set()  # significant vector drawings (>=50 ops)
    fig_marker_pages = set()
    table_marker_pages = set()
    fig_marker_count = 0
    table_marker_count = 0
    fig_marker_samples = []  # (page_1idx, snippet) — for diagnostic dump
    total_pages = len(doc)
    # Vector-drawing threshold: page borders / table grids typically <30
    # operations; real charts/diagrams have hundreds. 50 catches small line
    # drawings while filtering ornamental borders.
    VECTOR_DRAWING_THRESHOLD = 50

    for pg_idx in range(total_pages):
        page = doc[pg_idx]
        if page.get_images():
            image_pages.add(pg_idx)
        try:
            if len(page.get_drawings()) >= VECTOR_DRAWING_THRESHOLD:
                vector_pages.add(pg_idx)
        except Exception:
            pass
        try:
            text = page.get_text()
        except Exception:
            continue
        for m in _PDF_FIG_LINE_RE.finditer(text):
            fig_marker_count += 1
            fig_marker_pages.add(pg_idx)
            if len(fig_marker_samples) < 8:
                snippet = text[m.start():m.start() + 200].replace("\n", " | ")
                fig_marker_samples.append((pg_idx + 1, snippet[:200]))
        for m in _PDF_TBL_LINE_RE.finditer(text):
            table_marker_count += 1
            table_marker_pages.add(pg_idx)

    doc.close()
    # "Figure-bearing pages" = raster ∪ significant vectors ∪ caption-marker pages.
    # Any one of three independent signals: AMA Guides uses vector charts that
    # raster scan misses; some scanned books have figures as one whole-page
    # raster but are detected only via the "Figure X-Y" text marker; line-start
    # marker = explicit author intent regardless of how the visual is encoded.
    figure_bearing_pages = image_pages | vector_pages | fig_marker_pages
    return {
        "pdf_total_pages": total_pages,
        "pdf_image_pages": len(image_pages),  # raster only
        "pdf_vector_pages": len(vector_pages),
        "pdf_figure_bearing_pages": len(figure_bearing_pages),
        "pdf_fig_marker_count": fig_marker_count,
        "pdf_fig_marker_pages": len(fig_marker_pages),
        "pdf_table_marker_count": table_marker_count,
        "pdf_table_marker_pages": len(table_marker_pages),
        "_image_pages_set": figure_bearing_pages,  # use union for downstream coverage check
        "_raster_pages_set": image_pages,
        "_vector_pages_set": vector_pages,
        "_fig_marker_pages_set": fig_marker_pages,
        "_fig_marker_samples": fig_marker_samples,
    }


def compute_metrics(dryrun_figures: list, md_dir: Path,
                    pdf_audit_result: dict | None = None,
                    skipped_count: int = 0) -> dict:
    """Compute per-book quality metrics from a dry-run figures list.

    Args:
        dryrun_figures: list of figure dicts from extract_figures.py
        md_dir: chapter md directory for this book
        pdf_audit_result: optional output of pdf_audit() — adds PDF-side signals
        skipped_count: extract_figures stats['skipped'] — non-figure (table) caps
    """
    by_id = {}
    page_id_count = 0
    empty_caption_count = 0
    for f in dryrun_figures:
        fid = str(f.get("fig_id", ""))
        cap = (f.get("caption") or f.get("caption_full") or "").strip()
        if fid.startswith("page_"):
            page_id_count += 1
        if not cap:
            empty_caption_count += 1
        by_id.setdefault(_norm_fig_id(fid), []).append(f)

    caption_count = len(by_id)
    image_count = len(dryrun_figures)
    # dump_all signature: fig_id like `page_NN`. Empty captions alone are NOT a dump_all
    # signal — atlases with marker-only PDF blocks legitimately have empty captions
    # while still having proper Fig_X-Y fig_ids.
    dump_all_ratio = page_id_count / max(1, image_count)
    multipart = sum(1 for fid, items in by_id.items()
                    if any("region_clip" in (it.get("render", "")) for it in items))
    cross_page = sum(1 for fid, items in by_id.items() if len(items) > 1)
    multipart_ratio = multipart / max(1, caption_count)
    cross_page_ratio = cross_page / max(1, caption_count)

    # MD-side reference comparison
    md_fig_ids = parse_chapter_md_refs(md_dir)
    extracted_ids = set(by_id.keys())
    if md_fig_ids:
        ref_ratio = caption_count / len(md_fig_ids)
        # Coverage: of the ids referenced in MD, how many did we extract?
        md_covered = len(extracted_ids & md_fig_ids)
        md_coverage = md_covered / len(md_fig_ids)
    else:
        ref_ratio = None
        md_coverage = None

    # Sequence gaps within each chapter
    chapters = {}
    for fid in by_id:
        ch, _, n = fid.partition("-")
        if n.isdigit():
            chapters.setdefault(ch, []).append(int(n))
    total_gaps = 0
    expected_total = 0
    for ch, nums in chapters.items():
        if len(nums) < 2:
            expected_total += len(nums)
            continue
        nums_sorted = sorted(set(nums))
        span = nums_sorted[-1] - nums_sorted[0] + 1
        total_gaps += span - len(nums_sorted)
        expected_total += span
    gap_ratio = total_gaps / max(1, expected_total)

    metrics = {
        "caption_count": caption_count,
        "image_count": image_count,
        "multipart_count": multipart,
        "multipart_ratio": round(multipart_ratio, 3),
        "cross_page_count": cross_page,
        "cross_page_ratio": round(cross_page_ratio, 3),
        "md_unique_fig_refs": len(md_fig_ids) if md_fig_ids else None,
        "ref_ratio": round(ref_ratio, 3) if ref_ratio is not None else None,
        "md_coverage": round(md_coverage, 3) if md_coverage is not None else None,
        "id_gap_count": total_gaps,
        "id_gap_ratio": round(gap_ratio, 3),
        "skipped_table_count": skipped_count,
        "dump_all_ratio": round(dump_all_ratio, 3),
        "page_id_count": page_id_count,
        "empty_caption_count": empty_caption_count,
    }

    # PDF-side signals (signal A + C)
    if pdf_audit_result and "_error" not in pdf_audit_result:
        pa = pdf_audit_result
        metrics["pdf_total_pages"] = pa["pdf_total_pages"]
        metrics["pdf_image_pages"] = pa["pdf_image_pages"]  # raster only
        metrics["pdf_vector_pages"] = pa.get("pdf_vector_pages", 0)
        metrics["pdf_figure_bearing_pages"] = pa.get("pdf_figure_bearing_pages", pa["pdf_image_pages"])
        metrics["pdf_fig_marker_count"] = pa["pdf_fig_marker_count"]
        metrics["pdf_fig_marker_pages"] = pa["pdf_fig_marker_pages"]
        metrics["pdf_table_marker_count"] = pa["pdf_table_marker_count"]

        # Coverage signal A: how much of PDF's figure markers did parser catch?
        if pa["pdf_fig_marker_count"] > 0:
            pdf_caption_coverage = caption_count / pa["pdf_fig_marker_count"]
        else:
            pdf_caption_coverage = None
        metrics["pdf_caption_coverage"] = (
            round(pdf_caption_coverage, 3) if pdf_caption_coverage is not None else None)

        # Coverage signal: how many image-bearing pages had a caption nearby?
        # Approximation: union of pages where parser found captions vs PDF image pages
        caption_pages = {(f.get("pdf_page") or 0) - 1
                         for f in dryrun_figures
                         if (f.get("pdf_page") or 0) > 0}
        if pa["_image_pages_set"]:
            covered_img_pages = caption_pages & pa["_image_pages_set"]
            img_page_coverage = len(covered_img_pages) / len(pa["_image_pages_set"])
        else:
            img_page_coverage = None
        metrics["img_page_coverage"] = (
            round(img_page_coverage, 3) if img_page_coverage is not None else None)

        # Type ratio signal C
        total_typed = caption_count + skipped_count
        if total_typed > 10:
            fig_to_table_ratio = caption_count / max(1, skipped_count)
            metrics["fig_to_table_ratio"] = round(fig_to_table_ratio, 3)
        else:
            metrics["fig_to_table_ratio"] = None

    return metrics


def verdict(metrics: dict, no_image_ratio: float = 0.0) -> tuple:
    """Decide green/yellow/red and produce a list of reasons.

    Multi-signal: any single signal can red-flag. The most common reason
    each catches:
      - ref_ratio: parser missed many captions vs MD references
      - pdf_caption_coverage: parser missed captions vs PDF line-start markers
      - img_page_coverage: captions don't cover most image-bearing pages
      - fig_to_table_ratio: parser misclassified most figs as tables
      - no_image_ratio: caption matched but no image — orphan caption
    """
    reasons = []
    color = "green"

    if metrics["caption_count"] == 0:
        return ("red", ["zero_captions_detected"])

    # Signal: dump_all fallback detection (extract_figures bailed and dumped raw images)
    if metrics.get("dump_all_ratio", 0) >= 0.5:
        return ("red", [
            f"dump_all fallback detected — page_id={metrics.get('page_id_count',0)}, "
            f"empty_caption={metrics.get('empty_caption_count',0)} of {metrics.get('image_count','?')} figs. "
            f"Extract_figures couldn't parse any caption and dumped images by page number."])

    def _bump(new_color):
        nonlocal color
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[new_color] > order[color]:
            color = new_color

    # Signal: MD ref ratio
    if metrics.get("ref_ratio") is not None:
        rr = metrics["ref_ratio"]
        if rr < YELLOW_REF_RATIO_MIN:
            _bump("red")
            reasons.append(f"ref_ratio={rr:.2f} < {YELLOW_REF_RATIO_MIN} — many captions missed vs MD refs")
        elif rr < GREEN_REF_RATIO_MIN:
            _bump("yellow")
            reasons.append(f"ref_ratio={rr:.2f} below green threshold {GREEN_REF_RATIO_MIN}")

    # Signal: PDF-side caption coverage
    if metrics.get("pdf_caption_coverage") is not None:
        pcov = metrics["pdf_caption_coverage"]
        if pcov < YELLOW_PDF_COVERAGE_MIN:
            _bump("red")
            reasons.append(f"pdf_caption_coverage={pcov:.2f} < {YELLOW_PDF_COVERAGE_MIN} — parser missed line-start FIGURE markers")
        elif pcov < GREEN_PDF_COVERAGE_MIN:
            _bump("yellow")
            reasons.append(f"pdf_caption_coverage={pcov:.2f} below green threshold {GREEN_PDF_COVERAGE_MIN}")

    # Signal: image-page coverage (how many image pages had a caption)
    if metrics.get("img_page_coverage") is not None and metrics.get("pdf_figure_bearing_pages", metrics.get("pdf_image_pages", 0)) > MIN_IMG_PAGES_TO_CARE:
        icov = metrics["img_page_coverage"]
        if icov < YELLOW_IMG_PAGE_COVERAGE_MIN:
            _bump("red")
            reasons.append(f"img_page_coverage={icov:.2f} < {YELLOW_IMG_PAGE_COVERAGE_MIN} — most image pages have no caption")
        elif icov < GREEN_IMG_PAGE_COVERAGE_MIN:
            _bump("yellow")
            reasons.append(f"img_page_coverage={icov:.2f} below green threshold {GREEN_IMG_PAGE_COVERAGE_MIN}")

    # Signal: fig-to-table ratio (atlas/textbook should have more figs than tables)
    if metrics.get("fig_to_table_ratio") is not None:
        ftr = metrics["fig_to_table_ratio"]
        if ftr < 0.20 and metrics.get("pdf_figure_bearing_pages", metrics.get("pdf_image_pages", 0)) > MIN_IMG_PAGES_TO_CARE:
            _bump("red")
            reasons.append(f"fig_to_table_ratio={ftr:.2f} — parser found mostly tables, suspected figure misclassification")

    # Signal: floor check for low-extraction books with images
    if (metrics["caption_count"] < EXTRACT_FLOOR_LOWFIG
            and metrics.get("pdf_figure_bearing_pages", metrics.get("pdf_image_pages", 0)) > MIN_IMG_PAGES_TO_CARE):
        _bump("red")
        reasons.append(f"caption_count={metrics['caption_count']} but pdf_image_pages={metrics['pdf_image_pages']} — under-extraction")

    # Signal: no_image (orphan caption)
    if no_image_ratio > YELLOW_NO_IMAGE_MAX:
        _bump("red")
        reasons.append(f"no_image_ratio={no_image_ratio:.2f} > {YELLOW_NO_IMAGE_MAX}")
    elif no_image_ratio > GREEN_NO_IMAGE_MAX:
        _bump("yellow")
        reasons.append(f"no_image_ratio={no_image_ratio:.2f} above green threshold")

    # Signal: id-sequence gaps
    if metrics["id_gap_ratio"] > GREEN_GAP_MAX:
        _bump("yellow")
        reasons.append(f"id_gap_ratio={metrics['id_gap_ratio']:.2f} — fig_id sequence has gaps")

    return (color, reasons)


def diagnostic_dump(metrics: dict, pdf_audit_result: dict | None,
                    md_dir: Path | None = None) -> str:
    """Render a human-readable diagnostic block for red/yellow verdicts.

    Includes 3 sample line-start FIGURE snippets from the PDF so the user
    can see the caption format the parser missed and write a per-book fix.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("DIAGNOSTIC")
    lines.append("=" * 60)

    if pdf_audit_result and "_error" not in pdf_audit_result:
        pa = pdf_audit_result
        lines.append(f"PDF total pages       : {pa['pdf_total_pages']}")
        lines.append(f"PDF raster image pages: {pa['pdf_image_pages']}")
        lines.append(f"PDF vector pages (>=50 ops): {pa.get('pdf_vector_pages', 0)}")
        lines.append(f"PDF figure-bearing pages (raster | vector): {pa.get('pdf_figure_bearing_pages', pa['pdf_image_pages'])}")
        lines.append(f"PDF FIGURE markers    : {pa['pdf_fig_marker_count']} (on {pa['pdf_fig_marker_pages']} pages)")
        lines.append(f"PDF TABLE markers     : {pa['pdf_table_marker_count']} (on {pa['pdf_table_marker_pages']} pages)")

    lines.append(f"Parser caption_count  : {metrics.get('caption_count', '?')}")
    lines.append(f"Parser skipped tables : {metrics.get('skipped_table_count', '?')}")
    lines.append(f"Parser image_count    : {metrics.get('image_count', '?')}")

    if metrics.get("md_unique_fig_refs") is not None:
        lines.append(f"MD inline fig refs    : {metrics['md_unique_fig_refs']}")
    if metrics.get("ref_ratio") is not None:
        lines.append(f"ref_ratio             : {metrics['ref_ratio']}")
    if metrics.get("pdf_caption_coverage") is not None:
        lines.append(f"pdf_caption_coverage  : {metrics['pdf_caption_coverage']}")
    if metrics.get("img_page_coverage") is not None:
        lines.append(f"img_page_coverage     : {metrics['img_page_coverage']}")
    if metrics.get("fig_to_table_ratio") is not None:
        lines.append(f"fig_to_table_ratio    : {metrics['fig_to_table_ratio']}")

    # Sample FIGURE snippets — what the PDF actually has
    if pdf_audit_result and pdf_audit_result.get("_fig_marker_samples"):
        lines.append("")
        lines.append("Sample line-start FIGURE markers from PDF:")
        for pg, snippet in pdf_audit_result["_fig_marker_samples"][:5]:
            lines.append(f"  p{pg}: {snippet}")
        lines.append("")
        lines.append("Compare with parser's caption_count above. If PDF has many")
        lines.append("FIGURE markers but parser caught few, the caption format")
        lines.append("doesn't match the current CAPTION_PATTERNS in extract_figures.py.")
        lines.append("Common causes: multi-line block (marker on line 1, caption")
        lines.append("on line 2+), unusual separators, garbled fonts.")
        lines.append("→ Write per-book quirk in playbook + override in extract_figures.")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 3:
        print("usage: qc_metrics.py <stats.json> <md_dir> [pdf_path]")
        sys.exit(1)
    stats = json.load(open(sys.argv[1], encoding="utf-8"))
    md_dir = Path(sys.argv[2])
    pa = None
    if len(sys.argv) >= 4:
        pa = pdf_audit(sys.argv[3])
    skipped = stats.get("skipped", 0)
    m = compute_metrics(stats.get("figures", []), md_dir, pa, skipped)
    no_img = stats.get("no_image", 0)
    extracted = stats.get("extracted", 0)
    nir = no_img / max(1, no_img + extracted)
    color, reasons = verdict(m, nir)
    out = {"metrics": m, "no_image_ratio": round(nir, 3),
           "verdict": color, "reasons": reasons}
    print(json.dumps(out, indent=2, default=str))
    if color != "green" and pa is not None:
        print()
        print(diagnostic_dump(m, pa, md_dir))
