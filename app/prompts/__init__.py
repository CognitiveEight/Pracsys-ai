"""Prompts and system instructions for AI agents."""

from app.prompts.applicable_acts import (
    TARGETED_INDIAN_ACTS,
    format_targeted_acts_for_prompt,
)
from app.prompts.legal_intelligence_system import (
    LEGAL_INTELLIGENCE_SYSTEM_PROMPT,
    get_legal_intelligence_system_instruction,
    get_structured_document_feature_system_instruction,
)

__all__ = [
    "LEGAL_INTELLIGENCE_SYSTEM_PROMPT",
    "TARGETED_INDIAN_ACTS",
    "format_targeted_acts_for_prompt",
    "get_legal_intelligence_system_instruction",
    "get_structured_document_feature_system_instruction",
]
