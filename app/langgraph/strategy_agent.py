"""
Strategy & Objective Agent - Main LangGraph Implementation
Orchestrates strategy suggestion, validation, and locking.
"""

from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph
from loguru import logger

from app.langgraph.strategy_models import StrategyOption, StrategyState
from app.langgraph.strategy_nodes import (strategy_lock_node,
                                          strategy_suggestion_node,
                                          strategy_validation_node,
                                          user_objective_mapper_node)
from app.observability.audit import audit_logger
from app.observability.trace import tracer


class StrategyWorkflowState(TypedDict, total=False):
    """
    State schema for Strategy Agent workflow.

    This defines all possible state variables that flow through
    the strategy selection and locking process.
    """

    # Inputs (required)
    workspace_id: str
    role_context: Dict[str, Any]  # From Role-Based Agent
    workspace_snapshot: Dict[str, Any]  # Documents, facts, parties

    # Inputs (optional)
    user_objective_text: Optional[str]
    objective_priority: str
    readability_scores: Optional[Dict[str, Any]]
    selected_strategy_id: Optional[str]

    # Outputs from nodes
    suggested_strategies: list[StrategyOption]
    user_objective: Optional[Any]
    mapped_strategy_id: Optional[str]
    mapping_confidence: float
    new_strategy: Optional[StrategyOption]
    validation_result: Optional[Any]
    validated_strategy: Optional[StrategyOption]
    active_strategy: Optional[StrategyOption]

    # Control flags
    strategy_suggestion_complete: bool
    objective_mapping_complete: bool
    objective_mapping_skipped: bool
    validation_complete: bool
    strategy_locked: bool
    requires_reanalysis: bool

    # Errors
    strategy_error: Optional[str]
    objective_mapping_error: Optional[str]
    lock_error: Optional[str]

    # Downstream triggers
    downstream_trigger: Optional[Dict[str, Any]]


class StrategyAgent:
    """
    Strategy & Objective Agent

    Responsible for:
    - Generating role-aware strategy options
    - Mapping user objectives to strategies
    - Validating strategy compatibility
    - Locking active strategy and triggering reconfiguration

    This agent runs AFTER the Role-Based Agent and provides
    strategic direction for all downstream intelligence.
    """

    def __init__(self):
        self.graph: Optional[StateGraph] = None
        self.compiled_graph = None
        logger.info("Strategy Agent initialized")

    def _build_graph(self) -> StateGraph:
        """
        Build the Strategy Agent LangGraph.

        Workflow:
        1. Suggest Strategies → Generate 1-3 options based on role & facts
        2. Map Objective (optional) → Map user goal to strategy
        3. Validate Strategy → Check compatibility and constraints
        4. Lock Strategy → Activate and trigger downstream reconfiguration
        """
        logger.info("Building Strategy Agent graph...")

        # Create graph with state schema
        graph = StateGraph(StrategyWorkflowState)

        # Add nodes
        graph.add_node("suggest_strategies", strategy_suggestion_node)
        graph.add_node("map_objective", user_objective_mapper_node)
        graph.add_node("validate_strategy", strategy_validation_node)
        graph.add_node("lock_strategy", strategy_lock_node)

        # Define edges and conditional routing

        # Start → Suggest Strategies (always first)
        graph.set_entry_point("suggest_strategies")

        # Suggest Strategies → Map Objective
        graph.add_edge("suggest_strategies", "map_objective")

        # Map Objective → Validate Strategy (conditional)
        def route_after_mapping(state: Dict[str, Any]) -> str:
            """Route based on objective mapping result."""
            if state.get("objective_mapping_error"):
                # Mapping failed, end workflow
                return "end"
            elif state.get("objective_mapping_skipped"):
                # No objective provided, need user selection
                return "end"
            else:
                # Proceed to validation
                return "validate_strategy"

        graph.add_conditional_edges(
            "map_objective",
            route_after_mapping,
            {"validate_strategy": "validate_strategy", "end": END},
        )

        # Validate Strategy → Lock Strategy (conditional)
        def route_after_validation(state: Dict[str, Any]) -> str:
            """Route based on validation result."""
            validation_result = state.get("validation_result")

            if validation_result and validation_result.is_valid:
                # Valid strategy, proceed to lock
                return "lock_strategy"
            else:
                # Invalid strategy, end workflow
                return "end"

        graph.add_conditional_edges(
            "validate_strategy",
            route_after_validation,
            {"lock_strategy": "lock_strategy", "end": END},
        )

        # Lock Strategy → END (always terminal)
        graph.add_edge("lock_strategy", END)

        logger.success("Strategy Agent graph built")
        return graph

    def compile(self) -> None:
        """Compile the LangGraph for execution."""
        if not self.graph:
            self.graph = self._build_graph()

        logger.info("Compiling Strategy Agent graph...")
        self.compiled_graph = self.graph.compile()
        logger.success("Strategy Agent graph compiled")

    @tracer.trace("strategy_agent_suggest")
    def suggest_strategies(
        self,
        workspace_id: str,
        role_context: Dict[str, Any],
        workspace_snapshot: Dict[str, Any],
        readability_scores: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Suggest strategies based on role and workspace context.

        This is typically the first step - generating options for the user.

        Args:
            workspace_id: Workspace identifier
            role_context: RoleContext from Role-Based Agent (mandatory)
            workspace_snapshot: Documents, facts, parties, timelines
            readability_scores: Document quality metrics (optional)

        Returns:
            Dictionary containing suggested_strategies list

        Raises:
            ValueError: If role_context is missing
        """
        logger.info("Suggesting strategies for workspace: {}", workspace_id)

        if not self.compiled_graph:
            self.compile()

        # Create initial state
        initial_state = {
            "workspace_id": workspace_id,
            "role_context": role_context,
            "workspace_snapshot": workspace_snapshot,
            "readability_scores": readability_scores,
        }

        # Run graph (will stop after suggestions if no objective provided)
        result = self.compiled_graph.invoke(initial_state)

        logger.success(
            "Generated {} strategies", len(result.get('suggested_strategies', []))
        )

        return {
            "workspace_id": workspace_id,
            "suggested_strategies": result.get("suggested_strategies", []),
            "error": result.get("strategy_error"),
        }

    @tracer.trace("strategy_agent_select")
    def select_and_lock_strategy(
        self,
        workspace_id: str,
        role_context: Dict[str, Any],
        workspace_snapshot: Dict[str, Any],
        strategy_id: str,
        suggested_strategies: list[StrategyOption],
    ) -> Dict[str, Any]:
        """
        Validate and lock a selected strategy.

        This is used when user selects from suggested strategies.

        Args:
            workspace_id: Workspace identifier
            role_context: RoleContext from Role-Based Agent
            workspace_snapshot: Current workspace state
            strategy_id: ID of strategy to lock
            suggested_strategies: Previously suggested options

        Returns:
            Dictionary with active_strategy and downstream_trigger
        """
        logger.info("Selecting and locking strategy: {}", strategy_id)

        if not self.compiled_graph:
            self.compile()

        # Create state for validation + locking
        initial_state = {
            "workspace_id": workspace_id,
            "role_context": role_context,
            "workspace_snapshot": workspace_snapshot,
            "suggested_strategies": suggested_strategies,
            "selected_strategy_id": strategy_id,
            "objective_mapping_skipped": True,  # Skip objective mapping
        }

        # Jump to validation node
        result = self.compiled_graph.invoke(initial_state)

        if result.get("strategy_locked"):
            logger.success("Strategy locked: {}", strategy_id)
        else:
            logger.error("Failed to lock strategy: {}", result.get('lock_error'))

        return {
            "workspace_id": workspace_id,
            "active_strategy": result.get("active_strategy"),
            "validation_result": result.get("validation_result"),
            "downstream_trigger": result.get("downstream_trigger"),
            "requires_reanalysis": result.get("requires_reanalysis", False),
            "error": result.get("lock_error"),
        }

    @tracer.trace("strategy_agent_from_objective")
    def strategy_from_objective(
        self,
        workspace_id: str,
        role_context: Dict[str, Any],
        workspace_snapshot: Dict[str, Any],
        user_objective: str,
        suggested_strategies: Optional[list[StrategyOption]] = None,
    ) -> Dict[str, Any]:
        """
        Generate and lock strategy from user objective.

        This is the full workflow: suggest → map objective → validate → lock

        Args:
            workspace_id: Workspace identifier
            role_context: RoleContext from Role-Based Agent
            workspace_snapshot: Current workspace state
            user_objective: User's stated goal/objective
            suggested_strategies: Optional pre-generated strategies

        Returns:
            Dictionary with active_strategy and downstream_trigger
        """
        logger.info("Creating strategy from objective ({} chars)", len(user_objective or ""))

        if not self.compiled_graph:
            self.compile()

        # Create full workflow state
        initial_state = {
            "workspace_id": workspace_id,
            "role_context": role_context,
            "workspace_snapshot": workspace_snapshot,
            "user_objective_text": user_objective,
            "suggested_strategies": suggested_strategies or [],
        }

        # Run complete workflow
        result = self.compiled_graph.invoke(initial_state)

        if result.get("strategy_locked"):
            logger.success("Strategy created and locked from objective")
        else:
            logger.error(
                "Failed to create strategy: {}", result.get('objective_mapping_error') or result.get('lock_error')
            )

        return {
            "workspace_id": workspace_id,
            "active_strategy": result.get("active_strategy"),
            "validation_result": result.get("validation_result"),
            "downstream_trigger": result.get("downstream_trigger"),
            "requires_reanalysis": result.get("requires_reanalysis", False),
            "mapped_strategy_id": result.get("mapped_strategy_id"),
            "new_strategy": result.get("new_strategy"),
            "error": result.get("objective_mapping_error") or result.get("lock_error"),
        }

    def get_active_strategy(self, workspace_id: str) -> Optional[StrategyOption]:
        """
        Get currently active strategy for a workspace.

        Args:
            workspace_id: Workspace identifier

        Returns:
            Active StrategyOption or None
        """
        # In production, fetch from workspace state/database
        logger.info("Fetching active strategy for workspace: {}", workspace_id)
        return None

    def change_strategy(
        self,
        workspace_id: str,
        old_strategy_id: str,
        new_strategy_id: str,
        role_context: Dict[str, Any],
        workspace_snapshot: Dict[str, Any],
        suggested_strategies: list[StrategyOption],
    ) -> Dict[str, Any]:
        """
        Change from one strategy to another.

        This supersedes the old strategy and triggers full re-analysis.

        Args:
            workspace_id: Workspace identifier
            old_strategy_id: Current active strategy
            new_strategy_id: New strategy to activate
            role_context: RoleContext
            workspace_snapshot: Current state
            suggested_strategies: Available strategies

        Returns:
            Dictionary with updated active_strategy
        """
        logger.info("Changing strategy: {}  {}", old_strategy_id, new_strategy_id)

        # This uses the same select_and_lock logic
        result = self.select_and_lock_strategy(
            workspace_id=workspace_id,
            role_context=role_context,
            workspace_snapshot=workspace_snapshot,
            strategy_id=new_strategy_id,
            suggested_strategies=suggested_strategies,
        )

        if result.get("active_strategy"):
            logger.success(
                "Strategy changed successfully, downstream reconfiguration triggered"
            )

            # Log audit event
            audit_logger.log_event(
                event_type="strategy_changed",
                data={
                    "workspace_id": workspace_id,
                    "old_strategy": old_strategy_id,
                    "new_strategy": new_strategy_id,
                    "requires_reanalysis": True,
                },
                workspace_id=workspace_id,
            )

        return result


# Global strategy agent instance
strategy_agent = StrategyAgent()
