# OCR Ladder

Principle: minimize vision-model tokens. Extract text locally first; only
send an image to a hosted LLM as the absolute last resort.

## Choosing your hardware tier

The zero-setup `fitz`-only default (step 1 below) is the **right** starting
point for born-digital ebooks — most personal libraries are mostly
born-digital, and `fitz` alone handles them at essentially no cost. The rest
of the ladder (steps 2-5) is **opt-in**, only needed once you actually have
scanned books to process. Which of those opt-in steps are practical depends
on your hardware:

| Tier | Text (born-digital) | OCR (scanned) | Vision QC model (ollama) | Embedding (semantic index) | Skip / caveats |
|---|---|---|---|---|---|
| No GPU (CPU-only) | fitz — full speed, this is your whole pipeline | None practical. Surya-CPU only for a few short scans. Send rare scanned pages to frontier vision or skip. | Skip local vision QC → rely on deterministic QC only | bge-m3 on CPU OK for a small library, else grep-only | Don't install Surya/Paddle. Accept: scanned books aren't locally OCR-able. |
| Apple Silicon 8 GB | fitz | Surya on MPS (~1-3 pg/s) | minicpm-v:8b Q4 is tight in 8 GB unified; prefer a smaller VLM (e.g. llava-phi3, moondream) or run OCR and QC sequentially | bge-m3 via ollama Metal | Avoid PaddleOCR-VL (poor Metal support) |
| Apple Silicon 16 GB+ | fitz | Surya (MPS) | minicpm-v:8b comfortable | bge-m3 | PaddleOCR still Mac-weak — leave it off |
| NVIDIA 8 GB | fitz | Surya (batch 8-16) → PaddleOCR-VL fallback | minicpm-v:8b Q4 (~6 GB) — load sequentially, not concurrent with Surya | bge-m3 | Don't hold OCR + vision + embed resident at once |
| NVIDIA 16 GB+ | fitz | Surya (large batch) → PaddleOCR-VL | minicpm-v:8b at higher precision, or qwen2.5-vl:7b | bge-m3 (can stay resident) | Everything concurrent; reference tier |

Speed anchors to set expectations before committing a whole library to a
method: `fitz` text extraction runs ≫50 pages/s; a `pdfplumber` table pass
runs 2-10 pages/s; Surya on GPU runs ~1-4 pages/s vs ~0.05-0.2 pages/s on
CPU (impractical for whole books — CPU Surya is for a handful of pages, not
a library); a local VLM QC pass runs ~3-8 s/figure.

## The ladder

| Priority | Method | Cost | Notes |
|---|---|---|---|
| 1 | `fitz` text extraction (or `markitdown`) | 0 | Default path for born-digital PDFs |
| 2 | Local OCR engine A (GPU, e.g. Surya) | 0 | Default OCR engine — CJK + Latin |
| 3 | Local OCR engine B (fallback, e.g. PaddleOCR-VL) | 0 | Used when engine A's output looks wrong; tends to run away on dense tables, so it's fallback-only, not default |
| 4 | Local vision model via a local inference server (e.g. ollama running a small vision model) | 0 (no hosted LLM tokens) | Weak but free; good for a rough bounding-box suggestion or a sanity check |
| 5 | Frontier-model vision read (Claude, GPT, etc.) | High | Max ~20 pages per request; last resort only |

`tesseract` is deliberately excluded from this ladder: for CJK text in
particular, its output quality was too poor to be usable, and every project
that tried it ended up needing a better engine anyway. Skip straight to a
modern OCR model (step 2).

## The table rung (orthogonal to the OCR ladder)

The ladder above is about *reading text off a page*. Extracting **table
structure** is a separate problem, and it bites even on born-digital pages the
OCR ladder never touches: `pdfplumber` finds tables by ruling lines, so it
silently drops borderless / shaded-row tables and collapses some multi-column
tables into one column (values survive but the row↔column binding is
destroyed — worse than a missing table, because it reads as clean data).

`T2N_DOCLING=1` (default **off**) adds [Docling](https://github.com/DS4SD/docling)
as an alternative table source. It is invoked **only on pages the existing
table gate already flags** — never the whole book — and only replaces
`pdfplumber` for a page when it actually returns a table; otherwise that page
falls back to `pdfplumber`, whose known collapse modes stay guarded by the
page-frame and page-furniture rules. **The fitz text stream is never handed to
Docling** — it reorders page content, so only its *tables* are used, never its
reading order.

Docling runs as a persistent worker in its own venv over a line-delimited JSON
protocol (model cold start ~4–12 s, reused across the whole batch), and its
tables pass through the same flag-only QC gate as everything else
(content-retention, ragged-row, empty-first-cell, run-together, multi-value,
single-column) plus an oracle-gated ligature repair (`T2N_LIGATURE_REPAIR`,
default on) that fixes glyph-drop corruption (`speciic` → `specific`) only when
the source page's own text layer confirms the correct spelling. It is MIT and
**not** in `requirements.txt` — install it separately and point
`DOCLING_VENV_PY` at its interpreter.

General rule: **any page with an image gets OCR'd even if it already has a
text layer.** A native text layer is a cross-check, never a reason to skip
OCR — see the silent-failure detection below for why a text layer can lie.

Always run a cheap `fitz` quick-scan first (character count + image count per
page) to decide which path a given page needs, instead of committing the
whole document to one method.

Step 5 (frontier vision) is only justified for: figures/charts that need
layout understanding beyond OCR, or when steps 1–4 have all failed on a
given page. Every time step 4 or 5 is used, log a one-line note of *why*
steps 1–3 failed for that page — this makes it possible to notice if a
particular publisher/format pattern needs a permanent fix upstream instead of
a per-document escape hatch.

## `fitz` silent-failure detection (mandatory)

`fitz` (or any PDF text-layer extractor) can return text that *looks*
plausible while silently dropping content — vector-drawn glyphs, Type 3
fonts, or CID-encoded fonts with no `ToUnicode` map all produce this failure
mode. A page can report thousands of characters extracted and still be
functionally garbage. The quick-scan step must surface quality signals, not
just "did extraction return non-empty text" — and every check below is pure
script (PDF-library API calls + regex), costing 0 LLM tokens:

1. **Font risk flags** — inspect the page's font list. Flag pages using
   `Type 3` fonts, `Identity-H` encoding with no `ToUnicode` CMap, or subset
   fonts missing a character map. A flagged page's fitz text is not
   trustworthy — route it to OCR regardless of how much text it returned.
2. **Character-density anomaly** — for a text-heavy page, if the extracted
   character count is low (e.g. `< 100`) while the page's vector-drawing
   coverage is high, the "text" is likely drawn glyph outlines, not a real
   text layer. Route to OCR.
3. **Domain pattern miss** — the caller supplies an expected regex for the
   document type (e.g. numbered list markers, lettered options, section
   numbering). A page with zero matches for a pattern that adjacent pages
   satisfy is suspect — route to OCR.
4. **Sampling cross-check** — when any of the above trips, render one or two
   sample pages and ask a small local vision model for a rough character
   count. If it diverges from fitz's count by more than ~30%, re-OCR the
   whole document. This step still costs 0 hosted-LLM tokens (local model
   only).

The quick-scan should emit, per page: `char_count`, `font_risk`,
`pattern_hits`, and a final `verdict` of either `trust_fitz` or `force_ocr`.
Downstream code should always read the `verdict` field — never decide based
on raw extracted text length alone.

## Practical guidance

- Run OCR through an isolated Python environment per engine (these tools
  have finicky, sometimes conflicting dependency stacks — GPU driver
  bindings especially).
- On Windows, any OCR engine invoked as a subprocess should be read back in
  bytes mode and decoded explicitly as UTF-8 with error replacement — do not
  rely on the platform's default text-mode decoding, which can crash on
  legitimate UTF-8 subprocess output.
- Batch conversions should auto-route flagged pages to OCR rather than
  requiring a human to notice a bad conversion after the fact; single-file
  conversions can leave the decision to the caller.
