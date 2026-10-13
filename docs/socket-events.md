# Socket event catalog

Every Socket.IO event this service receives or sends, with its payload shape,
idempotency key and handler. Shapes only: field names and types, never
example values. `?` marks an optional field. Types come from the Pydantic
models in `app/socket/schemas.py` (and `app/socket/backend_events.py`), or
from the handler code where no model exists; the "Source" column says which.

The service is a Socket.IO **client**. "In" means backend → AI service; "Out"
means AI service → backend. Rules for handlers are in `CLAUDE.md`
("Architecture", "Stop is a fence", "A lit loader must always be paired with a
terminal emit") and in `docs/SOCKET_EVENTS_ARCHITECTURE.md`.

## How events reach a handler

- `app/socket/client.py` binds each event with `@self.sio.event` (or
  `@self.sio.on(...)`) and forwards it to `event_handler.handle(name, data)`.
- `SocketEventHandler.handle` (`app/socket/events.py`) looks the name up in
  the table filled by `_register_default_handlers`, sets the logging context
  (`request_id` = the payload's `requestId`, or a generated id), applies
  `aiModel`, and runs the handler inside its run scope
  (`_dispatch_in_run_scope`).
- An event with no `@self.sio.event` binding falls into the `*` catch-all in
  `client.py` and is dropped (logged at DEBUG, name only).

Cross-cutting fields read from the raw payload, outside the Pydantic models:

| Field | Read on | Effect |
|---|---|---|
| `aiModel: str \| null` | `workspace_created`, `workspace_sync`, `workspace_version_restore` | Sets the case's model. Absent = unchanged; `null` or `""` = default. `app/workspace/model_selection.py` |
| `aiModel: str` | `user_message_request` | Overrides the model for that chat turn only |
| `customInstructions: object` | `workspace_created`, `workspace_sync`, `workspace_version_restore` | Case-level output instructions (`app/workspace/custom_instructions.py`) |

Purge revocation: `workspace_created`, `workspace_sync`,
`workspace_version_restore`, `workspace_transferred`,
`document_reload_request`, `workspace_resume_request` and
`missing_information_edited` call `cancel_pending_purge`.

Stop: `workspace_created`, `workspace_sync`, `workspace_version_restore`,
`document_reload_request` and `workspace_resume_request` are cancellable runs
(`CANCELLABLE_RUN_EVENTS` in `app/socket/handlers/run_control.py`). Every other
workspace-scoped event is fenced, not cancelled.

## Shared shapes

**EvidenceFile** (`EvidenceFilePayload`): `originalName: str`,
`storeFileName: str`, `filename: str`, `extension: str`, `fileHash: str`,
`url: str`, `path: str`, `uploadedAt: str`, `id?: str`, `isDeleted?: bool`.
`filename` carries no extension; `extension` is the type signal.

**CurrentState** (`WorkspaceCurrentState`), every key optional:
`missingInfo`, `risks`, `weaknesses`, `relevantJurisdiction`:
`[{id: str, text: str, status?: str}]`; `relevantParties`:
`{approved: [{id, name}], pending: [{id, name}]}`; `listOfDates`:
`{approved: [{id, date, title}], pending: [{id, date, title}]}`;
`applicableLaws`: `[{act: str, section?: str}]`.

**Participant** (`WorkspaceParticipant`): `userId: str`, `name?: str`,
`permission?: "edit" | "view"`.

**Usage metadata** (`AIResponseMetadata`), every key optional: `messageId`,
`aiModel`, per-response `inputTokens`, `outputTokens`, `totalTokens`,
`inputPrice`, `outputPrice`, `price`, `ocrUnits`, `ocrPrice`, `sttSeconds`,
`sttPrice`, `searchQueries`, `searchPrice`, `groundingRequests`,
`groundingPrice`, `embedTokens`, `embedPrice`, `servicePrice`; workspace
running totals `totalInputTokens` … `totalServicePrice` (same names with a
`total` prefix); chat only: `tokensUsed`, `responseTime`, `error`,
`confidence`, `detectedIntent`.

## Inbound events

### Connection

| Event | Payload | Handler | Notes |
|---|---|---|---|
| `connect` | none | `on_connect`, `handlers/workspace_events.py` | Socket.IO built-in. Runs the reconnect replay (`_do_reemit_after_reconnect`) |
| `connected` | `message: str`, `userId: str`, `clientType: str` (`ConnectedPayload`, `backend_events.py`) | `on_connected`, `handlers/workspace_events.py` | Backend confirms the handshake. `client.py` logs an error if `clientType` is not this client. Also runs the reconnect replay and re-sends a pending `ai_response` |
| `disconnect` | `{reason?: str}` (built by `client.py`) | `on_disconnect`, `handlers/workspace_events.py` | Socket.IO built-in |
| `connect_error`, `error` | untyped | `on_error` (registered as `error`) | `connect_error` also logs the handshake refusal hints |
| `unauthorized` | untyped | inline in `client.py` | Logs only; not dispatched |
| `ai_heartbeat_ping` | `requestId?: str`, `serverSentAt?: str`, `source?: str` (`AIHeartbeatPingPayload`) | inline in `client.py` | Answered with `ai_heartbeat_pong`. Not dispatched through `event_handler` |

### Workspace lifecycle

| Event | Payload (source) | Idempotency | Handler | Notes |
|---|---|---|---|---|
| `workspace_created` | `workspaceId: str`, `userId: str`, `title: str`, `workspaceRole: str`, `evidenceFiles: [EvidenceFile]`, `createdAt: str`, `structure?: str`, `tone?: str`, `description?: str` (`WorkspaceCreatedPayload`) | none found | `on_workspace_created`, `handlers/workspace_events.py` | Role gate first, then ingestion and the full feature suite |
| `workspace_sync` | `workspaceId: str`, `userId: str`, `updatedAt: str`, `title?`, `status?`, `workspaceRole?`, `structure?`, `tone?`, `description?: str`, `evidenceFiles?: [EvidenceFile]`, `deletedDocumentIds?: [str]`, `currentState?: CurrentState`, `participants?: [Participant]` (`WorkspaceSyncPayload`) | Fingerprint of ids, files, role, title, status and a hash of `currentState`; replay inside 180 s is skipped | `on_workspace_sync`, `handlers/workspace_events.py` | `participants` absent (not `[]`) means the case is not shared and clears the roster |
| `workspace_version_restore` | `workspace_sync` fields plus `versionId: str`, `restoredDocumentIds: [str]` (`WorkspaceVersionRestorePayload`) | Fingerprint keyed on `versionId`, own 180 s window | `on_workspace_version_restore`, `handlers/workspace_events.py` | `evidenceFiles` is authoritative; `participants` is applied non-authoritatively |
| `workspace_transferred` | `workspaceId: str`, `previousUserId: str`, `newUserId: str`, `newUser?: {id: str, name?: str, email?: str}`, `title?`, `workspaceRole?`, `status?: str`, `evidenceFiles: [EvidenceFile]`, `reEmitFeatures: [str]`, `transferredAt?: str` (`WorkspaceTransferredPayload`) | Fingerprint of ids and `transferredAt`, 180 s window | `on_workspace_transferred`, `handlers/workspace_events.py` | Re-keys owner state; re-emits `reEmitFeatures` |
| `document_reload_request` | `requestId: str`, `workspaceId: str`, `userId: str`, `targetFile: EvidenceFile + {previousQualityScore?: float}`, `title?`, `status?`, `workspaceRole?`, `reason?`, `timestamp?: str`, `evidenceFiles?: [EvidenceFile]`, `currentState?: CurrentState`, `participants?: [Participant]` (`DocumentReloadRequestPayload`) | `requestId`: swallowed while in flight or just completed; re-runs after a failed attempt | `on_document_reload_request`, `handlers/document_reload.py` | `currentState` is mandatory in practice (CLAUDE.md). An id-less reload is abandoned |
| `workspace_stop_request` | `workspaceId: str`, `requestId?: str`, `userId?: str`, `stoppedBy?: {userId: str, name?: str}`, `receivedFeatures: [str]`, `pendingFeatures?: [str]`, `stoppedAt?: str`, `timestamp?: str` (`WorkspaceStopRequestPayload`) | `requestId`; a stop whose `stoppedAt` predates newer work is stale and ignored | `on_workspace_stop_request`, `handlers/run_control.py` | **Fire-and-forget: never replied to.** Bumps the run epoch |
| `workspace_resume_request` | `workspaceId: str`, `userId: str`, `requestId?: str`, `reEmitFeatures: [str]`, `title?`, `status?`, `workspaceRole?`, `timestamp?: str`, `evidenceFiles?: [EvidenceFile]`, `currentState?: CurrentState`, `participants?: [Participant]` (`WorkspaceResumeRequestPayload`) | `requestId`, as `document_reload_request` | `on_workspace_resume_request`, `handlers/run_control.py` | Emits `workspace:refresh` naming exactly `reEmitFeatures`, then one `document_feature_response` per feature, nothing else |
| `workspace_deleted` | `workspaceId: str`, `userId: str`, `deletedAt: str`, `purgeAfter: str`, `purgeAfterDays?: int`, `reason?: str`, `requestId?: str`, `event?: str`; extra keys allowed (`WorkspaceDeletedPayload`) | `requestId`; re-sends are expected until `purgeAfter` | `on_workspace_deleted`, `handlers/workspace_purge.py` | Compliance erasure. `purgeAfter` is absolute. No reply |
| `workspace_updated` | reads `workspace_id` or `workspaceId` only | none | `on_workspace_updated`, `handlers/workspace_events.py` | Legacy. Logs and audits only |

### Chat

| Event | Payload (source) | Idempotency | Handler | Notes |
|---|---|---|---|---|
| `user_message_request` | `content: str`, `chat_type: str`, `timestamp: str`, `conversation_history: [{role: "USER" \| "AI", content: str}]`, `workspaceId?`, `userId?`, `chatId?: str`, `attachment?: {url: str, fileName?: str, mimeType?: str}`, `askedBy?: {userId: str, name?: str}`, `actionableId?`, `source?`, `suggestedDraftId?`, `suggestionTitle?`, `suggestionDescription?`, `templateId?`, `templateUrl?`, `templateOriginalName?`, `templateMimeType?`, `draftMode?: str`, `formatLayout?: any` (`UserMessageRequestPayload`; aliases accepted) | none | `on_user_message_request`, `handlers/chat.py` | Replies with `ai_response`. Fenced, never cancelled by a stop. `askedBy` present only for a non-owner |
| `probing_prompts_request` | reads `workspaceId`, `userId`, `chatId`, `chatType`, `viewType`, `actionableId`, `maxPrompts`, `timestamp`, `lastUserMessage`, `lastAiResponse`, `conversationHistory` (snake_case aliases accepted) | none; responses throttled per workspace | `on_probing_prompts_request`, `handlers/chat.py` | Polled by the frontend; logs at DEBUG. No Pydantic model |

### Panel feedback and edits

| Event | Payload (source) | Idempotency | Handler | Notes |
|---|---|---|---|---|
| `analysis_report_feedback` | `workspaceId: str`, `userId: str`, `workspaceRole: str`, `section: "missingInfo" \| "riskOrWeakness"`, `rejectedValue: str`, `isRejected: bool`, `timestamp: str` (`AnalysisReportFeedbackPayload`) | none; batched, already-removed items are treated as stale | `on_analysis_report_feedback`, `handlers/analysis_report.py` | Re-emits `analysis_report` |
| `added_missing_information` | `workspaceId: str`, `userId: str`, `title: str`, `description: str`, `workspaceRole?: str`, `timestamp: str` (`AddedMissingInformationPayload`) | none found | `on_added_missing_information`, `handlers/analysis_report.py` | Re-emits `analysis_report` |
| `missing_information_edited` | `workspaceId: str`, `itemId: str`, `newText: str`, `requestId?`, `userId?: str`, `editedBy?: {userId?: str}`, `previousText?`, `editedAt?`, `timestamp?: str` (`MissingInformationEditedPayload`) | `requestId`; an older `editedAt` than the applied one is stale | `on_missing_information_edited`, `handlers/analysis_report.py` | Text only, no model call. Gated on `editedBy` via `resolve_asker` |
| `applicable_law_relevance_request` | `workspaceId: str`, `userId: str`, `requestId?`, `timestamp?: str`, `law: {act: str, section: str, id?, title?, description?, userDescription?: str}` (`ApplicableLawRelevanceRequestPayload`) | none; `requestId` is echoed | `on_applicable_law_relevance_request`, `handlers/law_relevance.py` | Replies twice (see outbound) |
| `add_case_law` | `workspaceId: str`, `userId: str`, `workspaceRole?`, `citation?`, `url?`, `text?`, `timestamp?: str` (`AddCaseLawRequest`) | none found | `on_add_case_law`, `handlers/panel_edits.py` | Re-emits `case_laws` |
| `case_law_document_request` | `workspaceId: str`, `userId: str`, `documentUrl: str`, `requestId?`, `documentName?`, `mimeType?`, `workspaceRole?`, `timestamp?: str` (`CaseLawDocumentRequest`) | none; `requestId` is echoed | `on_case_law_document_request`, `handlers/case_laws.py` | Replies with `case_law_document_response`; accepted cases also re-emit `case_laws` |
| `add_relevant_party` | `workspaceId: str`, `userId: str`, `name: str`, `workspaceRole?`, `role?`, `details?`, `description?`, `contactNumber?`, `email?`, `officialIdUrl?`, `timestamp?: str` (`AddRelevantPartyRequest`) | none found | `on_add_relevant_party`, `handlers/panel_edits.py` | Re-emits `relevant_parties` |
| `edit_relevant_party` | `workspaceId: str`, `userId: str`, `partyId: str`, then the same optional fields as add (`EditRelevantPartyRequest`) | none found | `on_edit_relevant_party`, `handlers/panel_edits.py` | Re-emits `relevant_parties` |
| `delete_relevant_party` | `workspaceId: str`, `userId: str`, `partyId: str`, `timestamp?: str` (`DeleteRelevantPartyRequest`) | none found | `on_delete_relevant_party`, `handlers/panel_edits.py` | Re-emits `relevant_parties` |
| `analysis_docx_generate_request` | `requestId: str`, `workspaceId: str`, `documentSource: object`, `userId?`, `trigger?`, `contentHash?`, `timestamp?: str` (`AnalysisDocxGenerateRequestPayload`) | none; `requestId` is echoed | `on_analysis_docx_generate_request`, `handlers/docx_export.py` | Deterministic text processing, no model call |

### Drafting templates

| Event | Payload (source) | Idempotency | Handler | Notes |
|---|---|---|---|---|
| `template_sync` | `action: "uploaded" \| "deleted" \| "renamed" \| "preset_created" \| "formatting_updated"`, `userId: str`, `templates: [TemplateSyncItem]`, `timestamp?: str` (`TemplateSyncPayload`) | none found | `on_template_sync`, `handlers/template_sync.py` | Lawyer-scoped, not workspace-scoped. Uploads emit `template_blueprint_proposal` |
| `template_sync_response` | `userId: str`, `requestId?: str`, `count?: int`, `templates: [TemplateSyncItem]`, `timestamp?: str` (`TemplateSyncResponsePayload`) | `requestId` correlates with our `request_template_sync` | `on_template_sync_response`, `handlers/template_sync.py` | Reply to `request_template_sync` |
| `request_blueprint_proposal` | reads `userId`, `templateId`, `fileHash`, `fileUrl`, `timestamp` (comment in `backend_events.py`; no model) | none found | `on_request_blueprint_proposal`, `handlers/template_sync.py` | "Regenerate Extra Info". Replies with `template_blueprint_proposal` |

`TemplateSyncItem`: `id: str`, `name: str`, `category: str`,
`extension: str`, `previousName?`, `mimeType?: str`, `size?: int`,
`fileHash?`, `fileUrl?`, `path?`, `kind?: str`, `formatting?: {page?: object,
letterhead?: object, court?: object, blueprint?: Blueprint}`.

`Blueprint`: `context?`, `tone?`, `structure?`, `depth?`,
`legalGuardrails?: str`, `variables: [{id?, label?, question?, kind?: str,
fillFromCase?: bool, required?: bool}]`.

## Outbound events

All workspace-scoped emits go through `self._socket_client`, which is fenced
by a stop (`_StopFencedSocket`). `SocketClient.emit` returns `True` only on
delivery and never raises.

| Event | Payload (source) | Sent by | Notes |
|---|---|---|---|
| `document_feature_response` | `featureType: str`, `workspaceId: str`, `userId: str`, `payload: object`, `metadata: Usage metadata` (envelope built in `_emit_document_feature`, `handlers/chat.py`) | `_emit_document_feature` and a few direct emits in `workspace_events.py`, `document_reload.py`, `run_control.py`, `objective_change.py` | The only event that clears a panel loader. Undelivered envelopes are buffered per `(workspaceId, featureType)` and re-sent on reconnect. Ingestion failure sends `{featureType: "document_assessments", workspaceId, userId, error: str, metadata}` |
| `workspace:refresh` | `workspaceId: str`, `featureTypes: [str]`, `reason: str`, `refreshId: str`, `timestamp: str`, `userId?: str` (`_emit_workspace_refresh`, `handlers/objective_change.py`) | `_emit_workspace_refresh` | Arms one frontend loader per `featureTypes` entry. Each one needs a terminal `document_feature_response` |
| `ai_response` | `chatId: str`, `workspaceId: str`, `userId: str`, `content: str`, `metadata?: Usage metadata`, `suggestionChips?: [str]`, `citations?: [Citation]`, `actionableId?`, `chat_type?: str`, `isDraft?: bool`, `draftStatus?: str`, `openQuestions?: [{id: str, label: str}]`, `draftDocument?: DraftDocument` (`AIResponsePayload`) | `SocketClient.emit_ai_response`, from `handlers/chat.py` | Last payload kept in `_pending_ai_response` and re-sent after a reconnect |
| `probing_prompts_response` | `workspaceId`, `userId`, `actionableId`, `chatId`, `chatType`, `viewType` (echoed), `prompts: [str]`, `timestamp?` (echoed), `metadata?: Usage metadata` (built in `_handle_probing_prompts_request`, `handlers/chat.py`) | `handlers/chat.py` | Throttled per workspace (`PROBING_THROTTLE_SECONDS`) |
| `applicable_law_relevance_response` | `workspaceId: str`, `userId: str`, `law: object`, `requestId?: str` (`handlers/law_relevance.py`) | `handlers/law_relevance.py` | Sent after a `document_feature_response` with `featureType: "applicable_law_relevance"` carrying the same body |
| `case_law_document_response` | `workspaceId: str`, `userId: str`, `accepted: bool`, `requestId?: str`, `caseLaw?: {citation?, court?, summary?, referenceUrl?, referenceName?: str, relevance: "High" \| "Medium" \| "Low"}`, `rejectionReason?: str` (`CaseLawDocumentResponse`) | `handlers/case_laws.py` | Echoes `requestId` |
| `analysis_docx_generate_response` | `requestId: str`, `workspaceId: str`, `success: bool`, `documentPayload: object \| null`, `error: str \| null`, `timestamp: str` (`process_analysis_docx_request`, `app/langgraph/output_filters.py`) | `handlers/docx_export.py` | Echoes `requestId` |
| `template_blueprint_proposal` | `userId: str`, `templateId: str`, `fileHash: str`, `status: str`, `draftCategory: str`, `blueprint: Blueprint + {quickAdd}`, `timestamp: str` (`build_blueprint_proposal`, `app/langgraph/drafting_agent.py`) | `handlers/template_sync.py` | The lawyer's approval comes back as `template_sync` with `action: "formatting_updated"` |
| `request_template_sync` | `userId: str`, `requestId: str` (`handlers/template_sync.py`) | `handlers/template_sync.py` | Asks for the full template library; awaits `template_sync_response` with a timeout |
| `ai_heartbeat_pong` | `aiTimestamp: str`, `status: "ok"`, `version: str`, `uptimeMs: int`, `requestId?: str`, `serverSentAt?: str` (`AIHeartbeatPongPayload`) | `client.py` | Echoes the ping's correlation fields |

`featureType` values seen in the code: `document_assessments`,
`workspace_metadata`, `strategy_objective`, `analysis_report`,
`list_of_dates`, `applicable_laws`, `procedural_timeline`, `case_laws`,
`relevant_parties`, `drafting_assistant`, `applicable_law_relevance`.

`Citation`: `index: int`, `type: "document" | "case_law" | "statute" |
"web"`, then optional `documentId`, `annexure: int`, `fileName`, `page: int`,
`pageEnd: int`, `timestamp`, `heading`, `quote`, `url`, `title`, `sheet`,
`rowStart: int`, `rowEnd: int`, `computed: bool`.

`DraftDocument`: `title: str`, `format: "markdown"`, `content: str`,
`version: int`, `sourceTemplateId?`, `sourceTemplateName?`, `sourceType?:
str`, `wordCount?: int`, `downloadUrl?: str`.

## Not fully typed

These are listed above, but their shape is not pinned by a model:

| Item | Why |
|---|---|
| `document_feature_response.payload` per `featureType` | Each panel builds its own dict in its handler module. Only `drafting_assistant` (`DraftingSuggestionsPayload`), `case_laws` (`CaseLawsPayload`) and `relevant_parties` (`RelevantPartiesPayload`) have models; the others are defined by the generator code |
| `probing_prompts_request` | No model; fields read with `data.get` in `handlers/chat.py` |
| `request_blueprint_proposal` | No model; fields documented only in a comment in `backend_events.py` |
| `workspace_updated` | Legacy; only the workspace id is read |
| `analysis_docx_generate_request.documentSource`, `…_response.documentPayload` | Typed as a free-form object; structure is in `app/langgraph/output_filters.py` |
| `applicable_law_relevance_response.law` | Built by `handlers/law_relevance.py`; no model |
| `aiModel`, `customInstructions` | Read from the raw payload in `events.py` / `workspace_events.py`; not declared on the inbound models |
| `connect_error`, `error`, `unauthorized` | Payloads come from python-socketio or the server and are not inspected |

## Defined but not used

- `subscribe` / `unsubscribe` (`SocketClient.subscribe` / `unsubscribe`,
  `app/socket/client.py`) emit `{workspace_id: str}`, but nothing in `app/`
  calls them.
- `app/socket/backend_events.py` and `app/socket/schemas.py` also define
  models for backend-to-frontend events (`SendMessagePayload`,
  `TypingPayload`, `StrategiesGeneratedPayload` and others) that this service
  neither sends nor receives.

## Keeping this current

Update this file in the same PR as any change to an event or payload:

- New inbound event: the `@self.sio.event` binding in `client.py`, the
  `register(...)` line in `_register_default_handlers` (`events.py`), the
  model in `schemas.py`, and a row here.
- New outbound event or `featureType`: a row here, and a CHANGELOG entry
  calling out the payload change.
- Check completeness with:

  ```bash
  grep -n 'self.register("' app/socket/events.py
  grep -n '@self.sio' app/socket/client.py
  grep -rnoE 'emit\(\s*"[a-zA-Z_:]+"' app
  grep -rn '_socket_client.emit($' -A1 app/socket/handlers
  ```

Last checked against the code on 2026-10-13.
