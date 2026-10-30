# SIM-174: AI service connects to the shared knowledge base

- **Jira:** SIM-174 (Task; template layout)
- **Owner:** AI service (SimplerToday)
- **Status:** draft — for review
- **Contract:** Confluence "AI brief: Knowledge base — connect the AI service to
  the shared reference shelf" (page 15466497, v1, 29/10/2026). Backend:
  pracsys-be `docs/contracts/backend-ai-socket-events.md` (knowledge-base rows),
  `docs/features/knowledge-base.md`.
- **Target branch flow:** feature/SIM-174-knowledge-base → development → staging → main

## Problem
The backend already sends `kb_document_added`, `kb_document_deleted` and
`kb_sync`, but this service has no handler for them. Documents admins add to
the shared shelf never reach the AI, so lawyers get no benefit from it. A
deletion has nothing to erase today, but once documents are held, a missed
deletion would leave data the platform has promised is erased.

## Goal
The AI holds exactly the shelf the admin panel shows, erases a deleted
document on receipt, and uses the relevant shelf documents for every case,
both in the panels built when documents are uploaded and in chat answers. They
are cited by title and never treated as the case's own documents.

## Scope
- In:
  - one platform-wide shelf keyed by `documentId`, kept apart from case evidence;
  - `kb_document_added`: fetch, read (OCR, DOCX, XLSX, PPTX, TXT), redact,
    index; the same id replaces;
  - `kb_document_deleted`: erase on receipt, including an ingest in flight;
  - `kb_sync`: a full replace; `documents: []` erases everything;
  - every case's Strategy, Analysis Report and Applicable Laws panels: the
    relevant shelf passages are added to the prompt as reference material,
    never as case evidence;
  - chat: relevant shelf passages as `[K#]`, cited with `type: "knowledge_base"`
    and the document's `title`.
- Out (explicitly not doing):
  - fact panels (List of Dates, Upcoming Dates, Relevant Parties): a
    judgment's facts must never become the case's facts;
  - drafting and the Case Laws panel (later);
  - AI → backend events (acknowledgements, a pull request for the shelf);
  - per-firm or per-lawyer filtering;
  - the global template work (SIM-58 / SIM-173).

## Users and surfaces
Inbound `kb_document_added`, `kb_document_deleted`, `kb_sync`; intelligence
chat (`user_message_request`) and its `ai_response.citations[]`.

## Constraints
- [x] Changes a socket payload (three new inbound events; a new citation
  `type` on `ai_response`, additive) — the lawyer app must render it
- [ ] Role-dependent behaviour
- [x] Touches personal data (erasure on delete, as under the DPDP Act;
  PII redaction before indexing)
- [x] Adds a paid call (OCR and embeddings at ingest; one rerank call per chat
  turn when the shelf is not empty)
- [x] Indian-law output (judgments and statutes the chat may cite)
- [ ] Shared workspaces

## Success measure
The staging checklist in the brief (§9): restart → `kb_sync` ingested; an
upload is indexed; a lawyer's question uses and cites it by title; a delete
erases it; a delete while the AI was down is erased by the next `kb_sync`;
DOCX and XLSX read; `documents: []` erases all.

## Open questions
Answers to the brief's questions are proposed in the hand-over; questions 3
(erase on first absence) and 5 (a pull event) depend on the backend's reply.
