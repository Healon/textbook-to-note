# Table review — catching row-misbinding the QC gate can't see

The table QC gate (`converter/docling_tables.py`) flags tables that are *structurally*
wrong: ragged rows, an empty first cell, run-together text, a table collapsed to one
column, cells whose text isn't in the page. Every one of those checks keys off
structure or missing content.

It cannot see a **misbinding**: a value merged into the **wrong row** of an otherwise
well-formed table. The grid has the right shape, no empty cells, nothing ragged,
nothing missing — one value is just attached to the wrong label. That is worse than a
dropped table. A missing table is a visible gap; a misbound one reads as clean,
citable data, and a downstream LLM writing notes from it will attribute the value to
the wrong entity with full confidence.

No cheap deterministic predicate separates a correct binding from a misbound one — the
information you'd need is semantic (*does this dose belong to this drug?*), and the
geometry doesn't carry it. So this is deliberately **not** gated in code. Instead the
pipeline routes the high-risk subset to an out-of-band review pass.

## The failure mode, concretely

The sharpest case is a drug-dosing table **broken across a page break**. The last drug
on page 1 has a dose that runs onto page 2. On page 2 that dose continues as an
**orphan row with no drug name** (the name was on page 1). The extractor, seeing an
unlabelled row above the next drug's labelled row, **fuses the orphan's dose into that
next row**. The markdown then reads:

```
| Anxiolytic X | <the previous drug's continued dose> 0.5–1 mg ... |
```

so a corticosteroid's dose is reported as the anxiolytic's. The table is a clean 7×4
grid — the QC gate sees nothing wrong. Looking at the continuation page *alone* (text
or image) you can't catch it either, because the owning drug's name is on the previous
page. You have to read the page-1→page-2 seam.

Same class, also invisible to the gate: a whole treatment option silently dropped from
a row-per-option table, a drug-interaction warning bleeding from one drug's row into
the next, a lab value's clinical interpretation attached to the wrong condition.

## What the pipeline flags (the trigger)

`converter/review_queue.py` is a deterministic, **flag-only** trigger — it never
rejects, never edits, never touches table content. It marks the two-part high-risk
subset (over-inclusive on purpose; a false flag costs one review, a missed misbinding
ships wrong clinical data):

1. **continuation-page tables** — where orphan-fusion lives, and
2. **dosage / numeric-threshold tables** — cells carrying `mg`, `mL`, `mg/kg`, `IU`,
   `mEq/L`, `mmHg`, dose ranges, cut-offs; a misbound value here is the highest-severity
   output the tool can produce.

Enable it with `T2N_REVIEW_QUEUE=1` (default OFF — it adds annotations, so it follows
the opt-in convention; **turn it on for clinical / drug-dosing corpora**). Each flagged
table gets a greppable marker in the markdown:

```
<!-- ⚠️ table needs out-of-band review (continuation-page; dosage/threshold values) —
     the structural QC gate cannot detect a value bound to the wrong row; verify this
     table against PDF page N before citing it as data -->
```

and `convert_pdf()`'s stats carry `review_flagged` (count) and `review_queue`
(`[(page, [reasons])]`) so a batch run can write a per-book review manifest.

> **Why testing found this worth doing.** In a pilot over the continuation×dose
> intersection, roughly **one in six** of those tables carried a high-severity
> misbinding a downstream model would cite as clean data — versus **~0** in a random
> table sample. The danger concentrates exactly where both triggers meet, which is why
> the queue keys on these two signals rather than reviewing every table.

## The second opinion (the review pass)

The marker gets a table *into* the queue; clearing it needs a second opinion. This is
an **external, bring-your-own-model step** — the tool doesn't hard-wire an LLM. Two
tiers, cheap first:

1. **Text model first (default).** A fast model (e.g. a small/cheap tier) gets the
   extracted table markdown **plus the source page's own text layer** (which the
   pipeline already has) and is asked whether each value is bound to the right label.
   For a continuation table, give it the **previous** page's text too — the owning
   label lives there.
2. **Vision model as the escalation.** Reserve a vision model (render the page(s) to an
   image) for scanned pages with no text layer, or cases the text pass calls ambiguous.

### Two lessons from testing (bake these into your prompt)

- **Prime the model on the orphan-fusion pattern, or it misses the worst case.** With a
  neutral "check this table" prompt, a capable model *passed* the canonical
  dexamethasone-dose-on-the-anxiolytic case — it saw a labelled row with a dose and
  assumed the dose was that row's. It only caught it once the prompt explicitly told it
  the first row of a continuation page may be an orphan whose label is on the previous
  page, and to test that. Use the template below.
- **Run two diverse lenses and flag on either.** A page-seam-focused prompt catches
  orphan-fusion but can miss a *mid-table* fusion; a general prompt catches mid-table
  fusions but misses subtle orphans. Neither alone is complete. Running both (or two
  differently-primed checkers) and flagging if **either** objects recovers cases each
  misses alone.

### Hazard-primed verifier prompt (template)

```
You check whether an auto-extracted markdown table binds each value to the correct
row label, judged against the source page(s). These are drug-dose tables that may be
SPLIT ACROSS A PAGE BREAK — the highest-risk case for a specific, easy-to-miss error.

THE FAILURE YOU ARE HUNTING: a table's last row on page 1 is a drug whose dose runs
onto page 2. On page 2 that dose continues as an ORPHAN row with NO drug name (the
name was on page 1). A bad extraction FUSES the orphan's dose into the next labelled
row, so drug A's dose is reported as drug B's. Do NOT be reassured that the row carries
a drug label — the label can be correct while the dose in that same cell belongs to the
previous page's drug.

Given: the previous-page text (or image), the continuation-page text (or image), and
the extracted markdown for the continuation page —
1. On page 1, find the last drug and note whether its dose runs over / ends "(Continued)".
2. On page 2, decide if the first data row is an orphan continuing that drug.
3. For EACH value in the first labelled row of the markdown, decide whether it belongs
   to that row's label or was fused in from the page-1 drug. Then check the other rows
   normally (right value on right label; no dropped rows; no fragment bleeding across
   cells).
Report each misbinding as {extracted_label, extracted_value, correct_label, severity}.
severity = high whenever a drug dose / clinical value is attached to the wrong entity.
```

Run a second, differently-framed pass (a general "is every value on the right row?"
check without the orphan priming) and treat the table as needing correction if either
pass reports a high-severity misbinding.

## Design stance

Flag, never auto-"fix". There is no safe automatic correction for a misbinding — the
right value-to-row assignment is exactly the thing in doubt, so a code fix would either
miss most cases or corrupt good tables. The queue's job is to make the risk **visible**
(to a human or a review model) before the markdown is cited, not to guess.
