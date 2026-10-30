---
title: Workspace deletion (compliance erasure)
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows: []
other_side: {}
related_specs: []
---

# Workspace deletion (compliance erasure)

## In plain words

When a lawyer deletes a case, the backend removes its own records and files.
It cannot reach the copy the AI service keeps: the documents' text, the stored
passages and the panel results. So the backend tells the AI service to erase
that copy too. There is a grace period. Until the deadline the backend sets,
the case can be restored, and any sign that the case is in use again cancels
the erasure. When the deadline passes, the AI service erases everything it
holds for that case: stored text and passages, saved panel results, the list of
people it was shared with, and its working memory. If the erasure fails, it
tries again several times. If it still fails, it raises a critical alert so a
person finishes the job, because nothing else will ask again.

## Who uses it

- Lawyers and administrators who delete a case.
- Compliance: this is what makes "your case is deleted" true of the AI's copy.

## What the user sees

Nothing in the AI service. The case disappears in the app when the backend
deletes it. If the case is restored inside the grace period, it works as before.

## How it works

1. **The backend sends `workspace_deleted`** with `workspaceId`, `requestId`,
   `purgeAfter`, `deletedAt`, `userId` and `reason`.
2. **`purgeAfter` is absolute.** It is parsed as an ISO UTC timestamp and
   obeyed as sent. It is never recomputed from `purgeAfterDays` or a local
   rule. An unparseable value is refused and audited
   (`workspace_purge_rejected`); no purge is guessed.
3. **Duplicates are expected.** The backend re-sends every pending deletion on
   each reconnect, under the same `requestId`, until `purgeAfter` passes.
   - Already purged: ignored.
   - Same `requestId` with its timer running: ignored.
   - A new `requestId`, or the same one with no live timer (for example after
     a restart): the timer is re-armed from this payload.
4. **A timer is armed** for the time left until `purgeAfter` (zero if already
   due), and the deletion is audited (`workspace_purge_scheduled`). The
   handler returns at once.
5. **Live traffic revokes it.** `cancel_pending_purge` is called by
   `workspace_created`, `workspace_sync`, `workspace_version_restore`,
   `workspace_transferred`, `document_reload_request`,
   `workspace_resume_request` and `missing_information_edited`. It cancels the
   timer, lifts the persistence block, and audits
   `workspace_purge_cancelled`. Chat, heartbeats and stop requests do not
   revoke it.
6. **The erasure, in order:**
   1. Block persistence for the case (`block_workspace_persistence`). State
      writes are fire-and-forget, so one already in flight could otherwise
      land after the delete and recreate a cache point.
   2. Clear memory (`_clear_workspace_memory`): sweep every dict and set on the
      event handler for keys equal to the workspace id or prefixed
      `"<workspace_id>:"`, plus chat-keyed entries found through
      `_chat_workspace_index`. Running per-case tasks found there are
      cancelled. Module-level state is cleared by name: custom instructions,
      participants, model selection and the unreported usage ledger.
   3. `workspace_manager.delete_workspace`: cancel processing, delete from the
      vector store, delete the workspace context, delete local files.
   4. `vector_store.delete_workspace` again, directly. It deletes by workspace
      id from the state-cache collection, the evidence collection and the
      tables collection, then withdraws the case from the knowledge graph. A
      graph failure here raises.
   5. Mark the case purged and audit `workspace_purged`.
7. **Retry and escalate.** A failed attempt is retried after 1, 5, 15 and 60
   minutes (five attempts in all). Before each retry it checks the purge is
   still wanted. If every attempt fails, it logs `logger.critical` and audits
   `workspace_purge_failed`.

## Rules and limits

- Never weaken the purge, its ordering, or its retry and escalation.
- The purge's own bookkeeping (`_pending_purges`, `_purged_workspaces`,
  `_seen_purge_requests`) is exempt from the sweep. Deleting the pending entry
  mid-purge would make the retry loop think the purge was revoked.
- Any new per-case cache on the event handler is swept automatically. Any new
  module-level per-case state must be cleared by name in
  `_clear_workspace_memory`.
- Any new chat entry point must call `note_chat_workspace`, or its chat-keyed
  caches survive the purge.
- Any new deletion path must go through `delete_documents` or
  `delete_workspace`, so every collection and the graph are cleared.
- A case rebuilt after its purge is indexed from scratch; nothing is restored.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| `purgeAfter` unparseable | Nothing | Refuses to schedule; logs an error and audits a rejection for the backend team |
| Service restarts during the grace period | Nothing | Timer is lost; the backend's next re-send re-arms it |
| Case restored before the deadline | Case works normally | Timer cancelled; persistence unblocked |
| Qdrant or graph unavailable at the deadline | Nothing | Retries with backoff; the case is not marked purged |
| All five attempts fail | Nothing | Critical log and `workspace_purge_failed` audit; a person must finish it |
| Re-send arrives after the purge | Nothing | Ignored as already purged |

## For engineers

### Where the code is

- `app/socket/handlers/workspace_purge.py` — `on_workspace_deleted`,
  `cancel_pending_purge`, `_run_purge_when_due`, `_purge_workspace`,
  `_clear_workspace_memory`, `note_chat_workspace`.
- `app/workspace/persistence.py` — `block_workspace_persistence`,
  `allow_workspace_persistence`.
- `app/workspace/manager.py` — `delete_workspace`.
- `app/vectorstore/qdrant.py` — `delete_workspace`.
- `app/core/knowledge_plane.py` — graph withdrawal hook (raises on a
  whole-case failure).
- Module-level clears: `app/workspace/participants.py`,
  `app/workspace/custom_instructions.py`, `app/workspace/model_selection.py`,
  `app/ai/usage.py`.

### Events

Inbound: `workspace_deleted`. No reply. Revoked by the events listed in step 5.
See [the socket contract](../contracts/backend-ai-socket-events.md).
Why it works this way: [DR-AI-0005](../decisions/DR-AI-0005-workspace-deletion-erases-every-copy.md);
the rules: [Workspace erasure policy](../policies/workspace-erasure.md).

### Data

What is erased: the three Qdrant collections for the case (state caches,
evidence chunks, spreadsheet rows), graph facts, the workspace context, files
under `settings.workspace_data_dir/<workspace_id>`, and in-memory state.

### Security

- Audit events: `workspace_purge_scheduled`, `workspace_purge_cancelled`,
  `workspace_purge_rejected`, `workspace_purged`, `workspace_purge_failed`.
- Retry delays: `_PURGE_RETRY_DELAYS = (0, 60, 300, 900, 3600)` seconds.

### Tests that guard it

- `tests/offline/verify_rag.py` — a purge fails loudly if the graph cannot be
  cleared, and a successful purge clears the graph.
- `tests/offline/verify_workspace_share.py` — the purge forgets the roster.
- `tests/test_usage_ledger.py`, `tests/test_model_selection.py` — the purge
  clears unreported spend and the model choice.
- `tests/offline/verify_run_control.py`, `tests/offline/verify_document_reload.py`,
  `tests/offline/verify_missing_info_edit.py` — live traffic revokes a pending
  purge.

### Known gaps

- No dedicated test drives `on_workspace_deleted` end to end (scheduling,
  duplicates, retry and escalation, ordering).
- Pending timers and the purged set are in memory only. The backend re-sends
  only until `purgeAfter` passes. If a restart loses the timer and the service
  does not reconnect before the deadline, no purge runs and nothing alerts.
  Whether the backend re-sends at all after the deadline was not verified.
- Generated draft files and template skeletons in cloud storage are not
  erased.
- The purge timer task is created with a bare `asyncio.create_task`; the
  handle is kept in `_pending_purges`.
- `_purged_workspaces` grows for the life of the process.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
