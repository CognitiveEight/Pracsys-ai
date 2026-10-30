"""In-memory cache of the Global Template Library.

Templates are pushed to this service in real time by the backend via the
inbound ``global_template_sync`` socket event (``published`` / ``retired`` /
``deleted``), and this service requests the full published set on every
(re)connect. Because the backend is the source of truth and re-syncs on
connect, we do **not** persist templates ourselves — they live only here,
in process memory, keyed by ``(owner, template_id)``.

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

Global Template Library (SIM-173)
--------------------------------
Admins publish one versioned library for the whole platform; lawyers no longer
have libraries of their own. It lives under the reserved owner
:data:`GLOBAL_OWNER`, one entry per template id holding its CURRENT version
(``scope="global"``, ``version``, ``global_status`` = published | retired).
Admin drafts being profiled live under :data:`GLOBAL_DRAFT_OWNER`, so a draft
is never suggested before it is published.

The routing lives here so the drafting agent keeps calling with the lawyer's
``user_id``:

- ``list_for_user(lawyer)`` returns the published global templates, the same
  for every lawyer.
- ``get(lawyer, id)`` reads the global library, retired templates included: a
  draft already started from a retired template keeps working. A lawyer's own
  bucket holds only a template lazily fetched for a draft that predates the
  global library (``templateUrl`` with no ``templateScope``); it is checked
  first and is never listed.
- A draft is pinned to the version it started from (:func:`version_pins`).
  An older version is kept apart from the current one, so publishing v4 never
  changes a draft started on v3.
- ``put(lawyer, ...)`` of a payload with ``scope="global"`` (a write-back of
  something ``get`` returned) goes back to the global entry for its version.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: Reserved owner for the CURRENT version of every global template.
GLOBAL_OWNER = "__global__"
#: Reserved owner for admin drafts profiled before they are published.
GLOBAL_DRAFT_OWNER = "__global_draft__"
#: ``scope`` value carried by global payloads and events.
GLOBAL_SCOPE = "global"

STATUS_PUBLISHED = "published"
STATUS_RETIRED = "retired"

_RESERVED_OWNERS = frozenset({GLOBAL_OWNER, GLOBAL_DRAFT_OWNER})

#: ``{template_id: version}`` pinned for the current drafting turn.
_version_pins: contextvars.ContextVar[Optional[Dict[str, int]]] = contextvars.ContextVar(
    "template_version_pins", default=None
)


def _as_version(value: Any) -> Optional[int]:
    """A template version as an int, or None when absent or not a number."""
    if isinstance(value, bool):
        return None
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return version if version > 0 else None


def is_global_payload(payload: Any) -> bool:
    """Whether a cached template payload belongs to the global library."""
    return isinstance(payload, dict) and payload.get("scope") == GLOBAL_SCOPE


@contextmanager
def version_pins() -> Iterator[None]:
    """Open a fresh pin table for one drafting turn (see :func:`pin_template_version`)."""
    token = _version_pins.set({})
    try:
        yield
    finally:
        _version_pins.reset(token)


def pin_template_version(template_id: Optional[str], version: Any) -> None:
    """Pin ``template_id`` to ``version`` for the rest of this drafting turn.

    Only takes effect inside :func:`version_pins`; outside it (no drafting turn)
    the current version is always read.
    """
    pins = _version_pins.get()
    tid = (template_id or "").strip()
    ver = _as_version(version)
    if pins is None or not tid or ver is None:
        return
    pins[tid] = ver


def pinned_version(template_id: Optional[str]) -> Optional[int]:
    """The version pinned for ``template_id`` in this drafting turn, if any."""
    pins = _version_pins.get()
    if not pins or not template_id:
        return None
    return pins.get(template_id.strip())


class TemplateStore:
    """Process-local ``{owner: {template_id: payload}}`` template cache.

    All public methods are ``async`` and guarded by a single lock so
    concurrent ``global_template_sync`` batches and drafting write-backs
    cannot interleave. Stored payloads are deep-copied on the way
    in and out so callers cannot mutate cached state by reference.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # Global versions that are not the current one: {(template_id, version): payload}.
        self._global_versions: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ internals

    def _global_current(self, template_id: str) -> Optional[Dict[str, Any]]:
        data = self._store.get(GLOBAL_OWNER, {}).get(template_id)
        return data if isinstance(data, dict) else None

    def _published_global(self) -> List[Dict[str, Any]]:
        return [
            p
            for p in self._store.get(GLOBAL_OWNER, {}).values()
            if isinstance(p, dict) and p.get("global_status") == STATUS_PUBLISHED
        ]

    def _listing(self, user_id: str) -> List[Dict[str, Any]]:
        if user_id not in _RESERVED_OWNERS:
            return self._published_global()
        return [p for p in self._store.get(user_id, {}).values() if isinstance(p, dict)]

    def _put_global_current(self, template_id: str, payload: Dict[str, Any]) -> None:
        """Write the current global entry, keeping the version it replaces."""
        bucket = self._store.setdefault(GLOBAL_OWNER, {})
        previous = bucket.get(template_id)
        new_version = _as_version(payload.get("version"))
        old_version = _as_version((previous or {}).get("version"))
        if previous and old_version and new_version and old_version != new_version:
            # Drafts started on the old version keep reading it.
            self._global_versions[(template_id, old_version)] = previous
        if new_version:
            self._global_versions.pop((template_id, new_version), None)
        bucket[template_id] = copy.deepcopy(payload)

    def _put_global_write_back(self, template_id: str, payload: Dict[str, Any]) -> None:
        """Route a write-back of a global payload made under a lawyer's id."""
        version = _as_version(payload.get("version"))
        current = self._global_current(template_id)
        if current is not None and _as_version(current.get("version")) == version:
            updated = copy.deepcopy(payload)
            # Status belongs to the admin feed, never to a drafting write-back.
            updated["global_status"] = current.get("global_status")
            self._store[GLOBAL_OWNER][template_id] = updated
            return
        # A version we only hold for drafting (older, retired-and-gone, or
        # lazily fetched): kept apart so it can never be suggested.
        self._global_versions[(template_id, version or 0)] = copy.deepcopy(payload)

    # ---------------------------------------------------------- public API

    async def put(
        self, *, user_id: str, template_id: str, payload: Dict[str, Any]
    ) -> bool:
        """Insert or overwrite a template payload. Returns True on success."""
        if not user_id or not template_id:
            return False
        async with self._lock:
            if user_id == GLOBAL_OWNER:
                self._put_global_current(template_id, payload)
            elif user_id not in _RESERVED_OWNERS and is_global_payload(payload):
                self._put_global_write_back(template_id, payload)
            else:
                self._store.setdefault(user_id, {})[template_id] = copy.deepcopy(
                    payload
                )
        return True

    async def get(
        self, *, user_id: str, template_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return a deep copy of the cached payload, or None if absent.

        For a lawyer's id this falls back to the global library, honouring the
        version pinned for this drafting turn (see the module docstring).
        """
        async with self._lock:
            if user_id in _RESERVED_OWNERS:
                data = self._store.get(user_id, {}).get(template_id)
                return copy.deepcopy(data) if isinstance(data, dict) else None

            pin = pinned_version(template_id)
            if pin is not None:
                current = self._global_current(template_id)
                if current is not None and _as_version(current.get("version")) == pin:
                    return copy.deepcopy(current)
                held = self._global_versions.get((template_id, pin))
                if held is not None:
                    return copy.deepcopy(held)
                # That version is not held: None makes the caller lazy-fetch it.
                own = self._store.get(user_id, {}).get(template_id)
                return copy.deepcopy(own) if isinstance(own, dict) else None

            own = self._store.get(user_id, {}).get(template_id)
            if isinstance(own, dict):
                return copy.deepcopy(own)
            current = self._global_current(template_id)
            return copy.deepcopy(current) if current is not None else None

    async def list_for_user(self, *, user_id: str) -> List[Dict[str, Any]]:
        """Return deep copies of every template ``user_id`` may be offered.

        For a lawyer that is the published global library. Returns the payloads directly
        (not ``{scope_id, data}`` wrappers) — callers iterate payloads and read
        ``template_id`` from each.
        """
        async with self._lock:
            return [copy.deepcopy(p) for p in self._listing(user_id)]

    async def delete(self, *, user_id: str, template_id: str) -> bool:
        """Remove a template from ``user_id``'s own bucket. Returns True if removed.

        Never reaches the global library through a lawyer's id; use
        :meth:`forget_global` for that.
        """
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
        """Number of templates :meth:`list_for_user` would return for ``user_id``."""
        async with self._lock:
            return len(self._listing(user_id))

    # ------------------------------------------------- global library only

    async def global_active(self) -> bool:
        """True once the global library has at least one published template."""
        async with self._lock:
            return bool(self._published_global())

    async def is_suggestable(self, *, user_id: str, template_id: str) -> bool:
        """Whether ``template_id`` is in the library ``user_id`` may be offered.

        Unlike :meth:`get`, a retired global template is NOT suggestable.
        """
        async with self._lock:
            return any(
                p.get("template_id") == template_id for p in self._listing(user_id)
            )

    async def global_ids(self) -> List[str]:
        """Ids of every current global entry, published or retired."""
        async with self._lock:
            return list(self._store.get(GLOBAL_OWNER, {}).keys())

    async def set_global_status(self, *, template_id: str, status: str) -> bool:
        """Mark the current global entry published or retired. True if it exists."""
        async with self._lock:
            current = self._global_current(template_id)
            if current is None:
                return False
            current["global_status"] = status
            return True

    async def forget_global(self, *, template_id: str) -> int:
        """Erase everything held for a global template id. Returns entries removed."""
        async with self._lock:
            removed = 0
            for owner in _RESERVED_OWNERS:
                bucket = self._store.get(owner)
                if bucket and bucket.pop(template_id, None) is not None:
                    removed += 1
            for key in [k for k in self._global_versions if k[0] == template_id]:
                self._global_versions.pop(key, None)
                removed += 1
            return removed


# Module-level singleton — import this everywhere templates are read/written.
template_store = TemplateStore()
