# AGENTS.md — Instructions for your AI coding agent

You are helping your user set up `textbook-to-note`: a local-first pipeline
that converts their own PDF/EPUB textbooks into searchable markdown, then
into structured, fully-cited notes in their personal knowledge vault
(Obsidian, Logseq, or a plain markdown folder). This repository is designed
to be deployed **by you**, an AI coding agent, working directly with the
user rather than requiring them to hand-run every script themselves.

Read this file fully before doing anything. Then work through the steps
below in order, checking in with the user at the marked decision points.

## What you're setting up

```
converter/    — PDF/EPUB → markdown conversion (0 LLM tokens)
figures/      — on-demand figure extraction with QC gating
skills/       — two Claude Code skill definitions (drop-in to ~/.claude/skills/)
workflows/    — the note-writing workflow specification
templates/    — real production note templates (zh-TW + English) for Step 1.1's topic-type table
docs/         — architecture + OCR-ladder reference docs
examples/     — one example output note showing the target format
shared/       — shared config (paths, env var names)
requirements.txt
```

Read `docs/architecture.md` first for the full picture. Leave
`docs/ocr-ladder.md` alone until Step 4.5 tells you to open it — it is a
reference for a failure case most users never hit, not part of setup.

## Step 1: Understand the user's situation

Ask, or infer from context:
- Where do their textbook PDFs/EPUBs already live? (a folder, a cloud-synced
  drive, etc.)
- What notes tool do they use, and where is their vault/notes folder?
- Do they want the optional semantic search index (LanceDB + a local
  embedding model via ollama), or is grep-only fine for their corpus size?
  Semantic search pays off once there are more than a handful of books;
  for a small personal library, skip it initially and add later.
  **This repo ships no indexer.** `post_convert.py --index` only shells out
  to whatever `INDEXER_SCRIPT` points at, and prints `[skip]` when it is
  unset. To actually get semantic search, point `INDEXER_SCRIPT` at the
  indexer from the companion repo
  [vault-search](https://github.com/drpwchen/vault-search) (or any indexer
  of your own with the same CLI: `<script> --incremental`, `<script>
  --book <name>`). Grep works out of the box with no extra install.

**Do not ask about GPUs here, and do not install any OCR engine yet.** OCR is
a reactive exception path in this pipeline, not a prerequisite — see Step 4.5.
Born-digital PDFs (bought ebooks, publisher downloads — the common case) go
through `fitz` alone and never touch it.

## Step 2: Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` covers **only** the core converter: PDF parsing, table
extraction, and the image helpers. Everything else is a separate manual
install, listed as comments at the bottom of the file rather than as
installable lines:

- EPUB conversion needs the `pandoc` CLI on `PATH` (not a pip package) —
  check with `pandoc --version` and prompt the user to install it if missing
- `requests` — only for `scan_fix_negatives.py --verify-dark` (ollama vision)
- `lancedb` — only for `post_convert.py`'s index-coverage audit, and it is
  useless on its own without an external indexer (see Step 1 / Step 5)
- OCR (Surya) lives in its own venv entirely — see Step 4.5

So `pip install -r requirements.txt` alone never gives the user semantic
search; do not tell them otherwise.

## Step 3: Configure paths

Configuration is environment-variable driven with repo-relative defaults —
`shared/config.py` documents every variable and works with zero setup:
drop books in `./books`, get markdown in `./output`.

Set env vars (in the shell, or a `.env` you source) only where the defaults
don't fit:
- `BOOKS_DIR` — where the user's PDFs/EPUBs live (default `./books`)
- `OUTPUT_DIR` — where converted markdown goes (default `./output`; keep it
  outside the notes vault — this corpus is for your reference, not for the
  user to read directly)
- Optional OCR fallback: `SURYA_VENV_PY` + `SURYA_ADAPTER` — **skip these
  unless Step 4.5 sends you here.** Setting them now buys nothing and pulls
  you toward installing an engine the user's books may never need.
  `SURYA_ADAPTER` points at `converter/surya_adapter.py`, which ships in this
  repo; `SURYA_VENV_PY` points at a *separate* venv's interpreter, since the
  OCR stack must not share dependencies with the converter
- Optional semantic search: `INDEXER_SCRIPT` + `VAULT_SEARCH_DIR`
- Figure output/cache locations: env-driven constants documented at the top
  of `figures/figure_qc_gate.py` (default `./output/figures`, inside the
  user's vault attachments folder if they want embeds to resolve)

Run `python shared/config.py` to print the resolved configuration and
confirm it before converting anything. Never hardcode paths into scripts or
skill files — always go through `shared/config.py` or env vars, so the repo
stays portable across machines and users.

## Step 4: Convert a first book (smoke test)

Pick one book the user cares about and convert it end to end, narrating what
you're doing:

```bash
python converter/convert.py "path/to/one.pdf" --book-label "Author Title — Ch.1"
```

Check the output markdown for:
- Readable, non-garbled text
- `<!-- page N -->` markers present
- Any `<!-- REF: Fig. X.Y → ... -->` markers where the source mentions
  figures

**For clinical / drug-dosing corpora, set `T2N_REVIEW_QUEUE=1`.** Extracted
tables can carry a *misbinding* — a value on the wrong row (a dose fused into
the wrong drug on a continuation page) — that is structurally invisible to the
QC gate and reads as clean, citable data. The flag marks the high-risk subset
(continuation-page + dosage/threshold tables) with a
`<!-- ⚠️ table needs out-of-band review … -->` comment for a second-opinion
pass. Before citing any flagged table as data, verify it against the PDF (or
run the review pass). See [`docs/table-review.md`](docs/table-review.md).

Once the smoke test looks right, convert the rest of the user's priority
books:

```bash
python converter/convert.py --batch-dir "path/to/their/textbook/folder"
```

This can take a while for a large library — run it as a background job and
report progress, don't block the conversation on it. Don't run two batch
conversions against the same `OUTPUT_DIR` concurrently — progress tracking
is last-writer-wins, so overlapping runs will corrupt each other's progress
state.

## Step 4.5: OCR — only if Step 4 actually came out wrong

**Skip this entire step unless the Step 4 output is genuinely garbled or
mostly empty.** If the markdown is readable, the pipeline is working as
designed and there is nothing here for you to install. Installing an OCR
engine "to be safe" is the single most expensive mistake you can make during
setup: it pulls in a heavyweight ML stack plus a multi-GB model download, and
has cost a user hours and tens of GB of RAM on a book that converted perfectly
without it.

Know where the OCR path actually lives before you reach for it:

- Routing to OCR exists **only** in the `--batch-dir` code path
  (`converter/convert.py`), where a PDF is sent to OCR if it is scan-only, if
  a silent `fitz` failure is detected, or if you passed `--force-surya`.
- The **single-file** path (`python converter/convert.py one.pdf`) never
  invokes OCR at all. If a single-file conversion produced text, OCR was not
  involved and installing an engine will change nothing.

If the output really is broken, diagnose before installing anything:

1. Open the PDF in a normal viewer and try to select text on a bad page. Text
   selects cleanly ⇒ it is a born-digital file and the problem is not OCR.
2. Garbled-but-present text usually means a broken font encoding (CID /
   Identity-H without ToUnicode, PUA codepoints) — the silent-failure case
   the OCR ladder exists to catch.
3. No selectable text at all ⇒ a true scan, and OCR is the right answer.

Only for cases 2 and 3, read [`docs/ocr-ladder.md`](docs/ocr-ladder.md),
including its "Choosing your hardware tier" table (CPU-only / Apple Silicon
8GB / Apple Silicon 16GB+ / NVIDIA 8GB / NVIDIA 16GB+), and size the engine
to the user's actual hardware before installing. On a CPU-only machine the
honest answer is that scanned books are not locally OCR-able — say so rather
than installing an engine that cannot finish a book.

Then set up the engine. A reference adapter for **Surya 0.22.x** ships as
`converter/surya_adapter.py`; [`docs/surya-adapter.md`](docs/surya-adapter.md)
has the venv commands, the inference-server launch line **with the memory caps
already in it**, and the interface contract if you would rather drop in a
different engine. Read the memory-cap section before starting a server — the
defaults size a KV cache far larger than the model itself, and that is what
turned into a multi-tens-of-GB run for one user.

```bash
export SURYA_VENV_PY=/path/to/surya22-venv/bin/python   # Scripts/python.exe on Windows
export SURYA_ADAPTER="$PWD/converter/surya_adapter.py"
```

Then re-run the affected book through `--batch-dir` — the only path that can
route to OCR. `--force` is required: a book already converted in Step 4 is
skipped both by the progress file and by the up-to-date `full_text.md` check,
so without it the re-run is a no-op.

```bash
python converter/convert.py --batch-dir "path/to/folder/with/that/book" --force
```

Cases 2 and 3 are both auto-detected, so that command is usually enough. Add
`--force-surya` only when auto-detection did *not* fire and the output is
still bad — a text layer healthy enough to pass the check while being
worthless (OCR-overlay scans, some digitized reprints). It forces OCR on
every PDF under that directory, so point `--batch-dir` at a folder holding
just the affected book.

## Step 5: (Optional) Build the semantic index

Only if the user opted in during Step 1:

```bash
python converter/post_convert.py --index
```

**This step is a hook, not an implementation.** `run_indexer()` runs the
script at `INDEXER_SCRIPT`; with that unset it prints
`[skip] INDEXER_SCRIPT not configured` and returns success, so a green run
here does *not* mean an index exists. Before running it:

1. Install an indexer — the companion repo
   [vault-search](https://github.com/drpwchen/vault-search) is the one this
   pipeline was built against — or write one exposing the same two calls
   (`--incremental`, `--book <name>`).
2. `pip install lancedb` (not in `requirements.txt`) if you also want
   `post_convert.py`'s index-coverage audit.
3. Set `INDEXER_SCRIPT` and `VAULT_SEARCH_DIR`, and have the local embedding
   model running (e.g. `ollama pull bge-m3` then `ollama serve`).

Then verify the index actually returns results for a test query before
telling the user it's ready. If the user does not want any of this, say
plainly that grep over `<OUTPUT_DIR>/` is the whole search story — that is a
supported configuration, not a degraded one.

## Step 6: Install the two Claude Code skills

```bash
mkdir -p ~/.claude/skills
cp -r skills/textbook-to-md ~/.claude/skills/
cp -r skills/figure-remap ~/.claude/skills/
```

Each copied `SKILL.md` references its scripts as `{REPO}/figures/...` or
`{REPO}/converter/...` — the scripts themselves stay in this cloned repo,
only the `SKILL.md` files move. Open each copied `SKILL.md` and replace every
`{REPO}` placeholder with the absolute path of this clone (e.g.
`C:\Users\you\textbook-to-note` or `/home/you/textbook-to-note`), so the
example commands resolve to the actual `converter/` and `figures/` code.

## Step 7: Run the note-writing workflow

`workflows/note-writing.md` is the full specification for turning a
converted textbook chapter into a structured note. It is written generically
so you should adapt phases 2/3 (the optional pluggable enrichment stages) to
whatever domain-specific tools the user actually has — a clinical evidence
API, a regulations database, a literature search tool, or nothing at all if
their domain doesn't need it.

Before writing the user's first real note, walk through
`examples/example-note.md` with them so they can confirm the target format
(nested bullets, per-claim citations, figure embed style) matches what they
want before you commit to writing dozens of notes in that shape.

Then, for a real topic:
1. Follow Phase 1 → 1.5 as written — draft blind from the textbook corpus,
   skeleton before prose
2. Skip phases 2/3 unless the user has a relevant enrichment source
   configured
3. Run Phase 3.5 figure harvest through the `figure-remap` skill — never
   hand-crop images yourself, always go through the QC-gated entrypoint
4. Run Phase 4 to merge with any existing note on the topic, following the
   deconstruct-and-reslot approach — do not just append new content to old
5. Optionally run Phase 5 link suggestion

## Step 8: Verify the note-format hook (optional but recommended)

If you (the agent) or the user want a mechanical pre-flight check before
every note write — catching missing citations, unescaped table syntax,
widthless figure embeds, and similar structural issues — build a small
format-lint script and wire it in as a pre-write hook in your agent
framework. This repo doesn't ship one by default since notes-tool
conventions vary too much across users, but `workflows/note-writing.md`'s
self-check section lists exactly what such a hook should catch.

## Token guardrails

These apply any time you're working with the converted corpus, not just
during initial setup:

- When consulting the converted corpus, **always grep or semantic-search
  first**, then `Read` only a bounded window (roughly ≤150 lines) around the
  hit. Never `Read` an entire `full_text.md` or a whole chapter file into
  context — a single book can be well over 1M tokens, and reading it whole
  defeats the entire point of converting to a searchable corpus.
- Frontier-vision figure escalation (ladder step 5 in `docs/ocr-ladder.md`)
  is per-figure opt-in and capped — at most a small fixed number of
  escalations per note. Prefer leaving a `<!-- TODO: figure -->` placeholder
  over a third escalation on the same note, and never batch-escalate a set
  of figures to frontier vision at once.

## Ongoing use

Once set up, the day-to-day loop is:
1. New textbook arrives → `converter/convert.py` (or batch-dir for several
   at once)
2. User wants a note on a topic → run the note-writing workflow
3. Note needs a figure mid-conversation, outside the full workflow → call
   `figure_remap.py extract` directly per `skills/figure-remap/SKILL.md`

## Guardrails — read before touching the filesystem

- Never delete or move files outside this repo and the user's configured
  vault/attachments directory without explicit confirmation.
- Never use raw destructive file operations (recursive delete, force move)
  on the user's attachments folder from within an automated sub-step —
  destructive operations belong to the orchestrating agent only, with the
  user able to see and approve what's being removed.
- Treat the user's source PDFs as read-only source-of-truth; all pipeline
  output should be a fresh copy, never an in-place edit of the original.
- If a step in this file references a script or path that doesn't exist yet
  in this repo, say so explicitly rather than guessing at its interface.
