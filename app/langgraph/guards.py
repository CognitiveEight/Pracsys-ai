"""
Validation guards and runtime checks for role-based system.
These enforce role consistency throughout the application.
"""

import functools
from typing import Callable

from loguru import logger

from app.agents.role_models import Role, RoleEnum, RoleSelectionError
from app.langgraph.state import WorkspaceState, validate_role_state
from app.observability.audit import audit_logger


def require_role(operation_name: str):
    """
    Decorator to enforce role selection for agent methods.

    This decorator ensures that NO agent method can execute without
    a validated role in the workspace state.

    Usage:
        @require_role("legal_analysis")
        async def analyze_case(self, state: WorkspaceState):
            # This will only run if role is selected
            pass

    Args:
        operation_name: Name of the operation (for logging)
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(self, state: WorkspaceState, *args, **kwargs):
            workspace_id = state.get("workspace_id", "unknown")

            # CRITICAL CHECK: Role must be selected
            if not validate_role_state(state):
                logger.error(
                    "GUARD BLOCKED: {} attempted without role selection in workspace {}",
                    operation_name,
                    workspace_id,
                )

                # Log audit event
                audit_logger.log_event(
                    event_type="guard_blocked",
                    data={
                        "workspace_id": workspace_id,
                        "operation": operation_name,
                        "reason": "No role selected",
                        "agent": self.__class__.__name__,
                    },
                    workspace_id=workspace_id,
                    severity="ERROR",
                )

                raise RoleSelectionError(
                    message=f"Role selection required before {operation_name}",
                    workspace_id=workspace_id,
                    attempted_operation=operation_name,
                )

            # Log successful guard pass
            role = state["role_state"]["role"]
            logger.debug(
                "GUARD PASSED: {} allowed for role {} in workspace {}",
                operation_name,
                role,
                workspace_id,
            )

            # Execute the function
            return await func(self, state, *args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(self, state: WorkspaceState, *args, **kwargs):
            workspace_id = state.get("workspace_id", "unknown")

            # CRITICAL CHECK: Role must be selected
            if not validate_role_state(state):
                logger.error(
                    "GUARD BLOCKED: {} attempted without role selection in workspace {}",
                    operation_name,
                    workspace_id,
                )

                raise RoleSelectionError(
                    message=f"Role selection required before {operation_name}",
                    workspace_id=workspace_id,
                    attempted_operation=operation_name,
                )

            # Log successful guard pass
            role = state["role_state"]["role"]
            logger.debug(
                "GUARD PASSED: {} allowed for role {} in workspace {}",
                operation_name,
                role,
                workspace_id,
            )

            # Execute the function
            return func(self, state, *args, **kwargs)

        # Return appropriate wrapper
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def validate_role_context(state: WorkspaceState, operation: str = "operation"):
    """
    Validate that role context is present and consistent.

    Args:
        state: Workspace state to validate
        operation: Operation name for error messages

    Raises:
        RoleSelectionError: If validation fails
    """
    workspace_id = state.get("workspace_id", "unknown")

    # Check 1: Role state exists
    if "role_state" not in state:
        raise RoleSelectionError(
            message="Role state missing from workspace",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )

    # Check 2: Role is selected
    role = state["role_state"].get("role")
    if not role:
        raise RoleSelectionError(
            message="No role selected",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )

    # Check 3: Role is valid
    if not RoleEnum.validate(role):
        raise RoleSelectionError(
            message=f"Invalid role: {role}",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )

    # Check 4: Role context exists
    if not state["role_state"].get("role_context"):
        raise RoleSelectionError(
            message="Role context missing",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )

    # Check 5: Role is validated
    if not state["role_state"].get("role_validated"):
        raise RoleSelectionError(
            message="Role not validated",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )

    logger.debug("Role context validation passed for workspace {}", workspace_id)


class RoleGuard:
    """
    Runtime role guard that can be used as context manager or explicit check.
    """

    def __init__(self, state: WorkspaceState, operation: str):
        """
        Initialize role guard.

        Args:
            state: Workspace state
            operation: Operation name
        """
        self.state = state
        self.operation = operation
        self.workspace_id = state.get("workspace_id", "unknown")

    def __enter__(self):
        """Enter context - validate role."""
        validate_role_context(self.state, self.operation)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context."""
        return False

    async def __aenter__(self):
        """Async enter context - validate role."""
        validate_role_context(self.state, self.operation)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async exit context."""
        return False

    def check(self) -> bool:
        """
        Perform explicit check.

        Returns:
            True if role is valid

        Raises:
            RoleSelectionError: If validation fails
        """
        validate_role_context(self.state, self.operation)
        return True


def assert_role(state: WorkspaceState, expected_role: Role, operation: str):
    """
    Assert that the current role matches expected role.

    Useful for operations that should only run for specific roles.

    Args:
        state: Workspace state
        expected_role: Expected role
        operation: Operation name

    Raises:
        RoleSelectionError: If role doesn't match
    """
    validate_role_context(state, operation)

    current_role = state["role_state"]["role"]

    if current_role != expected_role:
        workspace_id = state.get("workspace_id", "unknown")

        logger.error(
            "Role assertion failed: expected {} but got {} for {} in workspace {}",
            expected_role,
            current_role,
            operation,
            workspace_id,
        )

        raise RoleSelectionError(
            message=f"Operation {operation} requires role {expected_role}, but current role is {current_role}",
            workspace_id=workspace_id,
            attempted_operation=operation,
        )


def check_role_consistency(state: WorkspaceState) -> bool:
    """
    Check if role state is internally consistent.

    Args:
        state: Workspace state

    Returns:
        True if consistent, False otherwise
    """
    try:
        validate_role_context(state, "consistency_check")
        return True
    except RoleSelectionError:
        return False


async def log_role_access(
    state: WorkspaceState, agent_name: str, operation: str, success: bool = True
):
    """
    Log role-based access for audit trail.

    Args:
        state: Workspace state
        agent_name: Name of the agent
        operation: Operation performed
        success: Whether operation succeeded
    """
    workspace_id = state.get("workspace_id", "unknown")
    role = state["role_state"].get("role", "unknown")

    audit_logger.log_event(
        event_type="role_based_access",
        data={
            "workspace_id": workspace_id,
            "agent": agent_name,
            "operation": operation,
            "role": role,
            "success": success,
        },
        workspace_id=workspace_id,
    )
