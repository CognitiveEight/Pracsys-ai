---
title: Delete a workspace (compliance erasure)
type: flow
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, admin, backend, ai-service]
other_sides: {}
related_features: [../features/workspace-deletion.md]
related_specs: []
---

# Delete a workspace (compliance erasure)

## In plain words

When a case is deleted, the backend removes its own records and files, but it
cannot reach the copy of the documents the AI service has stored for search.
So the backend sends the AI service a separate instruction to erase its copy,
with an exact date and time after which to do it. Until then the deletion can
be undone: if the case shows any sign of life again, such as being restored or
updated, the AI service cancels the erasure. At the set time, it first stops
anything from being saved for that case, then clears everything it holds in
memory, then deletes the stored documents, saved panels, files on disk and any
knowledge-graph facts. If that fails, it tries again.
If every attempt fails, it raises a critical alert for a person to finish the
job by hand, because nothing else will ask again.

## The whole flow at a glance

There is no other-side page for this flow yet. The backend's own erasure
cascade does not reach the AI service; the `workspace_deleted` event is the only
link.

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app or admin | Owner or an admin deletes the case | Not yet documented on the other sides |
| 2 | Backend | Deletes its own data; records a pending AI erasure and sends `workspace_deleted` with an absolute `purgeAfter` | Inferred from the socket contract |
| 3 | Backend | Re-sends the same `workspace_deleted` (same `requestId`) on every reconnect until `purgeAfter` passes | Inferred from the socket contract |
| 4 | AI service | Arms a timer for `purgeAfter` | This page, step 1 |
| 5 | Backend, lawyer app | If the case is restored or used inside the window, ordinary workspace events arrive | Other sides |
| 6 | AI service | Any such event cancels the timer | This page, step 2 |
| 7 | AI service | At `purgeAfter`, erases every copy; retries and escalates on failure | This page, steps 3-5 |

## The AI service's steps in detail

1. **Arm the purge.** `on_workspace_deleted` validates `WorkspaceDeletedPayload`.
   It refuses, and audits `workspace_purge_rejected`, when `purgeAfter` cannot be
   parsed; it never guesses a time. `purgeAfter` is obeyed as sent and never
   recomputed from `purgeAfterDays`. A workspace already purged is ignored. A
   duplicate with the same `requestId` while the timer runs is ignored as
   expected. A different `requestId` (delete, restore, delete again) replaces the
   old timer. The timer task is stored in `_pending_purges`, and
   `workspace_purge_scheduled` is audited. The handler returns at once.
2. **Revoke on activity.** `cancel_pending_purge` is called by
   `workspace_created`, `workspace_sync`, `workspace_version_restore`,
   `workspace_transferred`, `document_reload_request`, `workspace_resume_request`
   and `missing_information_edited`. It cancels the timer, audits
   `workspace_purge_cancelled`, and lifts the persistence block. A workspace that
   was already purged is simply rebuilt from scratch by the incoming event.
3. **Erase, in this order** (`_purge_workspace`):
   1. `block_workspace_persistence`, so a state write already in flight cannot
      land after the delete and recreate a cache point;
   2. `_clear_workspace_memory` sweeps every dict and set on the handler for keys
      equal to the workspace id or starting `"<workspace_id>:"`, plus chat-keyed
      caches found through `_chat_workspace_index`; cancels any per-workspace
      task stored as a value; then clears module-level state by name: custom
      instructions, the sharing roster, the model choice and the unreported cost
      ledger; and resets reconnect pointers and a held chat reply for this case;
   3. `workspace_manager.delete_workspace`: vector store, workspace context and
      the local workspace folder;
   4. `vector_store.delete_workspace` again, directly. It deletes from the state
      collection, the evidence collection and the tables collection, and
      withdraws the workspace's knowledge-graph facts. A failed graph clear raises.
4. **Record success.** The pending entry is removed, the workspace is marked
   purged, and `workspace_purged` is audited with the number of entries cleared.
5. **Retry and escalate.** Any failure in step 3 is retried after 0, 60, 300, 900
   and 3600 seconds, checking before each attempt that the purge is still
   pending. If all five fail, the service logs `COMPLIANCE: purge ... FAILED` at
   CRITICAL and audits `workspace_purge_failed`. This must reach a human: the
   backend has stopped re-sending by then.

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 1 | `purgeAfter` missing or malformed | Nothing | No purge armed; error logged and audited for the backend team |
| 1 | AI service restarts during the grace window | Nothing | Timer is lost; the backend's re-send on reconnect re-arms it |
| 2 | Case restored inside the window | Case works normally | Timer cancelled, persistence unblocked |
| 2 | Case recreated after it was purged | Panels rebuilt from the re-sent documents | Purged mark cleared; normal ingestion |
| 3 | Qdrant or graph delete fails | Nothing | Retried with backoff |
| 5 | Every attempt fails | Nothing | CRITICAL log and `workspace_purge_failed` audit; manual erasure needed |

## For engineers

**Entry and exit.** Entry: `on_workspace_deleted` in
`app/socket/handlers/workspace_purge.py`. Exit: no socket reply; audit events
only. `workspace_deleted` runs outside any run scope, so a stop never fences or
cancels it.

**Events and payloads.** `WorkspaceDeletedPayload` (`workspaceId`, `userId` (owner),
`deletedAt`, `purgeAfter`, `purgeAfterDays`, `reason`, `requestId`, `event`) in
`app/socket/schemas.py`. Contract:
[Backend and AI service socket events](../contracts/backend-ai-socket-events.md).

**State and timing rules.**
- `_pending_purges`, `_purged_workspaces` and `_seen_purge_requests` are exempt
  from the sweep; deleting the pending entry mid-purge would make the retry loop
  think the purge was revoked.
- Persistence block: `app/workspace/persistence.py`.
- Deletion of chunks, tables and graph: `QdrantStore.delete_workspace` in
  `app/vectorstore/qdrant.py`, graph hook via `app/core/knowledge_plane.py`.
- Any new module-level per-workspace state must be cleared by name in
  `_clear_workspace_memory`.

**Security checks.** This is a compliance erasure, not a cache eviction. Never
weaken the order, the retries or the escalation (`.claude/rules/security.md`).

**Tests.** `make e2e` (`scripts/e2e_local_backend.py`) sends a due
`workspace_deleted` and checks that no Qdrant points and no on-disk copy remain.
`tests/offline/verify_workspace_share.py` checks the roster is forgotten.
There is no offline suite for the timer, revocation or retry logic.

**Known gaps.**
- Pending timers and the purged mark are in memory only. If the service is down
  across `purgeAfter` and the backend stops re-sending at that moment, nothing
  triggers the purge; the backend's exact cut-off was not verified.
- The purge drops the workspace's run-task registry entry but does not appear to
  cancel the tasks in it, so panel work already running for the case may still
  try to emit after the erase (its state writes are blocked).
- The retry window is about 1.5 hours in total; a longer outage of Qdrant or the
  graph ends in escalation.
- Embedding collections from an older embedding model are not reached by a purge
  (CLAUDE.md, "Model calls are provider-neutral").

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
