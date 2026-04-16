"""
LangGraph nodes for role-based agent system.
These nodes enforce role selection and conditioning.
"""

from typing import Any, Dict

from loguru import logger

from app.agents.role_models import (ROLE_GATE_MESSAGE, Role,
                                    RoleBehaviorContract, RoleContext,
                                    RoleEnum, RoleSelectionError,
                                    create_role_gate_response)
from app.langgraph.state import (WorkspaceState,
                                 invalidate_role_derived_intelligence,
                                 update_role_state, validate_role_state)
from app.observability.audit import audit_logger

# ============================================================================
# NODE 1: ROLE GATE NODE (MANDATORY CHECKPOINT)
# ============================================================================


def role_gate_node(state: WorkspaceState) -> WorkspaceState:
    """
    🚨 CRITICAL ENFORCEMENT NODE 🚨

    This node is the ABSOLUTE BLOCKING GATE for all downstream agents.

    HARD RULE:
    - If role is not selected: BLOCK ALL OPERATIONS
    - Return ONLY the role selection prompt
    - DO NOT allow any analysis, drafting, or reasoning
    - DO NOT call downstream agents
    - DO NOT hallucinate content

    Args:
        state: Current workspace state

    Returns:
        Updated state with gate decision
    """
    workspace_id = state["workspace_id"]

    logger.info("🚧 Role Gate: Checking role status for workspace {}", workspace_id)

    # ABSOLUTE CHECK: Is role selected and validated?
    if not validate_role_state(state):
        logger.warning(
            "🚨 Role Gate: BLOCKING operation - No role selected for workspace {}",
            workspace_id,
        )

        # Log audit event
        audit_logger.log_event(
            event_type="role_gate_blocked",
            data={
                "workspace_id": workspace_id,
                "reason": "No role selected",
                "blocked": True,
            },
            workspace_id=workspace_id,
            severity="WARNING",
        )

        # Return ONLY the role gate message
        gate_response = create_role_gate_response()

        state["agent_output"] = ROLE_GATE_MESSAGE
        state["metadata"]["role_gate_blocked"] = True
        state["metadata"]["requires_role_selection"] = True
        state["next"] = "role_selection"  # Route to role selection

        return state

    # Role is valid - allow continuation
    role = state["role_state"]["role"]
    logger.success(
        "✅ Role Gate: PASSED for workspace {} with role {}", workspace_id, role
    )

    # Log audit event
    audit_logger.log_event(
        event_type="role_gate_passed",
        data={"workspace_id": workspace_id, "role": role, "blocked": False},
        workspace_id=workspace_id,
    )

    state["metadata"]["role_gate_blocked"] = False
    state["next"] = "role_context_builder"  # Route to context builder

    return state


# ============================================================================
# NODE 2: ROLE SELECTION NODE (USER INPUT PROCESSOR)
# ============================================================================


def role_selection_node(state: WorkspaceState) -> WorkspaceState:
    """
    Role selection node - processes explicit user role input.

    REQUIREMENTS:
    - Accept ONLY explicit role selection
    - Validate against Role enum
    - NO inference from user text
    - NO defaults
    - Write role to workspace state

    Args:
        state: Current workspace state

    Returns:
        Updated state with role selection
    """
    workspace_id = state["workspace_id"]
    user_input = state.get("user_input", "").strip().upper()

    logger.info("🎭 Role Selection: Processing for workspace {}", workspace_id)

    # Extract role from user input
    # Expected formats: "PETITIONER", "role:PETITIONER", "select PETITIONER", etc.
    role_input = None

    # Try to extract role from input
    for valid_role in RoleEnum.list_roles():
        if valid_role in user_input:
            role_input = valid_role
            break

    # Validate role
    if not role_input or not RoleEnum.validate(role_input):
        logger.error("❌ Role Selection: Invalid role input: {}", user_input)

        state["error"] = (
            f"Invalid role. Must be one of: {', '.join(RoleEnum.list_roles())}"
        )
        state[
            "agent_output"
        ] = f"""Invalid role selection.

{ROLE_GATE_MESSAGE}"""
        state["next"] = "role_selection"  # Stay in role selection

        return state

    # Role is valid - check if it's a change
    current_role = state["role_state"].get("role")
    is_role_change = current_role is not None and current_role != role_input

    if is_role_change:
        logger.warning(
            "⚠️ Role Selection: Role change detected for workspace {} ({} → {})",
            workspace_id,
            current_role,
            role_input,
        )

        # Invalidate all derived intelligence on role change
        state = invalidate_role_derived_intelligence(state)

        # Log audit event for role change
        audit_logger.log_event(
            event_type="role_changed",
            data={
                "workspace_id": workspace_id,
                "previous_role": current_role,
                "new_role": role_input,
                "requires_reanalysis": True,
            },
            workspace_id=workspace_id,
            severity="WARNING",
        )

    # Get role context from behavior contract
    role_context = RoleBehaviorContract.get_context(role_input)

    # Update workspace state with role
    state = update_role_state(state, role_input, role_context)

    logger.success(
        "✅ Role Selection: Role set to {} for workspace {}", role_input, workspace_id
    )

    # Log audit event
    audit_logger.log_event(
        event_type="role_selected",
        data={
            "workspace_id": workspace_id,
            "role": role_input,
            "is_change": is_role_change,
        },
        workspace_id=workspace_id,
    )

    state[
        "agent_output"
    ] = f"""✅ Role confirmed: {role_input}

You can now proceed with legal intelligence.

Your perspective: {role_context.reasoning_style}
"""
    state["error"] = None
    state["next"] = "role_context_builder"  # Proceed to context builder

    return state


# ============================================================================
# NODE 3: ROLE CONTEXT BUILDER NODE (DOWNSTREAM CONDITIONING)
# ============================================================================


def role_context_builder_node(state: WorkspaceState) -> WorkspaceState:
    """
    Role context builder - prepares RoleContext for ALL downstream agents.

    This node is responsible for:
    - Building comprehensive RoleContext
    - Attaching context to state
    - Ensuring all downstream agents receive proper conditioning

    Args:
        state: Current workspace state

    Returns:
        Updated state with built role context
    """
    workspace_id = state["workspace_id"]

    # SAFETY CHECK: This should never be reached without a role
    if not validate_role_state(state):
        logger.error(
            "🚨 CRITICAL: Role Context Builder reached without valid role for workspace {}",
            workspace_id,
        )
        raise RoleSelectionError(
            message="Role context builder called without valid role",
            workspace_id=workspace_id,
            attempted_operation="role_context_builder",
        )

    role = state["role_state"]["role"]
    role_context_dict = state["role_state"]["role_context"]

    logger.info(
        "🏗️ Role Context Builder: Building context for role {} in workspace {}",
        role,
        workspace_id,
    )

    # The role context is already in the state, but we ensure it's properly formatted
    # and add any additional metadata needed for downstream agents

    state["metadata"]["role_context_active"] = True
    state["metadata"]["conditioning_applied"] = True
    state["metadata"]["downstream_agents_allowed"] = True

    logger.success(
        "✅ Role Context Builder: Context built for {} role in workspace {}",
        role,
        workspace_id,
    )

    # Log audit event
    audit_logger.log_event(
        event_type="role_context_built",
        data={
            "workspace_id": workspace_id,
            "role": role,
            "reasoning_style": role_context_dict["reasoning_style"],
            "framing_rules_count": len(role_context_dict["framing_rules"]),
        },
        workspace_id=workspace_id,
    )

    state["next"] = "downstream"  # Ready for downstream agents

    return state


# ============================================================================
# NODE 4: ROLE VALIDATION NODE (RUNTIME CONSISTENCY CHECK)
# ============================================================================


def role_validation_node(state: WorkspaceState) -> WorkspaceState:
    """
    Runtime role validation - ensures role consistency throughout execution.

    This node performs runtime checks to ensure:
    - Role exists
    - Role matches workspace state
    - No inconsistencies

    Args:
        state: Current workspace state

    Returns:
        Updated state with validation result

    Raises:
        RoleSelectionError: If validation fails
    """
    workspace_id = state["workspace_id"]

    logger.debug(
        "🔍 Role Validation: Checking consistency for workspace {}", workspace_id
    )

    # Check 1: Role state exists
    if "role_state" not in state or not state["role_state"]:
        logger.error(
            "🚨 Role Validation: FAILED - No role state in workspace {}", workspace_id
        )
        raise RoleSelectionError(
            message="Role state missing from workspace",
            workspace_id=workspace_id,
            attempted_operation="role_validation",
        )

    # Check 2: Role is selected
    role = state["role_state"].get("role")
    if not role:
        logger.error(
            "🚨 Role Validation: FAILED - No role selected in workspace {}",
            workspace_id,
        )
        raise RoleSelectionError(
            message="No role selected",
            workspace_id=workspace_id,
            attempted_operation="role_validation",
        )

    # Check 3: Role is valid
    if not RoleEnum.validate(role):
        logger.error(
            "🚨 Role Validation: FAILED - Invalid role {} in workspace {}",
            role,
            workspace_id,
        )
        raise RoleSelectionError(
            message=f"Invalid role: {role}",
            workspace_id=workspace_id,
            attempted_operation="role_validation",
        )

    # Check 4: Role context exists
    if not state["role_state"].get("role_context"):
        logger.error(
            "🚨 Role Validation: FAILED - No role context in workspace {}", workspace_id
        )
        raise RoleSelectionError(
            message="Role context missing",
            workspace_id=workspace_id,
            attempted_operation="role_validation",
        )

    # Check 5: Role is validated
    if not state["role_state"].get("role_validated"):
        logger.error(
            "🚨 Role Validation: FAILED - Role not validated in workspace {}",
            workspace_id,
        )
        raise RoleSelectionError(
            message="Role not validated",
            workspace_id=workspace_id,
            attempted_operation="role_validation",
        )

    logger.success(
        "✅ Role Validation: PASSED for workspace {} with role {}", workspace_id, role
    )

    state["metadata"]["role_validation_passed"] = True

    return state
