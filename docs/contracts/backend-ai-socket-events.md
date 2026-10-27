---
title: Backend ↔ AI service — socket events
type: contract
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Messages"
last_verified: 2026-10-21
jira: SIM-113
between: "backend, ai-service"
owner_ours: AI team
owner_theirs: Backend team
version: "handshake scheme v1; event payloads are not versioned"
source_of_truth: app/socket/schemas.py
---

# Backend ↔ AI service — socket events

## In plain words

The AI service and the backend talk over one live connection that the AI
service opens. The backend sends events when something happens in a case: a
case is created, documents change, a lawyer asks a question, presses Stop, or
deletes the case. The AI service does the work and sends results back, one
panel at a time, plus chat answers. Each result carries what it cost. The
connection is protected by a signature that changes on every connection
attempt, so a captured login cannot be replayed. If the connection drops, the
AI service keeps results it could not deliver and sends them again when it
reconnects. Many backend messages may arrive twice. The AI service is built to
ignore the repeat rather than do the work again.

## How we connect

- **Who connects.** The AI service is a Socket.IO client. It connects out to
  the backend's socket server (`SOCKET_URL`, `SOCKET_PATH`,
  `SOCKET_NAMESPACE`) as the client type `AI_Maverick`. The backend sends to
  it in the `ai_service` room.
- **Handshake (scheme v1).** Every connection attempt sends four headers:
  - `x-client-type`: `AI_Maverick`;
  - `x-signature-timestamp`: UTC Unix seconds, digits only;
  - `x-signature-nonce`: 32 lowercase hex characters (16 random bytes);
  - `x-signature`: HMAC-SHA256 with `SOCKET_SECRET_KEY` over
    `v1:AI_Maverick:<timestamp>:<nonce>`, encoded as unpadded base64url
    (43 characters).
- **Headers are single-use.** The backend refuses a nonce it has seen and a
  timestamp more than 300 s from its clock. `SocketClient.connect` passes the
  header builder as a callable, so python-socketio re-signs on every attempt,
  including automatic reconnects. Passing a dict would make every reconnect
  fail with `replayed_nonce`.
- **Rollback only.** `SOCKET_AUTH_SCHEME=legacy` sends the old fixed
  signature. It works only while the backend's legacy window is open.
- **Startup checks.** The service refuses to start without `SOCKET_URL` or
  `SOCKET_SECRET_KEY`, or with an unknown `SOCKET_AUTH_SCHEME`.
- **Reference.** Spec and refusal codes: `docs/AI_SERVICE_AUTH_MIGRATION.md`.
  Backend side of this contract:
  https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553.
  Decision: DR-0009 AI handshake HMAC v1,
  https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161659.

## Messages

Every inbound event is routed by `SocketEventHandler.handle`
(`app/socket/events.py`). Two exceptions are handled in `app/socket/client.py`
before the router: `ai_heartbeat_ping`, and `unauthorized`, which is logged
only. `userId` is always the case **owner**, also on shared cases. Unless a row
says otherwise, `aiModel` is not used.

### Backend → AI service

| Event | Direction | When | Key fields | What the receiver must do |
|---|---|---|---|---|
| `connected` | backend → AI | After a handshake is accepted | `message`, `userId`, `clientType` | Run recovery: re-send buffered panels, then replay cached panels and any unsent chat reply. |
| `unauthorized` | backend → AI | Authentication refused | refusal data | Log an error pointing at `SOCKET_SECRET_KEY`. A refused connect attempt is also logged with its timestamp and nonce, never the signature. |
| `workspace_created` | backend → AI | New case with evidence | `workspaceId`, `userId`, `title`, `workspaceRole`, `evidenceFiles[]`, `createdAt`, `structure`, `tone`, `description`, `aiModel` (optional) | Revoke any pending purge. Set the role and the case model. Process the evidence. Generate the full panel suite. Cancellable by Stop. |
| `workspace_sync` | backend → AI | Documents, role, instructions or sharing changed | `workspaceId`, `userId`, `evidenceFiles[]`, `deletedDocumentIds[]`, `workspaceRole`, `updatedAt`, `currentState`, `participants` (absent = not shared), `aiModel` | Revoke any pending purge. Apply the roster authoritatively. Skip a duplicate with the same fingerprint within 180 s. Reconcile documents and re-run the suite. |
| `workspace_version_restore` | backend → AI | Case rolled back to an earlier document version | as `workspace_sync`, plus `versionId` (required), `restoredDocumentIds[]` | Treat `evidenceFiles` as authoritative and reconcile the vector store to it. Dedupe on `versionId`. Apply the roster non-authoritatively. |
| `workspace_transferred` | backend → AI | Case moved to a new owner | `workspaceId`, `previousUserId`, `newUserId`, `reEmitFeatures[]`, `evidenceFiles[]` | Re-key owner state. Re-emit the listed features (today `drafting_assistant`). Revoke any pending purge. |
| `document_reload_request` | backend → AI | Lawyer reloads one document, or it scored poorly | `requestId`, `workspaceId`, `userId`, `targetFile`, `evidenceFiles[]`, `currentState` (mandatory), `reason` | Resolve the document id from the payload, never invent one. Purge, re-read and re-run the suite. Swallow a re-send while in flight or just completed (180 s). Re-run after a failure. |
| `workspace_stop_request` | backend → AI | Lawyer clicks Stop | `requestId`, `workspaceId`, `stoppedBy`, `receivedFeatures[]`, `pendingFeatures[]`, `stoppedAt` | **Never reply.** Bump the run epoch. Cancel the run's tasks. Drop buffered panels in memory and on disk. Ignore a stop older than newer work. Dedupe on `requestId` for 24 h. |
| `workspace_resume_request` | backend → AI | Lawyer clicks "Finish update" | `requestId`, `workspaceId`, `userId`, `reEmitFeatures[]`, `evidenceFiles[]`, `currentState` | Emit `workspace:refresh` naming exactly `reEmitFeatures`. Then emit one panel per declared feature, in product order, and nothing else. Dedupe on `requestId` like a reload. |
| `user_message_request` | backend → AI | Lawyer sends a chat message | `content`, `chatType`, `conversation_history[]`, `workspaceId`, `userId`, `chatId`, `timestamp`, `attachment`, `askedBy` (non-owner only), drafting fields (`actionableId`, `templateId`, …), `aiModel` | Answer with one `ai_response`. A `view` collaborator gets the answer but no case change. `aiModel` applies to this turn only. Chat is never cancelled by Stop. |
| `probing_prompts_request` | backend → AI | Frontend polls for suggestion chips | `workspaceId`, `userId`, `chatId`, `chatType`, `actionableId`, `viewType`, last messages (optional) | Reply with `probing_prompts_response`. Logged at DEBUG. |
| `analysis_report_feedback` | backend → AI | Lawyer rejects a Missing Information or Risk item | `workspaceId`, `userId`, `workspaceRole`, `section`, `rejectedValue`, `isRejected`, `timestamp` | Remember the rejection. Recalculate the score. Re-emit `analysis_report`. |
| `added_missing_information` | backend → AI | Lawyer supplies missing information | `workspaceId`, `userId`, `title`, `description`, `workspaceRole`, `timestamp` | Assess relevance. If related, adjust the score and its explanation. Re-emit `analysis_report`. |
| `missing_information_edited` | backend → AI | Lawyer rewords one Missing Information item | `requestId`, `workspaceId`, `userId`, `editedBy`, `itemId`, `newText`, `editedAt` | Store the edit (no model call). Re-emit the report. Swallow a re-sent `requestId`. Ignore an older `editedAt`. Gate on `editedBy`. |
| `applicable_law_relevance_request` | backend → AI | Lawyer asks whether a chosen law applies | `workspaceId`, `userId`, `requestId`, `law{act, section, …}` | Emit `document_feature_response` (`applicable_law_relevance`), then `applicable_law_relevance_response`. |
| `add_case_law` | backend → AI | Lawyer adds a precedent | `workspaceId`, `userId`, `citation` / `url` / `text` | Add only a source-verified case. Re-emit `case_laws`, unchanged if nothing verifies. |
| `case_law_document_request` | backend → AI | Lawyer uploads a judgment | `requestId`, `workspaceId`, `userId`, `documentUrl`, `documentName`, `mimeType` | Always reply `case_law_document_response` echoing `requestId`. On acceptance, also re-emit `case_laws`. |
| `add_relevant_party` / `edit_relevant_party` / `delete_relevant_party` | backend → AI | Lawyer edits the Relevant Parties panel | `workspaceId`, `userId`, `partyId` (edit, delete), party fields | Apply the change. Re-emit `relevant_parties`. |
| `analysis_docx_generate_request` | backend → AI | Export of the analysis to DOCX | `requestId`, `workspaceId`, `documentSource`, `contentHash`, `trigger` | Normalise the text. Reply `analysis_docx_generate_response`. |
| `template_sync` | backend → AI | Template library changed | `action` (uploaded, renamed, deleted, …), `userId`, `templates[]` | Update the user's template store. Emit `template_blueprint_proposal` for templates with no blueprint. |
| `template_sync_response` | backend → AI | Reply to `request_template_sync` | `userId`, `requestId`, `count`, `templates[]` | Fill the cold template store. Resolve the waiting request. |
| `request_blueprint_proposal` | backend → AI | "Regenerate Extra Info" on a template | `userId`, `templateId`, `fileUrl`, `fileHash` | Re-profile the template. Emit `template_blueprint_proposal`. User-scoped: cost is logged, not billed to a case. |
| `workspace_deleted` | backend → AI | Case deleted (compliance erasure) | `workspaceId`, `userId`, `deletedAt`, `purgeAfter` (absolute), `purgeAfterDays` (informational), `reason`, `requestId` | Schedule the purge at `purgeAfter`. Expect re-sends with the same `requestId` on every reconnect. Cancel the purge on any live traffic for the case. Retry a failed purge with backoff, then escalate. |
| `workspace_updated` | backend → AI | Legacy | `workspaceId` | Log and audit only. |
| `ai_heartbeat_ping` | backend → AI | Periodic keep-alive | `requestId`, `serverSentAt` | Reply `ai_heartbeat_pong` at once. |

### AI service → backend

| Event | Direction | When | Key fields | What the receiver must do |
|---|---|---|---|---|
| `document_feature_response` | AI → backend | One panel is ready, re-sent, or released | `featureType`, `workspaceId`, `userId`, `payload`, `metadata` (cost block, `messageId`, `aiModel`) | Store the panel. Clear that panel's loader. Record the cost under `metadata.messageId`. `featureType` is one of: `document_assessments`, `workspace_metadata`, `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`, `procedural_timeline`, `relevant_parties`, `case_laws`, `drafting_assistant`, `applicable_law_relevance`. |
| `document_feature_response` (`analysis_report`, after a reject) | AI → backend | Reply to `analysis_report_feedback` | the usual report, plus `rejectionFeedback`: `scoreChange` (applied) and `items[]` of `section`, `rejectedValue` (echoed), `isValidRejection`, `scoreIncrease`, `reason` (`RejectionFeedback` in `app/socket/schemas.py`) | Pass the field through to the frontend unchanged. Show `reason` beside the score so the lawyer sees why it did or did not move. It is present on this emit only; never store it on the report. |
| `workspace:refresh` | AI → backend | Before a regeneration starts (objective change, resume, …) | `workspaceId`, `featureTypes[]`, `reason`, `refreshId`, `timestamp`, `userId` | Arm one loader per listed `featureType`. Only a matching `document_feature_response` clears it. |
| `ai_response` | AI → backend | Chat answer | `chatId`, `workspaceId`, `userId`, `content`, `metadata`, `citations[]`, `suggestionChips[]`, drafting fields (`draftStatus`, `openQuestions`, `draftDocument`, `actionableId`) | Store and show the answer. Keep only citations whose `[n]` marker is in `content`. Record the cost. |
| `probing_prompts_response` | AI → backend | Reply to `probing_prompts_request` | `workspaceId`, `userId`, `chatId`, `chatType`, `actionableId`, `viewType`, `prompts[]`, `metadata` (only when tokens were spent) | Show the chips. |
| `applicable_law_relevance_response` | AI → backend | After the panel emit for the same request | `workspaceId`, `userId`, `law`, `requestId` | Match it to the request by `requestId`. |
| `analysis_docx_generate_response` | AI → backend | Reply to the DOCX request | `requestId`, normalised document | Build the DOCX. |
| `case_law_document_response` | AI → backend | Reply to an uploaded judgment | `requestId`, `workspaceId`, `userId`, `accepted`, `caseLaw`, `rejectionReason` | Close the request. Show the result or the rejection. |
| `template_blueprint_proposal` | AI → backend | A template needs a blueprint | proposal for one template | Show it on the approval screen. |
| `request_template_sync` | AI → backend | AI's template store is cold | `userId`, `requestId` | Reply `template_sync_response` with the full library and the same `requestId`. |
| `ai_heartbeat_pong` | AI → backend | Every ping | `requestId`, `serverSentAt` (echoed), `aiTimestamp`, `status`, `version`, `uptimeMs` | Mark the AI link alive. |

## Rules both sides must follow

1. **Fresh signature on every connect.** Headers are never reused. Clocks stay
   within 300 s (NTP).
2. **`userId` is the owner.** A collaborator appears only in `askedBy`,
   `editedBy`, `stoppedBy` or `participants`. The AI service never re-keys state
   by them.
3. **Re-sends are expected.** The AI service dedupes:
   - `workspace_deleted`, `workspace_stop_request`, `document_reload_request`,
     `workspace_resume_request` and `missing_information_edited` on
     `requestId`;
   - `workspace_version_restore` on `versionId`;
   - `workspace_sync` on a 180 s content fingerprint.

   Other events have no dedupe. A repeat runs again. `requestId`, when
   present, is also the correlation id on every log line for the event.
4. **Emit returns delivery.** `SocketClient.emit` never raises. It returns
   `True` only when the payload reached the socket. It waits up to
   `SOCKET_EMIT_WAIT_TIMEOUT_SECONDS` (default 15 s) for a reconnect first.
   `False` means dropped.
5. **Undelivered panels are kept.** A dropped `document_feature_response` is
   buffered per `(workspaceId, featureType)` and persisted, so it survives a
   restart. On reconnect, buffered envelopes go **first**. Then the last active
   case's cached panels are replayed, in product order, with a zero-cost
   metadata block. A stopped run's output is never buffered.
6. **Unsent chat replies are retried once.** One undelivered `ai_response` is
   kept and re-sent after the next `connected`.
7. **A lit loader always gets a terminal emit.** Each `featureType` named in
   `workspace:refresh` is followed by a `document_feature_response` of that
   type, even on failure or cancellation. `_release_refresh_loaders` sends the
   cached payload or a minimal fallback for any armed panel that produced
   nothing.
8. **Stop is a fence.** After a stop, work from the stopped run cannot emit
   `document_feature_response` or `workspace:refresh`. Those emits return
   `False`. `ai_response` is never fenced.
9. **Resume emits only what it declared.** No extra panel types.
10. **No personal data in logs.** Neither side logs payloads, names or
    signatures. The AI service logs event names, keys and counts.

## Changing this contract

- Add or change a field in `app/socket/schemas.py` first. Add an inbound
  event's `@self.sio.event` shim in `app/socket/client.py`, and register it in
  `_register_default_handlers` in `app/socket/events.py`.
- A payload change is called out in the PR and `CHANGELOG.md`, and the PR is
  labelled `needs-human-review`. The backend team is told before merge.
- New fields are additive and optional. The AI side tolerates unknown fields
  (`extra="ignore"` on most models).
- A new `featureType` needs cache and replay on reconnect, and loader pairing.
- `metadata` keys must exist in `AIResponseMetadata`.
  `_warn_unknown_metadata_keys` logs any that do not.
- Run `make e2e` (billed) before deploy when handlers or payloads change.

## Open questions with the other team

- `subscribe` / `unsubscribe` emits exist in `app/socket/client.py`, but no
  code calls them. Does the backend still expect them?
- `workspace_updated` is handled as legacy and does nothing. Can the backend
  confirm it no longer sends it?
- Reconnect replay covers only the most recently active case. Other cases rely
  on the persisted delivery buffer. Is that enough for the backend?
- Only one undelivered `ai_response` is kept. A second dropped reply before
  reconnect replaces the first.
- `emit_ai_response` re-raises a socket error from `sio.emit`, unlike `emit`.
  Every caller wraps it today.
- Could not verify from this repo: the backend's refusal handling, room names
  beyond `ai_service`, and how the backend persists `workspace:refresh`.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-27 | `rejectionFeedback` on the analysis report sent after a reject (additive) | SIM-163 |
