"""Prompt builders for the law_relevance feature (extracted from handlers, verbatim)."""


__all__ = [
    "assess_law_relevance_with_kanoon_and_llm_prompt",
]


def assess_law_relevance_with_kanoon_and_llm_prompt(*, act, desc, doc_context, section, signals, source_blob, title, user_note):
    return (
f"""You are a senior Indian lawyer. The user selected ONE statute provision; decide if it fits THIS case.

You MUST use BOTH:
(A) the case document excerpt, and
(B) the Indian Kanoon / case-law search lines (titles/snippets/URLs) when present.

Return exactly one JSON object (no markdown), shape:
{{
  "relevance": "High" | "Medium" | "Low" | "not relevant",
  "rationale": "string, 2–5 short sentences"
}}

SCORING (strict):
- **High**: Document facts clearly engage this act+section, AND search results show this provision or very similar charges/relief in comparable fact patterns (name the pattern briefly). Rationale must name at least one **specific document fact** and one **search-line theme** (or state search was empty but document fit is overwhelming—only if documents are very strong).
- **Medium**: Document facts plausibly engage this provision; search supports or is neutral; some uncertainty. Rationale must cite at least one **concrete document fact** and explain the fit.
- **Low**: Only if the **documents** still contain at least one concrete fact that could trigger this provision, but the fit is weak or peripheral. Do **not** pick Low from search alone. Rationale must quote the weak fact you rely on.
- **not relevant**: Provision does not meaningfully apply, or documents lack any hook, or search+documents show mismatch.

Rules:
- Do NOT fabricate facts not present in the document excerpt.
- Do NOT cite IPC unless the selected act is IPC; prefer BNS framing if overlapping.
- If search block is empty, you may still assign Medium or Low only with strong/slight document hooks respectively; **High** requires either search support or unusually clear statutory fit spelled out in the documents.

Selected law:
- Act: {act}
- Section: {section}
{f"- Title: {title}" if title else ""}
{f"- Prior description: {desc}" if desc else ""}
{f"- Lawyer note: {user_note}" if user_note else ""}

Workspace title / keywords (hint): {signals.get("workspaceTitle","")} | {", ".join(signals.get("keywords") or [])}

Case documents (excerpt):
{doc_context}

Indian Kanoon / search results (titles and snippets):
{source_blob}
"""
    )

