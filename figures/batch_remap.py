"""figure-remap batch orchestrator (legacy — see figures/CALIBRATION.md).

Subcommands:
  --list             list candidate books with figures/ dirs (shows layout)
  --book NAME --dry  dry-run on one book (prints stats + verdict)
  --book NAME --apply  apply (clean overwrite + visual sample)
  --batch [--dry|--apply]  iterate all candidates not flagged in playbook

State:
  <OUTPUT_DIR>/figure_batch_logs/_master_log.md  per-book run history (markdown table)
  <OUTPUT_DIR>/figure_batch_logs/playbook.md     knowledge of book quirks (skip rules,
                                                  threshold overrides) — override paths
                                                  via T2N_BATCH_LOG_PATH / T2N_PLAYBOOK_PATH

Layouts (auto-detected from figures/ contents):
  A  flat:        figures/Fig_*.png (single-PDF book, all figures in one dir)
  B  full_text:   figures/full_text/Fig_*.png (single-PDF book, nested one level)
  C  per-chapter: figures/Ch02_*/Fig_*.png (book split into per-chapter PDFs/md)

A/B run extract_figures on the whole PDF once. C iterates each chapter md,
finds its Source: PDF, and runs extraction into figures/<md_stem>/.

Depends on an optional caption-based batch extractor (extract_figures.py) that
is NOT part of this repo's core; this script degrades to a clear error message
if it isn't found (see EXTRACT_SCRIPT below). See figures/CALIBRATION.md for
what such an extractor needs to expose.
"""
import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(REPO_ROOT))
from shared.config import OUTPUT_DIR as TEXTBOOK_ROOT

# Optional external batch extractor — not shipped in this repo by default.
# Set T2N_EXTRACT_FIGURES_SCRIPT to point at your own caption-based extractor,
# or drop one at converter/extract_figures.py.
EXTRACT_SCRIPT = Path(os.environ.get("T2N_EXTRACT_FIGURES_SCRIPT")
                      or (REPO_ROOT / "converter" / "extract_figures.py"))
LOG_FILE = Path(os.environ.get("T2N_BATCH_LOG_PATH")
                or (TEXTBOOK_ROOT / "figure_batch_logs" / "_master_log.md"))
PLAYBOOK = Path(os.environ.get("T2N_PLAYBOOK_PATH")
                or (TEXTBOOK_ROOT / "figure_batch_logs" / "playbook.md"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qc = _load("qc_metrics", SCRIPTS_DIR / "qc_metrics.py")
visual = _load("visual_check", SCRIPTS_DIR / "visual_check.py")
if not EXTRACT_SCRIPT.exists():
    print(f"[WARN] batch extractor not found at {EXTRACT_SCRIPT} — "
          f"extraction commands (--dry/--apply) will fail until you provide one. "
          f"See figures/CALIBRATION.md.", file=sys.stderr)
    extract_figures = None
else:
    extract_figures = _load("extract_figures", EXTRACT_SCRIPT)


# ---------- layout detection ----------

def detect_layout(fig_dir: Path) -> str:
    """Return 'A' (flat), 'B' (full_text), 'C' (per-chapter), 'R' (reindex), or 'unknown'.

    Rules:
      - manifest has source=reindex_from_page_dump → R (chapter-split, no single PDF)
      - direct Fig_*.png/jpeg in figures/           → A
      - one subdir named 'full_text', no flat       → B
      - multiple subdirs                            → C
      - empty figures/ → A as default
    """
    if not fig_dir.is_dir():
        return "unknown"
    manifest = fig_dir / "figures_manifest.json"
    if manifest.exists():
        try:
            d = json.loads(manifest.read_text(encoding="utf-8"))
            if d.get("source") == "reindex_from_page_dump":
                return "R"
        except Exception:
            pass
    direct_figs = [p for p in fig_dir.iterdir()
                   if p.is_file() and p.name.startswith("Fig_")
                   and p.suffix.lower() in {".png", ".jpeg", ".jpg"}]
    subdirs = [p for p in fig_dir.iterdir() if p.is_dir()]

    if direct_figs:
        return "A"
    if len(subdirs) == 1 and subdirs[0].name == "full_text":
        return "B"
    if subdirs:
        return "C"
    return "A"  # empty dir: default to A (flat first-time extraction)


# ---------- discovery ----------

def discover_books() -> list:
    books = []
    if not TEXTBOOK_ROOT.is_dir():
        return books
    for d in sorted(TEXTBOOK_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        fig_dir = d / "figures"
        if not fig_dir.is_dir():
            continue
        pdf = _find_source_pdf(d)
        layout = detect_layout(fig_dir)
        books.append({
            "name": d.name,
            "md_dir": str(d),
            "figures_dir": str(fig_dir),
            "pdf": pdf,
            "layout": layout,
        })
    return books


def _find_source_pdf(md_dir: Path) -> str | None:
    for md in sorted(md_dir.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r"Source:\s*`([^`]+)`", text)
        if m and Path(m.group(1)).exists():
            return m.group(1)
    return None


# ---------- playbook helpers ----------

def load_playbook_skip_list() -> set:
    if not PLAYBOOK.exists():
        return set()
    skips = set()
    in_skip = False
    for line in PLAYBOOK.read_text(encoding="utf-8").splitlines():
        if line.startswith("## skip"):
            in_skip = True
            continue
        if in_skip and line.startswith("## "):
            in_skip = False
        if in_skip and line.startswith("- "):
            name = line[2:].split(" ", 1)[0].strip().rstrip(":")
            if name:
                skips.add(name)
    return skips


def append_playbook_entry(name: str, body: str):
    if not PLAYBOOK.exists():
        PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
        PLAYBOOK.write_text("# figure-remap playbook\n\nGrowing knowledge of per-book quirks.\n\n## skip\n\n## entries\n\n", encoding="utf-8")
    cur = PLAYBOOK.read_text(encoding="utf-8")
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n### {name} — {ts}\n{body.strip()}\n"
    PLAYBOOK.write_text(cur + entry, encoding="utf-8")


# ---------- log ----------

def append_log_row(name: str, layout: str, verdict: str, metrics: dict, reasons: list,
                   visual_result: dict | None = None):
    if not LOG_FILE.exists():
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(
            "| date | book | layout | verdict | captions | images | mp_ratio | ref_ratio | reasons | visual |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n",
            encoding="utf-8")
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    vr = ""
    if visual_result is not None:
        vr = f"{visual_result.get('passes',0)}/{visual_result.get('passes',0)+visual_result.get('fails',0)}"
    row = (f"| {ts} | {name} | {layout} | {verdict} "
           f"| {metrics.get('caption_count','?')} "
           f"| {metrics.get('image_count','?')} "
           f"| {metrics.get('multipart_ratio','?')} "
           f"| {metrics.get('ref_ratio','?')} "
           f"| {'; '.join(reasons) if reasons else 'ok'} "
           f"| {vr} |\n")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(row)


# ---------- core: extraction (direct function call, no subprocess) ----------

def _extract_one(pdf: str, out_dir: Path, dry_run: bool, clean: bool) -> dict:
    """Run extract_figures_from_pdf, return its stats dict."""
    if extract_figures is None:
        raise RuntimeError(f"no batch extractor available at {EXTRACT_SCRIPT}; "
                           f"see figures/CALIBRATION.md")
    out_dir.mkdir(parents=True, exist_ok=True)
    return extract_figures.extract_figures_from_pdf(
        pdf, str(out_dir), dry_run=dry_run, clean=clean)


def _gather_stats(book: dict, dry_run: bool, clean: bool) -> dict:
    """Layout-aware extraction. Returns aggregate stats with figures list."""
    fig_dir = Path(book["figures_dir"])
    layout = book.get("layout") or detect_layout(fig_dir)
    book["layout"] = layout

    agg = {"extracted": 0, "no_image": 0, "skipped": 0, "figures": [], "chapters": []}

    if layout == "A":
        if not book["pdf"]:
            return agg
        stats = _extract_one(book["pdf"], fig_dir, dry_run, clean)
        _merge(agg, stats, "flat")

    elif layout == "B":
        if not book["pdf"]:
            return agg
        target = fig_dir / "full_text"
        stats = _extract_one(book["pdf"], target, dry_run, clean)
        _merge(agg, stats, "full_text")

    elif layout == "R":
        # Reindex layout: chapter-split book with figures already mapped via
        # an offline reindex tool. Read manifest directly; no extraction.
        manifest_path = fig_dir / "figures_manifest.json"
        if manifest_path.exists():
            d = json.load(open(manifest_path, encoding="utf-8"))
            figs = d.get("figures", [])
            file_figs = [f for f in figs if f.get("file")]
            agg["extracted"] = len(file_figs)
            agg["no_image"] = d.get("no_image", len(figs) - len(file_figs))
            agg["figures"] = file_figs
            agg["chapters"].append("reindex")

    elif layout == "C":
        md_dir = Path(book["md_dir"])
        for md in sorted(md_dir.glob("*.md")):
            text = md.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"Source:\s*`([^`]+)`", text)
            if not m:
                continue
            pdf = m.group(1)
            if not Path(pdf).exists():
                continue
            if text.count("<!-- REF:") == 0 and not re.search(r"\bFig\.\s*\d+\.\d+", text):
                continue
            ch_dir = fig_dir / md.stem
            try:
                stats = _extract_one(pdf, ch_dir, dry_run, clean)
            except Exception as e:
                print(f"    ! {md.stem}: {e}")
                continue
            _merge(agg, stats, md.stem)

    return agg


def _merge(agg: dict, stats: dict, ch_label: str):
    agg["extracted"] += stats.get("extracted", 0) or 0
    agg["no_image"] += stats.get("no_image", 0) or 0
    agg["skipped"] = agg.get("skipped", 0) + (stats.get("skipped", 0) or 0)
    agg["figures"].extend(stats.get("figures", []) or [])
    agg["chapters"].append(ch_label)


# ---------- main commands ----------

def cmd_list(args):
    books = discover_books()
    skips = load_playbook_skip_list()
    print(f"Found {len(books)} books with figures/ dir at {TEXTBOOK_ROOT}\n")
    by_layout = {}
    for b in books:
        by_layout.setdefault(b["layout"], []).append(b)
    for layout in ("A", "B", "C", "R", "unknown"):
        bs = by_layout.get(layout, [])
        if not bs:
            continue
        print(f"--- layout {layout} ({len(bs)}) ---")
        for b in bs:
            flag = " (SKIP per playbook)" if b["name"] in skips else ""
            print(f"  {b['name']:60} pdf={'yes' if b['pdf'] else 'NO'}{flag}")
        print()


def cmd_book(args):
    books = {b["name"]: b for b in discover_books()}
    if args.book not in books:
        print(f"book not found: {args.book}", file=sys.stderr); sys.exit(1)
    b = books[args.book]
    skips = load_playbook_skip_list()
    if b["name"] in skips and not args.force:
        print(f"book {b['name']} is in playbook skip list; pass --force to override")
        sys.exit(2)
    if not b["pdf"] and b["layout"] != "R":
        print(f"no PDF source found for {b['name']}", file=sys.stderr); sys.exit(3)
    if b["layout"] == "unknown":
        print(f"unknown layout for {b['name']} — figures/ has no Fig_* files and no recognizable subdirs")
        append_log_row(b["name"], b["layout"], "red", {}, ["unknown_layout"])
        return

    if args.dry or args.apply:
        action = "apply" if args.apply else "dry"
        clean = bool(args.apply)
        print(f"[{action}] {b['name']} (layout={b['layout']}): pdf={b['pdf']}")
        agg = _gather_stats(b, dry_run=not args.apply, clean=clean)
        figs = agg["figures"]
        # R layout: skip ref_ratio (page-based reindex; fig_id format differs from MD refs)
        md_dir_for_qc = Path("/nonexistent") if b["layout"] == "R" else Path(b["md_dir"])
        # PDF-side audit: independent ground-truth scan (only for layout A/B with single PDF)
        pdf_audit_result = None
        if b["layout"] in ("A", "B") and b.get("pdf"):
            try:
                pdf_audit_result = qc.pdf_audit(b["pdf"])
            except Exception as e:
                print(f"  [WARN] pdf_audit failed: {e}")
        metrics = qc.compute_metrics(figs, md_dir_for_qc, pdf_audit_result, agg.get("skipped", 0))
        nir = agg["no_image"] / max(1, agg["no_image"] + agg["extracted"])
        verdict, reasons = qc.verdict(metrics, nir)
        # Strip private keys (prefixed _) from metrics before printing
        printable = {k: v for k, v in metrics.items() if not k.startswith("_")}
        print(json.dumps({"layout": b["layout"], "chapters_run": len(agg["chapters"]),
                          "metrics": printable, "no_image_ratio": round(nir, 3),
                          "verdict": verdict, "reasons": reasons}, indent=2, default=str))
        if verdict != "green" and pdf_audit_result is not None:
            print()
            print(qc.diagnostic_dump(metrics, pdf_audit_result, md_dir_for_qc))

        visual_result = None
        if args.apply and verdict == "green":
            # find first chapter dir with manifest and sample
            sample_dir = None
            if b["layout"] == "A":
                sample_dir = Path(b["figures_dir"])
            elif b["layout"] == "B":
                sample_dir = Path(b["figures_dir"]) / "full_text"
            elif b["layout"] == "C":
                for ch in sorted(Path(b["figures_dir"]).iterdir()):
                    if (ch / "figures_manifest.json").exists():
                        sample_dir = ch; break
            if sample_dir:
                man = sample_dir / "figures_manifest.json"
                if man.exists():
                    d = json.load(open(man, encoding="utf-8"))
                    visual_result = visual.sample_check(d.get("figures", []), sample_dir, n=3)
                    print("--- visual sanity ---")
                    print(json.dumps(visual_result, indent=2, ensure_ascii=False))
                    if visual_result["fails"] > 0:
                        verdict = "yellow"
                        reasons.append(f"visual_fails={visual_result['fails']}")

        append_log_row(b["name"], b["layout"], verdict, metrics, reasons, visual_result)
        if reasons:
            append_playbook_entry(b["name"],
                f"layout={b['layout']}; verdict={verdict}; metrics={metrics}; "
                f"chapters_run={len(agg['chapters'])}; reasons={reasons}")


def cmd_batch(args):
    books = discover_books()
    skips = load_playbook_skip_list()
    todo = [b for b in books if b["name"] not in skips and b["pdf"]
            and b["layout"] != "unknown"]
    print(f"batch: {len(todo)} books queued ({len(books)-len(todo)} skipped)")
    for i, b in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {b['name']} (layout={b['layout']})")
        ns = argparse.Namespace(book=b["name"], dry=args.dry, apply=args.apply, force=False)
        try:
            cmd_book(ns)
        except SystemExit:
            continue
        except Exception as e:
            print(f"  ERROR: {e}")
            append_log_row(b["name"], b.get("layout","?"), "red",
                           {"error": str(e)[:80]}, [f"exception: {e}"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("--book", help="run on a single book by directory name")
    p.add_argument("--batch", action="store_true", help="run on all candidates")
    p.add_argument("--dry", action="store_true", help="dry-run only (no writes)")
    p.add_argument("--apply", action="store_true", help="apply with --clean")
    p.add_argument("--force", action="store_true", help="ignore playbook skip list")
    args = p.parse_args()

    if args.list:
        cmd_list(args); return
    if args.book:
        cmd_book(args); return
    if args.batch:
        cmd_batch(args); return
    p.print_help()


if __name__ == "__main__":
    main()
