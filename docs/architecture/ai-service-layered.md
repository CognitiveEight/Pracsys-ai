---
title: AI service – layered architecture
type: architecture
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
view: components
diagram_source: docs/system-architecture-layered.drawio
related_views:
  - docs/system-architecture.drawio (12 pages: C4 context, containers, components, events, ingestion, panels, chat, model routing, data and erasure, deployment, security, delivery)
  - docs/SYSTEM_ARCHITECTURE.drawio (3 pages: system, multi-agent layer, data pipelines)
  - docs/TARGET_ARCHITECTURE.drawio (future state: AWS and Azure paths; not the current system)
related_decisions:
  - ../decisions/DR-AI-0001-one-model-router.md
  - ../decisions/DR-AI-0002-gate0-as-the-model-cost-and-log-layer.md
  - ../decisions/DR-AI-0003-nothing-is-cited-unless-checked.md
  - ../decisions/DR-AI-0004-stop-is-a-fence.md
  - ../decisions/DR-AI-0005-workspace-deletion-erases-every-copy.md
---

# AI service – layered architecture

## In plain words

The AI service is the part of SimplerToday that reads a case and writes the
analysis. It has no website or public address. It dials out to the
main backend and waits for messages such as "a case was created", "files
changed" or "the lawyer asked a question". For each one it reads the evidence,
turns scans and recordings into text, removes personal details, and stores the
text so it can be searched. It then asks an AI model to fill the case panels
or answer the question, and sends the result back. Nothing runs until the
lawyer's side in the case is known. Every quoted fact is checked against the
documents before it is shown. All model calls go through one switchboard, so
the model provider can change without changing the features. Every answer
carries its cost, and deleting a case erases every copy the service holds.

## What you're looking at

One page of the service as it runs today, top to bottom by layer. The diagram
is `docs/system-architecture-layered.drawio` (single page, "Layered system
architecture"). Detailed views, including sequences and deployment, are in
`docs/system-architecture.drawio`.

| Layer | What sits there |
|---|---|
| Presentation | The lawyer app (outside this repo): chat, seven analysis panels, supporting material, audio evidence, drafting. Users reach the AI service only through the backend. |
| Socket edge and application | The Socket.IO client, the signed handshake, and the event router (one handler class built from per-feature mixins). |
| Domain | The role gate, the panel generators and LangGraph agents, the governed chat graph, the ingestion pipeline, retrieval and citations, and the output pipeline every panel passes through. |
| Infrastructure | The model router and its provider adapters (gate0 among them), Google Cloud AI services (Vision OCR, Speech-to-Text, Vertex AI Search, embeddings), Indian Kanoon. |
| Persistence | Qdrant (evidence, spreadsheet rows, state caches), Google Cloud Storage (evidence files, audio batches), optional Neo4j, local disk for logs and statute data. |
| Cross-cutting | Usage and billing ledger, logging and audit, run control (stop and resume), workspace purge. |

Not in the system today, though they appear in older or future-state
diagrams: an HTTP API, a message queue, MongoDB, a prompt hub, and Redis.
Redis is in `docker-compose.yml` and in settings, but no code in `app/` reads
or writes it.

## The main parts

| Part | What it does | Built with | Owned by |
|---|---|---|---|
| Socket client | Connects out to the backend, signs every handshake, receives events, emits results. Each emit reports whether it was delivered and never raises. | python-socketio (asyncio), HMAC v1 handshake | AI team |
| Event router and handlers | One handler object built from per-feature mixins. Routes each inbound event, holds all per-workspace caches, replays cached results on reconnect. | Python asyncio, mixins in `app/socket/handlers/` | AI team |
| Role gate | Root agent. No panel or chat runs without a validated role (adjudicator, petitioner, respondent, mediator). A role change invalidates derived output. | LangGraph | AI team |
| Panel generators | Document assessments, workspace metadata, strategy and objective, analysis report, list of dates, applicable laws, procedural timeline, case laws, relevant parties, drafting assistant. | Handler mixins, LangGraph agents for dates, timeline, drafting and template matching | AI team |
| Intelligence chat | Fixed graph: build context, classify intent, explain, record the action, format, validate. Records which agent should act; never runs it in-line. | LangGraph | AI team |
| Ingestion | Fetches each evidence file, extracts text (text layer, OCR, office files, spreadsheets, audio), redacts personal data, classifies, scores readability, chunks and stores. | `app/processing/`, Google Vision, Speech-to-Text v2 | AI team |
| Retrieval and citations | Structure-aware chunks with page numbers; hybrid dense plus sparse search, rerank and retry; budgeted packing of panel prompts; citation check for chat; deterministic attribution for panels. | `app/rag/`, Qdrant | AI team |
| Statute register | Marks each applicable-law row verified, invalid or unverified, and adds the official section heading. | `app/statutes/`, India Code section index | AI team |
| Model router | Sends every model call to the configured profile, with timeout, retry, fallback, JSON repair, one log line and cost per call. | `app/llm/` behind the `gemini_client` facade | AI team |
| gate0 gateway | Governed AI gateway that can serve all model calls, hold provider keys, budgets and spend records. Optional; enforcement is off by default. | LiteLLM proxy (external) | gate0 platform team |
| Model providers | Gemini by default; OpenAI, Azure OpenAI, Azure AI Foundry, Bedrock, SageMaker, OpenAI-compatible servers available by configuration. | Provider SDKs, only inside `app/llm/providers/` | Vendors |
| Vector store | Evidence chunks with dense and sparse vectors, spreadsheet rows, and the workspace state caches. | Qdrant | AI team (data); infra (hosting) |
| Knowledge graph | Optional legal ontology graph. Adds evidence to chat and some panels; never gates them. | Neo4j 5, `app/ontology/`, `app/graph/`, `app/retrieval/` | AI team |
| Usage and billing | Per-turn usage scope plus a per-workspace ledger; prices tokens and paid services; every emit carries cost and running total. | `app/ai/usage.py`, `app/ai/pricing.py`, optional gate0 meter | AI team |
| Observability | Correlated logs per event, one line per model call, audit sink, structured JSON sink, PII-safe helpers. | Loguru, `app/observability/` | AI team |
| Persistence of state | Per-workspace caches written through to Qdrant, rehydrated on a cold workspace, blocked during a purge. | `app/workspace/persistence.py` | AI team |
| Run control | Stop fences a running update by epoch; resume re-emits exactly the declared panels. | `app/socket/handlers/run_control.py` | AI team |
| Workspace purge | Erases every copy of a deleted case at the backend's deadline; retries and escalates on failure. | `app/socket/handlers/workspace_purge.py` | AI team |

## How the parts talk

| From -> to | What travels | Protocol | Protected by |
|---|---|---|---|
| AI service -> backend (connect) | Handshake with signature, timestamp and nonce headers | Socket.IO over WebSocket, outbound only | HMAC v1 over a shared secret; headers single-use and rebuilt on every reconnect |
| Backend -> AI service | Workspace events (created, sync, version restore, reload, deleted, stop, resume, transferred), chat messages, panel feedback and edits, heartbeats | Socket.IO events | The authenticated socket; ids only identify the owner; sharing roster decides who may change a case |
| AI service -> backend | `document_feature_response` per panel, `ai_response` for chat, `workspace:refresh` to arm loaders, `case_law_document_response` | Socket.IO events | Stop fence suppresses stale panel and refresh emits; undelivered panels are buffered and re-sent on reconnect |
| Handlers -> evidence storage | Download of evidence files | HTTPS (Google Cloud Storage or signed URLs) | Service account; URLs never logged |
| Ingestion -> Google Vision / Speech-to-Text | Page images, audio | Google Cloud APIs | Service account credentials |
| Ingestion -> Qdrant | Redacted chunks, vectors, spreadsheet rows | Qdrant client | API key when configured; only redacted text is stored |
| Handlers and agents -> model router | Prompts and evidence packs | In-process call through the facade | Router is the only path; a content block is final |
| Model router -> provider or gate0 | Model requests; usage and cost back | HTTPS via provider adapters | Provider keys as secret settings; under enforcement only gate0 is allowed; requests carry ids, never names |
| Handlers -> Indian Kanoon, Vertex AI Search | Law and case-law searches, judgment fetches | HTTPS | Search queries are metered and billed |
| Ingestion and retrieval -> Neo4j (optional) | Extracted facts with chunk ids and pages; graph queries | Neo4j driver (Bolt) | Credentials in settings; off unless configured |
| Every handler -> state persistence -> Qdrant | Per-workspace caches (role, panels, participants, model choice, usage) | Fire-and-forget upserts | Writes blocked for a workspace being purged |
| Usage ledger -> gate0 meter (optional) | OCR, speech, search and grounding units, ids only | HTTPS, batched, on-disk outbox | Off by default; gate0 endpoint not deployed yet |
| Everything -> logs | Event, model-call and result lines; audit records | Local files | No payloads, prompts, document text, names or URLs in logs |

## Key decisions behind this shape

- **Outbound socket client, no HTTP API.** The backend owns users, files and
  access; the AI service only answers events. Handshake auth is the backend's
  DR-0009 (https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161659).
- **The role gate is the root.** No downstream agent runs without a validated
  role.
- **One model router** ([DR-AI-0001](../decisions/DR-AI-0001-one-model-router.md)).
- **gate0 as the model, cost and log layer**
  ([DR-AI-0002](../decisions/DR-AI-0002-gate0-as-the-model-cost-and-log-layer.md)).
- **Nothing is cited unless checked**
  ([DR-AI-0003](../decisions/DR-AI-0003-nothing-is-cited-unless-checked.md)).
  The backend's DR-0013 (check every AI reply,
  https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4227166)
  is the other side of this.
- **Stop is a fence** ([DR-AI-0004](../decisions/DR-AI-0004-stop-is-a-fence.md)).
- **Deleting a workspace erases every copy**
  ([DR-AI-0005](../decisions/DR-AI-0005-workspace-deletion-erases-every-copy.md)).
- **In-process background work.** Long panels run as tracked asyncio tasks so
  chat is never blocked. There is no queue; state survives restarts through the
  Qdrant-backed caches and replay on reconnect.
- **The knowledge graph is additive.** It augments evidence and is never
  required; an unreachable graph is logged and the service runs on hybrid
  search alone.

## Known gaps and risks

- **Single process, in-memory state.** All per-workspace caches live in one
  handler object, written through to Qdrant. Running two instances against the
  same backend is not designed for.
- **Thin automated coverage of handlers, panel agents and ingestion.** The
  offline suites cover specific paths (reload, run control, objective change,
  sharing, RAG, logging); `make e2e` is the only end-to-end check and it makes
  billed model calls.
- **Workspace purge has no dedicated offline test.** It is exercised by
  `make e2e` and by the vector-store deletion checks in
  `tests/offline/verify_rag.py` and `tests/test_tables.py`.
- **gate0 enforcement is off.** With `GATE0_ENFORCE=true` a gate0 outage stops
  all AI features by design, so it waits for gate0 high availability. Whether
  production currently routes through gate0 depends on deployed `LLM_*`
  settings, which this doc cannot see.
- **Two strategy paths.** Only the socket path in `app/socket/handlers/strategy.py`
  runs in production; `app/langgraph/strategy_agent.py` is programmatic only.
- **Large modules** (`drafting_agent.py`, `analysis_report.py`, `chat.py`,
  `workspace_events.py`) are slated to be split one per PR.
- **Embedding change means a new collection.** A purge does not reach an old
  collection left behind after a migration.
- **Hosting** (VMs, orchestration, monitoring) is not described in code; the
  deployment page of `docs/system-architecture.drawio` is the only reference.

## For engineers

**Entry point.** `python -m app.main` -> `app/core/lifecycle.py`: validates
config, starts `QdrantStore`, the document processor, the model client (with
the gate0 served-model check under enforcement), the gate0 meter, the optional
knowledge plane (`app/core/knowledge_plane.py`, non-strict), then
`SocketClient` and `event_handler.set_socket_client(...)`.

**Socket layer.**
- `app/socket/client.py` (connect, `emit` returning `bool`, header builder
  passed as a callable), `app/socket/auth.py` (v1 scheme),
  `app/socket/schemas.py` (payloads).
- `app/socket/events.py` is the composition root: `SocketEventHandler` = the
  mixins in `app/socket/handlers/`, all state in `__init__`, the
  `event_handler` singleton. Module map: `docs/SOCKET_EVENTS_ARCHITECTURE.md`.
- The emit chokepoint for panels is `_emit_document_feature` in
  `app/socket/handlers/chat.py`: statute annotation, panel attribution, usage
  metadata, pending-delivery buffer.
- Run scope and fence: `app/socket/handlers/run_control.py`
  (`_StopFencedSocket`, `CANCELLABLE_RUN_EVENTS`); detached work via
  `_track_background_task` in `app/socket/handlers/_shared.py`.
- Contract: [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

**Agents.** `app/langgraph/role_agent.py` (role_gate -> role_selection ->
role_context_builder -> role_validation), `intelligence_chat_agent.py` and
`chat_nodes.py`, `list_of_dates_agent.py`, `procedural_timeline_agent.py`,
`drafting_agent.py`, `template_match_graph.py`.

**Processing.** `app/processing/document_processor.py`, `file_handlers.py`,
`ocr.py`, `tables.py`, `audio_transcription.py`, `pii_detector.py`,
`document_classifier.py`, `readability.py`.

**RAG.** `app/rag/chunker.py`, `retriever.py`, `sparse.py`, `chat_context.py`,
`packing.py` (`fit_to_budget`, `PANEL_CONTEXT_CHARS` default 200,000),
`citations.py` (`verify_and_number`), `attribution.py`
(`attach_panel_citations`), `table_query.py`, `eval/`.

**Storage.** `app/vectorstore/qdrant.py`: collection `<name>` (state caches),
`<name>_evidence_v1` (chunks, dense plus sparse; suffixed per embedding space),
`<name>_tables_v1` (rows, no vectors). Deletion only via `delete_documents` /
`delete_workspace`, which also withdraw graph facts. State caches:
`app/workspace/persistence.py` (`CacheKinds`, `PersistentWorkspaceDict`,
`block_workspace_persistence`).

**Models.** `app/ai/gemini_client.py` (facade) -> `app/llm/router.py`
(`ModelRouter`) -> `app/llm/providers/` (`gemini`, `openai_compat`,
`azure_openai`, `aws`, `gate0`). Config names: `LLM_DEFAULT`, `LLM_ROUTES`,
`LLM_FALLBACKS`, `LLM_SELECTABLE_MODELS`, `LLM_MODEL_OPTIONS`,
`EMBEDDING_PROVIDER`, `GATE0_ENFORCE`, `GEMINI_REQUEST_TIMEOUT_SECONDS`
(default 300). Detail: `docs/MODEL_PORTABILITY.md` and
[Model calls through gate0](../contracts/model-calls-through-gate0.md).

**Graph (optional).** On only when `NEO4J_URL` and `NEO4J_PASSWORD` are set;
ingest only when `ONTOLOGY_INGEST_ENABLED=true`; panels only when listed in
`RETRIEVAL_PANELS`. Detail: `docs/AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md`.

**Usage and billing.** `app/ai/usage.py` (`capture_usage`, `@meter_usage`,
`record_*`, `drain_for_emit`), `app/ai/pricing.py`, `app/ai/gate0_meter.py`,
`scripts/reconcile_gate0_spend.py`.

**Observability.** `app/observability/logger.py` (sinks `logs/app.log`,
`logs/structured.log`, `logs/audit/audit.log`), `audit.py`, `trace.py`,
`app/utils/redact.py` (`chars`, `safe_url`). Policy:
[Logging and personal data](../policies/logging-and-personal-data.md).

**Tests that guard this shape.** `tests/test_llm_router.py` (no provider SDK
outside adapters, blocked is final), `tests/test_llm_gate0.py`,
`tests/test_usage_ledger.py`, `tests/test_ocr.py`, `tests/test_tables.py`,
`tests/offline/verify_rag.py`, `tests/offline/verify_run_control.py`,
`tests/offline/verify_logging.py`, `tests/offline/verify_workspace_share.py`,
`tests/offline/verify_document_reload.py`,
`tests/offline/verify_objective_change.py`; end to end `make e2e`
(`scripts/e2e_local_backend.py`).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
