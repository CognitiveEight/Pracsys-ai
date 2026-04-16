"""
Role models and enums for the role-based agent system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

# STRICT ROLE ENUM - NO ADDITIONS ALLOWED
Role = Literal["ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"]


class RoleEnum(str, Enum):
    """Role enumeration for validation."""

    ADJUDICATOR = "ADJUDICATOR"
    PETITIONER = "PETITIONER"
    RESPONDENT = "RESPONDENT"
    MEDIATOR = "MEDIATOR"

    @classmethod
    def validate(cls, role: str) -> bool:
        """Validate if role is valid."""
        try:
            cls(role)
            return True
        except ValueError:
            return False

    @classmethod
    def list_roles(cls) -> List[str]:
        """Get list of all valid roles."""
        return [role.value for role in cls]


@dataclass
class RoleContext:
    """
    Role context that conditions all downstream agents.
    This is the MANDATORY output of the role agent.
    """

    role: Role
    reasoning_style: str
    framing_rules: List[str]
    forbidden_actions: List[str]
    allowed_bias: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "role": self.role,
            "reasoning_style": self.reasoning_style,
            "framing_rules": self.framing_rules,
            "forbidden_actions": self.forbidden_actions,
            "allowed_bias": self.allowed_bias,
            "timestamp": self.timestamp.isoformat(),
        }


class RoleBehaviorContract:
    """
    Defines the behavior contract for each role.
    These rules are NON-NEGOTIABLE and must be enforced by all downstream agents.
    """

    ADJUDICATOR = RoleContext(
        role="ADJUDICATOR",
        reasoning_style="neutral-judicial",
        framing_rules=[
            "100% neutral stance",
            "No advocacy for any party",
            "Use ONLY uploaded documents and attached laws",
            "No external knowledge injection",
            "Structured and balanced output",
            "Evidence-referenced reasoning",
            "Judicial reasoning style",
        ],
        forbidden_actions=[
            "Making assumptions beyond documents",
            "Advocating for any party",
            "Using external legal precedents not provided",
            "Showing favoritism",
            "Making subjective judgments",
        ],
        allowed_bias="none",
    )

    PETITIONER = RoleContext(
        role="PETITIONER",
        reasoning_style="advocacy-aligned-petitioner",
        framing_rules=[
            "Highlight petitioner strengths",
            "Emphasize rights and grievances",
            "Focus on remedies available to petitioner",
            "Frame facts to support petitioner relief",
            "Emphasize favorable interpretations",
            "Remain legally sound",
        ],
        forbidden_actions=[
            "Defending respondent position",
            "Minimizing petitioner harm",
            "Highlighting petitioner weaknesses",
            "Advocating for respondent",
            "Neutral framing (must be petitioner-favorable)",
        ],
        allowed_bias="petitioner-favorable",
    )

    RESPONDENT = RoleContext(
        role="RESPONDENT",
        reasoning_style="advocacy-aligned-respondent",
        framing_rules=[
            "Emphasize respondent defenses",
            "Highlight inconsistencies in petitioner case",
            "Identify procedural gaps",
            "Emphasize mitigating factors",
            "Protect respondent position",
            "Minimize exposure where legally valid",
        ],
        forbidden_actions=[
            "Advocating for petitioner",
            "Strengthening petitioner arguments",
            "Minimizing respondent defenses",
            "Highlighting respondent weaknesses unnecessarily",
            "Neutral framing (must be respondent-favorable)",
        ],
        allowed_bias="respondent-favorable",
    )

    MEDIATOR = RoleContext(
        role="MEDIATOR",
        reasoning_style="neutral-settlement-focused",
        framing_rules=[
            "Remain strictly neutral",
            "Identify common ground between parties",
            "Highlight disputes objectively",
            "Propose balanced settlement options",
            "Avoid adversarial framing",
            "Focus on mutual benefit",
        ],
        forbidden_actions=[
            "Advocating for either party",
            "Taking sides in disputes",
            "Adversarial language",
            "Win-lose framing",
            "Ignoring legitimate concerns of either party",
        ],
        allowed_bias="none",
    )

    @classmethod
    def get_context(cls, role: Role) -> RoleContext:
        """
        Get the role context for a given role.

        Args:
            role: The role to get context for

        Returns:
            RoleContext with behavior contract

        Raises:
            ValueError: If role is invalid
        """
        contexts = {
            "ADJUDICATOR": cls.ADJUDICATOR,
            "PETITIONER": cls.PETITIONER,
            "RESPONDENT": cls.RESPONDENT,
            "MEDIATOR": cls.MEDIATOR,
        }

        if role not in contexts:
            raise ValueError(
                f"Invalid role: {role}. Must be one of: {list(contexts.keys())}"
            )

        return contexts[role]


@dataclass
class RoleSelectionError(Exception):
    """Exception raised when role selection is missing or invalid."""

    message: str
    workspace_id: str
    attempted_operation: Optional[str] = None

    def __str__(self):
        msg = f"RoleSelectionError for workspace {self.workspace_id}: {self.message}"
        if self.attempted_operation:
            msg += f" (attempted operation: {self.attempted_operation})"
        return msg


# MANDATORY ROLE GATE MESSAGE
ROLE_GATE_MESSAGE = """Please confirm your role to begin intelligence.

Available roles:
• Adjudicator
• Petitioner
• Respondent
• Mediator"""


def create_role_gate_response() -> dict:
    """
    Create the mandatory role gate response.
    This is the ONLY allowed output when role is not selected.
    """
    return {
        "blocked": True,
        "message": ROLE_GATE_MESSAGE,
        "requires_role_selection": True,
        "available_roles": RoleEnum.list_roles(),
    }
