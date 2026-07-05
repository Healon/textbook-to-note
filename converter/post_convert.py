"""
Post-conversion cleanup and indexing script.
Run AFTER batch-dir conversion completes.

Usage:
    python post_convert.py          # Full pipeline: verify + INDEX.md + optional figure registry + optional indexer
    python post_convert.py --verify # Only verify conversion quality
    python post_convert.py --index  # Only run the external semantic indexer (if configured)
    python post_convert.py --audit  # Only report index coverage (no backfill)

The semantic-indexer and figure-registry steps are optional integrations (this repo
doesn't ship them): set INDEXER_SCRIPT / VAULT_SEARCH_DIR / FIGURE_REGISTRY_SCRIPT in
shared/config.py (or the matching env vars) to wire in your own tooling. Without them,
those steps are skipped with a message.

Note: missing page markers no longer block indexing. epub (reflowable) and a few
vector-glyph PDFs index fine with page metadata = 0. audit_index_coverage() is the
safety net — it backfills any book folder that has md but no rows in the index.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import OUTPUT_DIR, INDEXER_SCRIPT, VAULT_SEARCH_DIR, FIGURE_REGISTRY_SCRIPT

OUTPUT_BASE = OUTPUT_DIR
PROGRESS_FILE = OUTPUT_BASE / "batch_progress.json"


def verify_conversion():
    """Check conversion quality across all books."""
    print("=== Verification ===")

    books = [d for d in sorted(OUTPUT_BASE.iterdir())
             if d.is_dir() and d.name not in {'misc', 'scripts', 'figures_test', '__pycache__'}]

    total_books = len(books)
    books_with_pages = 0
    books_without_pages = 0
    epub_books = 0
    total_chapters = 0
    total_fig_refs = 0
    scan_only = []
    no_chapters = []

    for d in books:
        md_files = list(d.glob("*.md"))
        if not md_files:
            continue

        has_pages = False
        is_epub = False
        fig_refs = 0
        chapter_files = [f for f in md_files if f.name.startswith("ch") or f.name.startswith("pages_")]

        for f in md_files[:3]:
            content = f.read_text(encoding="utf-8", errors="replace")[:5000]
            if "<!-- page" in content:
                has_pages = True
            if "<!-- SOURCE: epub" in content:
                is_epub = True
            fig_refs += content.count("<!-- REF:")

        if has_pages:
            books_with_pages += 1
        elif is_epub:
            epub_books += 1  # reflowable — no page numbers by design, not a defect
        else:
            books_without_pages += 1

        total_fig_refs += fig_refs

        if len(chapter_files) > 1:
            total_chapters += len(chapter_files)
        elif len(md_files) == 1 and md_files[0].name == "full_text.md":
            # Only full_text.md, no chapters
            content = md_files[0].read_text(encoding="utf-8", errors="replace")
            if len(content) < 500:
                scan_only.append(d.name)
            else:
                no_chapters.append(d.name)

    print(f"Total books: {total_books}")
    print(f"With <!-- page N --> markers: {books_with_pages}")
    print(f"epub (no pages by design): {epub_books}")
    print(f"Without page markers (PDF, suspect): {books_without_pages}")
    print(f"Total chapter files: {total_chapters}")
    print(f"Figure refs (sampled): {total_fig_refs}")

    if scan_only:
        print(f"\nScan-only (needs OCR): {len(scan_only)}")
        for name in scan_only[:10]:
            print(f"  {name}")

    if no_chapters:
        print(f"\nNo chapter split: {len(no_chapters)}")
        for name in no_chapters[:10]:
            print(f"  {name}")

    # Check progress file
    if PROGRESS_FILE.exists():
        progress = json.loads(PROGRESS_FILE.read_bytes().decode("utf-8"))
        errors = [e for e in progress.get("errors", []) if not e.startswith("[SCAN]")]
        scans = [e for e in progress.get("errors", []) if e.startswith("[SCAN]")]
        print(f"\nBatch progress: {len(progress['completed'])} completed")
        if errors:
            print(f"Errors: {len(errors)}")
            for e in errors:
                print(f"  {e}")
        if scans:
            print(f"Scanned PDFs skipped: {len(scans)}")

    return books_without_pages == 0


def run_indexer():
    """Run an external semantic indexer (incremental — only new/modified files), if configured."""
    print("\n=== Running Semantic Indexer ===")
    if not INDEXER_SCRIPT or not INDEXER_SCRIPT.exists():
        print("  [skip] INDEXER_SCRIPT not configured — see shared/config.py")
        return True
    result = subprocess.run(
        [sys.executable, str(INDEXER_SCRIPT), "--incremental"],
        capture_output=False,
    )
    return result.returncode == 0


def audit_index_coverage(backfill: bool = True):
    """Fallback safety net: list book folders that have md but no rows in the
    semantic index, and (optionally) backfill each via `indexer --book`.
    Catches books that slipped through (e.g. a prior gate-blocked run).
    Requires an external indexer (see shared/config.py); skipped otherwise."""
    print("\n=== Index Coverage Audit ===")
    if not INDEXER_SCRIPT or not VAULT_SEARCH_DIR:
        print("  [skip] INDEXER_SCRIPT/VAULT_SEARCH_DIR not configured — see shared/config.py")
        return
    try:
        import lancedb
    except ImportError:
        print("  [skip] lancedb not importable")
        return
    db_path = VAULT_SEARCH_DIR / "lance_db"
    try:
        db = lancedb.connect(str(db_path))
        tbl = db.open_table("textbook_chunks")
        indexed = set(tbl.to_lance().to_table(columns=["book"]).to_pydict()["book"])
    except Exception as e:
        print(f"  [skip] cannot read index: {e}")
        return

    # folders with ANY md (recursive — some books nest chapters in subdirs)
    folders = set()
    for d in OUTPUT_BASE.iterdir():
        if d.is_dir() and d.name not in {"misc", "scripts", "figures_test", "__pycache__"}:
            if any(d.rglob("*.md")):
                folders.add(d.name)

    missing = sorted(folders - indexed)
    print(f"  Folders with md: {len(folders)} | in index: {len(folders & indexed)} | missing: {len(missing)}")
    if not missing:
        print("  ✓ full coverage")
        return
    for name in missing:
        print(f"  - MISSING: {name}")
    if backfill:
        for name in missing:
            print(f"  [backfill] indexing {name} ...", flush=True)
            subprocess.run([sys.executable, str(INDEXER_SCRIPT), "--book", name], capture_output=False)


def update_figure_registry():
    """Regenerate a figure registry from current figures, if a generator script is configured."""
    print("\n=== Updating Figure Registry ===")
    if FIGURE_REGISTRY_SCRIPT and FIGURE_REGISTRY_SCRIPT.exists():
        result = subprocess.run(
            [sys.executable, str(FIGURE_REGISTRY_SCRIPT)],
            capture_output=False,
        )
        return result.returncode == 0
    else:
        print(f"  [skip] FIGURE_REGISTRY_SCRIPT not configured — see shared/config.py")
        return True


def update_index_md():
    """Regenerate INDEX.md from current output contents."""
    print("\n=== Updating INDEX.md ===")

    books = []
    for d in sorted(OUTPUT_BASE.iterdir()):
        if not d.is_dir() or d.name in {'misc', 'scripts', 'figures_test', '__pycache__'}:
            continue
        md_files = list(d.glob("*.md"))
        if not md_files:
            continue

        total_words = 0
        for f in md_files:
            content = f.read_text(encoding="utf-8", errors="replace")
            total_words += len(content.split())

        books.append((d.name, len(md_files), total_words))

    lines = [
        f"# Textbook-to-Note Index",
        f"",
        f"Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Total: {len(books)} books | {sum(f for _, f, _ in books)} md files | {sum(w for _, _, w in books):,} words",
        f"",
        f"| Book | Files | Words |",
        f"|------|-------|-------|",
    ]
    for name, files, words in books:
        lines.append(f"| {name} | {files} | {words:,} |")

    index_path = OUTPUT_BASE / "INDEX.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"INDEX.md updated: {len(books)} books")


def main():
    parser = argparse.ArgumentParser(description="Post-conversion cleanup and indexing")
    parser.add_argument("--verify", action="store_true", help="Only verify conversion quality")
    parser.add_argument("--index", action="store_true", help="Only run the external semantic indexer")
    parser.add_argument("--audit", action="store_true", help="Only audit index coverage (report, no backfill)")
    args = parser.parse_args()

    if args.verify:
        verify_conversion()
    elif args.index:
        run_indexer()
    elif args.audit:
        audit_index_coverage(backfill=False)
    else:
        # Full pipeline. Page markers are informational only — the indexer handles
        # marker-less books (epub / vector-glyph PDFs) fine (page metadata = 0), so
        # a few suspect books must NOT block indexing the rest. Coverage is enforced
        # afterward by audit_index_coverage(), which backfills anything that slipped.
        all_good = verify_conversion()
        update_index_md()
        update_figure_registry()
        if not all_good:
            print("\n[note] some PDF books lack page markers (see above) — indexing anyway; "
                  "re-run conversion on those if search quality is poor.")
        run_indexer()
        audit_index_coverage(backfill=True)


if __name__ == "__main__":
    main()
