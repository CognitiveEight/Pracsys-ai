---
title: Change the objective
type: flow
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, backend, ai-service]
other_sides:
  backend (socket contract): https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553
  lawyer app (AI chat): https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4653122
related_features: [../features/ai-chat.md, ../features/analysis-panels.md, ../features/stop-and-finish-update.md]
related_specs: []
---

# Change the objective

## In plain words

A lawyer can tell the AI, in chat, what they now want to achieve in the case,
for example a different remedy or forum. The AI writes a new strategic objective
and next steps, and rewrites the case summary to lead with the facts that matter
most for the new aim. It may reorder and re-emphasise, but never change the
facts: if the rewritten summary loses a figure, names someone or something not in
the documents, or drops its standard opening, it is thrown away and the old
summary stays. The new objective is kept either way. The AI then rebuilds the
analysis report and its score explanation, rescores the dates, and refreshes the
laws, case law and draft suggestions. Every panel it marked as loading is always
given an answer. If the change fails, the lawyer is told plainly that the old
objective still stands.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app | Lawyer states a new objective in chat (usually the Strategy & Objective chat) | [Lawyer app AI chat page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4653122) |
| 2 | Backend | Sends `user_message_request` | [Socket contract (backend)](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4456553) |
| 3 | AI service | Detects an objective change and sends `workspace:refresh` for six panels | This page, steps 1-2 |
| 4 | AI service | Regenerates objective, next steps and (re-framed) summaries; sends `strategy_objective` | This page, step 3 |
| 5 | AI service | Sends a short `ai_response` acknowledgement | This page, step 4 |
| 6 | AI service | Runs the cascade in the background and sends each panel | This page, step 5 |
| 7 | Backend, lawyer app | Store and show each panel; loaders clear as panels arrive | Other sides' pages |

## The AI service's steps in detail

1. **Detect.** Inside `on_user_message_request` (`app/socket/handlers/chat.py`),
   only when the asker may edit (`mutations_allowed`), the message is not a plain
   question (`_is_pure_informational_query`), and a case summary already exists.
   Routes into the change:
   - in the Strategy & Objective chat: an explicit objective marker, or
     `_llm_classify_objective_change_intent` returning `direct_update` or
     `ask_confirmation`;
   - in any chat: the panel-edit classifier flags an objective declaration;
   - "yes" after the AI offered to update the objective: the instruction is
     summarised from the thread;
   - after the chat agent answers in the Strategy & Objective chat with intent
     `STRATEGY_CHANGE`.
   A read-only collaborator gets a "not changed" notice instead.
2. **Arm loaders.** `workspace:refresh` with reason
   `objective_change_regeneration_started` and `OBJECTIVE_CHANGE_REFRESH_FEATURES`:
   strategy, analysis report, list of dates, applicable laws, procedural timeline,
   drafting suggestions.
3. **Regenerate the strategy** (`_regenerate_strategy_for_new_objective` in
   `app/socket/handlers/strategy.py`), awaited in the chat turn (about two
   minutes):
   - documents packed with `fit_to_budget`; an entity allow-list extracted;
     the lawyer's fact corrections and custom instructions added to the prompt;
   - up to two model attempts, each checked with `is_generation_error`;
   - re-framed `caseSummary` and `reportCaseSummary` accepted only if each keeps
     its opening ("This case is about" / "This matter is about"), keeps every
     figure (`find_dropped_case_figures`) and adds no new organisation, party or
     place (`find_ungrounded_proper_nouns`). If one fails, both previous texts
     stay;
   - an objective or next step naming an ungrounded entity gets one corrective
     re-prompt;
   - the result is cached and emitted as `strategy_objective`. It returns `True`
     only when delivered.
4. **Acknowledge.** On success the pending marker is cleared, reconnect replay
   is armed, and the chat reply says what changed. It names the case summary only
   if the text actually changed. On failure, `_release_refresh_loaders` settles
   all six loaders and the reply is `OBJECTIVE_CHANGE_FAILED_MESSAGE`.
5. **Cascade** (`_schedule_objective_change_reanalysis` →
   `_run_objective_change_incremental_refresh` in
   `app/socket/handlers/objective_change.py`), a tracked task under the pipeline
   lock:
   - **analysis report:** regenerated and replaced, not merged; if the
     score explanation is empty or unchanged it is regenerated with the new
     objective (`_ensure_quality_score_explanation(..., existing="")`); tagged
     against `currentState`;
   - **list of dates:** same entries, relevance re-scored for the new
     objective, else re-sent as is;
   - **applicable laws:** regenerated with the objective as the selection lens,
     replacing the old list;
   - **procedural timeline** and **relevant parties:** re-sent from cache;
   - **case laws:** regenerated for the objective, replacing the old set;
     cached set re-sent on failure;
   - **drafting suggestions:** regenerated; cached set re-sent on failure.
   A `finally` block releases every armed loader that did not emit.
6. **Two callers do not re-frame.** Role reassignment calls the same function with
   `refresh_case_summary=False`. A surgical wording edit to the objective goes
   through `_regenerate_strategic_objective_with_feedback`, which keeps both
   summaries byte-for-byte and runs no cascade.

**Interruptions.**
- **Socket drop during step 3:** a `CancelledError` records a resume marker
  (`CacheKinds.PENDING_OBJECTIVE_CHANGE`) and re-raises. On reconnect it is re-run
  once if under 15 minutes old, otherwise the loaders are released. If the payload
  was generated but not delivered, the buffered envelope is re-sent and only the
  cascade is scheduled.
- **Stop:** a change interrupted by `workspace_stop_request` is held
  (`heldByStop`), never auto-resumed. Only a `workspace_resume_request` that
  includes `strategy_objective` replays it (within 24 hours). A new full run
  (created, sync, version restore, reload) drops it.

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 1 | No case summary yet | Normal chat answer | Change not attempted |
| 3 | Model error, timeout or no JSON twice | "I could not update the strategic objective..." | Loaders released; `objective_change_failed` audited |
| 3 | Re-framed summary drops a figure or invents an entity | New objective, old summary; reply does not mention the summary | Candidate discarded |
| 3 | Socket drops mid-regeneration | Panels settle after reconnect | Marker re-run once on reconnect |
| 5 | One cascade step fails | That panel shows its cached or fallback content | Step falls back; `finally` releases the rest |
| 5 | Upload pipeline running at the same time | Panels update in order | Cascade waits for the pipeline lock |

## For engineers

**Entry and exit.** Entry: `on_user_message_request`. Exits:
`strategy_objective`, then cascade `document_feature_response` emits, and one
`ai_response`.

**Events and payloads.** No payload of its own; uses `user_message_request`,
`workspace:refresh`, `document_feature_response`, `ai_response`. Contract:
[Backend and AI service socket events](../contracts/backend-ai-socket-events.md).

**State and timing rules.** Constants in `app/socket/event_utils.py`.
Loader contract and stop fence: CLAUDE.md "A lit loader must always be paired
with a terminal emit" and "Stop is a fence". Resume and held changes:
`app/socket/handlers/run_control.py`.

**Security checks.** Objective text and model output are never logged; give-ups
log a reason code. Gated on `mutations_allowed`.

**Tests.** `tests/offline/verify_objective_change.py` groups 1-10 (re-frame
checks, reassignment and surgical-edit locks, honest ack), 11 (score
explanation), 12 (interruption recovery, loader release);
`tests/offline/verify_run_control.py` (`check_resume_objective`).

**Known gaps.**
- `docs/SYSTEM_ARCHITECTURE.md` says case laws and drafting are preserved on an
  objective change; the code regenerates both.
- The cascade also emits relevant parties and case laws, which the six-panel
  refresh does not arm.
- The chat acknowledgement mentions only the analysis report, dates and laws.

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
