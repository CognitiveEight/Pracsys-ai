# SIM-202: Drafting Assistant Word export fidelity — technical design

- **Requirements:** `specs/requirements/SIM-202-drafting-docx-fidelity.md`
- **Status:** draft
- **Approver:** tech lead

## Summary
A new deterministic normaliser cleans the model's draft Markdown once, at the
point where every draft path converges. That covers render, revision,
re-translation, translation and finalize. The renderer in
`app/processing/docx_generator.py` is fixed to emit real Word tables, nested
lists, styled headings 1–6, section separators and highlighted placeholders.
On the template path, when the filled template is missing structure that the
draft has, the export falls back to rendering the full draft in the
template's styles. No model calls are added.

## Affected code
| File | Change |
|---|---|
| `app/processing/draft_markdown.py` | **new**: `normalize_draft_markdown`, `normalize_if_enabled`, `count_placeholders` |
| `app/processing/docx_generator.py` | table rule enabled; nested `ListItem.children`; heading styles 1–6; `hr` dropped; page break before schedule/execution headings; placeholder highlight; hard breaks kept; header row repeat; **new** `markdown_structure`, `docx_structure`, `template_fill_lost_structure` |
| `app/core/config.py`, `env.example` | **new** `draft_markdown_normalize` (default true) |
| `app/langgraph/drafting_agent.py` | normalise in `_render_and_attach`, `_handle_finalize_export` and the translation path; structure check after `llm_diff` / heuristic fill; `docx_export` count line; `docxStatus` / `placeholderCount` on every `draftDocument` |
| `app/prompts/drafting.py` | FORMAT CONTRACT appended to the render prompt |
| `app/socket/schemas.py` | `DraftDocumentPayload.docxStatus`, `placeholderCount` (**new**, optional) |
| docs | `docs/socket-events.md`, `docs/contracts/backend-ai-socket-events.md`, `docs/features/drafting-and-templates.md` |

## Event and payload changes
`ai_response.draftDocument`:
- `content`: same type; now normalised Markdown (GFM tables with contiguous
  rows, unescaped `<Label>` placeholders, headings `#`–`###`, no `---`
  rules). **Additive in meaning**: a strict superset of renderable Markdown.
- `docxStatus` (**new**, optional): `"ready"` when `downloadUrl` is set,
  `"failed"` when export ran and failed. **Additive.**
- `placeholderCount` (**new**, optional int): the number of distinct
  `<Label>` placeholders still in the draft. **Additive.**

A linked backend ticket (to be raised) moves download and "Generate final
draft" to `downloadUrl`.

## Design
1. **Normaliser** (`normalize_draft_markdown(md) -> str`): pure, idempotent,
   line-based. It skips fenced code. Rules:
   - **Tables.** A run of `|` lines separated only by blank lines is joined
     into one block. A separator row is inserted after the header if one is
     missing. Every row is padded or trimmed to the header's column count.
   - **Placeholders.** `\<X\>` → `<X>`. Inside a placeholder, `\_`/`\*` are
     unescaped.
   - **Headings.** Headings deeper than `###` are lowered to `###`.
   - **Rules.** A stand-alone `---`/`***`/`___` line is removed. Headings
     already separate sections, and the renderer adds a page break before
     schedule and execution headings.
   - **Nested items.** Lines like `  - (a) …` / `  (a) …` under a list item
     are re-indented to 2-space nesting under a `- ` item.
   - **Blank lines.** Runs of blank lines collapse to one.
2. **Renderer:**
   - **Parsing.** `MarkdownIt("commonmark", ...).enable("table")`. Cell
     alignment comes from the `style` attribute (`text-align:`).
   - **Lists.** `ListItem.children` holds nested lists. A level-n item uses
     `List Bullet`/`List Number` for n = 1 and `List Bullet 2`/`List Number 2`
     … for deeper levels, falling back to a left indent of 0.25" per level.
   - **Tables.** The header row is bold, shaded and marked
     `w:tblHeader`. Rows are marked `cantSplit`. The table spans the full
     text width with column widths proportional to content (min 0.8"), and
     cell paragraphs are left-aligned with no 1.5 spacing.
   - **Headings.** Heading styles 1–6 are styled together, with
     keep-with-next. A level 1–2 heading whose text starts with `SCHEDULE`,
     `ANNEXURE`, `APPENDIX` or `EXECUTION` gets `page_break_before` (except
     the first block).
   - **Placeholders.** `<Label>` inside text becomes its own run with a
     yellow highlight.
   - **Hard breaks.** A hard break (`two spaces + newline` or `\`) becomes a
     Word line break, so signature blocks keep their lines. Soft breaks stay
     as spaces.
   - **Structure counts.** `docx_structure(bytes)` counts `{tables,
     leftover_markup}`. Leftover markup means paragraphs starting with `|`,
     `#`, `---`, or containing `\<`.
3. **Template path.** After `llm_diff` or the heuristic fill produce bytes,
   compare `docx_structure(bytes).tables` with
   `markdown_structure(md).tables`. If the draft has more tables than the
   filled template, or more than a third of the draft's level 1–2 headings
   are not found in the filled file's text, discard those bytes and use
   `skeleton_clone`. The route is logged.
4. **Logging.** One line per export, counts only:
   `docx_export route={} tables={} placeholders={} leftover_markup={}`.

## Checklist against CLAUDE.md
- Role gate: n/a. Rendering only, after the existing gated draft flow.
- Long-running work: n/a. Same call sites; rendering already runs in
  `asyncio.to_thread`.
- Loaders / `CancelledError`: n/a. No new tasks or `workspace:refresh`.
- Emits: unchanged (`ai_response` via the existing chat path).
- Model calls: none added. The prompt text change goes through the existing
  router call (`generate_longform`).
- Paid calls metered: n/a.
- New caches and purge: none. `session_state` keeps the same keys.
- Ingestion/deletion: n/a.
- `mutations_allowed`: unchanged.
- Logging: counts only. No draft text, titles or URLs (the existing
  `safe_url` is kept).

## Risks and rollback
- **Risk:** the normaliser alters legitimate text that contains `|` or
  `---`. Mitigations:
  - table joining needs at least 2 pipe rows with a matching column count;
  - fenced code is skipped;
  - idempotence is tested.
- **Risk:** the template fallback picks `skeleton_clone` more often, which
  keeps less of the template's exact bytes. It is logged, and the
  thresholds are constants.
- **Rollback:** `DRAFT_MARKDOWN_NORMALIZE=false` (**new** setting, default
  true) skips the normaliser and the template structure check. Renderer
  fixes are pure improvements; revert the commit if needed.

## Test plan
- `tests/test_draft_markdown.py` (**new**): normaliser rules and
  idempotence, using fictional fixtures.
- `tests/test_docx_generator.py` (**new**): open the rendered bytes with
  python-docx. Covers both the defaults and template-skeleton paths, plus
  the golden fixture.
- `make lint`, `make test`.
- `make e2e` before deploy. Drafting is not covered by e2e, so also run a
  manual check: one AI-only draft and one template draft against a
  connected backend, opened in Word.

## Implementation tasks
1. `draft_markdown.py` + tests.
2. Renderer fixes + tests.
3. Wire normaliser, payload fields and the export log line into
   `drafting_agent.py`; add the schema fields and the setting.
4. Template structure check.
5. Prompt format contract.
6. Docs + CHANGELOG.

## Decisions
- The literal `---` rule is dropped rather than turned into a marker. The
  renderer derives page breaks from schedule and execution headings, so a
  marker adds nothing.
- The structure helpers live in `docx_generator.py`, not
  `draft_markdown.py`, because they parse with the renderer's own parser.
- Page breaks apply to heading levels 1–3, not 1–2: models write
  `### SCHEDULE 1`.
- Ordered list items carry their number as text with a hanging indent, not
  Word numbering. Every `List Number` paragraph shares one numbering
  definition, so a second list would have continued from the first in Word.
- The draft render call passes `task="draft_render"`. It is a stable
  `LLM_ROUTES` key, so drafting can be routed to a stronger model by config
  alone. There is no change to the default model, and the call still goes
  through the router, so gate0 still applies.
- Signature/address blocks (paragraphs with line breaks) are left-aligned
  and kept together. A `_____` line directly after a text line is escaped at
  render time, so CommonMark does not read it as a thematic break and cut
  the block in three.
- A soft line break becomes a Word line break only when every line of the
  block is under 100 characters. That keeps signature/address stacks intact
  while wrapped prose still joins with spaces.
