# 0003: Socket.IO client with stop-as-fence run scopes

- **Date:** not recorded
- **Jira:** none recorded

## Context

The AI service sits behind the application backend. The backend owns users,
documents and the frontend connection; the AI service receives workspace and
chat events and returns panel results and chat replies. Panel generation is
long (the strategy regeneration takes about two minutes) and runs in
background tasks, so chat is never blocked. A lawyer can press Stop on a
running update, and can later "Finish update".

Cancelling asyncio tasks is not enough to make Stop correct. A cancel can land
late or not at all, detached work can outlive the handler that spawned it,
and a stale `workspace:refresh` or `document_feature_response` from the
stopped run would re-arm or overwrite the frontend's panels.

## Decision

- The service is a Socket.IO **client** with no HTTP API. It connects out to
  the backend (`app/socket/client.py`), and a single `SocketEventHandler`
  (`app/socket/events.py`, composed of per-feature mixins in
  `app/socket/handlers/`) dispatches every inbound event.
- Every workspace-scoped inbound event runs in a `contextvars` run scope
  `(workspace_id, epoch)`, set by `_dispatch_in_run_scope` from
  `SocketEventHandler.handle`. Tasks created inside inherit it.
- `workspace_stop_request` bumps the epoch. Work running under an older epoch:
  - cannot emit `document_feature_response` or `workspace:refresh`, because
    `_socket_client` is a property that wraps the client in
    `_StopFencedSocket`; the suppressed emit returns `False`;
  - cannot enter the pending-delivery buffer;
  - writes an objective-change marker as held (`heldByStop`), not resumable.
- The stop also cancels what it can, but correctness does not depend on the
  cancel landing. A stop is fire-and-forget: the service never replies to it.
- Chat turns are fenced, never cancelled. Chat events are not in
  `CANCELLABLE_RUN_EVENTS`.
- A resume emits exactly the features it declared in `reEmitFeatures`.

## Consequences

- New detached work must be spawned through `_track_background_task`, so a
  stop can cancel it; a task spawned by an already-stopped run is cancelled
  at birth.
- Emits must go through `self._socket_client`. Holding the raw client
  bypasses the fence.
- A run started after the stop captures the new epoch and is unaffected.
- Because there is no completion event, every armed `workspace:refresh`
  loader still needs a terminal emit on every exit path (see `CLAUDE.md`,
  "A lit loader must always be paired with a terminal emit").
- The backend re-sends several events (for example after a reconnect), so
  handlers treat duplicate `requestId`s as expected. Emits that were not
  delivered are buffered and re-sent on reconnect.

## Status

Accepted (documented retroactively).

## Sources

- `CLAUDE.md`, "What this service is", "Stop is a fence, not a
  cancellation", "Async scheduling and reconnect-safe caches", and the
  `workspace_stop_request` / `workspace_resume_request` entries under
  "Inbound socket events".
- `docs/SOCKET_EVENTS_ARCHITECTURE.md` (file map, `run_control.py` row).
- `app/socket/handlers/run_control.py` (`_StopFencedSocket`,
  `_dispatch_in_run_scope`, `CANCELLABLE_RUN_EVENTS`,
  `SUPERSEDING_RUN_EVENTS`).
- `tests/offline/verify_run_control.py`.
