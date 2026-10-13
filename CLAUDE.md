# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this service is

SimplerToday is a Python asyncio **Socket.IO client** (not a server). It connects outbound to the application backend, listens for workspace and chat events, and emits structured AI payloads back. It is driven entirely by socket events — there is no HTTP API surface to call. `app/main.py` boots `app/core/lifecycle.py`, which initializes the Qdrant vector store, document processor, Gemini client, and socket client in that order, then blocks on a shutdown signal.

## Common commands

```bash
make setup        # create venv, install deps, copy env.example → .env, mkdir logs/ data/
make run          # python -m app.main  (the only entry point)
make test         # pytest tests/  — the knowledge-plane suite
make eval         # run the evaluation harness over the golden set
make ontology-ddl # regenerate data/ontology/schema.{enterprise,community}.cypher
make smoke-graph  # end-to-end check against a real Neo4j (needs NEO4J_URL)
make statute-report  # statutory register coverage summary
make docker-up    # bring up Qdrant + Redis from docker-compose.yml
make docker-down
make lint         # pylint app/  (only if pylint is installed)
make format       # black app/   (only if black is installed)
./dev.sh start    # interactive helper around the same actions
```

`make test` runs `pytest tests` (ontology, graph, retrieval, statute layers) and
the offline `tests/offline/verify_*.py` suites. **Before a deploy, also run `make e2e`**
(`scripts/e2e_local_backend.py`). It starts the real service
(`python -m app.socket.ai_service`) against a local stand-in backend and drives:
- the handshake and all seven panels;
- chat with citations;
- stop/resume and the compliance purge;
- shutdown, plus a scan of the service log for errors and leaked names.

It makes real, billed Gemini calls on a small fictional case, uses a throwaway
Qdrant collection and never touches the real backend. Dev tools live in
`requirements-dev.txt`. Production installs `requirements.lock.txt`, the exact
tested versions. **It does not cover the socket handlers, the
panel agents or document processing** — those still have no automated tests, so
validate changes there by running `make run` against a connected backend or by
exercising the path manually.

Required env (in `.env`, never commit): `SOCKET_URL`, `SOCKET_SECRET_KEY`, `SOCKET_AI_PAYLOAD`, `GOOGLE_API_KEY`, `QDRANT_URL`, `SECRET_KEY`. See `env.example` for the full list. Settings load through `app/core/config.py` (pydantic-settings).

## Architecture: how the pieces fit

The system is a multi-agent pipeline gated by a role agent and triggered by socket events. The full event flow is documented in `docs/SYSTEM_ARCHITECTURE.md` — read it before changing event handling, payload shapes, or agent wiring.

**Inbound socket events (`app/socket/client.py` → `app/socket/events.py`):**
- `workspace_created` — initialize role, batch-process evidence files, generate the full feature suite (assessments → metadata → strategy → analysis → list_of_dates → applicable_laws → procedural_timeline).
- `workspace_sync` — re-run the suite when documents change (`is_update=True`). Also carries the sharing roster in `participants` (see "Workspace sharing" below).
- `workspace_version_restore` — workspace rolled back to an earlier document version. Separate handler (`on_workspace_version_restore`), **not** a branch of `on_workspace_sync`. `evidenceFiles` is authoritative: the handler diffs it against the live Qdrant set and reconciles, so the backend's `deletedDocumentIds` / `restoredDocumentIds` only need to be approximately right. Idempotency keyed on `versionId` in its own window.
- `document_reload_request` — one document re-read on request (the "reload" action in Supporting Material, or a poor-quality score). Handled by `app/socket/handlers/document_reload.py`. It is a **re-read, not a delete plus an upload**: the document keeps its backend id, which the handler resolves from the payload (`targetFile._id` → matching `fileHash` in `evidenceFiles` → `storeFileName`/`path` → a unique filename match in the cached assessments manifest) and never invents — an id-less reload is abandoned rather than ingested under a fabricated identity no later delete could reach. Vectors and the manifest entry are purged **before** the re-fetch; the removed-evidence ledger is written only if the re-read FAILS (and cleared when it succeeds), so a good reload never reports its own document as a gap. `currentState` is mandatory — the re-run replaces every panel, so the report is tagged against it or the lawyer's approved/rejected items are wiped. Re-sends under the same `requestId` are expected: swallowed while in flight or just completed, but a re-send after a failed attempt re-runs. Scoped to the one document; drift against `evidenceFiles` is logged, not acted on. Offline coverage: `tests/offline/verify_document_reload.py`.
- `workspace_deleted` — the backend's erasure cascade does not reach this service, so this event is the instruction to erase the AI-held copy. Handled by `app/socket/handlers/workspace_purge.py`. Three rules: `purgeAfter` is **absolute** (never recompute it from `purgeAfterDays`); duplicate re-sends under the same `requestId` are **expected**, not errors (the backend emit is fire-and-forget and re-sends on every reconnect until `purgeAfter` passes); and the purge is **revoked** by any live workspace traffic — `cancel_pending_purge` is called from `workspace_created` / `workspace_sync` / `workspace_version_restore` / `workspace_transferred` / `document_reload_request` / `workspace_resume_request` / `missing_information_edited`, without which a case restored on day 2 would be silently erased on day 7.
- `workspace_stop_request` — the lawyer clicked Stop on a running update. Handled by `app/socket/handlers/run_control.py`. **Fire-and-forget: never reply.** Cancels the update's tasks (never a chat turn), drops buffered deliveries in memory *and* in the persisted buffer, and disarms recovery markers. Re-sends under the same `requestId` are expected; a stop whose `stoppedAt` predates newer work is ignored as stale. See "Stop is a fence" below.
- `workspace_resume_request` — "Finish update" on a stopped run. Same handler module. Emits `workspace:refresh` declaring **exactly** `reEmitFeatures` (the frontend arms one spinner per declared type), then one `document_feature_response` per feature in product order, and nothing else. Idempotent on `requestId` like `document_reload_request`.
- `user_message_request` — chat path: build `ChatContext`, run the intelligence chat agent, emit `ai_response`. Optional `askedBy` identifies a non-owner asker on a shared workspace (see "Workspace sharing" below).
- `missing_information_edited` — a lawyer re-worded one Missing Information item (text only). Handled by `on_missing_information_edited` in `app/socket/handlers/analysis_report.py`. The item keeps its `itemId`. Score and explanations do not change, and no model is called. The edit goes into `_workspace_edited_missing_info` (`CacheKinds.EDITED_MISSING_INFO`), which `_tag_analysis_report_against_current_state` re-applies, so a regeneration from the documents cannot revert it. A re-sent `requestId` is swallowed, and an older `editedAt` is stale. Gated on `editedBy` via `resolve_asker`. Offline coverage: `tests/offline/verify_missing_info_edit.py`.
- `probing_prompts_request`, `analysis_report_feedback`, `ai_heartbeat_ping`.

**Outbound:** `document_feature_response` (with `featureType` ∈ `document_assessments`, `workspace_metadata`, `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`, `procedural_timeline`) and `ai_response`. Schemas live in `app/socket/schemas.py`.

### The role gate is non-bypassable

`app/langgraph/role_agent.py` is the root agent. **Without a validated role (ADJUDICATOR / PETITIONER / RESPONDENT / MEDIATOR), no downstream agent runs** — workspace init calls `workspace_role_integration.initialize_workspace_with_role(...)` first, and role changes invalidate all derived intelligence. When adding a new agent or feature, gate it through this state and don't smuggle role-dependent logic past it.

### Two strategy paths exist — know which one runs

There are two strategy implementations and they are **not equivalent**:
1. **Socket flow** uses `_generate_strategies_from_documents()` in `app/socket/events.py` — a one-shot Gemini call over concatenated vector-store content. **This is what production uses.**
2. **`app/langgraph/strategy_agent.py`** (suggest → map_objective → validate → lock) is a richer LangGraph pipeline but is **only invoked programmatically** (e.g. `strategy_example.py`), not from sockets.

Don't assume changes to `strategy_agent.py` affect socket-driven strategy output. Edit `_generate_strategies_from_documents()` for that.

### Async scheduling and reconnect-safe caches

Long-running feature generation (analysis report, list_of_dates, applicable_laws, procedural_timeline) is dispatched via `asyncio.create_task` from the socket handlers so chat handling (`user_message_request`) is never blocked. Each feature also writes to an in-memory payload cache keyed by workspace_id (e.g. `_workspace_strategy_payload`, `_workspace_analysis_payload`, `_workspace_applicable_laws_payload`, `_workspace_procedural_timeline_payload`). On reconnect, cached payloads are re-emitted. When you add a new feature payload, follow the same pattern: schedule the long work, cache the result, and re-emit on reconnect — otherwise clients will miss the payload after a disconnect.

### Intelligence chat is governed, not free-form

`app/langgraph/intelligence_chat_agent.py` runs a fixed graph: `context_builder → intent_classifier → explain_why (Gemini) → action_dispatcher → response_formatter → governance_validator`. Context is assembled in `events.py` (role, doc snippets from Qdrant, optional attachment) and passed as `ChatContext` (`app/langgraph/chat_models.py`). `action_dispatcher` only **records** which agent should handle an intent; it does not invoke other agents in-line. Chat replies must remain neutral/accuracy-first; role mostly affects panel outputs (strategy, drafts), not chat advocacy.

### Audio evidence (transcription + diarization)

Audio attachments (`.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`, `.opus`, `.webm`, `.aac`) are routed through `app/processing/audio_transcription.py` (Chirp 3 / Speech-to-Text V2 with GCS-backed BatchRecognize for native speaker diarization, plus Gemini content re-diarization for 2-speaker calls). The audio path runs inside `process_document` like any other file: the formatted transcript (`[MM:SS] Speaker A: text` lines) becomes the document's `extracted_text`, so summary/strength generation, vector storage and chat context all work without special-casing. The `document_assessments` builder in `app/socket/events.py` adds two extra fields per audio entry: `transcript` and `transcriptStatus` (`ready` / `pending` / `failed`); not-ready entries collapse to a minimal `{ documentId, fileName, transcriptStatus }` payload. Requires `ffmpeg` on PATH and `GCS_BUCKET_NAME` set for full diarization; without GCS, transcription still works but speaker tags may be absent. `pyannote.audio` and `torch` are optional voice-fallback dependencies and not in `requirements.txt`.

### Scanned PDFs: OCR reads the scan itself, in reading order

Image-only PDFs (phone scans, photocopies) go through `app/processing/ocr.py`;
`file_handlers` keeps the public functions and every bracketed sentinel callers
test for. A PDF with a real text layer never reaches this code. Each rule below
falls back to the previous behaviour, which is what keeps every PDF that read
before reading now (old vs new over 147 real PDFs: 0 regressions):

- **The scan's own photo, not a re-render**, when the page is provably just
  that photo (`native_page_jpeg`): one plain 8-bit JPEG, axis-aligned, nothing
  drawn after it, only white fills before it, no `/Rotate`, crop box = media
  box. Re-rendering at 150 DPI threw away 30-50% of a phone scan's pixels.
  Anything else, and any JPEG that fails to decode, is rendered (200 DPI,
  lowered only for huge pages). Never widen the native rule to other image
  encodings: `/Decode`, CCITT polarity and masks are PDF-side semantics a raw
  extraction gets wrong.
- **`document_text_detection` first**, `text_detection` if it errors or reads
  nothing. Transient failures (503, deadline, quota) retry once; a failed page
  used to be dropped silently.
- **Reading order is rebuilt from word boxes** (`layout_text`) on single-column
  pages: Vision orders widely spaced typed text by fragment, which swapped
  dates in a sentence. A column gutter, non-left-to-right text or fewer than 8
  words keep Vision's order.
- **Unreadable and low-confidence pages are marked** (with `mark_pages` only;
  template and `pdf_to_docx` text is unchanged): `[OCR note: ...]` after the
  page marker, added after the boilerplate strip.
- Pages run 4 at a time in copies of the caller's context, so OCR units are
  billed to the right scope and logs keep their `request_id`.

Offline coverage: `tests/test_ocr.py` (fake Vision client; no calls made).

### Spreadsheet evidence is data, not prose

An `.xlsx`/`.xlsm` is parsed **once** into a typed `TableSet`
(`app/processing/tables.py`): real spreadsheet row numbers, a header and kind
per column, the sheet's own Total rows flagged. Everything else derives from
that parse, so nothing can disagree with it. `filename` from the backend has no
extension; `extension` is the only type signal, so `process_batch` passes it and
routing uses `filename_for_extraction` (the stored `filename` never changes).

- **Redaction is by column.** `redact_tableset` scans each cell with only its
  own header as context (`scan_text(..., strict_context=True)`). The prose
  redactor's 80-char window reaches an "Account No" header from the next rows,
  which masked every phone number and amount beside it, and ~1 in 10
  transaction refs on Aadhaar checksum alone. Never run `scan_text` over
  rendered table text.
- **Rendered text** opens with a workbook summary computed from every row
  (counts, totals, date ranges, value counts), then rows in ~1,500-char blocks
  that each restate `Sheet: X (rows a-b)` and the header. The summary is the
  first paragraph, so `fit_to_budget` and the classifier always see whole-sheet
  figures; when packing drops row blocks, its marker names the rows dropped.
- **No chunk overlap for sheets** (`chunk_text(..., with_overlap=False)`, set by
  `add_document_chunks` from the row index): a repeated row is counted twice.
  Each chunk carries `sheet`, `row_start`/`row_end` and `row_offsets`, so a
  citation names the exact row (`sheet`/`rowStart`/`rowEnd` on `Citation`).
- **Rows are stored for computation** in `<collection>_tables_v1` (no vectors;
  `replace_document_tables`, manifest written last). `delete_documents` and
  `delete_workspace` clear it; any new deletion path must go through them.
  A non-spreadsheet re-read clears the document's rows.
- **The model never does arithmetic over rows.** `app/rag/table_query.py`: the
  model turns the question into a JSON spec, code validates it against the real
  columns (refusing, never guessing) and runs it over every row with exact
  decimals. Results join the evidence pack as `[T#]` and are verified by the
  citation checker like any passage. When nothing could be computed, the prompt
  says so and forbids totals from partial rows. `TABLE_QUERY_ENABLED=false`
  turns it off.
- **The knowledge graph skips spreadsheet chunks** (`chunks_for_extraction`):
  the prose extractor has no node for a measurement and verifies nothing
  against the row. Entity resolution never merges names whose numbers differ
  ("Player 1" / "Player 10", "Accused No. 1" / "No. 10").
- A parsed sheet gets `apply_lossless_floor` on its readability score; the OCR
  heuristics would otherwise mark a small numeric sheet unreadable and replace
  its text with a low-content marker.

Legacy `.xls`/`.ppt` are rejected with an explicit unsupported-format message.
Offline coverage: `tests/test_tables.py` (includes a real-Qdrant end-to-end
run, skipped without a local Qdrant) and `tests/test_office_extraction.py`.

### Deleted documents must stay visible as gaps

The cached `document_assessments` payload is the de-facto document manifest: `_build_document_inventory` injects it into every feature prompt as *"GROUND TRUTH for which documents are present"* and tells the agent not to flag anything listed there as missing. It must therefore track reality exactly — use `_merge_document_assessments` when adding documents (never overwrite; that shrinks the manifest) and `_prune_document_assessments` when removing them, **before** anything builds a prompt.

Pruning also snapshots each removed entry into the `REMOVED_DOCUMENTS` ledger, which survives the document and feeds two things: a REMOVED EVIDENCE prompt block that overrides the `missingInfo` suppression lists, and `_apply_removed_evidence_missing_info`, a deterministic append that guarantees one `missingInfo` gap per removed document type regardless of prompt compliance. Re-ingesting a `documentId` clears its ledger entry (`_forget_removed_documents`) — that is what makes a restore self-healing. When you add an ingestion or deletion path, wire all three helpers or deleted evidence will silently disappear again.

### Deleting a workspace must erase every copy

`workspace_deleted` is a compliance deletion, not a cache eviction. `_purge_workspace` blocks
persistence for the workspace **first** (`block_workspace_persistence` in `app/workspace/persistence.py`),
because `persist_state` is fire-and-forget and an upsert already in flight would otherwise land after
the delete and recreate the cache point. It then clears in-memory state, then deletes from Qdrant and
disk.

`_clear_workspace_memory` **sweeps `self.__dict__`** rather than naming each cache. The handler owns
dozens of per-workspace dicts and gains more with every feature; an explicit list would be correct
the day it was written and quietly incomplete a month later. Keys matched: the workspace id, and
composite keys prefixed `"<workspace_id>:"`. Chat-scoped caches hold extracted document text but are
keyed by bare `chat_id`, so they are reached through `_chat_workspace_index` — call
`note_chat_workspace(chat_id, workspace_id)` from any new chat entry point. The purge's own
bookkeeping (`_pending_purges`, `_purged_workspaces`, `_seen_purge_requests`) is exempt from the
sweep: it is workspace-keyed too, and deleting the pending entry mid-purge makes the retry loop think
the purge was revoked.

A failed purge **retries with backoff and escalates as `logger.critical` + a `workspace_purge_failed`
audit event**. It must not fail silently: the backend stops re-sending once `purgeAfter` has passed,
so nothing else would ever trigger a second attempt.

### An objective change re-frames the case summary, it does not re-fact it

Declaring a new objective now updates the Case Summary and Report Summary too —
`_regenerate_strategy_for_new_objective` (was
`_regenerate_strategy_objective_preserving_case_summary`) produces all of it in
one call and one coalesced `strategy_objective` emit.

The licence is narrow and worth understanding before you widen it: **re-framing
is emphasis and ordering, never facts.** The case summary is the fact-of-record
narrative (`_build_document_inventory` and every feature prompt are built on
it), and its facts come from the documents, not from the objective. Three
deterministic checks enforce that in the handler, because the prompt alone will
not: every figure in the previous summary must survive
(`find_dropped_case_figures` in `app/socket/event_utils.py`), no new
organisation/party/place may appear (`find_ungrounded_proper_nouns`), and the
mandated opening must be intact. A candidate failing any check is dropped and
the previous text stands; since the two summaries must agree, one failing holds
the other back. **The new objective lands regardless** — the re-frame is
best-effort on top of it, never a precondition. The chat ack names the case
summary only when it actually changed.

Two callers deliberately do NOT re-frame: **role reassignment** passes
`refresh_case_summary=False` (it changes who is advocated for, not what is being
argued), and a **surgical** objective edit goes through
`_regenerate_strategic_objective_with_feedback`, which still preserves both
summaries byte-for-byte. Offline coverage: `tests/offline/verify_objective_change.py`.

### A lit loader must always be paired with a terminal emit

`workspace:refresh` arms a set of frontend panel loaders (`featureTypes`), and
**only a `document_feature_response` for the same `featureType` clears one** —
there is no completion event and no client-side timeout. So any path that arms
a loader and then returns without emitting leaves that panel spinning for the
rest of the session. That is what the 2026-09-17 incident was: an objective
change whose 2-minute regeneration was cancelled by a socket disconnect, dying
without a log line or an emit because `asyncio.CancelledError` is a
`BaseException` that every `except Exception` on the path missed.

Four rules follow, and a new feature has to honour all of them:

- **Catch `asyncio.CancelledError` explicitly** (before `except Exception`) in
  any long-running generation a socket turn awaits — log it, mark the work, then
  re-raise. Schedule detached work through `_track_background_task`, never a
  bare `asyncio.create_task` whose handle is discarded.
- **`SocketClient.emit` returns a bool, and it means delivery.** It never
  raises; a `False` means the payload was dropped. `_emit_document_feature`
  buffers an undelivered envelope (`CacheKinds.PENDING_FEATURE_DELIVERY`, per
  `(workspace_id, featureType)`) and it is re-sent on reconnect *before* the
  cached replay, so the user gets the real output rather than the stale
  snapshot. **A socket test double must return `True`** or every handler will
  read its emits as dropped.
- **Release what you armed.** `_release_refresh_loaders(...)` in
  `app/socket/handlers/_shared.py` emits the terminal state for every armed
  featureType that did not emit. Call it on every exit branch that can leave a
  loader lit, and take the arming list from `OBJECTIVE_CHANGE_REFRESH_FEATURES`
  rather than re-typing it. It is a safety net, not a fix: also tell the user
  the change did not land (`OBJECTIVE_CHANGE_FAILED_MESSAGE`).
- **Never read a `gemini_client.generate` result without `is_generation_error()`.**
  It reports failure by *returning* a sentinel string, so an unchecked 429 or
  safety block surfaces as the far more misleading "model returned no JSON".
  All calls are bounded by `gemini_request_timeout_seconds` (default 300s; the
  legitimate strategy regeneration takes ~2 minutes).

Offline coverage: `tests/offline/verify_objective_change.py` group 12.

### Stop is a fence, not a cancellation

`workspace_stop_request` cancels what it can, but correctness never depends on
the cancel landing. Every workspace-scoped inbound event runs in a
`contextvars` **run scope** `(workspace_id, epoch)`, set by
`_dispatch_in_run_scope` from `SocketEventHandler.handle`. `create_task` copies
the scope, so detached work carries it too. A stop bumps the epoch. From then
on, anything running under an older epoch:

- cannot emit `document_feature_response` or `workspace:refresh`, because
  `_socket_client` is a property that wraps the client in `_StopFencedSocket`.
  The suppressed emit returns `False`, i.e. not delivered. This is what stops a
  stale refresh from re-arming the frontend's spinners;
- cannot enter the pending-delivery buffer (`_note_pending_feature_delivery`);
- writes an objective-change marker as **held** (`heldByStop`), not as
  resumable. A held change is never auto-resumed on reconnect or restart. Only
  a resume including `strategy_objective` replays it, and a new full run
  (`SUPERSEDING_RUN_EVENTS`) drops it. The new objective exists nowhere else,
  because the resume payload does not carry it.

A run started after the stop captures the new epoch and is untouched. Rules for
new code:

- **Spawn detached work through `_track_background_task`.** That is what
  registers it with the run so a stop can cancel it. A task spawned by an
  already-stopped run is cancelled at birth.
- **Keep emits going through `self._socket_client`.** Holding a reference to
  the raw client bypasses the fence.
- **Don't add a chat path to `CANCELLABLE_RUN_EVENTS`.** A stop must never eat
  the lawyer's answer. Chat turns are fenced, not cancelled.
- **A resume emits only what it declared.** Anything a generator emits beyond
  its own featureType shows up as a stray panel update in a resume. Offline
  coverage: `tests/offline/verify_run_control.py`.

### Retrieval and citations: nothing is cited that was not checked

Evidence lives in its own Qdrant collection (`<collection>_evidence_v1`, dense +
sparse vectors). The original collection keeps only the state/role caches.
Code lives in `app/rag/`; offline coverage is `tests/offline/verify_rag.py`, which needs a
local Qdrant.

**Ingestion and storage**
- PDF text carries `--- Page N ---` markers, but only on the evidence and
  chat-attachment paths (`process_file_by_extension(..., mark_pages=True)`).
  Other callers, such as `pdf_to_docx`, get plain text.
- `app/rag/chunker.py` splits on the document's own structure and records
  `page_start`/`page_end`, character offsets and the nearest heading. Chunks
  overlap; `overlap_prev` lets `reassemble()` rebuild the text exactly.
- Point ids are deterministic, and `add_document_chunks` replaces a
  document's chunks. **Never write chunks any other way.**
- `get_all_documents` returns documents in reading order with their
  `metadata`. The old store sorted chunks alphabetically and dropped the
  metadata, which left every prompt's document header empty.

**Embeddings**
- Queries use `RETRIEVAL_QUERY` and chunks use `RETRIEVAL_DOCUMENT`, with the
  dimensionality requested explicitly.
- Embeddings are never padded, truncated or replaced with zeros. A failed
  embedding keeps its chunk flagged `embedding_failed`: whole-document reads
  still see it, and exact-term search can still find it, but dense search skips
  it.

**Re-indexing and deletion**
- Legacy workspaces are re-indexed on first use (`ensure_workspace_indexed`),
  or in bulk with `scripts/reindex_chunks.py`.
- **Deletion must hit both collections.** `delete_documents` and
  `delete_workspace` already do; any new deletion path must go through them.

**Panel packing**
- Panel prompts no longer cut documents from the front.
- `fit_to_budget` (`app/rag/packing.py`) keeps a document's opening, its
  closing, and the middle paragraphs that matter for that panel, and marks
  every cut.
- The total budget is `PANEL_CONTEXT_CHARS`, 200K by default. Use
  `fit_to_budget` in any new content builder.

**Chat citations**
- `app/rag/chat_context.py` builds labelled passages: `[E#]` for document
  passages, `[C#]` for case laws, `[S#]` for statutes.
- Small workspaces go in whole. Large ones go through rewrite → hybrid search
  → rerank, with a retry if nothing relevant comes back.
- After generation, `verify_and_number` (`app/rag/citations.py`):
  - removes invented labels;
  - checks every figure in a cited sentence against the cited passage, and
    re-points the citation if the figure is elsewhere;
  - reports figures found nowhere;
  - renumbers to `[1]`, `[2]`.
- One repair pass follows. A figure that is still unsupported is flagged
  inline, and governance drops the reply's confidence to LOW.
- `ai_response.citations` carries only citations whose marker is still in the
  text.

**Panel citations**
- These are attached at the one emit chokepoint, `_emit_document_feature`, by
  `app/rag/attribution.py`. It works deterministically: the quote is copied
  from the stored passage, and the page is where that quote is.
- It re-points a chronology entry whose date is not in its claimed document.
  It gives no citation rather than a weak one.
- **All fields it adds are additive.** Never change an item's type to carry a
  citation.

**Applicable laws and case laws**
- Each law row keeps its Indian Kanoon `sourceUrl`.
- `registerStatus` (from `check_provision` in `app/statutes/register.py`):
  - `verified`, with the official `heading`;
  - `invalid`: the act's full section list is known and the section is not in
    it, or it is omitted (`"omitted": true`);
  - `unverified`: unknown act, an Order/Rule/Article/Schedule reference, a
    superseded code (IPC/CrPC/Evidence Act), or a repealed act
    (`"repealed": true`, `repealedBy`). Repeal is never `invalid`: the old act
    still governs earlier facts.
- Each row also gets `sectionTitle`, the official heading (Copyright Act s.19
  gives "Mode of assignment"), for display as "Section 19 – Mode of
  assignment". `section` itself is never changed, because merges, drafting and
  search match on it. `annotate_law_rows` sets both, and runs at the emit
  chokepoint so every path carries them, edits included.
- Section lists come from `app/statutes/data/sections.json`: 165 acts and
  12,530 sections from India Code. Rebuild it with
  `scripts/fetch_laws_indiacode.py` then `scripts/build_statute_index.py`.
  **Absence means `invalid` only for an act in that index.** The hand-built
  seed (`register.json`) still supplies the IPC/CrPC/Evidence Act mappings.
- Case laws add `citationVerified`: whether the citation string appears in the
  fetched judgment.

**Measuring accuracy**
- Measure changes with `scripts/eval_rag.py` on a golden set (format in
  `app/rag/eval/metrics.py`).
- It compares baseline and full retrieval (recall@k, MRR) and cited answers
  (citation precision, citation coverage, unsupported rate).

### Knowledge graph (ontology agentic RAG): optional, additive, never gating

The legal ontology is a set of node and edge types: Party, Event, Obligation,
Provision, Order and others (`app/ontology/schema.py`). Facts are stored in
Neo4j (`app/graph/store.py`) and planned and assembled by
`app/retrieval/` (planner, critic, graph queries).

It came from `feature/data_ingestion`, ported onto this branch's own RAG.
**It does not use Keel.** Vector and exact-term sub-questions run on
`app/rag/retriever` (hybrid search), and graph sub-questions run on Neo4j.

**Configuration**
- Off unless `NEO4J_URL` + `NEO4J_PASSWORD` are set.
- Connected once in `lifecycle.startup` via `app/core/knowledge_plane.py`, in
  non-strict mode: if the graph is unreachable, the service logs it and keeps
  serving on hybrid search alone.

**Ingest**
- Facts are extracted from the evidence-store chunks
  (`app/ontology/pipeline.py`). Every fact carries this branch's `chunk_id`
  (`{doc}_chunk_{i}`) and the page its own text is on, so graph facts and
  citations point at the same passages.
- Re-ingesting a document first withdraws its old facts.
- Runs as a tracked, metered background task (`schedule_document_ingest`),
  only when `ONTOLOGY_INGEST_ENABLED=true`, and is cancelled by a stop like any
  other work.

**Use in chat and panels**
- Only for names in `RETRIEVAL_PANELS`, and only in **augment** mode: evidence
  is added to the existing context, never substituted for it.
- **Chat** (`intelligence_chat`): graph facts appear as lines ending in the
  `[E#]` label of their source passage, so the citation checker verifies them
  like any other claim.
- **Panels:** `strategy_objective`, `analysis_report`, `list_of_dates` and
  `applicable_laws` take a `[G#]` evidence block via `_graph_evidence_block`.
- `procedural_timeline` and `relevant_parties` are deliberately not hooked.
  They extract per document with provenance, and injected graph text would be
  attributed to a document that does not exist.
- **Enable a panel only after `scripts/eval_rag.py --graph`** shows it beats
  the baseline on real golden matters.

**Deletion**
- `register_knowledge_plane` hooks graph withdrawal into
  `QdrantStore.delete_documents` and `delete_workspace`, so every deletion path
  clears the graph too.
- A failed graph clear during the **workspace purge raises**, so the purge
  retries and escalates. On a single-document delete it is logged CRITICAL and
  audited instead.

Tests: `pytest tests` (254 unit tests) plus the `[knowledge graph]` group in
`tests/offline/verify_rag.py`.

### The quality score explanation is objective-relative

The case resolution score measures readiness **for an objective**, so its
explanation must move when the objective does. `_ensure_quality_score_explanation`
therefore takes `strategic_objective` and hands it to all three producers — the
LLM pass, the deterministic fallback (`_objective_clause_for_explanation`) and
the main analysis prompt — each of which must state how the objective bears on
the score. Read the objective with `_current_strategic_objective(workspace_id)`
off the strategy cache; the analysis report never carries `strategicObjective`,
and on the objective-change path the strategy regen has already written the new
one there.

The objective-change cascade additionally **forces** the refresh: an explanation
that comes back empty, or byte-identical to the report being replaced, is
regenerated with the new objective and `reportQualityScoreExplanation` re-derived
from it. Don't remove that: `qualityScoreExplanation` is among the first fields
lost when the long report JSON truncates, and the refill that covers for it runs
over the gap lists alone, so it can reproduce the previous objective's text
verbatim. **Any new path that rewrites the score must pass the objective and pass
`existing=""`.** Offline coverage: `tests/offline/verify_objective_change.py` group 11.

### Workspace sharing is read/write, and `userId` is always the owner

A shared case reaches this service through two additive fields, and **neither
changes `userId`** — it stays the case OWNER on both, so every owner-scoped
cache (role, cumulative token usage, panel payloads) keeps its key.

- `workspace_sync.participants` — `[{userId, name, permission}]` with
  `permission` ∈ `edit` / `view`. **Absent entirely (not `[]`) when the case is
  not shared.** That makes an absent field meaningful on `workspace_sync`: it
  is how an un-share arrives, so it clears the roster
  (`set_participants(..., authoritative=True)`). `workspace_version_restore`
  inherits the field but passes `authoritative=False` — it is a
  document-version event and says nothing about sharing.
- `user_message_request.askedBy` — `{userId, name}`, present only when someone
  other than the owner typed the message.

`app/workspace/participants.py` is the join between them. It follows
`custom_instructions.py` exactly: module-level dict, write-through to Qdrant
(`CacheKinds.PARTICIPANTS`), hydrated on a cold workspace in
`_hydrate_workspace_caches`, and cleared explicitly by `_purge_workspace` —
module-level state is NOT reached by the `self.__dict__` purge sweep, and
participant names are personal data.

`resolve_asker(...)` answers the only question the rest of the service asks:
**may this person change the case?** A `view` collaborator gets the same
analysis for any question, but every chat-driven mutation is skipped and the
reply says so rather than implying the change landed — panel edits, fact
corrections, the objective-change cascade, AI-marker panel updates, and role
selection (the role gate is the root of all derived intelligence). Unknown
asker + known roster → read-only. Unknown asker + **no** roster cached →
allowed and logged; the sharing sync may simply not have reached this process,
and failing closed would break every collaborator on an unseen case. **When you
add a chat path that writes workspace state, gate it on `mutations_allowed`.**

Participants are app users, **not parties to the dispute** — keep them out of
legal prompts or they will corrupt `relevant_parties`, which is extracted from
evidence. Their one prompt appearance is the `WHO IS ASKING` attribution block
in `chat_context_builder_node`, which states outright that access level changes
nothing about the analysis. Offline coverage: `tests/offline/verify_workspace_share.py`.

### Indian-law grounding

Outputs are framed for Indian law: BNS / BNSS / BSA / IT Act. **IPC must not appear in `applicable_laws` output** — the normalizer (`_normalize_applicable_laws_payload`) drops IPC rows and the prompt forbids them. The applicable-laws path scrapes Indian Kanoon (`indiankanoon.org`) directly and falls back to **Google Vertex AI Search** (`GCP_PROJECT_ID` + `VERTEX_SEARCH_DATA_STORE_ID`, see `app/search/vertex_ai_search.py`) when configured. Vertex AI Search replaced the deprecated Google Custom Search (CSE) integration. The list is capped at 12 items with stable `law_N` ids. Prompt scaffolding is in `app/prompts/` (`legal_intelligence_system.py`, `applicable_acts.py`).

### Model calls are provider-neutral

`gemini_client` (`app/ai/gemini_client.py`) keeps its name, but it is a facade
over `app/llm/`. `ModelRouter` sends each call to the profile configured for its
task: Gemini, OpenAI, Azure OpenAI, Azure AI Foundry, Bedrock, SageMaker, or any
OpenAI-compatible server (DeepSeek, Qwen, Llama, …). Unset `LLM_*` means
Gemini, i.e. the old behaviour. Read `docs/MODEL_PORTABILITY.md` before
touching it.

**Rules:**
- **Never import a provider SDK outside `app/llm/providers/`.**
  `tests/test_llm_router.py` fails the build if you do. A new provider is one
  adapter plus a registry entry.
- **Adapters only translate.** Timeout, retry, fallback, JSON repair, the
  `llm_call` line and usage recording live in the router, once.
- **The route key is the `feature=` on the `llm_call` line.** That is the
  `@meter_usage` label, else the calling function. Pass `task=` explicitly
  when a call needs a stable key.
- **A `blocked` result is final.** Never route a content-filter block to
  another provider.
- **Sync code in a worker thread uses `gemini_client.generate_sync`.** It runs
  on the main loop in the caller's context, so cost lands in the right scope.
  Never call a provider SDK directly from a thread.
- **Price per call:** `record_usage(..., model=...)`. Read a scope's cost with
  `llm_price(usage)`, never `price_for(settings.gemini_model, …)`.
- **The user's model choice heads the chain.** It comes from `aiModel` and
  lives in `app/workspace/model_selection.py`. Grounding, embeddings and
  `LLM_ROUTES`-pinned tasks never follow it. Only ids in
  `LLM_SELECTABLE_MODELS` are honoured.
- **Under `GATE0_ENFORCE` nothing may bypass gate0.** A new model call
  path must go through the router, so the governance check applies.
- **An embedding provider, model or dimension change is a new collection.**
  Migrate with `scripts/reindex_chunks.py --from-collection` and delete the
  old collection, which a purge does not reach.

Offline coverage: `tests/test_llm_router.py`, `tests/test_llm_providers.py`.
For a live, billed check, run `scripts/smoke_llm_providers.py`.

### Cost tracking covers more than tokens

Every emit carries a `metadata` block with this response's usage/cost and the workspace's running total. **`price` and `totalPrice` are the whole bill — LLM tokens *plus* paid non-LLM APIs**: Vision OCR (`ocrUnits`, 1 per image and 1 per OCR'd PDF page), Speech-to-Text (`sttSeconds`), Vertex AI Search (`searchQueries`), Google Search grounding (`groundingRequests`) and embeddings (`embedTokens`, an estimate). Each source also reports its own `*Price`, summed as `servicePrice` / `totalServicePrice`.

The mechanism is a `contextvars` scope: `app/ai/usage.py` holds one `UsageAccumulator` per turn/feature, opened by `capture_usage()` / `@meter_usage`, and every paid call site reports into it (`record_usage`, `record_ocr_units`, `record_stt_seconds`, `record_search_queries`, `record_grounding_requests`, `record_embed_tokens`). Pricing is in `app/ai/pricing.py` (`price_for` for tokens, `service_price_for` for the rest); all rates are env-overridable, and free tiers are **not** modelled, so figures are an upper bound. Cumulative totals live in `metadata["token_usage"]` on the workspace context and ride the existing Qdrant `WORKSPACE_META` cache.

Two rules when adding a paid call or a new feature emit:
- **Instrument the single lowest call site, not its callers.** OCR is recorded once inside `FileProcessor.extract_text_from_image`, which is why the per-page PDF loop, standalone images and content-sniffed images are all counted correctly and exactly once.
- **Costs reach the case through the workspace ledger, not through the scope.** `record_*` charges the workspace bound in the logging context at record time (`app/ai/usage.py`), and every emit reports what `drain_for_emit` returns: the case's spend since its last emit. Background work with no emit of its own (knowledge-graph ingestion, re-evaluations) is therefore still billed to its case, which it was not before. Keep model calls inside the event's context (spawned tasks inherit it); a cost recorded with neither a workspace nor a scope is billed to nobody and logs a warning once per kind. Never add to `token_usage` any other way, or the case stops reconciling with gate0 (`scripts/reconcile_gate0_spend.py`). Offline coverage: `tests/test_usage_ledger.py`.

## Logging: traceable, and never a data leak

Setup lives in `app/observability/logger.py`. It is idempotent: a second
`setup_logging()` call does nothing, which is what keeps the audit sink alive.
Offline coverage: `tests/offline/verify_logging.py`, a static scan of every log call plus a
check of the sinks.

**Correlation**
- Every inbound event runs inside `logger.contextualize(request_id, event,
  workspace_id[, chat_id, run_epoch])`, set in `SocketEventHandler.handle` and
  `_dispatch_in_run_scope`. Tasks the event spawns inherit it.
- To follow one run, filter on its `request_id`. Never re-type ids into message
  text.

**Reading a run in production** (filter on the `request_id`):
- `event <name> received` / `event <name> finished in Xs status=ok|error|cancelled`
  for every inbound event. Polled events (`probing_prompts_request`,
  `ai_heartbeat_ping`) log these at DEBUG, so they don't bury everything else.
- `result <featureType> delivered=yes|no items=<list counts> price=<cost>`, one
  per panel emit (`_emit_document_feature`). An empty panel shows as
  `listOfDates=0`. Chat replies get `result ai_response delivered=... chars=
  citations=`.
- Long panel work runs in background tasks after `finished`. Its `result`
  lines carry the same `request_id`.

**One line per model call**
- `gemini_client` writes a single `llm_call` line with the kind, the feature
  (taken from the `@meter_usage` function name), model, status
  (`ok`/`error`/`timeout`/`blocked`/`truncated`), finish reason, duration and
  tokens.
- A failed call that returns the error sentinel is logged as `error`. Do not
  add per-call "generating…/generated" lines.

**Sinks**
- `app.log` follows `LOG_LEVEL`. Audit records go only to `logs/audit/`, and
  `@tracer.trace` records go only to `structured.log`.
- Tracebacks never include local variable values unless `LOG_DIAGNOSE=true`.

**Never log**
- socket payloads (log the keys and counts instead);
- prompts, model output or document text — use `chars(x)` from
  `app/utils/redact.py`, which logs the size only;
- URLs — use `safe_url(url)`, because signed URLs carry credentials, one valid
  about 25 years;
- party, participant or client names, phone numbers, chat or objective text,
  or fact values.

**Style**
- Loguru `{}` placeholders, never f-strings, and no emoji.
- One summary line for a list, not one per item.
- Exceptions: one `logger.opt(exception=e).error(...)`, never `error` followed
  by `exception`.

## Code conventions

- Python 3.11+, type hints on public functions, 4-space indent, `snake_case` modules and functions, `PascalCase` classes.
- Configuration is centralised in `app/core/config.py` — never hardcode secrets, URLs, or model names elsewhere.
- Logging uses Loguru (`from loguru import logger`); audit/trace helpers live in `app/observability/`. The logger format is `logger.info("msg: {}", value)` (Loguru-style placeholders, not f-strings) — match existing call sites.
- Commit prefixes follow Conventional Commits as used in history: `feat:`, `fix:`. Keep PR descriptions tied to user-visible behaviour and call out socket payload or schema changes explicitly.

## Sensitive files in tree

`bns-ai-*.json` and `gcp-vision.json` are GCP service-account credential files that exist in the working tree. Do not read, log, or include their contents in commits, diffs, or chat output. Treat `.env` the same way.

## Keel (`keel/`)

`keel/` is a **vendored plain copy** of the Keel knowledge-plane platform
(`github.com/keel-vc/keel-ai` @ `418cfb6`), copied 2026-08-24 so the Keel-side
tasks of the Agentic Ontology RAG programme can be done in this tree. It is
**not** part of the AI service and does not run in `env/` — its dependency pins
conflict with this repo's on fastapi, uvicorn, qdrant-client, redis and
python-multipart, so it needs its own venv or container. There is no git link to
upstream; refreshing is a manual rsync. See `docs/KEEL_VENDORED.md` before
touching it, and note the `.gitignore` `*.md` trap documented there.

## Where to look first

- Handshake authentication → `app/socket/auth.py` (scheme `v1`: HMAC over `v1:AI_Maverick:<unix_seconds>:<nonce>`, sent as `x-signature` / `x-signature-timestamp` / `x-signature-nonce`). Headers are **single-use** — `SocketClient.connect` passes the builder as a *callable* so python-socketio re-signs on every reconnect; passing a dict breaks every reconnect with `replayed_nonce`. Spec and failure-code table: `docs/AI_SERVICE_AUTH_MIGRATION.md`. `SOCKET_AUTH_SCHEME=legacy` is a rollback-only escape hatch.
- New socket event or payload type → `app/socket/handlers/` (per-feature mixins) + `app/socket/schemas.py`. `app/socket/events.py` is now a thin composition root (`SocketEventHandler` = the mixins + `__init__` state + `register`/`handle` + the `event_handler` singleton); pure helpers/constants live in `app/socket/event_utils.py`. See `docs/SOCKET_EVENTS_ARCHITECTURE.md` for the module map and conventions.
- New role behaviour → `app/langgraph/role_agent.py`, `state.py`, `integration.py`.
- New chat intent or response shape → `app/langgraph/intelligence_chat_agent.py`, `chat_nodes.py`, `chat_models.py`.
- Document ingestion / OCR → `app/processing/document_processor.py`, `file_handlers.py`.
- Vector retrieval → `app/vectorstore/qdrant.py` (storage) + `app/rag/` (chunking, hybrid retrieval, evidence, citations).
- Model providers, routing, per-task model choice → `app/llm/` (router + `providers/`) behind `app/ai/gemini_client.py`; `docs/MODEL_PORTABILITY.md`.
- Prompt edits (legal framing, guardrails) → `app/prompts/`.
- Knowledge plane / Keel → `keel/` + `docs/KEEL_VENDORED.md`. Ontology, statutory register and eval harness → `app/ontology/`, `app/statutes/`, `app/eval/` + `docs/AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md`.
- Full event-flow narrative → `docs/SYSTEM_ARCHITECTURE.md`. Recent feature-level history → `CHANGELOG.md`.
