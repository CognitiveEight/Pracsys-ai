"""Prompt builders for the workspace_metadata feature (extracted from handlers, verbatim)."""

from app.prompts.legal_intelligence_system import (
    APP_BRAND_EXCLUSION_DIRECTIVE,
    ENGLISH_ONLY_OUTPUT_DIRECTIVE,
    NAME_CAPITALISATION_DIRECTIVE,
)

__all__ = [
    "PARTY_ORDER_DIRECTIVE",
    "generate_case_summary_from_documents_prompt",
    "generate_case_summary_from_documents_prompt2",
    "generate_case_summary_from_documents_simple_prompt",
    "generate_fallback_metadata_prompt",
    "get_workspace_category_and_subcategory_prompt",
    "get_workspace_category_and_subcategory_prompt2",
]


PARTY_ORDER_DIRECTIVE = """PARTY ORDER (CRITICAL - titles are frequently reversed, do not reverse them):
- The title reads "<moving party> vs <opposing party>". The FIRST name is ALWAYS the side that
  initiated the matter: the petitioner / complainant / appellant / plaintiff / applicant / informant
  (in an FIR or police complaint, the complainant or the State prosecuting the case).
- The SECOND name is ALWAYS the side defending or answering: the respondent / accused / defendant /
  opposite party / non-applicant.
- If the document carries a cause title (lines such as "ABC ... ...Petitioner", "Versus",
  "XYZ ... ...Respondent"), COPY THAT ORDER EXACTLY. The label printed next to a name decides its
  side - never the order in which names happen to appear elsewhere in the text.
- In complaints, FIRs and legal notices the accused/opposite party is often named first or most
  prominently. That does NOT make them the first name in the title: the person who filed or sent
  the document comes first.
- Never output "<accused> vs <complainant>" or "<respondent> vs <petitioner>"."""


def generate_case_summary_from_documents_prompt(*, doc_content):
    return (
f"""You are summarizing a legal case document. The document may be in Hindi, English, or mixed languages.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

TASK: Write a SHORT CASE SUMMARY in English. Requirements:
- Exactly 3 or 4 lines. Each line = one short sentence (max 120 characters).
- Line 1: Name the ACTUAL parties (real names of people/organizations) and the SPECIFIC dispute or issue (e.g. "contract breach", "social media hacking", "defamation", "cricket association dispute").
- Line 2: State ONE or TWO concrete facts or allegations from the document (be specific, not vague).
- Line 3: State the SPECIFIC relief, investigation, or action sought (e.g. "seeks FIR", "requests damages", "demands investigation").
- Line 4 (optional): One more specific detail from the case.

FORBIDDEN PHRASES - DO NOT USE THESE EXACT WORDS:
- "Legal matter based on uploaded documents"
- "The case involves parties and issues requiring legal review"
- "The matter requires appropriate legal action"
- "Supporting documents provide further context"

You MUST extract and name the REAL parties and REAL dispute from the document. If the document mentions names like "Umesh Malik", "BCCI", "Karnataka State Cricket Association", "Shubham Malik", etc., USE THOSE NAMES. If it mentions hacking, contract issues, defamation, etc., STATE THAT SPECIFIC ISSUE.

IMPORTANT: Output exactly 3 or 4 lines. Put each sentence on its own line (use a newline after each sentence). Do not output a single paragraph.

Document content:
{doc_content}

Case summary (3-4 lines, one sentence per line):"""
    )


def generate_case_summary_from_documents_prompt2(*, doc_content):
    return (
f"""CRITICAL: Your previous response was too generic. For the SAME document below, write a SPECIFIC case summary.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

You MUST extract REAL information:
- Line 1: Find and name the ACTUAL complainant/petitioner and respondent/defendant from the document (e.g. if document says "Umesh Malik" or "BCCI" or "Karnataka State Cricket Association", USE THOSE NAMES). Then state the SPECIFIC dispute type (e.g. "social media account hacking", "contract violation", "defamation", "cricket association dispute").
- Line 2: Extract ONE concrete fact from the document (e.g. "hacked social media account", "edited videos made viral", "contract terms violated", "payment not received").
- Line 3: Extract the SPECIFIC relief sought (e.g. "seeks FIR under IT Act", "requests investigation", "demands compensation", "requests contract enforcement").
- Line 4 (optional): One more specific detail.

DO NOT use these phrases: "Legal matter based on uploaded documents", "parties and issues requiring legal review", "appropriate legal action", "Supporting documents provide further context".

Read the document carefully and extract REAL names and REAL facts. Output exactly 3 or 4 lines. Put each sentence on its own line (newline after each sentence).

Document content:
{doc_content}

Specific case summary (3-4 lines, one sentence per line):"""
    )


def generate_case_summary_from_documents_simple_prompt(*, doc_content):
    return (
f"""Summarize this legal case in exactly 3-4 short sentences in English. Name the actual parties and the actual dispute. Be specific. Put each sentence on its own line (newline after each sentence).

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

{doc_content}

Summary (3-4 lines, one sentence per line):"""
    )


def generate_fallback_metadata_prompt(*, doc_preview):
    return (
f"""From this document content, extract ACTUAL CASE DETAILS:
1. Title in strict "Petitioner vs Respondent" format. Indian-court multi-party shorthand: 2 parties on a side → "<First> & Anr"; 3+ parties → "<First> & Ors". Never join multiple parties with " & " in the final title. Use exactly " vs " (lowercase, with spaces). If only one party is identifiable, use "<Party> vs <Issue>".

{PARTY_ORDER_DIRECTIVE}

2. One legal category (CRIMINAL LAW, CIVIL LAW, CYBER LAW, etc.)
3. One relevant Indian act
4. A SHORT CASE SUMMARY in English only (3-4 lines). Summarize in your own words; do NOT copy-paste document text. Each line one short sentence.

Document: {doc_preview}

Return JSON: {{"caseTitle": "<First Petitioner> vs <First Respondent> & Anr", "caseCategory": "CIVIL LAW", "subcategories": ["Act Name"], "description": "Line 1: Actual party and issue from documents\\nLine 2: Key facts/allegations from documents\\nLine 3: Relief sought from documents\\nLine 4: Additional relevant details"}}"""
    )


def get_workspace_category_and_subcategory_prompt(*, combined):
    return (
f"""Based on the document content below, output a JSON object with ALL fields REQUIRED (no empty/null values):

- caseTitle: MUST be in format "<Petitioner-side party> vs <Respondent-side party>", petitioner side FIRST (see PARTY ORDER below). Use Indian-court multi-party shorthand: when one side has exactly 2 parties, use "<First Party> & Anr" (e.g. "Pradeep Mehra vs Ethos Limited & Anr"); when 3+ parties, use "<First Party> & Ors" (e.g. "Umesh Malik vs State of Karnataka & Ors"). Never join multiple parties with " & " in the final title — always reduce to first party + "& Anr"/"& Ors". Use exactly " vs " (lowercase, with spaces). If only one party can be identified, use "[Party] vs [Issue/Matter]". MANDATORY field.
- caseCategory: exactly one category in UPPERCASE (e.g. CRIMINAL LAW, CYBER LAW, CIVIL LAW, TRADE MARK LAW). MANDATORY - must be one of: CRIMINAL LAW, CIVIL LAW, CYBER LAW, CONTRACT LAW, PROPERTY LAW, LABOUR LAW, TAX LAW, or similar.
- subcategories: array of 1-3 relevant Indian acts (e.g. ["Bharatiya Nyaya Sanhita", "Information Technology Act, 2000", "The Trade Marks Act, 1999"]). MANDATORY - must have at least 1 act.
- description: a SHORT CASE SUMMARY in English only, exactly 3 to 4 lines (minimum 3, maximum 4). You MUST write a SPECIFIC summary for this case: name the actual parties (people or organizations) and the actual dispute. Do NOT use generic phrases like "Legal matter based on uploaded documents" or "The case involves parties and issues requiring legal review". Write in your own words; do NOT copy-paste the document. Each line one short sentence (under 120 characters). Line 1: actual parties and main issue. Line 2: key facts or allegations. Line 3: relief or action sought. Line 4 (optional): one more relevant detail. Output only the summary text, no prefixes like "Line 1:".

{PARTY_ORDER_DIRECTIVE}

Document content:
{combined}

CRITICAL: All four fields (caseTitle, caseCategory, subcategories, description) are MANDATORY. Do not leave any field empty or null.

Output ONLY this JSON, no markdown: {{"caseTitle": "Pradeep Mehra vs Ethos Limited & Anr", "caseCategory": "CIVIL LAW", "subcategories": ["Consumer Protection Act, 2019"], "description": "MLA Umesh Malik has filed a complaint regarding his son Shubham Malik's hacked social media account.\\nEdited videos and posts were made viral to defame the complainant and damage his political image.\\nThe complainant seeks a high-level investigation and strict action against the perpetrators.\\nThe complaint involves alleged violations under IT Act 2000 and requests preservation of digital evidence."}}"""
    )


def get_workspace_category_and_subcategory_prompt2(*, combined):
    return (
f"""From this document content, extract two parties or names and create a case title in format "Party1 vs Party2". If only one party, use "Party vs [Issue]".

{PARTY_ORDER_DIRECTIVE}

Document: {combined[:800]}

Return ONLY the title in format "Name1 vs Name2" or "Name vs Issue", no other text."""
    )

