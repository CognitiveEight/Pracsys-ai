"""
The model a user chose for a case, and for a single chat message.

The frontend sends the user's choice as ``aiModel`` (an id from
LLM_SELECTABLE_MODELS, e.g. ``"gemini-3.8-flash"`` or ``"gpt-4o"``):

- on ``workspace_created`` / ``workspace_sync`` / ``workspace_version_restore``
  it becomes the CASE's model. Every model call for the case — panels, chat,
  background work after a restart — runs on it. Absent means "no change";
  ``null`` or ``""`` resets the case to the default model;
- on ``user_message_request`` it overrides the model for that one turn only
  (the case keeps its model).

Rules, enforced where the router asks (:func:`selected_profile`):
- only ids in LLM_SELECTABLE_MODELS are honoured — anything else is logged and
  ignored, so the frontend can never route a case to an unvetted model;
- tasks pinned in LLM_ROUTES keep their own route, and Google-Search-grounded
  case-law search and embeddings never follow the choice (a case's documents
  are embedded once; switching chat models must not change its vector space).

Follows ``participants.py``: module-level dict (not reached by the handler's
``self.__dict__`` purge sweep, so ``_purge_workspace`` clears it explicitly),
write-through to Qdrant (``CacheKinds.MODEL_SELECTION``), hydrated on a cold
workspace.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Dict, Optional

from loguru import logger

#: workspace_id -> selected model id
_selected: Dict[str, str] = {}

#: Per-turn override from ``user_message_request.aiModel``. Set for the turn's
#: task, so the background work it spawns inherits it.
_message_model: ContextVar[Optional[str]] = ContextVar("simplertoday_message_model", default=None)


def _selectable() -> Dict[str, Any]:
    from app.llm.config import selectable_models

    return selectable_models()


def default_model_id() -> str:
    from app.llm.config import load_routing

    return load_routing().default[0].model


def _validate(model_id: Any, where: str) -> Optional[str]:
    """``model_id`` when it is selectable; None (logged) otherwise."""
    value = str(model_id or "").strip()
    if not value:
        return None
    if value not in _selectable():
        logger.warning(
            "aiModel '{}' on {} is not in LLM_SELECTABLE_MODELS; ignored", value[:60], where
        )
        return None
    return value


def set_workspace_model(workspace_id: str, model_id: Any) -> Optional[str]:
    """Record the case's model from a workspace event. Returns the model id now
    in force for the case (None = the default)."""
    if not workspace_id:
        return None
    raw = str(model_id or "").strip()
    if not raw:
        changed = _selected.pop(workspace_id, None) is not None
    else:
        value = _validate(raw, "workspace event")
        if value is None:
            return _selected.get(workspace_id)
        changed = _selected.get(workspace_id) != value
        if value == default_model_id():
            _selected.pop(workspace_id, None)
        else:
            _selected[workspace_id] = value
    if changed:
        logger.info("Case model set to {}", _selected.get(workspace_id) or default_model_id())
        _persist(workspace_id)
    return _selected.get(workspace_id)


def get_workspace_model(workspace_id: Optional[str]) -> Optional[str]:
    return _selected.get(workspace_id) if workspace_id else None


def set_message_model(model_id: Any) -> Optional[Token]:
    """Override the model for the current chat turn; returns a reset token."""
    value = _validate(model_id, "user_message_request")
    if value is None:
        return None
    return _message_model.set(value)


def reset_message_model(token: Optional[Token]) -> None:
    if token is not None:
        _message_model.reset(token)


def selected_profile(workspace_id: Optional[str] = None):
    """The profile the user chose for the running work, or None (use the
    configured route). The turn override wins over the case's model."""
    model_id = _message_model.get()
    if model_id is None:
        if workspace_id is None:
            from app.ai.usage import current_workspace_id

            workspace_id = current_workspace_id()
        model_id = get_workspace_model(workspace_id)
    if model_id is None:
        return None
    return _selectable().get(model_id)


def hydrate_from_cache(workspace_id: str, payload: Any) -> None:
    """Restore a persisted choice on a cold workspace (never overrides a newer
    in-memory one)."""
    if workspace_id in _selected or not isinstance(payload, dict):
        return
    value = _validate(payload.get("model"), "cached selection")
    if value is not None and value != default_model_id():
        _selected[workspace_id] = value


def clear_model_selection(workspace_id: str) -> None:
    """Workspace purge."""
    _selected.pop(workspace_id, None)


def _persist(workspace_id: str) -> None:
    try:
        from app.workspace.persistence import CacheKinds, persist_state

        persist_state(
            workspace_id=workspace_id,
            kind=CacheKinds.MODEL_SELECTION,
            data={"model": _selected.get(workspace_id)},
        )
    except Exception:  # persistence is best-effort; never block the caller
        pass
