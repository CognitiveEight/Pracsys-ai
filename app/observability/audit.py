"""
Audit logging for tracking important system events and user actions.
"""
import json
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from loguru import logger

from app.core.config import settings


class AuditLogger:
    """
    Handles audit logging for compliance and tracking.
    """
    
    def __init__(self):
        self.enabled = settings.enable_audit_log
        self._setup_audit_log()
    
    def _setup_audit_log(self) -> None:
        """Setup dedicated audit log file."""
        if not self.enabled:
            logger.info("Audit logging is disabled")
            return
        
        # Create audit logs directory
        audit_dir = Path("logs/audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        
        # Add audit log handler
        logger.add(
            audit_dir / "audit.log",
            format="{message}",
            level="INFO",
            rotation="100 MB",
            retention="1 year",
            compression="zip",
            serialize=True,
            filter=lambda record: record["extra"].get("audit", False)
        )
        
        logger.info("Audit logging configured")
    
    def log_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        severity: str = "INFO"
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
            "data": data
        }

        # Log with audit flag
        logger.bind(audit=True).info(json.dumps(audit_entry))

    async def log_workspace_event(
        self,
        workspace_id: str,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None
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
            event_type=event_type,
            data=data,
            user_id=user_id,
            workspace_id=workspace_id
        )

    async def log_agent_event(
        self,
        workspace_id: str,
        agent_type: str,
        action: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None
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
            data={
                "agent_type": agent_type,
                "action": action,
                **data
            },
            user_id=user_id,
            workspace_id=workspace_id
        )

    async def log_security_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        user_id: Optional[str] = None,
        severity: str = "WARNING"
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
            severity=severity
        )

    async def log_error(
        self,
        error_type: str,
        error_message: str,
        data: Dict[str, Any],
        workspace_id: Optional[str] = None,
        user_id: Optional[str] = None
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
            data={
                "error_message": error_message,
                **data
            },
            user_id=user_id,
            workspace_id=workspace_id,
            severity="ERROR"
        )


# Global audit logger instance
audit_logger = AuditLogger()

