"""LangGraph agent system - Role-Based Main Agent."""

from app.langgraph.role_agent import (
    RoleBasedAgent,
    role_agent,
    enforce_role_gate,
    get_role_context_for_downstream
)
from app.langgraph.state import (
    WorkspaceState,
    RoleState,
    create_initial_workspace_state,
    update_role_state,
    validate_role_state
)
from app.langgraph.nodes import (
    role_gate_node,
    role_selection_node,
    role_context_builder_node,
    role_validation_node
)
from app.langgraph.guards import (
    require_role,
    RoleGuard,
    validate_role_context,
    assert_role
)
from app.langgraph.integration import (
    workspace_role_integration,
    WorkspaceRoleIntegration
)
from app.langgraph.strategy_agent import (
    StrategyAgent,
    strategy_agent
)
from app.langgraph.strategy_models import (
    StrategyOption,
    StrategyType,
    StrategyStatus,
    StrategyState,
    UserObjective,
    StrategyValidationResult
)
from app.langgraph.intelligence_chat_agent import (
    IntelligenceChatAgent,
    intelligence_chat_agent
)
from app.langgraph.chat_models import (
    ChatIntent,
    ChatMessage,
    ChatResponse,
    ChatContext,
    ConfidenceLevel,
    SourceReference
)
from app.langgraph.chat_integration import (
    process_workspace_chat,
    create_chat_context_from_state
)

__all__ = [
    # Role-Based Main Agent
    "RoleBasedAgent",
    "role_agent",
    "enforce_role_gate",
    "get_role_context_for_downstream",
    
    # State management
    "WorkspaceState",
    "RoleState",
    "create_initial_workspace_state",
    "update_role_state",
    "validate_role_state",
    
    # Nodes
    "role_gate_node",
    "role_selection_node",
    "role_context_builder_node",
    "role_validation_node",
    
    # Guards
    "require_role",
    "RoleGuard",
    "validate_role_context",
    "assert_role",
    
    # Integration
    "workspace_role_integration",
    "WorkspaceRoleIntegration",
    
    # Strategy & Objective Agent
    "StrategyAgent",
    "strategy_agent",
    "StrategyOption",
    "StrategyType",
    "StrategyStatus",
    "StrategyState",
    "UserObjective",
    "StrategyValidationResult",
    
    # Intelligence Chat Module
    "IntelligenceChatAgent",
    "intelligence_chat_agent",
    "ChatIntent",
    "ChatMessage",
    "ChatResponse",
    "ChatContext",
    "ConfidenceLevel",
    "SourceReference",
    "process_workspace_chat",
    "create_chat_context_from_state"
]

