"""Socket.IO client module."""

from app.socket.client import SocketClient
from app.socket.events import event_handler
from app.socket.schemas import AgentResponse as SocketAgentResponse
from app.socket.schemas import (ChatMessage, EvidenceFile, SocketResponse,
                                WorkspaceData)

__all__ = [
    "SocketClient",
    "event_handler",
    "SocketResponse",
    "WorkspaceData",
    "EvidenceFile",
    "ChatMessage",
    "SocketAgentResponse",
]
