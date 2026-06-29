"""Prompt builders for the analysis_report feature (extracted from handlers, verbatim)."""

import json

__all__ = [
    "regenerate_analysis_report_with_feedback_prompt",
    "generate_analysis_report_from_documents_prompt",
    "generate_analysis_report_from_documents_prompt2",
    "ensure_structured_relevant_jurisdiction_prompt",
    "ensure_quality_score_explanation_prompt",
    "compute_rejection_score_delta_prompt",
    "compute_added_missing_info_score_delta_prompt",
]


def regenerate_analysis_report_with_feedback_prompt(*, SPEAKER_LABEL_GUARDRAIL, current_report_json, instruction, role):
    return (
f"""You are a legal-analysis-report editor for Indian law.

You will edit the existing Analysis Report (JSON below) by applying the user's instruction. Return the FULL report with the change applied and everything else reproduced verbatim.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{instruction}

CURRENT analysis report JSON (edit THIS, preserving its exact structure and keys):
{current_report_json}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL — override every other instinct):
1. The user's instruction OUTRANKS any tendency to "improve", "summarise", or "regenerate" the report. Apply ONLY the change the user named.
2. If the instruction is a RENAME / FIND-AND-REPLACE (e.g. replace every occurrence of "X" with "Y"), replace the term EVERYWHERE it appears across ALL string content: every `strengths[].point` and `strengths[].laws` entry, every `weaknesses`, `risks`, `missingInfo`, `nextSteps`, `pendingActions`, `suggestedDrafts`, and `relevantJurisdiction` item, and the `qualityScoreExplanation` / `reportQualityScoreExplanation` text. Miss NO occurrence.
2a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she") rather than by a literal existing token, identify that party from the role / gender / description context and use the supplied name wherever that party is referenced across ALL the string fields listed in rule 2 — INCLUDING role labels like "the Petitioner" / "the employee" — even if the name does not already appear in the report. A literal matching token is NOT required.
3. If the user QUOTES exact text (a sentence, a phrase, a list item) inside the instruction, find that exact text in the report and REMOVE (or rephrase, per the verb the user used) ONLY that text/item.
4. Every other field, list item, sentence, and reference MUST be reproduced WORD-FOR-WORD. Do NOT drop, reorder, compress, or re-score anything the user did not mention.
5. Keep `caseResolutionScore` and the score explanations unchanged UNLESS the user explicitly asked to change the score. (A rename inside the explanation text is allowed — that is just text replacement, not a score change.)
6. Preserve the exact JSON shape and keys present in CURRENT analysis report: `strengths` stays a list of objects with `point` (string) and `laws` (list of strings); `weaknesses` / `risks` / `missingInfo` / `nextSteps` / `pendingActions` / `suggestedDrafts` / `relevantJurisdiction` stay lists of strings.
7. If the user asks for something impossible (e.g. inventing a fact not present), ignore that part and apply only the valid edit.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Output rules:
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do NOT switch to IPC/CrPC/Evidence Act.
- Return ONLY the complete, valid analysis-report JSON object (no markdown code fences, no commentary)."""
    )


def generate_analysis_report_from_documents_prompt(*, num_user_uploads):
    return (
"AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    f"- The user has uploaded exactly {num_user_uploads} annexures.\n"
                    f"- The ONLY valid annexure references are: Annexure 1 through Annexure {num_user_uploads}.\n"
                    f"- You MUST NOT reference Annexure {num_user_uploads + 1} or any higher number.\n"
                    "- Do NOT invent additional annexures."
    )


def generate_analysis_report_from_documents_prompt2(*, _entities_block_ar, combined_document_content, data, missing_info):
    return (
"You produced an analysis report for an Indian-law matter but "
                        "left the strengths array nearly empty. The schema MANDATES "
                        "2-6 strengths. Every matter has at least two facts that help "
                        "the client's position.\n\n"
                        "Return ONE JSON object with exactly this shape (no commentary, "
                        "no markdown):\n\n"
                        "{\n"
                        '  "strengths": [\n'
                        '    { "point": "2-4 sentence factual strength naming the specific '
                        'party / date / Annexure / amount from EXTRACTED FACTS that '
                        'establishes it. Do NOT pad with statute boilerplate. Do NOT use '
                        '\'Party No. X\' placeholders.", "laws": ["BNS Section 318", '
                        '"BSA"] }\n'
                        "  ]\n"
                        "}\n\n"
                        "Anchor every strength to EXTRACTED FACTS. Valid strength "
                        "categories (use at least 2):\n"
                        "1. Documentary evidence preserved in an annexure (MLC, FIR, "
                        "registered deed, hospital record, signed contract, "
                        "audio/video transcript, bank statement, photograph, official "
                        "certificate).\n"
                        "2. Party admissions (opposing side stated, signed, or did "
                        "something supporting the client — partial payment, written "
                        "reply admitting a fact, official detention record).\n"
                        "3. Procedural advantage (favourable forum / jurisdiction, "
                        "limitation period still open, statutory presumption in "
                        "client's favour).\n"
                        "4. Statutory framework support (a named provision under BNS / "
                        "BNSS / BSA / IT Act / Specific Relief Act / Hindu Marriage "
                        "Act / Domestic Violence Act / etc. that on the recorded facts "
                        "points to relief for the client).\n"
                        "5. Corroboration across annexures (two or more annexures that "
                        "independently support the same fact).\n\n"
                        f"EXTRACTED FACTS:\n{_entities_block_ar}\n\n"
                        "CONTEXT — items you already produced for this report:\n"
                        f"- missingInfo: {(missing_info or [])[:8]}\n"
                        f"- weaknesses: {(data.get('weaknesses') or [])[:6] if isinstance(data.get('weaknesses'), list) else []}\n"
                        f"- risks: {(data.get('risks') or [])[:6] if isinstance(data.get('risks'), list) else []}\n\n"
                        "DOCUMENTS (in upload order):\n"
                        f"{combined_document_content[:60000]}\n"
    )


def ensure_structured_relevant_jurisdiction_prompt(*, case_summary, cleaned, document_excerpt, strategic_objective):
    return (
f"""Rewrite/produce relevantJurisdiction entries in STRICT structured form.

Current entries:
{json.dumps(cleaned, ensure_ascii=False)}

Case summary:
{case_summary}

Strategic objective:
{strategic_objective}

Document excerpt:
{(document_excerpt or "")[:12000]}

Rules:
- Output ONLY JSON object: {{"relevantJurisdiction": ["..."]}}
- 1 to 5 items only
- Entries MUST be COURT/FORUM names only (no reasons/legal basis text).
- Do NOT output explanations in parentheses, after hyphen, or after colon.
- Do NOT output location-only lines.
- Good: "Metropolitan Magistrate Court, Delhi"
- Good: "High Court of Delhi"
- Do NOT output lines like "Patna, Bihar: Permissible under ...".
- Prefer case-specific nearby forums from facts.
- Include High Court/Supreme Court only when plausibly relevant to the case posture.
"""
    )


def ensure_quality_score_explanation_prompt(*, case_resolution_score, summary):
    return (
f"You are a senior legal analyst. Given this case analysis summary (JSON only), "
                f"write exactly 3-5 sentences for an Indian-law workspace. "
                f'Start with "The current score is {case_resolution_score}% because". '
                "Then anchor the explanation to SPECIFIC items from the summary — quote (do not paraphrase generically) "
                "at least one missingInfo item by what is missing and from whom, one weakness or risk by the "
                "named party / date / amount / annexure that triggers it, and one strength by the named fact "
                "(party + date + Annexure) that supports it. If the score is 100, name the top 1-2 strengths "
                "instead and state that no missingInfo / weaknesses / risks were flagged. "
                "Never use generic phrasing like 'gaps in annexures' or 'procedural risks identified' without naming "
                "WHICH gap, WHICH risk, and FOR WHOM. Use names exactly as they appear in the summary; do NOT "
                "invent facts not present in the summary.\n\n"
                f"{summary}"
    )


def compute_rejection_score_delta_prompt(*, can_reach_100, case_summary, current_score, rejected_value, remaining_missing, remaining_risks, remaining_weak, report_summary, section, section_context, strategic_objective):
    return (
f"""You are a legal case analyst. The user rejected ONE item from their analysis report as not relevant. Two decisions are required:

1) Is the rejection VALID? It is VALID only when the rejected item is genuinely NOT relevant to THIS case (per the case summary, strategic objective, and remaining items below). If the rejected item IS relevant — i.e. it is a real evidence gap, factual weakness, or procedural risk that this case actually faces — the rejection is INVALID and the user should not be rewarded with a score increase.
2) If VALID, choose an integer scoreIncrease 1-5. The system will ADD this to the current score. If INVALID, set scoreIncrease=0.

**Current case resolution score**: {current_score}%
**Case summary**: {case_summary[:600] or "(not available)"}
**Report-facing summary**: {report_summary[:400] or "(not available)"}
**Strategic objective**: {strategic_objective[:400] or "(not available)"}
**Section from which item was rejected**: {section}
**Rejected item (exact text)**: "{rejected_value[:400]}"
**Remaining counts after this rejection**: missingInfo={remaining_missing}, weaknesses={remaining_weak}, risks={remaining_risks}
{f'**{section_context}**' if section_context else ''}
**Score rule**: When missingInfo, weaknesses, and risks are all 0 (no remaining items), the score can reach 100%. When any remain, score is capped at 95%. {"This rejection leaves no items in any category – score can go to 100%." if can_reach_100 else "Items still remain – score will be capped at 95%."}

VALIDITY GUIDELINES (decide isValidRejection)
- VALID: the rejected item is off-topic, generic boilerplate that doesn't fit this case's facts, addressed by another item, or clearly out-of-scope for the parties/role/jurisdiction.
- INVALID: the rejected item is core to the pleaded facts, supports a key element the case relies on, or directly addresses a real risk this case faces. Generic items that DO apply to this case are still INVALID rejections.

IMPACT RULES (only when isValidRejection=true) – choose scoreIncrease (1-5) based on the SPECIFIC rejected item:
- scoreIncrease MUST be an integer between 1 and 5.
- When this is the last rejection (all counts 0 above), use 4 or 5 so the score can reach 100%.

SECTION GUIDELINES (when isValidRejection=true)

missingInfo (user says this "missing" item is not needed):
- Minor detail (dates, small clarification): scoreIncrease = 1
- Useful but not critical evidence: scoreIncrease = 2–3
- Important missing evidence that would have weakened the case: scoreIncrease = 4–5

riskOrWeakness (user says this risk/weakness does not apply):
- Minor procedural or generic risk: scoreIncrease = 1
- Moderate risk or weakness: scoreIncrease = 2–3
- Major legal risk or strong weakness: scoreIncrease = 4–5

Output ONLY valid JSON, no markdown, no code block:
{{"isValidRejection": <true|false>, "scoreIncrease": <integer 0-5>, "reason": "<one short sentence>"}}
"""
    )


def compute_added_missing_info_score_delta_prompt(*, case_summary, current_score, missing_block, report_summary, risk_block, safe_description, safe_title, strategic_objective, weakness_block):
    return (
f"""You are a legal case analyst. The user manually supplied additional information to address previously-flagged "missing information" items in their analysis report. Two decisions are required:

1) Is the user-supplied text RELATED to this specific case? Use the case summary, missing info, weaknesses, and risks below as ground truth. Generic, off-topic, gibberish, or unrelated text → isRelated=false.
2) If RELATED, choose an integer scoreDecrease between 1 and 5. The system will SUBTRACT this from the current score because the user has just confirmed a real gap existed and the new context may surface additional risks/weaknesses.

**Current case resolution score**: {current_score}%

**Case summary**: {case_summary[:800] or "(not available)"}
**Report-facing summary**: {report_summary[:600] or "(not available)"}
**Strategic objective**: {strategic_objective[:400] or "(not available)"}

**Existing missingInfo items**:
{missing_block}

**Existing weaknesses**:
{weakness_block}

**Existing risks**:
{risk_block}

**User-supplied title**: "{safe_title}"
**User-supplied description**: "{safe_description}"

IMPACT RULES (only when isRelated=true):
- Minor / vague clarification that lightly touches one missing item: scoreDecrease = 1
- Moderate, on-topic information that addresses one missing item but introduces small new uncertainty: scoreDecrease = 2-3
- Highly relevant information that exposes additional gaps, factual weaknesses, or procedural risks: scoreDecrease = 4-5

When isRelated=false set scoreDecrease=0 and reason explains briefly why it is off-topic.

Output ONLY valid JSON, no markdown, no code block:
{{"isRelated": <true|false>, "scoreDecrease": <integer 0-5>, "reason": "<one short sentence>"}}
"""
    )

