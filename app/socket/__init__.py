"""Socket.IO client module.

``SocketClient`` and ``event_handler`` are resolved lazily (PEP 562). Importing
them eagerly here meant that importing ANY module under ``app.socket`` — even a
pure-pydantic one like ``app.socket.schemas`` — first executed this file and
pulled in the entire handler stack. That closed an import cycle:

    app.workspace.context  ->  app.socket.schemas  ->  app.socket.__init__
      ->  app.socket.client  ->  app.socket.events  ->  app.agents.role_models
      ->  app.agents.__init__  ->  app.agents.base  ->  app.vectorstore.memory
      ->  app.workspace.context   (still initialising)

which made ``python -m app.main`` fail at startup with "cannot import name ...
from partially initialized module". The public API is unchanged: both
``from app.socket import SocketClient`` and ``from app.socket.client import
SocketClient`` still work, the heavy modules just load on first attribute access
instead of at package-import time.
"""

from typing import TYPE_CHECKING

from app.socket.schemas import AgentResponse as SocketAgentResponse
from app.socket.schemas import (ChatMessage, EvidenceFile, SocketResponse,
                                WorkspaceData)

if TYPE_CHECKING:  # import-time only for type checkers, never at runtime
    from app.socket.client import SocketClient
    from app.socket.events import event_handler

_LAZY_ATTRS = {
    "SocketClient": ("app.socket.client", "SocketClient"),
    "event_handler": ("app.socket.events", "event_handler"),
}

__all__ = [
    "SocketClient",
    "event_handler",
    "SocketResponse",
    "WorkspaceData",
    "EvidenceFile",
    "ChatMessage",
    "SocketAgentResponse",
]


def __getattr__(name: str):
    """Resolve the heavy socket attributes on first access (PEP 562)."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])


def __dir__():
    return sorted(__all__)
