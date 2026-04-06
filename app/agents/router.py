"""
Agent router for determining which agent should handle a request.
"""
from typing import Optional, Dict, Any
from enum import Enum
from loguru import logger

from app.agents.base import BaseAgent, AgentResponse
from app.agents.chat_agent import create_chat_agent
from app.workspace.context import WorkspaceContext


class AgentType(Enum):
    """Available agent types."""
    
    CHAT = "chat"
    # Add more agent types as needed
    # DOCUMENT_ANALYSIS = "document_analysis"
    # LEGAL_RESEARCH = "legal_research"
    # SUMMARIZATION = "summarization"


class AgentRouter:
    """
    Routes requests to appropriate agents based on context and intent.
    """
    
    def __init__(self):
        self._agents: Dict[AgentType, BaseAgent] = {}
        self._initialize_agents()
    
    def _initialize_agents(self) -> None:
        """Initialize all available agents."""
        logger.info("Initializing agents...")
        
        # Initialize chat agent
        self._agents[AgentType.CHAT] = create_chat_agent()
        
        # TODO: Initialize other agents as they are implemented
        # self._agents[AgentType.DOCUMENT_ANALYSIS] = create_document_analysis_agent()
        # self._agents[AgentType.LEGAL_RESEARCH] = create_legal_research_agent()
        
        logger.success("Initialized {} agents", len(self._agents))
    
    async def route(
        self,
        workspace_context: WorkspaceContext,
        message: str,
        agent_type: Optional[AgentType] = None,
        **kwargs
    ) -> AgentResponse:
        """
        Route a request to the appropriate agent.
        
        Args:
            workspace_context: Workspace context
            message: User message
            agent_type: Optional explicit agent type to use
            **kwargs: Additional arguments for the agent
            
        Returns:
            AgentResponse from the selected agent
        """
        try:
            # Determine which agent to use
            if agent_type is None:
                agent_type = self._determine_agent_type(message, workspace_context)
            
            logger.info(
                "Routing request to {} agent for workspace: {}",
                agent_type.value,
                workspace_context.workspace_id
            )
            
            # Get the agent
            agent = self._agents.get(agent_type)
            
            if not agent:
                logger.error("Agent not found: {}", agent_type.value)
                return AgentResponse(
                    content="I'm sorry, but the requested agent is not available.",
                    metadata={"requested_agent": agent_type.value},
                    agent_name="router",
                    success=False,
                    error=f"Agent not found: {agent_type.value}"
                )
            
            # Process the request with the selected agent
            response = await agent.process(
                workspace_context=workspace_context,
                message=message,
                **kwargs
            )
            
            logger.debug(
                "Agent {} completed processing",
                agent_type.value
            )
            
            return response
            
        except Exception as e:
            logger.error("Error routing request: {}", str(e))
            
            return AgentResponse(
                content="I apologize, but I encountered an error processing your request.",
                metadata={"error": str(e)},
                agent_name="router",
                success=False,
                error=str(e)
            )
    
    def _determine_agent_type(
        self,
        message: str,
        workspace_context: WorkspaceContext
    ) -> AgentType:
        """
        Determine which agent should handle the request.
        
        This is a simple implementation. In production, this could use:
        - Intent classification model
        - Keyword matching
        - LLM-based routing
        - User preferences
        
        Args:
            message: User message
            workspace_context: Workspace context
            
        Returns:
            AgentType to handle the request
        """
        # Simple keyword-based routing (placeholder)
        message_lower = message.lower()
        
        # TODO: Implement more sophisticated routing logic
        # For now, default to chat agent
        
        # Example routing logic (to be expanded):
        # if any(word in message_lower for word in ["analyze", "document", "review"]):
        #     return AgentType.DOCUMENT_ANALYSIS
        # elif any(word in message_lower for word in ["research", "legal", "case"]):
        #     return AgentType.LEGAL_RESEARCH
        # elif any(word in message_lower for word in ["summarize", "summary", "brief"]):
        #     return AgentType.SUMMARIZATION
        
        # Default to chat agent
        return AgentType.CHAT
    
    def get_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        Get a specific agent by type.
        
        Args:
            agent_type: Agent type
            
        Returns:
            Agent instance or None if not found
        """
        return self._agents.get(agent_type)
    
    def list_available_agents(self) -> list[str]:
        """
        List all available agent types.
        
        Returns:
            List of agent type names
        """
        return [agent_type.value for agent_type in self._agents.keys()]


# Global agent router instance
agent_router = AgentRouter()

