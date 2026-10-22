---
title: Create a workspace and upload evidence
type: flow
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, backend, ai-service]
other_sides:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4390999
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013525
related_features: [../features/evidence-upload.md, ../features/analysis-panels.md, ../features/role-selection.md, ../features/ai-cost-tracking.md]
related_specs: []
---

# Create a workspace and upload evidence

## In plain words

When a lawyer opens a new case and adds documents, the AI service is told about
the case and given a temporary link to each file. It records which side the
lawyer acts for, reads every file (including scanned pages, audio recordings and
spreadsheets), and stores what it read so it can search it later. It then sends
back a short assessment of each document and a suggested case category. After
that, it builds the analysis panels one after another in the background, so the
lawyer can already ask questions in chat while the panels fill in. When the
lawyer later adds more documents to the same case, the service reads only the new
ones, shows a loading state on the panels that already exist, and rebuilds them
with the new evidence included.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app | Lawyer creates the case, picks a role, uploads files | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013525) |
| 2 | Backend | Stores the files and sends `workspace_created` with the role and a signed link per file | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4390999) |
| 3 | AI service | Records the role, reads and indexes every file | This page, steps A1-A3 |
| 4 | AI service | Sends `document_assessments`, then `workspace_metadata` | This page, steps A4-A5 |
| 5 | AI service | Builds the panels in the background and sends each one as it is ready | This page, step A6; [Evidence to analysis panels](evidence-to-analysis-panels.md) |
| 6 | Backend | Saves each panel and forwards it to the app | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4390999) |
| 7 | Lawyer app | Shows each panel as it arrives | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013525) |
| 8 | Lawyer app, backend | Lawyer adds more files; backend sends `workspace_sync` with the full file list | Other sides' pages |
| 9 | AI service | Reads only the new files and rebuilds the panels | This page, steps S1-S6 |

## The AI service's steps in detail

### New case (`workspace_created`)

- **A1. Accept the event.** The payload is validated against
  `WorkspaceCreatedPayload`. Any pending compliance purge for this workspace id is
  revoked. Custom instructions (structure, tone, description) are stored if sent.
  The workspace is remembered as the one to replay after a reconnect.
- **A2. Set the role.** The role is upper-cased; a missing or unknown role becomes
  `ADJUDICATOR` (logged as a warning). The role is persisted, the workspace
  context is created, and the role agent is run through
  `initialize_workspace_with_role`. If the role agent fails, the error is logged
  and creation continues.
- **A3. Read the files.** `document_processor.process_batch` downloads each file
  from its signed link and extracts text by type: text-layer PDFs, OCR for
  scans and images, transcription for audio, typed tables for spreadsheets. Text
  is chunked, embedded and stored in the evidence collection. The whole batch
  runs inside one cost scope, so OCR, speech-to-text, embedding and per-document
  model calls are billed to this case.
- **A4. Send the document assessments.** One `document_feature_response` with
  `featureType: document_assessments` lists every file with its summary,
  readability and strength. It carries the ingestion cost in `metadata`. The
  payload is cached as the case's document manifest (replacing any older one),
  and any successfully read document is cleared from the removed-evidence ledger.
- **A5. Send the case category.** If at least one file was read, one model call
  produces the title, category, subcategories and description, sent as
  `workspace_metadata` with `updateTitle: true` and cached.
- **A6. Build the panels in the background.** If at least one file was read, a
  tracked background task takes the per-workspace pipeline lock and produces
  strategy, analysis report, list of dates, applicable laws, procedural timeline,
  relevant parties, case laws and drafting suggestions, in that order. The event
  handler returns before this finishes, so chat is never blocked.
- **A7. Nothing readable.** If no file could be read, or the case has no files,
  the service sends a fallback analysis report and an empty list of dates, then
  schedules the remaining panels so every panel still gets a terminal state.

### More files on an existing case (`workspace_sync`)

- **S1. Roster and state first.** The sharing roster is updated (see
  [Share a case and revoke access](share-a-case-and-revoke-access.md)) and the
  lawyer's curated panel state (`currentState`) is cached when present.
- **S2. Drop replays.** A sync whose fingerprint (file ids, deleted ids,
  `updatedAt`, role, title, status, a hash of `currentState`) matches one seen in
  the last 180 seconds is ignored.
- **S3. Work out what changed.** Deleted ids come from `deletedDocumentIds`, from
  files flagged `isDeleted`, and from documents in the vector store that are no
  longer in `evidenceFiles` (only when every listed file has an id). Files already
  in the vector store are filtered out, so only genuinely new files are read.
- **S4. Show loaders early.** If the case already has panels, one
  `workspace:refresh` arms loaders for document assessments and all eight panels
  before reading starts. A first-ever upload does not send it.
- **S5. Read and report.** New files go through the same batch as A3. The
  `document_assessments` emit lists only the new files, but the cached manifest is
  merged, never replaced, so earlier files stay "present" in every prompt. A fresh
  `workspace_metadata` is sent with `updateTitle: false` so the case title is
  kept.
- **S6. Rebuild.** The same background pipeline as A6 runs with `is_update=True`.
  Deletions, role-only changes, an empty file list and a no-change re-sync take
  other branches, covered in [Evidence to analysis panels](evidence-to-analysis-panels.md).

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| A2 | Role missing or not one of the four | Panels framed as adjudicator | Defaults to `ADJUDICATOR`, logs a warning |
| A2 | Role agent raises | Nothing different | Logs the error, carries on with ingestion |
| A3 | One file cannot be downloaded or read | That file shows as failed or unreadable | Other files continue; the batch reports per-file success |
| A3 | Ingestion raises part-way | Document panel shows an error | Sends `document_assessments` with `error` and the partial cost spent |
| A6 | No files readable | Empty-state panels | Sends fallback analysis and empty dates, then the rest |
| A6 | Socket drops while panels are being sent | Panels arrive after reconnect | Undelivered panels are buffered and re-sent on reconnect |
| S2 | Backend re-sends the same sync | Nothing | Ignored inside the 180-second window |
| S3 | Backend omits deleted files from the list but sends no ids | Deleted file disappears from panels | Inferred as removed from the vector store comparison |

## For engineers

**Entry and exit.** Entry: `on_workspace_created` and `on_workspace_sync` in
`app/socket/handlers/workspace_events.py`, registered in `app/socket/events.py`.
Exit: one `document_feature_response` per panel plus `workspace:refresh` on a
sync with prior panels.

**Events and payloads.** `WorkspaceCreatedPayload`, `WorkspaceSyncPayload`,
`EvidenceFilePayload` in `app/socket/schemas.py`. Contract:
[Backend and AI service socket events](../contracts/backend-ai-socket-events.md).
`aiModel` on these events sets the case's model choice (`_apply_model_selection`
in `app/socket/events.py`). The ingestion `document_assessments` and
`workspace_metadata` envelopes are built by hand and sent with the raw socket
`emit`, not through `_emit_document_feature`.

**State and timing.**
- Ingestion: `app/processing/document_processor.py`, `app/processing/file_handlers.py`,
  `app/processing/ocr.py`, `app/processing/tables.py`, `app/processing/audio_transcription.py`.
- Scheduler: `_schedule_strategy_analysis_pipeline` and
  `_run_strategy_analysis_timeline_pipeline` in `app/socket/handlers/objective_change.py`;
  the tail runs inline under the same lock (`tail_inline=True`).
- Manifest: `_merge_document_assessments`, `_prune_document_assessments`,
  `_forget_removed_documents` (see CLAUDE.md "Deleted documents must stay visible as gaps").
- Every inbound event runs in a run scope, so a `workspace_stop_request` can cancel
  `workspace_created` / `workspace_sync` and fence their emits
  (`app/socket/handlers/run_control.py`).

**Security checks.** Signed file URLs are never logged (`safe_url`); the sync
log line records counts only. Purge revocation (`cancel_pending_purge`) is the
first thing both handlers do.

**Tests.** No offline suite drives `on_workspace_created` / `on_workspace_sync`
end to end. `make e2e` (`scripts/e2e_local_backend.py`) covers `workspace_created`
with real model calls. Ingestion pieces: `tests/offline/verify_ingestion.py`,
`tests/test_ocr.py`, `tests/test_tables.py`, `tests/test_office_extraction.py`,
`tests/test_audio_long_recordings.py`. Billing: `tests/test_usage_ledger.py`.

**Known gaps.**
- The role gate is not strict on this path: a bad role becomes `ADJUDICATOR`
  and a failed role agent does not stop ingestion.
- The sync path sends `workspace_metadata` but does not update the cached copy,
  so a reconnect replays the category from creation time.
- Ingestion envelopes go out via the raw socket, so a dropped
  `document_assessments` is not buffered for re-delivery; it relies on the cached
  reconnect replay instead.
- Reconnect replay covers one workspace only (the last one that armed it).

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
