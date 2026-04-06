"""
Intelligence Chat Module - State Management
TypedDict for LangGraph state tracking.
"""
from typing import TypedDict, List, Dict, Any, Optional


class ChatState(TypedDict, total=False):
    """
    State for Intelligence Chat workflow.
    
    Tracks the complete flow through all nodes.
    """
    # Input
    workspace_id: str
    user_id: str
    message_id: str
    user_message: str
    chat_context: Dict[str, Any]  # Complete ChatContext
    
    # Context Building
    prompt_context: str
    role: str
    context_built: bool
    
    # Intent Classification
    intent: str
    intent_classification: Dict[str, Any]
    intent_classified: bool
    
    # Explanation Generation
    response_text: str
    explanation: str
    sources: List[Dict[str, Any]]
    confidence: str
    suggested_actions: List[str]
    processing_time_ms: float
    explanation_complete: bool
    
    # Action Dispatching
    routed_to_agent: Optional[str]
    dispatch_message: Optional[str]
    action_dispatched: bool
    
    # Response Formatting
    formatted_response: str
    response_complete: bool
    
    # Governance Validation
    governance_validation: Dict[str, Any]
    role_compliant: bool
    strategy_compliant: bool
    governance_validated: bool
    
    # Error Handling
    error: Optional[str]
    error_message: Optional[str]
    
    # Metadata
    tokens_used: Optional[int]

