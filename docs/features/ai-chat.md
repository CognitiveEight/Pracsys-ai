---
title: AI chat
type: feature
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Chat question to AI answer
  - Evidence to analysis panels
other_side:
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4653122
related_specs: []
---

# AI chat

## In plain words

A lawyer types a question about the case and gets an answer drawn from the
documents they uploaded. The AI service finds the passages that matter, writes
the answer, and then checks it. Every source it points to must really contain
what the answer says. Numbers are checked against the page they came from. If
a number cannot be found in any document, the answer says so and is marked as
low confidence. The lawyer can also ask the chat to change a panel, for
example to correct a date or reword a summary, or to change the case
objective. On a shared case, a colleague with view-only access can ask
anything but cannot change the case; the reply tells them so.

## Who uses it

- Lawyers working on their own case.
- Colleagues on a shared case (edit or view access).
- The backend, which forwards each message to the AI service and stores the reply.

## What the user sees

A chat panel scoped to the whole case or to one panel (analysis report,
strategy and objective, applicable laws, case laws, relevant parties,
drafting assistant). Answers carry numbered source markers that open the
cited page or row. Clickable follow-up suggestions appear under most replies.
See the lawyer app page: [AI chat — lawyer app](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4653122).

## How it works

1. The backend sends `user_message_request`. The service loads the case's
   cached state (warming it from Qdrant after a restart) and works out who is
   asking: `userId` is always the owner; `askedBy`, when present, is a
   collaborator whose permission comes from the cached sharing roster.
2. If `chat_type` is `DRAFTING_ASSISTANT`, the turn is handed to the drafting
   agent and the rest of this list does not apply.
3. The workspace role is resolved. A message that is only a role name
   (for example "Petitioner") sets the role and gets a short confirmation
   (see [Role selection](role-selection.md)).
4. Evidence is assembled as labelled passages: `[E#]` for document passages,
   `[C#]` for case laws, `[S#]` for statutes. Small workspaces (up to 15
   documents) go in whole; larger ones go through query rewrite, hybrid
   search and rerank. Annexures the lawyer names ("Annexure 3", a file name)
   are always included. Spreadsheet questions can add computed `[T#]` figures,
   and the knowledge graph can add facts when it is enabled. A file attached
   to the message is read and kept for follow-up turns in the same chat.
5. Before answering, a panel-edit detector looks for edit requests ("remove
   the third line of the case summary", a corrected date or amount, a rename,
   a new objective). If found and the asker may edit, the edit is applied to
   the cached panels and the changed panels are re-emitted.
6. The question runs through a fixed graph: context builder, intent
   classifier, explain-why (the model call), action dispatcher (records which
   agent should act; it does not run it), response formatter, governance
   validator.
7. Citations are checked: invented labels are removed, every figure in a
   cited sentence is checked against the cited passage (and re-pointed if it
   lives elsewhere), one repair pass runs, and labels are renumbered `[1]`,
   `[2]`. A figure still unsupported is flagged in the text and confidence
   drops to LOW.
8. The service emits `ai_response` with the text, `citations` (only those
   whose marker is still in the text), suggestion chips, confidence, intent
   and cost metadata.

## Rules and limits

- Chat is neutral and accuracy-first. Role shapes panel outputs more than chat tone.
- Nothing is cited that was not checked against the passage actually in the prompt.
- A view-only collaborator never changes the case: panel edits, fact
  corrections, objective changes, panel-update markers and role selection are
  all skipped, and the reply says so. Unknown asker on a known roster is
  read-only; unknown asker with no roster cached is allowed and logged.
- Surgical edits change only what was asked; both summaries are kept
  byte-for-byte.
- A new objective re-frames the case summary and report summary (emphasis
  and order only). The re-frame is dropped if it loses a figure, adds a new
  organisation, party or place, or breaks the mandated opening. The new
  objective lands either way. A role reassignment ("we act for X") re-orients
  advocacy and never re-frames the summary.
- Attachment text is capped at 12,000 characters. Total document context is
  capped at about 400,000 characters.
- A Stop never cancels a chat turn; old-run emits are fenced instead.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| A figure in the answer is not in any document | The figure is flagged inline; confidence LOW | Repair pass tried once; governance overrides confidence |
| Passage retrieval fails | A normal answer, from whole-document context | Falls back to the legacy whole-document selector and logs a warning |
| Socket drops before the reply is delivered | Usually, the reply arrives after reconnect (see Known gaps) | The undelivered reply is buffered and re-sent on reconnect |
| Objective change fails or is interrupted | A message that the change did not land; panels stop loading | Armed loaders are released with cached or fallback payloads |
| View-only collaborator asks for an edit | An answer plus a note that nothing was changed | The edit is skipped and audited with the asker's permission |
| Attachment cannot be read | An answer without the attachment | Logs a warning and continues |
| Unexpected error in the turn | An apology message containing the error text | Logs the exception and emits an error `ai_response` |

## For engineers

### Where the code is

- Handler: `app/socket/handlers/chat.py` (`on_user_message_request`,
  `_handle_drafting_assistant_message`, `_emit_document_feature`)
- Graph: `app/langgraph/intelligence_chat_agent.py`, `app/langgraph/chat_nodes.py`,
  `app/langgraph/chat_models.py`, `app/langgraph/chat_state.py`
- Evidence and citations: `app/rag/chat_context.py`, `app/rag/citations.py`,
  `app/rag/retriever.py`, `app/rag/table_query.py`
- Objective change and surgical edits: `app/socket/handlers/strategy.py`,
  `app/socket/handlers/objective_change.py`, `app/socket/handlers/user_facts.py`
- Sharing: `app/workspace/participants.py` (`resolve_asker`)
- Limits: `app/socket/event_utils.py` (`CHAT_*` constants)

### Events

- In: `user_message_request` (`content`, `chat_type`, `conversation_history`,
  `workspaceId`, `userId`, `chatId`, optional `attachment`, `askedBy`,
  `aiModel`). Also `probing_prompts_request`.
- Out: `ai_response`; `document_feature_response` and `workspace:refresh`
  when a chat turn edits panels.
- Full contract: [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

### Data

- Per-chat caches keyed by `chatId` (attachment text, last AI reply), linked
  to the workspace via `note_chat_workspace` so a purge reaches them.
- Panel caches per workspace (`_workspace_strategy_payload`,
  `_workspace_analysis_payload`, ...), persisted to Qdrant.
- Evidence passages in `<collection>_evidence_v1`.

### Security

- Never log message text, objective text or names; logs carry sizes and counts.
- Mutations gated on `mutations_allowed` from `resolve_asker`. New chat paths
  that write workspace state must check it.
- Participants are kept out of legal prompts except the `WHO IS ASKING`
  attribution block.

### Tests that guard it

- `tests/test_chat_state.py` (graph state keys, which is how citations were once lost)
- `tests/offline/verify_rag.py` (citation checking, chat evidence building)
- `tests/offline/verify_workspace_share.py` (read-only askers, role flip blocked)
- `tests/offline/verify_objective_change.py` (re-frame checks, surgical edit, ack wording, loader release)
- `tests/offline/verify_fact_corrections.py` (fact-correction fan-out)
- `tests/offline/verify_run_control.py` (stop fences chat emits)
- `scripts/e2e_local_backend.py` (`make e2e`, chat with citations; billed, run by a human)

### Known gaps

- The socket handler and panel-edit classifier have no direct automated test;
  coverage is through the offline suites above and `make e2e`.
- The undelivered-reply buffer (`_pending_ai_response`) holds one reply for the
  whole process, not one per chat; two undelivered replies in a row lose the first.
- On reconnect, `_do_reemit_after_reconnect` resets `_had_disconnect` before
  `on_connected` checks it, so when a panel re-emit is also pending the buffered
  reply appears not to be re-sent. Read from the code; not reproduced.
- The error reply includes the raw exception text in user-visible content.
- Intent values beyond the documented list, and the exact rerank model used,
  were not re-verified for this page.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
