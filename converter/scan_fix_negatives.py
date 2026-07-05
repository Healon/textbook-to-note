"""
Scan all extracted textbook figures for color-inverted (negative) images and fix them.

Detection methods:
1. CMYK JPEG: PIL opens as mode "CMYK" → definitely inverted when viewed as RGB
2. Inverted Decode array: check source PDF xref for /Decode [1 0 ...]
3. Histogram heuristic: abnormally dark image (mean brightness < 40) for what should be
   a textbook figure → likely inverted

Fix: re-extract from source PDF using fitz.Pixmap with proper color space conversion.

Usage:
    # Scan only (report without fixing)
    python scan_fix_negatives.py --scan

    # Scan and fix
    python scan_fix_negatives.py --fix

    # Scan a specific book
    python scan_fix_negatives.py --scan --book "MyTextbook_6e_2021"
"""

import argparse
import fitz
import json
import os
import re
import sys
from pathlib import Path

# Windows console (e.g. a Traditional Chinese cp950 locale) defaults stdout to the
# system codepage, not UTF-8 — reconfigure so CJK filenames/output print cleanly.
sys.stdout.reconfigure(encoding='utf-8')

from PIL import Image
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import OUTPUT_DIR


def check_cmyk(img_path: str) -> bool:
    """Check if JPEG is CMYK mode."""
    try:
        with Image.open(img_path) as img:
            return img.mode == "CMYK"
    except Exception:
        return False


def check_dark_histogram(img_path: str, threshold: float = 40.0) -> bool:
    """Check if image is abnormally dark (likely inverted).
    Threshold 40/255 = ~15% brightness — normal textbook figures are mostly white background."""
    try:
        with Image.open(img_path) as img:
            if img.mode == "CMYK":
                return False  # Already caught by CMYK check
            gray = img.convert("L")
            arr = np.array(gray)
            mean_val = arr.mean()
            return mean_val < threshold
    except Exception:
        return False


def find_source_pdf(book_dir: Path) -> str | None:
    """Find source PDF path from first md file's Source: header."""
    md_files = sorted(book_dir.glob("*.md"))
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'Source:\s*`([^`]+\.pdf)`', content, re.IGNORECASE)
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


def find_xref_for_image(doc, fig_file: str, manifest: dict) -> tuple[int, int] | None:
    """Find the PDF xref for a figure file using manifest's pdf_page info."""
    # Look up page from manifest
    for fig in manifest.get("figures", []):
        if fig.get("file") == fig_file:
            page_idx = fig["pdf_page"] - 1  # manifest stores 1-indexed
            # Get images on that page
            images = doc[page_idx].get_images(full=True)
            if images:
                # If multiple images, try to match by position in list
                # For now return first large image
                for img_info in images:
                    if img_info[2] > 100 and img_info[3] > 100:
                        return page_idx, img_info[0]  # (page_idx, xref)
    return None


def fix_cmyk_with_pil(img_path: str) -> bool:
    """Convert CMYK JPEG to RGB PNG using PIL. No source PDF needed."""
    try:
        with Image.open(img_path) as img:
            if img.mode != "CMYK":
                return False
            rgb = img.convert("RGB")
            png_path = str(Path(img_path).with_suffix(".png"))
            rgb.save(png_path, "PNG")
        # Remove old JPEG
        if img_path != png_path:
            os.remove(img_path)
        return True
    except Exception as e:
        print(f"    [ERROR] PIL fix failed for {os.path.basename(img_path)}: {e}")
        return False


def fix_dark_with_invert(img_path: str) -> bool:
    """Invert a dark (negative) image back to normal using PIL."""
    try:
        from PIL import ImageOps
        with Image.open(img_path) as img:
            if img.mode == "CMYK":
                img = img.convert("RGB")
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            inverted = ImageOps.invert(img)
            png_path = str(Path(img_path).with_suffix(".png"))
            inverted.save(png_path, "PNG")
        if img_path != png_path and os.path.exists(img_path):
            os.remove(img_path)
        return True
    except Exception as e:
        print(f"    [ERROR] Invert failed for {os.path.basename(img_path)}: {e}")
        return False


def scan_book(book_dir: Path, do_fix: bool = False, fix_dark: bool = False,
              dark_fix_list: set = None) -> dict:
    """Scan a single book's figures for negatives.

    Args:
        do_fix: Fix CMYK images
        fix_dark: Fix dark images (only those in dark_fix_list if provided)
        dark_fix_list: Set of absolute paths to dark images confirmed as negatives
    """
    fig_dir = book_dir / "figures"
    if not fig_dir.exists():
        return {"book": book_dir.name, "total": 0, "cmyk": 0, "dark": 0, "fixed": 0}

    # Collect all JPEG files (recursively — some books have subdirs per chapter)
    jpegs = list(fig_dir.rglob("*.jpeg")) + list(fig_dir.rglob("*.jpg"))
    if not jpegs:
        return {"book": book_dir.name, "total": 0, "cmyk": 0, "dark": 0, "fixed": 0}

    cmyk_files = []
    dark_files = []

    for jpg in jpegs:
        if check_cmyk(str(jpg)):
            cmyk_files.append(str(jpg))
        elif check_dark_histogram(str(jpg)):
            dark_files.append(str(jpg))

    stats = {
        "book": book_dir.name,
        "total": len(jpegs),
        "cmyk": len(cmyk_files),
        "dark": len(dark_files),
        "cmyk_files": [os.path.relpath(f, OUTPUT_DIR) for f in cmyk_files],
        "dark_files": [os.path.relpath(f, OUTPUT_DIR) for f in dark_files],
        "fixed": 0,
    }

    # Fix CMYK: PIL convert directly
    if do_fix and cmyk_files:
        for f in cmyk_files:
            if fix_cmyk_with_pil(f):
                stats["fixed"] += 1

    # Fix dark: invert only confirmed negatives
    if fix_dark and dark_files:
        for f in dark_files:
            if dark_fix_list and f not in dark_fix_list:
                continue
            if fix_dark_with_invert(f):
                stats["fixed"] += 1

    return stats


def _check_decode_inverted(doc, xref: int) -> str | None:
    """Check if a PDF image xref will produce an inverted (negative) image.

    Returns the inversion reason string, or None if normal.
    Detects three mechanisms:
    1. /Decode [1 0 ...] — explicit inversion in the image dictionary
    2. /Separation color space — ink convention (1=dark) opposite to grayscale (0=dark)
    3. /Indexed + /Separation — indexed palette referencing a Separation space
    """
    try:
        xref_str = doc.xref_object(xref, compressed=False)

        # Check 1: inverted /Decode array
        decode_match = re.search(r'/Decode\s*\[([^\]]+)\]', xref_str)
        if decode_match:
            vals = [float(v) for v in decode_match.group(1).split()]
            if len(vals) >= 2 and vals[0] > vals[1]:
                return "decode_inverted"

        # Check 2 & 3: /Separation or /Indexed→/Separation color space
        cs_match = re.search(r'/ColorSpace\s+(\d+)\s+0\s+R', xref_str)
        if cs_match:
            cs_xref = int(cs_match.group(1))
            cs_str = doc.xref_object(cs_xref, compressed=False)
            if '/Separation' in cs_str:
                return "separation"
            # /Indexed wrapping a /Separation
            if '/Indexed' in cs_str and '/Separation' in cs_str:
                return "indexed_separation"

        # Inline /ColorSpace (not a reference)
        if '/Separation' in xref_str:
            return "separation_inline"

    except Exception:
        pass
    return None


def _collect_source_pdfs(book_dir: Path) -> list[str]:
    """Collect all unique source PDF paths from a book's md files."""
    pdfs = set()
    for md_file in sorted(book_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'Source:\s*`([^`]+\.pdf)`', content, re.IGNORECASE)
            if m and os.path.exists(m.group(1)):
                pdfs.add(m.group(1))
        except Exception:
            continue
    return sorted(pdfs)


def _load_manifests(fig_dir: Path) -> dict:
    """Load all figures_manifest.json files. Returns {dir_path: manifest_data}."""
    manifests = {}
    for mf in fig_dir.rglob("figures_manifest.json"):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            manifests[str(mf.parent)] = data
        except Exception:
            pass
    return manifests


def _find_files_for_page(fig_dir: Path, manifests: dict, pdf_page_1indexed: int) -> list[str]:
    """Find ALL extracted figure files from a given PDF page.

    Returns list of file paths (may be empty).
    """
    found = []
    seen = set()

    # Search all manifests for figures on this page
    for dir_path, manifest in manifests.items():
        for fig in manifest.get("figures", []):
            if fig.get("pdf_page") == pdf_page_1indexed:
                fname = fig["file"]
                base = os.path.splitext(fname)[0]
                # Check all possible extensions
                for existing_ext in [".jpeg", ".jpg", ".png"]:
                    candidate = os.path.join(dir_path, base + existing_ext)
                    if os.path.exists(candidate) and candidate not in seen:
                        found.append(candidate)
                        seen.add(candidate)

    # Fallback: try dump_all naming (page_NNNN[_X].ext)
    for ext in [".jpeg", ".jpg", ".png"]:
        for candidate in fig_dir.rglob(f"page_{pdf_page_1indexed:04d}*{ext}"):
            c = str(candidate)
            if c not in seen:
                found.append(c)
                seen.add(c)

    return found


def scan_decode_inversions(book_dir: Path, do_fix: bool = False) -> dict:
    """Scan source PDFs for inverted /Decode arrays and fix extracted figures.

    This is deterministic and 100% accurate — no heuristics or LLM needed.
    """
    fig_dir = book_dir / "figures"
    if not fig_dir.exists():
        return {"book": book_dir.name, "scanned_xrefs": 0, "inverted": 0, "fixed": 0, "files": []}

    source_pdfs = _collect_source_pdfs(book_dir)
    if not source_pdfs:
        return {"book": book_dir.name, "scanned_xrefs": 0, "inverted": 0, "fixed": 0,
                "files": [], "error": "no_source_pdf"}

    manifests = _load_manifests(fig_dir)
    stats = {"book": book_dir.name, "scanned_xrefs": 0, "inverted": 0, "fixed": 0, "files": []}

    # Collect pages with inverted xrefs, then fix ALL files from those pages
    inverted_pages = {}  # {pdf_path: {page_1indexed: reason}}

    for pdf_path in source_pdfs:
        doc = fitz.open(pdf_path)
        for page_idx in range(len(doc)):
            images = doc[page_idx].get_images(full=True)
            big_images = [img for img in images if img[2] > 100 and img[3] > 100]

            for img_info in big_images:
                xref = img_info[0]
                stats["scanned_xrefs"] += 1

                reason = _check_decode_inverted(doc, xref)
                if reason:
                    pdf_page = page_idx + 1
                    if pdf_path not in inverted_pages:
                        inverted_pages[pdf_path] = {}
                    inverted_pages[pdf_path][pdf_page] = reason
                    stats["inverted"] += 1

        doc.close()

    # Now fix all extracted files from inverted pages
    fixed_files = set()
    for pdf_path, pages in inverted_pages.items():
        doc = fitz.open(pdf_path) if do_fix else None
        for pdf_page, reason in sorted(pages.items()):
            fig_files = _find_files_for_page(fig_dir, manifests, pdf_page)

            for fig_file in fig_files:
                if fig_file in fixed_files:
                    continue  # Don't fix same file twice

                entry = {
                    "pdf": os.path.basename(pdf_path),
                    "page": pdf_page,
                    "reason": reason,
                    "file": os.path.relpath(fig_file, OUTPUT_DIR),
                }
                stats["files"].append(entry)

                if do_fix:
                    # Guard: skip files already fixed in a previous run.
                    # A .jpeg→.png conversion is the fix signature; if only .png
                    # exists (no matching .jpeg), this file was already fixed.
                    orig_jpeg = str(Path(fig_file).with_suffix(".jpeg"))
                    if fig_file.endswith(".png") and not os.path.exists(orig_jpeg):
                        # Check if this PNG was created by a previous fix run
                        # (original JPEG was deleted after fix)
                        print(f"    - p.{pdf_page} [{reason}] skip (already fixed): {os.path.basename(fig_file)}")
                        continue

                    try:
                        if reason in ("separation", "indexed_separation", "separation_inline"):
                            # Separation color space = ink convention (inverted by PDF spec).
                            # Always invert grayscale images; skip color illustrations.
                            try:
                                with Image.open(fig_file) as _img:
                                    _mode = _img.mode
                                    if _mode in ("RGB", "RGBA"):
                                        _arr = np.array(_img.convert("RGB"))
                                        _r, _g, _b = _arr[:,:,0].astype(float), _arr[:,:,1].astype(float), _arr[:,:,2].astype(float)
                                        _max_diff = max(np.mean(np.abs(_r - _g)), np.mean(np.abs(_r - _b)), np.mean(np.abs(_g - _b)))
                                        if _max_diff > 10:
                                            print(f"    - p.{pdf_page} [{reason}] skip (color): {os.path.basename(fig_file)}")
                                            continue
                            except Exception:
                                pass  # Proceed with fix if check fails
                            fix_dark_with_invert(fig_file)
                        else:
                            # /Decode inversion: Pixmap re-extraction
                            # Find matching xref for this page
                            page_idx = pdf_page - 1
                            images = doc[page_idx].get_images(full=True)
                            big_images = [img for img in images if img[2] > 100 and img[3] > 100]
                            if big_images:
                                xref = big_images[0][0]
                                pix = fitz.Pixmap(doc, xref)
                                if pix.n >= 4:
                                    pix = fitz.Pixmap(fitz.csRGB, pix)
                                elif pix.alpha:
                                    pix = fitz.Pixmap(pix, 0)
                                png_path = str(Path(fig_file).with_suffix(".png"))
                                pix.save(png_path)
                                if fig_file != png_path and os.path.exists(fig_file):
                                    os.remove(fig_file)
                        stats["fixed"] += 1
                        fixed_files.add(fig_file)
                        print(f"    ✓ p.{pdf_page} [{reason}] → {os.path.basename(fig_file)}")
                    except Exception as e:
                        print(f"    ✗ p.{pdf_page} [{reason}]: {e}")

            if not fig_files and do_fix:
                print(f"    ? p.{pdf_page} [{reason}]: no extracted file found")

        if doc:
            try:
                doc.close()
            except ValueError:
                pass

    return stats


def verify_dark_with_ollama_vision(dark_files: list[str], batch_size: int = 5) -> list[str]:
    """Use a local ollama vision model to verify which dark images are truly inverted.

    Requires an ollama server running locally with a vision-capable model pulled
    (e.g. `ollama pull gemma3:4b`). Returns list of file paths confirmed as inverted.
    """
    import base64
    import requests

    model = os.environ.get("OLLAMA_VISION_MODEL", "gemma3:4b")
    confirmed = []
    total = len(dark_files)

    for i, img_path in enumerate(dark_files):
        try:
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            # Determine mime type
            ext = Path(img_path).suffix.lower()
            mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

            resp = requests.post("http://localhost:11434/api/chat", json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Look at this image carefully. Is it color-inverted (a photographic negative)? "
                        "Signs of inversion: WHITE lines/text on BLACK background in a diagram or "
                        "anatomical drawing (normal textbook figures have BLACK lines on WHITE background). "
                        "Also inverted: photos where skin/objects appear as bright-on-dark negatives. "
                        "NOT inverted (naturally dark, leave alone): ultrasound, X-ray, MRI, CT, "
                        "EMG waveforms, fluoroscopy, dark-background charts/graphs. "
                        "Answer ONLY 'yes' or 'no'."
                    ),
                    "images": [img_b64],
                }],
                "stream": False,
            }, timeout=60)

            answer = resp.json()["message"]["content"].strip().lower()
            is_negative = answer.startswith("yes")

            status = "NEGATIVE" if is_negative else "ok"
            print(f"  [{i+1}/{total}] {os.path.basename(img_path)}: {status} ({answer[:30]})")

            if is_negative:
                confirmed.append(img_path)

        except Exception as e:
            print(f"  [{i+1}/{total}] {os.path.basename(img_path)}: ERROR ({e})")

    return confirmed


def main():
    parser = argparse.ArgumentParser(description="Scan/fix negative (color-inverted) textbook figures")
    parser.add_argument("--scan", action="store_true", help="Scan and report (default)")
    parser.add_argument("--fix", action="store_true", help="Fix CMYK images (PIL CMYK→RGB)")
    parser.add_argument("--check-decode", action="store_true",
                        help="Scan source PDFs for inverted /Decode arrays (deterministic, fast)")
    parser.add_argument("--fix-decode", action="store_true",
                        help="Scan AND fix inverted /Decode images via Pixmap re-extraction")
    parser.add_argument("--verify-dark", action="store_true",
                        help="Use a local ollama vision model to verify dark images, then fix confirmed negatives")
    parser.add_argument("--book", help="Process only this book directory name")
    parser.add_argument("--dark-threshold", type=float, default=40.0,
                        help="Mean brightness threshold for dark-image detection (default: 40)")
    args = parser.parse_args()

    if not args.scan and not args.fix and not args.verify_dark and not args.check_decode and not args.fix_decode:
        args.scan = True

    books = sorted(OUTPUT_DIR.iterdir())
    if args.book:
        books = [OUTPUT_DIR / args.book]
        if not books[0].exists():
            print(f"Book not found: {args.book}")
            sys.exit(1)

    all_stats = []
    total_cmyk = 0
    total_dark = 0
    total_fixed = 0
    total_images = 0
    all_dark_files = []

    for book_dir in books:
        if not book_dir.is_dir() or book_dir.name.startswith("."):
            continue
        fig_dir = book_dir / "figures"
        if not fig_dir.exists():
            continue

        stats = scan_book(book_dir, do_fix=args.fix)
        total_images += stats["total"]

        if stats["cmyk"] > 0 or stats["dark"] > 0:
            flag = ""
            if args.fix and stats["fixed"] > 0:
                flag = f" [FIXED {stats['fixed']}]"
            print(f"  {stats['book']}: {stats['cmyk']} CMYK, {stats['dark']} dark{flag}")
            all_stats.append(stats)
            total_cmyk += stats["cmyk"]
            total_dark += stats["dark"]
            total_fixed += stats["fixed"]
            # Collect dark files for vision-model verification
            all_dark_files.extend(
                str(OUTPUT_DIR / f) for f in stats.get("dark_files", [])
            )

    print(f"\n{'='*60}")
    print(f"Total images scanned: {total_images}")
    print(f"CMYK negatives: {total_cmyk}")
    print(f"Dark (likely inverted): {total_dark}")
    if args.fix:
        print(f"CMYK fixed: {total_fixed}")

    # /Decode inversion scan (deterministic, fast)
    if args.check_decode or args.fix_decode:
        print(f"\n--- /Decode inversion scan ---")
        decode_total_inverted = 0
        decode_total_fixed = 0
        decode_total_xrefs = 0
        decode_no_pdf = []
        decode_all_stats = []

        for book_dir in books:
            if not book_dir.is_dir() or book_dir.name.startswith("."):
                continue
            fig_dir = book_dir / "figures"
            if not fig_dir.exists():
                continue

            ds = scan_decode_inversions(book_dir, do_fix=args.fix_decode)
            decode_total_xrefs += ds["scanned_xrefs"]

            if ds.get("error") == "no_source_pdf":
                decode_no_pdf.append(ds["book"])
            if ds["inverted"] > 0:
                tag = f" [FIXED {ds['fixed']}]" if args.fix_decode else ""
                print(f"  {ds['book']}: {ds['inverted']} inverted{tag}")
                decode_all_stats.append(ds)
                decode_total_inverted += ds["inverted"]
                decode_total_fixed += ds["fixed"]

        print(f"\n{'='*60}")
        print(f"Xrefs scanned: {decode_total_xrefs}")
        print(f"Inverted /Decode found: {decode_total_inverted}")
        if args.fix_decode:
            print(f"Fixed: {decode_total_fixed}")
        if decode_no_pdf:
            print(f"Books with no source PDF: {len(decode_no_pdf)}")

        # Save decode report
        decode_report = OUTPUT_DIR / "scripts" / "decode_inversion_report.json"
        decode_report.parent.mkdir(parents=True, exist_ok=True)
        with open(decode_report, "w", encoding="utf-8") as f:
            json.dump({
                "xrefs_scanned": decode_total_xrefs,
                "inverted_count": decode_total_inverted,
                "fixed_count": decode_total_fixed,
                "no_source_pdf": decode_no_pdf,
                "books": decode_all_stats,
            }, f, indent=2, ensure_ascii=False)
        print(f"Report saved: {decode_report}")
        return

    # Vision-model verification + fix for dark images
    dark_confirmed = []
    dark_fixed = 0
    if args.verify_dark and all_dark_files:
        print(f"\n--- Vision-model verification of {len(all_dark_files)} dark images ---")
        dark_confirmed = verify_dark_with_ollama_vision(all_dark_files)
        print(f"\nConfirmed {len(dark_confirmed)}/{len(all_dark_files)} as negatives")

        if dark_confirmed:
            print("\nFixing confirmed negatives...")
            for f in dark_confirmed:
                if fix_dark_with_invert(f):
                    dark_fixed += 1
                    print(f"  ✓ {os.path.basename(f)}")
            print(f"Dark fixed: {dark_fixed}/{len(dark_confirmed)}")

    # Save report
    report_path = OUTPUT_DIR / "scripts" / "negative_scan_report.json"
    os.makedirs(report_path.parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "total_images": total_images,
            "cmyk_count": total_cmyk,
            "cmyk_fixed": total_fixed,
            "dark_count": total_dark,
            "dark_confirmed": len(dark_confirmed),
            "dark_fixed": dark_fixed,
            "dark_confirmed_files": [os.path.relpath(f, OUTPUT_DIR) for f in dark_confirmed],
            "books": all_stats,
        }, f, indent=2, ensure_ascii=False)
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
