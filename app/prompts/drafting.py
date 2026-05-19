"""
Prompts for the Drafting Assistant.

Three concerns:
  1. Suggested drafts panel — proactive 4-6 suggestions per workspace.
  2. Template analysis — map a chosen template to the case, classify slots
     (skeleton / case-variable / lawyer-input), and produce the open
     questions the lawyer must answer.
  3. Final draft rendering — render the draft as Markdown, preserving the
     template's structural skeleton verbatim.

All prompts are grounded in Indian law (BNS / BNSS / BSA / IT Act + targeted
civil acts). IPC is forbidden in outputs — the existing applicable-laws
normaliser already enforces this and the prompts here repeat the rule so the
model does not invent IPC sections.
"""

from __future__ import annotations

from typing import Optional


DRAFTING_SYSTEM_INSTRUCTION = (
    "You are the Drafting Assistant for an Indian legal workspace. "
    "You help a lawyer prepare pleadings, applications, legal notices, "
    "affidavits, replies, and other formal documents that will be filed or "
    "served in India. "
    "Operate in formal Indian legal register: passive voice, third-person, "
    "the conventions of pleadings before Indian courts and tribunals. "
    "Never use US-specific terminology (e.g. 'lemon law', 'discovery', "
    "'motion for summary judgment'). "
    "Always frame citations against Indian statutes (Bharatiya Nyaya Sanhita "
    "2023, Bharatiya Nagarik Suraksha Sanhita 2023, Bharatiya Sakshya "
    "Adhiniyam 2023, IT Act 2000, Hindu Marriage Act 1955, Protection of "
    "Women from Domestic Violence Act 2005, Code of Civil Procedure 1908, "
    "Consumer Protection Act 2019, and other targeted Indian acts). "
    "Never cite the Indian Penal Code, 1860 (IPC), Code of Criminal "
    "Procedure 1973 (CrPC), or Indian Evidence Act 1872 — these have been "
    "replaced by BNS / BNSS / BSA respectively for events from 1 July 2024. "
    "Never invent statute sections, case-law citations, court names, "
    "monetary figures, or party details. If a fact is not in the case "
    "context or the lawyer's input, ask for it instead of fabricating it."
)


# ---------------------------------------------------------------------------
# 1) Suggested drafts (proactive panel)
# ---------------------------------------------------------------------------

SUGGESTED_DRAFTS_INSTRUCTION = (
    DRAFTING_SYSTEM_INSTRUCTION
    + "\n\n"
    + "MODE: SUGGESTED-DRAFTS PANEL. "
    + "Read the workspace context (role, case summary, applicable laws, "
    + "strategy, analysis report, list of dates) and propose 4 to 6 "
    + "drafts that are actually useful for this matter right now. "
    + "Each suggestion is a one-line title plus a one-line description. "
    + "Output strict JSON only — no markdown fences, no commentary."
)


def build_suggested_drafts_prompt(
    role: str,
    case_summary: str,
    applicable_laws_brief: str,
    strategy_brief: str,
    analysis_brief: str,
    list_of_dates_brief: str,
) -> str:
    """Build the user prompt for proactive draft suggestions.

    Returns a prompt that asks for a JSON array of 4-6 suggestion objects.
    """
    role_upper = (role or "ADJUDICATOR").strip().upper()
    return f"""WORKSPACE ROLE: {role_upper}

CASE SUMMARY:
{case_summary or "(no summary available — derive from analysis report below)"}

APPLICABLE LAWS (already identified for this case):
{applicable_laws_brief or "(none yet)"}

STRATEGY / OBJECTIVE:
{strategy_brief or "(none yet)"}

ANALYSIS REPORT (strengths, weaknesses, gaps, pending actions):
{analysis_brief or "(none yet)"}

LIST OF DATES (chronology):
{list_of_dates_brief or "(none yet)"}

TASK:
Propose 4 to 6 drafts that this {role_upper} should be preparing right now,
given the case posture above. For each, return:

  - title:        formal one-line name including the statute and section,
                  e.g. "Application under Section 12 of the Protection of
                  Women from Domestic Violence Act, 2005".
  - relevance:    "High" | "Medium" | "Low" — based on urgency, statutory
                  deadlines, and how directly the draft advances the case.
  - description:  ONE short sentence (<= 160 characters) saying what the
                  draft is for in this specific case.

CONSTRAINTS:
  - No IPC, no CrPC, no Indian Evidence Act references. Use BNS / BNSS /
    BSA instead.
  - No invented sections or acts. If unsure of a section, omit it from the
    title rather than guess.
  - Prefer drafts that are time-bound (statutory deadlines, hearing dates
    in list-of-dates) or that close an explicit gap in the analysis report.
  - Do not duplicate suggestions; each must be a distinct legal action.
  - Cap the list at 6.

OUTPUT FORMAT — strict JSON array, no other text:
[
  {{"title": "...", "relevance": "High", "description": "..."}},
  ...
]
"""


# ---------------------------------------------------------------------------
# 2) Template analysis (kick-off turn)
# ---------------------------------------------------------------------------

TEMPLATE_KICKOFF_INSTRUCTION = (
    DRAFTING_SYSTEM_INSTRUCTION
    + "\n\n"
    + "MODE: TEMPLATE KICK-OFF. "
    + "You have been given the full text of a template the lawyer wants to "
    + "reuse for THIS case, plus the case context. "
    + "Decide what parts of the template are structural skeleton (keep as "
    + "is), what parts are case-variable facts (substitute from case "
    + "context), what parts are context-bound to the template's ORIGINAL "
    + "matter and must be stripped (e.g. property facts when adapting a "
    + "reply-notice template to a dowry case), and what slots still need "
    + "the lawyer's input. "
    + "Respond conversationally in the `message` field, and list the open "
    + "questions in `openQuestions` so the frontend can render them as "
    + "clear inputs. Output strict JSON only."
)


def build_template_kickoff_prompt(
    role: str,
    case_summary: str,
    case_facts_brief: str,
    template_name: str,
    template_text: str,
    lawyer_instructions: Optional[str],
    draft_title_hint: Optional[str],
) -> str:
    role_upper = (role or "ADJUDICATOR").strip().upper()
    instructions_block = (
        f"LAWYER INSTRUCTIONS (free-text addendum):\n{lawyer_instructions}\n"
        if lawyer_instructions
        else ""
    )
    title_hint = (
        f"DRAFT TITLE HINT (from the suggestion the lawyer picked): {draft_title_hint}\n"
        if draft_title_hint
        else ""
    )
    return f"""WORKSPACE ROLE: {role_upper}

CASE SUMMARY:
{case_summary or "(no summary available)"}

CASE FACTS / EVIDENCE BRIEF:
{case_facts_brief or "(no facts brief)"}

TEMPLATE NAME: {template_name or "(unnamed)"}

TEMPLATE FULL TEXT (this is the structural contract for the new draft):
\"\"\"
{template_text or "(template text unavailable)"}
\"\"\"

{title_hint}{instructions_block}

TASK:
1. Identify the template's structural skeleton — headers, salutations
   ("MOST RESPECTFULLY SHOWETH"), party-block layout, prayer scaffolding,
   verification, signature blocks. This MUST be preserved verbatim in the
   final draft.
2. Identify which facts in the template are context-bound to the ORIGINAL
   matter (e.g. property/land facts in a reply-notice template, specific
   sale-deed numbers, original parties) and must be stripped or replaced
   when adapting to THIS case.
3. Pre-fill any case-variable slot from the case context above whenever
   possible (parties, role, statute citations applicable to this matter,
   facts that are already in the case file).
4. List the SLOTS that still need the lawyer's input. Each open question
   must be focused and answerable in one short sentence (e.g. "What is the
   FIR number and date of registration?"), not a 5-part request.
5. If the template's structural skeleton is fundamentally incompatible
   with this case (e.g. a property dispute template for a corporate
   matter with no structural overlap), say so plainly in the `message`
   field and set `incompatible: true` — do NOT silently restructure.

OUTPUT FORMAT — strict JSON only, no markdown fences:
{{
  "message": "Conversational reply to the lawyer. Briefly explain what you mapped automatically from the case, what you stripped from the template's original matter, and then list the open questions in plain prose.",
  "draftTitle": "Proposed title for the final draft, e.g. 'Bail Application — <party> — Section 482 BNSS'",
  "openQuestions": [
    {{"id": "fir_details", "label": "FIR number, date of registration, and police station"}},
    {{"id": "lower_court", "label": "Date and name of the court that rejected initial bail"}}
  ],
  "gatheredFacts": {{
     "<slot_id>": "<pre-filled value from case context>",
     "...": "..."
  }},
  "strippedContext": ["Short note on what context-bound text was stripped from the template, one item per stripped section."],
  "incompatible": false
}}
"""


# ---------------------------------------------------------------------------
# 3) Final draft rendering
# ---------------------------------------------------------------------------

DRAFT_RENDER_INSTRUCTION = (
    DRAFTING_SYSTEM_INSTRUCTION
    + "\n\n"
    + "MODE: FINAL DRAFT RENDER. "
    + "Produce the complete draft as a single Markdown document. "
    + "When a template is supplied, the template's structural skeleton — "
    + "headings, section order, salutations, party-block layout, prayer "
    + "scaffolding, verification, signature blocks — is a CONTRACT and "
    + "MUST be reproduced verbatim. Substitute case-specific facts into "
    + "the slots; strip any context-bound text from the template's "
    + "original matter that does not apply to THIS case. Never invent "
    + "citations, case-law, monetary figures, court names, or party "
    + "details. If the lawyer has not provided a slot value, leave a "
    + "<placeholder> rather than fabricate it."
)


def build_draft_render_prompt(
    role: str,
    case_summary: str,
    case_facts_brief: str,
    template_name: Optional[str],
    template_text: Optional[str],
    gathered_facts_json: str,
    draft_title: str,
    lawyer_instructions: Optional[str],
    mode: str,
    previous_draft_markdown: Optional[str] = None,
    revision_instruction: Optional[str] = None,
) -> str:
    """Build the user prompt for final draft rendering.

    mode ∈ {"template", "suggestedDraft", "blank", "revision"}.
    """
    role_upper = (role or "ADJUDICATOR").strip().upper()

    template_block = ""
    if template_text:
        template_block = (
            f"TEMPLATE NAME: {template_name or '(unnamed)'}\n"
            f"TEMPLATE FULL TEXT (structural contract — preserve headings, section order, salutations, prayer scaffolding, verification, signature blocks verbatim; substitute case-variable facts; strip context-bound facts from the template's original matter):\n"
            f'"""\n{template_text}\n"""\n'
        )

    revision_block = ""
    if mode == "revision" and previous_draft_markdown:
        revision_block = (
            "PREVIOUS DRAFT (the lawyer asked for a revision; preserve the "
            "template skeleton and previously-substituted facts, then apply "
            "the revision instruction below):\n"
            f'"""\n{previous_draft_markdown}\n"""\n\n'
            f"REVISION INSTRUCTION: {revision_instruction or '(unspecified)'}\n"
        )

    instructions_block = (
        f"LAWYER INSTRUCTIONS (free-text addendum):\n{lawyer_instructions}\n"
        if lawyer_instructions
        else ""
    )

    return f"""WORKSPACE ROLE: {role_upper}
MODE: {mode}

CASE SUMMARY:
{case_summary or "(no summary available)"}

CASE FACTS / EVIDENCE BRIEF:
{case_facts_brief or "(no facts brief)"}

GATHERED FACTS FROM LAWYER (JSON):
{gathered_facts_json or "{{}}"}

DRAFT TITLE: {draft_title}

{template_block}{instructions_block}{revision_block}

TASK:
Produce the complete draft as a single Markdown document.

CONSTRAINTS:
  - Indian legal register, passive voice, third person.
  - No IPC / CrPC / Indian Evidence Act — use BNS / BNSS / BSA instead.
  - No invented citations, statute sections, court names, or monetary figures.
  - Where a slot value is missing, leave it as <placeholder> with a clear
    label, e.g. "<applicant address>". Do not fabricate.
  - When a template is provided, the template's skeleton is a CONTRACT.
    Preserve all of its headings, section order, party-block layout,
    salutations, prayer scaffolding, verification, and signature blocks.
    Substitute case-variable facts. Strip context-bound facts that do not
    apply to this matter.
  - Do not output any preamble, JSON, or commentary — output ONLY the
    Markdown draft body, starting with the document's title heading.
"""
