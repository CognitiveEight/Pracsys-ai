"""
LangGraph state management for workspace and role state.
"""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

from langgraph.graph import add_messages
from typing_extensions import TypedDict

from app.agents.role_models import Role, RoleContext


class RoleState(TypedDict):
    """
    Role-specific state that persists across the workspace lifecycle.
    This is workspace-level state, NOT session-level.
    """

    # Current role (None = not selected)
    role: Optional[Role]

    # Role context for downstream agents
    role_context: Optional[Dict[str, Any]]

    # When role was selected/changed
    role_selected_at: Optional[str]

    # Role change history
    role_history: List[Dict[str, str]]

    # Whether role has been validated
    role_validated: bool


class WorkspaceState(TypedDict):
    """
    Complete workspace state for LangGraph.
    Extends with role state and other necessary fields.
    """

    # Workspace identifiers
    workspace_id: str
    user_id: str

    # Role state (MANDATORY)
    role_state: RoleState

    # Conversation messages
    messages: Annotated[List[Dict[str, Any]], add_messages]

    # Current user input
    user_input: Optional[str]

    # Agent outputs
    agent_output: Optional[str]

    # Metadata
    metadata: Dict[str, Any]

    # Error state
    error: Optional[str]

    # Next action to take
    next: Optional[str]


def create_initial_workspace_state(workspace_id: str, user_id: str) -> WorkspaceState:
    """
    Create initial workspace state with empty role state.

    Args:
        workspace_id: Workspace identifier
        user_id: User identifier

    Returns:
        Initial WorkspaceState
    """
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role_state": {
            "role": None,
            "role_context": None,
            "role_selected_at": None,
            "role_history": [],
            "role_validated": False,
        },
        "messages": [],
        "user_input": None,
        "agent_output": None,
        "metadata": {},
        "error": None,
        "next": None,
    }


def update_role_state(
    state: WorkspaceState, role: Role, role_context: RoleContext
) -> WorkspaceState:
    """
    Update workspace state with new role selection.

    Args:
        state: Current workspace state
        role: Selected role
        role_context: Role context for downstream agents

    Returns:
        Updated workspace state
    """
    now = datetime.utcnow().isoformat()

    # Add to role history
    history_entry = {
        "role": role,
        "timestamp": now,
        "previous_role": state["role_state"]["role"],
    }

    # Update role state
    state["role_state"] = {
        "role": role,
        "role_context": role_context.to_dict(),
        "role_selected_at": now,
        "role_history": state["role_state"]["role_history"] + [history_entry],
        "role_validated": True,
    }

    return state


def validate_role_state(state: WorkspaceState) -> bool:
    """
    Validate that role state is properly configured.

    Args:
        state: Workspace state to validate

    Returns:
        True if role is selected and validated
    """
    role_state = state.get("role_state", {})
    return (
        role_state.get("role") is not None
        and role_state.get("role_validated", False)
        and role_state.get("role_context") is not None
    )


def invalidate_role_derived_intelligence(state: WorkspaceState) -> WorkspaceState:
    """
    Invalidate all role-derived intelligence when role changes.
    This should trigger full re-analysis.

    Args:
        state: Workspace state

    Returns:
        Updated state with invalidation markers
    """
    state["metadata"]["intelligence_invalidated"] = True
    state["metadata"]["invalidated_at"] = datetime.utcnow().isoformat()
    state["metadata"]["requires_reanalysis"] = True

    return state
