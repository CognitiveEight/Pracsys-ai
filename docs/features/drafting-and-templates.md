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
7. **Word export.** The draft is rendered to DOCX. With a template that has
   placeholders, they are filled in place in the original file. Otherwise the
   template is cloned and re-filled with its own styles. With no template,
   default styles are used. The file is uploaded to cloud storage and its link
   is returned as `draftDocument.downloadUrl`.

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
| Word export fails | Draft shows, no Word download | Returns `draftDocument` without `downloadUrl` |
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
- `app/langgraph/drafting_agent.py` — suggest, ingest, profile, match,
  `handle_turn`, `_export_draft_docx`.
- `app/langgraph/template_store.py` — in-memory template cache: lawyers' own
  libraries plus the Global Template Library (current version, admin drafts,
  older versions pinned by drafts).
- `app/processing/docx_template_filler.py`, `app/processing/docx_generator.py`,
  `app/processing/pdf_to_docx.py`.
- `app/prompts/drafting.py`; wire transform `drafting_payload_to_wire` in
  `app/socket/event_utils.py`.

### Events

Inbound: `global_template_sync`, `global_template_sync_response`,
`request_blueprint_proposal` (`scope: "global"`), `user_message_request`
(drafting chat), `workspace_transferred`. Outbound:
`request_global_template_sync` (every connect),
`template_blueprint_proposal`, `document_feature_response`
(`drafting_assistant`), `ai_response` (with draft fields). See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- Templates: process memory only, keyed by `(userId, templateId)`; global
  templates under a reserved owner, keyed by template id and version.
- Draft sessions: the Qdrant workspace state cache (`drafting_session`).
- Template DOCX skeletons and generated drafts: cloud storage
  (`GCS_DOCX_BUCKET_NAME`), under `templates/` and `draft/` prefixes.
- Suggestions: `_workspace_drafting_payload`, re-emitted on reconnect.

### Security

- Download links for generated drafts are long-lived signed URLs (about 25
  years). Only `safe_url()` forms are logged.
- Drafting chat is not gated by sharing permission: a view-only collaborator
  can run a drafting session. Drafting does not change case panels.

### Tests that guard it

- `tests/test_global_templates.py` covers the Global Template Library feed,
  global profiling and version pinning.
- No other dedicated drafting or template test suite exists. `drafting_assistant`
  appears only as one of the resumed panels in
  `tests/offline/verify_run_control.py` and in
  `tests/offline/verify_objective_change.py`.

### Known gaps

- No automated tests for matching quality, the drafting conversation or Word
  export.
- Generated draft files and template skeletons in cloud storage are never
  deleted: not on template delete, and not by the workspace purge.
- Approved blueprints and presets live in memory and rely on the backend
  re-sending them in the snapshot after a restart; this was not verified.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-29 | Global Template Library feed, versions, global profiling; per-lawyer template events removed | SIM-173 |
