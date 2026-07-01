"""Prompt builders for the procedural_timeline feature (extracted from handlers, verbatim)."""


__all__ = [
    "regenerate_procedural_timeline_with_feedback_prompt",
]


def regenerate_procedural_timeline_with_feedback_prompt(*, current_json, instruction):
    return (
f"""You are editing a case's PROCEDURAL TIMELINE / UPCOMING DATES (a JSON array of milestones). Apply ONLY the user's name change to the EXISTING entries and return the FULL array.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT procedural timeline (edit THIS array in place):
{current_json}

STRICT RULES:
1. This is a NAME change ONLY. Replace the renamed party's name wherever it appears in each entry's "title" and "description". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
2. Keep the SAME number of entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
3. Do NOT change "date" or "type" for any entry — reproduce them EXACTLY.
4. Change nothing except the renamed name.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""
    )

