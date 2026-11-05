---
title: Knowledge base
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-29
jira: SIM-174
related_flows:
  - Chat question to AI answer
other_side:
  backend: pracsys-be docs/features/knowledge-base.md
related_specs:
  - specs/requirements/SIM-174-knowledge-base.md
---

# Knowledge base

## In plain words

PracSys has one shared shelf of legal reference documents: judgments,
statutes, court formats, precedents and model documents. Super administrators
add and remove them in the admin panel; lawyers never see the shelf directly.
The AI keeps its own copy of the shelf in step with the admin panel and uses
it for every case. When a lawyer uploads documents, the Strategy, Analysis
Report and Applicable Laws panels draw on the shelf documents relevant to that
case, alongside the case's own documents. When a lawyer asks a chat question
the shelf can answer, the AI uses it and names the document by its title. A
shelf document is never presented as one of the case's own documents. When an admin deletes a document, the AI erases its copy
straight away.

## For engineers

**Events** (backend → AI only; nothing is sent back):

| Event | What the AI does |
|---|---|
| `kb_document_added` | Fetch the signed link at once (valid ~15 min), read, redact, index under `documentId`. Same id replaces. Re-sent `requestId` ignored. |
| `kb_document_deleted` | Erase every copy on receipt. Unknown id: no-op. |
| `kb_sync` | Sent on every connect. Full replace: erase unlisted ids, ingest new ones, keep held ones. `documents: []` erases all. |

**Where it lives.** `app/rag/knowledge_base.py` holds the shelf logic;
`app/socket/handlers/knowledge_base.py` the handlers. Documents are stored in
the evidence collection under the reserved workspace id `__knowledge_base__`
(not a 24-hex id, so never a case). That reuses chunking, embeddings and hybrid
search, and erasure goes through `delete_documents`, which clears the evidence,
tables and graph stores together. Every case query filters on its own workspace
id, so shelf chunks are never read or cited as case evidence, and a case purge
never touches the shelf. Shelf documents are not sent to the knowledge graph.

**Erasure wins every race.** An erase bumps the document's generation and
cancels its ingest. An ingest writes under the document's lock only if its
generation is unchanged, and a write already under way finishes before the
erase takes the lock. A failed erase retries three times, then logs CRITICAL,
audits `knowledge_base_erase_failed`, and is retried by the next `kb_sync`.

**Chat.** `passages_for_chat` runs only when the shelf holds something: hybrid
search over the shelf plus a rerank, keeping at most
`KNOWLEDGE_BASE_CHAT_PASSAGES` passages scoring at least
`KNOWLEDGE_BASE_MIN_RELEVANCE`. They appear in their own prompt section under
`[K#]` labels, after the case's annexures. The citation checker verifies
figures against them like any passage, and the citation is
`type: "knowledge_base"` with `title`, `category`, `page` and `quote`.

**Panels, for every case.** `_knowledge_base_block` (in `handlers/_shared.py`)
adds the shelf to Strategy, Analysis Report and Applicable Laws
(`KNOWLEDGE_BASE_PANELS`) on every build and regeneration. The search query is
each of the case's documents' type, subject and cited provisions (`case_query`),
identical for every panel, so a run makes one search. Up to `KNOWLEDGE_BASE_PANEL_PASSAGES` passages scoring at least
`KNOWLEDGE_BASE_PANEL_MIN_RELEVANCE` are kept, within
`KNOWLEDGE_BASE_PANEL_CHARS`. The block says plainly that this is not the case's
evidence: law, precedent and format only, never facts, parties, or a document
the case holds. Fact panels (List of Dates, Upcoming Dates, Relevant Parties)
never read it, so a judgment's facts can never become the case's facts. One
search per case, query and shelf version serves all panels of a run. Panel
citations still point only at the case's own documents.

Drafting does not use the shelf yet.

**Cost.** Ingest (download, OCR, embeddings) has no case to bill; it is priced
and logged as unbilled. Shelf retrieval for a case's panels or a chat turn is
billed to that case (one search per panel run, shared by its panels).

**Logging.** Ids and counts only. Never the link, the title or the text.

Tests: `tests/test_knowledge_base.py`.
