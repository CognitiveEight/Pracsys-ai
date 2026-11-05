"""
Intelligence Chat Integration Helper
Simplifies integration with workspace and role agents.
"""

import uuid
from typing import Any, Dict, Optional

from loguru import logger

from app.langgraph.chat_models import ChatContext, ChatMessage
from app.langgraph.intelligence_chat_agent import intelligence_chat_agent
from app.workspace.custom_instructions import build_custom_instruction_suffix
from app.workspace.manager import workspace_manager


async def process_workspace_chat(
    workspace_id: str,
    user_id: str,
    user_message: str,
    active_module: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Process a chat message for a workspace.

    Automatically retrieves:
    - RoleContext from workspace
    - ActiveStrategy from workspace
    - WorkspaceState
    - Approved facts, laws, etc.

    Args:
        workspace_id: Workspace identifier
        user_id: User identifier
        user_message: User's chat message
        active_module: Currently active module (optional)

    Returns:
        Chat response as dictionary

    Raises:
        ValueError: If workspace not found or role not set
    """
    logger.info("Processing chat for workspace: {}", workspace_id)

    # Get workspace context
    workspace_ctx = await workspace_manager.get_workspace(workspace_id)
    if not workspace_ctx:
        raise ValueError(f"Workspace {workspace_id} not found")

    # Get role context (from metadata or default)
    role_context = workspace_ctx.get_metadata("role_context")
    if not role_context:
        raise ValueError(
            f"Workspace {workspace_id} has no role context. "
            "Please set role first using Role-Based Agent."
        )

    # Get active strategy (if any)
    active_strategy = workspace_ctx.get_metadata("active_strategy")

    # Get workspace state
    workspace_state = {
        "workspace_id": workspace_id,
        "documents_count": len(workspace_ctx.get_metadata("documents", [])),
        "status": "active",
    }

    # Get approved facts (from metadata or vector store)
    approved_facts = workspace_ctx.get_metadata("approved_facts", [])

    # Get applicable laws
    applicable_laws = workspace_ctx.get_metadata("applicable_laws", [])

    # Get confidence scores
    confidence_scores = workspace_ctx.get_metadata("confidence_scores", {})

    # Get chat history
    chat_history = workspace_ctx.get_metadata("chat_history", [])

    # Custom instructions (structure, tone, description) for this workspace
    custom_suffix = build_custom_instruction_suffix(workspace_id)

    # Build chat context
    chat_context = ChatContext(
        workspace_id=workspace_id,
        user_id=user_id,
        role_context=role_context,
        workspace_state=workspace_state,
        active_strategy=active_strategy,
        approved_facts=approved_facts,
        applicable_laws=applicable_laws,
        confidence_scores=confidence_scores,
        active_module=active_module,
        chat_history=chat_history,
        custom_instruction_suffix=custom_suffix or None,
    )

    # Generate message ID
    message_id = str(uuid.uuid4())

    # Process message
    response = await intelligence_chat_agent.process_message(
        workspace_id=workspace_id,
        user_id=user_id,
        message_id=message_id,
        user_message=user_message,
        chat_context=chat_context,
    )

    # Save to chat history
    chat_history.append(
        {
            "message_id": message_id,
            "user_message": user_message,
            "response": response.response_text,
            "intent": response.detected_intent,
            "timestamp": ChatMessage(
                message_id=message_id,
                workspace_id=workspace_id,
                user_id=user_id,
                user_message=user_message,
            ).timestamp.isoformat(),
        }
    )
    workspace_ctx.set_metadata("chat_history", chat_history[-10:])  # Keep last 10

    logger.success("Chat processed: {}", message_id)

    return response.to_dict()


def create_chat_context_from_state(state: Dict[str, Any]) -> ChatContext:
    """
    Create ChatContext from LangGraph state.

    Useful for integrating with other agents.

    Args:
        state: LangGraph state dict

    Returns:
        ChatContext instance
    """
    return ChatContext(
        workspace_id=state.get("workspace_id", ""),
        user_id=state.get("user_id", ""),
        role_context=state.get("role_context", {}),
        workspace_state=state.get("workspace_snapshot", {}),
        active_strategy=state.get("active_strategy"),
        approved_facts=state.get("approved_facts", []),
        applicable_laws=state.get("applicable_laws", []),
        confidence_scores=state.get("confidence_scores", {}),
        active_module=state.get("active_module"),
    )
