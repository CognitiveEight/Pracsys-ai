# SIM-181: Strategy panel no longer appears twice in chat after a socket reconnect

- **Jira:** SIM-181
- **Owner:** product owner
- **Status:** approved
- **Target branch flow:** fix/SIM-181-reconnect-replay-duplicates → development → staging → main

## Problem
After the AI service's socket drops and reconnects, it replays the cached
panels of every case armed in the last 30 minutes, including panels the
backend already received. The frontend renders a re-sent `strategy_objective`
as a new chat message, so the lawyer sees an identical second Case Summary /
Strategic Objective / Possible next steps with a later timestamp. It looks as
if the AI re-ran or something changed.

## Goal
A reconnect never repeats a panel the backend already has. A panel that may
not have arrived is still re-sent.

## Scope
- In: skip the reconnect replay for a panel whose identical payload was
  delivered well before the disconnect.
- Out: the raw `document_assessments` / `workspace_metadata` replay (in-place
  updates); the pending-delivery buffer; frontend de-dup; the socket drop seen
  during the audio fallback (separate ticket).

## Users and surfaces
Reconnect replay (`_do_reemit_after_reconnect` → `_reemit_cached_features`),
`document_feature_response` for `strategy_objective`, `analysis_report`,
`list_of_dates`, `applicable_laws`, `procedural_timeline`, `relevant_parties`,
`case_laws`, `drafting_assistant`.

## Constraints
- [ ] Changes a socket payload or `featureType` — no
- [ ] Role-dependent behaviour — no
- [x] Touches the workspace purge — the new per-workspace record must be erased
- [ ] Adds a paid call — no
- [ ] Indian-law output — no
- [ ] Shared workspaces — no

## Success measure
After a reconnect, the service log shows `result <panel> replay skipped` for
delivered panels and the chat shows one strategy message per run.

## Open questions
None.
