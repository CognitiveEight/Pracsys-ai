---
title: Stop and finish update
type: feature
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Evidence to analysis panels
other_side:
  backend (inside Analysis panels): https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5013543
related_specs: []
---

# Stop and finish update

## In plain words

Updating a case can take several minutes, because the AI rebuilds up to ten
panels. The lawyer can press Stop part-way through. The AI then stops the work
it can stop, and makes sure nothing from the stopped update can still appear
later, even work that was already running when Stop arrived. Panels already
delivered stay as they are. Later, the lawyer can press Finish update. The AI
then produces only the panels that never arrived, in the usual order, and
nothing else. Stop never cuts off a chat answer: if the lawyer asked a question,
they still get the reply. If the stopped update included a change of objective,
that change is kept aside and applied when the lawyer finishes the update.

## Who uses it

- Lawyers who stop a long update, for example to add another document first.
- The backend, which sends the stop and resume requests and tracks which panels
  arrived.

## What the user sees

After Stop, spinners stop and no more panels change. After Finish update, a
spinner appears only for each missing panel, and each one fills in. See the
backend page in `other_side`, under Analysis panels.

## How it works

1. **Every workspace event runs in a run scope.** `_dispatch_in_run_scope` sets
   a context value `(workspace_id, epoch)` when the event arrives. Tasks the
   handler spawns inherit it.
2. **Stop is a fence.** On `workspace_stop_request` the service:
   1. Ignores a duplicate `requestId` (remembered for 24 hours), and ignores a
      stale stop whose `stoppedAt` is more than 30 seconds older than newer
      work on the case. Applying a stale stop would stop the new run.
   2. Bumps the case's epoch. From now on, any `document_feature_response` or
      `workspace:refresh` from an older epoch is dropped at the socket and
      reported as not delivered.
   3. Cancels the case's registered run tasks and waits up to 10 seconds for
      them to unwind.
   4. Clears the buffered undelivered panels, in memory and in the persisted
      copy, and disarms the restart-recovery path for the case.
   5. Holds an in-progress objective change (see below), and marks any
      in-flight reload or resume as `stopped` so it can be re-sent.
   6. Writes an audit record. It never replies.
3. **Which events a stop cancels.** `workspace_created`, `workspace_sync`,
   `workspace_version_restore`, `document_reload_request` and
   `workspace_resume_request`. Chat (`user_message_request`) is fenced, not
   cancelled: the answer (`ai_response`) is never fenced, only the panels a
   chat turn would update.
4. **Held objective change.** If an objective change was in progress and
   `strategy_objective` had not arrived, the instruction is marked
   `heldByStop`. It is not auto-resumed on reconnect or restart. Only a resume
   that includes `strategy_objective`, within 24 hours, replays it. A new full
   run (create, sync, restore, reload) drops it.
5. **Finish update.** `workspace_resume_request` names the missing panels in
   `reEmitFeatures`. The service:
   1. Revokes a pending deletion and applies the roster (absent means
      un-shared, as on sync).
   2. Skips a re-send of the same `requestId` while in flight (up to 30
      minutes) or within 180 seconds of completion. A re-send after a failure
      or a stop runs again.
   3. Keeps only known feature types, in product order. Unknown ones are
      dropped from both the refresh and the run.
   4. Emits `workspace:refresh` declaring exactly those types.
   5. Reconciles `evidenceFiles` against the vector store, conservatively (no
      deletions if any active file lacks an id).
   6. Produces each panel in order under the case's pipeline lock.
   7. Releases every declared loader that did not get a panel.

## Rules and limits

- Product order: `document_assessments`, `workspace_metadata`,
  `strategy_objective`, `analysis_report`, `list_of_dates`,
  `applicable_laws`, `procedural_timeline`, `relevant_parties`, `case_laws`,
  `drafting_assistant`.
- A resume emits only what it declared. Anything else would show as a stray
  panel update.
- Correctness never depends on cancellation landing; the fence holds even for
  a task that ignores the cancel.
- A stop does not revoke a pending workspace deletion; it is not live traffic.
- A task spawned by an already-stopped run is cancelled at birth.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| A task ignores the cancel | Nothing; no stray panel appears | Its emits are dropped by the fence; logs that it did not unwind within 10 s |
| Stop re-sent after a reconnect | Nothing | Ignored as a duplicate or as stale |
| Malformed stop payload with a workspace id | Update stops | Applied with defaults; a stop is never refused |
| Resume names an unknown panel | No spinner for it | Dropped from the refresh and the run; logged |
| A resumed panel fails | That panel settles on its previous content | Loader released with the cached payload or an empty one |
| Resume is stopped again | Spinners stop | Marked `stopped`; a later re-send runs |
| Held objective older than 24 h | Strategy finishes under the current objective | Held change discarded and logged |

## For engineers

### Where the code is

- `app/socket/handlers/run_control.py` — run scopes, `_StopFencedSocket`,
  task registry, `on_workspace_stop_request`, `on_workspace_resume_request`,
  `_reconcile_resume_evidence`, `_produce_resumed_feature`.
- `app/socket/handlers/_shared.py` — `_release_refresh_loaders`,
  `_track_background_task`.
- `app/socket/events.py` — `SocketEventHandler.handle` calls
  `_dispatch_in_run_scope`.

### Events

Inbound: `workspace_stop_request` (fire-and-forget),
`workspace_resume_request`. Outbound on resume: `workspace:refresh`, then one
`document_feature_response` per declared panel. See
[the socket contract](../contracts/backend-ai-socket-events.md).
Why it works this way: [DR-AI-0004](../decisions/DR-AI-0004-stop-is-a-fence.md).

### Data

- In memory: `_run_epochs`, `_workspace_run_tasks`, `_stop_requests`,
  `_resume_requests`, `_last_workspace_event_at`.
- Persisted: the pending-delivery buffer
  (`CacheKinds.PENDING_FEATURE_DELIVERY`) and the objective-change marker
  (`CacheKinds.PENDING_OBJECTIVE_CHANGE`).

### Security

- `stoppedBy.name` is personal data and is never logged; only the user id is.
- Applied stops, ignored stale stops and completed resumes write audit events (`workspace_run_stopped`,
  `workspace_run_resumed`, `workspace_run_stop_ignored`).

### Rules for new code

- Spawn detached work through `_track_background_task`.
- Emit through `self._socket_client`; a raw client reference bypasses the
  fence.
- Never add a chat path to `CANCELLABLE_RUN_EVENTS`.

### Tests that guard it

- `tests/offline/verify_run_control.py` — schema, fence, cancel, buffers,
  held objective, resume order, loader release, purge revocation.
- `tests/offline/verify_objective_change.py` — loader release and cancellation
  on the objective-change path.

### Known gaps

- Epochs live in memory. After a restart they start at zero again; this is
  safe only because no task from before the restart survives it.
- Some detached work still uses a bare `asyncio.create_task` (for example the
  `workspace_transferred` re-emit), so a stop cannot cancel it. Its panel
  emits are still fenced when it runs in a stopped scope.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
