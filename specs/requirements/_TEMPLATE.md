# ST-XXX: <title>

- **Jira:** ST-XXX
- **Owner:** <role, e.g. product owner>
- **Status:** draft | approved
- **Target branch flow:** feature/ST-XXX-… → development → staging → main

## Problem
What the lawyer (or the backend) cannot do today, and what it costs them.

## Goal
The outcome in one or two sentences, from the user's side.

## Scope
- In:
- Out (explicitly not doing):

## Users and surfaces
Which panel, chat path or socket event this touches
(e.g. `analysis_report` panel, `user_message_request`, `workspace_sync`).

## Constraints
Tick every one that applies; each one pulls in a rule the design must meet.
- [ ] Changes a socket payload or `featureType` (backend/frontend must be told)
- [ ] Role-dependent behaviour (must go through the role gate)
- [ ] Touches personal data, PII redaction or the workspace purge
- [ ] Adds a paid call (model, OCR, STT, search) — cost must be metered
- [ ] Indian-law output (BNS/BNSS/BSA; no IPC in `applicable_laws`)
- [ ] Shared workspaces: a `view` collaborator must not be able to mutate

## Success measure
How we will know it worked after release (metric, log line, eval score).

## Open questions
