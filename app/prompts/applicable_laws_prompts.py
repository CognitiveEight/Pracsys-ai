"""Prompt builders for the applicable_laws feature (extracted from handlers, verbatim)."""


__all__ = [
    "regenerate_applicable_laws_with_feedback_prompt",
    "generate_applicable_laws_from_sources_prompt",
]


def regenerate_applicable_laws_with_feedback_prompt(*, current_json, instruction):
    return (
f"""You are editing a case's APPLICABLE LAWS (JSON object {{"laws": [...]}}). Apply ONLY the user's name change to the EXISTING law entries and return the FULL object.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT applicable laws (edit THIS object in place):
{current_json}

STRICT RULES:
1. This is a NAME change ONLY. Replace the renamed party's name wherever it appears in each law's "description". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
2. Keep the SAME laws in the SAME order. Do NOT add, remove, merge, or reorder laws.
3. Do NOT change "id", "act", "section", or "relevance" for any law — reproduce them EXACTLY.
4. Change nothing except the renamed name inside "description". Statute names / section numbers stay verbatim.

Return ONLY the complete, valid JSON object (no markdown code fences, no commentary)."""
    )


def generate_applicable_laws_from_sources_prompt(*, objective_block, self, signals, source_blob, targeted_acts_block, targeted_acts_count, workspace_id, workspace_role):
    return (
f"""
You are an Indian legal research assistant.
Return ONLY one valid JSON object with this exact shape:
{{
  "laws": [
    {{
      "id": "law_1",
      "act": "Act name",
      "section": "Section X",
      "description": "Why this section is potentially applicable to this case.",
      "relevance": "High"
    }}
  ]
}}

Scope of analysis — Indian civil law AND criminal law (BOTH branches):
- Treat this as a dual-lens exercise. Before finalising the list, consciously ask:
  (1) "What CRIMINAL-law provisions could the documented facts attract?" and
  (2) "What CIVIL-law provisions / private remedies could the same facts attract?"
- Where the facts plausibly support BOTH a criminal angle and a civil angle, include
  representative sections from EACH branch (balanced coverage). Do not silently drop
  one branch just because the other is more obvious.
- Where the facts only support one branch, return only that branch — do NOT invent
  the other side just to fill a quota. Coherence beats balance.

Typical Indian statutes to consider (illustrative, NOT exhaustive — any other Indian
law may be cited if the facts and sources support it):
- Criminal-law side (preferred citations):
  * Bharatiya Nyaya Sanhita, 2023 (BNS) — substantive offences
  * Bharatiya Nagarik Suraksha Sanhita, 2023 (BNSS) — criminal procedure
  * Bharatiya Sakshya Adhiniyam, 2023 (BSA) — evidence
  * Information Technology Act, 2000 (cyber-offence sections)
  * Negotiable Instruments Act, 1881 (Section 138 — dishonour of cheque, quasi-criminal)
  * PMLA 2002, NDPS 1985, Arms Act 1959, POCSO 2012, Dowry Prohibition Act 1961, PCA 1988, etc., where facts fit.
- Civil-law side:
  * Indian Contract Act, 1872 (breach, misrepresentation, indemnity, agency)
  * Specific Relief Act, 1963 (specific performance, injunctions, declarations)
  * Transfer of Property Act, 1882 (sale, mortgage, lease, gift disputes)
  * Code of Civil Procedure, 1908 (suit / appeal / execution-related provisions)
  * Limitation Act, 1963 (time-bar issues)
  * Sale of Goods Act, 1930 (goods-quality / passing-of-property disputes)
  * Consumer Protection Act, 2019 (deficiency in service, unfair trade practice)
  * Companies Act, 2013 (oppression, mismanagement, director duties, NCLT)
  * Arbitration and Conciliation Act, 1996 (where there is an arbitration clause)
  * Hindu / Muslim / Special Marriage / Succession laws, family law statutes (matrimonial / succession / guardianship matters)
  * RERA 2016, Rent Control statutes, MV Act 1988 (compensation), CGST Act 2017, etc., where facts fit.

Hard rules:
- Do NOT cite the **Indian Penal Code, 1860 (IPC)**. If a provision is IPC-linked, map it to the corresponding **BNS, 2023** section instead.
- Do NOT cite the **Code of Criminal Procedure, 1973 (CrPC)**. If procedural, map it to the corresponding **BNSS, 2023** section instead.
- Do NOT cite the **Indian Evidence Act, 1872**. If an evidence rule is needed, use the corresponding **BSA, 2023** section instead.
- Do NOT restrict to only the lists above; ANY Indian statute (central or state) is allowed when it actually governs the fact pattern.
- **Grounding (any type of matter):** Every row must be justified only from (a) the document excerpt below and/or (b) the numbered Indian Kanoon search lines. Do **not** invent or guess Act/Section numbers; the statutory label you output must match the **legal wrong or remedy** actually discussed in those sources.
- **Coherence:** The offence, liability, or remedy described in your `description` must match what that Act and Section actually govern. If you cannot tie the fact pattern to the section with the sources in hand, **omit that row** rather than stretching unrelated provisions.
- In each `description`, briefly show the link (e.g. a concrete fact from the documents, or "Search line N …" referring to one of the numbered results).
- **No bracket tags or "angle" labels in `description`.** Do NOT prefix the description with "Criminal angle:", "Civil angle:", "Quasi-criminal angle:", "(criminal angle)", "(civil angle)", or any similar tag. The `act` field already makes the branch obvious to the lawyer; keep `description` as plain case-specific prose.
- Relevance must be one of: High, Medium, Low.
- Keep description concise and case-specific.
- If evidence is insufficient, return {{"laws":[]}}.

TARGETED INDIAN ACTS — strongly preferred universe ({targeted_acts_count} acts):
The product targets the following curated list of Indian statutes / codes / rules. **Every `act` value you emit MUST be drawn from this list whenever the fact pattern is governed by any act on the list.** Match the act name as closely as possible to the wording shown here.

If — and only if — a fact in the documents is clearly governed by an Indian law that is NOT on this list (rare), you may include it, provided (a) the source documents or numbered Indian Kanoon results explicitly support it, and (b) you are NOT citing IPC 1860, CrPC 1973, or the Indian Evidence Act 1872 (use BNS 2023 / BNSS 2023 / BSA 2023 instead).

{targeted_acts_block}

{self._build_document_inventory(workspace_id) or "DOCUMENT INVENTORY: (no classified inventory available)"}

Workspace role: {workspace_role}
Workspace title: {signals.get("workspaceTitle","")}
{objective_block}
Search query: {signals.get("query","")}
Case keywords: {", ".join(signals.get("keywords", []))}
Document evidence snippet:
{signals.get("evidenceSnippet","")}

Indian Kanoon / case-law search results:
{source_blob}
"""
    )

