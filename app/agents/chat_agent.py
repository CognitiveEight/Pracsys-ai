"""
Chat agent for handling general conversation and queries.
"""


from loguru import logger

from app.agents.base import AgentConfig, AgentResponse, BaseAgent
from app.vectorstore.memory import memory_manager
from app.workspace.context import WorkspaceContext


class ChatAgent(BaseAgent):
    """
    Agent for handling general chat interactions.
    """

    def __init__(self):
        config = AgentConfig(
            name="chat_agent",
            model="gpt-4",
            temperature=0.7,
            max_tokens=12000,
            system_prompt="""You are a helpful AI assistant for a legal workspace management system.
            
Your role is to:
- Answer questions about the workspace and its documents
- Help users understand their evidence files
- Provide legal information assistance (not legal advice)
- Guide users through workspace features

Always be professional, clear, and helpful. Reference specific documents when answering questions.""",
        )
        super().__init__(config)

    async def process(
        self, workspace_context: WorkspaceContext, message: str, **kwargs
    ) -> AgentResponse:
        """
        Process a chat message.

        Args:
            workspace_context: Workspace context
            message: User message
            **kwargs: Additional arguments

        Returns:
            AgentResponse with chat response
        """
        try:
            workspace_id = workspace_context.workspace_id

            logger.info("Processing chat message for workspace: {}", workspace_id)

            # Get relevant context from documents
            relevant_context = await self.get_relevant_context(
                workspace_id=workspace_id, query=message, limit=3
            )

            # Get conversation history
            memory = memory_manager.get_memory(workspace_id)
            conversation_history = memory.get_recent_messages(limit=10)

            # Build prompt with context
            system_prompt = self._build_system_prompt(
                workspace_title=workspace_context.title,
                workspace_role=workspace_context.workspace_role,
                evidence_files_count=workspace_context.get_evidence_file_count(),
            )

            # Format context and history
            formatted_context = self._format_context(relevant_context)
            formatted_history = self._format_conversation_history(conversation_history)

            # Add user message to memory
            await self.add_to_memory(workspace_id, "user", message)

            # Generate response (placeholder - will integrate with actual LLM)
            response_content = await self._generate_response(
                system_prompt=system_prompt,
                context=formatted_context,
                history=formatted_history,
                message=message,
            )

            # Add assistant response to memory
            await self.add_to_memory(workspace_id, "assistant", response_content)

            # Log activity
            await self._log_agent_activity(
                action="chat_response",
                workspace_id=workspace_id,
                details={
                    "message_length": len(message),
                    "response_length": len(response_content),
                    "context_items": len(relevant_context),
                },
            )

            return AgentResponse(
                content=response_content,
                metadata={
                    "context_items_used": len(relevant_context),
                    "conversation_length": len(conversation_history),
                },
                agent_name=self.name,
                success=True,
            )

        except Exception as e:
            logger.error("Error processing chat message: {}", str(e))

            return AgentResponse(
                content="I apologize, but I encountered an error processing your message. Please try again.",
                metadata={"error": str(e)},
                agent_name=self.name,
                success=False,
                error=str(e),
            )

    async def _generate_response(
        self, system_prompt: str, context: str, history: str, message: str
    ) -> str:
        """
        Generate response using Google Gemini 2.0 Flash.

        Args:
            system_prompt: System prompt with role context
            context: Relevant context from documents
            history: Conversation history
            message: User message

        Returns:
            Generated response
        """
        try:
            from app.ai.gemini_client import gemini_client

            # Initialize if needed
            if not gemini_client.is_available():
                gemini_client.initialize()

            # Build comprehensive prompt
            full_prompt = f"""
{context}

{history}

User: {message}

Please respond based on the context provided and maintain the perspective defined in the system instruction.
"""

            # Generate with Gemini 2.0 Flash
            response = await gemini_client.generate(
                prompt=full_prompt,
                system_instruction=system_prompt,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            logger.debug("Generated response with Gemini 2.0 Flash")
            return response

        except Exception as e:
            logger.error("Error generating response with Gemini: {}", str(e))

            # Fallback response
            return f"""I apologize, but I encountered an error generating a response. 

Error details: {str(e)}

Please try again or contact support if the issue persists."""


# Factory function to create chat agent
def create_chat_agent() -> ChatAgent:
    """Create and return a chat agent instance."""
    return ChatAgent()
