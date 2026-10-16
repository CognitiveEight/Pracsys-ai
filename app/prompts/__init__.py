"""Prompts and system instructions for AI agents."""

from app.prompts.applicable_acts import (
    TARGETED_INDIAN_ACTS,
    format_targeted_acts_for_prompt,
)
from app.prompts.drafting import (
    DOC_CLASS_CORRESPONDENCE,
    DOC_CLASS_FILING,
    DOC_CLASS_UNKNOWN,
    DRAFT_RENDER_INSTRUCTION,
    DRAFTING_SYSTEM_INSTRUCTION,
    SUGGESTED_DRAFTS_INSTRUCTION,
    TEMPLATE_KICKOFF_INSTRUCTION,
    TEMPLATE_MATCH_INSTRUCTION,
    build_draft_render_prompt,
    build_suggested_drafts_prompt,
    build_template_kickoff_prompt,
    build_template_match_prompt,
    infer_document_class,
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
    "DRAFTING_SYSTEM_INSTRUCTION",
    "SUGGESTED_DRAFTS_INSTRUCTION",
    "TEMPLATE_KICKOFF_INSTRUCTION",
    "TEMPLATE_MATCH_INSTRUCTION",
    "DRAFT_RENDER_INSTRUCTION",
    "build_suggested_drafts_prompt",
    "build_template_kickoff_prompt",
    "build_template_match_prompt",
    "build_draft_render_prompt",
    "infer_document_class",
    "DOC_CLASS_CORRESPONDENCE",
    "DOC_CLASS_FILING",
    "DOC_CLASS_UNKNOWN",
]
