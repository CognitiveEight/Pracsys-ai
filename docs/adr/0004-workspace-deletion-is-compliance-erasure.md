# 0004: Workspace deletion is a compliance erasure

- **Date:** not recorded
- **Jira:** none recorded

## Context

The backend's erasure cascade does not reach this service. The service holds
its own copies of a case: per-workspace in-memory caches, module-level state
(participants, custom instructions), Qdrant collections (evidence, tables,
state caches), files on disk and, when enabled, knowledge-graph facts. The
backend sends `workspace_deleted` fire-and-forget, re-sending it on every
reconnect until the absolute `purgeAfter` time passes, and then stops.

Two failure modes shaped the decision. Persistence is fire-and-forget, so an
upsert already in flight could land after the delete and recreate a cache
point. And the handler gains new per-workspace caches with every feature, so
a hand-written list of caches to clear would go stale.

## Decision

- `workspace_deleted` is the instruction to erase every AI-held copy, not a
  cache eviction. It is handled by `app/socket/handlers/workspace_purge.py`.
- `purgeAfter` is absolute and is never recomputed from `purgeAfterDays`.
  Duplicate re-sends under the same `requestId` are expected.
- Any live workspace traffic revokes a pending purge (`cancel_pending_purge`).
- `_purge_workspace` blocks persistence for the workspace first
  (`block_workspace_persistence`), then clears in-memory state, then deletes
  from Qdrant and disk.
- `_clear_workspace_memory` sweeps `self.__dict__` for keys equal to the
  workspace id or prefixed `"<workspace_id>:"`, instead of naming each cache.
  Chat-scoped caches are reached through `_chat_workspace_index`. The purge's
  own bookkeeping is exempt from the sweep.
- Module-level state is not reached by the sweep and is cleared explicitly.
- Deletion from stores goes through `delete_documents` / `delete_workspace`,
  which also clear the tables collection and the knowledge graph.
- A failed purge retries with backoff and escalates as `logger.critical` plus
  a `workspace_purge_failed` audit event. A failed graph clear during the
  purge raises, so the purge retries.

## Consequences

- A new instance cache is erased without extra code, as long as it is keyed
  by the workspace id or a `"<workspace_id>:"` prefix.
- New module-level state, collections, disk files or graph nodes must be
  wired into the purge explicitly. Reviewers check this.
- A new chat entry point must call `note_chat_workspace(chat_id,
  workspace_id)`, or its chat-keyed caches survive the purge.
- The retry and escalation must not be weakened: once `purgeAfter` has
  passed, nothing else would trigger another attempt.
- An embedding migration leaves an old collection that a purge does not
  reach; it must be deleted as part of the migration.

## Status

Accepted (documented retroactively).

## Sources

- `CLAUDE.md`, "Deleting a workspace must erase every copy", the
  `workspace_deleted` entry under "Inbound socket events", "Workspace sharing"
  (participants cleared explicitly) and "Knowledge graph" (deletion).
- `.claude/rules/security.md`.
- `app/socket/handlers/workspace_purge.py`, `app/workspace/persistence.py`.
