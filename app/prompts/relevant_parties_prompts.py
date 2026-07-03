"""Prompt builders for the relevant_parties feature (extracted from handlers, verbatim)."""


__all__ = [
    "classify_relevant_parties_prompt",
]


def classify_relevant_parties_prompt(*, evidence_snippet, parties_blob, workspace_role):
    return (
f"""
You are classifying the PARTIES already extracted from an Indian legal matter
into a fixed role vocabulary, for a "Relevant Parties" panel. The panel is a
directory of the KEY people and organisations who are actually part of THIS
dispute — not a list of every name mentioned anywhere in the documents.

Return ONLY one valid JSON object with this exact shape:
{{
  "parties": [
    {{
      "name": "exact name as given below (copy VERBATIM)",
      "role": "Accused",
      "details": "One neutral sentence on this party's role in the matter."
    }}
  ]
}}

HARD RULES:
- Use ONLY the parties listed below. Copy each `name` VERBATIM. Do NOT invent,
  merge, split, or rename parties, and do NOT add anyone not listed.
- `role` MUST be exactly one of: Petitioner, Respondent, Witness, Victim,
  Accused, Other.
  Guidance: the person/state prosecuting, complaining or alleging → Petitioner;
  the person defending/charged → Accused (criminal) or Respondent (civil/
  defendant-side); the wronged person → Victim; police/expert/officer giving
  evidence → Witness; counsel, courts, commissions, boards, regulators or
  anything unclear → Other.
- `details` is ONE neutral sentence, English only, no app/brand name, no
  advocacy.
- Do NOT include contact numbers, emails, ID numbers or any other personal
  contact details — those are supplied separately by the user.

RELEVANCE FILTER — decide, for each extracted entry, whether to KEEP or DROP it:
- KEEP only parties with a direct, material role in THIS dispute:
  * the petitioner(s)/complainant(s) and the respondent(s)/accused/defendant(s);
  * the victim; witnesses actually giving evidence in the matter;
  * the parties' counsel / advocates of record;
  * the adjudicating authority, forum or registry actually deciding or party to
    the matter (e.g. the Registrar of Trade Marks where the impugned application
    was filed).
- DROP entities that appear only tangentially and are NOT party to the dispute:
  * comparative / illustrative examples (e.g. a third-party trademark holder
    cited merely to show that a term is common or generic in the trade);
  * unrelated-ecosystem names, background mentions, or predecessor / associated
    entities that are not themselves party to this matter;
  * any entry whose `role_in_matter` indicates it is CITED AS AN EXAMPLE or
    referenced in passing rather than actually involved in the dispute.
- Use each entry's `designation` and `role_in_matter` (and the case context
  below) to judge this. When genuinely unsure whether someone is a real party,
  KEEP them; only drop entries that are CLEARLY example-only or tangential.

Workspace role: {workspace_role}
Case context (for classification only — do NOT extract new names from it):
{evidence_snippet[:3000]}

EXTRACTED PARTIES:
{parties_blob}
"""
    )

