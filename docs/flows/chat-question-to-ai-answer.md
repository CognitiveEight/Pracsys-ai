---
title: Chat question to AI answer
type: flow
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, backend, ai-service]
other_sides:
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4718649
  backend (socket contract): https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553
related_features: [../features/ai-chat.md, ../features/sharing-transfer-and-revoking-access.md, ../features/ai-cost-tracking.md]
related_specs: []
---

# Chat question to AI answer

## In plain words

When a lawyer types a question in a case, the AI service works out who is
asking and what side the case is on, then gathers the parts of the case
documents that bear on the question. For a small case it reads everything; for
a large one it searches for the most relevant passages. It also brings in what
the panels say and any attached file. The model writes an answer that points to the passages it used. Before
the answer is sent, the service checks every number and date in it against the
passage it cites. A citation that does not exist is removed, a figure found in a
different passage is re-pointed, and a figure found nowhere is marked as not
found in the documents and lowers the stated confidence. Some messages are
instructions rather than questions, such as a correction; those change the
panels instead.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app | Lawyer types a question in a chat (general or scoped to a panel), optionally with an attachment | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4718649) |
| 2 | Backend | Sends `user_message_request` with the owner's `userId`, history, `chat_type`, and `askedBy` if a collaborator typed it | [Socket contract (backend)](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553) |
| 3 | AI service | Resolves the asker, the role and the evidence; runs the chat graph; checks citations | This page, steps 1-8 |
| 4 | AI service | Sends one `ai_response` with the answer, citations, chips and cost | This page, step 9 |
| 5 | Backend | Stores the reply and forwards it | [Socket contract (backend)](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553) |
| 6 | Lawyer app | Shows the answer with numbered citations | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4718649) |

## The AI service's steps in detail

1. **Open the turn.** A per-turn cost scope opens. The payload is validated as
   `UserMessageRequestPayload`. The chat id is mapped to its workspace
   (`note_chat_workspace`) so a later purge can find chat-keyed caches. Caches are
   hydrated from Qdrant if the process is cold.
2. **Who is asking.** `resolve_asker` decides whether the asker may change the
   case. A read-only asker still gets a full answer; see
   [Share a case and revoke access](share-a-case-and-revoke-access.md).
3. **Drafting short-circuit.** `chat_type: DRAFTING_ASSISTANT` goes to the
   drafting agent instead and returns early.
4. **Role.** The role comes from the workspace context, then the persisted role
   cache. A message that is only a role name (for example "PETITIONER") sets the
   role and gets a short confirmation, unless the asker is read-only.
5. **Evidence.** All documents are listed in upload order (annexure N is the
   Nth upload). An annexure named in the message, by number or file name, is
   always included. With retrieval enabled, `build_chat_evidence`
   (`app/rag/chat_context.py`) builds labelled passages:
   - up to 15 documents that fit the budget go in whole;
   - otherwise the question is rewritten, searched with dense plus exact-term
     (hybrid) search, reranked, and retried once with new wording if nothing
     scores as relevant;
   - spreadsheet questions get exact computed results (`[T#]`), and graph facts
     are added when the knowledge graph is on.
   If that path fails, the older whole-document selection is used.
6. **Attachment.** A file attached to the message is downloaded and extracted
   (up to 12,000 characters) and cached for follow-up questions in the same chat.
7. **Instructions, not questions.** Before the model answers, the service checks
   for panel edits, fact corrections and objective changes. If one applies, the
   panels are updated and the reply becomes a short acknowledgement (see
   [Change the objective](change-the-objective.md)).
8. **The chat graph.** `ChatContext` (role, workspace state, evidence, cached
   analysis, case laws, parties, custom instructions, history, asker) goes to
   `intelligence_chat_agent.process_message`, which runs
   `context_builder → intent_classifier → explain_why → action_dispatcher →
   response_formatter → governance_validator`. `explain_why` makes the model call
   and then `_cite_answer` runs `verify_and_number` (`app/rag/citations.py`):
   invented labels removed, figures checked and re-pointed, labels renumbered to
   `[1]`, `[2]`. If figures are still unsupported, one repair call is made; any
   that remain are flagged inline, and `governance_validator` sets confidence to
   LOW. `action_dispatcher` only records routing; it calls no other agent.
9. **Reply.** Suggestion chips are generated (12 stored, 4 sent). The
   `ai_response` carries `content`, `metadata` (model, confidence, intent, cost),
   `suggestionChips` and only those `citations` whose marker is still in the
   text. If delivery is not confirmed, the reply is held and re-sent on
   reconnect.

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 4 | Role missing but documents exist | Normal answer | Proceeds with the stored or default role and logs a warning |
| 5 | Chunk store cannot serve the workspace | Normal answer, possibly less precise | Falls back to whole-document context |
| 5 | Search finds nothing relevant | Answer from first documents in upload order | Retries once with rewritten queries, then falls back |
| 8 | Model states a figure that no passage contains | Text says the figure could not be found in the documents; confidence LOW | One repair call, then inline flag |
| 8 | Model returns an error | Apology message with the error | Error `ai_response` sent with the cost so far |
| 9 | Socket drops as the reply is sent | Reply arrives after reconnect | Last undelivered reply buffered and re-sent |
| any | Lawyer pressed Stop on an update | Answer still arrives | Chat is never cancelled by a stop, only fenced for panel emits |

## For engineers

**Entry and exit.** Entry: `on_user_message_request` in
`app/socket/handlers/chat.py`. Exit: `SocketClient.emit_ai_response`.

**Events and payloads.** `UserMessageRequestPayload`, `AIResponsePayload`,
`AIResponseMetadata`, `Citation` in `app/socket/schemas.py`. Contract:
[Backend and AI service socket events](../contracts/backend-ai-socket-events.md).
A chat `aiModel` overrides the case model for this turn only.

**State and timing rules.**
- Graph: `app/langgraph/intelligence_chat_agent.py`, nodes in
  `app/langgraph/chat_nodes.py`, models in `app/langgraph/chat_models.py`.
- Retrieval: `app/rag/retriever.py`, `app/rag/chat_context.py`, `app/rag/table_query.py`.
- Flags: `RAG_V2_ENABLED`, `CITATIONS_ENABLED`, `CITATION_REPAIR_ENABLED`,
  `TABLE_QUERY_ENABLED` (`app/core/config.py`).
- Limits: `CHAT_SMALL_WORKSPACE_DOC_THRESHOLD` and the other `CHAT_*` constants in
  `app/socket/event_utils.py`.
- The turn is not in `CANCELLABLE_RUN_EVENTS`; `ai_response` is never fenced.

**Security checks.** Collaborator names reach the prompt only in the
`WHO IS ASKING` block. Message text, prompts and answers are not logged; the log
records counts and sizes. Every chat-driven write is gated on `mutations_allowed`.

**Tests.** `tests/offline/verify_rag.py` (evidence pack, citation checker,
graph facts), `tests/test_retrieval.py`, `tests/test_chat_state.py`,
`tests/test_tables.py` (table answers), `tests/offline/verify_workspace_share.py`
(read-only replies), `tests/offline/verify_fact_corrections.py`. `make e2e`
runs a real chat with citations.

**Known gaps.**
- When documents exist but no role is stored, the chat proceeds rather than
  asking for a role, so the role gate is soft here.
- Only one undelivered `ai_response` is held at a time; a second undelivered
  reply replaces the first.
- `user_message_request` does not revoke a pending purge (it is not in the
  revoking list in CLAUDE.md); not verified whether the backend blocks chat on a
  deleted case.

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
