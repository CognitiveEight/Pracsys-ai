---
title: Stop fences a run by epoch instead of relying on cancellation
type: decision
repo: law-firm-simplertoday-ai
owner: AI team
status: accepted
audience: everyone; engineers below "What this means"
last_verified: 2026-10-21
jira: SIM-113
number: DR-AI-0004
date: 2026-09-30
decided_by: AI team lead (role)
---

# DR-AI-0004 Stop fences a run by epoch instead of relying on cancellation

## In plain words

A case update runs about ten panels and can take minutes. Lawyers can press
Stop, and later Finish update. Stopping work that is already running is not
reliable: a request to an AI model may be half way through, and some work,
such as the answer to a chat question, must never be thrown away. So we
decided that Stop does two things. It cancels what it can, to save cost. And it
puts up a fence: anything that belonged to the stopped run, even if it keeps
running, can no longer send results to the lawyer or switch loading spinners
back on. Work that starts after the Stop is on the right side of the fence and
is untouched. Correct behaviour never depends on the cancel actually landing.

## Why we needed to decide

- The backend already discards responses that arrive after a stop, but the AI
  service kept paying for, and trying to deliver, work nobody would see.
- A cancelled task can be mid-await in a model call and finish anyway; detached
  tasks are easy to miss; and `asyncio.CancelledError` slips past every
  `except Exception`.
- A stale `workspace:refresh` from a stopped run would re-arm the frontend's
  spinners, which only a matching panel response can clear.
- Chat turns must not be killed by a stop.

## Options we considered

| Option | Pros | Cons |
|---|---|---|
| Cancel the run's tasks and trust that | Simple | Misses tasks in flight or not registered; late results still escape; no protection for spinners |
| Track a "stopped" flag per workspace | Easy to check | Also blocks the new run that starts after the stop; needs an id from the backend to tell runs apart |
| Run scope `(workspace, epoch)` in a context variable; stop bumps the epoch and the socket suppresses emits from older epochs; cancel as best effort | Causal, needs no run id from the backend; covers every task spawned by the run; new runs unaffected | Every emit must go through the fenced socket; detached work must be tracked |

## What we decided and why

The run-scope fence, in `app/socket/handlers/run_control.py`:

- Every workspace-scoped inbound event runs in a `contextvars` run scope
  `(workspace_id, epoch)`, set by `_dispatch_in_run_scope` from
  `SocketEventHandler.handle`. `create_task` copies the scope, so detached work
  carries it.
- A stop bumps the epoch. `_socket_client` is a property that wraps the client
  in `_StopFencedSocket`, which suppresses `document_feature_response` and
  `workspace:refresh` from an older epoch and returns `False` (not delivered).
- An older epoch cannot enter the pending-delivery buffer. An objective change
  caught by a stop is written as held (`heldByStop`), never auto-resumed;
  only a resume that includes `strategy_objective` replays it, and a new full
  run (`SUPERSEDING_RUN_EVENTS`) drops it.
- Cancellation is best effort, limited to `CANCELLABLE_RUN_EVENTS`
  (created, sync, version restore, document reload, resume). Chat is not in
  that set: chat turns are fenced, not cancelled.
- The stop is fire-and-forget (no reply). Re-sends under the same `requestId`
  are swallowed; a stop whose `stoppedAt` predates newer work is ignored.
- A resume emits `workspace:refresh` for exactly `reEmitFeatures`, then one
  panel response per feature in product order, and nothing else.

## What this means

- Spawn detached work through `_track_background_task`
  (`app/socket/handlers/_shared.py`); a task spawned by a stopped run is
  cancelled at birth.
- Always emit through `self._socket_client`; a raw client reference bypasses
  the fence.
- Never add a chat path to `CANCELLABLE_RUN_EVENTS`.
- Catch `asyncio.CancelledError` before `except Exception` in long generation,
  log it, and re-raise; release any armed loader on every exit path.
- Guarded by `tests/offline/verify_run_control.py`.
- Feature view: [Stop and finish update](../features/stop-and-finish-update.md).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-09-30 | Stop and Finish update with the run fence | 1d2923d |
| 2026-10-21 | First version, written from the code | SIM-113 |
