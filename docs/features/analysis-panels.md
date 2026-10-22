---
title: Analysis panels
type: feature
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Create a workspace and upload evidence
  - Evidence to analysis panels
other_side:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013543
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4194309
related_specs: []
---

# Analysis panels

## In plain words

When a lawyer creates a case and uploads evidence, the AI service reads every
document and fills a set of panels: an assessment of each document, the case
details, a strategy and objective, an analysis report with a readiness score,
a list of dates, the applicable laws, the procedural timeline, the people
involved, relevant case laws and drafting suggestions. It builds them one
after another and sends each panel as soon as it is ready. When documents
change, the panels are rebuilt, but items the lawyer already approved or
dismissed keep their identity. If a document is deleted, the gap it leaves is
shown as missing information rather than quietly forgotten. Points in the
panels link back to the exact page they came from where that can be checked.

## Who uses it

- Lawyers, who read and curate the panels.
- The backend, which stores each panel and echoes the lawyer's curated state back.

## What the user sees

Panels fill in one by one, each with a loading state while it generates. See
[Analysis panels — lawyer app](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4194309)
and [Analysis panels — backend](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013543).

## How it works

1. `workspace_created` (or `workspace_sync` with new documents) arrives. The
   role is resolved first ([Role selection](role-selection.md)), then the
   evidence files are batch-processed into the vector store.
2. `document_assessments` and `workspace_metadata` are built and emitted
   straight away.
3. A background pipeline, serialised per workspace by a lock, runs in product
   order: `strategy_objective`, `analysis_report`, `list_of_dates`,
   `applicable_laws`, `procedural_timeline`, `relevant_parties`, `case_laws`,
   then drafting suggestions (`drafting_assistant`). Chat is never blocked by it.
4. Each panel leaves through one chokepoint, `_emit_document_feature`. It
   applies the lawyer's fact corrections, adds register status and section
   titles to laws, attaches verified page citations, adds cost metadata, and
   emits `document_feature_response`. Every result is cached per workspace
   and persisted to Qdrant.
5. On a regeneration, items are tagged against the `currentState` the
   frontend echoed. A fresh item that matches a previous one keeps its id
   (`isMatched: true`); a new one gets a new id; dismissed items do not come
   back. Analysis items match by meaning (exact text first, then one model
   pass); laws match on act plus section.
6. Lawyer actions on single items are handled without a full rerun:
   `missing_information_edited` (reword one item, no model call),
   `added_missing_information`, `analysis_report_feedback`, and add, edit or
   delete of relevant parties.
7. After a reconnect, any undelivered panel is re-sent first, then the
   cached panels are replayed in product order.

## Rules and limits

- The `document_assessments` cache is the document manifest. It is merged on
  add and pruned on delete before any prompt is built, because every panel
  prompt treats it as ground truth for which documents exist.
- A deleted document is recorded in a removed-evidence ledger. Every
  analysis report gets at least one missing-information gap per removed
  document type, even if the model leaves it out, and rejected-item memory
  does not hide that gap. Re-ingesting the document clears the entry.
- A lawyer's reworded missing-information item survives every regeneration
  and keeps its id. A re-sent `requestId` is ignored; an older `editedAt` is
  stale; a view-only collaborator's edit is not applied.
- The readiness score explanation is relative to the current strategic
  objective. An objective change forces it to be regenerated, never reused.
- Every loader the service arms with `workspace:refresh` must be cleared by a
  `document_feature_response` for the same panel. Exit paths that cannot
  produce one call `_release_refresh_loaders` with cached or fallback content.
- Panel citations are additive fields only. The quote is copied from the
  stored passage and the page is where the quote is; a weak match gives no
  citation. Cited panels: strategy, analysis report, list of dates,
  procedural timeline, applicable laws.
- Laws and case laws are capped at 12 each (see
  [Case laws and applicable laws](case-laws-and-applicable-laws.md)).
- Prompts pack documents with `fit_to_budget` (default 200,000 characters).
- A Stop fences old-run emits; a resume emits only the panels it declared.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Model call fails or returns no JSON for a panel | A fallback or the previous panel content | Emits a fallback or cached payload so the loader clears; logs the error |
| Socket is disconnected when a panel is ready | The panel arrives after reconnect | Buffers the envelope and re-sends it before the cached replay |
| Generation is cancelled mid-run (disconnect, stop) | Loaders clear; a message if an objective change did not land | Catches the cancellation, releases armed loaders, re-raises |
| A document is deleted | A new missing-information gap for it | Prunes the manifest, records the ledger, regenerates |
| Lawyer rewords a missing-information item, then documents change | Their wording stays | The edit is re-applied on every tagging pass |
| Two triggers overlap (upload while generating) | Panels update in order, not interleaved | The per-workspace lock serialises the pipelines |
| Service restarted between events | Panels still re-emit | Caches are hydrated from Qdrant before use |

## For engineers

### Where the code is

- Orchestration: `app/socket/handlers/workspace_events.py`,
  `app/socket/handlers/objective_change.py` (pipeline, lock, `currentState` tagging)
- Per panel: `app/socket/handlers/documents.py`, `workspace_metadata.py`,
  `strategy.py`, `analysis_report.py`, `list_of_dates.py`, `applicable_laws.py`,
  `procedural_timeline.py`, `relevant_parties.py`, `case_laws.py`, `drafting.py`
- Shared: `app/socket/handlers/_shared.py` (`_release_refresh_loaders`,
  `_reconcile_against_current_state`, pending-delivery buffer),
  `app/socket/handlers/chat.py` (`_emit_document_feature`)
- Citations: `app/rag/attribution.py`; packing: `app/rag/packing.py`
- Stop and resume: `app/socket/handlers/run_control.py`

### Events

- In: `workspace_created`, `workspace_sync`, `workspace_version_restore`,
  `document_reload_request`, `workspace_resume_request`,
  `missing_information_edited`, `added_missing_information`,
  `analysis_report_feedback`, `add_relevant_party`, `edit_relevant_party`,
  `delete_relevant_party`.
- Out: `document_feature_response` (`featureType` one of the panels above,
  plus `applicable_law_relevance`), `workspace:refresh`.
- Full contract: [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

### Data

- In-memory panel caches per workspace, persisted through Qdrant cache
  kinds; the removed-documents and edited-missing-info ledgers.
- `PENDING_FEATURE_DELIVERY` holds undelivered envelopes per `(workspace, featureType)`.

### Security

- Logs carry item counts and sizes, never panel text or names.
- All per-workspace caches are reached by the workspace purge sweep.

### Tests that guard it

- `tests/offline/verify_missing_info_edit.py`
- `tests/offline/verify_objective_change.py` (groups 11 and 12: score explanation, loader release)
- `tests/offline/verify_run_control.py` (stop fence, resume order)
- `tests/offline/verify_document_reload.py` (manifest, removed-evidence ledger)
- `tests/offline/verify_rag.py` (panel attribution)
- `tests/offline/verify_ingestion.py`
- `scripts/e2e_local_backend.py` (`make e2e`, all panels; billed, run by a human)

### Known gaps

- The panel generators themselves have no unit tests; coverage is through
  the offline suites and `make e2e`.
- The reconnect replay covers one pending workspace (the most recent), runs
  in an untracked task, and sends assessments and metadata without going
  through `_emit_document_feature`.
- Each panel's payload fields were not re-verified one by one for this page;
  `app/socket/schemas.py` is the source of truth.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
