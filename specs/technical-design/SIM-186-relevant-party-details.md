# SIM-186: Relevant Parties shows each party's address, email, phone and masked ID numbers — technical design

- **Requirements:** `specs/requirements/SIM-186-relevant-party-details.md`
- **Status:** approved
- **Approver:** tech lead

## Summary
The per-document party extractor (relevant_parties path only) also returns
`address`, `emails`, `phones` and `identifiers`. Each value is verified against
that document's own text and kept only if present. The details are attached
to the classified parties by name match, so the classify model never handles
them. Identity numbers are re-masked at the emit chokepoint, so no path can
send an unmasked number.

## Affected code
| File | Change |
|---|---|
| `app/prompts/extraction_prompts.py` | `with_contact_details` flag adds the detail fields and rules |
| `app/socket/handlers/extraction.py` | `_extract_parties_subagent(..., with_contact_details=False)` |
| `app/prompts/relevant_parties_prompts.py` | `details` stays narrative; details attached separately |
| `app/socket/handlers/party_details.py` (new) | identifier types, `mask_identifier`, `normalize_identifiers`, `verify_party_details`, `merge_party_details`, `mask_party_payload` |
| `app/socket/handlers/relevant_parties.py` | collect, verify and attach details; carry them through normalize and the provenance merge |
| `app/socket/handlers/chat.py` | `_emit_document_feature` runs `mask_party_payload` for `relevant_parties` |
| `app/socket/handlers/panel_edits.py` | add/edit accept `address`, `identifiers` |
| `app/socket/schemas.py` | `PartyIdentifier`; `address`, `identifiers` on the item and requests |
| `app/langgraph/chat_nodes.py` | parties block renders the details |

## Event and payload changes
On `relevant_parties.parties[]`, aligned with the backend contract (SIM-184):
- `address`; `email`, `contactNumber` now auto-filled (several values joined with ", ")
- `sourceDocId` (earliest live evidence file the party came from, the id
  `list_of_dates` sends), `sourceDocName` (≤ 500 chars, from the stored chunk
  metadata, else the document_assessments manifest), `pageNumber` (≥ 1, page
  of the first mention, from the `--- Page N ---` markers)
- all of the above are LEFT OUT when unknown, never sent as "" (a change for
  `email` / `contactNumber`, which went out as ""); a user-added party has no
  source fields
- `identifiers: [{type, label, value, sourceDocId}]`, `value` always masked;
  not yet in the backend contract

`add_relevant_party` / `edit_relevant_party` accept optional `address` and
`identifiers: [{type, value}]`. A frontend/backend ticket is needed to render
and send them.

## Design
- **Prompt flag.** The extractor is shared with strategy/analysis
  (`_extract_case_entities`); only relevant_parties sets the flag, so their
  prompts are unchanged.
- **Verification** (per document, against `content_by_id[doc_id]`):
  email, case-insensitive substring; phone, last 10 digits equal to a phone-like
  digit run in the text; identifier, its compact upper-case alphanumeric form
  inside the compact text (the masked form is what the stored text holds);
  address, at least 3 word tokens, ≥ 70 % present, and any 6-digit PIN present.
  Unverified values are dropped and counted.
- **Masking.** `mask_identifier` dispatches to the maskers in
  `app/processing/pii_detector.py` (idempotent on masked input) and falls back
  to keep-last-4 for GSTIN, CIN and other. A result with more than 4 visible
  digits is re-masked with keep-last-4.
- **Precedence.** Values already on the party (user-entered, or extracted
  earlier) win; new extraction fills blanks. Identifiers are de-duplicated by
  `(type, last 4)`. On a full refresh, details of matching previous parties
  are merged in first, so a lawyer's edit survives.
- **Cache.** No new cache: the details live in the RELEVANT_PARTIES cache,
  already cleared by the workspace purge.

## Checklist against CLAUDE.md
- Role gate: unchanged; the generator still runs for the resolved role.
- Long-running work: unchanged scheduling and caching; reconnect replay sends the cache.
- Loaders: unchanged.
- Emits: through `_emit_document_feature`, which now also masks.
- Model calls: no new call; the extractor call is unchanged apart from the prompt.
- Paid calls: none added (more output tokens on the extractor, metered as before).
- Purge: no new cache.
- Ingestion/deletion: n/a.
- Chat mutation gate: add/edit are panel events, not chat; unchanged.
- Logging: counts only, never values.

## Risks and rollback
- A party's counsel's or the court's address attached to the party: the
  prompt forbids it, verification cannot catch it. The lawyer can edit.
- Rollback: revert; cached entries keep the extra fields, which old code drops
  on the next regeneration.

## Test plan
`tests/test_party_details.py` (masking, verification, merge, payload masking)
and `tests/offline/verify_relevant_party_details.py` (real generator, fake
model and vector store, add/edit, logs). `make e2e` before deploy.

## Decisions
- The emit chokepoint also leaves out unknown fields, so every path follows the
  backend's "omit, never \"\"" rule.
- Masking runs at the emit chokepoint instead of inside
  `_normalize_relevant_parties_payload`, which not every path calls.
