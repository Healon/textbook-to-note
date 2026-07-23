"""Turn a batch manifest into a triage list: which books can be trusted, which need a human.

A large batch produces one manifest entry per book and a log too big to read. Every quality
signal needed to say "do not trust this book" -- flag rates, content-loss counts, an empty
markdown -- is already in the manifest, but nothing assembles them, so failures get found only
when a human happens to look. This script is that missing step: run it after a batch and it names
the books that need individual handling, with the reason and the evidence.

Tiers:
  BLOCKED   the markdown is unusable as a source (near-empty output, table pass never ran)
  REVIEW    usable but a specific claim in it can be wrong (data loss inside tables, high flag
            rate, dosage/threshold values flagged) -- cite from it only after checking the PDF
  ODD       structurally strange but probably fine (chapter split way off, table count collapsed)
  OK        nothing tripped

Usage:
  python triage_report.py MANIFEST.json [--corpus DIR] [--json OUT.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> shared.config
from shared.config import OUTPUT_DIR  # noqa: E402

# Thresholds are CALIBRATED AGAINST A REFERENCE CORPUS, not picked from intuition. Measured over
# the books of one production batch:
#     flag_rate          median 0.47  p90 0.67  max 0.92
#     content_loss_rate  median 0.10  p90 0.26  max 0.46
#     dosage tables      median 0     p90 6     max 60
#     chapters/page      median 0.10  p90 0.47  max 1.00
# An "absolute" threshold like flag_rate>0.40 sounds strict but flags 84% of the corpus, which is
# the same as flagging nothing. These sit near p90, so a flagged book is genuinely an outlier
# among its peers. Re-derive them (rerun the distribution) whenever the table pipeline changes --
# a pipeline that gets better makes yesterday's p90 flag half the shelf again.
MIN_BYTES_PER_PAGE = 500        # below this the text layer almost certainly failed
CONTENT_LOSS_RATE = 0.25        # p90 = 0.26
FLAG_RATE = 0.70                # p90 = 0.67
DOSAGE_TABLES = 10              # p90 = 6; clinical dose values, so kept tighter than the others
CHAPTERS_PER_PAGE = 0.50        # p90 = 0.47
TABLE_COLLAPSE = 0.50           # kept <50% of the previous run's table count


def triage(stats: dict, corpus: Path) -> tuple[str, list[str]]:
    book = stats["book"]
    pages = max(stats.get("pages") or 0, 1)
    reasons = []
    tier = "OK"

    folder = corpus / book
    md_bytes = sum(f.stat().st_size for f in folder.rglob("*.md")) if folder.exists() else 0
    if md_bytes / pages < MIN_BYTES_PER_PAGE:
        tier = "BLOCKED"
        reasons.append(f"only {md_bytes/pages:.0f} bytes/page (text-layer extraction failed, or should have routed to OCR)")
    for w in stats.get("warnings", []):
        if "never ran on ANY page" in w:
            tier = "BLOCKED"
            reasons.append("table pass never ran on any page (no tables in the whole book)")
        elif "Surya OCR" in w:
            reasons.append("routed through OCR: no tables, and the body text is recognition output")

    loss = stats.get("content_loss_tables", 0)
    tables = stats.get("tables", 0)
    if tables and loss / tables > CONTENT_LOSS_RATE:
        tier = "BLOCKED" if tier == "BLOCKED" else "REVIEW"
        reasons.append(f"{loss}/{tables} tables lost real data ({loss/tables:.0%})")
    if stats.get("flag_rate", 0) > FLAG_RATE:
        tier = "BLOCKED" if tier == "BLOCKED" else "REVIEW"
        reasons.append(f"{stats['flag_rate']:.0%} of tables have unverified structure")

    dosage = sum(1 for _, rs in stats.get("review_queue", [])
                 if any("dosage" in r for r in rs))
    if dosage >= DOSAGE_TABLES:
        tier = "BLOCKED" if tier == "BLOCKED" else "REVIEW"
        reasons.append(f"{dosage} tables carry dosage/threshold values that may have landed in the wrong row")

    if stats.get("chapters", 0) / pages > CHAPTERS_PER_PAGE:
        tier = tier if tier in ("BLOCKED", "REVIEW") else "ODD"
        reasons.append(f"split into {stats['chapters']} chapters / {pages} pages (chapter splitter may be catching sub-headings)")
    before = (stats.get("before") or {}).get("tables") or 0
    if before >= 50 and tables < before * TABLE_COLLAPSE:
        tier = tier if tier in ("BLOCKED", "REVIEW") else "ODD"
        reasons.append(f"table count {before} -> {tables} (usually false-positive removal, but worth spot-checking)")
    return tier, reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--corpus", default=str(OUTPUT_DIR))
    ap.add_argument("--json", help="also write the triage as JSON here")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    # A manifest APPENDS one entry per conversion, so a book re-run after a fix appears twice and
    # the stale entry would be triaged alongside the fixed one. Last entry wins.
    manifest = list({s["book"]: s for s in manifest}.values())
    buckets = {"BLOCKED": [], "REVIEW": [], "ODD": [], "OK": []}
    for stats in manifest:
        tier, reasons = triage(stats, corpus)
        buckets[tier].append((stats["book"], reasons))

    total = sum(len(v) for v in buckets.values())
    print(f"triaged {total} books: BLOCKED {len(buckets['BLOCKED'])} / REVIEW {len(buckets['REVIEW'])} "
          f"/ ODD {len(buckets['ODD'])} / OK {len(buckets['OK'])}\n")
    for tier, header in [("BLOCKED", "not usable as a source -- re-run or change route"),
                         ("REVIEW", "usable, but check the source PDF before citing"),
                         ("ODD", "structurally odd -- a spot-check is enough")]:
        if not buckets[tier]:
            continue
        print(f"## {tier} -- {header}")
        for book, reasons in sorted(buckets[tier]):
            print(f"  {book}")
            for r in reasons:
                print(f"      - {r}")
        print()
    if args.json:
        Path(args.json).write_text(json.dumps(
            {t: [{"book": b, "reasons": r} for b, r in v] for t, v in buckets.items()},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
