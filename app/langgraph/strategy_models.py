"""
Strategy & Objective Agent - Data Models
Defines strategy types, options, and state management.
"""
from dataclasses import dataclass, field
from typing import Literal, List, Dict, Any, Optional
from datetime import datetime
from enum import Enum

from app.agents.role_models import Role, RoleEnum


# Strategy Types (aligned with legal reasoning)
StrategyType = Literal[
    "PROSECUTION_OR_RELIEF",      # Seeking legal action/remedy
    "DEFENSIVE_DISMISSAL",        # Defending/dismissing claims
    "NEGOTIATED_SETTLEMENT",      # Seeking compromise
    "FACTUAL_CLARIFICATION",      # Clarifying facts/evidence
    "PROCEDURAL_COMPLIANCE",      # Following procedures
    "EVIDENCE_STRENGTHENING"      # Building stronger case
]


class StrategyStatus(str, Enum):
    """Strategy lifecycle status."""
    SUGGESTED = "SUGGESTED"       # AI-generated suggestion
    VALIDATED = "VALIDATED"       # Passed validation checks
    ACTIVE = "ACTIVE"            # User-confirmed and locked
    SUPERSEDED = "SUPERSEDED"    # Replaced by newer strategy
    REJECTED = "REJECTED"        # User rejected or invalid


@dataclass
class StrategyOption:
    """
    A single strategy option that can be suggested or selected.
    
    This represents a specific legal approach within the context
    of the user's role and case facts.
    """
    strategy_id: str
    strategy_type: StrategyType
    title: str
    description: str
    rationale: str
    preconditions: List[str]
    risks: List[str]
    downstream_impact: Dict[str, str]
    
    # Metadata
    relevance_score: float = 0.0  # 0.0-1.0
    compatible_roles: List[RoleEnum] = field(default_factory=list)
    requires_evidence_types: List[str] = field(default_factory=list)
    estimated_complexity: str = "MEDIUM"  # LOW, MEDIUM, HIGH
    
    # State
    status: StrategyStatus = StrategyStatus.SUGGESTED
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "preconditions": self.preconditions,
            "risks": self.risks,
            "downstream_impact": self.downstream_impact,
            "relevance_score": self.relevance_score,
            "compatible_roles": [r.value for r in self.compatible_roles],
            "requires_evidence_types": self.requires_evidence_types,
            "estimated_complexity": self.estimated_complexity,
            "status": self.status.value,
            "created_at": self.created_at.isoformat()
        }


@dataclass
class UserObjective:
    """User-defined objective/goal for the case."""
    objective_text: str
    priority: str = "HIGH"  # LOW, MEDIUM, HIGH, CRITICAL
    constraints: List[str] = field(default_factory=list)
    preferred_timeline: Optional[str] = None
    budget_considerations: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "objective_text": self.objective_text,
            "priority": self.priority,
            "constraints": self.constraints,
            "preferred_timeline": self.preferred_timeline,
            "budget_considerations": self.budget_considerations
        }


@dataclass
class StrategyValidationResult:
    """Result of strategy validation."""
    is_valid: bool
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    explanation: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "is_valid": self.is_valid,
            "conflicts": self.conflicts,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "explanation": self.explanation
        }


@dataclass
class StrategyState:
    """
    Complete strategy state for a workspace.
    
    Tracks suggested options, active strategy, and user objectives.
    """
    workspace_id: str
    
    # Current active strategy (locked)
    active_strategy: Optional[StrategyOption] = None
    
    # All suggested strategies
    suggested_strategies: List[StrategyOption] = field(default_factory=list)
    
    # User's stated objective
    user_objective: Optional[UserObjective] = None
    
    # Validation results
    last_validation: Optional[StrategyValidationResult] = None
    
    # Change tracking
    strategy_history: List[Dict[str, Any]] = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)
    requires_reanalysis: bool = False
    
    def lock_strategy(self, strategy_id: str) -> bool:
        """
        Lock a strategy as active.
        
        Args:
            strategy_id: ID of strategy to lock
            
        Returns:
            True if locked successfully
        """
        # Find the strategy
        strategy = None
        for s in self.suggested_strategies:
            if s.strategy_id == strategy_id:
                strategy = s
                break
        
        if not strategy:
            return False
        
        # Mark old strategy as superseded
        if self.active_strategy:
            self.active_strategy.status = StrategyStatus.SUPERSEDED
            self.strategy_history.append({
                "action": "SUPERSEDED",
                "strategy_id": self.active_strategy.strategy_id,
                "timestamp": datetime.utcnow().isoformat()
            })
        
        # Activate new strategy
        strategy.status = StrategyStatus.ACTIVE
        self.active_strategy = strategy
        self.last_updated = datetime.utcnow()
        self.requires_reanalysis = True
        
        self.strategy_history.append({
            "action": "ACTIVATED",
            "strategy_id": strategy_id,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        return True
    
    def add_suggestion(self, strategy: StrategyOption):
        """Add a new strategy suggestion."""
        self.suggested_strategies.append(strategy)
        self.last_updated = datetime.utcnow()
    
    def clear_suggestions(self):
        """Clear all non-active strategies."""
        if self.active_strategy:
            self.suggested_strategies = [self.active_strategy]
        else:
            self.suggested_strategies = []
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "workspace_id": self.workspace_id,
            "active_strategy": self.active_strategy.to_dict() if self.active_strategy else None,
            "suggested_strategies": [s.to_dict() for s in self.suggested_strategies],
            "user_objective": self.user_objective.to_dict() if self.user_objective else None,
            "last_validation": self.last_validation.to_dict() if self.last_validation else None,
            "strategy_history": self.strategy_history,
            "last_updated": self.last_updated.isoformat(),
            "requires_reanalysis": self.requires_reanalysis
        }


# Role-Strategy Compatibility Matrix
ROLE_STRATEGY_COMPATIBILITY: Dict[RoleEnum, List[StrategyType]] = {
    RoleEnum.PETITIONER: [
        "PROSECUTION_OR_RELIEF",
        "EVIDENCE_STRENGTHENING",
        "NEGOTIATED_SETTLEMENT",
        "PROCEDURAL_COMPLIANCE"
    ],
    RoleEnum.RESPONDENT: [
        "DEFENSIVE_DISMISSAL",
        "NEGOTIATED_SETTLEMENT",
        "EVIDENCE_STRENGTHENING",
        "FACTUAL_CLARIFICATION",
        "PROCEDURAL_COMPLIANCE"
    ],
    RoleEnum.MEDIATOR: [
        "NEGOTIATED_SETTLEMENT",
        "FACTUAL_CLARIFICATION",
        "PROCEDURAL_COMPLIANCE"
    ],
    RoleEnum.ADJUDICATOR: [
        "FACTUAL_CLARIFICATION",
        "PROCEDURAL_COMPLIANCE"
    ]
}


def is_strategy_compatible_with_role(strategy_type: StrategyType, role: RoleEnum) -> bool:
    """
    Check if a strategy type is compatible with a role.
    
    Args:
        strategy_type: Type of strategy
        role: User's role
        
    Returns:
        True if compatible
    """
    return strategy_type in ROLE_STRATEGY_COMPATIBILITY.get(role, [])


def get_incompatibility_reason(strategy_type: StrategyType, role: RoleEnum) -> str:
    """
    Get explanation for why a strategy is incompatible with a role.
    
    Args:
        strategy_type: Type of strategy
        role: User's role
        
    Returns:
        Explanation string
    """
    explanations = {
        (RoleEnum.MEDIATOR, "PROSECUTION_OR_RELIEF"): 
            "Mediators must remain neutral and cannot pursue adversarial relief",
        (RoleEnum.MEDIATOR, "DEFENSIVE_DISMISSAL"):
            "Mediators cannot take defensive positions",
        (RoleEnum.ADJUDICATOR, "PROSECUTION_OR_RELIEF"):
            "Adjudicators must remain neutral and cannot advocate for relief",
        (RoleEnum.ADJUDICATOR, "DEFENSIVE_DISMISSAL"):
            "Adjudicators cannot take defensive positions",
        (RoleEnum.ADJUDICATOR, "NEGOTIATED_SETTLEMENT"):
            "Adjudicators decide matters, they don't negotiate settlements",
        (RoleEnum.ADJUDICATOR, "EVIDENCE_STRENGTHENING"):
            "Adjudicators evaluate evidence, they don't strengthen it"
    }
    
    return explanations.get((role, strategy_type), 
                           f"Strategy {strategy_type} is not applicable for role {role.value}")

