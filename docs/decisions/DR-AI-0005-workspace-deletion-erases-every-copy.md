---
title: Deleting a workspace erases every copy the AI service holds
type: decision
repo: law-firm-simplertoday-ai
owner: AI team
status: accepted
audience: everyone; engineers below "What this means"
last_verified: 2026-10-21
jira: SIM-113
number: DR-AI-0005
date: 2026-08-31
decided_by: AI team lead (role)
---

# DR-AI-0005 Deleting a workspace erases every copy the AI service holds

## In plain words

When a case is deleted, the backend's own clean-up does not reach the AI
service. The AI service keeps its own copies: searchable document text, panel
results, chat context, and notes in memory. We decided that the deletion
message from the backend is an instruction to erase all of it, at the deadline
the backend sets. Before erasing, the service blocks any new saving for that
case, so a save already on its way cannot bring data back. It then clears
everything in memory, including stores added in future, and deletes the stored
copies. If the case is restored before the deadline, the erasure is cancelled.
If erasing fails, the service retries five times over about eighty minutes, then
raises a critical alert and an audit record so a person finishes it. It never
fails quietly.

## Why we needed to decide

- Deleting a case is a compliance erasure, not a cache eviction. Client data
  must not outlive the deadline.
- Saving state is fire-and-forget, so a save scheduled before the delete could
  land after it and recreate the data.
- The handler owns dozens of per-workspace caches and gains more with every
  feature. A hand-written list of caches to clear would go stale.
- The backend re-sends the deletion on every reconnect until the deadline, then
  stops. After that, nothing would trigger a second attempt.

## Options we considered

| Option | Pros | Cons |
|---|---|---|
| Delete the Qdrant data only | Simple | Memory caches and later saves keep or restore data |
| Clear a named list of caches, then delete | Explicit | Silently incomplete once a new cache is added |
| Block persistence first, sweep every workspace-keyed entry, delete storage, retry with backoff and escalate | Nothing in flight can resurrect data; new caches are covered automatically; failure is visible | The sweep must exempt the purge's own bookkeeping; module-level stores need explicit clears |

## What we decided and why

The third option, in `app/socket/handlers/workspace_purge.py`:

1. `on_workspace_deleted` arms a purge for the backend's `purgeAfter`, which is
   absolute (never recomputed from `purgeAfterDays`). Re-sends under the same
   `requestId` are expected and idempotent.
2. Live traffic for the workspace (created, sync, version restore,
   transferred, document reload, resume, missing-information edit) calls
   `cancel_pending_purge`, revoking it. Stop does not revoke it.
3. `_purge_workspace` calls `block_workspace_persistence` first
   (`app/workspace/persistence.py`), then `_clear_workspace_memory`, which
   sweeps `self.__dict__` for the workspace id and `"<workspace_id>:"`
   composite keys and reaches chat-keyed caches through
   `_chat_workspace_index`. The purge's own bookkeeping (`_pending_purges`,
   `_purged_workspaces`, `_seen_purge_requests`) is exempt. Module-level
   stores (custom instructions, participants, model selection) are cleared
   explicitly.
4. It then deletes through `workspace_manager.delete_workspace` and
   `vector_store.delete_workspace`, which cover the evidence, tables and state
   collections and withdraw knowledge-graph facts. A failed graph clear raises
   so the purge retries.
5. Attempts run at 0, 60, 300, 900 and 3600 seconds. If all fail:
   `logger.critical` plus a `workspace_purge_failed` audit event. Success
   writes a `workspace_purged` audit event.

## What this means

- Any new deletion path must go through `delete_documents` /
  `delete_workspace`.
- Any new module-level per-workspace store must be cleared explicitly in the
  purge; the sweep only reaches the handler's own attributes.
- Any new chat entry point must call `note_chat_workspace(chat_id,
  workspace_id)`.
- Never weaken the persistence block, the sweep, or the retry and escalation.
- An old Qdrant collection left behind after an embedding migration is not
  reached by a purge; delete it as part of the migration.
- Tested end to end by `make e2e` (step "Compliance purge" in
  `scripts/e2e_local_backend.py`). Vector-store and graph deletion is checked
  in `tests/offline/verify_rag.py` and `tests/test_tables.py`. There is no
  dedicated offline test of the purge handler itself.
- Policy: [Workspace erasure](../policies/workspace-erasure.md); feature:
  [Workspace deletion](../features/workspace-deletion.md).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-08-31 | Workspace delete event and compliance purge | 274d745 |
| 2026-10-21 | First version, written from the code | SIM-113 |
