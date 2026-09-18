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
│  Emits: document_feature_response (document_assessments, workspace_metadata,     │
│         strategy_objective, analysis_report, list_of_dates, applicable_laws,     │
│         procedural_timeline, case_laws, relevant_parties, drafting_assistant) |  │
│         ai_response (chat) | case_law_document_response                           │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Entry Points: Socket Events

The AI service is driven by **Socket.IO** events from the backend. Main events:

| Event                        | Direction   | Purpose |
|-----------------------------|------------|---------|
| `workspace_created`         | Backend → AI | New workspace: init role, process evidence files, generate the full feature suite |
| `workspace_sync`            | Backend → AI | Workspace updated: re-run doc processing / regeneration when docs change. Also carries `participants` — the sharing roster (§4.2b) |
| `workspace_version_restore` | Backend → AI | Workspace rolled back to an earlier document version: reconcile the vector store to that version and regenerate every panel |
| `user_message_request`      | Backend → AI | User chat message: build context, run Intelligence Chat Agent (or the Drafting Assistant when `chat_type="DRAFTING_ASSISTANT"`), return reply. `askedBy` names a non-owner asker on a shared case (§4.2b) |
| `template_sync`             | Backend → AI | Lawyer's template library changed (uploaded / renamed / deleted) |
| `add_case_law`              | Backend → AI | Add a precedent by citation / URL / pasted text ("Add Case Law" button) |
| `case_law_document_request` | Backend → AI | Lawyer uploaded a judgment file (GCS); classify + extract it, reply with `case_law_document_response` |
| `add_relevant_party` / `edit_relevant_party` / `delete_relevant_party` | Backend → AI | Relevant Parties panel CRUD (card add/edit/delete icons) |
| `probing_prompts_request`   | Backend → AI | Suggestion chips for strategy or chat |
| `analysis_report_feedback`  | Backend → AI | User feedback on report; recalc and re-emit analysis |
| `applicable_law_relevance_request` | Backend → AI | Re-assess one law's relevance to the matter |

### `workspace_version_restore` payload contract

```jsonc
{
  "workspaceId": "…", "userId": "…",
  "versionId": "…",              // required — the version being restored TO
  "evidenceFiles": [ /* EvidenceFilePayload */ ],  // AUTHORITATIVE: the FULL surviving set at that version
  "deletedDocumentIds": ["…"],   // advisory cross-check
  "restoredDocumentIds": ["…"],  // advisory cross-check
  "updatedAt": "2026-07-30T…",   // required ISO
  "workspaceRole": "…", "title": "…", "status": "…",
  "currentState": { /* WorkspaceCurrentState */ },
  "structure": "…", "tone": "…", "description": "…"
}
```

`evidenceFiles` is the contract that matters: the handler diffs it against the
live Qdrant document set and reconciles to it, so `deletedDocumentIds` /
`restoredDocumentIds` only need to be approximately right (a stale document the
backend forgot to list is still removed). Documents removed by a restore are
snapshotted into the removed-evidence ledger (`CacheKinds.REMOVED_DOCUMENTS`)
and reported as `analysis_report.missingInfo` gaps; re-ingesting the same
`documentId` clears the ledger entry. Idempotency is keyed on `versionId` in its
own 180 s window — restoring back to a recently-live version must not be
mistaken for a `workspace_sync` replay.

**Outbound (AI → Backend):**

| Event                      | When |
|----------------------------|------|
| `document_feature_response` | Feature payloads — `featureType` ∈ `document_assessments`, `workspace_metadata`, `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`, `procedural_timeline`, `case_laws`, `relevant_parties`, `drafting_assistant` |
| `ai_response`              | After processing a user chat message (augmented with draft fields for the drafting assistant) |
| `case_law_document_response` | Accept/reject verdict for an uploaded judgment; echoes `requestId`, always terminal |
| `workspace:refresh`        | Turns on frontend loaders before a regeneration cascade (e.g. an objective change) |

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

**Location:** Socket handler helper `_generate_strategies_from_documents()` in `app/socket/handlers/strategy.py` (a mixin of `SocketEventHandler`; `app/socket/events.py` is the thin composition root — see `docs/SOCKET_EVENTS_ARCHITECTURE.md`).  
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

**Location:** `app/socket/handlers/applicable_laws.py` — `_generate_and_emit_applicable_laws`, `_generate_applicable_laws_from_sources`, `_extract_case_signals_from_documents`, `_search_indiankanoon`, helpers `_normalize_applicable_laws_payload`, `_heuristic_applicable_laws_fallback`; fallback search in `app/search/vertex_ai_search.py`.

**Role:** Populate the **Applicable Laws** panel with a structured list of **potentially relevant Indian statutes and sections**, grounded in workspace documents and external search snippets. Outputs support **research and drafting direction**, not definitive legal advice.

**Flow:**

1. **Case signals** (`_extract_case_signals_from_documents`): Read up to the first five documents from the **vector store**; build `workspaceTitle`, a **search query** (title + frequent keywords from text), `keywords`, and a **document evidence snippet** (concatenated excerpts).
2. **Search (Indian Kanoon–oriented)**:
   - **Primary:** HTTP GET to `indiankanoon.org` search HTML; parse result links/titles (`_search_indiankanoon`).
   - **Fallback:** **Google Vertex AI Search** (`app/search/vertex_ai_search.py`) when `GCP_PROJECT_ID` and `VERTEX_SEARCH_DATA_STORE_ID` are set; otherwise skip. (Replaced the deprecated Google Custom Search / CSE integration.)
3. **Synthesis** (`_generate_applicable_laws_from_sources`): Single **Gemini** call (`application/json`) with case signals + search result titles/snippets. System instruction uses `get_structured_document_feature_system_instruction` so output stays structured (no chat gate text). Model returns JSON: `laws[]` entries with `act`, `section`, `description`, `relevance` (`High` / `Medium` / `Low`). Prompt rules: Indian law focus, **no IPC** in output (prefer BNS where relevant), concise case-tied descriptions. **Objective-steered:** an optional `objective_context` (the current strategic objective) makes the objective the **primary law-selection lens** — used on an objective change so the statute list shifts to the new objective (with `merge_with_previous=False` to replace, not union, stale laws). See **§3.6a**.
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

**Role:** Generate the **Upcoming Dates** panel from workspace documents, then emit and cache it as `featureType: "procedural_timeline"`.

**Scope (court/law procedural dates ONLY).** The panel carries only genuine procedural dates that are set **by a court / tribunal / forum or by law** and control how the case proceeds — exactly five kinds: **filing deadlines, hearing dates, evidence deadlines, compliance dates, and adjourned / next dates** (emitted as type `Court Hearing` or `Deadline`). Pre-litigation notices, police complaints / FIR / GD / missing-person reports, lawyer consultations / engagements, client meetings, and reconciliation attempts are **excluded**. Classification is **LLM-driven** via the extraction + consolidation prompts (no hardcoded keyword rules), and the agent **never invents dates** — if no procedural date exists it returns an empty timeline. A generic schema guard in `_node_validate_and_merge` drops any entry whose resolved type is not `Court Hearing` / `Deadline`.

**Flow:**

1. Extract procedural-date candidates per document (date, title, type, source evidence).
2. Consolidate overlapping/duplicate items into one cleaned sequence.
3. Validate and normalize event type/date quality (and drop non-procedural types).
4. Emit via `document_feature_response` with payload `{ "proceduralTimeline": [...] }`.

**Scheduling and caching:**

- Procedural timeline generation runs in the scheduled chain after `list_of_dates` and `applicable_laws`.
- In-memory cache (`_workspace_procedural_timeline_payload`) supports reconnect/re-emit behavior, aligned with other feature payload caches.

---

### 3.6 Drafting Assistant

**Location:** `app/langgraph/drafting_agent.py` + `app/prompts/drafting.py` + `app/socket/events.py` (`on_template_sync`, `_generate_and_emit_drafting_suggestions`, `_handle_drafting_assistant_message`).

**Role:** Help the lawyer prepare case-specific pleadings, applications, legal notices, and replies — either from scratch (Blank Draft), from an AI-suggested draft, or by reusing one of their uploaded templates as a structural skeleton.

Two surfaces:

1. **Template Library** (lawyer-scoped, cross-workspace) — the lawyer uploads templates (Letterheads, Folders, Document Drafts). BE emits `template_sync` (`action="uploaded"|"renamed"|"deleted"`) to the AI service; AI downloads each new template, extracts the full text via `app/processing/file_handlers.py::FileProcessor.process_file_by_extension`, and persists it under a **lawyer-scoped Qdrant cache** keyed by `(userId, templateId)` via the new `upsert_user_state_cache` / `get_user_state_cache` helpers (zero-vector, metadata-only — same trick used for workspace state caches). `fileHash` is used as a content-identity check so a `template_sync` replay does not re-extract unchanged content. Templates are **not chunked or embedded** — they are reused as a whole-document structural reference.

   **Template ingest is noise-hardened.** Extracted template text is normalised before it is cached so extraction artifacts never reach the render LLM: a verbatim de-dup (`_normalize_template_text` / `_dedupe_leading_repeated_block`) collapses repeated PDF letterheads and `--- Page N ---` markers on both the OCR and text-layer paths, and a one-time LLM cleanup at ingest (`_clean_template_ocr_text`, chunked for long templates with an accept/reject gate) strips scanned-template seal/stamp garble, page footers, and margin notes while preserving every numbered paragraph, heading, recipient/signature block, and literal placeholder verbatim.

2. **Drafting Assistant panel** inside a workspace —
   - Proactive **suggested drafts** generated at `workspace_created` / `workspace_sync` via `drafting_agent.suggest(...)`, scheduled after `list_of_dates` / `applicable_laws` / `procedural_timeline`. The result (`{title, relevance, description}` items, no IPC references) is cached in `_workspace_drafting_payload` and emitted as `document_feature_response` with `featureType: "drafting_assistant"`. Re-emitted on reconnect like other panel features. A deterministic **template-coverage pass** (`_ensure_template_coverage`) runs after the LLM suggestions: for every ready template not already bound to a card, a focused applicability check (`build_template_applicability_prompt` / `TEMPLATE_APPLICABILITY_INSTRUCTION`) decides — by the template's **legal purpose + document type**, never its original parties/facts — whether a draft of that kind fits this matter, and any applicable template **type group** is **guaranteed** a case-framed card (same-type drafts share one card rather than spawning duplicates). Template-backed cards take priority and the panel cap is raised from 6 to 8 (`_MAX_SUGGESTIONS_WITH_TEMPLATES`). **Multi-draft matching:** a card surfaces **every** matching library draft, each scored 0–100 for its fit to this case — on the wire, `suggestedDrafts[].matches[]` of `{templateId, templateName, category, matchPercent}` sorted by score (serialized from the agent's richer internal shape by `event_utils.drafting_payload_to_wire`). There is no minimum-score gate: weak fits are shown with their honest score, and the frontend renders "DRAFT AVAILABLE" for one match or "N DRAFTS FOUND" + a pick-one modal for several.
   - On lawyer click ("Start a New Draft" → pick template / suggestion / Blank Draft / Custom AI Draft), BE emits the existing `user_message_request` event with `chat_type="DRAFTING_ASSISTANT"` plus optional drafting fields (`actionableId`, `source`, `draftMode`, `templateId` / `suggestedDraftId`, `templateUrl`, `templateOriginalName`, `templateMimeType`). The AI short-circuits the general intelligence chat and routes the turn to `drafting_agent.handle_turn(...)`. When `draftMode="custom"` ("No, Custom AI Draft") the kickoff **bypasses all template resolution / profiling** and drafts directly from case context, still gathering any missing lawyer-input slots through the same `openQuestions` flow.

**Conversation lifecycle (per `actionableId`):**

- **Kick-off turn** (`conversation_history` is empty, plus `source`/`draftMode`/`templateId`/`suggestedDraftId`): if a template is supplied, the agent resolves it from the lawyer cache (lazy-fetching via `templateUrl` if the cache is cold), then runs a single Gemini call that classifies the template into *skeleton* (preserve verbatim), *case-variable* (substitute), *context-bound* (strip — e.g. property facts in a reply-notice template adapted to a dowry matter), and *lawyer-input* slots; substantive content / substantiation clauses are also captured as fillable variables. The agent **confirms every variable with the lawyer rather than silently auto-filling** from case context: variables default to being asked, a global auto-fill quality gate rejects informal labels / contact ids / vague values, and the recipient email and each cost/compensation head are asked individually. It replies with a chat message + an `openQuestions[]` array of slots. `draftStatus="gathering_info"`. Kickoff wording is **domain-neutral** (questions refer to "the product / goods / asset" generically, never the template's original product/brand/party), literal placeholders (`<…>` / `{{…}}` / `(Pascal_Case)`) are guaranteed to surface as questions (`_ensure_placeholder_variables`), and the final question set is de-duplicated (`_dedup_kickoff_questions`) so an umbrella phrasing and its granular parts are not both asked. Profiled templates are versioned by `PROFILE_SCHEMA_VERSION` (currently **4**) so cached templates auto re-profile on the next template sync / reconnect.
- **Follow-up turns**: the agent maps the lawyer's free-text reply onto remaining `openQuestions` (using a focused Gemini call that returns a `{question_id: answer}` JSON map), updates session state, and either re-asks remaining questions or renders the final draft.
- **Render**: a single Gemini call with the *template-fidelity* prompt produces the full draft as a **Markdown string** carried in `draftDocument.content`, with no lead-in/preamble prose before the body. Headers, salutations, party blocks, prayer scaffolding, verification, and signature blocks from the template are preserved verbatim; repeated letterheads, page markers, and OCR seal/stamp garble are treated as artifacts and dropped. Indian-law framing only (BNS / BNSS / BSA / IT Act + targeted civil acts — no IPC, no CrPC, no Indian Evidence Act). `draftStatus="ready"`. Because drafts can run to 10–20 pages, the render (and draft translation) use the dedicated **long-form path** `gemini_client.generate_longform(...)` on the current **`google-genai`** SDK: full **64k output ceiling** (`max_tokens=65536`) and **`thinking_level="LOW"`** (lowered from `HIGH`, which removed a latency/truncation regression while still producing the full structure). Gemini 3 bills thinking tokens against the output budget, so `finish_reason` + token usage are logged per call. The path falls back to the legacy `generate()` automatically if the new SDK is unavailable.
- **Revision turn** (lawyer asks for a change after a `ready` turn): re-renders with the same skeleton, `version++`. The chosen draft language persists across revisions (`draft_language` in session state); clicking a *different* suggested-draft / template card re-kicks the conversation cleanly.

**Session state** is persisted per `actionableId` in a `drafting_session` workspace state cache (Qdrant), so drafts survive reconnects and AI service restarts.

### 3.6a Chat-Driven Panel Editing & Objective Change

**Location:** `app/socket/handlers/chat.py` (the `user_message_request` handler for `chat_type="STRATEGY_OBJECTIVE"`, classifiers `_llm_classify_panel_edit_intent` / `_llm_classify_objective_change_intent`), the per-panel surgical editors in each feature mixin, and the objective-change cascade `_run_objective_change_incremental_refresh` in `app/socket/handlers/objective_change.py` (scheduled by `_schedule_objective_change_reanalysis`).

A chat message in the Strategy & Objective panel is routed into one of three intents, decided by the LLM with a deterministic question guard (`_is_pure_informational_query`) in front so a plain question never edits a panel:

1. **Informational question** → answered normally; no panel changes.
2. **Mode 1 — surgical name / term / date / year edit** (`isRename` or a per-panel edit slot). The correction is applied as an exhaustive, workspace-wide **find-and-replace over EXISTING content** across all panels — Case Summary, Report Summary, Strategic Objective, Possible Next Steps, Analysis Report, List of Dates, Applicable Laws, Upcoming Dates — with **no regeneration** (no new/removed dates, laws, or items). A NAME is applied **semantically** (a party referenced by role/description is named correctly even where it appears as "the Petitioner"); a DATE/YEAR correction also rewrites the ISO `date` field of List-of-Dates / Upcoming-Dates entries, while statute names and section numbers stay verbatim. The dispatch fans one instruction to every panel editor deterministically. The strategy fields emit a single coalesced `strategy_objective` payload; other panels emit their own `document_feature_response`. A **terminal-emit guard** re-emits the preserved cached payload for any doc-derived panel whose edit was a no-op (the token didn't appear there), so the loaders lit by `workspace:refresh` always clear.
3. **Mode 2 — objective / case-perspective change** (`direct_update`). Declaring a new objective runs `_regenerate_strategy_for_new_objective` — which regenerates the objective, next steps and chips **and re-frames the Case Summary + Report Summary** (see below) — then the cascade, which:
   - **regenerates + REPLACES the Analysis Report** for the new objective (it does NOT merge with the prior report — a perspective change must drop the old framing and old quality score; `_merge_analysis_report_with_previous` is reserved for the document re-upload / `workspace_sync` paths), and **guarantees a refreshed quality score explanation** (see below);
   - **re-scores List of Dates relevance** only (`_reeval_list_of_dates_relevance` — same entries, no new/removed dates);
   - **regenerates Applicable Laws** with the objective as the primary law-selection lens (`_generate_and_emit_applicable_laws(objective_context=…, merge_with_previous=False)`);
   - **preserves and re-emits from cache** Upcoming Dates (procedural timeline), **Relevant Parties**, **Case Laws**, and **Drafting suggestions** — who the parties are, which precedents were found/uploaded, the court dates, and the template library don't change with the legal objective; those panels regenerate only on document add/delete.

   **Case Summary re-framing.** A new objective changes which facts are decisive, so the Case Summary and Report Summary are re-framed to lead with them, in the SAME call that produces the new objective (they ride the one coalesced `strategy_objective` emit). Re-framing is **emphasis and ordering, never facts**: the summary is the fact-of-record narrative that every other panel's prompt is built on, and its facts belong to the documents, not to the objective. Three deterministic checks enforce that in `_regenerate_strategy_for_new_objective`, because the prompt alone would not — every figure in the previous summary must survive (`find_dropped_case_figures`), no new organisation/party/place may appear (`find_ungrounded_proper_nouns`), and the mandated opening (`This case is about…` / `This matter is about…`) must be intact. A candidate that fails any check is discarded and the previous summary stands, and because the two summaries must state the same facts, one failing holds the other back too. **The new objective lands either way** — the re-frame is best-effort on top of it. The chat acknowledgement names the Case Summary only when it actually moved. `refresh_case_summary=False` restores the old lock-verbatim behaviour and is used by the **role-reassignment** caller, where the trigger changes who is advocated for rather than what is being argued. A **surgical** objective edit ("reword that line") is a different function, `_regenerate_strategic_objective_with_feedback`, and still leaves the summaries byte-for-byte.
   **Quality score explanation.** The score measures readiness *for an objective*, so the explanation of it moves when the objective moves — a lawyer who has just re-pointed the case and sees the same sentences concludes nothing happened. Two changes make that hold. First, the explanation is **objective-aware everywhere it is produced**: `_ensure_quality_score_explanation` takes the current objective (read from the strategy cache via `_current_strategic_objective`, since the analysis report never carries `strategicObjective`) and passes it to the LLM pass, the deterministic fallback (`_objective_clause_for_explanation`) and the main report prompt, each of which must state how the objective bears on the score. Second, the cascade **forces** the refresh: if the regenerated report's explanation is missing, or byte-identical to the report being replaced, it is regenerated with the new objective in hand and `reportQualityScoreExplanation` is re-derived from it. That second step matters because `qualityScoreExplanation` is one of the first fields lost to JSON truncation, and the generic refill that covers for it is objective-blind. The same objective is now also threaded through the `analysis_report_feedback` and `added_missing_information` regenerations, and through the `workspace_sync` merge.

   An up-front `workspace:refresh` (`reason="objective_change_regeneration_started"`) shows the loader across the affected panels; every preserved panel re-emits its cached payload as the terminal state so no loader is left stuck (a failed regeneration also falls back to a cached-or-empty terminal emit).

So a name correction stays surgical and consistent everywhere, while an objective change re-derives the objective-dependent intelligence (objective, summaries' framing, analysis, score, laws) and leaves the document-derived panels stable.

**Document deletion** (a `workspace_sync` with removed docs) is the opposite of an objective change: the derived panels must reflect the reduced document set, so the regenerated Analysis Report **replaces** the previous one and the web-search-derived panels (Case Laws, Applicable Laws) are re-derived with `merge_with_previous=False`; uploads keep the merge/accumulate behaviour.

### 3.6b Output Guardrails & Generation Controls

**Shared output directives** (`app/prompts/legal_intelligence_system.py`, injected into the structured + chat system instructions and the inline case-summary / list-of-dates prompts and the procedural-timeline instruction):

- `ENGLISH_ONLY_OUTPUT_DIRECTIVE` — all output in English / Latin script; non-Latin (e.g. Devanagari) names are transliterated.
- `APP_BRAND_EXCLUSION_DIRECTIVE` + `scrub_app_brand()` — the platform's own name ("SimplerToday") is never surfaced as a case party/fact (prompt guardrail + a deterministic recursive scrub applied to each feature payload before cache/emit; **not** applied at the Qdrant layer, which must keep the intake-PDF text intact for date parsing).
- `NAME_CAPITALISATION_DIRECTIVE` — person names / places / addresses in normal Title Case, never ALL-CAPS (statutory acronyms such as BNS / BNSS / BSA / FIR preserved).

**Gemini thinking budget.** gemini-3 is a thinking model whose hidden reasoning tokens are billed against `max_output_tokens`. `GeminiClient.generate()` runs primarily on the `google-genai` SDK with an explicit `thinking_level` (default `LOW`) so the reasoning pass stays bounded — this keeps responses fast and prevents `finish_reason=MAX_TOKENS` truncation — with a moderate output ceiling and a transparent fallback to the legacy `google-generativeai` SDK. Latency is controlled by `thinking_level`, not by inflating the token ceiling.

**Display trimming.** Panel descriptions (List of Dates, Procedural Timeline, Drafting suggestions) are trimmed only at a word boundary — never via a naive sentence split, which used to cut at abbreviation periods ("Model No.", "SRF No.") and leave dangling fragments.

**Outbound shape (augmented `ai_response`):**

```json
{
  "chatId": "...",
  "actionableId": "user_draft_abc123",
  "chat_type": "DRAFTING_ASSISTANT",
  "content": "Conversational reply (questions or lead-in for the final draft)",
  "draftStatus": "analyzing | gathering_info | ready | revising",
  "openQuestions": [{"id": "...", "label": "..."}],
  "draftDocument": {
    "title": "...",
    "format": "markdown",
    "content": "# IN THE COURT OF ...\n\n...",
    "version": 1,
    "sourceTemplateId": "...",
    "sourceTemplateName": "...",
    "sourceType": "template | suggestedDraft | blank",
    "wordCount": 487
  }
}
```

---

### 3.7 Case Laws (Source-Verified Precedents)

**Location:** `app/socket/handlers/case_laws.py` + `app/search/case_law_finder.py` + `app/prompts/case_laws_prompts.py`.

**Role:** Populate the **Case Laws** panel with prior court judgments relevant to the workspace, each carrying a real, clickable **source link** (`sourceUrl` + contract mirrors `referenceUrl` / `referenceName`), citation, court, headnote fields, and a High/Medium/Low relevance badge. Emitted as `featureType: "case_laws"` (`payload.caseLaws[]`, capped at 8, stable `caselaw_N` ids; `citation` carries the combined `"Title, Citation"` string per the backend contract).

**Retrieval (`find_case_law_sources()`)** aggregates three channels: Indian Kanoon direct search (a legal-issue query derived from the matter with party/brand names stripped), **Vertex AI Search** fallback, and **Gemini `google_search` grounding** (`gemini_client.generate_grounded()`, real-time web; grounding redirect URLs are resolved to their real destinations). The selection is **strict source-verified**: the LLM ranks and summarizes only retrieved judgments and may not invent citations.

**Two-pass legal digest:** Pass 1 selects relevant precedents and produces the case-vs-matter comparison fields (`relevance`, `matchedFacts`, `applicabilityReason`, `distinguishing`, `adverseImpact`, `confidence`). Pass 2 **fetches the actual judgment text** (`fetch_judgment_text()`, Indian Kanoon `/doc/` page preferred) and extracts the judgment-intrinsic headnote fields strictly from it (`citation`, `court`, `bench`, `date`, `legalIssues`, `materialFacts`, `ratio`, `holding`, `relief`, …). `bindingStrength`/`bindingStars` are derived deterministically from the court; `legalStatus` defaults to **"Not independently verified"**. A quality gate drops cards that failed enrichment or are off-topic.

**Manual additions:**

- `add_case_law` — by citation / URL / pasted text; the AI resolves and verifies a real source, assesses relevance, appends (deduped by URL), and re-emits. No real source → cached panel re-emitted unchanged.
- `case_law_document_request` → `case_law_document_response` — a lawyer-uploaded judgment file (GCS). The AI fetches `documentUrl`, **classifies whether it is a case law**, extracts `{citation, court, summary, relevance, referenceUrl, referenceName}` on acceptance and merges it into the panel; otherwise replies `accepted:false` with a `rejectionReason`. Every path (including failure) emits a terminal response echoing `requestId`.

**Lifecycle:** generated on `workspace_created` / `workspace_sync`, cached per-workspace (`CacheKinds.CASE_LAWS`), re-emitted on reconnect, **preserved on objective change**, re-derived (replace, not merge) on document deletion. Chat scope `chat_type:"CASE_LAWS"` injects the cached precedents into `ChatContext.case_laws`.

---

### 3.8 Relevant Parties

**Location:** `app/socket/handlers/relevant_parties.py` + `app/socket/handlers/panel_edits.py` + `app/prompts/relevant_parties_prompts.py`.

**Role:** A directory of the **key people/organisations actually party to the matter**, emitted as `featureType: "relevant_parties"` (`payload.parties[]`, capped at 30): `{ id, name, role, details, contactNumber, email, officialIdUrl }`. `role` is a normalized Title-Case enum — `Petitioner | Respondent | Witness | Victim | Accused | Other` (legacy/synonym wordings are mapped via `_normalize_party_role`). Contact fields are **PII: always empty on auto-generation**, populated only by the user through panel edits.

**Pipeline:** reuses the cached party extraction (`_extract_parties_subagent`) and classifies each party into the role enum (`_classify_relevant_parties`) **using only document-grounded names**, with a **relevance filter** that drops comparative/example-only or tangential entities while keeping genuine parties, counsel, and the adjudicating authority.

**Per-document provenance:** each auto-extracted party carries `sourceDocIds`. Deleting a document purges exactly that document's parties; a new upload extracts parties **only from the unseen document(s)**; missing/stale provenance triggers a one-time full refresh that keeps only user-added parties. Identity is a canonical name key (honorifics, corporate prefixes/suffixes, parentheticals, punctuation stripped) plus a guarded fuzzy match (`_party_names_match`) so re-extraction variants of the same entity never duplicate, and user-deleted parties stay deleted.

**CRUD events:** `add_relevant_party` (append, fuzzy-deduped, flagged `userAdded` so purging never removes it), `edit_relevant_party` (by `partyId`), `delete_relevant_party` (records the name in a deleted-set so regeneration won't re-add it). Each mutates the cache and re-emits.

**Lifecycle:** generated on `workspace_created` / `workspace_sync` (document-driven), cached (`CacheKinds.RELEVANT_PARTIES`), re-emitted on reconnect, **preserved on objective change**. Chat scope `chat_type:"RELEVANT_PARTIES"` injects the cached parties into `ChatContext.relevant_parties`.

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
        → schedule background chain (asyncio.create_task; chat is never blocked):
            list_of_dates → emit document_feature_response (list_of_dates)
            applicable_laws: signals from vector store → Kanoon / Vertex AI Search → Gemini JSON → normalize / heuristic
                → emit document_feature_response (applicable_laws)
            procedural_timeline → emit document_feature_response (procedural_timeline)
            relevant_parties (per-doc extraction + classification) → emit document_feature_response (relevant_parties)
            case_laws (Kanoon + Vertex + Gemini grounding, two-pass digest) → emit document_feature_response (case_laws)
            drafting suggestions (template matching + scoring) → emit document_feature_response (drafting_assistant)
```

Caches (`_workspace_document_assessments_payload`, `_workspace_strategy_payload`, `_workspace_analysis_payload`, `_workspace_list_of_dates_payload`, `_workspace_applicable_laws_payload`, `_workspace_procedural_timeline_payload`, `_workspace_relevant_parties_payload`, `_workspace_case_laws_payload`, `_workspace_drafting_payload`) allow re-emitting after socket reconnect.

### 4.2 workspace_sync

Similar to workspace_created but triggered when workspace is updated (e.g. new or removed documents). Handler decides whether to re-run document processing; if docs changed, it runs batch processing, then regenerates strategy, analysis report, list_of_dates, and applicable_laws and re-emits the same `document_feature_response` types. Strategy run uses `is_update=True`.

### 4.2b Workspace sharing (participants / askedBy)

A case owner can share a workspace with other users. Two additive fields carry
that to this service, and **`userId` never changes** on either — it stays the
case OWNER, so every owner-scoped cache (role, cumulative token usage, panel
payloads) keeps its existing key.

- **`workspace_sync.participants`** — `[{userId, name, permission}]`, permission
  `edit` or `view`. **Absent entirely (not an empty array) when the case is not
  shared**, so on `workspace_sync` an absent field is read as *un-shared* and
  drops the roster; keeping a stale one would leave a removed collaborator with
  edit rights. `workspace_version_restore` inherits the field but is **not**
  authoritative — it is a document-version operation and says nothing about
  sharing, so an absent field there leaves the roster alone.
- **`user_message_request.askedBy`** — `{userId, name}`, present only when
  someone **other than the owner** typed the message.

The roster lives in `app/workspace/participants.py` (module-level, write-through
to Qdrant as `CacheKinds.PARTICIPANTS`, hydrated on a cold workspace, cleared by
the `workspace_deleted` purge — participant names are personal data). It exists
to answer one question, via `resolve_asker(...)`: **may this person change the
case, or only read it?**

A `view` collaborator may ask anything and gets the same analysis, but every
chat-driven mutation is skipped for them and the reply says so instead of
implying the change landed: surgical panel edits, fact corrections, the
objective-change cascade, AI-marker panel updates, and role selection (the role
gate is the root of all derived intelligence, so it is the most damaging write a
reader could make). An asker who is not on a roster we *do* have is treated as
read-only; an asker on a workspace with **no** roster cached is allowed to edit
and logged — the sharing sync simply may not have reached this process yet, and
failing closed there would break every collaborator on a case we have not seen.

Participants are **application users, not parties to the dispute**. They are
deliberately kept out of every legal prompt (they would corrupt
`relevant_parties`, which is extracted from evidence). The only prompt exposure
is a short `WHO IS ASKING` attribution block naming the collaborator and their
access level, which explicitly instructs the model that access level changes
nothing about the analysis.

### 4.3 user_message_request

```
Backend emits user_message_request (chatId, workspaceId, userId, content, conversation_history, attachment?, askedBy?)
    → Socket handler (on_user_message_request)
    → resolve_asker(workspaceId, userId, askedBy) → owner | collaborator(edit|view)
    → workspace_manager.get_workspace(workspace_id) → role, role_context

    [if chat_type == "DRAFTING_ASSISTANT" — Drafting Assistant short-circuit]
        → _handle_drafting_assistant_message(payload, ...)
            → drafting_agent.handle_turn(message, case_context, request_fields, history)
                kick-off   → analyze template vs case → openQuestions[]
                follow-up  → map answer → remaining openQuestions OR render
                revision   → re-render preserving template skeleton
        → emit augmented ai_response (draftStatus, openQuestions, draftDocument)
        → return

    [else — general intelligence chat]
    → vector_store.get_all_documents(workspace_id) → doc snippets for context
    → Optional: download attachment, extract text, cache by chat_id
    → Build ChatContext(workspace_id, user_id, role_context, workspace_state, chat_history, ...)
    → intelligence_chat_agent.process_message(..., chat_context)   [Intelligence Chat Agent]
        → context_builder → intent_classifier → explain_why (Gemini) → action_dispatcher → response_formatter → governance_validator
    → _generate_contextual_suggestion_chips(...) (e.g. 12 chips, send random 4)
    → Emit ai_response (content, metadata, suggestionChips)
```

### 4.4 template_sync

```
Backend emits template_sync (action: uploaded|renamed|deleted, userId, templates[])
    → Socket handler (on_template_sync)
    → action == "uploaded": for each template
        → document_processor._download_file(fileUrl) → bytes
        → file_processor.process_file_by_extension(bytes, filename) → extracted text
        → vector_store.upsert_user_state_cache(userId, kind="template", scope_id=templateId, data={...})
    → action == "renamed": patch cached payload's name; no re-download
    → action == "deleted": vector_store.delete_user_state_cache(userId, "template", templateId)
```

---

## 5. Main Components

| Component | Path / Responsibility |
|-----------|------------------------|
| **Socket handler** | `app/socket/events.py` — thin composition root: `SocketEventHandler` = 21 per-feature mixin classes from `app/socket/handlers/` (strategy, analysis_report, list_of_dates, applicable_laws, procedural_timeline, case_laws, relevant_parties, drafting, chat, objective_change, workspace_events, panel_edits, …) + `register`/`handle` + the `event_handler` singleton. Pure helpers/constants in `app/socket/event_utils.py`. See `docs/SOCKET_EVENTS_ARCHITECTURE.md`. |
| **Socket client** | `app/socket/client.py` — Registers Socket.IO listeners (workspace_created, workspace_sync, user_message_request, template_sync, add_case_law, case_law_document_request, party CRUD, etc.) and delegates to handler. |
| **Workspace manager / context** | `app/workspace/` — In-memory workspace and context; stores role, evidence files, metadata. Integration syncs role from LangGraph state via `workspace_role_integration`. |
| **Role-Based Agent** | `app/langgraph/role_agent.py` — Root LangGraph: role gate → role_selection / role_context_builder → role_validation. Output: validated `WorkspaceState` and `role_context` for downstream agents. |
| **Strategy generation (socket path)** | `app/socket/events.py` → `_generate_strategies_from_documents()` — Uses vector store + Gemini to produce case summary, strategic objective, recommendations. Emits `strategy_objective` feature. |
| **Strategy Agent (LangGraph)** | `app/langgraph/strategy_agent.py` — Full pipeline: suggest → map_objective → validate → lock. Used programmatically (e.g. examples), not from socket in current flow. |
| **Intelligence Chat Agent** | `app/langgraph/intelligence_chat_agent.py` — LangGraph: context_builder → intent_classifier → explain_why (Gemini) → action_dispatcher → response_formatter → governance_validator. Used for every `user_message_request`. |
| **Document processing** | `app/processing/document_processor.py` — Batch download and text extraction; stores content in vector store (Qdrant). |
| **Vector store** | `app/vectorstore/qdrant.py` — Stores document content by workspace; used by strategy generation and chat context. |
| **Gemini client** | `app/ai/gemini_client.py` — Used for strategy prompt, chat “explain_why”, analysis report generation, and applicable-laws JSON synthesis. `generate()` runs primarily on the **`google-genai`** SDK with explicit **`thinking_level`** control (default `LOW`) and a moderate output ceiling, falling back to the legacy `google-generativeai` SDK; long-form draft render / translation use `generate_longform()` (`thinking_level="LOW"`, 64k ceiling); case-law research uses `generate_grounded()` (`google_search` grounding, returns text + source URLs; runs the sync google-genai client in a worker thread to avoid an aiohttp-version conflict). Bounding the thinking budget (rather than inflating `max_output_tokens`) keeps gemini-3 responses fast and avoids `MAX_TOKENS` truncation. All calls log `finish_reason` + token usage via `_log_generation_diagnostics`. Model is configurable via `GEMINI_MODEL` (e.g. `gemini-3-flash-preview`). |
| **Applicable laws (socket)** | `app/socket/handlers/applicable_laws.py` — `_generate_and_emit_applicable_laws` and helpers: vector-store signals, Indian Kanoon + optional Vertex AI Search fallback, Gemini normalization, heuristic fallback; see **§3.4**. |
| **Case laws** | `app/socket/handlers/case_laws.py` + `app/search/case_law_finder.py` — source aggregation (Kanoon / Vertex AI Search / Gemini `google_search` grounding), two-pass judgment-grounded digest, manual add & upload verification; see **§3.7**. |
| **Relevant parties** | `app/socket/handlers/relevant_parties.py` + `app/socket/handlers/panel_edits.py` — per-document provenance extraction, role classification, fuzzy identity dedup, CRUD events; see **§3.8**. |
| **Legal search** | `app/search/vertex_ai_search.py` — Google Vertex AI Search client used as the retrieval fallback for applicable laws and case laws (replaced Google CSE). |
| **Integration** | `app/langgraph/integration.py` — `WorkspaceRoleIntegration`: init workspace with role, sync role to workspace context, handle role change, process user message through role agent. |
| **Chat integration** | `app/langgraph/chat_integration.py` — `process_workspace_chat()` builds `ChatContext` from workspace and calls `intelligence_chat_agent.process_message()`. |
| **Observability** | `app/observability/` — Audit and tracing for role gate, role agent, strategy, and chat. |

---

## 6. State and Context Summary

- **WorkspaceState (LangGraph):** `workspace_id`, `user_id`, `role_state` (role, role_context, role_selected_at, role_history, role_validated), `messages`, `user_input`, `agent_output`, `metadata`, `error`, `next`. Used by Role-Based Agent.
- **ChatContext (input to Chat Agent):** `workspace_id`, `user_id`, `role_context`, `workspace_state`, `active_strategy`, `approved_facts`, `applicable_laws`, `case_laws`, `relevant_parties`, `active_module`, `chat_history`, `custom_instruction_suffix`, and for socket path: `uploaded_documents`, `chat_attachment`. Built in socket handler and optionally via `chat_integration.create_chat_context_from_state()`. Scoped chats (`chat_type` `CASE_LAWS` / `RELEVANT_PARTIES`) reason only over the corresponding cached panel content.
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
                    │  document_feature_response (assessments, metadata,       │
                    │  strategy_objective, analysis_report, list_of_dates,     │
                    │  applicable_laws, procedural_timeline, case_laws,        │
                    │  relevant_parties, drafting_assistant) | ai_response |   │
                    │  case_law_document_response                              │
                    └─────────────────────────────────────────────────────────┘
```

This is the **current system architecture** and **multi-agent flow** as implemented: Role-Based Agent as root and governance, document-based strategy generation via Gemini and vector store, and Intelligence Chat Agent for all user messages, all triggered and wired through the Socket.IO layer.
