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


# All workspace-scoped cache kinds we routinely read on hydrate.
WORKSPACE_HYDRATION_KINDS = (
    CacheKinds.WORKSPACE_META,
    CacheKinds.ANALYSIS_REPORT,
    CacheKinds.APPLICABLE_LAWS,
    CacheKinds.PROCEDURAL_TIMELINE,
    CacheKinds.CUSTOM_INSTRUCTIONS,
    CacheKinds.LIST_OF_DATES,
    CacheKinds.WORKSPACE_METADATA_PAYLOAD,
    CacheKinds.DOCUMENT_ASSESSMENTS,
    CacheKinds.STRATEGY_PAYLOAD,
    CacheKinds.REJECTED_ANALYSIS_ITEMS,
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

    async def _do_persist():
        try:
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
        *[
            restore_state(workspace_id=workspace_id, kind=k)
            for k in kind_list
        ],
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
