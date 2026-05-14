# SimplerToday System Architecture — Multi-Agent Flow

This document describes the **current system architecture** and **multi-agent flow** of the SimplerToday AI service: how socket events, LangGraph agents, document processing, and AI (Gemini) work together.

---

## 1. High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL (Backend / App)                                │
│  Socket.IO: workspace_created | workspace_sync | user_message_request | ...      │
└───────────────────────────────────────────┬─────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         AI SERVICE (SimplerToday)                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────────┐  │
│  │ Socket Handler   │  │ Workspace       │  │ Document Processing            │  │
│  │ (events.py)      │──│ Manager /       │  │ (batch extract → vector store) │  │
│  │                  │  │ Context         │  │                                 │  │
│  └────────┬─────────┘  └────────┬────────┘  └──────────────┬──────────────────┘  │
│           │                     │                         │                      │
│           ▼                     ▼                         ▼                      │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │                    MULTI-AGENT LAYER (LangGraph + Gemini)                  │ │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐ │ │
│  │  │ Role-Based Agent  │  │ Strategy Gen     │  │ Intelligence Chat Agent  │ │ │
│  │  │ (root / gate)     │──│ (Gemini + docs)  │  │ (context → intent →       │ │ │
│  │  │                   │  │                  │  │  explain → dispatch)      │ │ │
│  │  └──────────────────┘  └──────────────────┘  └──────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│           │                     │                         │                      │
│           ▼                     ▼                         ▼                      │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────────────┐  │
│  │ Vector Store    │  │ Gemini Client   │  │ Observability (audit, trace)    │  │
│  │ (Qdrant)        │  │ (strategy,      │  │                                 │  │
│  │                 │  │  chat, report)  │  │                                 │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Emits: document_feature_response (strategy_objective, document_assessments,     │
│         analysis_report, list_of_dates, procedural_timeline, applicable_laws) |  │
│         ai_response (chat)                                                        │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Entry Points: Socket Events

The AI service is driven by **Socket.IO** events from the backend. Main events:

| Event                        | Direction   | Purpose |
|-----------------------------|------------|---------|
| `workspace_created`         | Backend → AI | New workspace: init role, process evidence files, generate strategy & metadata |
| `workspace_sync`            | Backend → AI | Workspace updated: re-run doc processing / strategy / analysis when docs change |
| `user_message_request`      | Backend → AI | User chat message: build context, run Intelligence Chat Agent, return reply |
| `probing_prompts_request`   | Backend → AI | Suggestion chips for strategy or chat |
| `analysis_report_feedback`  | Backend → AI | User feedback on report; recalc and re-emit analysis |

**Outbound (AI → Backend):**

| Event                      | When |
|----------------------------|------|
| `document_feature_response` | After document assessments, strategy/objective, analysis report, list_of_dates, or applicable_laws |
| `ai_response`              | After processing a user chat message |

`document_feature_response` now also includes `procedural_timeline` as a first-class feature payload type.

---

## 3. Multi-Agent Flow

### 3.1 Role-Based Agent (Root / Governance)

**Location:** `app/langgraph/role_agent.py`  
**Role:** Root agent. **All role-dependent behavior is gated by this agent.** It enforces mandatory role selection and provides **role context** to downstream agents.

**LangGraph structure:**

```
START → role_gate → [blocked?] → role_selection → role_context_builder → role_validation → END
                 → [passed?]  → role_context_builder → role_validation → END
                 → [error]    → END
```

- **role_gate:** If role is not selected/validated → block and return role-selection prompt; else pass.
- **role_selection:** Handles explicit user input like `ROLE:PETITIONER`; validates against `RoleEnum`; writes role to workspace state.
- **role_context_builder:** Builds `role_context` (reasoning style, allowed bias, forbidden actions, etc.) for downstream agents.
- **role_validation:** Validates role state; marks workspace ready for downstream agents.

**When it’s used:**

- **Workspace creation:** `workspace_role_integration.initialize_workspace_with_role(workspace_id, user_id, initial_role)` calls `role_agent.invoke(..., user_input=f"ROLE:{initial_role}", ...)` so the workspace starts with a validated role.
- **Role change:** `workspace_role_integration.handle_role_change(...)` invokes the role agent with the new role and invalidates role-derived intelligence (triggers re-analysis).

**State:** `WorkspaceState` in `app/langgraph/state.py` (includes `role_state`: role, `role_context`, `role_selected_at`, `role_history`, `role_validated`).

---

### 3.2 Strategy & Objective Generation (Document-Based)

**Location:** Socket handler helper `_generate_strategies_from_documents()` in `app/socket/events.py` (and `ex_event.py`).  
**Not used from socket:** The **Strategy Agent** in `app/langgraph/strategy_agent.py` (LangGraph: suggest → map_objective → validate → lock) is available for programmatic use (e.g. `strategy_example.py`) but the **socket flow does not call it**. Instead, the socket flow uses **Gemini + vector store** to generate one-shot strategy/objective from document content.

**Flow:**

1. **Inputs:** `workspace_id`, `user_id`, `workspace_role`, optional `batch_result` (from document processing), `is_update`.
2. **Documents:** All documents for the workspace are loaded from the **vector store** (Qdrant) in upload order. Content is concatenated and truncated (e.g. ~12k chars), with a rule: if documents are unrelated, only the “most recently uploaded” is used for analysis.
3. **AI:** A single **Gemini** call with a structured prompt produces JSON: `caseSummary`, `strategicObjective`, `recommendations`, `possibleNextSteps`. Legal framing: BNS/BNSS/BSA/IT Act (Indian law).
4. **Output:** A payload suitable for `document_feature_response` with `featureType: "strategy_objective"` (and optional `suggestionChips`).

**When it’s used:**

- **workspace_created:** After batch document processing, `_generate_strategies_from_documents(...)` is called; result is emitted as `document_feature_response` and cached for re-emit on reconnect.
- **workspace_sync:** When documents change, strategy is regenerated and emitted again (with `is_update=True`).

So in the **current socket-driven flow**, “strategy” is **document-based AI generation**, not the LangGraph Strategy Agent pipeline (which remains available for future or programmatic use).

---

### 3.3 Intelligence Chat Agent (Governed Chat)

**Location:** `app/langgraph/intelligence_chat_agent.py`  
**Role:** Governed, role-aware chat: reads workspace intelligence, explains outputs, can trigger system actions via other agents, **does not mutate state directly**.

**LangGraph structure:**

```
context_builder → intent_classifier → explain_why → action_dispatcher → response_formatter → governance_validator → END
       ↓                   ↓                ↓                ↓                    ↓
   (error → END)     (unsupported → END)   Gemini          route to agent      one clean message
```

- **context_builder:** Builds prompt context from `ChatContext`: role_context, active_strategy, active_module, workspace_state, approved_facts, applicable_laws, uploaded_documents, chat_attachment. If role_context or workspace_state missing → error and stop.
- **intent_classifier:** Classifies user intent (e.g. GREETING, EXPLAIN, CLARIFY, VALIDATE, DRAFT, STRATEGY_CHANGE, NEXT_STEPS). If intent not supported → stop.
- **explain_why:** Calls **Gemini** with role-conditioned system instruction and document-aware prompt; produces answer, explanation, sources, confidence, suggested_actions. Handles “document upload gate” when no documents are uploaded.
- **action_dispatcher:** Maps intent to agent (e.g. fact_validation_agent, drafting_agent, strategy_agent, recommendation_agent) or “direct response”; does not invoke agents in this step—only records routing for the response.
- **response_formatter:** Single natural message (answer + optional explanation/sources/suggested actions).
- **governance_validator:** Ensures response is role- and strategy-compliant; sets flags on final response.

**When it’s used:**

- **user_message_request:** Handler in `events.py` builds `ChatContext` from workspace manager (role, document count, etc.), vector store (uploaded doc snippets), and optional chat attachment. Then calls `intelligence_chat_agent.process_message(workspace_id, user_id, message_id, user_message, chat_context)` and emits `ai_response` with the reply and suggestion chips.

**State:** `ChatState` in `app/langgraph/chat_state.py`; input is `ChatContext` (from `app/langgraph/chat_models.py`).

---

### 3.4 Applicable Laws Generation

**Location:** `app/socket/events.py` — `_generate_and_emit_applicable_laws`, `_generate_applicable_laws_from_sources`, `_extract_case_signals_from_documents`, `_search_indiankanoon`, `_search_google_cse_for_indiankanoon`, helpers `_normalize_applicable_laws_payload`, `_heuristic_applicable_laws_fallback`.

**Role:** Populate the **Applicable Laws** panel with a structured list of **potentially relevant Indian statutes and sections**, grounded in workspace documents and external search snippets. Outputs support **research and drafting direction**, not definitive legal advice.

**Flow:**

1. **Case signals** (`_extract_case_signals_from_documents`): Read up to the first five documents from the **vector store**; build `workspaceTitle`, a **search query** (title + frequent keywords from text), `keywords`, and a **document evidence snippet** (concatenated excerpts).
2. **Search (Indian Kanoon–oriented)**:
   - **Primary:** HTTP GET to `indiankanoon.org` search HTML; parse result links/titles (`_search_indiankanoon`).
   - **Fallback:** **Google Custom Search API** with `site:indiankanoon.org` (`_search_google_cse_for_indiankanoon`) when `GOOGLE_API_KEY` and `GOOGLE_CSE_ID` are set; otherwise skip.
3. **Synthesis** (`_generate_applicable_laws_from_sources`): Single **Gemini** call (`application/json`) with case signals + search result titles/snippets. System instruction uses `get_structured_document_feature_system_instruction` so output stays structured (no chat gate text). Model returns JSON: `laws[]` entries with `act`, `section`, `description`, `relevance` (`High` / `Medium` / `Low`). Prompt rules: Indian law focus, **no IPC** in output (prefer BNS where relevant), concise case-tied descriptions.
4. **Normalize** (`_normalize_applicable_laws_payload`): Validates fields, maps relevance, **drops IPC-labelled rows**, caps list at **12** items, assigns stable `id` fields (`law_1`, …).
5. **Heuristic fallback** (`_heuristic_applicable_laws_fallback`): If the model returns no laws, optional keyword-based stubs (e.g. IT Act, BNS, Companies Act, GST) from the evidence snippet.

**Outbound payload:**

- Socket event: `document_feature_response`
- `featureType`: `"applicable_laws"`
- `payload`: `{ "laws": [ { "id", "act", "section", "description", "relevance" }, ... ] }`

**Caching and scheduling:**

- In-memory cache: `_workspace_applicable_laws_payload[workspace_id]` for **reconnect / re-emit** (same pattern as analysis and list of dates).
- **Order in pipeline:** Usually runs **after** `list_of_dates` inside `_schedule_list_of_dates_and_applicable_laws`, or alone via `_schedule_applicable_laws_only` (e.g. zero-document fallbacks). Work is **`asyncio.create_task`**-scheduled from `_emit_analysis_report` and sync/create paths so **long Gemini + HTTP work does not block** concurrent **`user_message_request`** handling.

**Intelligence Chat:**

- When the workspace stores `applicable_laws` in metadata, **`ChatContext`** can include that list; **`chat_context_builder_node`** surfaces titles into the prompt so answers can align with the panel (`app/langgraph/chat_nodes.py`, `app/langgraph/chat_integration.py`).

---

### 3.5 Procedural Timeline Generation

**Location:** `app/langgraph/procedural_timeline_agent.py` + `app/socket/events.py` (`_generate_procedural_timeline_from_documents`, `_generate_and_emit_procedural_timeline`).

**Role:** Generate a lawyer-usable **procedural journey timeline** from workspace documents, then emit and cache it as `featureType: "procedural_timeline"`.

**Flow:**

1. Extract timeline candidates per document (date, title, type, source evidence).
2. Consolidate overlapping/duplicate items into one cleaned sequence.
3. Validate and normalize event type/date quality.
4. Emit via `document_feature_response` with payload `{ "proceduralTimeline": [...] }`.

**Scheduling and caching:**

- Procedural timeline generation runs in the scheduled chain after `list_of_dates` and `applicable_laws`.
- In-memory cache (`_workspace_procedural_timeline_payload`) supports reconnect/re-emit behavior, aligned with other feature payload caches.

---

## 4. End-to-End Data Flows

### 4.1 workspace_created

```
Backend emits workspace_created (workspaceId, userId, evidenceFiles, role, ...)
    → Socket handler (on_workspace_created)
    → Context manager: upsert_workspace(...)
    → workspace_role_integration.initialize_workspace_with_role(workspace_id, user_id, initial_role)
        → role_agent.invoke(..., user_input="ROLE:{role}", ...)   [Role-Based Agent]
    → document_processor.process_batch(files, workspace_id, user_id)
        → per file: download → extract text → store in vector_store (Qdrant)
    → Emit document_feature_response (document_assessments)
    → _get_workspace_category_and_subcategory(...) → emit document_feature_response (workspace_metadata)
    → _generate_strategies_from_documents(...)   [Gemini + vector store]
        → emit document_feature_response (strategy_objective)
    → _emit_analysis_report(...) / _generate_and_emit_analysis_report_for_sync(...)
        → _generate_analysis_report_from_documents(...)   [Gemini]
        → emit document_feature_response (analysis_report)
        → schedule background: list_of_dates then applicable_laws (see §3.4)
    → list_of_dates (scheduled) → emit document_feature_response (list_of_dates)
    → applicable_laws (scheduled): signals from vector store → Kanoon / CSE → Gemini JSON → normalize / heuristic
        → emit document_feature_response (applicable_laws)
```

Caches (`_workspace_document_assessments_payload`, `_workspace_strategy_payload`, `_workspace_analysis_payload`, `_workspace_list_of_dates_payload`, `_workspace_applicable_laws_payload`, etc.) allow re-emitting after socket reconnect.

### 4.2 workspace_sync

Similar to workspace_created but triggered when workspace is updated (e.g. new or removed documents). Handler decides whether to re-run document processing; if docs changed, it runs batch processing, then regenerates strategy, analysis report, list_of_dates, and applicable_laws and re-emits the same `document_feature_response` types. Strategy run uses `is_update=True`.

### 4.3 user_message_request

```
Backend emits user_message_request (chatId, workspaceId, userId, content, conversation_history, attachment?)
    → Socket handler (on_user_message_request)
    → workspace_manager.get_workspace(workspace_id) → role, role_context
    → vector_store.get_all_documents(workspace_id) → doc snippets for context
    → Optional: download attachment, extract text, cache by chat_id
    → Build ChatContext(workspace_id, user_id, role_context, workspace_state, chat_history, ...)
    → intelligence_chat_agent.process_message(..., chat_context)   [Intelligence Chat Agent]
        → context_builder → intent_classifier → explain_why (Gemini) → action_dispatcher → response_formatter → governance_validator
    → _generate_contextual_suggestion_chips(...) (e.g. 12 chips, send random 4)
    → Emit ai_response (content, metadata, suggestionChips)
```

---

## 5. Main Components

| Component | Path / Responsibility |
|-----------|------------------------|
| **Socket handler** | `app/socket/events.py` — Maps socket events to workspace init, document processing, strategy generation, analysis report, applicable laws, chat. Emits `document_feature_response` and `ai_response`. |
| **Socket client** | `app/socket/client.py` — Registers Socket.IO listeners (workspace_created, workspace_sync, user_message_request, etc.) and delegates to handler. |
| **Workspace manager / context** | `app/workspace/` — In-memory workspace and context; stores role, evidence files, metadata. Integration syncs role from LangGraph state via `workspace_role_integration`. |
| **Role-Based Agent** | `app/langgraph/role_agent.py` — Root LangGraph: role gate → role_selection / role_context_builder → role_validation. Output: validated `WorkspaceState` and `role_context` for downstream agents. |
| **Strategy generation (socket path)** | `app/socket/events.py` → `_generate_strategies_from_documents()` — Uses vector store + Gemini to produce case summary, strategic objective, recommendations. Emits `strategy_objective` feature. |
| **Strategy Agent (LangGraph)** | `app/langgraph/strategy_agent.py` — Full pipeline: suggest → map_objective → validate → lock. Used programmatically (e.g. examples), not from socket in current flow. |
| **Intelligence Chat Agent** | `app/langgraph/intelligence_chat_agent.py` — LangGraph: context_builder → intent_classifier → explain_why (Gemini) → action_dispatcher → response_formatter → governance_validator. Used for every `user_message_request`. |
| **Document processing** | `app/processing/document_processor.py` — Batch download and text extraction; stores content in vector store (Qdrant). |
| **Vector store** | `app/vectorstore/qdrant.py` — Stores document content by workspace; used by strategy generation and chat context. |
| **Gemini client** | `app/ai/gemini_client.py` — Used for strategy prompt, chat “explain_why”, analysis report generation, and applicable-laws JSON synthesis. |
| **Applicable laws (socket)** | `app/socket/events.py` — `_generate_and_emit_applicable_laws` and helpers: vector-store signals, Indian Kanoon + optional Google CSE search, Gemini normalization, heuristic fallback; see **§3.4**. |
| **Integration** | `app/langgraph/integration.py` — `WorkspaceRoleIntegration`: init workspace with role, sync role to workspace context, handle role change, process user message through role agent. |
| **Chat integration** | `app/langgraph/chat_integration.py` — `process_workspace_chat()` builds `ChatContext` from workspace and calls `intelligence_chat_agent.process_message()`. |
| **Observability** | `app/observability/` — Audit and tracing for role gate, role agent, strategy, and chat. |

---

## 6. State and Context Summary

- **WorkspaceState (LangGraph):** `workspace_id`, `user_id`, `role_state` (role, role_context, role_selected_at, role_history, role_validated), `messages`, `user_input`, `agent_output`, `metadata`, `error`, `next`. Used by Role-Based Agent.
- **ChatContext (input to Chat Agent):** `workspace_id`, `user_id`, `role_context`, `workspace_state`, `active_strategy`, `approved_facts`, `applicable_laws`, `active_module`, `chat_history`, `custom_instruction_suffix`, and for socket path: `uploaded_documents`, `chat_attachment`. Built in socket handler and optionally via `chat_integration.create_chat_context_from_state()`.
- **Strategy payload (output):** `caseSummary`, `strategicObjective`, `recommendations`, `possibleNextSteps`, `suggestionChips`, `role`, `isUpdate`, `analysis`, `timestamp`. Sent as `document_feature_response` with `featureType: "strategy_objective"`.
- **Applicable laws payload (cached / emitted):** `{ "laws": [ { "id", "act", "section", "description", "relevance" }, ... ] }` with `featureType: "applicable_laws"`. See **§3.4**.

---

## 7. Summary Diagram: Multi-Agent Flow

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                  SOCKET EVENTS                           │
                    │  workspace_created | workspace_sync | user_message_req   │
                    └───────────────────────────┬─────────────────────────────┘
                                                 │
         ┌───────────────────────────────────────┼───────────────────────────────────────┐
         │                                       │                                       │
         ▼                                       ▼                                       ▼
┌─────────────────────┐             ┌─────────────────────┐             ┌─────────────────────────────┐
│ workspace_created   │             │ workspace_sync      │             │ user_message_request        │
│ workspace_sync      │             │ (doc changes)      │             │                             │
└──────────┬──────────┘             └──────────┬──────────┘             └──────────────┬──────────────┘
           │                                    │                                      │
           ▼                                    ▼                                      ▼
┌─────────────────────┐             ┌─────────────────────┐             ┌─────────────────────────────┐
│ Role-Based Agent    │             │ Document Processor  │             │ Build ChatContext            │
│ (initialize /       │             │ → Vector Store      │             │ (role, docs, history,       │
│  role gate)         │             │                     │             │  attachment)                │
└──────────┬──────────┘             └──────────┬──────────┘             └──────────────┬──────────────┘
           │                                    │                                      │
           │                                    ▼                                      ▼
           │                         ┌─────────────────────┐             ┌─────────────────────────────┐
           │                         │ _generate_strategies│             │ Intelligence Chat Agent     │
           │                         │ _from_documents     │             │ (context → intent →          │
           │                         │ (Gemini + vector)   │             │  explain_why → format)       │
           │                         └──────────┬──────────┘             └──────────────┬──────────────┘
           │                                    │                                      │
           │                                    ▼                                      ▼
           │                         ┌─────────────────────┐             ┌─────────────────────────────┐
           │                         │ _emit_analysis_      │             │ ai_response (content,       │
           │                         │ report, list_of_dates│             │  suggestionChips)            │
           │                         │ applicable_laws      │             │                              │
           │                         └──────────┬──────────┘             └─────────────────────────────┘
           │                                    │                                      │
           └────────────────────────────────────┴──────────────────────────────────────┘
                                                 │
                                                 ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │  document_feature_response (assessments, strategy_       │
                    │  objective, analysis_report, list_of_dates, applicable_laws)│
                    │  | ai_response                                     │
                    └─────────────────────────────────────────────────────────┘
```

This is the **current system architecture** and **multi-agent flow** as implemented: Role-Based Agent as root and governance, document-based strategy generation via Gemini and vector store, and Intelligence Chat Agent for all user messages, all triggered and wired through the Socket.IO layer.
