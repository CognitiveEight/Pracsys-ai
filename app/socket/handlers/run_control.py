"""Stop / Finish-update for a running workspace update.

`workspace_stop_request` and `workspace_resume_request`.

Why this exists
---------------
A workspace update runs ten feature panels and can take minutes. The lawyer
can now click **Stop** (the backend sends ``workspace_stop_request`` and stops
waiting) and later **Finish update** (``workspace_resume_request`` naming only
the panels that never arrived).

The backend already guarantees correctness on its side: it stamps the run
stopped and discards every feature response that arrives afterwards. What this
module adds is that the AI stops paying for — and stops trying to deliver —
work nobody will see, and that nothing from the stopped run can come back
later.

The run fence
-------------
"Cancel in-flight work" is best-effort: a task can be mid-await in a Gemini
call, or be a chat turn that must not be killed. So cancellation is backed by
a fence that does not depend on it:

- Every workspace-scoped inbound event runs inside a *run scope*: a
  ``contextvars`` value ``(workspace_id, epoch)`` captured when the event
  arrived. ``asyncio.create_task`` copies the context, so every task a handler
  spawns carries the same scope — the same mechanism as ``app/ai/usage.py``.
- A stop bumps the workspace's epoch. From then on, any emit of
  ``document_feature_response`` or ``workspace:refresh`` made from an older
  scope is suppressed at the socket (``_StopFencedSocket``), and nothing from
  an older scope may enter the pending-delivery buffer or arm a resume marker.
- A new run (upload, resume, a chat turn, ...) arrives after the stop, so it
  captures the new epoch and is untouched. Inbound events are processed in
  socket order, so this is causal and needs no identity from the backend.

That is also why a stale ``workspace:refresh`` from the stopped run can never
re-arm the frontend's spinners: it is never sent.

What stop does NOT do
---------------------
- It does not cancel chat turns. A lawyer who asked a question gets the
  answer; a chat-driven objective change inside that turn is fenced (its
  panels are dropped) and its instruction is *held* for a later resume instead
  of being lost.
- It does not reply. The event is fire-and-forget by contract.
- It does not revoke a pending compliance purge (it is not live traffic).
"""

import asyncio
import contextvars
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from loguru import logger

from app.ai.usage import capture_usage
from app.observability.audit import audit_logger
from app.processing.document_processor import document_processor
from app.socket.handlers.workspace_purge import _parse_iso_utc
from app.socket.schemas import (
    WorkspaceResumeRequestPayload,
    WorkspaceStopRequestPayload,
)
from app.workspace.participants import set_participants
from app.workspace.persistence import CacheKinds, restore_state

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

#: ``(workspace_id, epoch)`` of the inbound event this code is running for.
#: None outside any workspace event (connect/reconnect work, heartbeats).
_RUN_SCOPE: contextvars.ContextVar[Optional[Tuple[str, int]]] = (
    contextvars.ContextVar("simplertoday_run_scope", default=None)
)

#: While a resume runs, the set of featureTypes it has emitted. Lets the
#: handler release exactly the loaders it declared and never satisfied, without
#: every generator having to report back.
_EMIT_OBSERVER: contextvars.ContextVar[Optional[Set[str]]] = (
    contextvars.ContextVar("simplertoday_emit_observer", default=None)
)

# ---------------------------------------------------------------------------
# Event classes
# ---------------------------------------------------------------------------

#: Events that ARE a running update. Their handler task is registered so a stop
#: can cancel it. Chat is deliberately absent: cancelling a turn would leave
#: the lawyer's question unanswered.
CANCELLABLE_RUN_EVENTS = frozenset(
    {
        "workspace_created",
        "workspace_sync",
        "workspace_version_restore",
        "document_reload_request",
        "workspace_resume_request",
    }
)

#: Events that start a fresh full run. An objective change held by an earlier
#: stop is dropped by these: the new run regenerates against the objective in
#: force, and replaying the held one on some later resume would be a surprise.
SUPERSEDING_RUN_EVENTS = frozenset(
    {
        "workspace_created",
        "workspace_sync",
        "workspace_version_restore",
        "document_reload_request",
    }
)

#: Events that start new work. Only these move ``_last_workspace_event_at``,
#: the clock a stale stop is judged against. Polling traffic (probing prompts,
#: suggestion chips) is not "newer work", and counting it would make a stop
#: re-sent after a reconnect look stale while the run it targets still runs.
_WORK_STARTING_EVENTS = CANCELLABLE_RUN_EVENTS | frozenset(
    {
        "user_message_request",
        "workspace_transferred",
        "analysis_report_feedback",
        "added_missing_information",
    }
)

#: Events that never get a run scope. The stop must not fence itself, and a
#: compliance purge / heartbeat emits no panels.
_UNSCOPED_EVENTS = frozenset(
    {"workspace_stop_request", "workspace_deleted", "ai_heartbeat_ping"}
)

#: The only emits the fence suppresses: the panel payload and the loader hint.
#: ``ai_response`` is never fenced — a chat answer always reaches the lawyer.
_FENCED_EMIT_EVENTS = frozenset({"document_feature_response", "workspace:refresh"})

#: Every panel a resume can produce, in product order. Resumed features run in
#: THIS order regardless of the order the backend listed them in, because each
#: panel reads the caches of the ones before it.
RESUMABLE_FEATURES = (
    "document_assessments",
    "workspace_metadata",
    "strategy_objective",
    "analysis_report",
    "list_of_dates",
    "applicable_laws",
    "procedural_timeline",
    "relevant_parties",
    "case_laws",
    "drafting_assistant",
)

# ---------------------------------------------------------------------------
# Timings
# ---------------------------------------------------------------------------

#: How long a stop waits for cancelled tasks to unwind before clearing the
#: buffers. Their CancelledError handlers write markers, and clearing before
#: they finish would let those writes land afterwards. Past this the fence
#: still holds; the wait only orders the bookkeeping.
_STOP_SETTLE_SECONDS = 10.0

#: How long a stop requestId is remembered. The backend re-sends on reconnect.
_STOP_DEDUPE_SECONDS = 24 * 3600.0

#: A stop whose ``stoppedAt`` is older than the latest inbound event for the
#: workspace by more than this is a stale re-send (the lawyer has since started
#: new work). Applying it would fence the NEW run and strand its spinners. The
#: tolerance absorbs clock skew between the two services.
_STALE_STOP_TOLERANCE_SECONDS = 30.0

#: Resume de-dupe, mirroring document_reload: a re-send while in flight or just
#: completed is swallowed; a re-send after a failure re-runs.
_RESUME_DEDUPE_SECONDS = 180.0
_RESUME_INFLIGHT_TIMEOUT_SECONDS = 1800.0

#: An objective change held by a stop is replayed only by a resume inside this
#: window. The backend's own example resumes an hour later; a day is generous
#: and still stops a forgotten instruction resurfacing much later.
_HELD_OBJECTIVE_MAX_AGE_SECONDS = 24 * 3600.0


def current_run_scope() -> Optional[Tuple[str, int]]:
    """The ``(workspace_id, epoch)`` the calling code runs under, if any."""
    return _RUN_SCOPE.get()


class _StopFencedSocket:
    """The socket client, with every panel emit passed through the run fence.

    Installed by the ``_socket_client`` property, so the ~20 call sites that
    emit directly (not via ``_emit_document_feature``) are covered without
    touching them. Returns ``False`` for a suppressed emit — it was NOT
    delivered, and callers that report on delivery (the objective-change chat
    acknowledgement) must not claim otherwise.
    """

    __slots__ = ("_inner", "_owner")

    def __init__(self, inner: Any, owner: Any) -> None:
        self._inner = inner
        self._owner = owner

    @property
    def wrapped(self) -> Any:
        """The underlying client."""
        return self._inner

    async def emit(self, event: str, data: Dict[str, Any]) -> bool:
        try:
            if self._owner._should_suppress_emit(event, data):
                return False
        except Exception as guard_err:  # pragma: no cover - never block an emit
            logger.warning("Run-fence check failed (emitting anyway): {}", guard_err)
        return await self._inner.emit(event, data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RunControlMixin:
    """Run scopes, the stop fence, and the stop / resume handlers."""

    # ------------------------------------------------------------ socket slot

    @property
    def _socket_client(self) -> Any:
        return self.__dict__.get("_fenced_socket_client")

    @_socket_client.setter
    def _socket_client(self, client: Any) -> None:
        if client is not None and not isinstance(client, _StopFencedSocket):
            client = _StopFencedSocket(client, self)
        self.__dict__["_fenced_socket_client"] = client

    # ---------------------------------------------------------------- scopes

    def _run_epoch(self, workspace_id: str) -> int:
        """Current epoch for a workspace. Bumped by every applied stop."""
        return int(self._run_epochs.get(str(workspace_id or ""), 0))

    def _is_stale_run(self, workspace_id: Optional[str] = None) -> bool:
        """True when the calling code belongs to a run that has been stopped.

        ``workspace_id`` narrows the check to one workspace: an emit for a
        different workspace than the scope's is never fenced by it.
        """
        scope = _RUN_SCOPE.get()
        if not scope:
            return False
        scope_ws, epoch = scope
        if workspace_id and str(workspace_id) != scope_ws:
            return False
        return epoch < self._run_epoch(scope_ws)

    def _should_suppress_emit(self, event: str, data: Any) -> bool:
        """The fence itself. Also feeds the resume's emit observer."""
        if event not in _FENCED_EMIT_EVENTS or not isinstance(data, dict):
            return False
        workspace_id = str(data.get("workspaceId") or "")
        if self._is_stale_run(workspace_id):
            label = data.get("featureType") or ",".join(
                str(f) for f in (data.get("featureTypes") or [])
            )
            logger.info(
                "⛔ {} ({}) suppressed for workspace {} — its run was stopped",
                event,
                label or "-",
                workspace_id[:12],
            )
            return True
        if event == "document_feature_response":
            observed = _EMIT_OBSERVER.get()
            feature = str(data.get("featureType") or "")
            if observed is not None and feature:
                observed.add(feature)
        return False

    async def _dispatch_in_run_scope(
        self, event_name: str, data: Any, handler: Any
    ) -> Any:
        """Run one inbound event's handler inside its run scope.

        Called by ``SocketEventHandler.handle`` for every event. Events with no
        ``workspaceId`` (connect, heartbeat) run unscoped, as before.
        """
        workspace_id = ""
        if event_name not in _UNSCOPED_EVENTS and isinstance(data, dict):
            workspace_id = str(data.get("workspaceId") or "").strip()
        if not workspace_id:
            return await handler(data)

        if event_name in _WORK_STARTING_EVENTS:
            self._last_workspace_event_at[workspace_id] = time.time()
        if event_name in SUPERSEDING_RUN_EVENTS:
            self._drop_held_objective_change(workspace_id, trigger=event_name)

        token = _RUN_SCOPE.set((workspace_id, self._run_epoch(workspace_id)))
        task = (
            asyncio.current_task() if event_name in CANCELLABLE_RUN_EVENTS else None
        )
        if task is not None:
            self._register_run_task(workspace_id, task)
        try:
            return await handler(data)
        except asyncio.CancelledError:
            # A BaseException: the `except Exception` in handle() never sees it.
            # Name it, so a stopped run is readable in the log rather than
            # inferred from an absence of lines.
            logger.warning(
                "⏹️ {} for workspace {} cancelled mid-run", event_name, workspace_id[:12]
            )
            raise
        finally:
            _RUN_SCOPE.reset(token)
            if task is not None:
                self._forget_run_task(workspace_id, task)

    # ------------------------------------------------------------ task registry

    def _register_run_task(self, workspace_id: str, task: "asyncio.Task") -> None:
        """Record a task as part of this workspace's running update."""
        if not workspace_id or task is None:
            return
        tasks = self._workspace_run_tasks.setdefault(workspace_id, {})
        tasks[task] = time.time()
        task.add_done_callback(
            lambda finished, ws=workspace_id: self._forget_run_task(ws, finished)
        )

    def _forget_run_task(self, workspace_id: str, task: "asyncio.Task") -> None:
        tasks = self._workspace_run_tasks.get(workspace_id)
        if not tasks:
            return
        tasks.pop(task, None)
        if not tasks:
            self._workspace_run_tasks.pop(workspace_id, None)

    def _adopt_background_task(self, task: "asyncio.Task") -> None:
        """Tie a detached task to the run that spawned it.

        Called from ``_track_background_task``. A task spawned by a run that has
        already been stopped (typically from a CancelledError handler) is
        cancelled on the spot — it would only produce fenced output.
        """
        scope = _RUN_SCOPE.get()
        if not scope:
            return
        workspace_id, epoch = scope
        if epoch < self._run_epoch(workspace_id):
            task.cancel()
            logger.info(
                "Background task spawned by a stopped run cancelled at birth "
                "(workspace={})",
                workspace_id[:12],
            )
            return
        self._register_run_task(workspace_id, task)

    def _cancel_workspace_run_tasks(self, workspace_id: str) -> List["asyncio.Task"]:
        """Cancel every registered task for a workspace; return them to await."""
        current = asyncio.current_task()
        cancelled: List[asyncio.Task] = []
        for task in list((self._workspace_run_tasks.get(workspace_id) or {}).keys()):
            if task is current or task.done():
                continue
            task.cancel()
            cancelled.append(task)
        return cancelled

    # ------------------------------------------------------ held objective change

    def _drop_held_objective_change(self, workspace_id: str, *, trigger: str) -> None:
        """Forget an objective change a stop was holding for a resume."""
        marker = self._pending_objective_changes.get(workspace_id)
        if isinstance(marker, dict) and marker.get("heldByStop"):
            self._pending_objective_changes[workspace_id] = {}
            logger.info(
                "Objective change held by a stop dropped: {} started a new run "
                "(workspace={})",
                trigger,
                workspace_id[:12],
            )

    def _peek_held_objective_change(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        """The held objective change, if it is still young enough to replay.

        Not consumed here: the caller clears it only once the objective has
        actually landed, so a resume that fails (and is re-sent) can retry it.
        An expired or empty marker is cleared on sight.
        """
        marker = self._pending_objective_changes.get(workspace_id)
        if not (isinstance(marker, dict) and marker.get("heldByStop")):
            return None
        held_at = float(marker.get("heldAt") or marker.get("startedAt") or 0.0)
        age = time.time() - held_at if held_at else 0.0
        if not str(marker.get("instruction") or "").strip():
            self._pending_objective_changes[workspace_id] = {}
            return None
        if age > _HELD_OBJECTIVE_MAX_AGE_SECONDS:
            logger.warning(
                "Held objective change for workspace {} is {:.0f}s old — not replayed",
                workspace_id[:12],
                age,
            )
            self._pending_objective_changes[workspace_id] = {}
            return None
        return marker

    async def _hold_objective_change_for_resume(
        self, workspace_id: str, pending_features: Optional[List[str]]
    ) -> bool:
        """Turn an interrupted objective change into a held one (or drop it).

        The new objective exists nowhere else — the resume payload does not
        carry it — so clearing the marker would make the resume regenerate
        the strategy under the OLD objective. It is kept, but disarmed: nothing
        replays it except a ``workspace_resume_request`` that includes
        ``strategy_objective``. If the strategy was already received, the
        objective landed and there is nothing to hold.
        """
        marker = self._pending_objective_changes.get(workspace_id)
        if not (isinstance(marker, dict) and marker.get("instruction")):
            if workspace_id not in self._recovered_workspaces:
                # Cold process: the marker from before a restart lives only in
                # Qdrant, where the post-hydrate recovery would auto-resume it.
                try:
                    restored = await restore_state(
                        workspace_id=workspace_id,
                        kind=CacheKinds.PENDING_OBJECTIVE_CHANGE,
                    )
                    marker = restored if isinstance(restored, dict) else None
                except Exception as restore_err:
                    logger.debug(
                        "Could not read a persisted objective marker on stop: {}",
                        restore_err,
                    )
                    marker = None
        if not (isinstance(marker, dict) and marker.get("instruction")):
            if marker:
                self._pending_objective_changes[workspace_id] = {}
            return False
        still_owed = pending_features is None or "strategy_objective" in (
            pending_features or []
        )
        if not still_owed:
            self._pending_objective_changes[workspace_id] = {}
            return False
        self._pending_objective_changes[workspace_id] = {
            **marker,
            "heldByStop": True,
            "heldAt": float(marker.get("heldAt") or time.time()),
            "attempts": 0,
        }
        return True

    # ------------------------------------------------------------------ stop

    async def on_workspace_stop_request(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_stop_request (Server → AI_Maverick).

        Order is load-bearing:
        1. De-dupe on ``requestId``; ignore a stale re-send (see
           ``_STALE_STOP_TOLERANCE_SECONDS``).
        2. Bump the epoch — the fence is up before anything else happens, so
           nothing the stopped run does from here on can reach the socket,
           the pending-delivery buffer or a resume marker.
        3. Cancel the registered run tasks and wait (bounded) for them to
           unwind: their CancelledError handlers write bookkeeping, and the
           clear in step 4 must come after it.
        4. Drop buffered deliveries (memory AND the persisted copy) and disarm
           recovery markers, so a restart cannot resurface a stopped panel.
        Never replies.
        """
        raw = data if isinstance(data, dict) else {}
        try:
            payload = WorkspaceStopRequestPayload(**raw)
        except Exception as parse_err:
            workspace_id = str(raw.get("workspaceId") or "").strip()
            if not workspace_id:
                logger.error(
                    "workspace_stop_request rejected — no workspaceId ({})", parse_err
                )
                return
            # A malformed stop still stops: this is the moment something is
            # already wrong, and refusing would leave the work running.
            logger.warning(
                "workspace_stop_request for {} failed validation ({}); applying it "
                "with defaults",
                workspace_id[:12],
                parse_err,
            )
            payload = WorkspaceStopRequestPayload(
                workspaceId=workspace_id, requestId=str(raw.get("requestId") or "")
            )

        workspace_id = str(payload.workspaceId or "").strip()
        if not workspace_id:
            logger.error("workspace_stop_request carried an empty workspaceId; ignored")
            return
        request_id = str(payload.requestId or "").strip()
        pending_features = (
            [str(f) for f in payload.pendingFeatures]
            if payload.pendingFeatures is not None
            else None
        )
        # stoppedBy.name is personal data — only the id is ever logged.
        stopped_by = payload.stoppedBy.userId if payload.stoppedBy else ""

        now = time.time()
        seen = self._stop_requests.setdefault(workspace_id, {})
        for rid in [r for r, ts in seen.items() if now - ts > _STOP_DEDUPE_SECONDS]:
            seen.pop(rid, None)
        if request_id and request_id in seen:
            logger.debug(
                "workspace_stop_request {} for {} is a duplicate — already applied",
                request_id,
                workspace_id[:12],
            )
            return
        if request_id:
            seen[request_id] = now

        stopped_at = _parse_iso_utc(payload.stoppedAt or payload.timestamp)
        last_event_at = self._last_workspace_event_at.get(workspace_id)
        if (
            stopped_at is not None
            and last_event_at is not None
            and last_event_at
            > stopped_at.timestamp() + _STALE_STOP_TOLERANCE_SECONDS
        ):
            logger.warning(
                "workspace_stop_request {} for {} ignored as stale: stoppedAt {} "
                "predates newer workspace activity — applying it would stop the "
                "NEW run",
                request_id or "<none>",
                workspace_id[:12],
                stopped_at.isoformat(),
            )
            audit_logger.log_event(
                "workspace_run_stop_ignored",
                {
                    "workspace_id": workspace_id,
                    "request_id": request_id,
                    "reason": "stale_resend",
                },
            )
            return

        # 2. The fence.
        epoch = self._run_epoch(workspace_id) + 1
        self._run_epochs[workspace_id] = epoch

        # 3. Cancel and settle.
        cancelled = self._cancel_workspace_run_tasks(workspace_id)
        if self._pending_reemit_workspace_id == workspace_id:
            # Otherwise the next reconnect replays this workspace's panels.
            self._pending_reemit_workspace_id = None
            self._pending_reemit_user_id = None
        unsettled = 0
        if cancelled:
            _, still_running = await asyncio.wait(
                cancelled, timeout=_STOP_SETTLE_SECONDS
            )
            unsettled = len(still_running)
            if unsettled:
                logger.warning(
                    "{} task(s) for workspace {} did not unwind within {:.0f}s of "
                    "the stop — their output is fenced regardless",
                    unsettled,
                    workspace_id[:12],
                    _STOP_SETTLE_SECONDS,
                )

        # 4. Buffers and markers. Assigning (not popping) writes the empty value
        #    through to Qdrant, which also covers a cold process whose buffer
        #    exists only in the persisted copy.
        buffered = self._pending_feature_deliveries.get(workspace_id)
        dropped = sorted(buffered.keys()) if isinstance(buffered, dict) else []
        self._pending_feature_deliveries[workspace_id] = {}
        held = await self._hold_objective_change_for_resume(
            workspace_id, pending_features
        )
        # A reload the stop interrupted must be re-runnable, not swallowed as
        # "in flight" for the next 30 minutes.
        for ledger in (
            self._document_reload_requests.get(workspace_id),
            self._resume_requests.get(workspace_id),
        ):
            for entry in (ledger or {}).values():
                if entry.get("status") == "in_flight":
                    entry["status"] = "stopped"
                    entry["ts"] = now
        # The post-hydrate recovery must not run for this workspace now: its
        # only jobs are flushing the buffer and resuming objective changes.
        self._recovered_workspaces.add(workspace_id)

        logger.success(
            "⏹️ Run stopped for workspace {} (request={}, owner={}, stoppedBy={}): "
            "{} task(s) cancelled, dropped buffered {}, objective change {}",
            workspace_id[:12],
            request_id or "<none>",
            payload.userId or "<unknown>",
            stopped_by or "<unknown>",
            len(cancelled),
            dropped or "nothing",
            "held for resume" if held else "none",
        )
        audit_logger.log_event(
            "workspace_run_stopped",
            {
                "workspace_id": workspace_id,
                "user_id": payload.userId or "",
                "stopped_by": stopped_by,
                "request_id": request_id,
                "epoch": epoch,
                "cancelled_tasks": len(cancelled),
                "unsettled_tasks": unsettled,
                "dropped_deliveries": dropped,
                "objective_change_held": held,
                "received_features": list(payload.receivedFeatures or []),
                "pending_features": pending_features,
            },
        )

    # ---------------------------------------------------------------- resume

    def _should_skip_resume_request(self, workspace_id: str, request_id: str) -> bool:
        """In flight, or completed inside the window → skip. Failed/stopped → run."""
        if not request_id:
            return False
        entry = (self._resume_requests.get(workspace_id) or {}).get(request_id)
        if not entry:
            return False
        status = str(entry.get("status") or "")
        age = time.time() - float(entry.get("ts") or 0.0)
        if status == "in_flight" and age < _RESUME_INFLIGHT_TIMEOUT_SECONDS:
            logger.info(
                "Skipping workspace_resume_request {} for {}: already in flight "
                "({:.0f}s ago)",
                request_id,
                workspace_id[:12],
                age,
            )
            return True
        if status == "done" and age < _RESUME_DEDUPE_SECONDS:
            logger.info(
                "Skipping duplicate workspace_resume_request {} for {} (completed "
                "{:.0f}s ago)",
                request_id,
                workspace_id[:12],
                age,
            )
            return True
        return False

    def _mark_resume_request(self, workspace_id: str, request_id: str, status: str) -> None:
        if not request_id:
            return
        ledger = self._resume_requests.setdefault(workspace_id, {})
        now = time.time()
        ledger[request_id] = {"status": status, "ts": now}
        for rid in [
            r
            for r, e in ledger.items()
            if now - float(e.get("ts") or 0.0) > _RESUME_INFLIGHT_TIMEOUT_SECONDS * 2
        ]:
            ledger.pop(rid, None)

    @staticmethod
    def _normalize_resume_features(requested: Iterable[Any]) -> Tuple[List[str], List[str]]:
        """Split the requested list into (known, in product order) and unknown.

        An unknown featureType is dropped from BOTH the refresh and the run:
        declaring a loader this service cannot satisfy would strand it.
        """
        wanted: Set[str] = set()
        unknown: List[str] = []
        for raw in requested or []:
            feature = str(raw or "").strip()
            if not feature:
                continue
            if feature in RESUMABLE_FEATURES:
                wanted.add(feature)
            elif feature not in unknown:
                unknown.append(feature)
        return [f for f in RESUMABLE_FEATURES if f in wanted], unknown

    async def on_workspace_resume_request(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_resume_request (Server → AI_Maverick).

        Flow:
        1. Parse, revoke a pending purge, apply the roster, de-dupe on requestId
        2. Cache currentState, hydrate, resolve role, upsert context
        3. Normalise reEmitFeatures (known only, product order)
        4. ``workspace:refresh`` declaring EXACTLY those features
        5. Reconcile evidenceFiles against the vector store
        6. Produce each feature, in order, under the pipeline lock
        7. Release every declared loader that did not get an emit
        """
        payload = WorkspaceResumeRequestPayload(**(data or {}))
        workspace_id = str(payload.workspaceId or "").strip()
        user_id = str(payload.userId or "").strip()
        request_id = str(payload.requestId or "").strip()

        # Resume is live workspace traffic: revoke any armed compliance purge.
        self.cancel_pending_purge(workspace_id, trigger="workspace_resume_request")
        # Same semantics as workspace_sync: an absent roster means "not shared".
        try:
            set_participants(workspace_id, payload.participants, authoritative=True)
        except Exception as participants_err:
            logger.warning(
                "Participant roster update on resume failed (non-fatal): {}",
                participants_err,
            )

        if self._should_skip_resume_request(workspace_id, request_id):
            return
        self._mark_resume_request(workspace_id, request_id, "in_flight")

        features, unknown = self._normalize_resume_features(payload.reEmitFeatures)
        logger.info(
            "▶️ workspace_resume_request {} for workspace {} (owner={}): resuming {}",
            request_id or "<none>",
            workspace_id[:12],
            user_id,
            features,
        )
        if unknown:
            logger.warning(
                "workspace_resume_request {}: unknown featureType(s) {} dropped from "
                "the refresh and the run",
                request_id or "<none>",
                unknown,
            )
        if not features:
            logger.info(
                "workspace_resume_request {} named nothing this service produces — "
                "no refresh, no work",
                request_id or "<none>",
            )
            self._mark_resume_request(workspace_id, request_id, "done")
            return

        workspace_role = "ADJUDICATOR"
        refreshed = False
        emitted: Set[str] = set()
        try:
            if payload.currentState is not None:
                try:
                    self._workspace_current_state_payload[workspace_id] = (
                        payload.currentState.model_dump()
                    )
                except Exception as cs_err:
                    logger.debug("Failed to cache currentState on resume: {}", cs_err)

            # Hydrate BEFORE any upsert (a cold upsert wipes token_usage), and
            # before reading the held objective (a cold one lives in Qdrant).
            try:
                await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
            except Exception as hydrate_err:
                logger.debug(
                    "Workspace cache hydration before resume failed ({}): {}",
                    workspace_id[:12],
                    hydrate_err,
                )

            if payload.workspaceRole:
                await self._remember_workspace_role(
                    workspace_id, user_id, str(payload.workspaceRole)
                )
            workspace_role = await self._resolve_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                preferred_role=payload.workspaceRole,
            )

            active_files = [
                f
                for f in (payload.evidenceFiles or [])
                if not (getattr(f, "isDeleted", False) or getattr(f, "is_deleted", False))
            ]
            if payload.evidenceFiles is not None:
                from app.workspace.context import context_manager

                updated_at = datetime.utcnow()
                try:
                    if payload.timestamp:
                        updated_at = datetime.fromisoformat(
                            payload.timestamp.replace("Z", "+00:00")
                        )
                except Exception:
                    pass
                context_manager.upsert_workspace(
                    workspace_id,
                    user_id,
                    title=payload.title,
                    status=payload.status,
                    workspace_role=payload.workspaceRole or workspace_role,
                    evidence_files=active_files,
                    updated_at=updated_at,
                )

            held_objective = self._peek_held_objective_change(workspace_id)
            if held_objective and "strategy_objective" not in features:
                # The strategy was received, so the objective already landed.
                self._pending_objective_changes[workspace_id] = {}
                held_objective = None

            # The loaders go on BEFORE the slow part, and name exactly what
            # will be produced: every featureType declared here is one the
            # frontend waits for.
            await self._emit_workspace_refresh(
                workspace_id=workspace_id,
                user_id=user_id,
                reason="ai_resume_started",
                feature_types=list(features),
            )
            refreshed = True

            token = _EMIT_OBSERVER.set(emitted)
            try:
                batch_result, ingest_usage, deleted_ids = (
                    await self._reconcile_resume_evidence(
                        workspace_id,
                        user_id,
                        payload.evidenceFiles,
                        reingest_unassessed="document_assessments" in features,
                    )
                )
                if ingest_usage is not None and "document_assessments" not in features:
                    # Spent, but no emit will carry it: fold it into the
                    # cumulative so the next emit bills it.
                    self._ingest_usage_metadata(workspace_id, ingest_usage)

                async with self._workspace_pipeline_lock(workspace_id):
                    for feature in features:
                        try:
                            await self._produce_resumed_feature(
                                feature,
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=workspace_role,
                                batch_result=batch_result,
                                ingest_usage=ingest_usage,
                                deleted_ids=deleted_ids,
                                held_objective=held_objective,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as feature_err:
                            logger.opt(exception=feature_err).warning(
                                "Resumed {} failed for workspace {}: {}",
                                feature,
                                workspace_id[:12],
                                feature_err,
                            )
            finally:
                _EMIT_OBSERVER.reset(token)

            missing = [f for f in features if f not in emitted]
            if missing:
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    missing,
                    reason="resume_incomplete",
                    workspace_role=workspace_role,
                )
            self._mark_resume_request(workspace_id, request_id, "done")
            audit_logger.log_event(
                "workspace_run_resumed",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "request_id": request_id,
                    "features": features,
                    "unknown_features": unknown,
                    "released_features": missing,
                    "objective_change_replayed": bool(held_objective),
                    "ingested": int((batch_result or {}).get("successful") or 0),
                    "deleted": len(deleted_ids or []),
                },
            )
            logger.success(
                "✅ Resume {} completed for workspace {}: emitted {}{}",
                request_id or "<none>",
                workspace_id[:12],
                sorted(emitted),
                f", released {missing}" if missing else "",
            )
        except asyncio.CancelledError:
            # Stopped again. The backend clears its own spinners on a stop, so
            # no loader release — and a release would be fenced anyway.
            self._mark_resume_request(workspace_id, request_id, "stopped")
            logger.warning(
                "Resume {} for workspace {} was stopped mid-run",
                request_id or "<none>",
                workspace_id[:12],
            )
            raise
        except Exception as resume_err:
            self._mark_resume_request(workspace_id, request_id, "failed")
            logger.opt(exception=resume_err).error(
                "workspace_resume_request {} failed for workspace {}: {}",
                request_id or "<none>",
                workspace_id[:12],
                resume_err,
            )
            if refreshed:
                # Every declared loader must end; a spinner with no emit coming
                # hangs for the rest of the session.
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    [f for f in features if f not in emitted],
                    reason="resume_failed",
                    workspace_role=workspace_role,
                )
            raise

    async def _reconcile_resume_evidence(
        self,
        workspace_id: str,
        user_id: str,
        evidence_files: Optional[List[Any]],
        *,
        reingest_unassessed: bool,
    ) -> Tuple[Optional[Dict[str, Any]], Any, List[str]]:
        """Bring the vector store in line with ``evidenceFiles`` before generating.

        A stop can land mid-ingestion (chunks written, no assessment) or
        mid-deletion. Returns ``(batch_result, ingest_usage, deleted_ids)``.

        Deliberately conservative, because unlike a version restore a resume
        is not asked to change the document set:
        - no ``evidenceFiles`` → no reconciliation at all;
        - any active file without an id → no deletions (an id-less file's live
          copy would otherwise look orphaned and be deleted);
        - a live-document read failure → skip and generate from what is there.

        ``reingest_unassessed`` is set when ``document_assessments`` is itself
        being resumed — the one signal that ingestion was what got stopped.
        Documents then indexed but absent from the assessments manifest are
        re-read (old chunks first), so the manifest and the store agree.
        """
        if evidence_files is None:
            return None, None, []
        active = [
            f
            for f in evidence_files
            if not (getattr(f, "isDeleted", False) or getattr(f, "is_deleted", False))
        ]
        files_by_id: Dict[str, Any] = {}
        id_less = 0
        for f in active:
            fid = str(getattr(f, "id", None) or getattr(f, "_id", None) or "").strip()
            if fid:
                files_by_id[fid] = f
            else:
                id_less += 1

        from app.vectorstore.qdrant import vector_store

        try:
            live_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
            live_ids = {
                str(d.get("document_id")).strip()
                for d in (live_docs or [])
                if d.get("document_id")
            }
        except Exception as live_err:
            logger.warning(
                "Resume could not read live documents for {} ({}); generating from "
                "what is indexed",
                workspace_id[:12],
                live_err,
            )
            return None, None, []

        target_ids = set(files_by_id.keys())
        to_delete = sorted(live_ids - target_ids) if not id_less else []
        if id_less and (live_ids - target_ids):
            logger.warning(
                "Resume: {} active file(s) carry no _id, so {} indexed document(s) "
                "not matched to evidenceFiles are left in place",
                id_less,
                len(live_ids - target_ids),
            )
        partial: List[str] = []
        if reingest_unassessed:
            cached = self._workspace_document_assessments_payload.get(workspace_id)
            manifest_ids = {
                str(e.get("documentId") or "").strip()
                for e in (
                    ((cached or {}).get("payload") or {}).get("documentAssessments")
                    or []
                )
                if isinstance(e, dict)
            } if isinstance(cached, dict) else set()
            partial = sorted((target_ids & live_ids) - manifest_ids)
        to_ingest = sorted((target_ids - live_ids) | set(partial))

        logger.info(
            "▶️ Resume reconciliation for {}: live={}, target={}, delete={}, "
            "ingest={} (of which re-read unassessed={})",
            workspace_id[:12],
            len(live_ids),
            len(target_ids),
            to_delete,
            to_ingest,
            partial,
        )

        purge_ids = to_delete + partial
        if purge_ids:
            await vector_store.delete_documents(
                workspace_id=workspace_id, document_ids=purge_ids
            )
        if to_delete:
            # Deleted evidence must stay visible as a gap (CLAUDE.md).
            try:
                self._record_removed_documents(
                    workspace_id,
                    self._prune_document_assessments(workspace_id, to_delete),
                )
            except Exception as prune_err:
                logger.warning(
                    "Assessment prune / removed-document ledger on resume failed: {}",
                    prune_err,
                )

        if not to_ingest:
            return None, None, to_delete

        files_to_process = []
        for fid in to_ingest:
            f = files_by_id[fid]
            files_to_process.append(
                {
                    "url": f.url,
                    "filename": f.filename,
                    "originalName": f.originalName,
                    "storeFileName": f.storeFileName,
                    "extension": f.extension,
                    "fileHash": f.fileHash,
                    "path": f.path,
                    "uploadedAt": f.uploadedAt,
                    "_id": fid,
                }
            )
        batch_result: Optional[Dict[str, Any]] = None
        ingest_usage = None
        try:
            # Own scope: OCR / STT / embeddings are billed to nobody without one.
            with capture_usage() as ingest_usage:
                batch_result = await document_processor.process_batch(
                    files=files_to_process,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as ingest_err:
            logger.opt(exception=ingest_err).error(
                "Resume re-ingestion failed for {}: {}; generating from the "
                "documents that are present",
                workspace_id[:12],
                ingest_err,
            )
            return None, ingest_usage, to_delete

        if batch_result:
            try:
                # Merge (never replace): the manifest must list every live doc.
                envelope = self._build_document_assessment_envelope(
                    workspace_id, user_id, batch_result
                )
                self._workspace_document_assessments_payload[workspace_id] = (
                    self._merge_document_assessments(workspace_id, envelope)
                )
                self._forget_removed_documents(
                    workspace_id,
                    [
                        r.get("document_id")
                        for r in batch_result.get("results", [])
                        if r.get("document_id") and r.get("success")
                    ],
                )
            except Exception as merge_err:
                logger.warning("Manifest merge after resume ingestion failed: {}", merge_err)
        return batch_result, ingest_usage, to_delete

    async def _produce_resumed_feature(
        self,
        feature: str,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
        ingest_usage: Any,
        deleted_ids: List[str],
        held_objective: Optional[Dict[str, Any]],
    ) -> None:
        """Generate and emit ONE resumed feature. Caller holds the pipeline lock.

        Every branch reuses the generator the normal run uses, so currentState
        tagging (isMatched) is applied exactly as on a re-run. Nothing here
        emits a feature other than ``feature``.
        """
        if not self._socket_client:
            return
        replace_derived = bool(deleted_ids)

        if feature == "document_assessments":
            if batch_result:
                envelope = self._build_document_assessment_envelope(
                    workspace_id, user_id, batch_result
                )
            else:
                cached = self._workspace_document_assessments_payload.get(workspace_id)
                if not isinstance(cached, dict):
                    return  # the loader release settles it
                envelope = dict(cached)
            envelope["metadata"] = self._ingest_usage_metadata(workspace_id, ingest_usage)
            await self._socket_client.emit("document_feature_response", envelope)
            return

        if feature == "workspace_metadata":
            with capture_usage() as meta_usage:
                meta = await self._get_workspace_category_and_subcategory(
                    workspace_id=workspace_id, user_id=user_id
                )
            envelope = {
                "featureType": "workspace_metadata",
                "workspaceId": workspace_id,
                "userId": user_id,
                # Empty title + updateTitle=False: a resume never renames the case.
                "payload": {
                    "workspaceTitle": "",
                    "updateTitle": False,
                    "caseCategory": meta.get("caseCategory", "General"),
                    "subcategories": meta.get("subcategories", []),
                    "description": meta.get("description", ""),
                },
            }
            envelope["metadata"] = self._ingest_usage_metadata(workspace_id, meta_usage)
            await self._socket_client.emit("document_feature_response", envelope)
            self._workspace_metadata_payload[workspace_id] = envelope
            return

        if feature == "strategy_objective":
            current = self._workspace_strategy_payload.get(workspace_id)
            if held_objective and isinstance(current, dict) and (
                current.get("caseSummary") or ""
            ).strip():
                # The stopped run was an objective change. Replaying the
                # lawyer's instruction is the only way the new objective lands:
                # a plain regeneration would re-derive the old one.
                logger.info(
                    "Resume replays the objective change held by the stop "
                    "(workspace={})",
                    workspace_id[:12],
                )
                landed = await self._regenerate_strategy_for_new_objective(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=str(
                        held_objective.get("workspaceRole") or workspace_role
                    ),
                    user_objective_instruction=str(held_objective.get("instruction")),
                    current_strategy=current,
                )
                if landed:
                    self._clear_pending_objective_change(workspace_id)
                else:
                    # Kept held: a re-sent resume retries it. The loader is
                    # settled by the caller's release, from the cached strategy.
                    logger.warning(
                        "Held objective change did not land on resume (workspace={}); "
                        "kept for a retry",
                        workspace_id[:12],
                    )
                return
            strategies_result = None
            _u_strategy = None
            try:
                with capture_usage() as _u_strategy:
                    strategies_result = await self._generate_strategies_from_documents(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=batch_result,
                        is_update=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as strategy_err:
                logger.warning(
                    "Strategy generation on resume failed: {}; using fallback",
                    strategy_err,
                )
            if not strategies_result:
                strategies_result = self._strategy_objective_fallback(
                    workspace_id, user_id, workspace_role, is_update=True
                )
            await self._emit_document_feature(
                feature_type="strategy_objective",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=strategies_result,
                usage=_u_strategy,
            )
            self._workspace_strategy_payload[workspace_id] = strategies_result
            return

        if feature == "analysis_report":
            analysis_report = None
            _u_analysis = None
            try:
                with capture_usage() as _u_analysis:
                    analysis_report = await self._generate_analysis_report_from_documents(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=batch_result,
                        is_update=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as ar_err:
                logger.warning(
                    "Analysis report on resume failed: {}; using fallback", ar_err
                )
            if not isinstance(analysis_report, dict) or not analysis_report:
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
            try:
                analysis_report = await self._tag_analysis_report_against_current_state(
                    workspace_id, analysis_report, log_context=" (resume)"
                )
            except Exception as tag_err:
                logger.warning(
                    "Analysis report currentState tagging on resume failed: {}; "
                    "emitting untagged",
                    tag_err,
                )
            await self._emit_document_feature(
                feature_type="analysis_report",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=analysis_report,
                usage=_u_analysis,
            )
            self._workspace_analysis_payload[workspace_id] = analysis_report
            return

        if feature == "list_of_dates":
            await self._generate_and_emit_list_of_dates(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                deleted_doc_ids=deleted_ids or None,
            )
        elif feature == "applicable_laws":
            await self._generate_and_emit_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                merge_with_previous=not replace_derived,
            )
        elif feature == "procedural_timeline":
            await self._generate_and_emit_procedural_timeline(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                deleted_doc_ids=deleted_ids or None,
            )
        elif feature == "relevant_parties":
            await self._generate_and_emit_relevant_parties(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
        elif feature == "case_laws":
            await self._generate_and_emit_case_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                merge_with_previous=not replace_derived,
            )
        elif feature == "drafting_assistant":
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
