---
title: Drafting and templates
type: feature
repo: law-firm-simplertoday-ai
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

Lawyers keep a library of their own templates: notices, applications,
petitions. The AI service reads each template when it is uploaded and works out
its structure, tone and the blanks it needs filled. The lawyer can approve or
adjust that reading. For each case, the Drafting Assistant suggests four to six
documents the lawyer is likely to need next, and says which of the lawyer's
templates fit each one, with a match percentage. The lawyer can start a draft
from a suggestion, from a template, or as a fully custom AI draft with no
template. The assistant then works through a conversation: it asks for what is
missing, fills what it can from the case evidence, and produces the draft. When
a template is available, the draft also comes back as a Word file in the
template's own formatting.

## Who uses it

- Lawyers building drafts for a case.
- Lawyers managing their template library on the dashboard.
- A new owner after a workspace transfer, whose own library is used from then on.

## What the user sees

A Drafting Assistant panel with suggested drafts, each with its matching
templates. A blueprint approval screen for each new template. A drafting chat
that asks questions, then shows the draft with a download link. See the lawyer
app page in `other_side`.

## How it works

1. **Template library sync.** The backend sends `template_sync` with an
   `action`:
   - `uploaded`: each template is downloaded, its text extracted (scanned
     templates are OCR'd), its type classified and profiled. An unchanged file
     hash reuses the earlier extraction.
   - `renamed`: only the cached name changes.
   - `deleted`: the cached template is dropped.
   - `formatting_updated`: the lawyer's approved blueprint is stored as
     authoritative; the file is not re-read.
   - `preset_created`: a court or letterhead preset (no file body) is stored
     for use at Word export.
2. **Template profiling.** For each new template without an approved
   blueprint, the service emits `template_blueprint_proposal`: the variables it
   found, tone, filing context and outline. `request_blueprint_proposal`
   ("Regenerate Extra Info") re-profiles from scratch and resets any prior
   approval.
3. **Warm library.** Templates live in process memory only. Before matching,
   `ensure_templates_synced` checks the store. If it is empty (for example
   after a restart), it emits `request_template_sync` and waits for the
   `template_sync_response` snapshot. A snapshot that answers our own request
   is authoritative: cached templates missing from it are deleted.
4. **Suggested drafts.** After a run, the agent builds 4-6 suggestions from the
   case summary, laws, strategy, analysis and dates, and pre-matches each
   against the owner's library. The panel goes out as
   `document_feature_response` with `featureType: drafting_assistant`. Each
   card carries `{title, relevance, description, matches[]}`; each match is
   `{templateId, templateName, category, matchPercent}`; `matches` is `[]`
   when nothing fits.
5. **Library changes heal the panel.** When the template count changes, cards
   that already have a live match stay frozen. Unmatched cards are re-matched,
   and cards matched to a deleted template are demoted and re-matched.
6. **Drafting chat.** A `user_message_request` with
   `chat_type: DRAFTING_ASSISTANT` goes to the drafting agent, not the general
   chat. The session is keyed by `actionableId`. A kickoff needs an explicit
   `templateId` or `suggestedDraftId`; a greeting alone never starts a draft.
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
- Matches always come from the current owner's library. On transfer, the new
  owner's library is force-synced before suggestions are rebuilt.
- A deleted template must never drive a draft. Cards matched to it are
  demoted.
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

- `app/socket/handlers/template_sync.py` — `on_template_sync`,
  `on_template_sync_response`, `on_request_blueprint_proposal`,
  `ensure_templates_synced`.
- `app/socket/handlers/drafting.py` — suggestions panel, re-match on library
  change.
- `app/socket/handlers/chat.py` — `_handle_drafting_assistant_message`.
- `app/langgraph/drafting_agent.py` — suggest, ingest, profile, match,
  `handle_turn`, `_export_draft_docx`.
- `app/langgraph/template_store.py` — in-memory, user-scoped template cache.
- `app/processing/docx_template_filler.py`, `app/processing/docx_generator.py`,
  `app/processing/pdf_to_docx.py`.
- `app/prompts/drafting.py`; wire transform `drafting_payload_to_wire` in
  `app/socket/event_utils.py`.

### Events

Inbound: `template_sync`, `template_sync_response`,
`request_blueprint_proposal`, `user_message_request` (drafting chat),
`workspace_transferred`. Outbound: `request_template_sync`,
`template_blueprint_proposal`, `document_feature_response`
(`drafting_assistant`), `ai_response` (with draft fields). See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- Templates: process memory only, keyed by `(userId, templateId)`.
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

- No dedicated drafting or template test suite exists. `drafting_assistant`
  appears only as one of the resumed panels in
  `tests/offline/verify_run_control.py` and in
  `tests/offline/verify_objective_change.py`.

### Known gaps

- No automated tests for template sync, profiling, matching, the drafting
  conversation or Word export.
- `ensure_templates_synced` waits with no time cap by default. If the backend
  never replies to `request_template_sync`, the wait does not end.
- Generated draft files and template skeletons in cloud storage are never
  deleted: not on template delete, and not by the workspace purge.
- Approved blueprints and presets live in memory and rely on the backend
  re-sending them in the snapshot after a restart; this was not verified.
- The docstring of `on_template_sync` describes a "lawyer-scoped Qdrant
  cache"; the code now uses process memory (`template_store.py`). The
  docstring is out of date (comment only, no behaviour impact).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
