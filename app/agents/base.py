"""
Base agent class and interfaces.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

# NOTE: ``memory_manager`` is imported lazily inside the two methods that
# use it, not at module scope. Importing it here closed an import cycle:
#   app.vectorstore.memory -> app.workspace.context -> app.socket.schemas
#   -> app.socket.client -> app.socket.events -> app.agents.role_models
#   -> app.agents.base -> app.vectorstore.memory (still initialising)
# which made `python -m app.main` fail with
# "cannot import name 'memory_manager' from partially initialized module".
# The lazy import is the same idiom used throughout the handlers.
from app.workspace.context import WorkspaceContext


@dataclass
class AgentConfig:
    """Configuration for an agent."""

    name: str
    model: str = "gpt-4"
    temperature: float = 0.7
    max_tokens: int = 2000
    system_prompt: Optional[str] = None


@dataclass
class AgentResponse:
    """Response from an agent."""

    content: str
    metadata: Dict[str, Any]
    agent_name: str
    success: bool = True
    error: Optional[str] = None


class BaseAgent(ABC):
    """
    Abstract base class for all agents.
    """

    def __init__(self, config: AgentConfig):
        """
        Initialize agent.

        Args:
            config: Agent configuration
        """
        self.config = config
        self.name = config.name
        logger.info("Initialized agent: {}", self.name)

    @abstractmethod
    async def process(
        self, workspace_context: WorkspaceContext, message: str, **kwargs
    ) -> AgentResponse:
        """
        Process a message and return a response.

        Args:
            workspace_context: Workspace context
            message: User message
            **kwargs: Additional arguments

        Returns:
            AgentResponse
        """
        pass

    async def get_relevant_context(
        self, workspace_id: str, query: str, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Get relevant context from workspace documents.

        Args:
            workspace_id: Workspace identifier
            query: Search query
            limit: Maximum number of results

        Returns:
            List of relevant context items
        """
        from app.vectorstore.memory import memory_manager

        memory = memory_manager.get_memory(workspace_id)
        return await memory.search_relevant_context(query, limit)

    async def add_to_memory(
        self,
        workspace_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Add message to conversation memory.

        Args:
            workspace_id: Workspace identifier
            role: Message role
            content: Message content
            metadata: Optional metadata
        """
        from app.vectorstore.memory import memory_manager

        memory = memory_manager.get_memory(workspace_id)
        await memory.add_message(role, content, metadata)

    def _format_context(self, context_items: List[Dict[str, Any]]) -> str:
        """
        Format context items for inclusion in prompt.

        Args:
            context_items: List of context items

        Returns:
            Formatted context string
        """
        if not context_items:
            return "No relevant context found."

        formatted = "Relevant context from workspace documents:\n\n"

        for i, item in enumerate(context_items, 1):
            formatted += f"[{i}] {item.get('content', '')}\n"
            if metadata := item.get("metadata"):
                formatted += f"   Source: {metadata.get('filename', 'Unknown')}\n"
            formatted += "\n"

        return formatted

    def _format_conversation_history(
        self, messages: List[Dict[str, Any]], max_messages: int = 10
    ) -> str:
        """
        Format conversation history for inclusion in prompt.

        Args:
            messages: List of conversation messages
            max_messages: Maximum number of messages to include

        Returns:
            Formatted conversation history
        """
        if not messages:
            return "No previous conversation."

        recent_messages = messages[-max_messages:]
        formatted = "Recent conversation:\n\n"

        for msg in recent_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            formatted += f"{role.upper()}: {content}\n\n"

        return formatted

    def _build_system_prompt(self, **context) -> str:
        """
        Build system prompt with context.

        Args:
            **context: Context variables to include

        Returns:
            Formatted system prompt
        """
        base_prompt = self.config.system_prompt or "You are a helpful AI assistant."

        if context:
            context_str = "\n\nContext:\n"
            for key, value in context.items():
                context_str += f"- {key}: {value}\n"
            base_prompt += context_str

        return base_prompt

    async def _log_agent_activity(
        self, action: str, workspace_id: str, details: Dict[str, Any]
    ) -> None:
        """
        Log agent activity for observability.

        Args:
            action: Action performed
            workspace_id: Workspace identifier
            details: Activity details
        """
        from app.observability.audit import audit_logger

        await audit_logger.log_agent_event(
            workspace_id=workspace_id, agent_type=self.name, action=action, data=details
        )


class AgentError(Exception):
    """Base exception for agent-related errors."""

    def __init__(
        self, message: str, agent_name: str, details: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.agent_name = agent_name
        self.details = details or {}
        super().__init__(self.message)
