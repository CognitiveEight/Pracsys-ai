"""
Audit logging for tracking important system events and user actions.
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger

from app.core.config import settings


class AuditLogger:
    """
    Handles audit logging for compliance and tracking.
    """

    def __init__(self):
        # The sink itself is registered by setup_logging() (logger.py). It used
        # to be added here, at import time — and setup_logging()'s
        # logger.remove() then deleted it, so audit.log silently stopped being
        # written on 2026-04-02.
        self.enabled = settings.enable_audit_log

    def log_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        severity: str = "INFO",
    ) -> None:
        """
        Log an audit event (synchronous; no I/O).

        Args:
            event_type: Type of event (e.g., "workspace_created")
            data: Event data
            user_id: Optional user identifier
            workspace_id: Optional workspace identifier
            severity: Event severity (INFO, WARNING, ERROR)
        """
        if not self.enabled:
            return

        audit_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "severity": severity,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "data": data,
        }

        # Log with audit flag
        logger.bind(audit=True).info(json.dumps(audit_entry))

    async def log_workspace_event(
        self,
        workspace_id: str,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> None:
        """
        Log a workspace-related event.

        Args:
            workspace_id: Workspace identifier
            event_type: Type of event
            data: Event data
            user_id: Optional user identifier
        """
        self.log_event(
            event_type=event_type, data=data, user_id=user_id, workspace_id=workspace_id
        )

    async def log_agent_event(
        self,
        workspace_id: str,
        agent_type: str,
        action: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> None:
        """
        Log an agent-related event.

        Args:
            workspace_id: Workspace identifier
            agent_type: Type of agent
            action: Action performed
            data: Event data
            user_id: Optional user identifier
        """
        self.log_event(
            event_type=f"agent_{action}",
            data={"agent_type": agent_type, "action": action, **data},
            user_id=user_id,
            workspace_id=workspace_id,
        )

    async def log_security_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
        severity: str = "WARNING",
    ) -> None:
        """
        Log a security-related event.

        Args:
            event_type: Type of security event
            data: Event data
            user_id: Optional user identifier
            severity: Event severity
        """
        self.log_event(
            event_type=f"security_{event_type}",
            data=data,
            user_id=user_id,
            severity=severity,
        )

    async def log_error(
        self,
        error_type: str,
        error_message: str,
        data: Dict[str, Any],
        workspace_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Log an error event.

        Args:
            error_type: Type of error
            error_message: Error message
            data: Additional error data
            workspace_id: Optional workspace identifier
            user_id: Optional user identifier
        """
        self.log_event(
            event_type=f"error_{error_type}",
            data={"error_message": error_message, **data},
            user_id=user_id,
            workspace_id=workspace_id,
            severity="ERROR",
        )


# Global audit logger instance
audit_logger = AuditLogger()
