"""
Socket.IO client module.

The public names below are resolved **lazily**, via PEP 562 ``__getattr__``,
rather than imported at package-import time. That is not a style preference —
eager re-exports here made the whole application unimportable.

``app/workspace/context.py`` imports ``app.socket.schemas``. Importing any
submodule executes this ``__init__``, so an eager ``from app.socket.client
import SocketClient`` pulled in the client, the event handler, and through it
``app.agents`` — which imports back into ``app.workspace`` and
``app.vectorstore.memory`` while both are still part-way through their own
imports. The result was ``ImportError: cannot import name 'memory_manager' from
partially initialized module``, and ``python -m app.main`` died at startup.

Resolving on first attribute access breaks that cycle at its root: ``import
app.socket.schemas`` now costs exactly ``app.socket.schemas``. ``from app.socket
import SocketClient`` still works for anyone who wants it, and pays for the
client only at that point.

Prefer importing the submodule directly (``from app.socket.client import
SocketClient``) in new code — it is explicit about what it costs.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # for type checkers and IDEs only; never executed at runtime
    from app.socket.client import SocketClient
    from app.socket.events import event_handler
    from app.socket.schemas import AgentResponse as SocketAgentResponse
    from app.socket.schemas import (
        ChatMessage,
        EvidenceFile,
        SocketResponse,
        WorkspaceData,
    )

__all__ = [
    "SocketClient",
    "event_handler",
    "SocketResponse",
    "WorkspaceData",
    "EvidenceFile",
    "ChatMessage",
    "SocketAgentResponse",
]

#: Public name -> (submodule, attribute in that submodule).
_LAZY_EXPORTS = {
    "SocketClient": ("app.socket.client", "SocketClient"),
    "event_handler": ("app.socket.events", "event_handler"),
    "SocketResponse": ("app.socket.schemas", "SocketResponse"),
    "WorkspaceData": ("app.socket.schemas", "WorkspaceData"),
    "EvidenceFile": ("app.socket.schemas", "EvidenceFile"),
    "ChatMessage": ("app.socket.schemas", "ChatMessage"),
    "SocketAgentResponse": ("app.socket.schemas", "AgentResponse"),
}


def __getattr__(name: str) -> Any:
    """
    Resolve a public name on first access (PEP 562).

    Args:
        name: The attribute being read off ``app.socket``.

    Returns:
        The object, imported on demand.

    Raises:
        AttributeError: For any name not in :data:`_LAZY_EXPORTS`, matching
            normal module behaviour.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache, so this runs once per name
    return value


def __dir__() -> list:
    """Keep tab-completion and ``dir()`` honest about the lazy names."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
