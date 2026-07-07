"""
Intelligence Chat Module - Data Models
Role-aware, strategy-aware, governed chat interface.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

# Intent Types (aligned with routing requirements)
ChatIntent = Literal[
    "GREETING",  # Simple greetings and casual conversation
    "EXPLAIN",  # Explain existing intelligence
    "CLARIFY",  # Ask follow-up questions
    "VALIDATE",  # Route to validation loop
    "DRAFT",  # Route to Drafting Agent
    "STRATEGY_CHANGE",  # Route to Strategy Agent
    "REMOVE_INSIGHT",  # Route to validation/rejection
    "NEXT_STEPS",  # Route to Recommendation Agent
    "UNKNOWN",  # Unclassified or unsupported
]


class ChatIntentEnum(str, Enum):
    """Intent enumeration for validation."""

    GREETING = "GREETING"
    EXPLAIN = "EXPLAIN"
    CLARIFY = "CLARIFY"
    VALIDATE = "VALIDATE"
    DRAFT = "DRAFT"
    STRATEGY_CHANGE = "STRATEGY_CHANGE"
    REMOVE_INSIGHT = "REMOVE_INSIGHT"
    NEXT_STEPS = "NEXT_STEPS"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(str, Enum):
    """Confidence levels for responses."""

    HIGH = "HIGH"  # 90-100%
    MEDIUM = "MEDIUM"  # 70-89%
    LOW = "LOW"  # 50-69%
    UNCERTAIN = "UNCERTAIN"  # <50%


@dataclass
class SourceReference:
    """
    Reference to source of information.
    Ensures traceability and explainability.
    """

    source_type: str  # document, fact, law, strategy, etc.
    source_id: str
    source_name: str
    excerpt: Optional[str] = None
    page_number: Optional[int] = None
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "excerpt": self.excerpt,
            "page_number": self.page_number,
            "confidence": self.confidence,
        }


@dataclass
class ChatMessage:
    """
    Single chat message with full context.
    """

    message_id: str
    workspace_id: str
    user_id: str
    user_message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Context at time of message
    role: Optional[str] = None
    active_strategy: Optional[str] = None
    active_module: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "message_id": self.message_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "user_message": self.user_message,
            "timestamp": self.timestamp.isoformat(),
            "role": self.role,
            "active_strategy": self.active_strategy,
            "active_module": self.active_module,
        }


@dataclass
class ChatResponse:
    """
    Governed chat response with full explainability.

    MANDATORY: Every response must include explanation and sources.
    """

    message_id: str
    response_text: str
    explanation: str  # MANDATORY: Why this answer

    # Evidence and traceability
    sources: List[SourceReference] = field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM

    # Intent and routing
    detected_intent: ChatIntent = "EXPLAIN"
    routed_to_agent: Optional[str] = None

    # Governance validation
    role_compliant: bool = True
    strategy_compliant: bool = True

    # Optional next actions
    suggested_actions: List[str] = field(default_factory=list)

    # Metadata
    processing_time_ms: Optional[float] = None
    tokens_used: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "message_id": self.message_id,
            "response_text": self.response_text,
            "explanation": self.explanation,
            "sources": [s.to_dict() for s in self.sources],
            "confidence": self.confidence.value,
            "detected_intent": self.detected_intent,
            "routed_to_agent": self.routed_to_agent,
            "role_compliant": self.role_compliant,
            "strategy_compliant": self.strategy_compliant,
            "suggested_actions": self.suggested_actions,
            "processing_time_ms": self.processing_time_ms,
            "tokens_used": self.tokens_used,
        }


@dataclass
class ChatContext:
    """
    Complete context required for governed chat.

    All critical inputs for the Intelligence Chat module.
    """

    workspace_id: str
    user_id: str

    # MANDATORY inputs (must be present)
    role_context: Dict[str, Any]  # From RoleAgent
    workspace_state: Dict[str, Any]

    # OPTIONAL inputs (may be None)
    active_strategy: Optional[Dict[str, Any]] = None
    approved_facts: List[Dict[str, Any]] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    applicable_laws: List[Dict[str, Any]] = field(default_factory=list)
    case_laws: List[Dict[str, Any]] = field(default_factory=list)
    relevant_parties: List[Dict[str, Any]] = field(default_factory=list)
    timelines: List[Dict[str, Any]] = field(default_factory=list)
    templates: List[Dict[str, Any]] = field(default_factory=list)
    user_plan: Optional[Dict[str, Any]] = None
    active_module: Optional[str] = None

    # Chat history for context
    chat_history: List[Dict[str, Any]] = field(default_factory=list)

    # Optional custom instructions (structure, tone, description) - appended to prompts when set
    custom_instruction_suffix: Optional[str] = None

    def validate(self) -> tuple[bool, List[str]]:
        """
        Validate that critical inputs are present.

        Returns:
            (is_valid, list_of_missing_fields)
        """
        missing = []

        if not self.role_context:
            missing.append("role_context")

        if not self.workspace_state:
            missing.append("workspace_state")

        return (len(missing) == 0, missing)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role_context": self.role_context,
            "active_strategy": self.active_strategy,
            "workspace_state": self.workspace_state,
            "approved_facts": self.approved_facts,
            "applicable_laws": self.applicable_laws,
            "case_laws": self.case_laws,
            "relevant_parties": self.relevant_parties,
            "active_module": self.active_module,
            "chat_history_length": len(self.chat_history),
            "custom_instruction_suffix": self.custom_instruction_suffix,
            "chat_history": self.chat_history,
        }


@dataclass
class IntentClassificationResult:
    """
    Result of intent classification.
    """

    intent: ChatIntent
    confidence: float
    reasoning: str
    requires_agent: bool = False
    target_agent: Optional[str] = None

    # Validation flags
    is_supported: bool = True
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "requires_agent": self.requires_agent,
            "target_agent": self.target_agent,
            "is_supported": self.is_supported,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class GovernanceValidationResult:
    """
    Result of governance validation.
    """

    is_valid: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "is_valid": self.is_valid,
            "violations": self.violations,
            "warnings": self.warnings,
            "explanation": self.explanation,
        }


# Module-specific focus areas
MODULE_FOCUS_MAP: Dict[str, str] = {
    "strategy_objective": "strategic alignment and objective clarity",
    "analysis_report": "strengths, risks, gaps, and case analysis",
    "list_of_dates": "chronology clarification and timeline accuracy",
    "applicable_laws": "section interpretation and legal provisions",
    "drafting_assistant": "drafting requirements and template selection",
    "case_laws": "precedent explanation and citation analysis",
    "relevant_parties": "parties, their roles, and relationships in the matter",
    "supporting_material": "document quality and evidence completeness",
}


def get_module_focus(module_name: Optional[str]) -> str:
    """
    Get the focus area for a given module.

    Args:
        module_name: Name of the active module

    Returns:
        Focus description
    """
    if not module_name:
        return "general workspace intelligence"

    return MODULE_FOCUS_MAP.get(
        module_name.lower().replace(" ", "_"), "module-specific context"
    )
