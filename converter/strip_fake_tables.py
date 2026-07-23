"""Demote markdown blocks that are not really tables back into plain text.

A corpus converted before the frame-reject fixes existed accumulates false-positive tables.
Two shapes make up nearly all of them:

  single-column   `| THE DRUGS vviiii |` -- front matter, a table-of-contents line, a running
                  header. A one-column "table" is a line of text with pipes around it. In one
                  book, 33 of 104 "tables" were this shape.
  mostly-empty    a diagram's axis labels caught by the ruling detector, e.g. a stress-strain
                  curve coming out as `|  |  | Toe region |  | Damage region |`.
                  Threshold: >70% of cells empty.

Nothing is deleted: the cell text is re-emitted as plain lines, so a grep for any word inside
the block still hits. Only the pipe scaffolding (and the QC marker that belongs to it) goes.

This is a post-hoc cleanup for markdown already produced by an older converter; new conversions
reject these blocks at write time. It is content-preserving by construction, but rewrites files
in place, so it defaults to a dry run and reports before it touches anything.

Usage:
  python strip_fake_tables.py --dry-run              # corpus-wide report, writes nothing
  python strip_fake_tables.py --dry-run --book NAME  # one book
  python strip_fake_tables.py --apply                # rewrite in place
  python strip_fake_tables.py --apply --only-completed PROGRESS.json
        # restrict to books a batch has already finished, so a running batch is never raced
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> shared.config
from shared.config import OUTPUT_DIR  # noqa: E402

EMPTY_CELL_RATIO = 0.70
# A diagram's text layer, sliced by the ruling detector, splits words across cell boundaries:
#   |  |  |  | Hyster | esis area |      <- "Hysteresis"
#   | Unloa | ding pat | h |  |  |       <- "Unloading path"
# A split alone is NOT enough: real tables split words across columns too, when a header like
# "Fiber type" straddles two columns (measured on a genuine nerve-fiber classification table
# that an earlier, looser version of this rule would have destroyed). What separates them is
# size: a diagram's label cluster is a handful of rows, a real table is not. Measured
# separation -- diagram blocks: 2-3 rows / 2-3 splits / 50-67% empty; real tables that split a
# header: 6-16 rows. All three conditions must hold.
FRAGMENT_MIN_SPLITS = 2
FRAGMENT_MAX_ROWS = 4
FRAGMENT_EMPTY_RATIO = 0.40
_WORD_END = re.compile(r"[A-Za-z]$")
_WORD_CONT = re.compile(r"^[a-z]")
# "Table 15.4", "TABLE 3", "Tabelle 2.1" (German textbooks), "Tableau 1" (French), "Table A1"
_TABLE_LABEL = re.compile(r"\b(?:Table|TABLE|Tabelle|Tableau)\s+[A-Z]?\d", re.I)

# A table block plus the optional decorations convert.py writes immediately above it: the
# "**[Table on page N]**" label and any "<!-- ... -->" QC/continuation markers. They are part of
# the table, so a demoted table must not leave them stranded.
BLOCK_RE = re.compile(
    r"(?:^\*\*\[Table on page \d+\]\*\*\n)?"
    r"(?:^<!--[^\n]*-->\n)*"
    r"(?P<rows>(?:^\|[^\n]*\|[ \t]*\n)+)",
    re.M)


def _rows(block_text: str) -> list[list[str]]:
    out = []
    for line in block_text.strip().splitlines():
        if set(line.strip()) <= set("|-: "):      # separator row
            continue
        out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def classify(block_text: str) -> str | None:
    """Return a reason string if this block is not a real table, else None."""
    rows = _rows(block_text)
    if not rows:
        return None
    # Veto: the book itself numbered this "Table 15.4". Whatever the shape looks like, the author
    # says it is a table, and that outranks every heuristic below. On a random book sample this
    # rescues ~1.3% of otherwise-demoted blocks -- and they are the worst ones to lose: a
    # legitimate one-column list table numbered "Table 52.1", or a real six-column table whose
    # HEADER was sliced ("Post|ure  X-ra|ys  Comm|ents"), which is exactly the fingerprint the
    # word-split rule hunts for. "Box 3.1" is deliberately NOT vetoed: a boxed sidebar is prose,
    # and demoting it to text is the right outcome.
    if _TABLE_LABEL.search(" ".join(rows[0])):
        return None
    ncols = max(len(r) for r in rows)
    if ncols <= 1:
        return "single-column"
    cells = [c for r in rows for c in r]
    if not cells:
        return None
    empty_ratio = sum(1 for c in cells if not c) / len(cells)
    if empty_ratio > EMPTY_CELL_RATIO:
        return "mostly-empty"
    splits = 0
    for row in rows:
        for left, right in zip(row, row[1:]):
            if left and right and _WORD_END.search(left) and _WORD_CONT.match(right):
                splits += 1
    if (splits >= FRAGMENT_MIN_SPLITS and len(rows) <= FRAGMENT_MAX_ROWS
            and empty_ratio >= FRAGMENT_EMPTY_RATIO):
        return "word-split"
    return None


def demote(block_text: str) -> str:
    """Re-emit the block's text without pipe scaffolding. Content-preserving by construction."""
    lines = []
    for row in _rows(block_text):
        text = " ".join(c for c in row if c).strip()
        if text:
            lines.append(text)
    return ("\n".join(lines) + "\n") if lines else ""


def process_text(text: str) -> tuple[str, dict]:
    counts = {"single-column": 0, "mostly-empty": 0, "word-split": 0, "kept": 0}
    out, pos = [], 0
    for m in BLOCK_RE.finditer(text):
        reason = classify(m.group("rows"))
        if reason is None:
            counts["kept"] += 1
            continue
        counts[reason] += 1
        out.append(text[pos:m.start()])
        out.append(demote(m.group("rows")))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), counts


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
    totals = {"single-column": 0, "mostly-empty": 0, "word-split": 0, "kept": 0}
    touched_books, touched_files = 0, 0
    per_book = []

    for bk in books:
        if completed is not None and bk.name not in completed:
            continue
        b_counts = {"single-column": 0, "mostly-empty": 0, "word-split": 0, "kept": 0}
        b_files = 0
        for md in sorted(bk.rglob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"  [skip] {md}: {e}", file=sys.stderr)
                continue
            new, counts = process_text(text)
            for k in b_counts:
                b_counts[k] += counts[k]
            if new != text:
                b_files += 1
                if args.apply:
                    md.write_text(new, encoding="utf-8")
        removed = sum(v for k, v in b_counts.items() if k != "kept")
        if removed:
            touched_books += 1
            touched_files += b_files
            per_book.append((removed, bk.name, b_counts))
        for k in totals:
            totals[k] += b_counts[k]

    per_book.sort(reverse=True)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] corpus={corpus}")
    print(f"  fake tables demoted: single-column={totals['single-column']} "
          f"mostly-empty={totals['mostly-empty']} word-split={totals['word-split']}  "
          f"(real tables kept {totals['kept']})")
    print(f"  affected {touched_books} book(s) / {touched_files} file(s)")
    print("\n  top 20 books by demotions:")
    for removed, name, c in per_book[:20]:
        share = removed / max(removed + c["kept"], 1)
        print(f"    {removed:5d} ({share:4.0%} of that book's tables)  {name[:60]}"
              f"   [1col={c['single-column']} empty={c['mostly-empty']} word-split={c['word-split']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
