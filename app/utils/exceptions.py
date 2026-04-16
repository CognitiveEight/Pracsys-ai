"""
Custom exceptions for the application.
"""


class SimplerTodayException(Exception):
    """Base exception for all application-specific exceptions."""

    def __init__(self, message: str, code: str = None, details: dict = None):
        self.message = message
        self.code = code or "GENERAL_ERROR"
        self.details = details or {}
        super().__init__(self.message)


class SocketConnectionError(SimplerTodayException):
    """Raised when socket connection fails."""

    def __init__(self, message: str = "Socket connection failed", details: dict = None):
        super().__init__(message, "SOCKET_CONNECTION_ERROR", details)


class SocketEventError(SimplerTodayException):
    """Raised when socket event handling fails."""

    def __init__(
        self, message: str = "Socket event handling failed", details: dict = None
    ):
        super().__init__(message, "SOCKET_EVENT_ERROR", details)


class WorkspaceError(SimplerTodayException):
    """Raised when workspace operations fail."""

    def __init__(
        self, message: str = "Workspace operation failed", details: dict = None
    ):
        super().__init__(message, "WORKSPACE_ERROR", details)


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when workspace is not found."""

    def __init__(self, workspace_id: str):
        super().__init__(
            f"Workspace not found: {workspace_id}",
            details={"workspace_id": workspace_id},
        )


class AgentError(SimplerTodayException):
    """Raised when agent operations fail."""

    def __init__(self, message: str = "Agent operation failed", details: dict = None):
        super().__init__(message, "AGENT_ERROR", details)


class VectorStoreError(SimplerTodayException):
    """Raised when vector store operations fail."""

    def __init__(
        self, message: str = "Vector store operation failed", details: dict = None
    ):
        super().__init__(message, "VECTORSTORE_ERROR", details)


class ConfigurationError(SimplerTodayException):
    """Raised when configuration is invalid or missing."""

    def __init__(self, message: str = "Configuration error", details: dict = None):
        super().__init__(message, "CONFIGURATION_ERROR", details)


class SecurityError(SimplerTodayException):
    """Raised when security validation fails."""

    def __init__(
        self, message: str = "Security validation failed", details: dict = None
    ):
        super().__init__(message, "SECURITY_ERROR", details)


class FileProcessingError(SimplerTodayException):
    """Raised when file processing fails."""

    def __init__(self, message: str = "File processing failed", details: dict = None):
        super().__init__(message, "FILE_PROCESSING_ERROR", details)
