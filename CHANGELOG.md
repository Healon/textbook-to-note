# Changelog

All notable changes to this project are documented here. The through-line since the initial
release has been **table fidelity** — a textbook's tables are where its densest, most citable
data lives, and also where PDF extraction fails most silently. Each entry below is a distinct
failure mode found by measuring real books, with a deliberate fix and a kill-switch.

The format is based on [Keep a Changelog](https://keepachangelog.com/); this project uses
loose semantic versioning.

## [Unreleased]

### Fixed
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
