# Changelog

All notable changes to the Law Firm SimplerToday AI Service are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A missing workspace role defaulted to ADJUDICATOR.** The lawyer app makes the
  role mandatory and pre-selects PETITIONER, but 114 inline fallbacks turned a
  missing or invalid role into a neutral judge's view. One constant
  (`app/core/roles.py`, PETITIONER) now backs every fallback, with a warning.
- **Deleting a case did not stop its running work.** The purge dropped the task
  registry without cancelling the tasks, so panel generation kept running on an
  erased case and could still emit. It now raises the stop fence and cancels
  them first; the fence is no longer swept back to 0.
- **Two background tasks escaped Stop and the purge**: the transfer re-emit and
  the reconnect replay. Both are tracked and scoped to their workspace now.
- **Payload values in logs.** Invalid-payload log lines printed the payload or a
  pydantic error containing input values, and case-law uploads logged the file
  name. `validation_fields()` (`app/utils/redact.py`) logs field paths only.
- **Lost chat replies after a disconnect.** Undelivered replies were held in one
  slot for the whole service and the re-send was skipped whenever a panel replay
  was pending. They are now buffered per chat and always re-sent; cached panels
  are replayed for every case active in the last 30 minutes, not only the last.
  No socket payload change.

- **Long audio whose speakers collapsed was discarded (hotfix).** When
  diarization collapsed a recording to one speaker, the whole call became one
  segment, split into ~134 pieces that all went into one 8000-token
  transliteration call, which the model could not answer (finish_reason
  MAX_TOKENS); the failure threw away the finished transcript ("Audio
  transcription crashed", readability 0%). Now pieces are sent 10 per call,
  pieces already in Roman script are not re-sent, and a transliteration failure
  keeps the Devanagari transcript instead of failing the document. On `main` the
  crash itself was `response.text` raising outside a `try`; here the router
  already returns an error sentinel for that. No payload change.
- **Every audio transcription failed with `NameError` (SIM-29 regression).**
  The model-portability refactor dropped the module-level
  `_RESOLVED_CREDENTIALS_PATH` / `_CREDENTIALS_RESOLVED` declarations that
  `_resolve_credentials_path` reads, so its first call raised before any
  recognition ran. Restored. Offline coverage:
  `tests/test_audio_transliteration.py`.
- **Python 3.10: every failed model call crashed its panel.**
  `reraise_if_cancelled` (`app/llm/errors.py`) called `Task.cancelling()`,
  which exists only from Python 3.11; the staging VM runs 3.10, so a model
  error (e.g. gateway unreachable) raised `AttributeError` instead of failing
  cleanly. It now falls back to the task's pending-cancel flag on 3.10. The
  full suite passes on 3.10.18 and 3.12. Note: `requirements.lock.txt` needs
  Python 3.11+ (`bidict==0.24.1`); a 3.10 host can only install `requirements.txt`.
- **SIM-29: every case-scoped model call through gate0 was rejected (401).**
  The gate0 adapter sent the workspace id as a `workspace_id` metadata key,
  which LiteLLM bans (an Anthropic/Bedrock workspace selector), so any call
  made inside a workspace event failed once gate0 was in use. The key is now
  `case_id`; the `workspace_id:<id>` tag that reconciliation reads is unchanged.
- **SIM-29: embedding cost through gate0 is gate0's own figure**, read from
  `x-litellm-response-cost`, instead of a token estimate × a configured rate.
  Gateway costs are also no longer rounded per call (a $0.00000975 call was
  billed as $0.00001). **Value change, not a shape change:** `embedPrice`,
  `servicePrice` and `price` in every emit's `metadata` move to gate0's figures.
- **Audio recordings over 20 minutes (hotfix).** Chirp 3 BatchRecognize rejects
  files longer than 20 minutes, so every long call paid for a doomed upload and
  then fell back to ~40 s Recognize chunks, whose speaker tags drift across
  dozens of joins. Recordings longer than `AUDIO_BATCH_WINDOW_SECONDS` (1140 s)
  are now cut at pauses into overlapping windows straight from the source file,
  run as parallel BatchRecognize jobs (`AUDIO_BATCH_CONCURRENCY`), and stitched
  with speakers matched on the shared overlap audio. A window whose job fails
  falls back to chunked Recognize for that window only; a window that cannot be
  transcribed fails the transcription explicitly rather than leaving a silent gap.
- Gemini re-diarization no longer drops every utterance after the 5,000th: long
  transcripts are assigned in overlapping blocks, reconciled for 1/2 label swaps.
- STT cost is recorded only for batch jobs that processed the audio; a rejected
  job was charged at its full duration.
- A cancelled transcription (Stop, purge, disconnect) now stops its worker thread
  at the next window/batch instead of running on.
- `AUDIO_MAX_DURATION_MINUTES` (default 1440, 0 = off) rejects a recording before
  any paid call with `transcriptStatus: failed`.
- Logs: the chunked-fallback line states its real reason (it always said "no GCS
  bucket configured"), and document/audio log lines no longer carry file names,
  which often contain party names.
- **Per-case cost was missing background work.** A case's cost total now
  matches gate0/LiteLLM's spend for that workspace. Before, a case showed
  $0.24 while gate0 had charged $0.30: knowledge-graph ingestion and other
  background steps with no emit of their own spent money that never reached
  the case.
  - **How costs reach a case now.** Every model call and paid unit is charged
    to its workspace when it is recorded (the workspace ledger in
    `app/ai/usage.py`). Each emit reports, and adds to the total, the case's
    spend since its previous emit.
  - **No double reporting.** A second emit in the same turn no longer repeats
    the per-response `price`, so the sum of per-response prices equals
    `totalPrice`.
  - **Payload.** The socket payload shape is unchanged. The per-response
    `price` now means "spend since the case's last emit".
  - **New script:** `scripts/reconcile_gate0_spend.py` compares each case with
    gate0's spend logs.
  - **Tests and purge.** `tests/test_office_extraction.py` no longer makes
    real, billed classifier calls on every run. A workspace purge also clears
    the case's unreported spend.

### Added

- **SIM-29: gate0 as the single model, cost and log layer.**
  - Every `llm_call` line has `cost=` and `gateway_call_id=` (gate0's call id),
    and embeddings get their own `llm_call embed` line, so each call matches
    one gate0 usage record.
  - Model features through gate0 come from `LLM_MODEL_OPTIONS` (`thinking`,
    `grounding`, `supports_documents`, `extra_body`, `family`), never from the
    model name, so any model gate0 serves, open-source ones included, can be
    configured. Gemini behaviour is unchanged.
  - Cohere embeddings get the query/document `input_type` through gate0.
  - Under `GATE0_ENFORCE`, startup reports configured models gate0 does not
    serve, without blocking.
  - `GATE0_METER_ENABLED` (off by default) reports OCR, Speech-to-Text, search
    and unpriced grounding units to gate0's meter with an on-disk outbox.
    Needs gate0's usage-record endpoint (schema 1.1.0), not yet deployed.

- **The user chooses the model per case** (`aiModel`).
  - **Socket payload change:** a new optional `aiModel` field.
    - On `workspace_created` / `workspace_sync` / `workspace_version_restore`
      it sets the case's model for every model call. Absent = unchanged;
      `null` / `""` = default.
    - On `user_message_request` it overrides the model for that turn only.
  - **Allowed models.** Only ids in `LLM_SELECTABLE_MODELS` are honoured. The
    default (`LLM_DEFAULT`, `gemini-3.8-flash`) stays behind the choice as the
    fallback.
  - **Not affected by the choice.** Tasks pinned in `LLM_ROUTES`, case-law
    grounding (Gemini) and embeddings never follow it.
  - **Storage.** The choice is persisted per case
    (`CacheKinds.MODEL_SELECTION`), rehydrated on a cold workspace and purged
    with the workspace.
  - **Embeddings through gate0** (`gemini-embedding-001`, 768) are the same
    vector space as direct Gemini (verified identical), so they keep the
    existing collection with no re-index.

- **Model portability: one provider-neutral model layer**
  (`app/llm/`, `docs/MODEL_PORTABILITY.md`). Every model call can now be served
  by Gemini, OpenAI, Azure OpenAI, Azure AI Foundry, AWS Bedrock (Converse),
  AWS SageMaker endpoints, or any OpenAI-compatible server. That last option
  covers open-weight DeepSeek, Qwen, Llama and Mistral on vLLM, Ollama or TGI,
  or through the hosted DeepSeek, DashScope, Together and Groq APIs.
  - **Routing.** It is set per task: `LLM_DEFAULT`, `LLM_ROUTES` (the key is
    the `feature=` on the `llm_call` log line) and `LLM_FALLBACKS`. Calls fall
    back on error or timeout, never after a content block. A transient
    429/5xx/network error is retried once on the same profile.
  - **Capability checks.** A request needing images, audio or grounding skips
    profiles that cannot take it. `LLM_MODEL_OPTIONS` clamps the output budget
    to each model's ceiling. A misconfigured explicit profile stops startup.
    With no `LLM_*` settings, behaviour is exactly as before (Gemini).
  - **Facade.** `gemini_client` keeps its name and API. Callers and the offline
    suites are unchanged, except `verify_rag.py`, which patched the old
    client's internals. The `llm_call` log line gains `provider=`.
  - **Embeddings.** `EMBEDDING_PROVIDER` takes Gemini, OpenAI, Azure, Bedrock
    Titan/Cohere or OpenAI-compatible models. Each embedding space has its own
    evidence collection, and the legacy Gemini space keeps its name, so
    nothing is re-indexed on deploy. A vector-size mismatch refuses to start.
    `scripts/reindex_chunks.py --from-collection` migrates chunks to a new
    space.
  - **Cost.** Each call is priced by the model that served it, so a turn mixing
    providers is billed correctly. `metadata.aiModel` is the model actually
    used. Non-Gemini rates come from `LLM_PRICING_OVERRIDES`; none are
    hard-coded.
  - **Audio and `agent.py`.** The audio diarization and transliteration
    helpers, and the `agent.py` text filters, no longer call the Gemini SDK
    directly. They are routed, bounded by the request timeout, and logged.
  - **Clean-ups.** `google-generativeai`, `google-ai-generativelanguage` and
    `langchain-google-genai` are removed. Ad-hoc `startswith("Error")` checks
    now use `is_generation_error`.
  - **New deps:** `openai`, `azure-identity`, `aioboto3`.
  - **New tests:** `tests/test_llm_router.py` and `tests/test_llm_providers.py`.
  - **New script:** `scripts/smoke_llm_providers.py` (live, billed).
- **Gate0 gateway mode** (`app/llm/providers/gate0.py`). Profiles
  `gate0:<model>` send model calls through ViitorCloud's gate0 gateway (LiteLLM
  proxy), which holds the provider keys, budgets and spend records.
  - **Governance.** `GATE0_ENFORCE=true` makes gate0 the only path. Startup
    refuses any direct profile, and the router never calls one.
  - **Attribution.** Each request carries `metadata` and `tags` (app, feature,
    workspace and request ids).
  - **Billing.** SimplerToday bills the gateway's own cost (the
    `x-litellm-response-cost` header).
  - **Gemini features through gate0:** thinking level, BLOCK_NONE safety,
    Google Search grounding with sources, and embedding task types.
  - **Budgets.** A budget-exhausted 429 is final; a rate-limit 429 is retried
    once.
  - **Default.** Off by default. See `docs/MODEL_PORTABILITY.md` for the
    gate0-side configuration this needs: model groups by name, a timeout of at
    least 300 s, `drop_params: false`, no fallbacks across classes, and high
    availability.

- **Excel (`.xlsx`/`.xlsm`) and PowerPoint (`.pptx`) evidence.** Both now go
  through `process_document` like any other file. Spreadsheets are read sheet
  by sheet (openpyxl, cached formula values), in blocks sized to one chunk.
  Each block opens with the sheet name, its row range and the header row, so
  any chunk retrieved from a long sheet keeps its column names. Hidden sheets
  are included and labelled. Spreadsheets are capped at 200K cells, and the
  text says so when the cap is hit. Decks are read slide by slide: title, text,
  tables, chart data and speaker notes. Each slide is marked
  `--- Page N ---` with N = the slide number, so citations name the slide.
  Slides that are only a pasted picture are OCR'd, up to 50 images per deck. Legacy `.xls`/`.ppt` now fail with an explicit
  "unsupported format" message, where they used to come out of the DOC
  extractor as byte noise. New deps: `openpyxl`, `python-pptx`.
  Workbooks are parsed once into typed tables (`app/processing/tables.py`):
  - the header is found below any title rows;
  - numbers and day-first dates typed as text are read as values;
  - percentages are shown as the sheet shows them;
  - formulas with no saved value are kept as formula text, counted, and
    excluded from every figure.
  The text opens with a workbook summary computed from every row.

- **`missing_information_edited` (Server → AI).** A lawyer can re-word a
  Missing Information item. `on_missing_information_edited` replaces the item's
  text by `itemId` in the cached analysis report and keeps its id and
  `isMatched`. Score, explanations and the other lists are left alone, and no
  model call is made. The report is then re-emitted as `analysis_report`. The
  edit is recorded in a per-workspace ledger (`CacheKinds.EDITED_MISSING_INFO`)
  that `_tag_analysis_report_against_current_state` re-applies. Without it, the
  next upload, sync or objective change would regenerate the model's original
  wording from the documents. A re-sent `requestId` is swallowed. An edit whose
  `editedAt` is older than the one already applied is dropped as stale. A
  `view` collaborator's edit (`editedBy`) is not applied. Offline coverage:
  `verify_missing_info_edit.py`.

- **Exact spreadsheet calculations in chat (`[T#]`).** A question asking for a
  count, total, average, minimum, maximum, percentage or list of rows is now
  computed in code over every row (`app/rag/table_query.py`). The model only
  turns the question into a JSON spec. Code validates it against the real
  columns and refuses anything it cannot run, then computes with exact
  decimals. Each result joins the evidence pack as `[T#]`, and the citation
  checker verifies every figure the answer quotes against it. The sheet's own
  Total rows are excluded so nothing is counted twice; blank and non-numeric
  cells are counted and stated. When nothing could be computed, the prompt
  says so and forbids totals from partial rows. Rows are stored in a new
  vector-less collection, `<collection>_tables_v1`, cleared by
  `delete_documents` and `delete_workspace` (the compliance purge). New
  settings: `QDRANT_TABLES_COLLECTION`, `TABLE_QUERY_ENABLED`.
- **Spreadsheet citations name the sheet and row.** `ai_response.citations`
  and panel citations gain optional `sheet`, `rowStart`, `rowEnd` and
  `computed`, and a cell quote is prefixed with its column header. **Socket
  payload change (additive):** the `Citation` schema has these four optional
  fields.

### Fixed

- **Scanned PDFs: a failed OCR page was dropped silently.** One transient
  Vision `503` lost a page of a 19-page judgment, with nothing in the text to
  say so. Transient failures now retry once. A page that still cannot be read
  keeps its `--- Page N ---` marker with an `[OCR note: this page could not be
  read ...]`, and so does a page read with low word confidence.
- **Scanned PDFs: OCR quality and speed.** `app/processing/ocr.py`:
  - The scan's own JPEG is sent instead of a 150-DPI re-render, which threw
    away 30-50% of a phone scan's pixels.
  - `document_text_detection` runs first (Vision's mode for dense pages and
    handwriting); the original `text_detection` is the fallback.
  - Reading order is rebuilt from word boxes on single-column pages. Vision
    had put "renewed from 02-05-2006 to 02-05-2012" out of order on widely
    spaced typed text.
  - Pages run 4 at a time on one shared client: a 15-page scan went from
    118 s to 5 s.
  Every step falls back to the previous behaviour. Over 147 real PDFs, the
  old and new code agree with 0 regressions: text-layer PDFs are
  byte-identical, and no scanned PDF has a new error, a lost page or a
  lower-resolution image. Guarded by `tests/test_ocr.py`.
- **Evidence was routed on a filename with no extension.** The backend's
  `filename` ("annexure-3-...-results") has no suffix. `extension` is the only
  type signal, and `process_batch` never passed it, so every file was routed by
  content sniffing and any ZIP went to the DOCX parser. `process_document` now
  takes `extension` and routes on `filename_for_extraction(filename,
  extension)`. The stored and displayed `filename` is unchanged. This also
  fixes audio sent with an extension-less `filename`, which was never sent for
  transcription. Guarded by `tests/test_office_extraction.py`.
- **PII redaction masked whole spreadsheet columns that are not
  identifiers.** The prose redactor decides from an 80-char window, and in a
  table that window reaches the "Account No" header from the next rows. On a
  500-row ledger it masked all 500 mobile numbers as bank accounts, plus 52 of
  500 transaction refs, about 10% of which pass the Aadhaar checksum alone.
  Spreadsheets are now redacted by column: each cell is judged with its own
  header as context (`scan_text(strict_context=True)`). The same ledger now
  has 0 mobiles and 0 refs masked, and all 500 account numbers masked.
- **Spreadsheet chunks repeated rows.** Chunk overlap duplicated 71 rows of a
  500-row sheet across chunks, so anything reading adjacent chunks counted
  them twice. Sheets are chunked without overlap; prose is unchanged.
- **Citation figures matched inside longer numbers and ISO dates were checked
  by year only.** "15.5" was supported by "115.5", and "2026-09-02" by any 2026
  date. Figures now match as whole numbers and equal by value across trailing
  zeros ("₹1,05,47,271.00" matches 10547271). ISO dates are checked whole.
  `_date_forms` now includes the ISO form, without which no chronology entry
  taken from a spreadsheet could be cited.
- **Entity resolution merged numbered parties.** "Player 1" vs "Player 10"
  scored 94 against a merge threshold of 92, so 40 players became 9 entities;
  "Accused No. 1" / "No. 10" had the same risk. Names whose numbers (or roman
  ordinals) differ are never merged.
- **Spreadsheets in the knowledge graph.** Graph ingest now skips spreadsheet
  chunks. The prose extractor has no node for a measurement and checks nothing
  against the row.
- **A debug log line could include a document quote.** Dropped citations are
  logged by keys only.
- **Chat replies carried no citations.** `ChatState` did not declare
  `evidence_index` / `citations` / `citation_report`, and LangGraph drops
  undeclared keys between nodes, so every `ai_response` went out with an empty
  `citations` list. Guarded by `tests/test_chat_state.py`.
- **Stop did not stop in-flight Gemini work.** google-genai 2.25 needs
  `aiohttp.ClientConnectorDNSError`, which aiohttp 3.9.5 lacks. A cancelled or
  failed request therefore surfaced as `AttributeError`, the client retried on
  the legacy SDK, and a stopped pipeline kept running, which also delayed a
  following resume. aiohttp is now 3.14.3, which also clears the 3.9-line
  security advisories. The Gemini client re-raises a cancellation instead of
  falling back (`_reraise_if_cancelled`).
- **Legal notices and past filings appeared on no panel.** The list-of-dates
  classifier dropped them as procedural, and the procedural timeline excludes
  them by design. They are now kept on the list of dates.
- **List of dates empty for undated letters.** Letters and representations
  often carry their date only in the file name ("25 11 29 Letter KCA.pdf",
  "30 Nov BCCI Letter.pdf"). The date is now read from the name, including the
  "annexure-N-" upload prefix (`app/utils/document_dates.py`), and used as the
  document's date and year anchor. The year backstop no longer moves a date
  into the future (it had turned a 2025 letter into 2026, which was then
  dropped as future-dated). The procedural timeline uses the same parser.
- **"Verification" deleted from ingested PDFs.** With one word per line in the
  PDF text layer, a 12-character word repeated on 3+ pages counted as a
  running header. "BCCI Age Verification Committee" became "BCCI Age
  Committee" in every panel. A single word is no longer a header.
- **"documents" rewritten to "annexures" everywhere.** Analysis-report text
  read "Annexure 1 annexures an official representation" and "identity
  annexures". Only references to uploads are rewritten now
  (`_upload_wording`).
- **Party names in logs.** Three log lines carried them: the metadata case
  title, the analysis-report party list and the applicable-laws payload dump.
  Two strategy retry warnings and the role-reassignment line did too.
  `verify_logging.py` now has a personal-data rule.

### Changed

- **Statute check uses India Code.** `check_provision` validates against
  `app/statutes/data/sections.json`: 165 acts and 12,530 sections fetched from
  India Code. It flags 16 repealed acts, never reads an Order/Rule as a
  section, and marks IPC-numbered BNS sections `invalid`.
- **Exact dependency versions.**
  - `requirements.lock.txt` is regenerated on Python 3.12 and verified by every
    suite on a clean venv.
  - `pytest` moved to `requirements-dev.txt`.
  - The Dockerfile is on Python 3.12 and installs the lock; it adds ffmpeg,
    poppler and antiword and runs as a non-root user.
  - `.dockerignore` excludes the credential files.

### Added

- **`sectionTitle` on applicable-laws rows (socket payload, additive).** This
  is the official section heading from India Code, e.g. Copyright Act s.19 →
  "Mode of assignment". `section` is unchanged. The frontend and the backend
  PDF need to render it (e.g. "Section 19 – Mode of assignment").
- **Production-readable logs.**
  - `event … received/finished` per inbound event.
  - `result <panel> delivered=yes|no items=… price=…` per emit.
  - The 5-second probing-prompts poll is at DEBUG.
  - `llm_call` lines name the calling feature instead of `-`.
- `scripts/e2e_local_backend.py` (`make e2e`): the pre-deploy end-to-end run.
  `--evidence FILE…` runs real case files.
- `scripts/fetch_laws_indiacode.py`, `scripts/build_statute_index.py`,
  `scripts/clean_master_laws.py`: the statute data pipeline.

- **Knowledge-graph retrieval (ontology agentic RAG), behind flags.**
  - **What it is:** the ontology, Neo4j graph store, fact extraction, entity
    resolution, contradiction detection and retrieval planner/critic from
    `feature/data_ingestion`, ported onto this branch's own hybrid RAG. No
    Keel server.
  - **Provenance:** facts carry evidence-store chunk ids and the page their
    text is on.
  - **Where it is used:** chat gets graph facts tied to citable passages;
    four panels can take a graph evidence block (augment only).
  - **Deletion:** every delete or purge withdraws graph facts, and a failed
    purge clear raises.
  - **Off by default:** `NEO4J_URL`, `ONTOLOGY_INGEST_ENABLED` and
    `RETRIEVAL_PANELS` control it. Neo4j 5 Community starts with
    `make graph-up` (compose profile `graph`).
  - **Statute register:** the branch's validated register replaces the
    previous loader, with the same `check_provision` behaviour.
  - **Tests:** `make test` now runs the 254 ported unit tests plus the
    offline suites. It used to point at a missing `test_setup.py`.

### Changed

- **Logging rebuilt for tracing and data safety.**
  - **Correlation:** every log line carries `request_id` / `event` /
    `workspace_id` from the inbound event, including lines from the tasks it
    spawns.
  - **Model calls:** one `llm_call` line per model call, with feature,
    status, duration and tokens. Failed calls are no longer traced as
    success.
  - **Audit log restored.** `logs/audit/audit.log` had stopped receiving
    events on 2026-04-02 because a second logging setup removed its sink.
  - **Removed from logs:**
    - raw socket payloads (dumped up to three times per event);
    - full model responses and document text;
    - signed URLs (one valid about 25 years);
    - a client mobile number, party names, chat and objective text, and
      fact values.
  - **Reconnect storm throttled:** one ERROR per refused handshake, with the
    diagnosis hint once per outage. It had been about 40% of `app.log`.
  - **Clean-up:**
    - 99 f-string log calls converted to placeholders;
    - emoji removed from 303 messages;
    - 13 duplicate error-then-exception pairs merged;
    - per-item dumps (about 30 lines per analysis report) replaced by count
      lines;
    - three unused example modules deleted.
  - **Sinks:** `app.log` follows `LOG_LEVEL`; file writes are queued off the
    event loop; tracebacks no longer include local variables unless
    `LOG_DIAGNOSE=true`.
  - **Regression guard:** `verify_logging.py`.


### Added

- **Accurate retrieval with verified citations** (`app/rag/`). Socket payloads
  gain only additive, optional fields, and the frontend should render them:
  - `ai_response.citations[]`: one entry per inline `[n]` marker, with
    `type` (document / case_law / statute), `documentId`, `annexure`,
    `fileName`, `page`, `quote`, or `url` for case laws and statutes;
  - `list_of_dates` / `procedural_timeline` entries: `evidenceSnippet`,
    `citation`, plus a corrected `pageNumber` and `sourceDocId`;
  - `analysis_report` item objects: `citations[]`;
  - `strategy_objective.caseSummaryCitations[]`;
  - `applicable_laws` rows: `sourceUrl`, `registerStatus`, `citations[]`;
  - `case_laws` cards: `citationVerified`.
- **Hybrid search.** Dense and sparse (legal-aware tokens such as `s.302`,
  `498A`, case numbers, amounts) with RRF fusion, query rewriting, LLM
  reranking, per-document diversity, and a rewrite-and-retry when nothing
  relevant is found.
- **Page-aware, structure-aware chunking** with overlap, in a new evidence
  collection. Legacy workspaces are re-indexed on first use or with
  `scripts/reindex_chunks.py`.
- **Evaluation:** `scripts/eval_rag.py` plus a golden-set format.

### Changed

- **Model** defaults to `gemini-3.8-flash`. `MINIMAL` thinking maps to `LOW`,
  because 3.8 Flash rejects `MINIMAL` and long-form continuations would
  otherwise fail. Pricing is $0.75 / $3.75 per 1M tokens until 2026-12-31.
- **Embeddings** use the correct task type for queries versus documents, an
  explicit dimensionality, batching and retries. They are never zero-vector
  substitutes.
- **Panels** get relevance-aware excerpts instead of a front cut, with a 200K
  budget (`PANEL_CONTEXT_CHARS`).
- **Law signals** are drawn from every document, and cited provisions lead the
  search query.
- **`qdrant-client`** 1.7.3 → 1.18.0.

### Fixed

- **Long documents were reassembled in alphabetical chunk order** for every
  feature.
- **`get_all_documents` dropped document metadata**, so prompt headers were
  always empty.
- **Page numbers were guessed or defaulted to 1.** No page markers were ever
  written.
- **Re-ingesting a document duplicated its chunks.**
- **`CHAT_MIN_SCORE_THRESHOLD` was never applied.**
- **Deleting a document or workspace now clears both collections.**


### Added

- **Stop and "Finish update" for a running workspace update**
  (`workspace_stop_request`, `workspace_resume_request`; new
  `app/socket/handlers/run_control.py`, schemas `WorkspaceStopRequestPayload` /
  `WorkspaceResumeRequestPayload`). Socket contract: two new inbound events;
  the only outbound change is that a resume's `workspace:refresh` carries
  `reason: "ai_resume_started"`.
  - **Stop** cancels the running update's tasks (never a chat turn), drops
    buffered deliveries in memory *and* in the persisted
    `PENDING_FEATURE_DELIVERY` copy, disarms recovery markers, and never
    replies. It is backed by a **run fence**: every workspace-scoped event runs
    in a `(workspace_id, epoch)` context scope, a stop bumps the epoch, and the
    socket proxy suppresses any `document_feature_response` or
    `workspace:refresh` from an older scope. So a stale refresh from the stopped
    run cannot re-arm the frontend's spinners, and a new run started after the
    stop is untouched. Re-sends under the same `requestId` are swallowed; a stop
    whose `stoppedAt` predates newer work is ignored as a stale re-send.
  - An **objective change interrupted by a stop is held**, not dropped (the
    resume payload does not carry the new objective): never auto-resumed on
    reconnect or restart, replayed only by a resume that includes
    `strategy_objective`, dropped by a new full run.
  - **Resume** declares exactly `reEmitFeatures` (unknown types dropped) in
    `workspace:refresh`, reconciles `evidenceFiles` against the vector store,
    produces one `document_feature_response` per feature in product order with
    `currentState` honoured, and releases any declared loader it could not
    satisfy. Idempotent on `requestId` (a failed attempt re-runs).
  - `_release_refresh_loaders` can now settle `document_assessments` and
    `workspace_metadata` from their cached envelopes.
  - Offline coverage: `verify_run_control.py`.

### Fixed

- **An objective change no longer hangs the workspace on "refreshing" when the
  socket drops mid-regeneration.** Confirmed root cause of the 2026-09-17
  incident: the objective change was accepted at 11:59:56 IST, the socket
  disconnected 25 s into a ~2-minute regeneration, and the work died leaving no
  log line and no emit — so all six panel loaders armed by `workspace:refresh`
  span for the rest of the session. `asyncio.CancelledError` derives from
  `BaseException`, so every `except Exception` on that path missed it.

  Six changes, in the order that matters:

  - **Cancellation is caught, reported and recoverable.**
    `_regenerate_strategy_for_new_objective` and the objective-change cascade
    runner now handle `asyncio.CancelledError` explicitly: they log the stage
    reached, record the instruction for resume, release any loader they armed,
    and re-raise. Detached tasks are held in `_background_tasks` with a
    done-callback (`_track_background_task`) — asyncio keeps only a weak
    reference to a task, so a fire-and-forget `create_task` could be collected
    mid-flight and reported nothing when it died.
  - **Emits now report delivery.** `SocketClient.emit` / `emit_ai_response`
    return `bool` instead of `None` (still never raising), and
    `_emit_document_feature` propagates it. An undelivered payload is buffered
    per `(workspace_id, featureType)` and re-sent on reconnect
    (`_flush_pending_feature_deliveries`), **before** the cached-feature replay,
    so the frontend receives the genuinely-generated objective rather than the
    pre-change snapshot. The buffer is persisted
    (`CacheKinds.PENDING_FEATURE_DELIVERY`) because the disconnect that loses an
    emit is often what restarted the process.
  - **Work that produced nothing is re-run.** An interrupted change is recorded
    as `CacheKinds.PENDING_OBJECTIVE_CHANGE` and resumed once on reconnect
    (`_resume_pending_objective_changes`), capped at one attempt and dropped
    after 15 minutes. A change whose payload was merely undelivered is *not*
    re-run — the cascade is scheduled instead.
  - **Gemini calls are bounded.** New `gemini_request_timeout_seconds`
    (default 300 s, env-overridable) applied in `generate`, `generate_longform`
    and `generate_grounded`; nothing in the service had a timeout before. A
    timeout on the new-SDK path does not fall back to the legacy SDK, which
    would have doubled the stall.
  - **LLM failures are no longer success-shaped.** `generate` reports failure by
    *returning* a sentinel string, so a 429 / 5xx / safety block used to surface
    as the far more misleading "model returned no JSON". New
    `is_generation_error()` is checked before parsing; the objective
    regeneration logs the real reason and retries once. Every give-up path now
    emits one structured line plus an `objective_change_failed` audit event with
    a reason code (`llm_error`, `no_json`, `no_objective`, `emit_dropped`,
    `cancelled`, …).
  - **A lit loader can no longer outlive its work.** New shared
    `_release_refresh_loaders` emits each panel's terminal state for any
    featureType that was armed but never emitted, and is called from all four
    objective-change failure branches and from a `finally` in the cascade
    runner. The chat reply now says plainly that the objective was not updated
    (`OBJECTIVE_CHANGE_FAILED_MESSAGE`) instead of falling through to the
    general chat agent.

  Also fixed in the same class: the analysis-report chat-update path armed its
  loader and skipped the emit when no cached report existed; cascade step 1
  (the analysis regeneration) was unwrapped, so a raise there aborted the
  remaining six panels; `_regenerate_strategy_for_new_objective` returned `True`
  without emitting when there was no socket client; and the dead
  `_suppress_analysis_emit_until_ts` guard (never assigned, and a no-emit
  `return False` if it ever had been) was removed.

  `workspace:refresh` now carries a `refreshId` so an arm/release pair can be
  read end to end in the log — the 2026-09-17 cause had to be reconstructed from
  the *absence* of lines. The frontend may ignore the field; echoing it back on
  `document_feature_response` is a follow-up that needs backend agreement.

  A vague request ("just revise the strategic objective as per proper
  documents") is no longer passed through *as* the new objective: it is
  reframed as a document re-derivation
  (`_document_grounded_objective_instruction`), and `revise` / `rework` /
  `reframe` / `redefine` / `regenerate` were added to the panel-edit verb gate.

  Offline coverage: `verify_objective_change.py` group 12.

### Changed

- **The quality score explanation is now objective-relative and always
  refreshed by an objective change.** `_ensure_quality_score_explanation` takes
  the current strategic objective and passes it to all three producers (the LLM
  pass, the deterministic fallback, and the main analysis-report prompt), each of
  which must state how the objective bears on the score. The objective-change
  cascade forces the refresh when the regenerated report's explanation comes back
  empty or unchanged — the case that used to leave stale text on screen, since
  `qualityScoreExplanation` is among the first fields lost to JSON truncation and
  the generic refill is objective-blind. `reportQualityScoreExplanation` is
  re-derived alongside it. The objective is also threaded through the
  `analysis_report_feedback`, `added_missing_information` and `workspace_sync`
  merge regenerations.
- **An objective change now updates the Case Summary as well.** Declaring a new
  strategic objective re-frames `caseSummary` and `reportCaseSummary` alongside
  the objective, next steps and chips, in one call and one coalesced
  `strategy_objective` emit (`_regenerate_strategy_for_new_objective`, renamed
  from `_regenerate_strategy_objective_preserving_case_summary`).

  Re-framing is emphasis and ordering, **never facts** — the summary is the
  fact-of-record narrative every other panel's prompt is built on. Three
  deterministic checks enforce it: every figure in the previous summary must
  survive (new `find_dropped_case_figures`), no new organisation/party/place may
  appear (`find_ungrounded_proper_nouns`), and the mandated opening must be
  intact. A candidate failing any check is discarded and the previous text
  stands; the new objective lands either way, and the chat acknowledgement names
  the case summary only when it actually moved.

  Unchanged: **role reassignment** still locks both summaries
  (`refresh_case_summary=False`), and a **surgical** objective edit
  (`_regenerate_strategic_objective_with_feedback`) still preserves them
  byte-for-byte.

### Added

- **New inbound event `document_reload_request`** (`app/socket/schemas.py`,
  `app/socket/client.py`, `app/socket/events.py`,
  `app/socket/handlers/document_reload.py`). The lawyer hits "reload" on one
  document in Supporting Material — or the backend does, because the document
  scored 0 — and the AI purges everything it holds for that file, re-fetches the
  freshly-signed URL, re-extracts it, and re-runs the workspace feature suite
  over the full evidence set. Until now the only recovery for an unreadable
  upload (a scan whose OCR produced nothing, a failed transcription) was to
  delete the document and upload it again, which threw away its id and every
  curated item tied to it. **Socket payload change** —
  `DocumentReloadRequestPayload` carries `requestId`, `targetFile` (an evidence
  file plus an advisory `previousQualityScore`), the full active `evidenceFiles`,
  a MANDATORY `currentState`, and optional `participants`. Steps 6–7 of the flow
  are the emits a `workspace_sync` re-run already produces
  (`workspace:refresh`, then `document_feature_response` per feature), so the
  whole output path is reused. Four rules shape the handler: the document's
  vectors and its entry in the cached `document_assessments` manifest are purged
  **before** the re-fetch, so no prompt is built on the copy the reload exists to
  replace; the document's backend id is **resolved, never invented** (`_id` →
  matching `fileHash` → `storeFileName`/`path` → a unique filename match in the
  manifest), and an unresolvable reload is abandoned rather than ingested under a
  second identity no later delete could reach; the removed-evidence ledger is
  written **only when the re-read fails** and cleared when it succeeds, so a good
  reload never reports its own document as a `missingInfo` gap; and re-sends
  under the same `requestId` are swallowed while in flight or just completed but
  **re-run after a failure**, since the backend eventually stops retrying. The
  re-ingestion runs under its own `capture_usage()` scope, and the reload
  revokes a pending compliance purge like every other live-workspace event.
  Scoped to the one document: drift between `evidenceFiles` and the live vector
  store is logged, never acted on.

- `verify_document_reload.py` — offline verification of the reload: schema,
  id resolution (including the ambiguous cases it must refuse), purge-before-read
  ordering, manifest merge, ledger behaviour on success and failure, curation
  survival, the `requestId` de-dupe, and purge revocation. 69 checks, no API key,
  no backend, no Qdrant.

- `verify_objective_change.py` — offline verification of the re-frame and
  of every case in which it must be refused.

- **Workspace sharing.** Two new optional inbound fields, neither of which
  changes `userId` (it stays the case OWNER on both, so owner-scoped caches keep
  their key):
  - `workspace_sync.participants` — `[{userId, name, permission}]`, permission
    `edit` or `view`. Absent entirely (not `[]`) when the case is not shared, so
    an absent field on `workspace_sync` is read as *un-shared* and drops the
    roster. `workspace_version_restore` inherits the field but is not
    authoritative over it.
  - `user_message_request.askedBy` — `{userId, name}`, present only when someone
    other than the owner typed the message.

  The roster lives in `app/workspace/participants.py` (`CacheKinds.PARTICIPANTS`,
  hydrated on a cold workspace, cleared by the `workspace_deleted` purge).
  `resolve_asker(...)` turns the pair into a permission: a `view` collaborator
  gets the same analysis for any question, but chat-driven **writes** are
  skipped — panel edits, fact corrections, the objective-change cascade,
  AI-marker panel updates and role selection — and the reply says so instead of
  implying the change landed. The asker also reaches the chat prompt as a short
  attribution block; participants are app users, not parties to the dispute, so
  they are kept out of every other prompt.
- `verify_workspace_share.py` — offline verification of the sharing path
  (schemas, roster lifecycle, permission resolution, and end-to-end viewer /
  editor / owner chat behaviour).
- **User-asserted fact ledger.** A fact the lawyer corrects in chat ("the disputed
  amount is 6 lakh USD, not 16 lakh") is now pinned per workspace
  (`CacheKinds.USER_ASSERTED_FACTS`, `app/socket/handlers/user_facts.py`). Every
  generator prompt carries it as an AUTHORITATIVE USER CORRECTIONS block, and a
  deterministic pass at the `document_feature_response` chokepoint enforces it on
  the way out — so a later `workspace_sync` regenerating from documents that still
  state the old value can no longer revert the correction. Corrections to the same
  fact supersede rather than accumulate. Mirrors the existing `REMOVED_DOCUMENTS`
  contract (prompt block + deterministic pass + ledger).
- **Fact corrections fan out across every panel**, behind
  `ENABLE_FACT_CORRECTION_FANOUT` (default `true`). New classifier signal
  `isFactCorrection` / `factCorrections` (Rule 14). Text panels are edited by the
  existing surgical editors; `list_of_dates`, `applicable_laws` and
  `procedural_timeline` are corrected by a Python substitution restricted to an
  allowlist of text fields — never an ISO `date`, never a verbatim
  `evidenceSnippet`.
- `verify_fact_corrections.py` and `verify_ingestion.py` — offline verification
  of the chat-edit path and the ingestion pipeline (no API key, backend or
  Qdrant required).

### Changed

- **Socket payload behaviour**: a chat panel edit now emits a
  `document_feature_response` for every panel whose loader it lit, including a
  preserved re-emit when a panel did not change. Previously only the rename path
  did this, and only for three panels.
- The chat confirmation after a panel edit now names which panels changed, which
  did not contain the targeted content, and which failed — instead of asserting
  blanket success.

### Fixed

- **`python -m app.socket.ai_service` started a half-booted service.** That entry
  point opened the socket and nothing else — the vector store, document
  processor and Gemini client were never initialised, and `stop()` disconnected
  without cleaning either of them up. It reported a healthy connection and then
  failed on the first document, which reads like an ingestion bug rather than a
  service that was never booted. `AISocketService.start()`/`stop()` now delegate
  to `app.core.lifecycle`, and the CLI entry runs the same `Application` as
  `python -m app.main` (full startup, SIGTERM/SIGINT handling, logging setup,
  graceful shutdown), so the two commands are equivalent and cannot drift.
- **`python -m app.main` did not start.** Two import cycles, closed at
  `app/agents/base.py` and `app/socket/__init__.py`. Importing any module under
  `app.socket` — even pure-pydantic `app.socket.schemas` — executed the package
  `__init__`, which eagerly pulled in the whole handler stack and cycled back
  through `app.workspace.context`. `SocketClient` / `event_handler` now resolve
  lazily (PEP 562, public API unchanged) and `memory_manager` is imported inside
  the two methods that use it. The service boots and connects again.
- **Policy boilerplate was read as case evidence.** A standard-form terms of
  service, privacy statement or acceptable-use policy names its own publisher,
  its version-control signatories and its third-party vendors — none of them
  parties to the dispute. With no way to tell such a document from a
  case-specific contract, a boilerplate template's "Service Provider" was
  selected as the client over the individual actually named in the evidence. New
  `policy_boilerplate` document type plus a classifier-independent
  `is_policy_boilerplate()` detector; such annexures are rendered with a
  `[CITED INSTRUMENT]` banner, and both the entity extractor and the role→party
  resolution are told not to take parties or the client from them.
- **A lawyer's own drafted objective was paraphrased.** Pasting finished
  drafting into the Strategy chat returned a reworded version. The regeneration
  prompt now reproduces a drafted objective verbatim and derives only the next
  steps and chips from it.
- **A new objective declared outside the Strategy & Objective chat did nothing.**
  Classifier Rule 4 declined it as a surgical edit and the objective-change flow
  was gated on `chat_type`, so it was silently dropped. Rule 4 now emits
  `isObjectiveDeclaration` + `objectiveDeclarationText`, which carries the
  declaration across that gate with no extra model call; the looser promotion
  heuristic in `_llm_classify_objective_change_intent` stays Strategy-only.
- **Custom instructions survived a workspace deletion.** `_custom_instructions`
  is a module-level dict and so outside the handler's `__dict__` purge sweep;
  `clear_custom_instructions()` is now called on `workspace_deleted`.
- **PDF extraction silently deleted body text.** `_strip_repeated_pdf_boilerplate`
  drops lines that recur across pages as running headers. PyPDF2 emits many PDFs
  one word per line, so ordinary function words recurred on every page and were
  deleted — *"our mission is to improve the effectiveness and sophistication of
  partnership"* reached the model as *"our mission improve effectiveness
  sophistication partnership"*, leaving it to reconstruct the missing connective
  tissue. A repeated line must now carry header substance (>= 12 chars or >= 3
  words) before the repetition signal fires; known watermark patterns are
  unaffected. An implausible strip ratio now logs a warning instead of passing
  silently.
- **Non-parties were extracted as case participants.** The three entity
  sub-extractors ran without `SPEAKER_LABEL_GUARDRAIL` (imported but never
  used), so audio diarization placeholders — "Caller", "Receiver", "Speaker A" —
  were returned as parties. The parties prompt also asked for "official
  signatories", which pulled the Author / Reviewer / "Approved By" rows out of an
  uploaded policy's Document Control table. Because these entities become the
  EXTRACTED FACTS block that downstream prompts treat as *"the ONLY
  organisations, authorities, bodies, parties and places you may name"*, the
  leakage was not merely tolerated — it was authorised and propagated into the
  case summary, the strategic objective and the analysis report. The guardrail is
  now injected into all three extractors, the prompt excludes document-control
  metadata and policy boilerplate entities, and `_drop_non_party_entities`
  enforces both deterministically.
- **A chat edit no longer reports success when nothing changed.** The three
  strategy editors (`strategicObjective`, `caseSummary` + `reportCaseSummary`,
  `possibleNextSteps`) returned `True` whenever Gemini returned any non-empty
  text, including byte-identical output, so the reply claimed panels it never
  touched. They now detect a no-op, as the other four editors already did.
- **Stuck panel loaders.** Any editor returning `False` left its spinner lit
  forever; the terminal-emit guard now covers every panel on every edit path.
- **Applicable-laws edits could silently drop rows and lost `isMatched`.** The
  editor now merges the model's `description` by index onto the cached row and
  re-checks the count after normalisation, rejecting the edit on shrinkage. This
  also revives the no-op guard, which could never fire because normalisation
  stripped `isMatched` from the comparison.
- **Surgical analysis-report edits could blank `nextSteps` / `pendingActions` /
  `caseResolutionScore`** when the model returned only the sections it changed.
  The edit is now layered over the current report so unmentioned keys survive.
- **Both analysis-report scoring calls ran with no case description.**
  `_compute_rejection_score_delta` and `_compute_added_missing_info_score_delta`
  read `caseSummary` / `reportCaseSummary` / `strategicObjective` off the analysis
  report, which never contains them, so the prompts rendered "(not available)".
  They now read the strategy payload, where those fields actually live. **Expect
  `caseResolutionScore` values to move on `analysis_report_feedback` and
  `added_missing_information` events.**
- **The panel-edit precheck matched verbs as substrings** — "edit" inside
  "credited", "add" inside "address" — sending ordinary questions to the LLM
  classifier. Now word-boundary matched, with a new contrastive-correction branch
  so a value correction with no edit verb ("the amount is 6 lakh, not 16 lakh")
  reaches the classifier at all.
- `_schedule_objective_change_reanalysis` now takes the per-workspace pipeline
  lock, as the other schedulers do; its full analysis-report regeneration could
  previously interleave with a document-upload pipeline. The strategy cache is
  also re-read after the classifier call, so a sync completing during that
  round-trip is no longer silently discarded.

---

## [5.1.0] - 2026-07-31

### Added

- **Paid non-LLM API cost is now tracked and reported alongside LLM token cost** (`app/ai/usage.py`, `app/ai/pricing.py`, `app/core/config.py`, `app/processing/file_handlers.py`, `app/processing/audio_transcription.py`, `app/search/vertex_ai_search.py`, `app/ai/gemini_client.py`, `app/workspace/context.py`, `app/socket/handlers/chat.py`, `app/socket/schemas.py`). Costing was token-only, so every paid Google Cloud call the pipeline makes was billed to nobody: Vision OCR (1 unit per image **and per PDF page**), Speech-to-Text V2 / Chirp 3 (audio seconds, including retries and chunk overlap), Vertex AI Search (per query), Google Search grounding (per request), and embeddings (per input token). `UsageAccumulator` now carries `ocr_units` / `stt_seconds` / `search_queries` / `grounding_requests` / `embed_tokens` — reported via new `record_ocr_units` / `record_stt_seconds` / `record_search_queries` / `record_grounding_requests` / `record_embed_tokens` helpers, priced by the new `service_price_for()`, and accumulated per workspace by `add_token_usage`.

  **Socket payload change** — `AIResponseMetadata` (and therefore the `metadata` block on both `ai_response` and every `document_feature_response`) gains per-response `ocrUnits`/`ocrPrice`, `sttSeconds`/`sttPrice`, `searchQueries`/`searchPrice`, `groundingRequests`/`groundingPrice`, `embedTokens`/`embedPrice`, `servicePrice`, and the matching cumulative `total*` fields plus `totalServicePrice`. **`price` and `totalPrice` now include this service cost** — `price == inputPrice + outputPrice + servicePrice` and `totalPrice == totalInputPrice + totalOutputPrice + totalServicePrice`. Every field is optional, so consumers that ignore them are unaffected; a consumer that *recomputes* `price` from `inputPrice + outputPrice` will now understate the bill and should read `price` (or add `servicePrice`) instead.

  Rates live in `app/ai/pricing.py`, were verified against Google's published pricing in July 2026 (Vision $1.50/1k units, STT $0.016/min, Vertex AI Search $1.50/1k queries, embeddings $0.15/1M tokens), and are each overridable from the environment (`OCR_PRICE_PER_1K_UNITS`, `STT_PRICE_PER_MINUTE`, `SEARCH_PRICE_PER_1K_QUERIES`, `GROUNDING_PRICE_PER_1K_REQUESTS`, `EMBED_PRICE_PER_MILLION_TOKENS`). The grounding default is model-aware — $35/1k grounded prompts on Gemini 2.5 vs $14/1k search queries on Gemini 3 — so switching `GEMINI_MODEL` to a Gemini 3 model does not over-report by 2.5x. Neither free tiers nor volume discounts are modelled (both are monthly and account-wide), so reported cost is an upper bound on the true invoice. `embedTokens` is a ~4-chars/token estimate because the legacy `genai.embed_content` response carries no `usage_metadata`.

- **Deleted documents are now reported as missing information** (`app/workspace/persistence.py`, `app/socket/events.py`, `app/socket/handlers/state_roles.py`, `app/socket/handlers/documents.py`, `app/socket/handlers/analysis_report.py`, `app/socket/handlers/objective_change.py`, `app/socket/handlers/workspace_events.py`). Deleting a document made it vanish from the intelligence entirely instead of surfacing as a gap — if a matter's invoice was deleted, nothing said the invoice was missing. A new `CacheKinds.REMOVED_DOCUMENTS` ledger snapshots each pruned `document_assessments` entry (`documentId`, `fileName`, `documentType`, `keyFields`, `removedAt`) *before* the entry is dropped, so the classification outlives the document. `_build_removed_documents_block` injects a REMOVED EVIDENCE block into the analysis-report prompt ahead of STEP 2, and both the DOCUMENT-TYPE COVERAGE TABLE and the STRICT EXCLUSIONS list (which forbade ever flagging a missing `invoice` / `receipt` / `proof of purchase`) now carry an explicit carve-out for removed documents. Because prompt compliance is not a guarantee, `_apply_removed_evidence_missing_info` deterministically appends one `missingInfo` entry per removed documentType the agent did not raise itself, re-clamping `caseResolutionScore` to 95 when it does. These items are exempt from rejected-item memory: a gap the user dismissed *because* a document answered it is real again once that document is withdrawn. Re-ingesting the same `documentId` (upload or restore) clears the ledger entry, so the flow is self-healing.

- **New inbound event `workspace_version_restore`** (`app/socket/schemas.py`, `app/socket/client.py`, `app/socket/events.py`, `app/socket/handlers/workspace_events.py`). A version restore was previously indistinguishable from a normal add: if the restored file's vectors had been deleted it was silently re-ingested and re-billed, and if they had not, the "already indexed" filter skipped it and **no panel changed at all**. **Socket payload change** — `WorkspaceVersionRestorePayload` extends `WorkspaceSyncPayload` with a required `versionId` and an advisory `restoredDocumentIds`. `on_workspace_version_restore` is a separate handler rather than another branch of `on_workspace_sync`, whose `if/elif` chain computes `skip_reprocess` only when nothing was deleted and would have swallowed the restore. It reconciles against reality rather than trusting the delta — diffing the live Qdrant document set against `evidenceFiles` to derive what to delete and what to ingest — then deletes, prunes, ingests under its own `capture_usage()` scope, and regenerates the full suite. Idempotency is keyed on `versionId` in its own 180 s window, since restoring back to a recently-live version produces a document set the sync fingerprint has already seen. Passing both `batch_result` and `deleted_doc_ids` to the tail is deliberate: the latter drives replace-not-merge for the provenance-less `applicable_laws` / `case_laws` panels, while the former keeps `list_of_dates` / `procedural_timeline` off their pure-deletion fast path so restored documents are actually re-read.

### Changed

- **`document_assessments` envelope construction is now a single shared helper** (`app/socket/handlers/documents.py`, `app/socket/handlers/workspace_events.py`). `_build_document_assessment_envelope` replaces three would-be copies of the same 60-line transformation (created / sync / restore paths), which were byte-identical including the audio-collapse and failure branches.

### Fixed

- **The document inventory no longer lies about which documents are present** (`app/socket/handlers/documents.py`, `app/socket/handlers/workspace_events.py`). `_build_document_inventory` is built from the cached `document_assessments` payload and is injected into every feature prompt as *"GROUND TRUTH for which documents are present"* with the instruction not to flag anything listed there as missing — but that cache was wrong in both directions. **On delete** it was never pruned (the delete branch removed Qdrant vectors and regenerated panels but left the cache untouched), so the agent kept asserting a deleted document was still in evidence and suppressed it from `missingInfo`. **On incremental upload** it was overwritten with only the newly-processed files, so the inventory and `_get_annexure_count` collapsed to the new-file count and previously-uploaded documents started being flagged as missing. New `_prune_document_assessments` (which also feeds the removed-evidence ledger) runs before any regeneration builds a prompt, and `_merge_document_assessments` keys by `documentId` so the cache stays a manifest of every live document. The incremental emit is unchanged on the wire; only the cached copy carries the full manifest, which additionally means a reconnect replay now re-sends the complete set. The all-documents-deleted branch (`evidenceFiles: []`) prunes the whole cache the same way.

- **Document-ingestion spend is no longer lost** (`app/socket/handlers/workspace_events.py`). `process_batch` ran outside any `capture_usage()` scope on both `workspace_created` and `workspace_sync`, so `record_usage` was a silent no-op and the per-document summary / evidentiary-strength / classification Gemini calls — plus all ingestion OCR, transcription and embedding spend — were billed to nobody. The `document_assessments` emit reported per-response zeros via `_cumulative_usage_metadata`. Both call sites now wrap `process_batch` in `capture_usage()` and emit through the new `_ingest_usage_metadata` helper (which supplies the `messageId` the backend `WorkspaceCostService` requires). The helper is idempotent per accumulator, so the ingestion error path reports genuine partial spend (e.g. OCR that ran before the failure) without any chance of double-reporting. Reconnect replay still overwrites the metadata with cumulative-only values, so a dropped socket never re-bills.
- **A workspace's cumulative bill survives a restart again** (`app/workspace/persistence.py`, `app/socket/handlers/workspace_events.py`). `ContextManager.upsert_workspace` creates a context with empty `metadata` on cold memory and persists it immediately, and `upsert_workspace_state_cache` fully overwrites the `workspace_meta` point — so the first `workspace_sync` after an AI-service restart wiped the persisted `token_usage` and `totalPrice` restarted from 0 (reachable via `_resolve_workspace_role` → `upsert_workspace`, before any hydration). `on_workspace_sync` now hydrates from Qdrant before anything can upsert (as the chat path already did), and `persist_state` refuses to let a payload with no `token_usage` clobber a stored one — the recovered block is written back into the live context metadata too, so the next `add_token_usage` continues from the real total instead of counting up from zero.
- **JSON-repair retries are now billed** (`app/ai/gemini_client.py`). When a JSON-mode generation came back malformed, `generate()` made up to two extra `generate_content` calls and recorded none of them, so every feature using `response_mime_type="application/json"` (list_of_dates, analysis_report, applicable_laws, case_laws, relevant_parties, procedural_timeline, …) under-reported whenever its output needed repairing.
- **Partial spend is reported when a document *sync* fails mid-ingestion** (`app/socket/handlers/workspace_events.py`). `workspace_created` already reported the OCR/STT burned before a crash; the `workspace_sync` re-ingestion path had no equivalent, so that spend was lost. Its error emit now carries the same idempotent `_ingest_usage_metadata` block. The sync success emit also refreshes the reconnect cache, so a replay no longer re-sends the older `workspace_created` assessments payload.
- **Token spend consumed with no usage scope open is now visible** (`app/ai/usage.py`, `app/socket/handlers/chat.py`, `app/socket/handlers/template_sync.py`). `record_usage` returned silently when no `capture_usage()` scope was open — unlike the five paid-service recorders, which warn once per kind — so an uninstrumented LLM path was billed to nobody *and* invisible in the logs. It now shares the same warn-once path. The three handlers that tripped it are instrumented: `probing_prompts_request` bills its contextual-chip generation to the workspace (**socket payload change**: `probing_prompts_response` gains the standard `metadata` block, but only when chips were actually generated — a cached-chip poll spends nothing and carries no block), while template ingestion and blueprint proposals are user-scoped with no workspace to bill, so their spend is priced and logged via the new `log_unbilled_usage`.
- **A feature's spend is billed even when the socket is down** (`app/socket/handlers/chat.py`). `_emit_document_feature` returned before building the metadata block when `_socket_client` was falsy, and since building it is what increments the workspace cumulative, the whole feature's tokens/OCR/STT were dropped. The block is now built first (billing the spend) and only the emit is skipped.
- **Metadata keys that the backend contract has no field for are flagged** (`app/socket/handlers/chat.py`). `document_feature_response` has no pydantic model, so its hand-built `metadata` mirrored `AIResponseMetadata` by convention only and a typo'd key shipped silently — the backend `WorkspaceCostService` would then never record that cost. Emits now log a warning listing any key absent from `AIResponseMetadata`; nothing is dropped or renamed.
- **Audio re-diarization Gemini tokens are now counted** (`app/processing/audio_transcription.py`). The three raw-SDK helpers (`_generate_gemini_json_raw`, `_generate_gemini_plaintext_raw`, `_generate_gemini_text`) call `GenerativeModel.generate_content` directly, bypassing `GeminiClient`'s metering hook, so their tokens were never recorded. A new `_record_audio_gemini_usage` reads `usage_metadata` at each site.

---

## [5.0.1] - 2026-07-09

### Fixed

- **Surgical panel edits from chat now reply with a real confirmation instead of the terse `"Updated: <panel>."` one-liner** (`app/socket/handlers/chat.py`, `app/socket/handlers/strategy.py`, `app/prompts/strategy_prompts.py`). When a lawyer's message is routed to the free-form panel-edit path (e.g. *"remove 1. … from possible next steps"*), the intelligence chat agent is intentionally skipped — but the acknowledgement was a bare `"Updated: possible next steps."`, which read as the AI not responding. The pre-dispatcher now records each applied per-panel edit instruction (`panel_edit_instructions`), and a new helper `_build_panel_edit_ack` (prompt: `panel_edit_ack_prompt`) makes one small Gemini call to compose a 2–4 sentence plain-text confirmation stating **what** was changed and the resulting panel state (e.g. how many next steps remain and which is now first; renames confirm the replacement across the affected panels). On any generation failure it falls back to a templated acknowledgement — never worse than before. The `ai_response` shape, the surgical-edit routing, the skip of the full chat graph, and the no-cascade rule for panel edits are all unchanged.

---

## [5.0.0] - 2026-07-07

Two new intelligence panels (**Case Laws** and **Relevant Parties**), manual case-law upload, multi-draft template matching in the Drafting Assistant, and a large no-behaviour-change decomposition of the socket layer. **Major version bump: several socket payload contracts changed shape** (party role vocabulary and field names, drafting-suggestion match fields, case-law item ids) — the backend/frontend must be released in step.

### Added

- **Relevant Parties panel** (`app/socket/handlers/relevant_parties.py`, `app/socket/handlers/panel_edits.py`, `app/socket/schemas.py`, `app/socket/client.py`, `app/workspace/persistence.py`, `app/langgraph/chat_models.py`, `app/langgraph/chat_nodes.py`). New document-feature panel, mirroring `case_laws`/`applicable_laws`, that lists the key people/organisations who are actually party to the matter — each with a normalized **role badge** and a one-line description. It reuses the cached party extraction (`_extract_parties_subagent`) and adds a classification pass (`_classify_relevant_parties`) that maps each party into a fixed role enum **using only the document-grounded names (never invented)** and applies a **relevance filter** (comparative/example-only or tangential entities are dropped; genuine parties, counsel, and the adjudicating authority are kept). Generated on `workspace_created` / `workspace_sync` (document-driven, preserved on objective change), cached per-workspace (`CacheKinds.RELEVANT_PARTIES`), re-emitted on reconnect.
  - **New outbound payload** `document_feature_response` with `featureType:"relevant_parties"`: `payload.parties[]` of `{ id, name, role, details, contactNumber, email, officialIdUrl }` (capped at 30). `role` ∈ `Petitioner | Respondent | Witness | Victim | Accused | Other` (Title Case; legacy/synonym wordings like APPELLANT, PLAINTIFF, DEFENDANT, ADVOCATE, AUTHORITY are normalized via `_normalize_party_role`). The contact fields are **PII: always emitted empty on auto-generation** and only populated when the user supplies them through the panel-edit events. Schemas: `RelevantPartyItem` / `RelevantPartiesPayload`.
  - **New inbound CRUD events** `add_relevant_party` / `edit_relevant_party` / `delete_relevant_party` (the card add/edit/delete icons). Add appends (deduped by name); edit updates a party by `partyId`; delete removes by id and records the name in a per-workspace deleted-set (`CacheKinds.DELETED_PARTIES`) so regeneration won't re-add it. Each mutates the cache and re-emits `relevant_parties`. User-added parties are flagged `userAdded` so document-driven purging never removes them.
  - **Per-document provenance (deterministic on delete, incremental on upload).** Each auto-extracted party carries the `sourceDocIds` it was drawn from. Deleting a document purges exactly that document's parties; uploading one extracts parties **only from the new document** (per-doc extraction, classified in one pass) instead of re-deriving the whole workspace; a missing/stale provenance state (legacy cache, first run) triggers a one-time full refresh that keeps only user-added parties. This stops the "New for Review" churn the old whole-workspace re-extraction caused.
  - **New chat scope** `chat_type:"RELEVANT_PARTIES"` → `active_module="relevant_parties"`; cached parties are injected into `ChatContext.relevant_parties` and rendered in the chat context so a parties-scoped chat reasons only over listed parties.

- **Case Laws panel — relevant precedents with source links** (`app/search/case_law_finder.py`, `app/socket/handlers/case_laws.py`, `app/socket/schemas.py`, `app/ai/gemini_client.py`). New document feature, mirroring `applicable_laws`, that surfaces prior court judgments relevant to the workspace, each carrying a real **source URL** (citation, court, one-line holding, and a High/Medium/Low relevance badge). Sources are aggregated across three channels — Indian Kanoon direct search, Vertex AI Search fallback, and **Gemini `google_search` grounding** (real-time, for the latest judgments) — via `find_case_law_sources()`. The selection is **strict source-verified**: the LLM ranks and summarizes only the retrieved judgments and may not invent citations; any case not backed by a retrieved source URL is dropped. Generated on `workspace_created` / `workspace_sync` alongside applicable laws, cached per-workspace (`CacheKinds.CASE_LAWS`), re-emitted on reconnect, and **preserved on objective change** (re-derived only when documents are added or removed).
  - **Query, quality gate & reconciliation.** A legal-issue search query is derived from the matter (`_derive_case_law_query`, doctrine/statute/remedy terms with party/brand names stripped) so Indian Kanoon returns real judgment pages instead of noisy web articles. After enrichment, a quality gate drops cards that would render half-empty (enrichment failed — e.g. an unfetchable news/blog page) or are off-topic (Low relevance + low confidence), so only substantive headnotes reach the panel. Pass 2 also re-extracts the case **title** from the judgment text and re-assesses the comparison fields against the real holding, so the title/relevance can never contradict the disposition. Net effect: higher precision, with the card count varying with how many sources enrich.
  - **Legal-digest output (two-pass, judgment-grounded).** Each case is rendered as a proper Indian headnote rather than a bare summary. Pass 1 (select & compare) picks the relevant precedents from the retrieved sources and produces the case-vs-matter fields (`relevance`, `matchedFacts`, `applicabilityReason`, `distinguishing`, `adverseImpact`, `confidence`). Pass 2 (enrich) **fetches the actual judgment text** (`fetch_judgment_text()`, Indian Kanoon `/doc/` page preferred) and extracts the judgment-intrinsic fields STRICTLY from it — `citation`, `equivalentCitation`, `court`, `bench`, `date`, `areaOfLaw`, `legalIssues`, `materialFacts`, `ratio` (ratio decidendi), `holding`, `relief` — so the headnote is grounded, not recalled. `bindingStrength`/`bindingStars` are derived deterministically from the court (Supreme Court = Binding ★★★★★; High Court = Binding-in-state ★★★★; National Commission = Highly Persuasive; State/District = Persuasive). `legalStatus` defaults to **"Not independently verified"** — good-law status is only asserted with real evidence (confirm on SCC Online / Manupatra).
  - **New outbound payload** `document_feature_response` with `featureType:"case_laws"`: `payload.caseLaws[]` of `{ id, title, citation, equivalentCitation, court, bench[], date, bindingStrength, bindingStars, areaOfLaw[], legalIssues[], materialFacts[], ratio, holding, relief[], summary, relevance, matchedFacts[], applicabilityReason, distinguishing[], adverseImpact, confidence, sourceUrl, sourceName, referenceUrl, referenceName, legalStatus, verified }` (capped at 8). Ids are stable `caselaw_N`; per the backend contract `citation` carries the combined `"Title, Citation"` string (idempotently recombined so legacy-cached entries conform too), and `referenceUrl` / `referenceName` carry the clickable **source-file link** (e.g. the Indian Kanoon `/doc/` page). Schemas: `CaseLawItem` / `CaseLawsPayload` in `app/socket/schemas.py`.
  - **New inbound event** `add_case_law` (`AddCaseLawRequest`: `workspaceId`, `userId`, `workspaceRole`, one of `citation` / `url` / `text`) — the "Add Case Law" button. The AI resolves and verifies a real source, assesses relevance, appends to the panel (deduped by URL), and re-emits `case_laws`; if no real source resolves, the cached panel is re-emitted unchanged.
  - **Manual case-law upload** — new inbound event `case_law_document_request` → outbound reply `case_law_document_response` (`app/socket/handlers/case_laws.py`, `app/prompts/case_laws_prompts.py`). A lawyer uploads a judgment file (stored in GCS by the backend); the AI fetches `documentUrl`, extracts its text, and **classifies whether it actually is a case law** (`classify_and_extract_uploaded_case_law_prompt`). Accepted documents are extracted into `{ citation, court, summary, relevance, referenceUrl, referenceName }`, merged into the Case Laws panel (re-emitting `featureType:"case_laws"`), and acknowledged with `accepted:true` + the extracted `caseLaw`; anything else (a contract, a notice, an unreadable scan) is rejected with `accepted:false` and a `rejectionReason` naming what the document actually is. Every path — including failures — emits a terminal response echoing `requestId`, so the frontend never waits forever. Schemas: `CaseLawDocumentRequest` / `UploadedCaseLaw` / `CaseLawDocumentResponse`.
  - **New chat scope** `chat_type:"CASE_LAWS"` → `active_module="case_laws"`; the cached precedents are injected into `ChatContext.case_laws` so a Case-Laws-scoped chat reasons only over listed (source-verified) precedents.
  - **New Gemini method** `gemini_client.generate_grounded()` returns `(text, [{title, url}])` from `google_search` grounding metadata. It runs the **sync** google-genai client in a worker thread on purpose: the async transport uses aiohttp and, with the search tool enabled, references `aiohttp.ClientConnectorDNSError` (aiohttp ≥3.10), but the service pins aiohttp 3.9.5 for python-socketio — the sync client uses httpx and is unaffected.
  - **Retrieval robustness:** the Indian Kanoon scraper parses the `result_title`/`docfragment` element (real case names + clean `/doc/<id>/` URLs) and retries with a trimmed query when a long many-term query returns nothing (Kanoon ANDs terms). Gemini-grounding **redirect URLs** (`vertexaisearch.cloud.google.com/grounding-api-redirect/...`) are resolved to their real destinations so links are verifiable, and the grounding model's prose answer is passed to the ranker as **research notes** (case names/citations it found on the live web) — giving real substance even when the bare source rows are thin, while every emitted case still maps to a real source URL. `find_case_law_sources()` returns `(sources, research_notes)`.

- **Multiple matched library drafts per drafting suggestion, each with its own % match score** (`app/langgraph/drafting_agent.py`, `app/langgraph/template_match_graph.py`, `app/socket/schemas.py`, `app/socket/event_utils.py`). A suggested-draft card no longer binds to a single "winner" template: **every** library draft of the matching document type is surfaced, each scored 0–100 for its fit to *this* case, so the lawyer picks one from a selection modal ("N DRAFTS FOUND" + per-draft "% Match" badge) instead of the AI silently choosing.
  - **New wire shape:** `DraftingSuggestionItem` now carries `matches[]` of `MatchItem` `{ templateId, templateName, category, matchPercent }`, sorted by `matchPercent` desc (`[]` for AI-only suggestions). The old top-level `templateId` / `templateName` / `matchMode` / `matchScore` / `matchReason` fields are replaced at the emit boundary by `event_utils.drafting_payload_to_wire` (the agent keeps the richer internal card shape).
  - The **minimum-match-score gate is removed**: weak fits (e.g. 34%) are shown with their honest score rather than hidden, so an uploaded template can always be found. Guaranteed template-coverage cards are now **grouped by document type** — same-type drafts share one card (all listed in its matches) instead of each spawning a duplicate card — and per-(card, template) scores are cached so re-scoring is cheap.

### Changed

- **Objective / case-perspective change now preserves the document-derived panels instead of regenerating them** (`app/socket/handlers/objective_change.py`). The Mode-2 cascade still regenerates the Strategic Objective + Next Steps, replaces the Analysis Report, re-scores List-of-Dates relevance, and regenerates Applicable Laws for the new objective — but **Relevant Parties, Case Laws, Upcoming Dates (procedural timeline), and Drafting suggestions are preserved and re-emitted from cache** (who the parties are, which precedents were found/uploaded, the court dates, and the lawyer's template library don't change with the legal objective; they regenerate only on document add/delete). Panel emit order now matches the main flow: applicable_laws → procedural_timeline → relevant_parties → case_laws → drafting.
- **Document deletion replaces derived panels instead of union-merging them** (`app/socket/handlers/workspace_events.py`, `app/socket/handlers/objective_change.py`). After a document is deleted, the regenerated Analysis Report **replaces** the previous one (analysis items are free text with no per-document linkage, so merging kept strengths/risks derived from the deleted document), and the web-search-derived panels (Case Laws, Applicable Laws) are re-derived with `merge_with_previous=False` so they reflect the reduced document set. Uploads keep the merge/accumulate behaviour.
- **Relevant-party role vocabulary and fields follow the new backend contract** (breaking): roles are Title-Case `Petitioner | Respondent | Witness | Victim | Accused | Other` (previously an UPPERCASE nine-role enum), the one-line text moved from `description` to `details`, `rawDesignation`/`sourceDoc` were dropped, and PII contact fields (`contactNumber`, `email`, `officialIdUrl`) were added (user-supplied only). Legacy-cached entries are coerced to the new shape on merge.
- **Centralized all socket prompts into `app/prompts/<feature>_prompts.py` — no behaviour change.** The 51 inline prompt f-strings that lived inside the socket handler methods were extracted, verbatim, into per-feature builder functions (e.g. `app/prompts/case_laws_prompts.py::select_case_laws_prompt`). Each handler now builds its interpolation values and calls the builder; prompt wording is edited in one place per feature. Verified by a multiset equality check against the pre-extraction source — both the literal prompt text AND every interpolated expression are byte-identical (51/51). The prompt packages depend only on stdlib + `app.prompts.legal_intelligence_system`, so `app.prompts` stays free of `app.socket` dependencies.
- **Decomposed `app/socket/events.py` (20,987 lines) into per-feature mixins — no behaviour change.** The monolithic `SocketEventHandler` is now a thin composition root (`app/socket/events.py`, ~444 lines) that owns `__init__` state + `register`/`handle`/`set_socket_client` + the `event_handler` singleton, and is composed from 21 feature mixin classes under `app/socket/handlers/` (case_laws, relevant_parties, applicable_laws, analysis_report, strategy, chat, workspace_events, objective_change, …). Pure module-level helpers/constants/regexes moved to `app/socket/event_utils.py`. Method bodies were relocated verbatim — verified by an AST method-body equality check (218/218 nodes identical) plus a runtime diff (222 methods + 21 `_handlers` keys unchanged, same singleton object). Public surface is unchanged: `event_handler` is still the only symbol other modules import. See `docs/SOCKET_EVENTS_ARCHITECTURE.md`.

### Fixed

- **Dateless source dates now resolve to the current year across every panel** (`app/langgraph/list_of_dates_agent.py`, `app/socket/handlers/strategy.py`, `app/socket/handlers/analysis_report.py`, `app/prompts/legal_intelligence_system.py`, `app/prompts/strategy_prompts.py`). A date the source states without a year ("25 May", "26 tareekh") was often stamped with a training-era year (commonly 2024) by the model. Two layers fix this: (1) an explicit **DATE INTERPRETATION** directive carrying today's date is injected into the structured system instruction and the strategy/analysis prompts — use the stated year when present, otherwise the current year, stepping back one year only if that would put the event in the future; (2) a deterministic backstop — `snap_hallucinated_year` tightened to `slack_years=0`, plus a new `snap_years_in_text()` that rewrites the year of ungrounded **full dates** embedded in free text (case summary, strategic objective, next steps, analysis strengths/weaknesses/risks/missing-info, quality-score explanations). Statute years ("IT Act, 2000", "BNS, 2023"), currency amounts (₹2000), bare years, and any year literally present in the source are never touched.
- **List of Dates / Applicable Laws / Upcoming Dates loaders no longer stick after a chat panel edit** (`app/socket/handlers/chat.py`, `app/socket/handlers/objective_change.py`). A Mode-1 rename turns the frontend spinners on via `workspace:refresh`, but a panel editor that had nothing to change (the token doesn't appear in that panel) returned without emitting — leaving the spinner on forever. A terminal-emit guard now re-emits the preserved cached payload for every doc-derived panel whose edit was a no-op. Likewise, a Case Laws failure during an objective edit now emits the cached-or-empty payload as a terminal state instead of leaving "Analysis is in progress".
- **Mode-1 surgical chat edits now handle TERM / DATE / YEAR corrections, not just names** (`app/prompts/applicable_laws_prompts.py`, `app/prompts/list_of_dates_prompts.py`, `app/prompts/procedural_timeline_prompts.py`, plus the matching handlers). "Change 2024 to 2026" is applied as a find-and-replace across the existing entries — including the ISO `date` field of List of Dates / Upcoming Dates entries — while statute names, section numbers, entry order, and every untouched field stay verbatim; nothing is regenerated.
- **Duplicate Relevant Parties after a re-extraction** (`app/socket/handlers/relevant_parties.py`). The exact-string dedupe key treated obvious variants of the same entity as different parties ("M/s Pro Sports Outlets Pvt. Ltd." vs "PRO SPORTS OUTLETS PVT LTD"), so a non-deterministic re-extraction re-surfaced them as "new". Party identity is now canonical (honorific/corporate prefixes and suffixes, parentheticals, and punctuation stripped) with a guarded fuzzy match (`_party_names_match`, ratio ≥ 0.92 + first-token agreement) absorbing 1–2-character spelling drift — and user-deleted parties are re-checked through the same logic so they stay deleted.

---

## [4.2.0] - 2026-06-29

### Added

- **Guaranteed template coverage in AI Suggested Drafts** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). Previously a reusable template surfaced as a suggested-draft card only when the (non-deterministic) suggestion LLM happened to propose a draft of that type for the current case posture — so changing case details (client / brand / new matter) could silently drop an applicable template even though its legal purpose still fit. Now `suggest()` runs a deterministic **template-coverage pass** (`_ensure_template_coverage`): for every ready template not already bound to a matched card, a focused **applicability check** (`build_template_applicability_prompt` / `TEMPLATE_APPLICABILITY_INSTRUCTION`) decides — by the template's **legal purpose + document type**, never its original parties/facts — whether a draft of that kind fits this matter. Applicable templates are **guaranteed** a case-framed card with the template pre-bound (`matchMode="matched"`, prematch locked for the kickoff click). Template-backed cards take priority and the panel cap is raised from 6 to 8 (`_MAX_SUGGESTIONS_WITH_TEMPLATES`) so applicable templates are never crowded out by AI-only suggestions. The suggestion prompt and matcher now also see the fuller `filing_context` (legal purpose) instead of only the coarse `document_purpose`.
- **Custom AI Draft mode (`draftMode="custom"`)** — bypasses the template/suggestion path entirely (`app/langgraph/drafting_agent.py`). When the lawyer picks "No, Custom AI Draft" the kickoff skips all template resolution and profiling and generates a draft directly from case context (role, analysis report, list of dates, applicable laws, strategy), still gathering any missing lawyer-input slots through the same `openQuestions` flow. Lets a lawyer produce a case-framed draft when no suitable template exists.
- **Content / substantiation clauses are captured as interactive template variables** for any document type (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). The profiler now treats substantive body clauses (claims, grounds, facts to be substantiated) as fillable slots, so the drafting kickoff asks the lawyer for every unfilled slot instead of carrying over the template's original facts. `PROFILE_SCHEMA_VERSION` bumped (now **4**) so cached templates auto re-profile on the next template sync / reconnect.

### Changed

- **The lawyer now confirms every template variable; nothing is silently auto-filled from case context** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). Previously the kickoff could pre-fill slots from the analysis/strategy and skip asking — risking a wrong value shipped into a draft. Variables now default to being asked, with a **global auto-fill quality gate** that rejects informal labels, contact ids, and vague values, and always asks for the recipient **email** and each **cost / compensation head** individually rather than collapsing them into one umbrella answer.
- **Draft render switched to `thinking_level="LOW"`** (`app/langgraph/drafting_agent.py`). Render and draft-translation long-form calls previously ran at `thinking_level="HIGH"`; they now run at `LOW`, which removed a latency regression and truncation risk on long drafts while still producing the full structure (gemini-3 bills thinking tokens against the output budget). The 64k output ceiling (`max_tokens=65536`) is unchanged.
- **No draft preamble; clean re-kick on a different card.** The render no longer emits lead-in/preamble prose before the draft body, and clicking a *different* suggested-draft or template card re-kicks the conversation cleanly instead of continuing the previous draft's session.

### Fixed

- **Profiler now always captures the recipient email and any hardcoded demand / compensation amounts as per-matter variables** (`app/langgraph/drafting_agent.py`) so they are asked at kickoff rather than carried over verbatim from the template. `PROFILE_SCHEMA_VERSION` bumped to force re-profiling of cached templates.
- **Draft language persists across revisions** (`app/langgraph/drafting_agent.py`). A draft rendered/translated into a target language stays in that language on subsequent revision turns (`draft_language` is persisted in session state), and the translate instruction was tightened so abbreviations and numerals are handled correctly.
- **Firm letterhead no longer repeats on every page of a generated draft** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). A running-header letterhead in a PDF template is extracted into the body of every page — with `--- Page N ---` markers on the OCR path (`file_handlers._extract_text_from_pdf_with_ocr`) and without markers on the text-layer path (`extract_text_from_pdf`) — and, fed verbatim to the render LLM as the structural contract, was reproduced at the top of every page. Template text is now normalised at ingest (`_normalize_template_text`) for **both** paths: the marker-based pass strips `--- Page N ---` markers and collapses the header block that repeats across the majority of pages, and a marker-independent backstop (`_dedupe_leading_repeated_block`) collapses a leading block that recurs verbatim through the body (text-layer PDFs). Both are conservative — only a *leading* block that recurs verbatim is removed, so ordinary body prose and DOCX/single-page extracts are untouched. `DRAFT_RENDER_INSTRUCTION` also instructs the model to treat repeated letterheads / page markers as extraction artifacts (render once, never emit page markers).
- **Literal template placeholders (e.g. `<Client Address>`) are now always captured and asked, instead of shipping unfilled** (`app/langgraph/drafting_agent.py`). The profiler's deterministic placeholder seed only worked for DOCX (`_deterministic_placeholders` returns `[]` for PDF-only templates), so a `<...>` slot in a PDF template relied entirely on the profiler LLM noticing it in prose — and when missed, it was never added to the interactive-variable registry, never asked at kickoff, and rendered as a literal hole. Now literal slots are also detected from the extracted text (`_literal_placeholders_in_text`, reusing the shared `PLACEHOLDER_PATTERNS`) to seed the profiler for PDFs too, and a deterministic safety net (`_ensure_placeholder_variables`) guarantees every high-signal slot (`<...>` / `{{...}}` / `(Pascal_Case)`) reaches the registry so `_ensure_registry_coverage` surfaces it as a kickoff question (such synthesised slots default to `fill_from_case=false` so they are always asked rather than risking a wrong auto-fill).
- **Kickoff questions no longer leak the template's original subject matter into an unrelated case** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). Reusing a watch legal-notice template for a *car* case produced questions like "What is the serial number of the **watch**?" — the profiler had baked the template's original product noun into each variable's `question`/`label`, and the deterministic registry-coverage step asks that text verbatim regardless of the new matter. `TEMPLATE_PROFILE_INSTRUCTION` now requires **domain-neutral wording**: labels/questions must refer to the subject generically ("the product / goods / asset / item"), never the template's original product, brand, or party names. `PROFILE_SCHEMA_VERSION` bumped to 4 so already-cached templates are automatically re-profiled (on the next template sync / reconnect) under the neutral-wording rule rather than serving stale watch-specific questions.
- **Drafting kickoff no longer asks the same question twice** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). The kickoff built its question list from two sources that were only de-duplicated by exact id — the kickoff LLM's own `openQuestions` (umbrella phrasings like "Notice Reference & Date", "Compensation Heads") and `_ensure_registry_coverage`'s per-variable additions (the granular "Notice Reference Number" + "Notice Date", individual compensation heads, etc.) — so semantically-equivalent questions were asked under both Critical and Important. A hybrid de-dup (`_dedup_kickoff_questions`) now runs on the final set: a deterministic identical-token pass (`_dedup_questions_by_tokens`) collapses pure rephrasings (e.g. "Client Professional Background" = "Professional Background"), then an LLM pass (`_consolidate_questions_llm`, new `QUESTION_DEDUP_INSTRUCTION` / `build_question_dedup_prompt`) drops synonym/umbrella-vs-parts duplicates that share no words — keeping the granular questions and dropping the umbrella so every distinct value is still captured. The LLM pass only ever drops existing ids, ignores a result that would remove most questions, and falls back to the input on failure. The profiler prompt (`TEMPLATE_PROFILE_INSTRUCTION` rule 2) is also strengthened to forbid emitting overlapping variables (umbrella + parts, combined + split, synonyms) at the source.
- **Scanned-document OCR noise (rubber-stamp/seal text, page-number footers, editor margin notes) no longer leaks into generated drafts** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). A scanned PDF template is OCR'd, so a circular firm seal is read as scrambled fragments that **differ on every page** (`SSOCIATES`, `ASSOCI`, `FUSKH`, `GURUGRAM`, `INDIA`…) and, together with `Page N of M` footers and editor notes, lands between body paragraphs — which the verbatim de-dup (`_normalize_template_text`) cannot remove. Template text is now passed through a one-time LLM cleanup at ingest (`_clean_template_ocr_text`, on a fresh extraction only) that strips letterheads / seal garble / page footers / margin notes while preserving every numbered paragraph, heading, recipient/signature block and literal placeholder verbatim (new `TEMPLATE_CLEANUP_INSTRUCTION` / `build_template_cleanup_prompt`, via `gemini_client.generate_longform`). Long templates are not skipped: they are split on line/paragraph boundaries (`_chunk_text_for_cleanup`) and cleaned chunk-by-chunk in bounded parallel, with mid-document chunks flagged as continuation fragments. An accept/reject gate (`_accept_cleaned_template_text`) runs per chunk and on the reassembled document — rejecting any result that is drastically shorter/longer or drops a literal placeholder and falling back to the original chunk / deterministically-normalised text — so ingest never loses content. `DRAFT_RENDER_INSTRUCTION` also names garbled stamps/seals and page footers as artifacts to ignore.
- (Document changes here before release)

---

## [4.1.0] - 2026-06-12

### Added

- **Chat-driven panel editing from the Strategy & Objective chat**, with two clearly separated modes (`app/socket/events.py`):
  - **Mode 1 — name / word / line edit (surgical).** A NAME change is applied as an exhaustive, workspace-wide rename across the EXISTING content of every panel — Case Summary, Report Summary, Strategic Objective, Possible Next Steps, Analysis Report, List of Dates, Applicable Laws, and Upcoming Dates — with **no regeneration** (no new/removed dates, laws, or items). The intent classifier returns an `isRename` signal and the dispatch fans one rename instruction to all panel editors deterministically (so coverage no longer depends on the LLM flagging each panel). Name changes are applied **semantically** (a party referenced by role/description, e.g. "the female employee", is named correctly even where she appears as "the Petitioner").
  - **Mode 2 — case-perspective / objective change (regeneration).** Declaring a new objective regenerates the Strategic Objective + Possible Next Steps, regenerates the Analysis Report, re-scores List-of-Dates relevance (same entries), regenerates Applicable Laws and Drafting suggestions for the new objective, and preserves the Case Summary + Upcoming Dates.
- New surgical panel editors: `_regenerate_analysis_report_with_feedback`, `_regenerate_list_of_dates_with_feedback`, `_regenerate_applicable_laws_with_feedback`, `_regenerate_procedural_timeline_with_feedback`, plus `_reeval_list_of_dates_relevance` (Mode 2 relevance re-score).
- **Output guardrails** shared across every generated panel and chat (`app/prompts/legal_intelligence_system.py`, injected into the structured + chat system instructions and the inline case-summary / list-of-dates / procedural-timeline prompts):
  - `ENGLISH_ONLY_OUTPUT_DIRECTIVE` — all output in English/Latin script; Devanagari/mixed-script names are transliterated.
  - `APP_BRAND_EXCLUSION_DIRECTIVE` + `scrub_app_brand()` — the platform's own name ("SimplerToday") never appears as a case party/fact (prompt guardrail + deterministic scrub before cache/emit).
  - `NAME_CAPITALISATION_DIRECTIVE` — person names / places / addresses in Title Case, never ALL-CAPS (statutory acronyms preserved).
- `GeminiClient.generate()` now accepts an explicit **`thinking_level`** (default `LOW`) and runs primarily on the `google-genai` SDK with `ThinkingConfig`, falling back to the legacy SDK.

### Changed

- **Objective change fully re-derives the downstream panels.** `_run_objective_change_incremental_refresh` now **regenerates and REPLACES** the Analysis Report for the new objective (previously it merged with the prior report, which kept the old-perspective strengths/risks and preserved the old quality score) and regenerates Applicable Laws with the **objective as the primary law-selection lens** (`merge_with_previous=False`). So changing the objective (e.g. jewellery-theft → divorce) produces an analysis report, quality score, applicable laws, and drafting suggestions framed entirely around the new objective. The incremental-merge helper is unchanged for the document re-upload / `workspace_sync` paths.
- **Procedural Timeline scope narrowed** (`app/langgraph/procedural_timeline_agent.py`): the panel now carries ONLY genuine court/law-set procedural dates — filing deadlines, hearing dates, evidence deadlines, compliance dates, and adjourned/next dates (types `Court Hearing` / `Deadline`). Pre-litigation notices, police complaints / FIR / GD / missing-person reports, lawyer consultations / engagements, meetings, and reconciliation attempts are excluded, classification is LLM-driven (no hardcoded keyword rules), and the agent never invents dates — it returns an empty timeline when none exist.
- **Gemini latency / completeness** governed by `thinking_level` rather than the output ceiling: gemini-3 reasoning tokens are billed against `max_output_tokens`, so the budget is kept moderate and the thinking pass is bounded (`LOW`) to keep responses fast while still completing the full output.
- Drafting template sync no longer applies a hard 30-second cutoff (`ensure_templates_synced`) — suggestion matching waits for the lawyer's templates to finish ingesting instead of matching against an empty library; the awaiting future always resolves so it cannot hang.
- A multi-field chat edit now emits a single consolidated `strategy_objective` payload instead of one partial emit per field (no "previous version" flicker).

### Fixed

- **Objective change left the Analysis Report and Applicable Laws on the OLD objective.** Root cause: the analysis report was merged with the previous report (keeping stale strengths/risks and the old quality score). Now replaced + re-derived for the new objective (see Changed).
- **Intermittent 0% Quality Score** on a populated Analysis Report. `caseResolutionScore` parsing is now robust (accepts `"72"`, `"72%"`, floats) and a content-derived score is used when the model omits/zeroes it, so an analysed case never reads 0%.
- **`finish_reason=MAX_TOKENS` truncation** that cut large structured outputs (e.g. the strategy JSON dropped `strategicObjective` / `possibleNextSteps`) and a follow-on latency regression — both traced to the gemini-3 thinking budget; fixed via `thinking_level` control on `generate()`.
- **Mid-sentence truncation** of panel descriptions. A naive sentence splitter treated abbreviation periods ("Model No.", "SRF No.", "Rs.") as sentence ends and chopped descriptions; List of Dates, Procedural Timeline, and Drafting-suggestion descriptions now trim only at a word boundary.
- Normal questions in the Strategy & Objective chat (e.g. "explain the applicable case laws") no longer trigger an objective regeneration — a question vs objective-update guard (`_is_pure_informational_query` + a strengthened LLM classifier) routes genuine objective changes to the regeneration flow and leaves questions/renames alone.
- A genuine objective change now correctly shows the loader for and re-emits List of Dates / Applicable Laws / Upcoming Dates / Drafting (previously only the Analysis Report refreshed).

---

## [4.0.0] - 2026-06-08

### Added

- Added the **Drafting Assistant** feature (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`).
  - New inbound event **`template_sync`** (uploaded / renamed / deleted) for the lawyer-scoped Template Library — each template is downloaded, its text extracted via `app/processing/file_handlers.py`, and persisted in a new lawyer-scoped Qdrant cache (`upsert_user_state_cache` / `get_user_state_cache` / `delete_user_state_cache` / `list_user_state_cache`).
  - New outbound `document_feature_response` with **`featureType: "drafting_assistant"`** — 4–6 proactive draft suggestions (`{title, relevance, description}`) generated at `workspace_created` / `workspace_sync`, cached in `_workspace_drafting_payload`, re-emitted on reconnect.
  - Multi-turn drafting **chat flow** on the existing `user_message_request` ↔ `ai_response` pair (`chat_type="DRAFTING_ASSISTANT"`). The drafting agent keeps per-`actionableId` session state in a new `drafting_session` workspace state cache, asks the lawyer for missing slots, and emits an augmented `ai_response` with `draftStatus`, `openQuestions`, and (when ready) `draftDocument` (Markdown body). When the lawyer picks a template, the template's structural skeleton is preserved verbatim; only case-specific facts are substituted.
  - Lawyer-scoped state cache support in `app/vectorstore/qdrant.py` (mirrors the existing workspace-state cache pattern but keyed by `user_id`, with no vectors).
- Added a dedicated **long-form generation path** `GeminiClient.generate_longform()` (`app/ai/gemini_client.py`) for draft render and draft translation, built on the current **`google-genai`** SDK with native async (`client.aio.models.generate_content`). It exposes explicit **`thinking_level`** control (Gemini 3 thinking models) and falls back to the legacy `generate()` path automatically if the new SDK is unavailable or returns no text — so drafting never hard-fails.
- Added Gemini generation **diagnostics** (`GeminiClient._log_generation_diagnostics`): every Gemini call now logs `finish_reason` and token usage (`prompt` / `output` / `thinking` / `total`), at WARNING when a response is truncated or blocked. Makes draft cut-offs immediately diagnosable in production logs.

### Changed

- Draft render (`_render_and_attach`) and draft translation (`_handle_translate_draft`) in `app/langgraph/drafting_agent.py` now call `generate_longform(...)` with `max_tokens=65536` (the model's full 64k output ceiling) and `thinking_level="HIGH"`. Drafting is the product's core feature, so it uses the deepest reasoning pass for fuller legal structure and more complete drafts. Previously these used the legacy one-shot call capped at 32000 tokens.

### Fixed

- **Incomplete / truncated drafts.** Long drafts (rejoinders, multi-affidavit packs, 10–20 page legal notices) were rendering cut off mid-sentence. Root cause: draft generation ran on the deprecated `google-generativeai` SDK with no control over the Gemini 3 thinking budget (reasoning tokens are billed against `max_output_tokens`) and a 32000-token cap. Fixed by migrating the draft path to the `google-genai` SDK with explicit thinking control and raising the output ceiling to 64k.

---

## [3.0.0] - 2026-04-27

### Added

- Added a new **Procedural Timeline Agent** (`app/langgraph/procedural_timeline_agent.py`) that extracts, consolidates, validates, and emits a legal-journey timeline across uploaded case documents.
- Added a new socket feature payload type: **`procedural_timeline`** (via `document_feature_response`) with reconnect-safe cache and merge behavior.
- Added `app/prompts/applicable_acts.py` to strengthen Indian-law grounding used in timeline and law-relevance reasoning.

### Changed

- Updated strategy, analysis, chat, and applicable-laws behavior to apply a **dual legal lens**: evaluate both **criminal-law** and **civil-law** tracks when facts support both.
- Improved role-conditioned legal prompting so outputs remain role-consistent while still covering both legal branches where relevant.
- Expanded scheduled feature generation flow to include `list_of_dates` → `applicable_laws` → `procedural_timeline` without blocking chat handling.

### Fixed

- Fixed procedural timeline event typing/normalization and downstream payload consistency in socket emission flow.
- Fixed prompt-level behavior drift so criminal/civil coverage is consistently applied across generated legal outputs.

---

## [2.0.1] - 2026-04-23 — Hotfix

### Fixed

- Fixed quality score explanation (`qualityScoreExplanation`) being returned as raw JSON object or wrapped in markdown code fences instead of plain readable text; response is now unwrapped and sanitized before use.
- Fixed quality score explanation appending internal rejection tracking notes (score delta, rejected item snippet) that were visible to end clients; on item rejection the explanation is now fully regenerated from current workspace state with no internal metadata included.
- Fixed list-of-dates agent emitting entries with placeholder/invalid dates (e.g. `1111-11-11`) and UI-scraped garbage text (e.g. "View PDF", "Feedback", "Copyright Policy", "Skip to main content") as date descriptions or evidence snippets.
- Fixed list-of-dates extraction including irrelevant dates such as statute enactment years, legal citation years, and background biographical dates unrelated to the active case.
- Fixed list-of-dates listing-history table rows (columns like "Date of Listing / Date of Upload / Order") producing excessive low-quality entries; extraction now limits such tables to at most three significant dates (case filing, registration, and most recent substantive hearing).
- Improved list-of-dates evidence snippet quality — raw document fragments and bare PDF link lists are rejected as evidence.

---

## [2.0.0] - 2026-04-16

### Added

- AI-driven **versioned analysis** generation for DOCX/report flows with content-hash caching and request-safe finalize behavior.
- **Document generation** for AI outputs, enabling structured legal draft documents from analysis and strategy results.
- **Strategy summary** and report cover field support (`reportCaseSummary`, contact/date metadata) with nested and snake_case payload mapping.
- **Applicable laws** enrichment with larger catalog support and paginated search.
- **List of dates / parties** persistence for pending suggestions and approval lifecycle across refreshes.
- Persistence for approved **missing info**, strengths, weaknesses, risks, and jurisdiction across AI refresh cycles.

### Changed

- Workspace refresh and merge behavior refined for first ingest and incremental sync stability.
- Socket/event observability improved with cleaner logs and better signal-to-noise for AI operations.
- Runtime dependencies updated for reliability and vulnerability remediation.

### Fixed

- Applicable-laws update/search reliability issues fixed, including duplicate event emission edge cases.
- Workspace reset now clears replay/cache state to prevent stale AI strategy data.
- Rejected pending list-of-dates entries no longer reappear after user rejection.
- User-defined workspace title is preserved and not overwritten by AI metadata refresh.
- Strategy and analysis event handling stabilized during workspace refresh.

### Security

- Search/update paths hardened for applicable-laws operations.
- Dependency vulnerability fixes included in this release cycle.

---

## [1.0.0] - 2026-03-06

### Added

- AI Socket.IO integration for workspace sync and real-time intelligence chat responses.
- Document intelligence baseline: file ingestion, extraction pipeline, and workspace-scoped vector retrieval.
- Initial strategy and analysis generation flow for legal workspaces.
- Initial **list-of-dates** extraction and timeline generation support (early version).
- Analysis report flow integrated.

### Fixed

- Stability fixes for early workspace processing and chat event handling.
- Reliability improvements for AI feature-response emission under load.

---

[Unreleased]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v5.0.0...HEAD
[5.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v4.2.0...v5.0.0
[4.2.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v4.1.0...v4.2.0
[4.1.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v4.0.0...v4.1.0
[4.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.1...v3.0.0
[2.0.1]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v2.0.0
[1.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v1.0.0
