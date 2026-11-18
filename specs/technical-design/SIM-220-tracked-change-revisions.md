# SIM-220: AI revisions as tracked changes — technical design (AI side)

- **Requirements:** `specs/requirements/SIM-220-tracked-change-revisions.md`
- **Status:** draft — needs approval (written together with the implementation)
- **Approver:** tech lead

## Summary
The revision flow is unchanged up to the revised Markdown. A new
deterministic step then writes the difference between the Markdown the model
was given and its revision into the lawyer's own .docx as Word tracked
changes, and that file is uploaded instead of the clean render. Any fallback
uploads the SIM-219 clean file. Off by default.

## Affected code
| File | Change |
|---|---|
| `app/processing/docx_track_changes.py` | **new**: `build_tracked_revision(...) -> TrackedResult` (never raises; `docx` None + fixed `reason` on fallback) and `apply_revision_as_tracked_changes(...) -> TrackedResult \| None` |
| `app/processing/draft_revision_base.py` | `RevisionBase.docx_bytes` (turn-local); refusal message names the AI's own edits |
| `app/langgraph/drafting_agent.py` | `_TURN_REVISION_BASE` ContextVar (set in `_adopt_revision_base`, reset in `handle_turn`); `_render_and_attach`: verbatim instruction, tracked export, `trackedChanges`, fallback notice; **new** `_export_tracked_revision`, `_tracked_revisions_enabled`, `TRACKED_REVISION_VERBATIM_INSTRUCTION`, `TRACKED_FALLBACK_NOTICE` |
| `app/core/config.py`, `env.example` | `draft_revisions_as_tracked_changes` (false), `draft_tracked_changes_max_changed_ratio` (0.4) |
| `app/socket/schemas.py` | `DraftDocumentPayload.trackedChanges: Optional[bool]` |
| docs | `docs/contracts/backend-ai-socket-events.md`, `docs/socket-events.md`, `docs/features/drafting-and-templates.md` |

## Event and payload changes
Outbound `ai_response`, additive and optional:
`draftDocument.trackedChanges: true` when `downloadUrl` is the lawyer's file
with the AI's edits as tracked changes (and at least one change). Absent for
a clean file. `draftDocument.content` is the revised Markdown either way.
On the too-many-changes fallback `content` ends with one fixed sentence.

## Design
1. **Keeping the base.** `load_revision_base` returns the file's bytes on
   `RevisionBase.docx_bytes`. `_adopt_revision_base` puts `(bytes, adopted
   Markdown)` in `_TURN_REVISION_BASE`; `handle_turn` sets it to None at the
   start of a turn and resets it in `finally`. Never in `session_state`.
2. **When the path is taken.** `_render_and_attach` with `mode ==
   "revision"`, the setting on, and a base adopted this turn. Then the
   prompt gains `TRACKED_REVISION_VERBATIM_INSTRUCTION` (no other path's
   prompt changes).
3. **Like with like.** `_export_tracked_revision` passes the base Markdown
   through `_check_totals(normalize_if_enabled(...))`, the same normaliser
   and totals check, in the same order, as the delivered revision.
4. **Three texts.** A = the file's body blocks (walked like
   `docx_markdown`: content controls opened, empty paragraphs skipped, Word
   list markers and heading styles read with python-docx); B = the
   post-processed base Markdown; R = the revision. B and R are parsed with
   the renderer's own `parse_markdown_blocks` (each list item its own block).
   The edit is B → R; A ↔ B only locates B's blocks, rows and words in the
   file. Text that differs between A and B (normaliser repairs, a totals
   marker) is equal in B and R, so it is never shown as an AI edit.
5. **Block alignment.** `SequenceMatcher` on normalised block keys (list
   markers and emphasis removed). Inside a replaced region, blocks of the
   same type pair when their words are ≥ 50% similar, by an in-order DP
   (capped at 40,000 comparisons; beyond that, nothing pairs).
   Unchanged pair → untouched XML. Changed paragraph → word diff. B block
   without R → deleted. R block without B → inserted.
6. **Word diff.** The paragraph's runs (or a table cell's paragraphs) are
   split at word boundaries, each piece keeping a copy of its `w:rPr`. Words
   are `\w+`, a figure (`1,00,000`) as one word, whitespace runs, line
   breaks, single punctuation. B → R opcodes are mapped onto A through the
   A ↔ B matching blocks: removed words are wrapped in `w:del` (`w:t` →
   `w:delText`, consecutive runs share one wrapper); added words go in one
   `w:ins` after the last deleted or preceding word, with the neighbouring
   run's `w:rPr` plus `w:b`/`w:i` where the Markdown has `**`/`*`.
   `<Label>` placeholders are plain text. Whitespace at line ends is layout
   and never diffed.
7. **Paragraphs.** Inserted: new `w:p` with the `w:pPr` of the nearest
   preceding paragraph of the same kind (heading level, bullet, numbered,
   plain), without `w:sectPr`/`w:pPrChange` (and without `w:numPr` when the
   kind differs), runs from that paragraph's first run, paragraph mark marked
   `w:pPr/w:rPr/w:ins`. A Word-numbered source drops the typed enumerator.
   Deleted: every run in `w:del`, mark `w:pPr/w:rPr/w:del`.
8. **Tables.** Column count must be equal in A, B and R, else fallback
   `table_columns_changed`. Rows pair like blocks. Changed cells: word diff.
   Added row: copy of a neighbouring simple row (no `gridSpan`/`vMerge`),
   cells emptied to `w:tcPr` plus one inserted paragraph, `w:trPr/w:ins`.
   Removed row: `w:trPr/w:del` and its text deleted. A whole new table copies
   a same-width table of the file, or gets a plain bordered grid.
9. **Change elements.** Every `w:ins`/`w:del` has a unique `w:id` (above the
   largest `w:id` in any `word/*.xml` part), `w:author` "SimplerToday AI",
   `w:date` ISO UTC. Only `word/document.xml` is rewritten; every other zip
   entry is copied byte for byte with its original `ZipInfo`.
10. **Fallbacks (`docx` None, fixed reason).** `too_many_changes`: (changed +
    removed B blocks) / B blocks > `DRAFT_TRACKED_CHANGES_MAX_CHANGED_RATIO`.
    A translation changes every block, so it falls back by design.
    `base_mismatch`: < 60% of B blocks found in the file.
    `table_columns_changed`; `unsupported_structure` (a changed paragraph
    with a field, drawing, text box, content control or ruby; a merged cell
    edited; nested table in a changed cell; element walk mismatch);
    `not_a_docx`; `empty`; `error` (any exception). `no_changes` returns the
    base file unchanged (`trackedChanges` absent). The caller also falls back
    on `no_bucket` and `upload_failed`.
11. **Notice.** Only `too_many_changes` appends `TRACKED_FALLBACK_NOTICE` to
    the reply `content` (not to `draftDocument.content`). Technical fallbacks
    only log.
12. **Logging.** One line per revision with a base:
    `draft_tracked_changes outcome=applied|fallback|off inserted= deleted=
    changed_blocks= unmatched= reason=`. Counts and fixed codes only.

## Checklist against CLAUDE.md
- Role gate: n/a (drafting role handling unchanged).
- Long-running work: the writer runs in `asyncio.to_thread`; no new tasks.
- Loaders / `CancelledError`: no loaders. `_export_tracked_revision` catches
  `asyncio.CancelledError` before `except Exception`, logs and re-raises.
- Emits: unchanged (`ai_response` from the chat handler).
- Model calls: none added. The render prompt gains one instruction on this
  path only. `is_generation_error`: no new `generate` call sites.
- Paid calls metered: n/a.
- New caches and purge: none. The base bytes live in a ContextVar for one
  turn. The tracked file is uploaded through `upload_draft_docx`
  (`drafts/<workspaceId>/`), which the purge already reaches.
- `mutations_allowed`: unchanged (drafting has no viewer gate).
- Logging: counts and reason codes; no text, titles or URLs.
- Provider SDKs: none imported.

## Risks and rollback
- **Risk:** a lawyer-saved file whose structure the walker reads differently
  from the conversion → `base_mismatch` fallback (clean file, as SIM-219).
- **Risk:** the model rewords text it was not asked to change; every
  rewording becomes a suggestion. Mitigated by the verbatim instruction and
  the too-many-changes cut-off.
- **Risk:** on very short drafts one changed block is already over 40%; such
  revisions come as clean files. Tunable by setting.
- **Risk:** the AI's suggestions block the next revision until settled
  (decided; the refusal message says so).
- **Rollback:** `DRAFT_REVISIONS_AS_TRACKED_CHANGES=false` (the default).

## Test plan
- `tests/test_docx_track_changes.py` (**new**): one word; paragraph
  inserted/deleted; cell changed, row added, row removed; neighbour font and
  Markdown bold; placeholder; Word numbering kept; headers/footers/comments
  and `sectPr` byte-identical; accept-all = revision and reject-all = base;
  scanner counts the output; unique ids/author/date and re-open; fallbacks
  (different text, columns changed, not a docx); ratio threshold both sides;
  no changes; base-only marker not tracked.
- `tests/test_draft_tracked_revision.py` (**new**): the drafting path with
  model, download and upload stubbed (other model methods raise): setting on
  → tracked upload, `trackedChanges: true`, one word only despite a totals
  marker; verbatim instruction only on the tracked path; setting off →
  SIM-219; writer raising or returning nothing → clean file; full rewrite →
  clean file plus notice.
- Manual: real drafts in ONLYOFFICE and Word (accept/reject all, table rows,
  numbering) before the setting is turned on.

## Decisions
- **`build_tracked_revision` returns a result with a reason**, and
  `apply_revision_as_tracked_changes` (the name in the brief) wraps it and
  returns None on any fallback. The caller needs the reason to tell
  `too_many_changes` (user notice) from technical fallbacks (log only).
- **Three-way diff (B → R, located through A)** rather than diffing the file
  against the revision, so normaliser repairs and totals markers are never
  shown as AI edits.
- **The ratio counts changed and removed blocks only.** Insertions are well
  represented as tracked changes and do not by themselves mean a rewrite.
- **No-change revision** returns the base file unchanged and no
  `trackedChanges`, so the next revision is not blocked.
- **`_renumber_draft_markdown` is not applied to the base.** The brief named
  the normaliser and the totals check; a renumbering of the lawyer's text is
  an AI change and is shown as one.
- **Formatting-only differences are not tracked** (text only); `**` in
  inserted text sets bold on the new run, plain inserted text keeps the
  neighbour's formatting.
