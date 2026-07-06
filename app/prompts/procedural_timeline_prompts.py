"""Prompt builders for the procedural_timeline feature (extracted from handlers, verbatim)."""


__all__ = [
    "regenerate_procedural_timeline_with_feedback_prompt",
]


def regenerate_procedural_timeline_with_feedback_prompt(*, current_json, instruction):
    return (
f"""You are editing a case's PROCEDURAL TIMELINE / UPCOMING DATES (a JSON array of milestones). Apply ONLY the user's find-and-replace / correction instruction to the EXISTING entries and return the FULL array. This is a surgical edit, NOT a regeneration.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT procedural timeline (edit THIS array in place):
{current_json}

STRICT RULES:
1. Apply ONLY what the instruction specifies — a correction of a NAME, TERM, DATE, or YEAR:
   - NAME change: replace the party's name wherever it appears in each entry's "title" and "description". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
   - DATE or YEAR change: apply it to the "date" field as well. "date" is an ISO value "YYYY-MM-DD"; when the instruction replaces a year (e.g. 2024 → 2026), rewrite the year of EVERY "date" that carries the old year ("2024-05-25" → "2026-05-25"), and also update any date/year text that appears in "title" and "description".
2. Keep the SAME number of entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
3. Reproduce "type" EXACTLY. Change "date" ONLY when the instruction targets a date or year; otherwise reproduce it unchanged.
4. Change nothing except what the instruction specifies.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""
    )

