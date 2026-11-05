"""
Integration with existing SimplerToday workspace system.
Bridges LangGraph role agent with workspace management.
"""

from typing import Optional

from loguru import logger

from app.utils.redact import chars

from app.agents.role_models import Role, RoleEnum
from app.langgraph.role_agent import role_agent
from app.langgraph.state import WorkspaceState, create_initial_workspace_state
from app.observability.audit import audit_logger
from app.workspace.context import WorkspaceContext


class WorkspaceRoleIntegration:
    """
    Integration layer between workspace system and role agent.
    """

    @staticmethod
    async def initialize_workspace_with_role(
        workspace_id: str, user_id: str, initial_role: Optional[Role] = None
    ) -> WorkspaceState:
        """
        Initialize workspace with optional role.

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            initial_role: Optional initial role

        Returns:
            Initialized WorkspaceState
        """
        logger.info(
            "Initializing workspace {} for user {} with role: {}",
            workspace_id,
            user_id,
            initial_role or "None",
        )

        # Create initial state
        state = create_initial_workspace_state(workspace_id, user_id)

        # If initial role provided, set it
        if initial_role:
            if not RoleEnum.validate(initial_role):
                logger.error("Invalid initial role: {}", initial_role)
                raise ValueError(f"Invalid role: {initial_role}")

            # Invoke role agent with role selection
            state = await role_agent.invoke(
                workspace_id=workspace_id,
                user_id=user_id,
                user_input=f"ROLE:{initial_role}",
                existing_state=state,
            )

            logger.success(
                "Workspace {} initialized with role {}", workspace_id, initial_role
            )
        else:
            logger.info(
                "ℹ Workspace {} initialized without role (will require selection)",
                workspace_id,
            )

        # Log audit event
        audit_logger.log_event(
            event_type="workspace_role_initialized",
            data={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "initial_role": initial_role,
            },
            workspace_id=workspace_id,
            user_id=user_id,
        )

        return state

    @staticmethod
    def sync_role_to_workspace_context(
        state: WorkspaceState, workspace_context: WorkspaceContext
    ) -> WorkspaceContext:
        """
        Sync role from LangGraph state to workspace context.

        Args:
            state: LangGraph workspace state
            workspace_context: Workspace context

        Returns:
            Updated workspace context
        """
        role = state["role_state"].get("role")
        role_context = state["role_state"].get("role_context")

        if role and role_context:
            # Store role in workspace context metadata
            workspace_context.set_metadata("role", role)
            workspace_context.set_metadata("role_context", role_context)
            workspace_context.set_metadata(
                "role_selected_at", state["role_state"].get("role_selected_at")
            )

            logger.debug(
                "Synced role {} to workspace context for workspace {}",
                role,
                workspace_context.workspace_id,
            )

        return workspace_context

    @staticmethod
    def get_role_from_workspace_context(
        workspace_context: WorkspaceContext,
    ) -> Optional[Role]:
        """
        Get role from workspace context.

        Args:
            workspace_context: Workspace context

        Returns:
            Role if set, None otherwise
        """
        return workspace_context.get_metadata("role")

    @staticmethod
    async def handle_role_change(
        workspace_id: str, user_id: str, new_role: Role, state: WorkspaceState
    ) -> WorkspaceState:
        """
        Handle role change for a workspace.

        This triggers:
        - Role validation
        - Intelligence invalidation
        - Re-analysis trigger

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            new_role: New role to set
            state: Current workspace state

        Returns:
            Updated workspace state
        """
        current_role = state["role_state"].get("role")

        logger.warning(
            "Role change requested for workspace {}: {}  {}",
            workspace_id,
            current_role,
            new_role,
        )

        # Invoke role agent with new role
        state = await role_agent.invoke(
            workspace_id=workspace_id,
            user_id=user_id,
            user_input=f"ROLE:{new_role}",
            existing_state=state,
        )

        # Log audit event
        audit_logger.log_event(
            event_type="role_changed",
            data={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "previous_role": current_role,
                "new_role": new_role,
                "requires_reanalysis": True,
            },
            workspace_id=workspace_id,
            user_id=user_id,
            severity="WARNING",
        )

        logger.success(
            "Role changed for workspace {}: {}  {}",
            workspace_id,
            current_role,
            new_role,
        )

        return state

    @staticmethod
    async def process_user_message(
        workspace_id: str, user_id: str, message: str, state: WorkspaceState
    ) -> WorkspaceState:
        """
        Process user message through role agent.

        This automatically handles:
        - Role selection if not set
        - Role validation
        - Message processing

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            message: User message
            state: Current workspace state

        Returns:
            Updated workspace state
        """
        logger.info(
            "Processing message for workspace {}: {}",
            workspace_id,
            chars(message),
        )

        # Invoke role agent
        state = await role_agent.invoke(
            workspace_id=workspace_id,
            user_id=user_id,
            user_input=message,
            existing_state=state,
        )

        return state


# Global integration instance
workspace_role_integration = WorkspaceRoleIntegration()
