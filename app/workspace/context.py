"""
Workspace context management for maintaining workspace state and data.
"""
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, field
from loguru import logger

from app.socket.schemas import WorkspaceData, EvidenceFile


@dataclass
class WorkspaceContext:
    """
    Maintains the context and state for a workspace.
    """
    
    workspace_id: str
    user_id: str
    title: str
    workspace_role: str
    status: str
    evidence_files: List[EvidenceFile] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    
    @classmethod
    def from_workspace_data(cls, data: WorkspaceData) -> "WorkspaceContext":
        """
        Create WorkspaceContext from WorkspaceData schema.
        
        Args:
            data: WorkspaceData instance
            
        Returns:
            WorkspaceContext instance
        """
        return cls(
            workspace_id=data.id,  # Changed from data._id to data.id
            user_id=data.userId,
            title=data.title,
            workspace_role=data.workspaceRole,
            status=data.status,
            evidence_files=data.evidenceFiles,
            created_at=data.createdAt,
            updated_at=data.updatedAt,
            metadata={}
        )
    
    def update_from_workspace_data(self, data: WorkspaceData) -> None:
        """
        Update context from WorkspaceData.
        
        Args:
            data: WorkspaceData instance
        """
        self.title = data.title
        self.workspace_role = data.workspaceRole
        self.status = data.status
        self.evidence_files = data.evidenceFiles
        self.updated_at = data.updatedAt
        logger.debug("Updated context for workspace: {}", self.workspace_id)
    
    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a message to conversation history.
        
        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional message metadata
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {}
        }
        self.conversation_history.append(message)
        logger.debug(
            "Added {} message to workspace {}",
            role,
            self.workspace_id
        )
    
    def get_conversation_history(
        self,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get conversation history.
        
        Args:
            limit: Optional limit on number of messages to return
            
        Returns:
            List of conversation messages
        """
        if limit:
            return self.conversation_history[-limit:]
        return self.conversation_history
    
    def get_evidence_file_urls(self) -> List[str]:
        """
        Get URLs of all evidence files.
        
        Returns:
            List of evidence file URLs
        """
        return [file.url for file in self.evidence_files]
    
    def get_evidence_file_count(self) -> int:
        """
        Get count of evidence files.
        
        Returns:
            Number of evidence files
        """
        return len(self.evidence_files)
    
    def set_metadata(self, key: str, value: Any) -> None:
        """
        Set metadata value.
        
        Args:
            key: Metadata key
            value: Metadata value
        """
        self.metadata[key] = value
        logger.debug(
            "Set metadata {} for workspace {}",
            key,
            self.workspace_id
        )
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """
        Get metadata value.
        
        Args:
            key: Metadata key
            default: Default value if key not found
            
        Returns:
            Metadata value
        """
        return self.metadata.get(key, default)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert context to dictionary.
        
        Returns:
            Dictionary representation
        """
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "title": self.title,
            "workspace_role": self.workspace_role,
            "status": self.status,
            "evidence_files_count": len(self.evidence_files),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "conversation_count": len(self.conversation_history),
            "metadata": self.metadata
        }


class ContextManager:
    """Manages all workspace contexts."""
    
    def __init__(self):
        self._contexts: Dict[str, WorkspaceContext] = {}
    
    def create_context(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Create a new workspace context.
        
        Args:
            data: WorkspaceData instance
            
        Returns:
            Created WorkspaceContext
        """
        context = WorkspaceContext.from_workspace_data(data)
        self._contexts[context.workspace_id] = context
        logger.info("Created context for workspace: {}", context.workspace_id)
        return context
    
    def get_context(self, workspace_id: str) -> Optional[WorkspaceContext]:
        """
        Get workspace context.
        
        Args:
            workspace_id: Workspace identifier
            
        Returns:
            WorkspaceContext if found, None otherwise
        """
        return self._contexts.get(workspace_id)
    
    def update_context(self, data: WorkspaceData) -> WorkspaceContext:
        """
        Update existing context or create new one.
        
        Args:
            data: WorkspaceData instance
            
        Returns:
            Updated or created WorkspaceContext
        """
        context = self._contexts.get(data.id)
        
        if context:
            context.update_from_workspace_data(data)
            logger.debug("Updated context for workspace: {}", data.id)
        else:
            context = self.create_context(data)
            logger.info("Created new context for workspace: {}", data.id)
        
        return context

    def upsert_workspace(
        self,
        workspace_id: str,
        user_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        workspace_role: Optional[str] = None,
        evidence_files: Optional[List[Any]] = None,
        updated_at: Optional[datetime] = None,
    ) -> WorkspaceContext:
        """
        Create or update workspace context from sync/update payload.
        Used when backend sends workspace_sync (e.g. role change in Edit Workspace)
        so that get_workspace(workspace_id) returns the correct role for chat and strategy.
        """
        context = self._contexts.get(workspace_id)
        now = datetime.utcnow()
        if context:
            if title is not None:
                context.title = title
            if status is not None:
                context.status = status
            if workspace_role is not None:
                context.workspace_role = (workspace_role or "ADJUDICATOR").strip().upper()
                if context.workspace_role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                    context.workspace_role = "ADJUDICATOR"
            if evidence_files is not None:
                context.evidence_files = list(evidence_files)
            if updated_at is not None:
                context.updated_at = updated_at
            logger.debug("Updated context for workspace: {} (role={})", workspace_id, context.workspace_role)
            return context
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            role = "ADJUDICATOR"
        context = WorkspaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title or "",
            workspace_role=role,
            status=status or "active",
            evidence_files=list(evidence_files) if evidence_files else [],
            created_at=now,
            updated_at=updated_at or now,
        )
        self._contexts[workspace_id] = context
        logger.info("Created context for workspace: {} (role={})", workspace_id, role)
        return context
    
    def delete_context(self, workspace_id: str) -> bool:
        """
        Delete workspace context.
        
        Args:
            workspace_id: Workspace identifier
            
        Returns:
            True if deleted, False if not found
        """
        if workspace_id in self._contexts:
            del self._contexts[workspace_id]
            logger.info("Deleted context for workspace: {}", workspace_id)
            return True
        return False
    
    def has_context(self, workspace_id: str) -> bool:
        """
        Check if context exists.
        
        Args:
            workspace_id: Workspace identifier
            
        Returns:
            True if context exists
        """
        return workspace_id in self._contexts
    
    def get_all_workspace_ids(self) -> List[str]:
        """
        Get all workspace IDs.
        
        Returns:
            List of workspace IDs
        """
        return list(self._contexts.keys())
    
    def clear_all(self) -> None:
        """Clear all contexts."""
        self._contexts.clear()
        logger.info("Cleared all workspace contexts")


# Global context manager instance
context_manager = ContextManager()

