"""
Memory management for conversation history and context retrieval.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from app.vectorstore.qdrant import vector_store
from app.workspace.context import context_manager


@dataclass
class MemoryItem:
    """Represents a memory item in conversation history."""

    role: str
    content: str
    timestamp: datetime
    metadata: Dict[str, Any]


class ConversationMemory:
    """
    Manages conversation memory with semantic search capabilities.
    """

    def __init__(self, workspace_id: str, max_history: int = 20):
        """
        Initialize conversation memory.

        Args:
            workspace_id: Workspace identifier
            max_history: Maximum number of messages to keep in memory
        """
        self.workspace_id = workspace_id
        self.max_history = max_history

    async def add_message(
        self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a message to conversation memory.

        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Optional metadata
        """
        context = context_manager.get_context(self.workspace_id)
        if context:
            context.add_message(role, content, metadata)
            logger.debug("Added message to memory for workspace: {}", self.workspace_id)

    def get_recent_messages(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get recent conversation messages.

        Args:
            limit: Optional limit on number of messages

        Returns:
            List of recent messages
        """
        context = context_manager.get_context(self.workspace_id)
        if context:
            if limit is None:
                limit = self.max_history
            return context.get_conversation_history(limit)
        return []

    async def search_relevant_context(
        self, query: str, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Search for relevant context from workspace documents.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of relevant context items
        """
        try:
            results = await vector_store.search(
                workspace_id=self.workspace_id, query=query, limit=limit
            )

            logger.debug(
                "Found {} relevant context items for workspace: {}",
                len(results),
                self.workspace_id,
            )

            return results

        except Exception as e:
            logger.error("Failed to search relevant context: {}", str(e))
            return []

    def get_conversation_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the conversation.

        Returns:
            Conversation summary
        """
        messages = self.get_recent_messages()

        user_messages = [m for m in messages if m["role"] == "user"]
        assistant_messages = [m for m in messages if m["role"] == "assistant"]

        return {
            "total_messages": len(messages),
            "user_messages": len(user_messages),
            "assistant_messages": len(assistant_messages),
            "oldest_message": messages[0]["timestamp"] if messages else None,
            "latest_message": messages[-1]["timestamp"] if messages else None,
        }

    def clear(self) -> None:
        """Clear conversation memory."""
        context = context_manager.get_context(self.workspace_id)
        if context:
            context.conversation_history.clear()
            logger.info(
                "Cleared conversation memory for workspace: {}", self.workspace_id
            )


class MemoryManager:
    """Manages memory instances for multiple workspaces."""

    def __init__(self):
        self._memories: Dict[str, ConversationMemory] = {}

    def get_memory(
        self, workspace_id: str, max_history: int = 20
    ) -> ConversationMemory:
        """
        Get or create conversation memory for a workspace.

        Args:
            workspace_id: Workspace identifier
            max_history: Maximum history to maintain

        Returns:
            ConversationMemory instance
        """
        if workspace_id not in self._memories:
            self._memories[workspace_id] = ConversationMemory(
                workspace_id=workspace_id, max_history=max_history
            )
            logger.debug("Created memory for workspace: {}", workspace_id)

        return self._memories[workspace_id]

    def remove_memory(self, workspace_id: str) -> None:
        """
        Remove memory for a workspace.

        Args:
            workspace_id: Workspace identifier
        """
        if workspace_id in self._memories:
            del self._memories[workspace_id]
            logger.info("Removed memory for workspace: {}", workspace_id)

    def clear_all(self) -> None:
        """Clear all memories."""
        self._memories.clear()
        logger.info("Cleared all memories")


# Global memory manager instance
memory_manager = MemoryManager()
