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
      "role": "ACCUSED",
      "rawDesignation": "the designation/status as written in the documents",
      "description": "One neutral sentence on this party's role in the matter.",
      "sourceDoc": "first_mention value if any, else empty"
    }}
  ]
}}

HARD RULES:
- Use ONLY the parties listed below. Copy each `name` VERBATIM. Do NOT invent,
  merge, split, or rename parties, and do NOT add anyone not listed.
- `role` MUST be exactly one of: PETITIONER, RESPONDENT, COMPLAINANT, ACCUSED,
  VICTIM, WITNESS, ADVOCATE, AUTHORITY, OTHER.
  Guidance: the person/state prosecuting or alleging → COMPLAINANT/PETITIONER;
  the person defending/charged → ACCUSED (criminal) or RESPONDENT/DEFENDANT-side
  (civil → RESPONDENT); the wronged person → VICTIM; police/expert/officer giving
  evidence → WITNESS; counsel/advocate → ADVOCATE; courts, commissions, boards,
  government bodies and regulators → AUTHORITY; anything unclear → OTHER.
- `description` is ONE neutral sentence, English only, no app/brand name, no
  advocacy. `rawDesignation` keeps the document's own wording.
- Keep every distinct party; do not drop anyone.

Workspace role: {workspace_role}
Case context (for classification only — do NOT extract new names from it):
{evidence_snippet[:3000]}

EXTRACTED PARTIES:
{parties_blob}
"""
    )

