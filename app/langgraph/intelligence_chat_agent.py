"""
Intelligence Chat Agent - Main Orchestration
Governed, role-aware, strategy-aware chat interface.
"""

from typing import Any, Dict

from langgraph.graph import END, StateGraph
from loguru import logger

from app.langgraph.chat_models import ChatContext, ChatMessage, ChatResponse
from app.langgraph.chat_nodes import (action_dispatcher_node,
                                      chat_context_builder_node,
                                      explain_why_node,
                                      governance_validator_node,
                                      intent_classifier_node,
                                      response_formatter_node)
from app.langgraph.chat_state import ChatState


class IntelligenceChatAgent:
    """
    Intelligence Chat Agent using LangGraph.

    This is the governed orchestration layer that:
    - Reads workspace intelligence
    - Explains existing outputs
    - Asks clarification questions
    - Triggers system actions via agents
    - Never mutates state directly
    """

    def __init__(self):
        """Initialize the Intelligence Chat Agent."""
        self.graph = self._build_graph()
        self.workflow = self.graph.compile()
        logger.info("💬 Intelligence Chat Agent initialized")

    def _build_graph(self) -> StateGraph:
        """
        Build the LangGraph workflow for governed chat.

        Flow:
        1. ChatContextBuilder → Builds prompt context
        2. IntentClassifier → Classifies user intent
        3. ExplainWhy → Generates explained response
        4. ActionDispatcher → Routes to agents if needed
        5. ResponseFormatter → Formats final output
        6. GovernanceValidator → Validates compliance

        Returns:
            Compiled StateGraph
        """
        logger.info("🏗️ Building Intelligence Chat graph...")

        # Create graph
        workflow = StateGraph(ChatState)

        # Add nodes
        workflow.add_node("context_builder", chat_context_builder_node)
        workflow.add_node("intent_classifier", intent_classifier_node)
        workflow.add_node("explain_why", explain_why_node)
        workflow.add_node("action_dispatcher", action_dispatcher_node)
        workflow.add_node("response_formatter", response_formatter_node)
        workflow.add_node("governance_validator", governance_validator_node)

        # Define edges (linear flow with conditional exits)
        workflow.set_entry_point("context_builder")

        # Context Builder → Check for errors
        workflow.add_conditional_edges(
            "context_builder",
            self._should_continue_after_context,
            {"continue": "intent_classifier", "error": END},
        )

        # Intent Classifier → Explain Why
        workflow.add_conditional_edges(
            "intent_classifier",
            self._should_continue_after_intent,
            {"continue": "explain_why", "error": END},
        )

        # Explain Why → Action Dispatcher
        workflow.add_edge("explain_why", "action_dispatcher")

        # Action Dispatcher → Response Formatter
        workflow.add_edge("action_dispatcher", "response_formatter")

        # Response Formatter → Governance Validator
        workflow.add_edge("response_formatter", "governance_validator")

        # Governance Validator → END
        workflow.add_edge("governance_validator", END)

        logger.success("✅ Intelligence Chat graph built")

        return workflow

    def _should_continue_after_context(self, state: Dict[str, Any]) -> str:
        """Decide whether to continue after context building."""
        if state.get("error"):
            logger.error("❌ Context building failed - stopping workflow")
            return "error"
        return "continue"

    def _should_continue_after_intent(self, state: Dict[str, Any]) -> str:
        """Decide whether to continue after intent classification."""
        intent_classification = state.get("intent_classification", {})
        if not intent_classification.get("is_supported"):
            logger.warning("⚠️ Intent not supported - stopping workflow")
            return "error"
        return "continue"

    async def process_message(
        self,
        workspace_id: str,
        user_id: str,
        message_id: str,
        user_message: str,
        chat_context: ChatContext,
    ) -> ChatResponse:
        """
        Process a chat message through the governed workflow.

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            message_id: Unique message ID
            user_message: User's message text
            chat_context: Complete context (role, strategy, workspace, etc.)

        Returns:
            ChatResponse with full explainability

        Raises:
            ValueError: If critical inputs are missing
        """
        logger.info("💬 Processing chat message: {}", message_id)
        logger.debug("User message: {}", user_message[:100])

        # Validate chat context
        is_valid, missing_fields = chat_context.validate()
        if not is_valid:
            error_msg = f"Missing critical context: {', '.join(missing_fields)}"
            logger.error(f"❌ {error_msg}")
            raise ValueError(error_msg)

        # Build initial state
        initial_state = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "message_id": message_id,
            "user_message": user_message,
            "chat_context": chat_context.to_dict(),
        }

        try:
            # Run workflow
            logger.info("🚀 Starting chat workflow...")
            result = await self.workflow.ainvoke(initial_state)

            # Check for errors
            if result.get("error"):
                logger.error(f"❌ Workflow error: {result.get('error_message')}")
                return ChatResponse(
                    message_id=message_id,
                    response_text=result.get("error_message", "An error occurred"),
                    explanation="Workflow failed due to error",
                    confidence="UNCERTAIN",
                    role_compliant=False,
                    strategy_compliant=False,
                )

            # Build ChatResponse
            response = ChatResponse(
                message_id=message_id,
                response_text=result.get(
                    "formatted_response", result.get("response_text", "")
                ),
                explanation=result.get("explanation", ""),
                sources=[],  # Will be populated from result
                confidence=result.get("confidence", "MEDIUM"),
                detected_intent=result.get("intent", "UNKNOWN"),
                routed_to_agent=result.get("routed_to_agent"),
                role_compliant=result.get("role_compliant", True),
                strategy_compliant=result.get("strategy_compliant", True),
                suggested_actions=result.get("suggested_actions", []),
                processing_time_ms=result.get("processing_time_ms"),
                tokens_used=result.get("tokens_used"),
            )

            # Add sources
            if result.get("sources"):
                from app.langgraph.chat_models import SourceReference

                response.sources = [
                    SourceReference(**src) if isinstance(src, dict) else src
                    for src in result.get("sources", [])
                ]

            logger.success("✅ Chat message processed successfully")
            logger.info(
                f"📊 Intent: {response.detected_intent}, Confidence: {response.confidence}"
            )

            return response

        except Exception as e:
            logger.error(f"❌ Chat processing error: {e}")
            raise

    def process_message_sync(
        self,
        workspace_id: str,
        user_id: str,
        message_id: str,
        user_message: str,
        chat_context: ChatContext,
    ) -> ChatResponse:
        """
        Synchronous version of process_message.

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            message_id: Unique message ID
            user_message: User's message text
            chat_context: Complete context

        Returns:
            ChatResponse
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(
            self.process_message(
                workspace_id, user_id, message_id, user_message, chat_context
            )
        )


# Global intelligence chat agent instance
intelligence_chat_agent = IntelligenceChatAgent()
