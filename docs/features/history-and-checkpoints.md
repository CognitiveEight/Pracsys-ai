---
title: History and checkpoints
type: feature
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Evidence to analysis panels
other_side:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456510
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013505
related_specs: []
---

# History and checkpoints

## In plain words

Every change to a case's documents creates a checkpoint the lawyer can go back
to. When the lawyer restores an earlier checkpoint, the AI service makes its
own copy of the evidence match that checkpoint exactly. Documents that should
not be there are removed, and documents that should be there are read again.
It then rebuilds every analysis panel from that document set. When a document
is removed, by a restore or a normal delete, the AI remembers that it used to
be there. The analysis then lists it as missing evidence, rather than
quietly forgetting it. If the document comes back later, that note is cleared
on its own. The lawyer's approved and rejected items keep their status across
a restore.

## Who uses it

- Lawyers who roll a case back to an earlier version of its documents.
- Every panel prompt, which relies on an accurate list of the documents present.

## What the user sees

All panels show loaders, then refresh to match the restored version. A document
removed by the restore appears in Missing Information. The case title does not
change. See the lawyer app page in `other_side`.

## How it works

1. The backend sends `workspace_version_restore`. It has its own handler, not a
   branch of `workspace_sync`: in that chain, a restore whose files were
   already indexed would be skipped.
2. Any pending workspace deletion is revoked. The roster is applied only if
   the payload carries one. `currentState` is cached when present, so the
   rebuilt panels keep the lawyer's curation.
3. A duplicate check runs on `versionId`, `updatedAt` and the backend's
   deleted and restored id lists, in its own 180-second window. Restoring to a
   version that was live minutes ago is not mistaken for a replayed sync.
4. Caches are hydrated, the role is resolved and the workspace context is
   updated with the target file set (the non-deleted `evidenceFiles`).
5. **Reconcile.** The live document ids in the vector store are read and
   diffed against the target ids. `evidenceFiles` is authoritative. The
   backend's `deletedDocumentIds` and `restoredDocumentIds` are logged as
   advisory only.
6. `workspace:refresh` lights the panel loaders. `document_assessments` is
   included only when something will be re-ingested.
7. **Delete.** Extra documents are removed through `delete_documents` (vector
   store, tables, graph). Their entries are pruned from the document manifest
   and written to the removed-documents ledger.
8. **Ingest.** Missing documents are read again (metered). The new assessments
   are merged into the manifest, and each successful document is cleared from
   the ledger. `workspace_metadata` is sent with `updateTitle: false`, so a
   restore never renames the case.
9. **Regenerate.** Strategy and analysis run inline (with fallbacks), the
   analysis report is tagged against `currentState`, and the rest of the suite
   (dates, laws, timeline, parties, case laws, drafting) is scheduled. The
   web-derived panels are replaced, not merged, when documents were removed.

### The removed-documents ledger

The cached `document_assessments` payload is the case's document manifest.
Every feature prompt reads it as the ground truth for which documents are
present. So it must shrink when a document leaves and grow when one returns.

- `_prune_document_assessments` drops the entries and returns them.
- `_record_removed_documents` keeps a small snapshot of each (id, name, type,
  up to six key fields, time removed) in the `REMOVED_DOCUMENTS` ledger.
- The ledger feeds a REMOVED EVIDENCE prompt block and
  `_apply_removed_evidence_missing_info`, which guarantees one Missing
  Information entry per removed document type, even if the model ignores the
  prompt. The agent's own wording wins when it already raised the gap.
- `_forget_removed_documents` clears entries when a document id is ingested
  again. That is what makes a restore self-healing, even when it arrives as an
  ordinary `workspace_sync`.

## Rules and limits

- Never trust the delta; diff against the live store.
- Prune the manifest before any prompt is built.
- Any new ingestion or deletion path must call merge, prune and the ledger
  helpers, or deleted evidence silently disappears again.
- The deterministic Missing Information append respects the 8-item cap.
- The ledger is deduplicated by document id.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Live document list cannot be read | Nothing changes | Aborts before any loader is lit; no destructive guesswork |
| Vector store delete fails | Loaders may keep spinning | Aborts so panels are not built on a half-reconciled set |
| Re-ingestion fails | Document panel settles on its previous content | Re-emits the cached `document_assessments`; continues with what is present |
| Evidence file without an id | That file is not restored | Logs a warning and skips it |
| Same restore replayed within 180 s | Nothing new | Skipped as a duplicate |
| Missing Information already has 8 items | A removed document may not be listed | Logs a warning naming the type |

## For engineers

### Where the code is

- `app/socket/handlers/workspace_events.py` — `on_workspace_version_restore`.
- `app/socket/handlers/documents.py` — `_merge_document_assessments`,
  `_prune_document_assessments`, `_record_removed_documents`,
  `_forget_removed_documents`, `_apply_removed_evidence_missing_info`.
- `app/vectorstore/qdrant.py` — `get_all_documents`, `delete_documents`.
- `docs/BACKEND_VERSION_RESTORE_SPEC.md` — the backend-facing spec.

### Events

Inbound: `workspace_version_restore`. Outbound: `workspace:refresh`, then
`document_feature_response` for each panel. See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- `_last_version_restore_fingerprint` (in memory).
- Ledger: `_workspace_removed_documents`, persisted as
  `CacheKinds.REMOVED_DOCUMENTS`.
- Manifest: `_workspace_document_assessments_payload`.

### Security

- Deletion goes through `delete_documents`, which also clears spreadsheet rows
  and withdraws graph facts.
- The ledger holds masked extracted fields only, never `sensitiveFields`.

### Tests that guard it

- `tests/offline/verify_run_control.py` — a document dropped on resume is
  recorded in the ledger.
- `tests/offline/verify_document_reload.py` — ledger written only on a failed
  re-read and cleared on success.

### Known gaps

- No automated test drives `on_workspace_version_restore` itself.
- A failure after `workspace:refresh` (delete error, or an exception during
  regeneration) re-raises without `_release_refresh_loaders`, so loaders can
  stay lit.
- An evidence file without an id is left out of the target set, so its live
  copy is treated as extra and deleted. `workspace_resume_request` guards
  against this; the restore path does not.
- `workspace_metadata` is emitted but not declared in the refresh list.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
