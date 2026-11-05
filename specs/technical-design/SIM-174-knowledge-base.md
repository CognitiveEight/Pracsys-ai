# SIM-174: Shared knowledge base — technical design

- **Requirements:** `specs/requirements/SIM-174-knowledge-base.md`
- **Status:** draft — for review (implemented on the ticket branch)
- **Contract:** Confluence AI brief "Knowledge base", v1

## Storage
The shelf is stored in the evidence collection under the reserved workspace id
`__knowledge_base__` (`KB_WORKSPACE_ID`). Not a 24-hex id, so never a case.
- Reuses `add_document_chunks` (deterministic ids, replace per document),
  dense + sparse vectors and hybrid search.
- Erasure goes through `delete_documents`, which clears the legacy, evidence
  and tables collections and withdraws graph facts.
- Every case query filters on its own workspace id, so shelf chunks are never
  case evidence; `delete_workspace` for a case never reaches the shelf.
- Chunk metadata: `knowledge_base`, `title`, `category` (unknown → `other`),
  `description`, `mime_type`, `uploaded_at`. Never the `fileUrl`.
- Not sent to the knowledge graph (no `schedule_document_ingest`).
- New `QdrantStore.list_document_ids(workspace_id)`: payload ids only, used to
  diff a `kb_sync` against what is actually held (correct after a restart).

## Shelf logic (`app/rag/knowledge_base.py`, `KnowledgeBase`)
- `add(doc, request_id, spawn)`: a re-sent `requestId` is ignored; an ingest in
  flight for the same id is cancelled and restarted (the newer link wins).
- Ingest: download (`document_processor._download_file`) and extract in a worker
  thread under a semaphore (`KNOWLEDGE_BASE_INGEST_CONCURRENCY`, 2). The routing
  name is `kb_<id><ext from mimeType>`, never the title.
  - Spreadsheets: `parse_workbook` → `redact_tableset` → `render_text`, with
    the row index (no `scan_text` over rendered tables).
  - Other files: `process_file_by_extension(..., mark_pages=True)`, then
    `scan_text`.
  - An extraction that yields only a bracketed message is "unreadable" and
    indexes nothing.
- `erase(ids)`: bump each id's generation, cancel its ingest, take its lock,
  `delete_documents`. An ingest writes under the same lock only if its
  generation is unchanged. A write under way is shielded and finishes before
  the lock is released, so an erase can never be overtaken by a write.
  - Three attempts with backoff, then CRITICAL plus the
    `knowledge_base_erase_failed` audit event; the held set is reset so the
    next sync retries.
- `sync(documents, keep, spawn)`, serialised: held = store ids (fresh read).
  - Erase (held ∪ in flight) − listed − keep.
  - Ingest listed − held − in flight.
  - `keep` holds the ids of invalid items, so a bad item never erases a held
    copy.
- `passages_for_chat(question)`: skipped when chat use is off or nothing is
  held. Otherwise `retrieve_passages(KB_WORKSPACE_ID, …, use_rewrite=False)`
  (one rerank call), keeping passages with `rerank_score ≥
  KNOWLEDGE_BASE_MIN_RELEVANCE` (6) up to `KNOWLEDGE_BASE_CHAT_PASSAGES` (4).
  rerank's keep-top-3 fallback is filtered out on purpose.

## Handlers (`app/socket/handlers/knowledge_base.py`, `KnowledgeBaseMixin`)
- `on_kb_document_added`, `on_kb_document_deleted` and `on_kb_sync` validate
  with the new schemas and call the shelf.
- Ingests are spawned through `_track_background_task`.
- Logs carry ids and counts only.
- `kb_sync.documents` items are validated one by one.
- Wiring:
  - registration in `events.py`;
  - bindings in `client.py`;
  - constants `KB_DOCUMENT_ADDED`, `KB_DOCUMENT_DELETED` and `KB_SYNC` in
    `backend_events.py`.
- No workspace id, so the events run outside any run scope; Stop does not
  reach them.

## Chat
- `chat.py` (intelligence chat only; drafting returns earlier) puts the hits in
  `workspace_state["knowledge_base_passages"]`. A failure is logged and the
  turn continues.
- `chat_nodes.chat_context_builder_node` renders a separate "KNOWLEDGE BASE —
  SHARED LEGAL REFERENCE LIBRARY" section, with `[K#] title (category)` blocks
  and rules:
  - use the shelf for law, precedent and formats;
  - take case facts from the case's documents;
  - never call a shelf passage an annexure.

  It creates an `EvidencePack` when the case has none, so `[K#]` is still
  citable.
- `EvidencePack.add_knowledge_base` (kind `knowledge_base`, prefix `K`, new
  optional `category`).
  - `CHECKABLE_KINDS` includes it, so figures are verified against shelf text.
  - The label regexes in `citations.py` accept `K`.
- Citation entry: `type: "knowledge_base"`, `documentId`, `title`, `category`,
  `page`/`pageEnd`, `heading`, `quote` (plus `sheet`/rows for spreadsheets).
  `Citation.type` gains the literal; `category` is a new optional field.

## Panels (every case)
- `_knowledge_base_block(panel, workspace_id, docs=, query=)` in
  `handlers/_shared.py`, next to `_graph_evidence_block`.
  - It runs only for panels in `KNOWLEDGE_BASE_PANELS` (default
    `strategy_objective,analysis_report,applicable_laws`).
  - It never raises, and it re-raises `CancelledError`.
- Query (`case_query`): per document (up to 12) its type,
  `extracted_fields.subject` and `legal_provisions`, else the first 200
  characters of its text. Capped at 1,500 characters. Every call site passes ALL
  the case's documents (`get_all_documents`, unfiltered), and the objective is
  deliberately left out: the first run writes it between panels, which would
  split the search, and each panel prompt already steers by it. So the three
  panels of a run build the identical query.
- `KnowledgeBase.passages_for_case`:
  - one `retrieve_passages` over the shelf, with no rewrite and one rerank;
  - keeps `rerank_score ≥ KNOWLEDGE_BASE_PANEL_MIN_RELEVANCE` (5), up to
    `KNOWLEDGE_BASE_PANEL_PASSAGES` (6);
  - caches by (case, query hash, shelf version), so the three panels of a run
    share one search. Every write or erase bumps the version, and an erase
    clears the cache.
- `render_panel_block`: a "SHARED LEGAL REFERENCE LIBRARY — NOT this case's
  documents" block within `KNOWLEDGE_BASE_PANEL_CHARS` (12,000). Its rules:
  - law, precedent, procedure and format only;
  - no facts, parties, dates or amounts;
  - never counted as a document the case holds, or as filling a gap;
  - no names in the case summary;
  - name a source by its title.
- Call sites:
  - Strategy (`_generate_strategies_from_documents`: initial build, every
    re-run and the objective-change cascade) and Analysis Report append it
    after the document content and graph block. The document budget shrinks
    by its length.
  - Applicable Laws appends it to the prompt itself, NOT to
    `evidenceSnippet`, whose heuristic fallback would otherwise mine shelf
    statutes as if the case cited them. It reads the case's documents itself
    and builds the same query as Strategy and Analysis.
- Panel citations (`attribution.py`) still match only the case's own
  passages, so shelf text is never attributed to a case document.
- The objective-change re-frame checks (`find_ungrounded_proper_nouns`) drop a
  summary that picks up a shelf party name, and the previous summary stands.

## Config
`KNOWLEDGE_BASE_CHAT_ENABLED` (true), `KNOWLEDGE_BASE_CHAT_PASSAGES` (4),
`KNOWLEDGE_BASE_MIN_RELEVANCE` (6.0), `KNOWLEDGE_BASE_INGEST_CONCURRENCY` (2),
`KNOWLEDGE_BASE_PANELS`, `KNOWLEDGE_BASE_PANEL_PASSAGES` (6),
`KNOWLEDGE_BASE_PANEL_CHARS` (12,000), `KNOWLEDGE_BASE_PANEL_MIN_RELEVANCE` (5.0).

## Cost
- Ingest runs in `capture_usage` and is logged with `log_unbilled_usage`
  (no case to bill).
- Chat and panel retrieval run inside the case's scope and are billed to it
  (one rerank call per run for the panels, cached across them).

## Risks
- Links expire in ~15 minutes. A large `kb_sync` is fetched two at a time; a
  link that expires before its turn fails with 403 and is retried only by the
  next `kb_sync` (next connect). A pull event would close this gap (brief Q5).
- The first deploy ingests the whole production shelf on connect (OCR and
  embedding cost, once).
- The lawyer app must render `type: "knowledge_base"` citations; until then, a
  client that ignores unknown types shows the `[n]` marker without a source.
