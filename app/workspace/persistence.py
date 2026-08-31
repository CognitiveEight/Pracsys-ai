"""
Workspace state persistence helpers.

Backs the in-memory workspace caches (analysis report, procedural timeline,
applicable laws, workspace meta, custom instructions, …) with Qdrant so the
state survives an AI service restart or socket reconnect.

Design:
- The Qdrant `QdrantStore.upsert_workspace_state_cache` stores arbitrary JSON
  payloads keyed by (workspace_id, cache_kind, optional chat_id).
- `persist_state` performs a fire-and-forget background write so callers do
  not have to await Qdrant on every mutation. It is safe from sync code as
  long as an event loop is running; otherwise the write is queued via
  `asyncio.run` on a worker thread.
- `restore_state` and `hydrate_workspace_state` are async and used by event
  handlers when they detect a cold workspace (typical reason: AI service
  restarted while the chat server kept the same workspace_id).
- `PersistentWorkspaceDict` is a drop-in dict replacement for module-level
  `Dict[workspace_id, Any]` caches that need write-through behavior with no
  call-site changes.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Iterable, Optional

from loguru import logger


# Cache kind constants — keep these in sync with the strings in events.py /
# workspace context so reads and writes line up.
class CacheKinds:
    WORKSPACE_META = "workspace_meta"
    ANALYSIS_REPORT = "analysis_report"
    APPLICABLE_LAWS = "applicable_laws"
    # Relevant case-law precedents panel (judgments matched to the workspace,
    # each backed by a real source URL). Generated on workspace_created /
    # workspace_sync alongside applicable_laws and re-emitted on reconnect.
    CASE_LAWS = "case_laws"
    # Relevant parties panel (people/organisations extracted from the documents
    # and classified into a role enum). Document-driven: regenerated on
    # create/sync/doc-delete, re-emitted on reconnect.
    RELEVANT_PARTIES = "relevant_parties"
    # Per-workspace memory of parties the user has deleted, so a later
    # regeneration does not silently re-add them (mirrors REJECTED_ANALYSIS_ITEMS).
    DELETED_PARTIES = "deleted_parties"
    # Per-workspace ledger of documents the user uploaded and later deleted,
    # snapshotted from the document_assessments entry before it is pruned
    # (documentType + key fields survive the document itself). Read by the
    # analysis report so a withdrawn document is reported as a gap in
    # missingInfo instead of silently vanishing. Entries are dropped when the
    # same documentId is re-ingested, which is what makes a version restore
    # self-healing. Shape:
    #   [{"documentId","fileName","documentType","keyFields","removedAt"}, ...]
    REMOVED_DOCUMENTS = "removed_documents"
    PROCEDURAL_TIMELINE = "procedural_timeline"
    CUSTOM_INSTRUCTIONS = "custom_instructions"
    LIST_OF_DATES = "list_of_dates"
    WORKSPACE_METADATA_PAYLOAD = "workspace_metadata_payload"
    DOCUMENT_ASSESSMENTS = "document_assessments"
    STRATEGY_PAYLOAD = "strategy_payload"
    # Per-workspace memory of analysis-report items the user has rejected via
    # analysis_report_feedback. Used so that an objective-change regeneration
    # does not silently re-introduce items the user already dismissed.
    # Shape: {"missingInfo": [str, ...], "riskOrWeakness": [str, ...]}
    REJECTED_ANALYSIS_ITEMS = "rejected_analysis_items"
    # Per-workspace memory of missingInfo entries the user added themselves via
    # added_missing_information, so the answered-by-upload resolution pass
    # never auto-removes them (only analysis_report_feedback can).
    USER_ADDED_MISSING_INFO = "user_added_missing_info"
    # Drafting Assistant — proactive suggested drafts panel (one entry per
    # workspace, regenerated on workspace_created / workspace_sync, re-emitted
    # on reconnect like analysis_report).
    DRAFTING_SUGGESTIONS = "drafting_suggestions"
    # Latest currentState echoed back by the frontend on workspace_sync — the
    # previously-emitted, user-curated panel state used to tag regenerated items
    # as matched (carried over) vs new. See schemas.WorkspaceCurrentState.
    CURRENT_STATE = "current_state"


# All workspace-scoped cache kinds we routinely read on hydrate.
WORKSPACE_HYDRATION_KINDS = (
    CacheKinds.WORKSPACE_META,
    CacheKinds.ANALYSIS_REPORT,
    CacheKinds.APPLICABLE_LAWS,
    CacheKinds.CASE_LAWS,
    CacheKinds.RELEVANT_PARTIES,
    CacheKinds.DELETED_PARTIES,
    CacheKinds.REMOVED_DOCUMENTS,
    CacheKinds.PROCEDURAL_TIMELINE,
    CacheKinds.CUSTOM_INSTRUCTIONS,
    CacheKinds.LIST_OF_DATES,
    CacheKinds.WORKSPACE_METADATA_PAYLOAD,
    CacheKinds.DOCUMENT_ASSESSMENTS,
    CacheKinds.STRATEGY_PAYLOAD,
    CacheKinds.REJECTED_ANALYSIS_ITEMS,
    CacheKinds.USER_ADDED_MISSING_INFO,
    CacheKinds.DRAFTING_SUGGESTIONS,
    CacheKinds.CURRENT_STATE,
)


def _vector_store():
    """Lazy import to avoid a circular dependency at module import time."""
    from app.vectorstore.qdrant import vector_store

    return vector_store


def _schedule_async(coro) -> None:
    """Run a coroutine as fire-and-forget from any context.

    Tries the running loop first; if no loop is available (e.g. called from
    code that is itself sync and outside an event loop), drops onto a daemon
    thread so the write still happens without blocking the caller.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        loop.create_task(coro)
        return

    def _runner():
        try:
            asyncio.run(coro)
        except Exception as e:  # pragma: no cover - best-effort persistence
            logger.debug("Background persistence task failed: {}", e)

    threading.Thread(target=_runner, daemon=True).start()


def _persisted_token_usage(data: Any) -> Any:
    """Return the ``token_usage`` block inside a WORKSPACE_META payload, if any."""
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get("token_usage") or None


async def _preserve_token_usage(workspace_id: str, data: Any) -> Any:
    """Keep a persisted cumulative bill from being wiped by a cold-context write.

    ``upsert_workspace_state_cache`` fully overwrites the WORKSPACE_META point, and
    ``ContextManager.upsert_workspace`` persists immediately after creating a
    context whose ``metadata`` is ``{}`` on cold memory — which would drop the
    workspace's cumulative ``token_usage`` (``totalPrice`` back to 0 after a
    restart). When the outgoing payload has no ``token_usage`` but the stored row
    does, carry the stored block over.

    Deliberately narrow: only ``token_usage`` is preserved, because only that key
    is append-only money. Every other metadata key is legitimately replaceable.
    Best-effort — on any failure the write proceeds unchanged.

    Note the write-back also heals the IN-MEMORY context, by design:
    ``WorkspaceContext.to_cache_dict`` puts the live ``self.metadata`` dict into the
    payload (same object, not a copy), so restoring ``token_usage`` here means the
    next ``add_token_usage`` continues from the recovered totals instead of counting
    up from zero and overwriting the row again on its own persist.
    """
    if _persisted_token_usage(data) is not None:
        return data
    try:
        existing = await restore_state(
            workspace_id=workspace_id, kind=CacheKinds.WORKSPACE_META
        )
        stored = _persisted_token_usage(existing)
        if stored is None:
            return data
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        metadata["token_usage"] = stored
        logger.warning(
            "Preserved cumulative token_usage for workspace {} (totalPrice={}): a "
            "cold-context persist would have wiped it — hydrate before upsert",
            str(workspace_id)[:12],
            stored.get("totalPrice") if isinstance(stored, dict) else "?",
        )
    except Exception as e:  # never block the write on the guard
        logger.debug(
            "token_usage preservation check failed (workspace={}): {}", workspace_id, e
        )
    return data


# ---------------------------------------------------------------------------
# Purge suppression
# ---------------------------------------------------------------------------
# `persist_state` is fire-and-forget: the Qdrant upsert is scheduled and the
# caller moves on. That is fine normally, but it races the compliance purge in
# `workspace_deleted` — an upsert scheduled just before the purge can land just
# after it, silently recreating a cache point for data we told the client was
# erased. A deletion that half-works is worse than one that fails loudly.
#
# So a purged workspace is recorded here and every subsequent write for it is
# dropped at the source. `allow_workspace_persistence` lifts the block when the
# workspace legitimately comes back (a restore inside the grace window, or a
# fresh workspace_sync after it) — see WorkspacePurgeMixin.
_purge_blocked_workspaces: set = set()
_purge_lock = threading.Lock()


def block_workspace_persistence(workspace_id: str) -> None:
    """Drop all future state writes for a workspace (called by the purge)."""
    if not workspace_id:
        return
    with _purge_lock:
        _purge_blocked_workspaces.add(str(workspace_id))


def allow_workspace_persistence(workspace_id: str) -> None:
    """Re-enable state writes for a workspace that has legitimately returned."""
    if not workspace_id:
        return
    with _purge_lock:
        _purge_blocked_workspaces.discard(str(workspace_id))


def is_workspace_persistence_blocked(workspace_id: str) -> bool:
    """True when a purge has disabled writes for this workspace."""
    if not workspace_id:
        return False
    with _purge_lock:
        return str(workspace_id) in _purge_blocked_workspaces


def persist_state(
    workspace_id: str,
    kind: str,
    data: Any,
    *,
    chat_id: Optional[str] = None,
) -> None:
    """Schedule a fire-and-forget persistence of workspace state to Qdrant."""
    if not workspace_id or not kind:
        return
    if data is None:
        return
    # Refuse to resurrect state for a purged workspace (see the note above).
    if is_workspace_persistence_blocked(workspace_id):
        logger.debug(
            "Dropping state write for purged workspace (workspace={}, kind={})",
            workspace_id,
            kind,
        )
        return

    async def _do_persist():
        nonlocal data
        try:
            if kind == CacheKinds.WORKSPACE_META:
                data = await _preserve_token_usage(workspace_id, data)
            ok = await _vector_store().upsert_workspace_state_cache(
                workspace_id=workspace_id,
                kind=kind,
                data=data,
                chat_id=chat_id,
            )
            if not ok:
                logger.debug(
                    "State cache upsert returned False (workspace={}, kind={})",
                    workspace_id,
                    kind,
                )
        except Exception as e:  # pragma: no cover - persistence is best-effort
            logger.debug(
                "State cache upsert raised (workspace={}, kind={}): {}",
                workspace_id,
                kind,
                e,
            )

    _schedule_async(_do_persist())


async def restore_state(
    workspace_id: str,
    kind: str,
    *,
    chat_id: Optional[str] = None,
) -> Optional[Any]:
    """Read a single previously-persisted state payload from Qdrant."""
    if not workspace_id or not kind:
        return None
    try:
        return await _vector_store().get_workspace_state_cache(
            workspace_id=workspace_id,
            kind=kind,
            chat_id=chat_id,
        )
    except Exception as e:
        logger.debug(
            "State cache restore raised (workspace={}, kind={}): {}",
            workspace_id,
            kind,
            e,
        )
        return None


async def hydrate_workspace_state(
    workspace_id: str,
    kinds: Iterable[str] = WORKSPACE_HYDRATION_KINDS,
) -> Dict[str, Any]:
    """Read every requested cache kind for a workspace in parallel.

    Returns a dict mapping kind -> payload (or missing key when not cached).
    Used after AI service restart to repopulate the in-memory dictionaries.
    """
    if not workspace_id:
        return {}
    kind_list = [k for k in kinds if k]
    if not kind_list:
        return {}
    results = await asyncio.gather(
        *[restore_state(workspace_id=workspace_id, kind=k) for k in kind_list],
        return_exceptions=True,
    )
    out: Dict[str, Any] = {}
    for kind, value in zip(kind_list, results):
        if isinstance(value, BaseException):
            continue
        if value is None:
            continue
        out[kind] = value
    return out


class PersistentWorkspaceDict(dict):
    """`dict[str, Any]` keyed by workspace_id that mirrors writes to Qdrant.

    Used as a drop-in replacement for the in-memory caches in
    `app.socket.events.AIEventHandler` so we get write-through persistence
    with zero call-site changes. Reads remain pure dict accesses; explicit
    hydration is performed by `hydrate_workspace_state` at chat entry points.
    """

    def __init__(self, kind: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._kind = kind

    @property
    def cache_kind(self) -> str:
        return self._kind

    def __setitem__(self, key: Any, value: Any) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        if isinstance(key, str) and key:
            persist_state(workspace_id=key, kind=self._kind, data=value)

    def setdefault(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default
            return default
        return super().__getitem__(key)

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        merged = dict(*args, **kwargs)
        for k, v in merged.items():
            self[k] = v

    def pop(self, key: Any, *default: Any) -> Any:  # type: ignore[override]
        return super().pop(key, *default)

    def hydrate_from_cache(self, workspace_id: str, value: Any) -> None:
        """Populate the in-memory entry without re-persisting (loop avoidance)."""
        if not workspace_id or value is None:
            return
        super().__setitem__(workspace_id, value)
