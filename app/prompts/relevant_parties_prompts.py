"""Prompt builders for the relevant_parties feature (extracted from handlers, verbatim)."""


__all__ = [
    "classify_relevant_parties_prompt",
]


def classify_relevant_parties_prompt(*, evidence_snippet, parties_blob, workspace_role):
    return (
f"""
You are classifying the PARTIES already extracted from an Indian legal matter
into a fixed role vocabulary, for a "Relevant Parties" panel.

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
- Keep every distinct party; do not drop anyone.

Workspace role: {workspace_role}
Case context (for classification only — do NOT extract new names from it):
{evidence_snippet[:3000]}

EXTRACTED PARTIES:
{parties_blob}
"""
    )

