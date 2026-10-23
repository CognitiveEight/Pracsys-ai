# SIM-151: Daily PracSys worklist in Slack

- **Jira:** SIM-151 (epic SIM-71)
- **Owner:** product owner
- **Status:** approved by the product owner on 2026-10-23
- **Target branch flow:** feature/SIM-151-daily-worklist → development → staging → main

## Problem
The team has no single place, each morning, that shows what is being worked on
and what comes next across all of PracSys (AI service, backend, lawyer app,
admin panel). People open Jira boards one by one, or ask in chat.

## Goal
Every working morning, one Slack post shows the day's worklist for the whole
platform, taken from Jira SIM, without anyone preparing it.

## Scope
- In:
  - A post in `#pracsys-daily` at 09:00 IST, Monday to Saturday.
  - Three sections, for all components (AI, BE, FE, Admin, DevOps, QA):
    1. **In Progress and In Review**, grouped by assignee, oldest first;
       unassigned items listed under "Unassigned".
    2. **Next up**: the top 10 To Do tickets by priority (P1 → P4), oldest
       first within a priority.
    3. **Blocked and overdue**: flagged tickets and tickets past their due
       date that are not Done.
  - Each line: key (linked), summary, component, priority; for in-progress
    items, how many days since the last update.
  - A kill switch (repo variable) and a manual run.
- Out:
  - No AI model: the list is built by a script, so it is predictable and free
    to run.
  - No changes to Jira tickets; read-only.
  - Per-person direct messages, sprint burndown, story-point totals.

## Users and surfaces
- Everyone in the team reads `#pracsys-delivery`'s sister channel
  `#pracsys-daily`.
- Data source: Jira project SIM (the service account already used by the
  triage workflow).

## Constraints
- [ ] Socket payload or schema change: no
- [ ] Role gate: no
- [x] PII: ticket summaries are posted; they must not contain client data
  (team rule already); the script posts only key, summary, component,
  priority, assignee display name and dates
- [ ] Paid calls: none (no model)
- [ ] Indian-law grounding: n/a
- [ ] Shared workspaces: n/a

## Success measure
A post appears in `#pracsys-daily` every working morning between 09:00 and
09:45 IST (GitHub may start scheduled runs late), and its counts match the
same JQL run by hand in Jira.

## Open questions
- Should Sunday be included later? (Not for now.)
- Should the post also list drafts waiting for approval in
  `#pracsys-requests`? (Not chosen for now.)
