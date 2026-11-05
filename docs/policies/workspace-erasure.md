---
title: Workspace erasure (AI service)
type: policy
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers from "The rules"
last_verified: 2026-10-21
jira: SIM-113
approved_by: to confirm
review_by: 2027-04-21
applies_to: the AI service in every environment, and every copy of case data it creates (memory, Qdrant, knowledge graph, local disk)
---

# Workspace erasure (AI service)

## In plain words

When a lawyer deletes a case, the main application tells the AI service, and
the AI service must erase its own copy of that case: the documents it read,
the analysis it produced, the list of people the case was shared with, and
everything it remembers about the case. The main application says exactly
when the erasure must happen, usually after a short grace period. If the
lawyer restores the case before then, any activity on the case cancels the
erasure. When the time comes, the service first stops anything new being
saved for the case, then erases every copy. If erasing fails, it tries again
several times over about eighty minutes. If it still fails, it raises an
urgent alert for a person to finish the job, because nothing would trigger
another attempt.

## What it covers / doesn't cover

What the AI service holds per case, and whether erasure reaches it:

| Copy | Where | Erased by the purge |
|---|---|---|
| Panel payloads, roles, chat attachment text, run state and other per-case caches | In-memory dicts and sets on the event handler | Yes, by a sweep of the handler's attributes |
| Custom instructions, sharing roster (participant names), chosen model, unreported cost | Module-level state in `app/workspace/` and `app/ai/usage.py` | Yes, cleared by name |
| Workspace context | `context_manager` | Yes |
| Cached state: role, panel payloads, document manifest, removed-evidence ledger, participants, cost totals and the rest | Qdrant `<QDRANT_COLLECTION_NAME>` | Yes |
| Evidence passages (dense and sparse vectors) | Qdrant `<name>_evidence_v1<embedding suffix>` | Yes |
| Spreadsheet rows | Qdrant `<name>_tables_v1` | Yes |
| Knowledge-graph facts | Neo4j, when configured | Yes; a failure fails the purge |
| Working files | `WORKSPACE_DATA_DIR/<workspace id>` on local disk | Yes |

Not covered by the purge, and handled by hand or by retention:

- Copies outside the running configuration: an old evidence collection left
  after an embedding change, Qdrant snapshots, backup volumes, an old cluster
  after a move. See [../runbooks/reindex-evidence.md](../runbooks/reindex-evidence.md)
  and `docs/migration-guide.md`.
- Logs. They hold ids, not case content, and follow their retention (audit
  log one year). See [logging-and-personal-data.md](logging-and-personal-data.md).
- Temporary audio uploaded to the speech service bucket. It is deleted right
  after transcription, best effort; it is not part of the purge.
- Copies held by the backend, the lawyer app or model providers.

For the user-facing feature, see
[../features/workspace-deletion.md](../features/workspace-deletion.md).

## The rules

1. **`workspace_deleted` is an instruction to erase**, not a cache eviction.
   It erases every copy listed as "Yes" above.
2. **`purgeAfter` is absolute.** The purge runs at or after that instant. It is
   never recomputed from `purgeAfterDays` or from a local clock offset. A
   `purgeAfter` that cannot be parsed schedules nothing, is logged as an error
   and audited as `workspace_purge_rejected`, and is escalated to the backend
   team. The service never guesses an instant.
3. **Duplicates are expected.** The backend re-sends a pending deletion on
   every reconnect with the same `requestId` until `purgeAfter` passes. A
   re-send for an armed purge is ignored; a re-send whose timer is gone (for
   example after a restart) re-arms it; a new `requestId` replaces the earlier
   schedule; a re-send for an already purged case is ignored.
4. **Live traffic revokes a pending purge.** Each of these events cancels it:
   `workspace_created`, `workspace_sync`, `workspace_version_restore`,
   `workspace_transferred`, `document_reload_request`,
   `workspace_resume_request`, `missing_information_edited`. The cancellation
   is audited as `workspace_purge_cancelled` and saving is allowed again. Any
   new handler that represents real activity on a case must call
   `cancel_pending_purge`.
5. **Block writes first.** The purge blocks state persistence for the case
   before deleting, so a save already in flight cannot recreate a cache point
   after the delete.
6. **Then erase in this order:** in-memory state (cancelling per-case
   background tasks), then Qdrant (all three collections) with the graph,
   the workspace context and local files, then a second direct Qdrant delete.
7. **The memory sweep is generic.** It removes every handler attribute entry
   keyed by the workspace id, by a `"<workspace id>:"` prefix, or by a chat id
   linked to the case through `note_chat_workspace`. The purge's own
   bookkeeping is exempt. Module-level state is not reached by the sweep and
   must be cleared by name in `_clear_workspace_memory`.
8. **Every deletion path goes through `QdrantStore.delete_workspace` or
   `delete_documents`.** That is what reaches the evidence and tables
   collections and the graph.
9. **Failure is loud and retried.** Up to 5 attempts, waiting 0 s, 1 min, 5 min,
   15 min and 1 h before each one (about 80 minutes in all). A case is marked purged only when
   every stage succeeded. After the last failure the service logs
   `COMPLIANCE: purge of workspace … FAILED` at CRITICAL and audits
   `workspace_purge_failed`. A person must then complete the erasure.
10. **Every outcome is audited** in `logs/audit/audit.log`:
    `workspace_purge_scheduled`, `workspace_purge_cancelled`,
    `workspace_purged`, `workspace_purge_failed`, `workspace_purge_rejected`.
11. **The purge may not be weakened.** No change may remove the block, the
    retries, the escalation or any collection from the delete. Such a change
    is labelled `needs-human-review`.

## How we enforce it

| What | Code | Tests that guard it |
|---|---|---|
| Event handling, schedule, duplicates, revocation, retries, escalation, memory sweep | `app/socket/handlers/workspace_purge.py` | `make e2e` step 6 (`scripts/e2e_local_backend.py`: no Qdrant point left for the case, no disk copy). Human-run, billed. |
| Payload shape | `WorkspaceDeletedPayload` in `app/socket/schemas.py` | |
| Write block | `block_workspace_persistence` / `allow_workspace_persistence` in `app/workspace/persistence.py` | |
| Qdrant and graph delete | `QdrantStore.delete_workspace` in `app/vectorstore/qdrant.py`; graph hook from `app/core/knowledge_plane.py` | `tests/test_tables.py` (tables collection cleared), `tests/offline/verify_rag.py` (knowledge-graph group) |
| Context and disk | `WorkspaceManager.delete_workspace` in `app/workspace/manager.py` | `make e2e` step 6 |
| Participant names | `clear_participants` in `app/workspace/participants.py` | `tests/offline/verify_workspace_share.py` (roster forgotten on purge) |
| Review | `reviewer` and `security-auditor` agents; `.claude/rules/security.md` | |

## Exceptions and approvals

- There are no exceptions to erasing a case at `purgeAfter`.
- A legal hold or a request to keep a deleted case must be handled by the
  backend before it sends `workspace_deleted`. The AI service has no hold
  mechanism.
- Manual completion after a `workspace_purge_failed` alert is done by the AI
  team on call with the tech lead, and recorded.

Known gaps and risks (not verified, or not covered):

- The pending-purge timers, the purged-case list and the write block are held
  in memory only. After a restart, a pending purge exists again only if the
  backend re-sends it. If the service is down from before `purgeAfter` until
  after it, and the backend has stopped re-sending, the purge may never run.
  The backend's exact re-send cut-off was not verified from this repo.
- No offline or unit test drives `on_workspace_deleted`, revocation, the
  retry loop or the memory sweep. Only `make e2e` runs a purge, and it runs
  without the knowledge graph.
- Clearing custom instructions, participants, model choice and the cost
  ledger in memory is best effort: a failure there is logged at DEBUG and does
  not fail the purge. The Qdrant copies of these are still deleted.
- `docs/migration-guide.md` lists "purge bookkeeping" as stored in Qdrant; the
  code keeps it in memory.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
