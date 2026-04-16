"""
Per-workspace custom instructions (structure, tone, description).
Used to append user preferences to AI prompts when set; otherwise default prompt only.
"""

from typing import Any, Dict, Optional

# workspace_id -> { "structure": str, "tone": str, "description": str }
_custom_instructions: Dict[str, Dict[str, str]] = {}

STRUCTURE_OPTIONS = ("default", "bullet points", "paragraphs")
TONE_OPTIONS = ("default(empathetic)", "formal", "assertive")


def set_custom_instructions(
    workspace_id: str,
    structure: Optional[str] = None,
    tone: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Store custom instructions for a workspace. None values mean 'not set' / use default."""
    if workspace_id not in _custom_instructions:
        _custom_instructions[workspace_id] = {}
    if structure is not None:
        _custom_instructions[workspace_id]["structure"] = (structure or "").strip()
    if tone is not None:
        _custom_instructions[workspace_id]["tone"] = (tone or "").strip()
    if description is not None:
        _custom_instructions[workspace_id]["description"] = (description or "").strip()


def get_custom_instructions(workspace_id: str) -> Dict[str, str]:
    """Get custom instructions for a workspace. Returns empty dict if none set."""
    return dict(_custom_instructions.get(workspace_id, {}))


def build_custom_instruction_suffix(workspace_id: str) -> str:
    """
    Build the text to append to prompts when custom instructions are set.
    Returns empty string if no custom instructions or all are default/empty.
    """
    prefs = get_custom_instructions(workspace_id)
    if not prefs:
        return ""
    parts = []
    structure = (prefs.get("structure") or "").strip().lower()
    if structure and structure != "default":
        parts.append(f"Structure your response as: {structure.title()}.")
    tone = (prefs.get("tone") or "").strip().lower()
    if tone and "default" not in tone:
        parts.append(f"Tone: {tone.title()}.")
    description = (prefs.get("description") or "").strip()
    if description:
        parts.append(f"Additional instructions: {description}")
    if not parts:
        return ""
    return "\n\n**User custom instructions (apply these):**\n- " + "\n- ".join(parts)
