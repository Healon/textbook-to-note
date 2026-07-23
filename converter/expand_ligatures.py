"""Expand typographic ligatures in a corpus that was converted by an older build.

`clean_text()` expands the ligature codepoints a PDF text layer emits (`ﬁ`, `ﬂ`, `ﬀ`, ...) to
their ASCII pairs, so `specific` still finds a page that typeset `speciﬁc` as one glyph. Books
converted before that fix carry the raw ligatures and are silently unsearchable for any word that
contains one.

This is the post-hoc pass for that corpus: it applies the *same* ligature map the converter uses
(imported from `convert.py`, so the two never drift) to markdown already on disk. It touches
nothing else — not line-joining, not soft-hyphens — so the only bytes that change are the ligature
glyphs themselves, and the pass is idempotent (a book already converted with the fix reports zero
replacements and is left byte-identical).

Usage:
  python expand_ligatures.py --dry-run              # corpus-wide report, writes nothing
  python expand_ligatures.py --dry-run --book NAME  # one book
  python expand_ligatures.py --apply                # rewrite in place
  python expand_ligatures.py --apply --only-completed PROGRESS.json
        # restrict to books a batch has already finished, so a running batch is never raced
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> shared.config
sys.path.insert(0, str(Path(__file__).resolve().parent))          # converter/ -> convert
from shared.config import OUTPUT_DIR  # noqa: E402
from convert import LIGATURES, LIGATURE_RE  # noqa: E402  (single source of truth)


def process_text(text: str) -> tuple[str, int]:
    """Expand ligatures; return the new text and how many glyphs were replaced."""
    n = len(LIGATURE_RE.findall(text))
    if not n:
        return text, 0
    return LIGATURE_RE.sub(lambda m: LIGATURES[m.group()], text), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="rewrite files (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--book", help="limit to one book folder")
    ap.add_argument("--only-completed", help="path to a batch progress.json; skip books not ok:true there")
    ap.add_argument("--exclude-pending", nargs=2, metavar=("BOOKS_JSON", "PROGRESS_JSON"),
                    help="skip books a RUNNING batch has not finished yet -- it will overwrite "
                         "them anyway, and cleaning a folder mid-write races the converter")
    ap.add_argument("--corpus", default=str(OUTPUT_DIR))
    args = ap.parse_args()
    if args.apply == args.dry_run:
        ap.error("pass exactly one of --apply / --dry-run")

    corpus = Path(args.corpus)
    completed = None
    if args.only_completed:
        completed = {k for k, v in json.loads(Path(args.only_completed).read_text(encoding="utf-8")).items()
                     if v.get("ok")}

    pending = set()
    if args.exclude_pending:
        bj, pj = args.exclude_pending
        all_books = {b["book"] for b in json.loads(Path(bj).read_text(encoding="utf-8"))}
        done = {k for k, v in json.loads(Path(pj).read_text(encoding="utf-8")).items() if v.get("ok")}
        pending = all_books - done
        print(f"  excluding {len(pending)} book(s) a running batch has not finished")

    books = [corpus / args.book] if args.book else sorted(p for p in corpus.iterdir() if p.is_dir())
    books = [b for b in books if b.name not in pending]
    total_glyphs = 0
    touched_books, touched_files = 0, 0
    per_book = []

    for bk in books:
        if completed is not None and bk.name not in completed:
            continue
        b_glyphs, b_files = 0, 0
        for md in sorted(bk.rglob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"  [skip] {md}: {e}", file=sys.stderr)
                continue
            new, n = process_text(text)
            if n:
                b_glyphs += n
                b_files += 1
                if args.apply:
                    md.write_text(new, encoding="utf-8")
        if b_glyphs:
            touched_books += 1
            touched_files += b_files
            per_book.append((b_glyphs, bk.name))
            total_glyphs += b_glyphs

    per_book.sort(reverse=True)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] corpus={corpus}")
    print(f"  ligature glyphs expanded: {total_glyphs}  across {touched_books} book(s) / {touched_files} file(s)")
    print("\n  top 20 books by ligature count:")
    for n, name in per_book[:20]:
        print(f"    {n:7d}  {name[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
