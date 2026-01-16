"""Utilities module."""

from app.utils.exceptions import (
    SimplerTodayException,
    SocketConnectionError,
    SocketEventError,
    WorkspaceError,
    WorkspaceNotFoundError,
    AgentError,
    VectorStoreError,
    ConfigurationError,
    SecurityError,
    FileProcessingError
)

__all__ = [
    "SimplerTodayException",
    "SocketConnectionError",
    "SocketEventError",
    "WorkspaceError",
    "WorkspaceNotFoundError",
    "AgentError",
    "VectorStoreError",
    "ConfigurationError",
    "SecurityError",
    "FileProcessingError"
]

