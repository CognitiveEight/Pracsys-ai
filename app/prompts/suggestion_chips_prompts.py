"""Prompt builders for the suggestion_chips feature (extracted from handlers, verbatim)."""


__all__ = [
    "generate_contextual_suggestion_chips_prompt",
    "generate_drafting_chips_prompt",
    "generate_analysis_report_suggestion_chips_prompt",
]


def generate_contextual_suggestion_chips_prompt(*, ai_preview, conversation_summary, role_upper, user_preview):
    return (
f"""You are suggesting follow-up prompts for a legal workspace chat. The workspace role is: {role_upper}.

Last user message: "{user_preview}"
Last assistant reply (summary): "{ai_preview}"
{f'Recent context: {conversation_summary[:200]}' if conversation_summary else ''}

You MUST output a JSON array of exactly 10, 11, or 12 short follow-up prompts. Minimum 10 items. Not 4. Not 5. At least 10 strings in the array.
Each prompt: 2-8 words, role-appropriate, case-related, actionable. User will click one and it gets sent as their next message.
Vary types: draft, explain, list, risks, next steps, clarify, evidence, options.

Return ONLY a JSON array, no other text. Example with 10+ items: ["Draft a settlement proposal", "List key issues for mediation", "What are the risks for each party?", "Suggest compromise options", "Summarise evidence so far", "Outline procedural next steps", "Identify gaps in the complaint", "Recommend documents to collect", "Clarify jurisdiction", "Compare both sides' positions", "Suggest interim measures"]
"""
    )


def generate_drafting_chips_prompt(*, draft_preview, mode_block, msg_preview, open_q_text, status, title):
    return (
f"""You are suggesting follow-up chips for the Drafting Assistant chat panel of an Indian legal workspace. The lawyer is mid-draft.

DRAFT TITLE: {title}
DRAFT STATUS: {status or "ready"}

OPEN QUESTIONS STILL ON FILE (if any):
{open_q_text or "(none — all answered)"}

LAST ASSISTANT MESSAGE (the most recent chat turn shown to the lawyer):
\"\"\"
{msg_preview}
\"\"\"

DRAFT BODY PREVIEW (if a draft has been delivered):
\"\"\"
{draft_preview or "(no draft yet)"}
\"\"\"

{mode_block}

CONSTRAINTS:
- Each chip MUST be 2–6 words.
- Use Indian-advocate phrasing (\"Adjust the Prayer\", \"Add Section 50 Ground\", \"Tighten the Facts\", \"Cite Arnesh Kumar\", \"Mention Tihar Detention\", \"Add Verification Clause\").
- Reference real case anchors when visible in the draft body (e.g. quantity numbers, dates, party names, statute citations).
- No IPC / CrPC / Indian Evidence Act references. Use BNS / BNSS / BSA where citations are needed.
- No full sentences. No trailing punctuation.
- Each chip stands alone — if the lawyer clicks it, the text is sent as their next message and the drafting agent should be able to act on it as a revision instruction (or as a slot answer when in gathering_info).

OUTPUT FORMAT — strict JSON array of 8 to 12 chip strings, no commentary, no markdown fences:
["Strengthen the Grounds", "Adjust the Prayer", "Add Section 50 Ground", ...]
"""
    )


def generate_analysis_report_suggestion_chips_prompt(*, context_str, role_upper):
    return (
f"""You are generating suggestion chips for an Analysis Report view in a legal case tool. Role: {role_upper}.

Analysis report summary:
{context_str}

Output a JSON array of exactly 15 or 16 short, actionable prompt labels (2-5 words each). Each label is a question or action the user might tap to ask about the analysis. Examples: "What is in this document", "Explain key strengths", "List missing information", "What are the risks", "Summarise weaknesses", "Suggest next steps", "Explain quality score", "Relevant jurisdiction", "Pending actions", "Recommended drafts". No full sentences. Vary: missing info, strengths, weaknesses, risks, next steps, drafts, jurisdiction, score explanation.

Return ONLY a JSON array of 15-16 strings, no other text."""
    )

