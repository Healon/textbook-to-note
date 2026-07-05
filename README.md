# textbook-to-note

Turn your own PDF textbooks into an AI-searchable knowledge base and structured, well-cited Obsidian notes — figures included.

[繁體中文說明 → README.zh-TW.md](README.zh-TW.md)

## What it does

Studying from large textbooks with an AI assistant has three problems: feeding raw PDFs to a frontier model is slow and expensive, the model silently misses content on scanned or badly-encoded pages, and figures — often the most important part of a technical chapter — get lost entirely.

This pipeline solves all three locally, spending (almost) zero LLM tokens on the heavy lifting:

1. **Convert** (`converter/`) — PDF/EPUB → clean chapter-split markdown. Text extraction via PyMuPDF + pdfplumber (~130 ms/page, 0 tokens), with an OCR fallback ladder for scanned pages (Surya → PaddleOCR-VL → local vision model → frontier-model vision as the very last resort). Silent-failure detection catches pages where text extraction *looks* fine but isn't.
2. **Index** (optional) — build a local semantic search index (LanceDB + bge-m3 embeddings via ollama) so your AI assistant can search across every book you own instead of reading them cover to cover.
3. **Write notes** (`workflows/`) — a structured AI workflow that drafts a complete note on a topic from your converted books: skeleton first, every claim cited to book + chapter, uncited additions explicitly marked as inferred, then merged with any existing note you have.
4. **Extract figures** (`figures/`) — on-demand single-figure extraction with a deterministic QC gate: geometric matching against the figure registry, whitespace/text-bleed/OCR checks, and an optional local vision model for guided retries. The AI never "eyeballs" a crop and calls it done — the gate decides.

## Designed to be deployed by an AI

You are probably reading this because you want *your* AI assistant to set it up. Good — that is the intended path:

> Point Claude Code (or any capable coding agent) at this repo and say:
> **"Read AGENTS.md and set this up for me."**

[`AGENTS.md`](AGENTS.md) contains step-by-step instructions written for the agent: dependency install, configuration, converting a first book, installing the two Claude Code skills, and running the note workflow.

Manual setup instructions are in [`docs/architecture.md`](docs/architecture.md) if you prefer to drive yourself.

## Repo layout

```
converter/    PDF/EPUB → markdown scripts (convert.py is the entrypoint)
figures/      figure extraction + QC gate (figure_remap.py is the entrypoint)
skills/       drop-in Claude Code skill definitions (textbook-to-md, figure-remap)
workflows/    the note-writing workflow prompt (adapt to your own note system)
docs/         architecture, OCR ladder, calibration guide
examples/     a sample output note showing the target format
shared/       env-driven configuration (config.py)
```

## Requirements

- Python 3.10+, `pip install -r requirements.txt`
- Works CPU-only for digitally-born PDFs (the common case)
- Optional, for scanned books and figure QC: an NVIDIA GPU + [Surya OCR](https://github.com/VikParuchuri/surya), [ollama](https://ollama.com) with a small vision model (e.g. `minicpm-v:8b`) and `bge-m3` for embeddings — all local, nothing leaves your machine
- Tested on Windows 11 and macOS; Windows-specific gotchas are documented in the code

## Philosophy

- **Local-first, token-frugal**: the expensive AI is reserved for the one thing it's uniquely good at (synthesizing notes), never for mechanical page-by-page reading.
- **Deterministic gates over AI vibes**: every figure crop and every OCR page passes rule-based QC before an AI is allowed to judge it. Thresholds are never tuned to make a failing case pass.
- **Citations or it didn't happen**: every claim in a generated note carries its source (book + chapter). Anything the AI adds from its own knowledge is explicitly flagged.

## Bring your own books

This tool ships **no textbook content**. It operates on PDF files you already own — your purchased ebooks, institutional-access downloads, open-licensed texts (e.g. [OpenStax](https://openstax.org)), or scans of your own paper books where your local law permits. Respect your books' licenses.

## License

MIT © Po-Wei Chen ([drpwchen](https://github.com/drpwchen))
