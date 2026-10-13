# Architecture

Start here. This page is the map; the linked documents hold the detail.
CLAUDE.md holds the rules every change must respect.

## What the service is

SimplerToday AI is a Python asyncio **Socket.IO client**. It connects out to
the application backend, receives workspace and chat events, and emits AI
results back. There is no HTTP API. `python -m app.main` boots
`app/core/lifecycle.py`, which starts the Qdrant store, the document
processor, the model client and the socket client, then waits for shutdown.

```
Backend (Socket.IO server)
   │  workspace_created / workspace_sync / user_message_request / …
   ▼
app/socket/client.py ──► SocketEventHandler (app/socket/events.py)
                            = per-feature mixins in app/socket/handlers/
   │
   ├─ role gate (app/langgraph/role_agent.py) — nothing runs without a role
   ├─ ingestion: app/processing/ → PII redaction → classify → app/rag/ chunk
   │             → Qdrant (app/vectorstore/) [+ optional graph, app/ontology/]
   ├─ panels: strategy, analysis report, list of dates, applicable laws,
   │          procedural timeline, … (handlers + app/langgraph/ agents)
   └─ chat: intelligence chat graph (app/langgraph/intelligence_chat_agent.py)
            with checked citations (app/rag/citations.py)
   │
   ▼  document_feature_response (featureType=…) / ai_response
Backend
```

## Code map

| Package | Responsibility |
|---|---|
| `app/core/` | Settings (`config.py`), startup/shutdown, knowledge-plane wiring |
| `app/socket/` | Socket client, auth, schemas, event handlers (one mixin per feature) |
| `app/langgraph/` | Role agent, chat graph, panel agents, drafting, output filters |
| `app/agents/` | Role models and the role gate response, base agent classes |
| `app/processing/` | Text extraction (PDF, OCR, Office, audio), PII, classification, readability |
| `app/rag/` | Chunking, hybrid retrieval, packing, citations, attribution, eval |
| `app/vectorstore/` | Qdrant collections (evidence, tables, state caches) |
| `app/ontology/`, `app/graph/`, `app/retrieval/` | Optional knowledge graph (Neo4j): extraction, store, planner |
| `app/llm/`, `app/ai/` | Provider-neutral model router behind `gemini_client`; usage and pricing |
| `app/statutes/`, `app/search/` | Statutory register, case-law and Vertex AI search |
| `app/workspace/` | Per-workspace state: persistence, participants, model choice |
| `app/storage/` | GCS helpers |
| `app/observability/` | Logging, audit, tracing |
| `app/prompts/` | Prompt text |

## Detailed documents

| Topic | Document |
|---|---|
| Full event flow and multi-agent design | `docs/SYSTEM_ARCHITECTURE.md` |
| Handler module map and conventions | `docs/SOCKET_EVENTS_ARCHITECTURE.md` |
| Reconnects and socket stability | `docs/socket-reconnection-and-stability.md` |
| Handshake authentication | `docs/AI_SERVICE_AUTH_MIGRATION.md` |
| Version restore contract | `docs/BACKEND_VERSION_RESTORE_SPEC.md` |
| Model providers and routing | `docs/MODEL_PORTABILITY.md` |
| Knowledge graph / ontology RAG | `docs/AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md` |
| Vendored Keel copy | `docs/KEEL_VENDORED.md` |
| Architecture decisions and why | `docs/adr/` |
| How work flows from Jira to release | `docs/development-workflow.md` |
| Specs for individual changes | `specs/` |
| Feature history | `CHANGELOG.md` |

## Known structural debt

- Several modules are very large (`app/langgraph/drafting_agent.py`,
  `app/socket/handlers/analysis_report.py`, `chat.py`, `workspace_events.py`).
  Split them one per PR, each verified with `make e2e`.
- Socket handlers, panel agents and document processing have no full
  automated coverage; `make e2e` is the only end-to-end check.
- Two strategy implementations exist; only the socket path
  (`_generate_strategies_from_documents`) runs in production.
