"""Prompt builders for the strategy feature (extracted from handlers, verbatim)."""

import json

__all__ = [
    "generate_strategies_from_documents_prompt",
    "generate_strategies_from_documents_prompt2",
    "generate_strategies_from_documents_prompt3",
    "generate_strategies_from_documents_prompt4",
    "generate_strategies_from_documents_prompt5",
    "generate_strategies_from_documents_prompt6",
    "regenerate_strategy_objective_preserving_case_summary_prompt",
    "regenerate_case_summary_with_feedback_prompt",
    "regenerate_next_steps_with_feedback_prompt",
    "regenerate_strategic_objective_with_feedback_prompt",
    "summarize_objective_instruction_from_thread_prompt",
    "should_offer_objective_update_after_discussion_prompt",
    "llm_classify_objective_change_intent_prompt",
    "llm_classify_panel_edit_intent_prompt",
    "llm_classify_panel_edit_intent_prompt2",
]


def generate_strategies_from_documents_prompt(*, combined_document_content, workspace_role):
    return (
f"""You are a legal strategy analyst for Indian law. Analyze the following documents and provide a detailed strategy and objective.

**Role**: {workspace_role}
**Document Content** (documents are in upload order; the last one is the most recently uploaded):
{combined_document_content}

**Task**: Based on the document content above, provide:

1. **Case Summary** (10-20 sentences): Start EXACTLY with the phrase: **"This case is about..."**. Write a fact-rich narrative that NAMES the parties exactly as written in the documents (full names + designations / roles such as plaintiff, defendant, complainant, vendor, vendee). When the documents contain them, you MUST include: property/land/document identifiers (e.g. *Arazi No. 262, area 0.263 Hectares*, *Khasra No. 285*), location to the most specific level supported by the documents (Mauza/Village → Block/Tehsil → District/State), exact monetary amounts in the same notation as the source (*Rs. 11,64,000*, *Rs. 10,00,000*), specific dates of agreements/notices/filings (DD/MM/YYYY), and any suit/case numbers (e.g. *O.S. No. 827/2019*). Reference uploaded files only as **Annexure N**. Do not generalise — extract.

2. **Report Case Summary** (10-20 sentences): MUST start EXACTLY with **"This matter is about..."**. Mirror the same facts, structure, and coverage as the case summary but as a neutral, expanded, polished narrative for formal reports and filings. Apply the SAME fact-extraction checklist: named parties (full names + designations), property/document identifiers, full location, exact amounts as written, specific dates, suit/case numbers, named statutes. **Strictly do not** use: Petitioner, Respondent, Client, Case, Adjudicator, Mediator, or role-style labels; describe parties only by names, designations, or factual identifiers from the documents.

3. **Strategic Objective** (10-20 sentences): Write a role-tailored strategic objective for the **{workspace_role}** perspective. It MUST anchor on the actual strongest facts (named parties, specific dates, named documents/Annexures, exact amounts, suit numbers) — never generic categories. Cover: (a) the strongest facts from the documents (named), (b) the legal issues (with named statutes such as BNS / BNSS / BSA / IT Act / Specific Relief Act etc. as relevant), (c) practical priority order (what to do first), and (d) how evidence/timeline supports or limits the objective.

4. **Possible Next Steps** (3-5 bullet points): Immediate, executable actions for right away. Keep them concrete (who/what to do). Prefer steps that preserve evidence, clarify missing facts, and move the case forward procedurally for the selected role. When citing an uploaded file by its order in the bundle, write **Annexure N** (same order as above); **never** write "Document N" or "document" for an upload—use **annexure / annexures** only.

**Completed-action recognition (CRITICAL).** Inspect the metadata header on every annexure. When a document is a `notice` / `complaint` / `affidavit` / `application` / `plaint` AND either (a) its `ISSUED BY` field names the client side (matches the `{workspace_role}` perspective, the named client, or the client's advocate), OR (b) the header carries a `[DRAFTED BY ASSISTANT …]` marker, treat the underlying procedural action as **ALREADY COMPLETED** in this matter. Apply this consistently across every output field:
- `caseSummary` / `reportCaseSummary`: narrate the completed action in past tense (e.g. *"On 22/05/2026, a legal notice for recovery of €17,185 was issued by Advocate Rohan Mehta to Dana Europe OÜ and the related noticees…"*), citing the served-on date, the addressees, the demand amount, and the compliance window. Then continue with the next stage of the matter.
- `strategicObjective`: refer to the action in past tense (*"the demand notice has been served on …"*) and shift focus to the next pending action that the served notice enables — monitoring the compliance window, preparing the follow-up suit/FIR/application on default, evaluating arbitration invocation, etc.
- `possibleNextSteps`: **DO NOT re-list the action that has already been performed**. If a legal/demand notice has been served, the next step is NOT *"issue a legal notice"*; it is monitoring the compliance deadline (cite the exact date if available in `[KEY DATES]`) and the natural follow-up (e.g. summary suit under Order XXXVII CPC, BNS §318 / §316 criminal complaint, arbitration invocation per the agreement). If an FIR complaint has been filed, the next step is NOT *"file an FIR"*; it is follow-up on registration / investigation status. Same for affidavits, applications, plaints, written statements.
- Apply this rule independently to each completed action — if multiple drafted documents exist in the annexure set, each one is a separate completed milestone.

**Format your response as JSON**:
{{
  "caseSummary": "...",
  "reportCaseSummary": "...",
  "strategicObjective": "...",
  "possibleNextSteps": ["...", "...", "...", "..."]
}}

**Important**: 
- Be specific to the case facts in the documents. Do NOT invent facts.
- Use Indian legal terminology appropriately.
- Reference BNS (Bharatiya Nyaya Sanhita) sections, NOT IPC (Indian Penal Code is replaced).
- Reference BNSS (Bharatiya Nagarik Suraksha Sanhita) for procedural matters, NOT CrPC.
- Reference BSA (Bharatiya Sakshya Adhiniyam) for evidence matters, NOT Indian Evidence Act.
- For IT Act, continue using Information Technology Act, 2000 sections as it's still in force.
- Make next steps actionable and role-specific.
- If documents do not contain enough information, explicitly state the limitation in caseSummary/reportCaseSummary/strategicObjective (do not guess).
- If documents seem to include multiple matters, explicitly call out the split and focus your output on the dominant dispute supported by strongest evidence.

**Legal-domain coverage (civil law AND criminal law — BOTH branches):**
- Treat caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps as a dual-lens exercise. Before writing each field, consciously ask:
  (1) "What CRIMINAL-law angle (e.g. BNS 2023 / BNSS 2023 / BSA 2023 / IT Act 2000 / NI Act §138 / PMLA / NDPS / POCSO / PCA, etc.) do the facts attract?", and
  (2) "What CIVIL-law angle (e.g. Indian Contract Act 1872 / Specific Relief Act 1963 / Transfer of Property Act 1882 / CPC 1908 / Limitation Act 1963 / Sale of Goods Act 1930 / Consumer Protection Act 2019 / Companies Act 2013 / Arbitration Act 1996 / family laws / RERA / MV Act / CGST Act / Insolvency & Bankruptcy Code, etc.) do the same facts attract?"
- When BOTH branches are plausibly supported by the documents, the output MUST cover both:
  * `caseSummary` and `reportCaseSummary`: name the criminal-law issues AND the civil-law / private-law issues raised by the facts.
  * `strategicObjective`: address the role's plan on BOTH the criminal-law track (e.g. FIR / complaint / prosecution / defence / quashing under BNSS) AND the civil-law track (e.g. suit for damages / specific performance / declaration / injunction / consumer complaint / arbitration / NCLT petition), in priority order.
  * `possibleNextSteps`: include at least one immediate criminal-track action AND at least one immediate civil-track action when both branches are live (e.g. "Preserve digital evidence and prepare BNS §318 cheating complaint" alongside "Issue legal notice under Indian Contract Act §73 demanding compensation").
- When the facts only support one branch (purely civil OR purely criminal), stay in that branch — do NOT manufacture the other side just to look balanced. Coherence beats balance.
- **No bracket tags in user-facing output.** Do NOT prefix or label items with "(criminal angle)", "(civil angle)", "(criminal-law angle)", "(civil-law angle)", or any similar bracketed tag. Weave the criminal-law and civil-law content naturally into the prose of `caseSummary` / `reportCaseSummary` / `strategicObjective` and into the plain text of each `possibleNextSteps` bullet, the same way a lawyer would draft them in a memo."""
    )


def generate_strategies_from_documents_prompt2(*, SPEAKER_LABEL_GUARDRAIL, _entities_block, annexure_limit_block, combined_document_content, strategy_inventory, workspace_role, current_year, today_str):
    return (
f"""Analyze these legal documents and provide a JSON response.

EXTRACTED FACTS (these entities were already pulled from the documents by a
focused extraction step — treat them as authoritative ground truth. Anchor
every named reference in your output to these. Do NOT rename, abbreviate,
or modify any of these names. Do NOT introduce 'Party No. 1' / 'Party No. 2'
/ 'the plaintiff' / 'the respondent' / 'the petitioner' style placeholders —
EVERY reference to a person, entity, property, suit, or amount in your
output MUST use the exact form listed below. If a relevant fact is missing
from this block but is clearly present in the DOCUMENT CONTENT below, you
may add it, but never invent.):

{_entities_block}

{strategy_inventory or "DOCUMENT INVENTORY: (no classified inventory available)"}

{annexure_limit_block}

DOCUMENT CONTENT:
{combined_document_content}

TRUNCATED-TEXT AND PERIPHERAL-ENTITY RULES (CRITICAL — applies to caseSummary, reportCaseSummary, strategicObjective, possibleNextSteps, and suggestionChips):

1. Some uploaded annexures are SCREENSHOTS of mobile-app or web-app UIs (UPI apps, billers directories, recent-payees lists, vendor catalogs, search-suggestion dropdowns, etc.). In those UIs, the application itself truncates long labels with "..." or "…" so they fit the row width. OCR captures the truncation verbatim.

2. TREAT ANY TOKEN, NAME, OR PHRASE IN THE DOCUMENT CONTENT THAT ENDS IN "..." OR "…" AS A UI-TRUNCATED LABEL, NOT A REAL ENTITY NAME. Examples of the pattern you must filter: "Smart Prep...", "Uttar Pradesh Power Corporatio...", "MSEDCL Mahavitaran - Maharashtra ...", "ABC Industri…".

3. NEVER reproduce a UI-truncated label verbatim in your output. NEVER write any phrase that ends in "..." or "…" inside caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps / suggestionChips. NEVER attempt to guess the full name from a truncated prefix — that would be a hallucination.

4. PERIPHERAL ENTITIES — Only include in your output the entities that are MATERIALLY CONNECTED to the alleged events or transactions described in the documents (named parties, amounts paid/received, identified accused/complainants, banks where the disputed transactions occurred, statutes invoked, courts named). DO NOT list incidental items pulled from app-UI screenshots — billers directories, recent-payees lists, vendor catalogs, search results — unless a specific entry is itself part of the alleged events.

5. If after filtering you have NO concrete case facts left in a section, state that limitation explicitly per the INSUFFICIENT DATA rule below — do NOT fill the gap with peripheral biller / vendor / payee listings.

{SPEAKER_LABEL_GUARDRAIL}

DATE INTERPRETATION (CRITICAL): Today is {today_str} (current year {current_year}). If the source states a date together with its year, use that exact year. If a date is stated WITHOUT a year (e.g. "25 May", "26 tareekh", "last month"), use the CURRENT YEAR ({current_year}); step back one year only if the current year would place the event in the future. Never default to 2024 or any other legacy year for a date whose year was not stated in the source. Statute years (e.g. "Information Technology Act, 2000", "BNS, 2023") are fixed by law and MUST NOT be changed by this rule.

YOUR ROLE: {workspace_role}

TASK:
1. Case summary (10-20 sentences — write the FULL chronological narrative, do NOT compress ; MUST start with "This case is about..." ; MUST name every party exactly as written, include every property/land/document identifier, exact amounts in source notation, every significant date in DD/MM/YYYY, every suit/case/FIR number, and named statutes when present in the documents — never generic categories)
2. Report case summary (10-20 sentences — write the FULL narrative, do NOT compress ; MUST start EXACTLY with "This matter is about..." ; MUST mirror the same facts, structure, and coverage as the case summary BUT rewritten as a neutral, expanded, and polished narrative suitable for formal reports and filings ; same fact-extraction checklist as the case summary — named parties, property/document IDs, exact amounts, every significant date, suit numbers, named statutes ; STRICTLY DO NOT use the words: Petitioner, Respondent, Client, Case, Adjudicator, Mediator, or any role-based labels ; avoid even generic role references ; describe parties only by their names, designations, or factual identifiers from the documents ; maintain an advanced, professional tone while keeping it clear and easy to read)
3. Strategic objective (10-20 sentences — write the FULL plan, do NOT compress ; role-specific for {workspace_role} ; MUST anchor on named parties, specific dates, exact amounts, suit numbers, and named statutes from the documents — never generic categories)
4. Possible next steps (3-5 immediate, concrete actions that should be taken right away). When referring to an uploaded file by index, use **Annexure N** where N is the upload-order index within the hard limit specified in AVAILABLE ANNEXURES above. Never use the word **Document** or **document** for an upload—use **annexure** / **annexures** only.

LEGAL FRAMEWORK (CRITICAL - USE CORRECT INDIAN LAWS):
- Always reference applicable and **most relevant Indian statutes/sections based on the case facts**, including but not limited to:
  CRIMINAL-LAW SIDE:
  • Bharatiya Nyaya Sanhita (BNS), 2023 — substantive offences
  • Bharatiya Nagarik Suraksha Sanhita (BNSS), 2023 — criminal procedure
  • Bharatiya Sakshya Adhiniyam (BSA), 2023 — evidence
  • Information Technology Act, 2000 — cyber-offences
  • Negotiable Instruments Act, 1881 — Section 138 (cheque dishonour, quasi-criminal)
  • Prevention of Money Laundering Act, 2002; NDPS Act, 1985; Arms Act, 1959; POCSO, 2012; PCA, 1988; UAPA, 1967, etc., where facts fit
  CIVIL-LAW SIDE:
  • Indian Contract Act, 1872
  • Specific Relief Act, 1963
  • Transfer of Property Act, 1882
  • Code of Civil Procedure, 1908
  • Limitation Act, 1963
  • Sale of Goods Act, 1930
  • Consumer Protection Act, 2019
  • Companies Act, 2013
  • Arbitration and Conciliation Act, 1996
  • Hindu Marriage Act 1955 / Special Marriage Act 1954 / Hindu Succession Act 1956 / Guardians and Wards Act 1890 (family / succession matters)
  • Real Estate (Regulation and Development) Act, 2016; Motor Vehicles Act, 1988; Insolvency and Bankruptcy Code, 2016, etc., where facts fit
  REGULATORY / TAX (where relevant):
  • Goods and Services Tax (GST) Acts
  • Right to Information Act, 2005
  • SC/ST (Prevention of Atrocities) Act, 1989
  • Any other applicable central or state law

- Do NOT restrict analysis to only one statute—identify and apply **all relevant laws**.
- Prefer **latest applicable laws** (e.g., BNS/BNSS/BSA instead of IPC/CrPC/Evidence Act where applicable). Do NOT cite IPC 1860, CrPC 1973, or the Indian Evidence Act 1872.

DUAL-LENS RULE (CIVIL LAW + CRIMINAL LAW — BOTH BRANCHES):
- Before writing caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps, consciously ask:
  (1) What is the **criminal-law angle** of these facts? (BNS / BNSS / BSA / IT Act / NI Act §138 / etc.)
  (2) What is the **civil-law angle** of the same facts? (Contract / Specific Relief / TPA / CPC / Consumer Protection / Companies Act / Arbitration / family law / etc.)
- When BOTH branches are plausibly supported by the documents, the output MUST cover both:
  * caseSummary + reportCaseSummary: name the criminal-law issues AND the civil-law / private-law issues raised by the facts.
  * strategicObjective: address the role's plan on BOTH the criminal-law track (e.g. FIR / complaint / prosecution / defence / quashing under BNSS) AND the civil-law track (e.g. suit for damages / specific performance / declaration / injunction / consumer complaint / arbitration / NCLT petition), in priority order.
  * possibleNextSteps: include at least one immediate criminal-track action AND at least one immediate civil-track action when both branches are live.
- When the facts only support one branch (purely civil OR purely criminal), stay in that branch — do NOT manufacture the other side just for balance. Coherence beats balance.
- **No bracket tags in user-facing output.** Do NOT prefix or label items with "(criminal angle)", "(civil angle)", "(criminal-law angle)", "(civil-law angle)", or any similar bracketed tag. Weave both threads into natural prose / bullet text the way a lawyer would draft them in a memo.

MULTIPLE MATTERS:
If documents appear to include multiple matters, explicitly mention ambiguity and focus your output on the dominant dispute supported by strongest evidence.

INSUFFICIENT DATA:
If documents do not contain enough information, explicitly state the limitation in caseSummary, reportCaseSummary, and strategicObjective. Do NOT guess facts.

OUTPUT (JSON only, complete all fields):
```json
{{
  "caseSummary": "This case is about... (facts, parties, core legal issues)",
  "reportCaseSummary": "This matter is about... (same facts/coverage as caseSummary; neutral expanded narrative; no Petitioner/Respondent/Client/Case/Adjudicator/Mediator labels)",
  "strategicObjective": "Role-tailored strategic objective for {workspace_role} (facts, priorities, evidence support/limits)",
  "possibleNextSteps": [
    "Immediate action 1",
    "Immediate action 2",
    "Immediate action 3",
    "Immediate action 4",
    "Immediate action 5"
  ],
  "suggestionChips": [
    "Chip 1", "Chip 2", "Chip 3", "Chip 4", "Chip 5", "Chip 6", "Chip 7", "Chip 8",
    "Chip 9", "Chip 10", "Chip 11", "Chip 12", "Chip 13", "Chip 14", "Chip 15", "Chip 16"
  ]
}}
```

suggestionChips: exactly 15-16 short, actionable next-step labels (2-5 words each) specific to this case and role. Examples: "Review Title Deeds", "Map Legal Heirs", "Draft Demand Notice", "File FIR", "Preserve Digital Evidence". No full sentences. Provide 15-16 varied chips.

CRITICAL: Output complete, valid JSON only. Do not truncate; close every string with \", close every array with ], and make sure the root JSON object is fully closed. Keep caseSummary, reportCaseSummary, and strategicObjective under 4500 characters each AND target 10-20 sentences each — use the full budget to include every named party, every suit/document/property identifier, every exact amount (in the same notation as the documents), every significant date, and every named statute reference. Do NOT compress to a short summary; do NOT pad with generic boilerplate. Ensure caseSummary starts exactly with "This case is about..." and reportCaseSummary starts exactly with "This matter is about...". In possibleNextSteps strings, never use "Document" or "document" to mean an uploaded file—use Annexure N or annexure/annexures."""
    )


def generate_strategies_from_documents_prompt3(*, num_annexures):
    return (
"AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    f"- The user has uploaded exactly {num_annexures} annexures.\n"
                    f"- The ONLY valid annexure references are: Annexure 1 through Annexure {num_annexures}.\n"
                    f"- You MUST NOT reference Annexure {num_annexures + 1} or any higher number.\n"
                    "- Do NOT invent additional annexures."
    )


def generate_strategies_from_documents_prompt4(*, _entities_block_recovery, _fields_json, _rules_block, case_summary, combined_document_content, strategic_objective, workspace_role):
    return (
f"""Return ONLY one valid JSON object with these keys:
{{
  {_fields_json}
}}

Context:
- Role: {workspace_role}
- Case summary: {case_summary}
- Strategic objective (existing, may be empty/short): {strategic_objective}

EXTRACTED FACTS (authoritative ground truth — use exact names/spellings, do NOT rename or abbreviate):
{_entities_block_recovery}

Documents:
{combined_document_content}

Rules:
{_rules_block}
- Use "Annexure N" wording for uploaded files (never "Document N").
- No invented facts; only use what is in the EXTRACTED FACTS and Documents above.
- JSON only; no markdown fences.
"""
    )


def generate_strategies_from_documents_prompt5(*, _report_case_summary, case_summary, strategic_objective):
    return (
"\n\nRewrite all THREE fields so EVERY missing "
                                    "item above appears in the prose. Do NOT introduce "
                                    "new facts beyond what is needed to incorporate "
                                    "these. Do NOT delete any existing facts. Keep the "
                                    "length budget (under 4500 characters each). Keep "
                                    "the openers EXACTLY: caseSummary must start with "
                                    "'This case is about...'; reportCaseSummary must "
                                    "start with 'This matter is about...'.\n\n"
                                    "Return ONE JSON object with exactly these three "
                                    "keys: caseSummary, reportCaseSummary, "
                                    "strategicObjective. No other keys, no "
                                    "commentary, no markdown.\n\n"
                                    "ORIGINAL caseSummary:\n"
                                    f"{case_summary}\n\n"
                                    "ORIGINAL reportCaseSummary:\n"
                                    f"{_report_case_summary}\n\n"
                                    "ORIGINAL strategicObjective:\n"
                                    f"{strategic_objective}\n"
    )


def generate_strategies_from_documents_prompt6(*, workspace_role):
    return (
"- strategicObjective: 10-20 sentences. "
                                        f"Role-specific for {workspace_role}. "
                                        "Anchor on named parties from the "
                                        "EXTRACTED FACTS, exact dates in "
                                        "DD/MM/YYYY, suit/FIR numbers, exact "
                                        "amounts, and named statutes "
                                        "(BNS/BNSS/BSA/IT Act/NDPS/etc.). Do "
                                        "NOT use Petitioner / Respondent / "
                                        "Client / Case / Adjudicator / "
                                        "Mediator labels. Cover both "
                                        "criminal-law and civil-law tracks "
                                        "when both are supported by the "
                                        "facts. No bracket tags like "
                                        "'(criminal angle)'."
    )


def regenerate_strategy_objective_preserving_case_summary_prompt(*, SPEAKER_LABEL_GUARDRAIL, case_summary, combined_document_content, current_strategy, report_case_summary, role, user_objective_instruction):
    return (
f"""You are a legal strategy analyst for Indian law.

The following case summaries are FIXED. Do NOT change, paraphrase, or contradict them.

LOCKED caseSummary (verbatim context):
{case_summary}

LOCKED reportCaseSummary (verbatim context):
{report_case_summary or "(same facts as case summary; neutral report narrative)"}

**User's new strategic priority / instruction** (follow this as the primary constraint):
{user_objective_instruction.strip()}

Current strategic objective (to replace):
{(current_strategy.get("strategicObjective") or "").strip()}

Current possible next steps (to replace):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

**Role**: {role}

**Document content** (for fact consistency only; do not rewrite the case summaries):
{combined_document_content}

Return JSON ONLY with these keys:
{{
  "strategicObjective": "5-6 sentences, role-tailored for {role}, aligned with the user's instruction and the case facts",
  "possibleNextSteps": ["3-5 concrete immediate actions; use Annexure N for uploads, never Document N"],
  "suggestionChips": ["15-16 short labels, 2-5 words each, actionable"]
}}

Rules:
- Do not restate or replace caseSummary/reportCaseSummary in the output.
- The new strategicObjective and possibleNextSteps MUST reflect the new user instruction and MUST NOT just repeat the current objective/steps.
- BNS/BNSS/BSA naming (not IPC/CrPC/IEA where replaced).
- No invented facts; if the instruction conflicts with documents, note the tension in strategicObjective.
- {SPEAKER_LABEL_GUARDRAIL}
- JSON only; complete strings and arrays."""
    )


def regenerate_case_summary_with_feedback_prompt(*, SPEAKER_LABEL_GUARDRAIL, combined_document_content, current_case_summary, current_report_case_summary, current_strategy, role, user_feedback):
    return (
f"""You are a legal-strategy editor for Indian law.

You will edit the existing Case Summary and Report Case Summary based on the user's instruction below. The Strategic Objective and Possible Next Steps are LOCKED and must NOT change.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{user_feedback.strip()}

CURRENT caseSummary (you must produce an edited version of this text after applying the user's instruction):
{current_case_summary}

CURRENT reportCaseSummary (apply the SAME edit; keep it a neutral expanded narrative — no role labels like Petitioner/Respondent/Client/Case/Adjudicator/Mediator):
{current_report_case_summary or current_case_summary}

LOCKED strategicObjective (verbatim context, do not output):
{(current_strategy.get("strategicObjective") or "").strip()}

LOCKED possibleNextSteps (verbatim context, do not output):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

**Role**: {role}

**Document content** (for fact consistency only; do NOT invent named parties, dates, amounts, suit numbers, or statutes that are not in this content):
{combined_document_content}

EDIT-INSTRUCTION RULES (CRITICAL — override all preservation rules below when they conflict):
1. The user's instruction OUTRANKS the "preserve every named party / amount / date / statute" rule. If the user says "remove" / "delete" / "drop" / "change" a specific sentence, paragraph, fact, or phrase, you MUST apply that change to the text — even if it means dropping a named party, an amount, or a date.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced — including role labels like "the Petitioner" — even if the name does not already appear in the text. A literal matching token is NOT required.
2. If the user QUOTES exact text inside the instruction (anything that looks like a verbatim sentence or phrase from the current case summary), find that exact text in the CURRENT caseSummary above and REMOVE it (and apply the same removal to reportCaseSummary if the same content appears there). Do not "interpret" — match the surface text.
3. Apply ONLY the edits the user asked for. Do NOT remove other content the user did not mention.
4. If the user's instruction asks for something impossible (e.g. add a new dated fact not in the documents), ignore that invention and apply only the parts of the instruction that are removals / rephrases / valid edits.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise from screenshots, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Return JSON ONLY with these two keys:
{{
  "caseSummary": "edited case summary; MUST start EXACTLY with 'This case is about...'; reflect the user's instruction; preserve everything else from CURRENT caseSummary that the user did NOT ask to change",
  "reportCaseSummary": "edited report case summary; MUST start EXACTLY with 'This matter is about...'; mirror the caseSummary facts after the edit; neutral expanded narrative; do NOT use the words Petitioner/Respondent/Client/Case/Adjudicator/Mediator"
}}

Output rules:
- Do not output strategicObjective, possibleNextSteps, or suggestionChips.
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do not switch to IPC/CrPC/Evidence Act.
- JSON only; complete strings."""
    )


def regenerate_next_steps_with_feedback_prompt(*, current_case_summary, current_strategic_objective, current_strategy, role, user_feedback):
    return (
f"""You are a legal-strategy editor for Indian law.

You will edit the existing Possible Next Steps based on the user's instruction below. The Case Summary, Report Case Summary, and Strategic Objective are LOCKED and must NOT change.

**User's edit instruction** (PRIMARY CONSTRAINT — apply EXACTLY as worded):
{user_feedback.strip()}

CURRENT possibleNextSteps (apply the user's instruction to this list; the indices are 1-based):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

CURRENT suggestionChips (refresh only as needed to stay aligned with the updated next steps):
{json.dumps(current_strategy.get("suggestionChips") or [], ensure_ascii=True)}

LOCKED caseSummary (verbatim context, do not output):
{current_case_summary}

LOCKED strategicObjective (verbatim context, do not output):
{current_strategic_objective}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL):
1. The user's instruction OUTRANKS any general guidance. If the user says "remove the 4th point", the returned list MUST omit the 4th item from CURRENT possibleNextSteps and keep the other items in their original order.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced in the steps — including role labels like "the Petitioner" — even if the name does not already appear. A literal matching token is NOT required.
2. If the user says "add a step about X", APPEND a new step about X (or insert at the position they specified) and keep all existing steps unless the user also asked to drop something.
3. If the user QUOTES an existing step verbatim and asks to remove / rephrase it, match the surface text and apply the change.
4. Do NOT silently rewrite untouched steps. Only the steps the user asked to change should differ; the rest must be reproduced word-for-word.

TRUNCATED-TEXT RULE: Never produce a token ending in "..." or "…" — those are UI-truncation noise from screenshots, not real entity names.

Return JSON ONLY with these two keys:
{{
  "possibleNextSteps": ["the resulting 3-5 next steps after applying the instruction; reference uploads as Annexure N (NEVER 'Document N')"],
  "suggestionChips": ["15-16 short labels, 2-5 words each, actionable; aligned with the updated next steps"]
}}

Output rules:
- Do not output caseSummary, reportCaseSummary, or strategicObjective.
- No invented facts (no new named parties / dates / amounts / suit numbers / statutes that are not already in caseSummary or strategicObjective).
- Use Indian-law naming (BNS/BNSS/BSA/IT Act). Do not cite IPC/CrPC/IEA.
- JSON only; complete strings and arrays."""
    )


def regenerate_strategic_objective_with_feedback_prompt(*, SPEAKER_LABEL_GUARDRAIL, current_case_summary, current_strategic_objective, current_strategy, role, user_feedback):
    return (
f"""You are a legal-strategy editor for Indian law.

You will edit the existing Strategic Objective based on the user's instruction below. The Case Summary, Report Case Summary, Possible Next Steps, and Suggestion Chips are LOCKED and must NOT change.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{user_feedback.strip()}

CURRENT strategicObjective (you must produce an edited version of this text after applying the user's instruction):
{current_strategic_objective}

LOCKED caseSummary (verbatim context, do not output):
{current_case_summary}

LOCKED reportCaseSummary (verbatim context, do not output):
{(current_strategy.get("reportCaseSummary") or "").strip()}

LOCKED possibleNextSteps (verbatim context, do not output):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

LOCKED suggestionChips (verbatim context, do not output):
{json.dumps(current_strategy.get("suggestionChips") or [], ensure_ascii=True)}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL — override every other instinct):
1. The user's instruction OUTRANKS any tendency to "improve" or "summarise" the strategic objective. Apply ONLY the change the user named.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced — including role labels like "the Petitioner" — even if the name does not already appear in the text. A literal matching token is NOT required.
2. If the user QUOTES exact text (a sentence, a phrase, a list of names) inside the instruction, find that exact text in CURRENT strategicObjective and REMOVE (or rephrase, per the verb the user used) ONLY that text.
3. Every other sentence, clause, and reference in CURRENT strategicObjective MUST be reproduced WORD-FOR-WORD in the output. Do NOT compress lists of entities. Do NOT drop arbitration/summary-suit/GST/cloud-suspension/leverage sentences. Do NOT change the length of the output unless the user explicitly asked for that.
4. Do NOT enforce any sentence-count or word-count target. The output length should equal the input length minus what the user removed (or plus what the user asked to add).
5. If the user asks for something impossible (e.g. inventing a fact not in the documents), ignore that invention and apply only the valid removal / rephrase parts.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Return JSON ONLY with ONE key:
{{
  "strategicObjective": "edited strategic objective; reflect the user's instruction; reproduce every other sentence from CURRENT strategicObjective verbatim"
}}

Output rules:
- Do NOT output caseSummary, reportCaseSummary, possibleNextSteps, or suggestionChips.
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do NOT switch to IPC/CrPC/Evidence Act.
- JSON only; complete string."""
    )


def summarize_objective_instruction_from_thread_prompt(*, role, thread):
    return (
f"""The lawyer chatted in informal language about how they want to steer the case. They then confirmed they want the strategy panels updated.

Summarize the NEW strategic objective / priority they want (venue, relief, procedural focus, or emphasis), in 2-8 sentences. Use ONLY what appears in the thread; do not invent facts. If the thread does not support a concrete objective, use an empty string.

Workspace role: {role}

Thread:
{thread}

Reply JSON only: {{"objectiveText": "..."}}"""
    )


def should_offer_objective_update_after_discussion_prompt(*, ar, um):
    return (
f"""Classify whether the USER message suggests a possible NEW strategic priority or direction for their legal case (e.g. different court/venue, focus on one remedy, shift emphasis, change timing), in informal or indirect wording — even if they do NOT ask to "update the objective" or "change strategy".

Return JSON only: {{"offer": true or false}}

Rules:
- offer=true: user is discussing or leaning toward a different strategic emphasis that could change the formal strategic objective; includes informal / "lemon language".
- offer=false: pure legal information question, greeting, document request, or no strategic repositioning.

User message:
{um[:4000]}

Assistant reply (for context, first 1200 chars):
{ar[:1200]}"""
    )


def llm_classify_objective_change_intent_prompt(*, msg):
    return (
f"""You are a STRICT intent classifier for a legal strategic-objective workflow.

Your job is to determine whether the user is:
1. DEFINING or CHANGING a strategy/objective (actionable intent), OR
2. EXPLORING / ASKING / ANALYZING (non-actionable intent)

--------------------------------
OUTPUT FORMAT (JSON ONLY)
--------------------------------
{{
  "mode": "direct_update" | "ask_confirmation" | "none",
  "objectiveText": "normalized objective text or empty",
  "isQuestionLike": true or false,
  "updateRequestedNow": true or false
}}

--------------------------------
CORE PRINCIPLE
--------------------------------
"direct_update" is ONLY allowed when the user clearly intends an IMMEDIATE strategy/objective update.

If immediate intent is missing -> DO NOT use direct_update.

--------------------------------
STEP-BY-STEP DECISION LOGIC
--------------------------------

STEP 1: INFORMATIONAL QUESTION DETECTION (HIGHEST PRIORITY)
If the user is REQUESTING INFORMATION — asking you to explain, describe, analyse,
list, compare, summarise, or answer a what/how/why/which question ABOUT the case,
the law, the evidence, the current objective, the risks, etc.:

-> isQuestionLike = true
-> updateRequestedNow = false
-> mode = "none"

This includes questions that merely MENTION the words "objective" or "strategy"
without asking to change them, e.g.:
- "explain the applicable case laws"            -> mode = none (information)
- "what is the current strategic objective?"    -> mode = none (information)
- "how does the objective help the petitioner?" -> mode = none (information)
- "which laws apply to this dispute?"            -> mode = none (information)

CRITICAL EXCEPTION — a question FORM that is actually an INSTRUCTION to change the
objective is NOT an informational question. If the user, even politely or as a
question, asks you to UPDATE / CHANGE / REVISE / SET / REWRITE the objective or
strategy, treat it as an update:
- "can you update the objective to focus on bail?"   -> mode = direct_update
- "please change the strategy to file in Patna only" -> mode = direct_update
- "could you set the objective to recover Rs 12,000?" -> mode = direct_update
The test is INTENT, not punctuation: information requested -> none; a change to the
objective requested -> direct_update (or ask_confirmation if tentative).

--------------------------------

STEP 2: EXPLORATION / TENTATIVE INTENT
If the user is suggesting, brainstorming, or unsure:

Examples:
- "should we focus on..."
- "maybe we can..."
- "what if we..."
- "we could consider..."

-> mode = "ask_confirmation"
-> updateRequestedNow = false
-> isQuestionLike = true or false (based on phrasing)

--------------------------------

STEP 3: DIRECT STRATEGY / OBJECTIVE UPDATE (STRICT)
Classify as "direct_update" ONLY if ALL conditions are satisfied:

A. The statement is DECLARATIVE (not a question)
B. The intent is FINAL (not exploratory)
C. The user is clearly choosing a direction
D. The user expects this change to be APPLIED NOW

--------------------------------
STRATEGY CHANGE RULE (CRITICAL)
--------------------------------

A strategy shift MUST be treated as "direct_update" if it is decisive and immediate,
even if the user does NOT explicitly say "update objective".

This includes statements like:
- "Focus on X instead of Y"
- "Drop X and pursue Y"
- "We will proceed with X"
- "Shift strategy to X"
- "Prioritize X over Y"

If the above statements are:
- Declarative
- Final (not tentative)
- Actionable immediately

-> mode = "direct_update"
-> updateRequestedNow = true
-> isQuestionLike = false

--------------------------------
DECLARATIVE OBJECTIVE STATEMENTS (CRITICAL — VERY COMMON)
--------------------------------

When the lawyer DECLARES the objective using phrasing like:
- "objective is <X>"
- "the objective is <X>"
- "our objective is <X>"
- "client objective is <X>" / "client's objective is <X>"
- "the client's goal/aim is <X>"
- "objective should be <X>" / "the objective should be <X>"
- "the objective of the case is <X>"
- "set the objective to <X>" / "set objective: <X>"
- "objective: <X>"
- "we want <X>" / "we are seeking <X>" (declarative, not exploratory)

…this is a DECLARATIVE STATEMENT OF THE STRATEGIC OBJECTIVE.

It is NOT an explanation, NOT informational, and NOT exploratory.
You MUST classify it as:
-> mode = "direct_update"
-> updateRequestedNow = true
-> isQuestionLike = false
-> objectiveText = the concrete content of <X> (the part AFTER "is" / ":" / "to")

Counter-examples that are NOT declarative objective statements
(these remain mode = "none"):
- Meta-talk about the concept: "the objective of a writ petition is generally to..."
  (talking ABOUT what an objective is, not setting one)
- Generic teaching: "in cases like this, the objective is usually to ..."
  (describing typical practice, no first-person commitment)
- Question forms: "what should the objective be?", "is the objective to ...?"
- Hypotheticals: "if the objective were X, then ..."

Heuristic to disambiguate:
- First-person / case-specific subject ("our", "client", "the case", "we")
  + "objective"/"goal"/"aim" + "is"/"should be"/":"/"to"
  -> DECLARATIVE -> direct_update
- Third-person / generic subject ("the objective of <concept>", "an objective is")
  + descriptive verb -> EXPLANATORY -> none

--------------------------------
HARD CONSTRAINTS (MANDATORY)
--------------------------------

1. NEVER set mode = "direct_update" if updateRequestedNow = false

2. If isQuestionLike = true:
   -> mode MUST be "none"

3. Analytical or informational requests ALWAYS -> mode = "none"

Examples:
- "identify risks"
- "explain strategy"
- "list options"
- "summarize"
- "compare approaches"
- "what should we do?"

4. If the intent is unclear -> default to "none"

--------------------------------
OBJECTIVE TEXT RULE
--------------------------------

- Extract ONLY when mode != "none"
- Keep it SHORT, CLEAR, and ACTION-ORIENTED
- Do NOT add new information
- Do NOT hallucinate or expand meaning
- For declarative statements ("objective is X", "client objective is X",
  "objective: X", "set the objective to X"), the objectiveText is exactly
  the content X — strip the leading "objective is" / "the objective is" /
  "client objective is" / ":" / "to " framing.

Examples:
Input: "Focus on breach of contract instead of negligence"
Output: "Focus on breach of contract over negligence"

Input: "Client objective is to obtain regular bail under Section 439 CrPC"
Output: "Obtain regular bail under Section 439 CrPC"

Input: "the objective is to challenge the territorial jurisdiction"
Output: "Challenge the territorial jurisdiction"

Input: "objective: secure interim injunction before next hearing"
Output: "Secure interim injunction before next hearing"

--------------------------------
FINAL INSTRUCTION
--------------------------------

Return ONLY valid JSON.
No explanation.
No extra text.

--------------------------------
USER MESSAGE
--------------------------------
{msg[:5000]}
"""
    )


def llm_classify_panel_edit_intent_prompt(*, msg, scoped_panel_rule):
    return (
f"""You are a STRICT intent classifier for a legal-strategy workspace chat.

The user is viewing a workspace with FIVE editable panels:
- CASE SUMMARY (caseSummary): a 10-20 sentence factual narrative of the case.
- REPORT CASE SUMMARY (reportCaseSummary): the same facts rewritten as a neutral, expanded narrative for formal reports.
- STRATEGIC OBJECTIVE (strategicObjective): the role-tailored strategic plan, 10-20 sentences.
- POSSIBLE NEXT STEPS (possibleNextSteps): 3-5 immediate actionable bullets.
- ANALYSIS REPORT (analysisReport): the case analysis — strengths, weaknesses, risks, missing information, and score explanations.

Your job: decide whether the user is DIRECTLY asking to edit one or more of these panels, and split the user's instruction into per-panel chunks.

USER MESSAGE:
\"\"\"{msg}\"\"\"

OUTPUT FORMAT (JSON ONLY):
{{
  "caseSummary":        {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for case summary; empty when shouldEdit=false"}},
  "reportCaseSummary":  {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for report case summary; empty when shouldEdit=false"}},
  "strategicObjective": {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for strategic objective; empty when shouldEdit=false"}},
  "possibleNextSteps":  {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for possible next steps; empty when shouldEdit=false"}},
  "analysisReport":     {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for the analysis report; empty when shouldEdit=false"}},
  "listOfDates":        {{"shouldEdit": true | false, "instruction": "set true ONLY for a NAME change / rename (Rule 12) so the person's name is replaced in the existing dated events; empty otherwise"}},
  "isRename":           true | false,
  "isQuestionLike":     true | false
}}
("isRename" = true ONLY for a workspace-wide NAME change / rename / name-assignment per Rule 12; false for every other kind of edit.)

DECISION RULES:

1) QUESTIONS / EXPLANATIONS / GREETINGS → all four shouldEdit = false, isQuestionLike = true.
   Examples: "what laws apply?", "explain the case summary", "hi", "can you describe this?",
   "is X applicable?", "tell me about ...".

2) EDIT VERBS that trigger shouldEdit when paired with a panel name:
   remove, delete, drop, change, replace, edit, modify, rewrite, add, append,
   insert, expand, shorten, rephrase, fix, correct, update.

3) PANEL NAME RECOGNITION (tolerate typos and case):
   - "case summary", "summary", "the summary"                → caseSummary
   - "report case summary", "report summary"                 → reportCaseSummary
   - "strategic objective", "stategic objective" (typo),
     "objective", "strategy"                                 → strategicObjective
   - "possible next steps", "next steps", "next step",
     "action items", "steps"                                 → possibleNextSteps
   - "analysis report", "analysis", "the report", "report",
     "strength", "strengths", "weakness", "weaknesses",
     "risk", "risks", "missing info", "missing information"  → analysisReport

4) "client objective is to ..." / "the objective is to ..." / "we want to ..." PREFIX RULE (CRITICAL):
   These phrases are NOT a strategic objective declaration when they are followed by an edit verb plus a panel name.
   Example: "client objective is to remove sentence X from case summary" means
   caseSummary.shouldEdit=true, instruction="Remove sentence X from case summary".
   Do NOT treat the wrapping phrase as a strategic objective change.

   NEW-OBJECTIVE DECLARATION → NOT A SURGICAL EDIT (CRITICAL):
   When the user DECLARES or CHANGES the strategic objective to a NEW DIRECTION — e.g.
   "objective is to <direction>", "the objective is to <direction>", "client objective is <direction>",
   "the objective should be <direction>", "change/set/update the objective to <new direction>",
   "we want to pursue <direction>" (e.g. "objective is to focus on recovering ₹12,000 first",
   "change the objective to quashing the false FIR") — this is NOT a surgical panel edit.
   Set strategicObjective.shouldEdit=FALSE and isQuestionLike=false. A separate objective-change
   flow regenerates the whole strategy + analysis from the new objective; this classifier must
   NOT claim it.
   Set strategicObjective.shouldEdit=TRUE ONLY for a SURGICAL edit to the EXISTING objective TEXT —
   remove / delete / drop / rephrase / replace a specific sentence, phrase, or quoted text in the
   objective — or for a workspace-wide rename (see Rule 12).
   Worked contrasts:
     - "objective is to prioritise custody of the son"          -> strategicObjective.shouldEdit=FALSE (new direction → objective-change flow)
     - "change the objective to focus on quashing the FIR"       -> strategicObjective.shouldEdit=FALSE (new direction → objective-change flow)
     - "remove the jurisdiction line from the objective"         -> strategicObjective.shouldEdit=TRUE  (surgical text edit)
     - "rephrase the second sentence of the objective"           -> strategicObjective.shouldEdit=TRUE  (surgical text edit)
     - rename "Replace 'Wife' with 'Sandhya'" (Rule 12)          -> strategicObjective.shouldEdit=TRUE  (rename)

5) SPLIT MULTI-PANEL MESSAGES INTO PER-PANEL INSTRUCTIONS. CRITICAL.
   Each shouldEdit=true field's `instruction` MUST contain ONLY the user's edit text for THAT panel — not the wrapping phrase, not the other panels' edits.
   Use the surrounding "from case summary" / "in the objective" / "from possible next steps" clauses to attribute each sub-instruction.
   Worked example for a three-panel message:
     INPUT: "client objective is to remove this sentence 'AAA AAA AAA' from case summary and 'BBB BBB BBB' remove this from strategic objective and remove 4th point from possible next steps"
     OUTPUT:
     {{
       "caseSummary":        {{"shouldEdit": true,  "instruction": "Remove this sentence from the case summary: AAA AAA AAA"}},
       "reportCaseSummary":  {{"shouldEdit": false, "instruction": ""}},
       "strategicObjective": {{"shouldEdit": true,  "instruction": "Remove this from the strategic objective: BBB BBB BBB"}},
       "possibleNextSteps":  {{"shouldEdit": true,  "instruction": "Remove the 4th point from possible next steps"}},
       "isQuestionLike":     false
     }}

6) WHEN THE USER QUOTES EXACT TEXT TO REMOVE/CHANGE — preserve that quoted text verbatim inside the per-panel `instruction`.

7) `instruction` MUST be non-empty whenever shouldEdit=true. If you cannot extract a clean per-panel instruction with the targeted text, set shouldEdit=false for that panel instead of guessing.

8) AMBIGUITY: if the user says "remove this" / "the response" / "that paragraph" without naming a panel, set ALL four shouldEdit=false. The chat agent will ask for clarification.

9) NEVER trigger an edit when the message is a question, even if a panel name appears.
   "Explain the objective" → strategicObjective.shouldEdit = false, isQuestionLike = true.

10) FACT DISCUSSION WITHOUT EDIT VERB: if the message is only discussing facts
    ("I think the date was actually 16/05/2026") with NO imperative edit verb, set all five shouldEdit=false.

12) WORKSPACE-WIDE RENAME / FIND-AND-REPLACE (HIGHEST PRIORITY — OVERRIDES RULES 8 AND 11):
    When the user asks to correct / replace / change / rename / fix a specific WORD, NAME, TERM,
    DATE, or PHRASE "X" with/to "Y" — e.g. "correct the name Rambur with rambir", "change Rambur to
    Rambir in the workspace", "replace 19/04/2024 with 18/04/2024 everywhere", "fix the spelling of
    Sandhya" — this is a workspace-wide rename. It is NOT scoped to a single panel even when the chat
    is scoped (rule 11) and even if no panel is named.
    Set shouldEdit=true for ALL SIX panels — caseSummary, reportCaseSummary, strategicObjective,
    possibleNextSteps, analysisReport, AND listOfDates — and give EACH the SAME instruction in
    the form:
      Replace every occurrence of 'X' with 'Y' wherever it appears; change nothing else.
    (Substitute the actual X and Y. Preserve the user's exact spelling/casing for X and Y.)
    listOfDates is included ONLY for a rename / name change (so the person's name is replaced in
    the existing dated events) — never for any other kind of edit.
    isQuestionLike=false. Only a genuine question (no edit verb) stays isQuestionLike=true.

    NAME-ASSIGNMENT sub-case (a party identified by ROLE / DESCRIPTION / GENDER, not a literal
    existing token): when the user assigns or corrects the NAME of a party who is described
    rather than named — e.g. "the female employee's name is heena", "the petitioner is named X",
    "the complainant is heena because she is female" — DO NOT emit a bare literal "Replace 'X'
    with 'Y'" (the party may currently appear only as a role label like "the Petitioner" / "the
    employee" / "she", which a literal find-and-replace would miss). Instead set shouldEdit=true
    for ALL SIX panels (including listOfDates) with a SEMANTIC instruction that preserves the
    descriptor and any gender clue, e.g.:
      Name the female employee (the female party, currently referred to as "the Petitioner" / "the
      employee" / "she") "heena" wherever she is referenced — including role labels; do not require
      a literal matching token; change nothing else.

    For BOTH the literal-rename and the name-assignment sub-case, also set "isRename": true. For
    every other kind of edit (line/word add/update/delete, rephrase, remove a sentence), set
    "isRename": false.
{scoped_panel_rule}
Return JSON only — no markdown, no commentary."""
    )


def llm_classify_panel_edit_intent_prompt2(*, _PANEL_LABELS, default_panel):
    return (
"\n11) SCOPED PANEL DEFAULT (CRITICAL — OVERRIDES RULE 8): "
                    f"This chat is scoped to the {_PANEL_LABELS[default_panel]} panel. "
                    "If the message contains an edit verb that operates on the EXISTING panel "
                    'TEXT but names NO panel (e.g. "change the wordings", "add a few words", '
                    '"make it shorter", "rephrase it", "remove the 2nd line"), attribute the '
                    f"edit to {default_panel}.shouldEdit=true with the user's full edit text as "
                    "the instruction. Rule 8 still applies when the user explicitly names a "
                    "DIFFERENT panel. Genuine questions still classify as isQuestionLike=true. "
                    "EXCEPTION: a statement that DECLARES a NEW strategic objective / direction "
                    "(see Rule 4's NEW-OBJECTIVE DECLARATION clause) is NOT a surgical edit — do "
                    "NOT apply this scoped default to it; leave strategicObjective.shouldEdit=false "
                    "so the objective-change flow handles it.\n"
    )

