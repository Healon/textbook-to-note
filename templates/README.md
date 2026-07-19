# Templates

These are the actual note templates used daily in production with this
pipeline (see Stage 4, "Template-driven extraction," in the main README).
Each template fixes a topic type's section structure so the AI knows exactly
what to hunt for in the source material, and every resulting note has the
same predictable shape.

Two parallel sets are provided:

- `zh-TW/` — the original Traditional Chinese templates, unedited.
- `en/` — faithful English translations, kept structurally identical
  (same heading levels, same callout syntax, same placeholder-comment style).

The English versions aren't just field labels translated in isolation — the
placeholder comments (the `<...>` guidance text inside each section) encode
the note-writing algorithm itself: what to search for, when to cross-link
instead of retyping, where evidence citations go, when a sub-heading should
be skipped rather than left empty. Translating that guidance faithfully was
the point; a label-only translation would lose the algorithm.

## zh-TW ↔ en mapping

| zh-TW | en | Topic shape |
|---|---|---|
| `疾病.md` | `condition.md` | Disease / condition |
| `技術.md` | `procedure.md` | Procedure / technique |
| `檢查.md` | `diagnostic-test.md` | Diagnostic test / study |
| `藥物.md` | `drug.md` | Drug |
| `評估工具.md` | `assessment-tool.md` | Assessment tool / scale |

These correspond to the generic template table in
[`workflows/note-writing.md`](../workflows/note-writing.md) (Step 1.1): the
disease template maps to "Concept/entity note," procedure/diagnostic-test to
"Procedure/method note," drug to a specialized concept-note variant, and
assessment-tool to "Tool/instrument note."

## Adapting these to your own field

These templates are medical/rehabilitation-specific (PMR). If your domain is
different:

1. Keep the shape, not the content — every template follows
   Purpose/Principle → Indication/Criteria → Procedure/Management →
   Interpretation/Evidence → Resource. That top-level flow (background →
   evaluation → application → sourcing) generalizes past medicine.
2. Replace domain-specific sections wholesale rather than patching them —
   e.g. `技術.md`'s "健保規範 / Reimbursement rules" section only makes sense
   if your field has an analogous coverage/compliance concept; drop it if
   not, don't leave it as a permanently-empty heading.
3. Keep the placeholder-comment discipline — every `<...>` should tell the
   AI *what to look for and where to put it*, not just describe the field
   name. That's what makes template-driven extraction work instead of
   producing a form with blank boxes.
4. Keep the frontmatter shape (`created` / `tags` / `aliases` / `progress`)
   or replace it with whatever your notes tool uses for metadata — either
   way, decide it once per template rather than per note.
