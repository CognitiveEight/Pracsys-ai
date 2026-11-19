---
title: Drafting and templates
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Evidence to analysis panels
  - Transfer a workspace to another owner
other_side:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5242901
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4391021
related_specs: []
---

# Drafting and templates

## In plain words

Administrators keep one Global Template Library for the whole platform:
notices, applications, petitions, letterheads and court formats (SIM-58 /
SIM-173). Lawyers no longer have templates of their own. The AI service reads
each template when an admin uploads it and works out its structure, tone and
the blanks it needs filled; the admin can approve or adjust that reading before
publishing. For each case, the Drafting Assistant suggests four to six
documents the lawyer is likely to need next, and says which published templates
fit each one, with a match percentage. The lawyer can start a draft
from a suggestion, from a template, or as a fully custom AI draft with no
template. The assistant then works through a conversation: it asks for what is
missing, fills what it can from the case evidence, and produces the draft. When
a template is available, the draft also comes back as a Word file in the
template's own formatting.

## Who uses it

- Lawyers building drafts for a case, from the published global templates.
- Administrators publishing, versioning and retiring templates in the admin
  panel.

## What the user sees

A Drafting Assistant panel with suggested drafts, each with its matching
templates. A blueprint approval screen in the admin panel for each new global
template. A drafting chat
that asks questions, then shows the draft with a download link. See the lawyer
app page in `other_side`.

## How it works

1. **Global template library sync.** Admins publish versioned templates; the
   backend sends `global_template_sync` once for the platform (no `userId`):
   - `published`: the template becomes the current version. A new file is
     downloaded, its text extracted (scanned templates are OCR'd), its type
     classified and profiled; the same file hash reuses the earlier extraction,
     and the same version and hash again is a no-op. An approved blueprint in
     `formatting.blueprint` is stored as authoritative. A preset (no file) is
     stored for use at Word export.
   - `retired`: never suggested again; a draft already started from it keeps
     working.
   - `deleted`: a never-published draft is erased.
   The per-lawyer `template_sync` / `template_sync_response` /
   `request_template_sync` events are no longer handled.
2. **Template profiling.** `request_blueprint_proposal` (on admin upload or
   "Re-profile"; `scope: "global"`, no `userId`) profiles the draft template,
   which is never suggested before it is published. The service replies
   `template_blueprint_proposal` with `scope: "global"`, the request's
   `fileHash` and no `userId`: the variables it found, tone, filing context
   and outline, or `status: "failed"`.
3. **Warm library.** Templates live in process memory only. On every connect
   the service emits `request_global_template_sync` and applies the
   `global_template_sync_response` snapshot as a full replace: anything held
   that it omits is retired. `ensure_templates_synced` waits (bounded) for that
   snapshot before matching.
4. **Suggested drafts.** After a run, the agent builds 4-6 suggestions from the
   case summary, laws, strategy, analysis and dates, and pre-matches each
   against the published global templates. The panel goes out as
   `document_feature_response` with `featureType: drafting_assistant`. Each
   card carries `{title, relevance, description, matches[]}`; each match is
   `{templateId, templateName, category, matchPercent}`; `matches` is `[]`
   when nothing fits.
5. **Library changes heal the panel.** When the template count changes, cards
   that already have a live match stay frozen. Unmatched cards are re-matched,
   and cards matched to a retired or deleted template are demoted and
   re-matched.
6. **Drafting chat.** A `user_message_request` with
   `chat_type: DRAFTING_ASSISTANT` goes to the drafting agent, not the general
   chat. The session is keyed by `actionableId`. A kickoff needs an explicit
   `templateId` or `suggestedDraftId`; a greeting alone never starts a draft.
   A draft keeps the `templateVersion` it started from, fetched from
   `templateUrl` when that version is not held.
   `draftMode: custom` ("Custom AI Draft") skips all template lookup and drafts
   from the case alone. Each reply carries `draftStatus` (analyzing,
   gathering_info, ready, revising), `openQuestions` and, when ready,
   `draftDocument` (Markdown).

   **Revising the lawyer's latest version (SIM-219).** The backend stores
   every version as a .docx, including the lawyer's edits made in the editor.
   When a drafting turn carries `latestVersion` `{version, url, expiresAt,
   openChanges, sha256}` (or the earlier `baseDocumentUrl` / `baseVersion`),
   the agent reads that file once, before any model call
   (`app/processing/draft_revision_base.py`):
   - `latestVersion.openChanges` true → refused with `open_changes` before
     any download (no counts);
   - tracked changes or comments still open (`docx_review_state.py`, the
     contract's list) → refused with `draftRefusal.code: "open_changes"` and
     the counts; the lawyer settles them in the editor first;
   - no link, link failed or expired, file too large, SHA-256 different from
     `latestVersion.sha256`, or not a .docx → refused with `base_unreadable`.
     The AI's own copy is never used instead;
   - otherwise the body is read back into the drafting Markdown
     (`docx_markdown.py`: headings, GFM tables, lists, bold/italic, line
     breaks) and replaces the session's draft, so the revision is vN+1 of the
     lawyer's vN. `draftDocument` echoes `draftId` and `baseVersion`.

   A refusal makes no model call, sends no `draftDocument`, and keeps
   `draftStatus: ready`. Turns without `latestVersion` or `baseDocumentUrl` behave as before.
   One `draft_revise_from_base outcome= tracked= comments= chars=` line is
   logged per turn that carries a base (counts only; the link is never
   stored, and logged only without its query string).

   **Revisions as tracked changes (SIM-220, `DRAFT_REVISIONS_AS_TRACKED_CHANGES`,
   default off).** On a revision built from the lawyer's file, the AI returns
   that same file with its edits as Word tracked changes (author
   "SimplerToday AI"), so the lawyer accepts or rejects them in the editor.
   `app/processing/docx_track_changes.py` lines up the file's paragraphs and
   tables with the Markdown the model saw and with its revision (difflib on
   blocks, then words) and writes only the differences: changed words as
   `w:del`/`w:ins` with the neighbouring run's formatting, new and removed
   paragraphs and table rows with their marks. Everything else keeps its XML;
   other parts of the file are copied byte for byte. No model call is added;
   the revision prompt gains one instruction to copy untouched text verbatim.
   The file falls back to the clean rebuild (and `trackedChanges` is absent)
   when the revision changed more than `DRAFT_TRACKED_CHANGES_MAX_CHANGED_RATIO`
   (0.4) of the blocks (the reply then says so), when a table's columns
   changed, or when a changed paragraph holds a field, drawing or content
   control. One `draft_tracked_changes outcome= inserted= deleted=
   changed_blocks= unmatched= reason=` line per revision.
7. **Word export.** First, the draft Markdown is normalised
   (`app/processing/draft_markdown.py`, SIM-202). Table rows are joined and
   given a separator. `\<Label\>` becomes `<Label>`. Headings deeper than
   `###` are lowered and `---` rules are dropped. The draft is then rendered
   to DOCX:
   - **With a template:** it is filled in place in the original file. If the
     filled file lost tables or sections the draft has, the full draft is
     rendered in the template's styles instead.
   - **With no template:** default styles are used.

   The renderer emits:
   - real Word tables (bold repeating header row, columns sized to fit the
     page);
   - nested lists;
   - styled headings 1–6;
   - a page break before schedules, annexures and the execution page;
   - yellow-highlighted `<Label>` placeholders;
   - line breaks inside signature blocks.

   **The lawyer's chosen format is applied last (SIM-217).** The
   `formatLayout` sent with every drafting message (court or letterhead) is
   applied to the rendered file by `app/processing/draft_format.py`
   (`apply_format_layout`), on every route: template fill, `llm_diff`,
   heuristic fill, skeleton clone and defaults. Its page settings override
   the template's and the SIM-208 standard layout; a letterhead replaces the
   header and footer; a court block is inserted at the top unless the draft
   already starts with it. Letterhead images are fetched once by
   `app/processing/format_images.py`; a failed image is skipped. A formatter
   failure uploads the unformatted file. One `draft_format applied type= …`
   line (counts and enum values only) is logged per export. The function
   works on any .docx, so it can later re-apply a format to a lawyer-edited
   file.

   The file is uploaded to cloud storage and its link is returned as
   `draftDocument.downloadUrl`, with `docxStatus` and `placeholderCount`.
   One `docx_export route= tables= placeholders= leftover_markup=` line is
   logged per export. `DRAFT_MARKDOWN_NORMALIZE=false` turns off the
   normaliser and the template structure check.
8. **Template fidelity (SIM-203, off by default).** Package
   `app/processing/template_fidelity/`.
   - **`TEMPLATE_FIDELITY_INGEST`.** A PDF, scanned PDF or photo template is
     rebuilt as a clean, editable .docx from its measured format. That
     covers fonts, sizes, emphasis, alignment, indents, numbering labels,
     spacing, tables, letterhead and logo, header/footer, page numbers and
     margins.
     - A text PDF is read from its own spans.
     - A scan is read from Vision OCR word positions plus pixel
       measurements.
     - `TEMPLATE_SCAN_VISION` adds one Gemini vision call per page, for
       tables and the font family.
     - On any failure, pdf2docx runs as before.
   - **`TEMPLATE_FILL_V2`.** At export the draft is written *into* the
     template.
     - The model sees an ID for every paragraph, row and cell, and returns
       operations (`replace`, `insert_after` with `like`, `add_row`,
       `delete`).
     - The operations are applied in place, so each edit keeps that
       paragraph's own format, new clauses copy their sibling's format, and
       the header and footer are never touched.
     - On any failure, the `llm_diff` chain runs.
   - Logs: `template fidelity built …` and `template_fill ops= applied= …`
     (counts only).
9. **Rebuild Word file (SIM-216).** `draft_file_build_request` builds a
   .docx from a draft the backend already stores ("Rebuild Word file", and
   the first Word file of an older draft) with the request's `formatLayout`
   applied, and always answers `draft_file_build_response` (`status: ok`
   with `downloadUrl`, or `status: error` with `errorCode`).
   - `format: "markdown"` goes through the normaliser; `format: "html"` (the
     in-app editor) is first converted to the same Markdown
     (`app/processing/draft_html.py`: headings, bold/italic, nested lists,
     tables, line breaks; scripts, styles and unknown tags dropped, no
     attributes kept).
   - Same renderer as the chat (`DraftingAgent.build_docx_from_text` →
     `_render_draft_docx`): the template when `templateId` is given and
     held, else the standard layout for the title. Uploaded through
     `upload_draft_docx`, so the purge reaches it.
   - Idempotent on `requestId` (in flight: swallowed; answered: replayed
     for 10 minutes). Capped by `DRAFT_DOCX_BUILD_TIMEOUT_SECONDS` (120).
     A deleted case is refused and its pending purge is not revoked. Stop
     does not cancel it.
   - One `draft_docx_build status= reason= format= route= template= chars=
     placeholders=` line per build; never the title, text or link.

## Rules and limits

- The backend owns the template library. This service only caches it.
- Template work is user-scoped, not case-scoped, so its cost is priced and
  logged but not billed to any case.
- Matches come only from the published Global Template Library (SIM-173),
  the same for every lawyer and firm. A transfer changes nothing about it.
- A retired global template is never suggested, but a draft already started
  from it keeps working, at the version it started from.
- A retired or deleted template must never drive a new draft. Cards matched
  to it are demoted.
- If Word export fails, the Markdown draft still ships without `downloadUrl`.
- The Analysis report download (`analysis_docx_generate_request`) is a
  separate, deterministic export and is not part of drafting.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| AI restarted, template store empty | Suggestions arrive a little later | Requests a full snapshot and waits for it before matching |
| Template OCR finishes after suggestions were built | A card gains a template match later | Library count change triggers a re-match of unmatched cards |
| Matched template deleted | The card shows other matches or none | Card demoted and re-matched against the current library |
| Draft storage bucket not configured (`GCS_DOCX_BUCKET_NAME`) | Draft shows, no Word download | Export returns nothing; Markdown still returned |
| Word export fails | Draft shows, no Word download | Returns `draftDocument` without `downloadUrl`; the backend can ask again with `draft_file_build_request` |
| Rebuild requested for a deleted case | Rebuild fails | `draft_file_build_response` `error` / `invalid_request`; the purge stays armed |
| Latest version has open tracked changes or comments (including the AI's own suggested edits) | "…including any changes suggested by the AI. Accept or reject them in the editor, then ask again." | `draftRefusal: open_changes` with counts; no model call |
| A tracked-changes revision rewrote most of the draft (SIM-220) | A clean new version, and one sentence saying why | Clean rebuild; `trackedChanges` absent; `reason=too_many_changes` |
| Latest version's link expired or file unreadable | "I could not open the latest version…" | `draftRefusal: base_unreadable`; no model call |
| Suggestion generation fails | Empty Drafting Assistant panel | Returns an empty list; never blocks case creation |
| Blueprint profiling fails | No approval screen for that template | Per-template failure is logged and skipped |

## For engineers

### Where the code is

- `app/socket/handlers/template_sync.py` — `on_global_template_sync`,
  `on_global_template_sync_response`, `request_global_template_sync`,
  `on_request_blueprint_proposal`, `ensure_templates_synced`.
- `app/socket/handlers/drafting.py` — suggestions panel, re-match on library
  change.
- `app/socket/handlers/chat.py` — `_handle_drafting_assistant_message`.
- `app/socket/handlers/draft_docx_build.py` — `on_draft_docx_build_request`
  (SIM-216); `app/processing/draft_html.py` — editor HTML to draft Markdown.
- `app/langgraph/drafting_agent.py` — suggest, ingest, profile, match,
  `handle_turn`, `_export_draft_docx`.
- `app/langgraph/template_store.py` — in-memory template cache: lawyers' own
  libraries plus the Global Template Library (current version, admin drafts,
  older versions pinned by drafts).
- `app/processing/draft_format.py` (chosen court/letterhead format, SIM-217)
  and `app/processing/format_images.py` (its image fetcher).
- `app/processing/docx_template_filler.py`, `app/processing/docx_generator.py`,
  `app/processing/pdf_to_docx.py`.
- `app/prompts/drafting.py`; wire transform `drafting_payload_to_wire` in
  `app/socket/event_utils.py`.

### Events

Inbound: `global_template_sync`, `global_template_sync_response`,
`request_blueprint_proposal` (`scope: "global"`), `user_message_request`
(drafting chat), `workspace_transferred`, `draft_file_build_request`.
Outbound:
`request_global_template_sync` (every connect),
`template_blueprint_proposal`, `document_feature_response`
(`drafting_assistant`), `ai_response` (with draft fields),
`draft_file_build_response`. See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- Templates: process memory only, keyed by `(userId, templateId)`; global
  templates under a reserved owner, keyed by template id and version.
- Draft sessions: the Qdrant workspace state cache (`drafting_session`).
- Template DOCX skeletons and generated drafts: cloud storage
  (`GCS_DOCX_BUCKET_NAME`, the same bucket the backend uses for
  `templates/`, `letterheads/`, `avatars/` and `audio/`). AI skeletons under
  `ai-templates/` (older ones stay under `templates/` and remain readable
  through their stored gs:// URI); generated drafts under
  `drafts/<workspaceId>/<uuid>.docx` (SIM-211), written only through
  `app/storage/draft_files.upload_draft_docx`. The old
  `draft/<userId>/<uuid>-<title>.docx` layout is removed by
  `scripts/cleanup_legacy_draft_files.py`.
- Suggestions: `_workspace_drafting_payload`, re-emitted on reconnect.

### Security

- Download links for generated drafts are short-lived signed URLs
  (`DRAFT_DOCX_LINK_TTL_SECONDS`, default 15 minutes): a hand-off the backend
  copies at once. No draft title in object names. Only `safe_url()` forms are
  logged.
- Drafting chat is not gated by sharing permission: a view-only collaborator
  can run a drafting session. Drafting does not change case panels.

### Tests that guard it

- `tests/test_global_templates.py` covers the Global Template Library feed,
  global profiling and version pinning.
- `tests/test_draft_html.py` (HTML to Markdown) and
  `tests/offline/verify_docx_build.py` (`draft_file_build_request`) cover
  the Word-file rebuild (SIM-216).
- `tests/test_draft_format.py` covers the chosen format (SIM-217): page,
  letterhead, court block, images, bad input, the export path and the
  session copy.
- No other dedicated drafting or template test suite exists. `drafting_assistant`
  appears only as one of the resumed panels in
  `tests/offline/verify_run_control.py` and in
  `tests/offline/verify_objective_change.py`.

### Known gaps

- No automated tests for matching quality or the drafting conversation.
  Word export rendering is covered by `tests/test_docx_generator.py` and
  `tests/test_draft_markdown.py` (SIM-202), but the template-diff path
  (`llm_diff`) is not.
- Template tables are not read at ingestion, so the model never sees a
  template's tables (`extract_text_from_docx` without `mark_pages`).
- Template skeletons in cloud storage are not deleted on template delete.
  Generated draft files are deleted by the workspace purge (SIM-211).
- Approved blueprints and presets live in memory and rely on the backend
  re-sending them in the snapshot after a restart; this was not verified.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-29 | Global Template Library feed, versions, global profiling; per-lawyer template events removed | SIM-173 |
| 2026-11-17 | `draft_docx_build_request`: Word file from stored draft text (Markdown or editor HTML) | SIM-216 |
| 2026-11-17 | The lawyer's chosen court/letterhead format is applied to the draft's Word file | SIM-217 |
