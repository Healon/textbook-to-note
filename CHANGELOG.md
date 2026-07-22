# Changelog

All notable changes to this project are documented here. The through-line since the initial
release has been **table fidelity** — a textbook's tables are where its densest, most citable
data lives, and also where PDF extraction fails most silently. Each entry below is a distinct
failure mode found by measuring real books, with a deliberate fix and a kill-switch.

The format is based on [Keep a Changelog](https://keepachangelog.com/); this project uses
loose semantic versioning.

## [Unreleased]

### Fixed
- **One dash definition, shared by both sides of the figure pipeline**
  ([#10](https://github.com/drpwchen/textbook-to-note/issues/10)). #8 widened the converter's
  figure/table reference detection to U+2010–U+2015 and U+2212, but the figure stage kept its own
  narrower, hand-maintained copies (`qc_metrics._SEP`, `figure_qc_gate._DASHES`,
  `figure_scanned._DASHES`) — so a book typeset in, say, non-breaking hyphens would get
  `<!-- REF: ... -->` markers the caption matcher silently failed to consume. The canonical set
  now lives in `shared/config.py` (`DASH_CHARS` / `SEP_CLASS`) and all four call sites import it;
  the OCR-substitution neighbour checks inside `normalize_fig_id()` were widened with it. Existing
  normalize/caption tests re-run: 242 pass / 0 fail / 5 skip, unchanged from baseline.

### Changed
- **Three usage profiles replace the implicit all-or-nothing setup** (docs only). The repo reads
  as one pipeline you either adopt whole or not at all, but the parts stack cleanly:
  **A** converter-only (markdown + `grep`), **B** A plus the note workflow and figures,
  **C** B plus semantic search via the companion
  [vault-search](https://github.com/drpwchen/vault-search) indexer. Both READMEs open with the
  table, and `AGENTS.md` now asks which profile *first* — before any install — and marks every
  later step with the profiles it belongs to. The failure this prevents is an agent helpfully
  installing ollama, an embedding model, and the skills for a user who only ever wanted greppable
  markdown; the same over-eagerness that made the OCR path in 0.3.0 expensive.

## [0.3.0] — 2026-07-22 — The OCR rung, and telling the truth in the docs

Where 0.2.0 was about table fidelity, this one is about the two places the project was
quietly asking users to trust something that wasn't there: an OCR rung whose central
component was never shipped, and documentation describing behaviour the code did not have.
Both were found the same way — by an outside user's agent following the docs literally and
burning hours on it ([#3](https://github.com/drpwchen/textbook-to-note/issues/3)).

### Added
- **A reference OCR adapter ships** — `converter/surya_adapter.py`, targeting Surya 0.22.x, plus
  [`docs/surya-adapter.md`](docs/surya-adapter.md) ([#4](https://github.com/drpwchen/textbook-to-note/issues/4)).
  `SURYA_ADAPTER` had been a first-class config value, required for `surya_available()`, executed
  as a subprocess — and the file it pointed at existed only on the author's machine, with no
  published interface. An agent following the setup guide reasonably assumed it was part of the
  repo, could not find it, and reverse-engineered one against the removed `surya.ocr` API. The doc
  now publishes the contract (JSON Lines on stdout, `fixture` + `blocks[].text` + **required**
  `blocks[].bbox`, one line per image even when blank, logging on stderr, non-zero exit fails the
  batch) so any engine can take that rung, and states plainly why an adapter written for Surya
  0.17 dies on 0.22: `surya.ocr` is gone, `FoundationPredictor` became `SuryaInferenceManager`,
  and blocks now carry `html` rather than flat text.
- **Documented, tested memory caps for the OCR inference server.** Surya 0.22 serves its VLM
  behind llama.cpp or vllm, and llama.cpp sizes its KV cache as
  `parallel × ctx_per_slot` — defaulting to `8 × 12288 = 98304` tokens for a model whose weights
  are ~1.4 GB. That cache, not the model, is what became a reported 48 GB run for one user. The
  doc ships a launch command with the caps in it; measured peak RSS with them was **3121 MB**.
- **OCR output QC gate** — the OCR rung now fails loud like every other rung. `surya_ocr_pdf()`
  counts unparseable adapter lines, images with no answer, and empty pages, and **raises instead
  of writing markdown** when the empty-page ratio exceeds `T2N_OCR_EMPTY_PAGE_MAX` (0.35) or mean
  characters per page falls below `T2N_OCR_MIN_CHARS_PER_PAGE` (200). Previously a JSON parse
  failure was silently `continue`d and a near-empty book reported success in KB — the shape of a
  real incident where a scanned book produced 20 KB where ~1.6 MB was expected (~25 chars/page).
  Thresholds are calibrated against two real scanned references (689 and 788 pages: empty ratios
  0.054 / 0.003, mean 1791 / 2005 chars per page), so they clear legitimate blank and plate pages
  by roughly an order of magnitude. Per-book counters are surfaced in the batch report.

### Fixed
- **Two-column reading order on OCR'd pages** — the OCR path sorted blocks by `(y0, x0)`, which
  walks across the gutter and back on every band of a two-column page, interleaving the columns.
  Every character is present and individually correct, so no downstream check could see it — the
  same defect the fitz path already carried a dedicated column sort for. Both paths now share one
  `column_order_boxes()` (`T2N_COLUMN_SORT=0` restores the old sort on both). Byte-identity of the
  fitz path across two full books was verified as part of the same re-conversion run used for #5.
- **`DOCLING_DEVICE` defaults to `auto`** instead of a hardcoded `"cuda"`, resolving CUDA → Apple
  Silicon MPS → CPU inside the worker. A CPU-only machine no longer asks Docling for CUDA, and
  `mps` is now reachable at all (previously anything that wasn't the literal `"cuda"` mapped to
  CPU). Resolution logic is unit-tested without torch or a GPU; **the real MPS path is unverified**
  — there is no Apple Silicon in the development environment.
- **pdfplumber page cache is released after each page** ([#5](https://github.com/drpwchen/textbook-to-note/issues/5)).
  `convert_pdf()` opened one `pdfplumber.PDF` for a whole book and reached into `plumber.pages[i]`
  per page. `PDF.pages` holds every materialized `Page` for the object's lifetime, and each `Page`
  caches its `chars` / `edges` / `rects` on first use, so peak memory grew roughly linearly with the
  number of pages the table gate let through — nothing released them until `close()` at the end of
  the book. Every page is read exactly once, so `flush_cache()` + `close()` after its single use
  cannot change output, and **byte-identity was verified** on two books (a 1297-page table-dense
  reference, 627 tables, and a 638-page ordinary one): 23/23 output files identical each, before
  and after. Measured peak RSS: **6899 MB → 803 MB** on the dense book (8.6x), **6209 MB → 133 MB**
  on the ordinary one (46.6x), and the curve flattens instead of climbing to the last page.
  The ordinary book is the more interesting number: the issue predicted table-sparse books would
  barely leak because the gate skips them, but the gate admits far more pages than actually yield
  tables (638 pages, 12 tables extracted, still 6.2 GB), so the leak was never confined to
  table-dense books.

### Changed
- **Documentation audited against the code, and corrected where it disagreed.** Each of these
  was somewhere a reader could act on the docs and get a different result than the repo delivers:
  semantic search was described as if an indexer shipped (it does not — `post_convert.py --index`
  prints `[skip]` and returns success without `INDEXER_SCRIPT`, which is now stated, with the
  companion repo [vault-search](https://github.com/drpwchen/vault-search) named as the thing to
  point it at); `requirements.txt` was said to cover the semantic-search stack (lines 7-16 are all
  comments); the figure-remap contract was documented with a `match_method` key that the
  validator actively *rejects* (the real key is `match_quality`, and `qc_degraded` / `qc_skipped`
  were undocumented); docs used a `textbook-md/` output directory that is really `OUTPUT_DIR`
  (default `./output`); `architecture.md` pointed at a section of itself that does not exist; and
  `scan_fix_negatives.py` read `OLLAMA_VISION_MODEL` while the rest of the repo uses the `T2N_`
  namespace (now `T2N_OLLAMA_VISION_MODEL`, with the old name kept as a fallback so existing
  environments don't silently switch models). Also corrected: the skill claimed a strict figure
  hard_fail exits 2 (it exits 1), and that chapter splitting is not attempted on OCR'd books
  (it is, best-effort).
- **README no longer claims per-page OCR engine selection** (both variants). The detection
  signals are per-page; the routing decision is per-book — one trip of the check sends the whole
  PDF to OCR. Per-page routing is future work and is now labelled as such rather than described
  as shipped.
- **Setup guide no longer provisions OCR up front** (`AGENTS.md`, [#3](https://github.com/drpwchen/textbook-to-note/issues/3)).
  Step 1 asked whether the user had a GPU and pointed at the OCR ladder before a single page had
  been converted, which reads to a coding agent as "install the OCR stack now." It is an exception
  path: OCR routing exists only in the `--batch-dir` code path, and the single-file path never
  invokes it at all. A first-time user's agent installed Surya, hit the missing adapter ([#4](https://github.com/drpwchen/textbook-to-note/issues/4)),
  wrote its own against the removed `surya.ocr` API, chased that into a local VLM inference server,
  and exhausted 48 GB of RAM — on a born-digital PDF that converted correctly with no OCR at all.
  The GPU question is gone from Step 1 and OCR now lives in a new **Step 4.5**, entered only when
  Step 4's output is actually garbled or empty, with a diagnose-before-installing checklist.
- **Related-projects section now links [note-supplement](https://github.com/drpwchen/note-supplement)**
  (both README variants). It covers the direction this pipeline deliberately does not: merging new
  source material into notes that already exist, where the risk is not missing content but silently
  overwriting content the existing note already got right.

## [0.2.0] — 2026-07-21 — Table fidelity

A sustained pass over how the pipeline handles textbook tables, driven by measuring the real
corpus rather than by intuition. Every change defaults to preserving prior output (a kill-switch
restores byte-identical behavior) unless it corrects output that was already wrong.

### Added
- **Cross-page table merge** (`T2N_TABLE_MERGE=1`, default OFF) — stitch a table that ends near
  a page bottom to a geometrically-matching table at the top of the next page; dedupes the
  repeated header, leaves a `<!-- table continues from page N -->` trace.
- **Docling table rung** (`T2N_DOCLING=1`, default OFF) — a layout-model table extractor that
  gets multi-column shape right where `pdfplumber` collapses borderless grids; falls back to
  `pdfplumber` per-page when it finds nothing, so no page is left worse off.
- **Table QC gate** — flags *structural* damage (ragged rows, empty first cell, run-together
  text, single-column collapse, content-retention) with a `<!-- ⚠️ … -->` trace, never
  auto-"fixing" it.
- **Out-of-band review queue** (`T2N_REVIEW_QUEUE=1`, default OFF; recommended ON for clinical
  corpora) — the QC gate sees structure but not **misbinding** (a value merged into the *wrong
  row* of an otherwise clean grid). There is no safe automatic fix, so the high-risk subset
  (continuation-page tables + dosage/threshold tables) is flagged for a bring-your-own-model
  second opinion. In testing ~1 in 6 continuation×dose tables carried a high-severity misbinding
  vs ~0 in a random sample. See [`docs/table-review.md`](docs/table-review.md).
- **Spanned category-header collapse** (`T2N_TABLE_HEADER_COLLAPSE=1`, default ON) — a section
  header broadcast across every column of a wide grid becomes a phantom full-width data row. A
  real data row never repeats one ≥15-char string across ≥3 columns, so the row is re-cast as a
  single header cell — structural only, never moves a value between rows. Hit 130/232 (56%) of
  one dense pharmacology reference's tables.
- **Book-level table-reliability banner** (detection only) — when ≥40% of a book's tables (given
  ≥10) trip a QC flag, a `> [!caution]` banner is hung at the top of the markdown telling the
  downstream model to verify every table against the source PDF; `reliability_flagged` /
  `flag_rate` land in the per-book stats. One reference ran 66%.
- **Whole-book table-failure warning** (`T2N_BOOK_TABLE_CHECK`, default ON) — table loss is
  bimodal (a book extracts fine or loses every table); a loud warning is emitted when a book
  yields 0 tables despite ≥10 captions, or `pdfplumber` parses 0 pages while `fitz` opens fine.

### Fixed
- **Page-frame pseudo-table rejection** (`T2N_TABLE_FRAME_REJECT`, default ON) — page-decoration
  rectangles made `pdfplumber` "find" a whole-page 1-column table that dumps every word into one
  cell (real multi-column tables arrive column-interleaved but caption-and-values intact, reading
  as clean data while the binding is destroyed). Rejected and replaced by a trace comment.
  Measured at 9.9% of extracted tables across 128 books.
- **Two false-positive table detections** — running prose and a navigation strip that the frame
  heuristic misread as tables, narrowed without regressing real rejections.
- **Docling ligature corruption** — repair ligature glyphs against the page's own text layer
  before emitting, so the markdown and the QC retention check both see corrected text.
- **Furniture false-rejections** — a geometry-only running-header/footer band rule, measured for
  its false-kill rate and narrowed.

## [0.1.0] — 2026-07-19 — Initial public release

PDF/EPUB textbook → AI-searchable markdown → structured, fully-cited notes. Five stages:
convert (0-token, silent-failure detection, column-aware reading order), chunk (heading-aware),
retrieve (local LanceDB semantic search with source weighting), write (template-driven,
citation-enforced, non-destructive), extract figures (geometric match + deterministic QC gate).
Bilingual READMEs and note templates; ships as skills an AI agent installs from `AGENTS.md`.

[0.2.0]: https://github.com/drpwchen/textbook-to-note/releases/tag/v0.2.0
[0.1.0]: https://github.com/drpwchen/textbook-to-note/releases/tag/v0.1.0
