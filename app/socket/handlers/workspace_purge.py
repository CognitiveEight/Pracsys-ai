"""Compliance purge for deleted workspaces (`workspace_deleted`).

Why this exists
---------------
The backend's erasure cascade stops at its own database and file storage. The
copy of the client's documents that this service embedded into Qdrant is not
reachable by it, so before this event that copy survived indefinitely — which
made "your case is deleted" untrue of the AI's copy. This handler is the
instruction to erase it.

Three properties of the event shape the implementation, all of them from the
backend contract (see `WorkspaceDeletedPayload`):

1. ``purgeAfter`` is ABSOLUTE. It is obeyed as sent and never recomputed from
   ``purgeAfterDays`` or from a local notion of "7 days" — that is what keeps
   clock skew between the two services out of the argument.
2. DUPLICATES ARE EXPECTED and are not an error. The backend emit is
   fire-and-forget, so an event sent while this service is restarting simply
   evaporates; the backend compensates by re-sending every pending deletion on
   each reconnect under the same ``requestId`` until ``purgeAfter`` passes.
   Re-sends are how this service recovers a purge it lost to a restart, so they
   must re-arm the timer while remaining idempotent.
3. The deletion is REVOCABLE until it runs. A restore inside the grace window
   arrives as ordinary workspace traffic, which cancels the pending purge. See
   :meth:`WorkspacePurgeMixin.cancel_pending_purge` — without it, a workspace
   restored on day 2 would be silently erased on day 7.

What "delete everything under it" covers
----------------------------------------
- Qdrant: document chunks AND every workspace state-cache point, via a single
  workspace_id-scoped delete.
- The workspace context and any files staged on local disk.
- Every in-memory cache in the event handler keyed by this workspace.
- Future writes: a purged workspace is blocked at
  ``app.workspace.persistence`` so an upsert already in flight cannot land
  after the delete and quietly resurrect a cache point.

Failure is loud and retried. If the purge cannot complete, the workspace is
NOT marked purged and the attempt is retried with backoff, because the backend
stops re-sending once ``purgeAfter`` has passed — a silent failure here would
reopen the compliance gap with nothing left to trigger a second attempt.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger

from app.observability.audit import audit_logger
from app.socket.schemas import WorkspaceDeletedPayload
from app.workspace.manager import workspace_manager
from app.workspace.persistence import (
    allow_workspace_persistence,
    block_workspace_persistence,
)
from app.vectorstore.qdrant import vector_store

#: Attributes on the event handler that are not per-workspace caches and must
#: never be swept. Everything else that is a dict or set is fair game — that is
#: deliberate, so a per-workspace cache added later is purged without anyone
#: having to remember to update this module.
#:
#: The purge's own bookkeeping is exempt for a specific reason: it is keyed by
#: workspace_id like everything else, so an unguarded sweep deletes the pending
#: entry for the purge that is running. That entry is what the retry loop reads
#: to decide it is still wanted, so a failed first attempt would look like a
#: revoked purge and quietly stop retrying — leaving the data in place with
#: nothing left to trigger another attempt. `_pending_purges` is instead
#: cleared explicitly, only once the erase has actually succeeded.
_SWEEP_EXEMPT = frozenset(
    {
        "_handlers",
        "_socket_client",
        "_chat_workspace_index",
        "_pending_purges",
        "_purged_workspaces",
        "_seen_purge_requests",
    }
)

#: Backoff between purge attempts, in seconds. A purge that fails every one of
#: these is escalated as a compliance incident rather than dropped.
_PURGE_RETRY_DELAYS = (0, 60, 300, 900, 3600)


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """
    Parse an ISO 8601 UTC timestamp into an aware datetime.

    Tolerates the trailing ``Z`` the backend sends. Returns None on anything
    unparseable so the caller can decide — this function never guesses a
    fallback instant, because guessing would mean either deleting too early or
    never deleting at all.

    Args:
        value: The raw field value, expected to be an ISO 8601 string.

    Returns:
        A timezone-aware UTC datetime, or None if it could not be parsed.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class WorkspacePurgeMixin:
    """`workspace_deleted` handling: schedule, revoke and execute the purge."""

    # ------------------------------------------------------------------ event

    async def on_workspace_deleted(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_deleted (Server → AI_Maverick).

        Records the deletion and arms a purge for ``purgeAfter``. Returns as
        soon as the timer is armed — the erase itself runs in the background,
        so this never blocks the socket loop for the length of the grace window.

        Args:
            data: A ``WorkspaceDeletedPayload``-shaped dict.
        """
        try:
            payload = WorkspaceDeletedPayload(**(data or {}))
        except Exception as e:
            logger.error(
                "workspace_deleted payload rejected ({}); raw={}", str(e), repr(data)
            )
            return

        workspace_id = str(payload.workspaceId or "").strip()
        if not workspace_id:
            logger.error("workspace_deleted carried no workspaceId; ignoring")
            return

        request_id = str(payload.requestId or "").strip()
        purge_after = _parse_iso_utc(payload.purgeAfter)
        if purge_after is None:
            # Refuse rather than invent an instant: deleting on a guessed
            # schedule is worse than escalating a malformed event.
            logger.error(
                "workspace_deleted for {} has an unparseable purgeAfter {!r}; "
                "no purge scheduled — escalate to the backend team",
                workspace_id,
                payload.purgeAfter,
            )
            audit_logger.log_event(
                "workspace_purge_rejected",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "reason": "unparseable_purge_after",
                    "purge_after": payload.purgeAfter,
                },
            )
            return

        # Already erased. The backend stops re-sending once purgeAfter passes,
        # but a re-send racing that boundary is normal, not an error.
        if workspace_id in self._purged_workspaces:
            logger.info(
                "workspace_deleted for {} ignored — already purged at {} (request={})",
                workspace_id,
                self._purged_workspaces[workspace_id].get("purgedAt"),
                request_id or "<none>",
            )
            return

        pending = self._pending_purges.get(workspace_id)
        if pending:
            same_request = request_id and pending.get("requestId") == request_id
            task = pending.get("task")
            if same_request and task is not None and not task.done():
                # The expected duplicate: same deletion, timer already running.
                logger.debug(
                    "workspace_deleted duplicate for {} (request={}) — purge already "
                    "armed for {}",
                    workspace_id,
                    request_id,
                    pending.get("purgeAfter"),
                )
                return
            # Either a NEW deletion for this workspace (delete → restore →
            # delete carries a new requestId and a new deadline), or the same
            # one whose timer is gone. Re-arm against the payload just received.
            self._cancel_purge_task(workspace_id, reason="superseded")

        delay = max(0.0, (purge_after - datetime.now(timezone.utc)).total_seconds())
        task = asyncio.create_task(
            self._run_purge_when_due(
                workspace_id=workspace_id,
                delay_seconds=delay,
                request_id=request_id,
                user_id=str(payload.userId or ""),
                reason=str(payload.reason or ""),
            )
        )
        self._pending_purges[workspace_id] = {
            "requestId": request_id,
            "purgeAfter": purge_after.isoformat(),
            "deletedAt": payload.deletedAt,
            "userId": str(payload.userId or ""),
            "reason": str(payload.reason or ""),
            "armedAt": time.time(),
            "task": task,
        }
        if request_id:
            self._seen_purge_requests[request_id] = time.time()

        logger.warning(
            "🗑️ workspace_deleted: {} (owner={}, reason={}, request={}) — purge armed "
            "for {} (in {:.0f}s)",
            workspace_id,
            payload.userId,
            payload.reason or "unspecified",
            request_id or "<none>",
            purge_after.isoformat(),
            delay,
        )
        audit_logger.log_event(
            "workspace_purge_scheduled",
            {
                "workspace_id": workspace_id,
                "user_id": str(payload.userId or ""),
                "request_id": request_id,
                "reason": str(payload.reason or ""),
                "deleted_at": payload.deletedAt,
                "purge_after": purge_after.isoformat(),
                "delay_seconds": round(delay),
            },
        )

    # ------------------------------------------------------------- revocation

    def cancel_pending_purge(self, workspace_id: str, *, trigger: str) -> None:
        """
        Revoke a scheduled purge because the workspace is live again.

        Called from every handler that represents real activity on a workspace
        (created / sync / version restore / transferred). A workspace restored
        inside the grace window would otherwise keep its armed timer and be
        erased on schedule days later, with the user seeing a working case right
        up until it vanished.

        Also lifts the persistence block, so state written after the restore is
        saved again.

        Args:
            workspace_id: Workspace that just showed signs of life.
            trigger: Event name that caused the revocation, for the audit trail.
        """
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return

        was_pending = workspace_id in self._pending_purges
        was_purged = self._purged_workspaces.pop(workspace_id, None)

        if was_pending:
            self._cancel_purge_task(workspace_id, reason=trigger)
            logger.warning(
                "↩️ Pending purge for {} CANCELLED by {} — workspace is live again",
                workspace_id,
                trigger,
            )
            audit_logger.log_event(
                "workspace_purge_cancelled",
                {"workspace_id": workspace_id, "trigger": trigger},
            )

        if was_purged:
            # Rebuilding after the grace window expired: the data is genuinely
            # gone, the backend re-sends the documents, and we index from
            # scratch. Nothing to restore, only the block to lift.
            logger.info(
                "Workspace {} was purged at {}; {} is rebuilding it from scratch",
                workspace_id,
                was_purged.get("purgedAt"),
                trigger,
            )

        if was_pending or was_purged:
            allow_workspace_persistence(workspace_id)

    def _cancel_purge_task(self, workspace_id: str, *, reason: str) -> None:
        """Drop the pending entry for a workspace and cancel its timer task."""
        pending = self._pending_purges.pop(workspace_id, None)
        if not pending:
            return
        task = pending.get("task")
        if task is not None and not task.done():
            task.cancel()
        logger.debug(
            "Purge timer for {} cancelled ({})", workspace_id, reason
        )

    # ------------------------------------------------------------- scheduling

    async def _run_purge_when_due(
        self,
        *,
        workspace_id: str,
        delay_seconds: float,
        request_id: str,
        user_id: str,
        reason: str,
    ) -> None:
        """
        Sleep until ``purgeAfter``, then purge, retrying on failure.

        Cancellation is the normal revocation path (the workspace came back)
        and is not an error.

        Args:
            workspace_id: Workspace to erase.
            delay_seconds: Seconds until purgeAfter; 0 when already due.
            request_id: Backend de-duplication id, for the audit trail.
            user_id: Case owner.
            reason: "user_deleted" or "admin_deleted".
        """
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

            for attempt, backoff in enumerate(_PURGE_RETRY_DELAYS, start=1):
                if backoff:
                    await asyncio.sleep(backoff)
                # A restore can land while we are sleeping between retries.
                if workspace_id not in self._pending_purges:
                    logger.info(
                        "Purge for {} abandoned — no longer pending", workspace_id
                    )
                    return
                try:
                    await self._purge_workspace(
                        workspace_id=workspace_id,
                        request_id=request_id,
                        user_id=user_id,
                        reason=reason,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "Purge attempt {}/{} for {} failed: {}",
                        attempt,
                        len(_PURGE_RETRY_DELAYS),
                        workspace_id,
                        str(e),
                    )

            # Every attempt failed. The backend has stopped re-sending by now,
            # so nothing else will trigger this: it needs a human.
            logger.critical(
                "🚨 COMPLIANCE: purge of workspace {} FAILED after {} attempts. "
                "Client data is still held by the AI service after the erasure "
                "deadline. Escalate — this must be completed manually.",
                workspace_id,
                len(_PURGE_RETRY_DELAYS),
            )
            audit_logger.log_event(
                "workspace_purge_failed",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "request_id": request_id,
                    "attempts": len(_PURGE_RETRY_DELAYS),
                },
            )
        except asyncio.CancelledError:
            logger.debug("Purge timer for {} cancelled", workspace_id)
            raise

    # ---------------------------------------------------------------- erasure

    async def _purge_workspace(
        self,
        *,
        workspace_id: str,
        request_id: str = "",
        user_id: str = "",
        reason: str = "",
    ) -> None:
        """
        Erase every copy of a workspace held by this service.

        Order matters: writes are blocked FIRST so that a state upsert already
        scheduled by another task cannot land between the Qdrant delete and the
        end of this method and recreate what we just removed.

        Raises:
            Exception: Propagated to the retry loop if any stage fails. The
                workspace is only marked purged when every stage succeeded.
        """
        logger.warning("🗑️ Purging workspace {} — erasing all AI-held data", workspace_id)

        # 1. Stop new state from being written for this workspace.
        block_workspace_persistence(workspace_id)

        # 2. Drop in-memory caches and cancel per-workspace background work
        #    before the durable delete, so nothing in flight rewrites Qdrant.
        cleared = self._clear_workspace_memory(workspace_id)

        # 3. Qdrant + workspace context + local files. delete_workspace filters
        #    on workspace_id alone, so it takes document chunks and every
        #    state-cache kind in one pass.
        await workspace_manager.delete_workspace(workspace_id)

        # 4. Belt and braces: delete_workspace goes through the workspace
        #    manager, which also clears context and disk. Re-running the vector
        #    delete directly costs one filtered call and closes the case where
        #    the manager short-circuits on a workspace it never loaded.
        await vector_store.delete_workspace(workspace_id)

        self._pending_purges.pop(workspace_id, None)
        self._purged_workspaces[workspace_id] = {
            "purgedAt": datetime.now(timezone.utc).isoformat(),
            "requestId": request_id,
            "userId": user_id,
            "reason": reason,
        }

        logger.success(
            "✅ Workspace {} purged (owner={}, reason={}, request={}) — {} in-memory "
            "entr(ies) cleared",
            workspace_id,
            user_id or "<unknown>",
            reason or "unspecified",
            request_id or "<none>",
            cleared,
        )
        audit_logger.log_event(
            "workspace_purged",
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "request_id": request_id,
                "reason": reason,
                "cleared_entries": cleared,
            },
        )

    def _clear_workspace_memory(self, workspace_id: str) -> int:
        """
        Remove every in-memory trace of a workspace from the event handler.

        Sweeps ``self.__dict__`` rather than naming each cache. The handler owns
        several dozen per-workspace dicts and gains more with each feature; an
        explicit list would be correct on the day it was written and quietly
        incomplete a month later, which for an erasure guarantee is the worst
        kind of wrong. Anything dict- or set-shaped is swept, keyed by:

        - the workspace id itself (the common case), and
        - composite keys prefixed ``"<workspace_id>:"`` (the role caches use
          ``"<workspace>:<user>"`` and ``"<workspace>:<user>:<chat>"``).

        Chat-keyed caches hold extracted document text but are keyed by bare
        chat_id, so they are resolved through ``_chat_workspace_index``.

        Per-workspace state held in a *module-level* global is outside
        ``self.__dict__`` and therefore outside the sweep — those must be
        cleared by name (see the custom-instructions block below).

        Args:
            workspace_id: Workspace being erased.

        Returns:
            Number of entries removed, for the audit record.
        """
        removed = 0
        prefix = f"{workspace_id}:"

        # Chat ids belonging to this workspace, so chat-keyed caches (chat
        # attachments carry extracted document content) are reachable.
        chat_ids = {
            chat_id
            for chat_id, ws in getattr(self, "_chat_workspace_index", {}).items()
            if ws == workspace_id
        }
        doomed_keys = {workspace_id} | chat_ids

        for name, value in list(vars(self).items()):
            if name in _SWEEP_EXEMPT:
                continue

            if isinstance(value, dict):
                for key in [
                    k
                    for k in list(value.keys())
                    if k in doomed_keys
                    or (isinstance(k, str) and k.startswith(prefix))
                ]:
                    entry = value.pop(key, None)
                    # Per-workspace background work (feedback debounce flushes,
                    # generation pipelines) must not outlive the data it reads.
                    if isinstance(entry, asyncio.Task) and not entry.done():
                        entry.cancel()
                    removed += 1

            elif isinstance(value, set):
                for key in [
                    k
                    for k in list(value)
                    if k in doomed_keys
                    or (isinstance(k, str) and k.startswith(prefix))
                ]:
                    value.discard(key)
                    removed += 1

        for chat_id in chat_ids:
            self._chat_workspace_index.pop(chat_id, None)

        # Module-level per-workspace state. The sweep above only reaches
        # ``self.__dict__``, so anything held in a module global has to be
        # named here explicitly or it survives an erasure request for the life
        # of the process. Custom instructions hold a user-authored description.
        try:
            from app.workspace.custom_instructions import clear_custom_instructions

            clear_custom_instructions(workspace_id)
            removed += 1
        except Exception as ci_error:  # pragma: no cover - best effort
            logger.debug(
                "Clearing custom instructions failed (workspace={}): {}",
                workspace_id,
                ci_error,
            )

        # Scalars pointing at this workspace: leaving them set would make the
        # reconnect re-emit path try to republish a purged workspace's panels.
        if getattr(self, "_pending_reemit_workspace_id", None) == workspace_id:
            self._pending_reemit_workspace_id = None
            self._pending_reemit_user_id = None
            removed += 1
        if getattr(self, "_last_workspace_id_with_chips", None) == workspace_id:
            self._last_workspace_id_with_chips = None
            removed += 1
        pending_response = getattr(self, "_pending_ai_response", None)
        if isinstance(pending_response, dict) and workspace_id in {
            str(pending_response.get("workspaceId") or ""),
            str(pending_response.get("workspace_id") or ""),
        }:
            self._pending_ai_response = None
            removed += 1

        logger.debug(
            "Cleared {} in-memory entr(ies) for workspace {}", removed, workspace_id
        )
        return removed

    # ------------------------------------------------------------------ hooks

    def note_chat_workspace(self, chat_id: str, workspace_id: str) -> None:
        """
        Record which workspace a chat belongs to.

        Chat-scoped caches (attachment text, last assistant reply) are keyed by
        chat_id alone, so without this index a purge could not find them and
        extracted document content would outlive the workspace in memory.

        Args:
            chat_id: Chat identifier used as the cache key.
            workspace_id: Owning workspace.
        """
        if not chat_id or not workspace_id:
            return
        self._chat_workspace_index[str(chat_id)] = str(workspace_id)

    def is_workspace_purged(self, workspace_id: str) -> bool:
        """True when this workspace has been erased and not yet rebuilt."""
        return str(workspace_id or "") in self._purged_workspaces
