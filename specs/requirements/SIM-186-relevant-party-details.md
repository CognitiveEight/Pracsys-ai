# SIM-186: Relevant Parties shows each party's address, email, phone and masked ID numbers

- **Jira:** SIM-186
- **Owner:** product owner
- **Status:** approved
- **Target branch flow:** feature/SIM-186-relevant-party-details → development → staging → main

## Problem
The Relevant Parties panel lists each party's name, role and one sentence.
`contactNumber` and `email` exist but are always empty unless the lawyer types
them in, and there is no address or identity number at all. The lawyer has to
search every document by hand for each party's address, email, phone and IDs
(Aadhaar, PAN, passport, etc.).

## Goal
Every party in the panel carries the address, email, phone and identity
numbers the evidence gives for that party, with every identity number masked
(last 4 characters visible at most).

## Scope
- In:
  - extraction of address, emails, phones and identity numbers per party,
    copied verbatim from that party's own source document;
  - deterministic verification against the source text (nothing invented);
  - identity numbers always masked on every emit path;
  - user-entered values win over extracted ones;
  - add/edit accept `address` and `identifiers`; chat sees the details.
- Out:
  - unmasked identity numbers (`sensitiveFields` stays backend-only);
  - masking phones or emails (kept in clear by the existing PII policy);
  - joining `documentAssessments[].extractedFields` onto parties.

## Users and surfaces
`relevant_parties` panel (`document_feature_response`), `add_relevant_party`,
`edit_relevant_party`, relevant-parties chat context.

## Constraints
- [x] Changes a socket payload — additive fields `address`, `identifiers`;
      backend/frontend must render them
- [ ] Role-dependent behaviour
- [x] Touches personal data — identity numbers masked; no values logged
- [ ] Adds a paid call — no new call; the existing per-document extraction
      returns more fields
- [ ] Indian-law output
- [ ] Shared workspaces

## Success measure
The `relevant_parties` emit carries non-empty `address` / `email` /
`contactNumber` / `identifiers` for parties whose documents state them; the
log line `relevant_parties details ...` reports the counts.

## Open questions
None.
