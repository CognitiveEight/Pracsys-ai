"""Prompt builders for the case_laws feature (extracted from handlers, verbatim)."""


__all__ = [
    "derive_case_law_query_prompt",
    "select_case_laws_prompt",
    "extract_judgment_digest_prompt",
    "classify_and_extract_uploaded_case_law_prompt",
]


def derive_case_law_query_prompt(*, base, objective_context, snippet):
    return (
f"""
From the matter below, produce a concise Indian legal-research search query for
finding RELEVANT CASE-LAW PRECEDENTS on Indian Kanoon.

RULES:
- Use ONLY legal doctrine, cause of action, statute/section, and remedy terms
  (e.g. "deficiency in service defective goods warranty replacement consumer
  protection"; "groundless threat trademark infringement passing off").
- EXCLUDE all party names, person names, brand/product names, and places.
- 4 to 10 words. Return ONLY JSON: {{"query": "..."}}

{("OBJECTIVE: " + objective_context.strip()) if objective_context else ""}
MATTER:
{base}
{snippet[:2000]}
"""
    )


def select_case_laws_prompt(*, objective_block, research_block, self, signals, source_blob, workspace_id, workspace_role):
    return (
f"""
You are an Indian legal research assistant selecting relevant CASE-LAW PRECEDENTS
(prior court judgments) for the matter described below.

Return ONLY one valid JSON object with this exact shape:
{{
  "caseLaws": [
    {{
      "title": "Party A v. Party B",
      "citation": "(2014) 8 SCC 273",
      "court": "Supreme Court of India",
      "date": "2014",
      "summary": "One sentence: the holding and why it matters to THIS matter.",
      "relevance": "High",
      "matchedFacts": ["fact shared with the present matter", "..."],
      "applicabilityReason": "Why this precedent applies to the present matter.",
      "distinguishing": ["a way the present matter differs from this precedent"],
      "adverseImpact": "Any way this precedent could weaken the user's position (or empty).",
      "confidence": 85,
      "sourceUrl": "https://indiankanoon.org/doc/...",
      "sourceName": "Indian Kanoon"
    }}
  ]
}}

HARD RULES (apply strictly):
- Use ONLY the numbered SOURCE RESULTS below. Each case you output MUST set
  `sourceUrl` to one of those URLs, copied VERBATIM, and `sourceName` to that
  result's source.
- Do NOT invent, guess, or "recall" citations, case names, courts, or years.
  Derive `title`/`citation`/`court`/`date` only from the SOURCE RESULTS and the
  GROUNDED RESEARCH NOTES. If unknown, use an empty string — never fabricate.
- `matchedFacts`, `applicabilityReason`, `distinguishing`, `adverseImpact`
  compare the precedent to the PRESENT matter (facts below). Be candid about
  adverse impact and distinguishing features — a lawyer needs both sides.
- `relevance` is one of High, Medium, Low. `confidence` is an integer 0-100.
- If a source is not a genuine precedent for this matter, OMIT it.
- Rank most on-point and authoritative first. Return AT MOST 6.
- If none are genuinely relevant, return {{"caseLaws": []}}.

{self._build_document_inventory(workspace_id) or "DOCUMENT INVENTORY: (no classified inventory available)"}

Workspace role: {workspace_role}
Workspace title: {signals.get("workspaceTitle","")}
{objective_block}
Case keywords: {", ".join(signals.get("keywords", []))}
Document evidence snippet:
{signals.get("evidenceSnippet","")}

{research_block}

SOURCE RESULTS (case-law search — Indian Kanoon / Vertex / web):
{source_blob}
"""
    )


def extract_judgment_digest_prompt(*, case, judgment_text, signals):
    return (
f"""
You are preparing a legal headnote/digest for an Indian judgment AND assessing
how it bears on the PRESENT MATTER described at the bottom.

PART A — extract STRICTLY from the JUDGMENT TEXT (no outside knowledge, no
fabrication; empty string / empty list if not present):
  title (the actual case name as stated in THIS text, "X v. Y" — never the
  adjudicating commission/court as a party), citation, equivalentCitation,
  court, bench[], date, areaOfLaw[], legalIssues[], materialFacts[] (3-5 short
  bullets), ratio (ratio decidendi, 1-3 sentences), holding (the actual
  disposition — allowed / dismissed / partly allowed, etc.), relief[].

PART B — assess against the PRESENT MATTER, grounded in the REAL holding above
(be candid: if the judgment was DISMISSED or cuts against the present party, say
so honestly in adverseImpact and lower the relevance/confidence accordingly):
  relevance (High|Medium|Low), confidence (0-100), matchedFacts[],
  applicabilityReason, distinguishing[], adverseImpact, summary (one sentence:
  the holding + why it matters to the present matter).

Return ONLY one valid JSON object with exactly these keys:
{{
  "title": "", "citation": "", "equivalentCitation": "", "court": "", "bench": [],
  "date": "", "areaOfLaw": [], "legalIssues": [], "materialFacts": [],
  "ratio": "", "holding": "", "relief": [],
  "legalStatus": "ONLY if the TEXT itself states overruled/affirmed/followed; else empty",
  "relevance": "High", "confidence": 80, "matchedFacts": [],
  "applicabilityReason": "", "distinguishing": [], "adverseImpact": "",
  "summary": ""
}}

Known case: title={case.get('title','')} | source={case.get('sourceUrl','')}

PRESENT MATTER:
{signals.get("evidenceSnippet", "")}

JUDGMENT TEXT:
{judgment_text}
"""
    )


def classify_and_extract_uploaded_case_law_prompt(*, judgment_text, signals):
    return (
f"""
A user has UPLOADED a document. First DECIDE whether it is a genuine case law
(a judicial judgment / court order / legal precedent). If — and only if — it is,
extract a legal digest AND assess how it bears on the PRESENT MATTER below.

STEP 1 — CLASSIFY (be strict):
- A case law is a judgment/order of a court or tribunal: it has parties, a court,
  legal issues, reasoning, and a disposition. Statutes, contracts, notices,
  invoices, resumes, coding assignments, articles, and forms are NOT case law.
- IGNORE publisher header/footer metadata. Legal-database exports (SCC Online,
  TruePrint, Manupatra, Indian Kanoon, etc.) stamp EVERY page with boilerplate
  such as "SCC Online Web Edition", "TruePrint™ source: Supreme Court Cases",
  "Printed For: ...", "Page N", and copyright lines like "Eastern Book Company
  v. D.B. Modak, (2008) 1 SCC 1". This boilerplate is NOT the document's content
  — judge "isCaseLaw" ONLY on the substantive body (case title, bench, headnotes,
  issues, reasoning, holding). NEVER reject a document as "just repeated headers"
  or "a legal database search result" merely because such boilerplate is present;
  if any judgment body exists, classify it as a case law.
- If it is NOT a case law, set "isCaseLaw": false and give a concrete, specific
  "rejectionReason" that names what the document actually is (e.g. "The uploaded
  document is a technical coding assignment titled '...'. It does not contain a
  judicial judgment, legal precedent, or any case law information."). Leave
  "caseLaw" as null.

STEP 2 — EXTRACT (only when isCaseLaw is true), STRICTLY from the DOCUMENT TEXT
(no outside knowledge, no fabrication; empty string / empty list if absent):
  title ("X v. Y" — the actual case name, never the court/commission as a party),
  citation, equivalentCitation, court, bench[], date, areaOfLaw[], legalIssues[],
  materialFacts[] (3-5 short bullets), ratio (ratio decidendi, 1-3 sentences),
  holding (the disposition — allowed / dismissed / partly allowed, etc.), relief[].

STEP 3 — ASSESS against the PRESENT MATTER, grounded in the REAL holding (be
candid: if the judgment cuts against the present party, say so in adverseImpact
and lower relevance/confidence):
  relevance (High|Medium|Low), confidence (0-100), matchedFacts[],
  applicabilityReason, distinguishing[], adverseImpact, summary (one sentence:
  the holding + why it matters to the present matter).

Return ONLY one valid JSON object with exactly these keys:
{{
  "isCaseLaw": true,
  "rejectionReason": "",
  "caseLaw": {{
    "title": "", "citation": "", "equivalentCitation": "", "court": "",
    "bench": [], "date": "", "areaOfLaw": [], "legalIssues": [],
    "materialFacts": [], "ratio": "", "holding": "", "relief": [],
    "legalStatus": "ONLY if the TEXT itself states overruled/affirmed/followed; else empty",
    "relevance": "High", "confidence": 80, "matchedFacts": [],
    "applicabilityReason": "", "distinguishing": [], "adverseImpact": "",
    "summary": ""
  }}
}}
When "isCaseLaw" is false, set "caseLaw" to null and fill "rejectionReason".

PRESENT MATTER:
{signals.get("evidenceSnippet", "")}

DOCUMENT TEXT:
{judgment_text}
"""
    )

