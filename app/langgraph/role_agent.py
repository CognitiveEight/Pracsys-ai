"""
Main Role-Based Agent using LangGraph.

This is the ROOT/ENTRY agent for the entire SimplerToday agentic system.
It enforces mandatory role selection and conditions all downstream agents.
"""
from typing import Dict, Any, Literal
from langgraph.graph import StateGraph, END
from loguru import logger

from app.langgraph.state import WorkspaceState, create_initial_workspace_state, validate_role_state
from app.langgraph.nodes import (
    role_gate_node,
    role_selection_node,
    role_context_builder_node,
    role_validation_node
)
from app.agents.role_models import RoleSelectionError
from app.observability.audit import audit_logger
from app.observability.trace import tracer


class RoleBasedAgent:
    """
    Main Role-Based Agent - Root agent for the SimplerToday system.
    
    This agent:
    - Enforces mandatory role selection
    - Persists role as workspace-level state
    - Conditions all downstream agents
    - Guarantees role-consistent reasoning
    
    Architecture:
    - This is a GOVERNANCE agent, not a legal analysis agent
    - All downstream agents MUST depend on this agent's output
    - No agent can run without validated role context
    """
    
    def __init__(self):
        """Initialize the role-based agent with LangGraph."""
        self.graph = self._build_graph()
        self.compiled_graph = None
        logger.info("🎭 Role-Based Agent initialized")
    
    def _build_graph(self) -> StateGraph:
        """
        Build the LangGraph state graph.
        
        Graph Structure:
        ┌─────────────────┐
        │   START         │
        └────────┬────────┘
                 │
                 ▼
        ┌─────────────────┐
        │  Role Gate      │ ◄─── BLOCKING CHECKPOINT
        └────────┬────────┘
                 │
         ┌───────┴────────┐
         │                │
    (blocked)        (passed)
         │                │
         ▼                ▼
    ┌─────────────┐  ┌──────────────┐
    │ Role        │  │ Role Context │
    │ Selection   │  │ Builder      │
    └─────┬───────┘  └──────┬───────┘
          │                 │
          └────────┬────────┘
                   │
                   ▼
            ┌──────────────┐
            │ Validation   │
            └──────┬───────┘
                   │
                   ▼
            ┌──────────────┐
            │ Downstream   │
            │ Agents       │
            └──────────────┘
        
        Returns:
            Configured StateGraph
        """
        logger.info("🏗️ Building Role-Based Agent graph...")
        
        # Create graph
        workflow = StateGraph(WorkspaceState)
        
        # Add nodes
        workflow.add_node("role_gate", role_gate_node)
        workflow.add_node("role_selection", role_selection_node)
        workflow.add_node("role_context_builder", role_context_builder_node)
        workflow.add_node("role_validation", role_validation_node)
        
        # Set entry point
        workflow.set_entry_point("role_gate")
        
        # Add conditional edges from role_gate
        workflow.add_conditional_edges(
            "role_gate",
            self._route_from_role_gate,
            {
                "role_selection": "role_selection",
                "role_context_builder": "role_context_builder",
                "end": END
            }
        )
        
        # Add edge from role_selection to role_context_builder
        workflow.add_edge("role_selection", "role_context_builder")
        
        # Add edge from role_context_builder to validation
        workflow.add_edge("role_context_builder", "role_validation")
        
        # Add edge from validation to end (ready for downstream)
        workflow.add_edge("role_validation", END)
        
        logger.success("✅ Role-Based Agent graph built")
        return workflow
    
    def _route_from_role_gate(
        self,
        state: WorkspaceState
    ) -> Literal["role_selection", "role_context_builder", "end"]:
        """
        Routing function for role gate node.
        
        Determines next node based on role gate decision:
        - If blocked → role_selection
        - If passed → role_context_builder
        - If error → end
        
        Args:
            state: Current workspace state
            
        Returns:
            Next node name
        """
        # Check if role gate blocked
        if state["metadata"].get("role_gate_blocked"):
            logger.debug("🚧 Routing: role_gate → role_selection (blocked)")
            return "role_selection"
        
        # Check if role is validated
        if validate_role_state(state):
            logger.debug("✅ Routing: role_gate → role_context_builder (passed)")
            return "role_context_builder"
        
        # Error case
        logger.error("❌ Routing: role_gate → end (error)")
        return "end"
    
    @tracer.trace("role_agent_compile")
    def compile(self) -> "RoleBasedAgent":
        """
        Compile the LangGraph.
        
        Returns:
            Self for chaining
        """
        logger.info("⚙️ Compiling Role-Based Agent graph...")
        self.compiled_graph = self.graph.compile()
        logger.success("✅ Role-Based Agent graph compiled")
        return self
    
    @tracer.trace("role_agent_invoke")
    async def invoke(
        self,
        workspace_id: str,
        user_id: str,
        user_input: str,
        existing_state: Dict[str, Any] = None
    ) -> WorkspaceState:
        """
        Invoke the role-based agent.
        
        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            user_input: User input message
            existing_state: Existing workspace state (if any)
            
        Returns:
            Updated WorkspaceState
            
        Raises:
            RoleSelectionError: If role enforcement fails
        """
        logger.info(
            "🚀 Invoking Role-Based Agent for workspace {} with input: {}",
            workspace_id,
            user_input[:50] + "..." if len(user_input) > 50 else user_input
        )
        
        # Ensure graph is compiled
        if not self.compiled_graph:
            self.compile()
        
        # Create or use existing state
        if existing_state:
            state = existing_state
            state["user_input"] = user_input
        else:
            state = create_initial_workspace_state(workspace_id, user_id)
            state["user_input"] = user_input
        
        try:
            # Run the graph
            result = await self.compiled_graph.ainvoke(state)
            
            logger.success(
                "✅ Role-Based Agent completed for workspace {}",
                workspace_id
            )
            
            # Log audit event
            audit_logger.log_event(
                event_type="role_agent_invoked",
                data={
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "role": result["role_state"].get("role"),
                    "success": True
                },
                workspace_id=workspace_id,
                user_id=user_id
            )
            
            return result
            
        except RoleSelectionError as e:
            logger.error("❌ Role enforcement error: {}", str(e))
            
            # Log audit event
            await audit_logger.log_error(
                error_type="role_enforcement",
                error_message=str(e),
                data={
                    "workspace_id": workspace_id,
                    "attempted_operation": e.attempted_operation
                },
                workspace_id=workspace_id
            )
            
            raise
        
        except Exception as e:
            logger.error("❌ Role-Based Agent error: {}", str(e))
            
            # Log audit event
            await audit_logger.log_error(
                error_type="role_agent",
                error_message=str(e),
                data={
                    "workspace_id": workspace_id,
                    "user_id": user_id
                },
                workspace_id=workspace_id,
                user_id=user_id
            )
            
            raise
    
    @tracer.trace("role_agent_get_role_context")
    def get_role_context(self, state: WorkspaceState) -> Dict[str, Any]:
        """
        Extract role context from state for downstream agents.
        
        This method should be called by all downstream agents to get
        the role context that conditions their behavior.
        
        Args:
            state: Current workspace state
            
        Returns:
            Role context dictionary
            
        Raises:
            RoleSelectionError: If role is not selected
        """
        if not validate_role_state(state):
            raise RoleSelectionError(
                message="Cannot get role context - role not selected",
                workspace_id=state.get("workspace_id", "unknown"),
                attempted_operation="get_role_context"
            )
        
        return state["role_state"]["role_context"]
    
    def requires_role_selection(self, state: WorkspaceState) -> bool:
        """
        Check if role selection is required.
        
        Args:
            state: Current workspace state
            
        Returns:
            True if role selection is required
        """
        return not validate_role_state(state)
    
    def get_current_role(self, state: WorkspaceState) -> str:
        """
        Get current role from state.
        
        Args:
            state: Current workspace state
            
        Returns:
            Current role or None
        """
        return state["role_state"].get("role")
    
    def visualize_graph(self, output_path: str = "role_agent_graph.png"):
        """
        Visualize the LangGraph structure.
        
        Args:
            output_path: Path to save visualization
        """
        try:
            from langchain_core.runnables.graph import MermaidDrawMethod
            
            if not self.compiled_graph:
                self.compile()
            
            # Generate mermaid diagram
            mermaid_png = self.compiled_graph.get_graph().draw_mermaid_png(
                draw_method=MermaidDrawMethod.API
            )
            
            with open(output_path, "wb") as f:
                f.write(mermaid_png)
            
            logger.success("✅ Graph visualization saved to {}", output_path)
            
        except Exception as e:
            logger.error("❌ Failed to visualize graph: {}", str(e))


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

# Create global role agent instance
role_agent = RoleBasedAgent()


# ============================================================================
# HELPER FUNCTIONS FOR DOWNSTREAM AGENTS
# ============================================================================

async def enforce_role_gate(
    workspace_id: str,
    user_id: str,
    operation: str,
    state: WorkspaceState
) -> bool:
    """
    Enforce role gate for downstream operations.
    
    This function should be called by ALL downstream agents before
    performing any analysis, drafting, or reasoning.
    
    Args:
        workspace_id: Workspace identifier
        user_id: User identifier
        operation: Operation name (for logging)
        state: Current workspace state
        
    Returns:
        True if operation is allowed
        
    Raises:
        RoleSelectionError: If role is not selected
    """
    if not validate_role_state(state):
        logger.error(
            "🚨 Role Gate: Blocking {} operation for workspace {} - No role selected",
            operation,
            workspace_id
        )
        
        # Log audit event
        audit_logger.log_event(
            event_type="downstream_blocked",
            data={
                "workspace_id": workspace_id,
                "user_id": user_id,
                "operation": operation,
                "reason": "No role selected"
            },
            workspace_id=workspace_id,
            user_id=user_id,
            severity="ERROR"
        )
        
        raise RoleSelectionError(
            message=f"Role selection required before {operation}",
            workspace_id=workspace_id,
            attempted_operation=operation
        )
    
    logger.debug(
        "✅ Role Gate: Allowing {} operation for workspace {}",
        operation,
        workspace_id
    )
    
    return True


def get_role_context_for_downstream(state: WorkspaceState) -> Dict[str, Any]:
    """
    Get role context for downstream agents.
    
    ALL downstream agents must call this function to get the role context
    that conditions their behavior.
    
    Args:
        state: Current workspace state
        
    Returns:
        Role context dictionary
        
    Raises:
        RoleSelectionError: If role is not selected
    """
    return role_agent.get_role_context(state)

