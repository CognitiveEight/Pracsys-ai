---
title: Evidence to analysis panels
type: flow
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, backend, ai-service]
other_sides:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161538
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4685857
related_features: [../features/analysis-panels.md, ../features/evidence-upload.md, ../features/drafting-and-templates.md, ../features/stop-and-finish-update.md]
related_specs: []
---

# Evidence to analysis panels

## In plain words

Once the case documents have been read, the AI service builds ten panels: the
document list, the case category, strategy and objective, the analysis report,
the list of dates, the applicable laws, upcoming court dates, the people
involved, related case law and suggested drafts. It builds them one at a time,
always in the same order, because later panels use what earlier ones found. Each
panel is sent as soon as it is ready. Before a rebuild, the service tells the app
which panels to show as "loading"; a panel stops loading only when its new
content arrives, so the service makes sure every loading panel always gets an
answer, even when something fails. It also keeps a copy of every panel. If the
connection drops, it re-sends anything that did not arrive and then replays the
saved copies.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Backend | Sends `workspace_created` or `workspace_sync` with the files | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161538) |
| 2 | AI service | Reads the files (see [Create a workspace and upload evidence](create-workspace-and-upload-evidence.md)) | That page |
| 3 | AI service | On a rebuild, sends `workspace:refresh` naming the panels to put in loading state | This page, step 1 |
| 4 | Lawyer app | Shows a loader on each named panel | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4685857) |
| 5 | AI service | Builds each panel in product order and sends one `document_feature_response` per panel | This page, steps 2-4 |
| 6 | Backend | Stores each panel and the cost block, forwards it to the app | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161538) |
| 7 | Lawyer app | Replaces the loader with the panel content | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4685857) |
| 8 | AI service | After a disconnect: re-sends undelivered panels, then replays cached panels | This page, step 6 |

## The AI service's steps in detail

1. **Arm the loaders (rebuilds only).** `_emit_workspace_refresh` sends
   `workspace:refresh` with `featureTypes`, a `reason` and a `refreshId`. A new
   upload on a case that already has panels names document assessments plus all
   eight generated panels; a deletion names the eight; a role change names
   strategy, analysis, list of dates, applicable laws and procedural timeline.
   A first-ever upload sends none.
2. **Run the pipeline under one lock.** `_schedule_strategy_analysis_pipeline`
   starts a tracked background task that holds the per-workspace pipeline lock for
   the whole sequence, so two triggers can never interleave their emits:
   - **strategy_objective**: one model call over all documents
     (`_generate_strategies_from_documents`), giving case summary, objective, next
     steps and chips;
   - **analysis_report**: generated from the documents, then tagged against the
     lawyer's curated `currentState` so kept items carry their old id;
   - **list_of_dates**, **applicable_laws**, **procedural_timeline**,
     **relevant_parties**, **case_laws**, **drafting_assistant**: run by
     `_run_list_of_dates_and_applicable_laws`. Each step traps its own error so one
     failure never skips the rest.
3. **Pass every panel through one chokepoint.** `_emit_document_feature` applies
   the lawyer's fact corrections, adds statute status and section titles to
   applicable laws, attaches verified citations to the five cited panels (dates,
   timeline, analysis, strategy, laws), builds the cost `metadata` block, and
   sends the envelope. It returns `True` only when the socket accepted it.
4. **Cache what was sent.** Each panel is kept per workspace (strategy, analysis,
   dates, laws, timeline, parties, case laws, drafts, plus the document manifest
   and category). These caches write through to Qdrant, so they survive a
   restart.
5. **Terminal states.** A failed generation never leaves a panel empty: analysis
   falls back to a minimal report, strategy to a fallback objective, case laws to
   the cached or empty list. Paths that armed loaders and cannot finish call
   `_release_refresh_loaders`, which sends the cached payload (or the feature's
   minimal fallback) for every armed panel that did not emit.
6. **Recover after a disconnect.** On reconnect, `_recover_interrupted_work`
   first re-sends every buffered undelivered envelope
   (`_flush_pending_feature_deliveries`), and only then `_do_reemit_after_reconnect`
   replays the cached panels in product order, under the pipeline lock. The order
   matters: the buffered envelope is the real new output; the cache may still hold
   the older snapshot. After a restart, the same buffer is read back from Qdrant
   the first time the workspace is touched (`_recover_workspace_after_hydrate`).

**Variants on `workspace_sync`.**
- **Documents deleted:** vectors deleted, manifest pruned, removed documents
  recorded as gaps; strategy and analysis are regenerated inline, the analysis
  report replaces the old one, and the tail runs with the deleted ids so dated
  panels drop their entries and case laws / applicable laws are replaced, not
  merged.
- **Role changed, no file change:** strategy and analysis regenerated for the
  new role; dates and timeline re-sent from cache; applicable laws regenerated.
- **Every file removed (`evidenceFiles: []`):** the workspace's vectors are
  cleared and fallback strategy, analysis and empty dates are sent.

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 2 | One panel's model call fails | That panel shows fallback or cached content | Step traps the error; later panels still run |
| 2 | Two syncs arrive close together | Panels update once, in order | Second pipeline waits for the lock |
| 3 | Socket is down when a panel is ready | Loader keeps spinning until reconnect | Envelope buffered per workspace and feature, persisted, re-sent first on reconnect |
| 3 | Lawyer pressed Stop | No stale panel arrives after the stop (the app's own stopped state is on its page) | Emits from the stopped run are fenced and not buffered; the AI service sends no reply to a stop |
| 5 | A path armed loaders then failed | Panel returns to its previous content | `_release_refresh_loaders` sends the cached or fallback state |
| 6 | Service restarted mid-run | Real output appears after the next event on the case | Persisted buffer is read on hydration and re-sent |

## For engineers

**Entry and exit.** Entry: the schedulers in `app/socket/handlers/objective_change.py`
(`_schedule_strategy_analysis_pipeline`, `_schedule_list_of_dates_and_applicable_laws`,
`_schedule_applicable_laws_only`, `_emit_analysis_report`). Exit:
`_emit_document_feature` in `app/socket/handlers/chat.py`.

**Events and payloads.** `document_feature_response`
(`featureType`, `workspaceId`, `userId`, `payload`, `metadata`) and
`workspace:refresh` (`workspaceId`, `featureTypes`, `reason`, `refreshId`,
`timestamp`, `userId`). Contract:
[Backend and AI service socket events](../contracts/backend-ai-socket-events.md).
Panel generators: `app/socket/handlers/strategy.py`, `analysis_report.py`,
`list_of_dates.py`, `applicable_laws.py`, `procedural_timeline.py`,
`relevant_parties.py`, `case_laws.py`, `drafting.py`.

**State and timing rules.**
- Detached work goes through `_track_background_task` (`app/socket/handlers/_shared.py`),
  which also ties it to the run so a stop can cancel it.
- `SocketClient.emit` (`app/socket/client.py`) never raises and waits briefly for a
  reconnect before returning `False`.
- Undelivered buffer: `CacheKinds.PENDING_FEATURE_DELIVERY`, one envelope per
  `(workspace, featureType)`, cleared when a fresher one is delivered.
- Citations: `app/rag/attribution.py`; statute status: `app/statutes/register.py`.

**Security checks.** The `result <featureType> delivered=... items=...` log line
carries counts only, never content. Emits go through `self._socket_client`, which
is the stop-fenced wrapper (`_StopFencedSocket` in `app/socket/handlers/run_control.py`).

**Tests.** `tests/offline/verify_run_control.py` (fence, buffer, restart
recovery, resume order), `tests/offline/verify_objective_change.py` group 12
(buffer and loader release), `tests/offline/verify_rag.py` (panel citations),
`tests/test_statutes.py`, `tests/test_usage_ledger.py`. The full pipeline is
exercised only by `make e2e`.

**Known gaps.**
- The reconnect replay task itself is a bare `asyncio.create_task`, not tracked.
- Reconnect replay covers one workspace: the last one that armed
  `_pending_reemit_workspace_id`.
- On deletion, strategy and analysis run inline in the event handler and outside
  the pipeline lock; only the tail is locked.
- The "no new documents, replay from cache" branch of `on_workspace_sync` is
  effectively unreachable because already-indexed files are filtered out first; a
  no-change re-sync simply does nothing.
- The role-change path arms five loaders; whether `_generate_and_emit_applicable_laws`
  emits a terminal state on every failure was not traced end to end.

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
