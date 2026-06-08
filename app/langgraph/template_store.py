"""In-memory, lawyer-scoped template cache.

Templates are pushed to this service in real time by the backend via the
inbound ``template_sync`` socket event (``uploaded`` / ``renamed`` /
``deleted``), and the backend re-pushes the full library on every
(re)connect. Because the backend is the source of truth and re-syncs on
connect, we do **not** persist templates ourselves — they live only here,
in process memory, keyed by ``(user_id, template_id)``.

This replaces the previous Qdrant ``user_state_cache`` (``kind="template"``)
persistence for templates. The four operations mirror the old Qdrant
helpers so the drafting agent's call sites map across one-to-one:

    put          ← upsert_user_state_cache
    get          ← get_user_state_cache
    list_for_user← list_user_state_cache
    delete       ← delete_user_state_cache

The skeleton DOCX used for exact-template rendering still lives in GCS; only
its pointer (``template_docx_gcs_uri``) is carried inside the cached payload
here — so dropping Qdrant does not affect skeleton production/consumption.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Optional

from loguru import logger


class TemplateStore:
    """Process-local ``{user_id: {template_id: payload}}`` template cache.

    All public methods are ``async`` and guarded by a single lock so the
    read-modify-write in :meth:`rename` (and concurrent ``template_sync``
    batches) cannot interleave. Stored payloads are deep-copied on the way
    in and out so callers cannot mutate cached state by reference.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def put(
        self, *, user_id: str, template_id: str, payload: Dict[str, Any]
    ) -> bool:
        """Insert or overwrite a template payload. Returns True on success."""
        if not user_id or not template_id:
            return False
        async with self._lock:
            self._store.setdefault(user_id, {})[template_id] = copy.deepcopy(
                payload
            )
        return True

    async def get(
        self, *, user_id: str, template_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return a deep copy of the cached payload, or None if absent."""
        async with self._lock:
            data = self._store.get(user_id, {}).get(template_id)
            return copy.deepcopy(data) if isinstance(data, dict) else None

    async def list_for_user(self, *, user_id: str) -> List[Dict[str, Any]]:
        """Return deep copies of every cached payload for ``user_id``.

        Returns the payloads directly (not ``{scope_id, data}`` wrappers) —
        callers iterate payloads and read ``template_id`` from each.
        """
        async with self._lock:
            return [
                copy.deepcopy(p)
                for p in self._store.get(user_id, {}).values()
                if isinstance(p, dict)
            ]

    async def delete(self, *, user_id: str, template_id: str) -> bool:
        """Remove a template. Returns True if an entry was removed."""
        async with self._lock:
            bucket = self._store.get(user_id)
            if not bucket or template_id not in bucket:
                return False
            bucket.pop(template_id, None)
            if not bucket:
                self._store.pop(user_id, None)
            return True

    async def clear_user(self, *, user_id: str) -> int:
        """Drop the entire library for a user. Returns the count removed."""
        async with self._lock:
            bucket = self._store.pop(user_id, None)
            return len(bucket) if bucket else 0

    async def count_for_user(self, *, user_id: str) -> int:
        """Number of templates currently cached for ``user_id``."""
        async with self._lock:
            return len(self._store.get(user_id, {}))


# Module-level singleton — import this everywhere templates are read/written.
template_store = TemplateStore()
