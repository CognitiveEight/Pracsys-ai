# SIM-152: Instant replies in #pracsys-requests

- **Jira:** SIM-152 (epic SIM-71)
- **Owner:** product owner
- **Status:** draft
- **Target branch flow:** feature/SIM-152-instant-triage → development → staging → main

## Problem
The triage bot (SIM-133) runs from GitHub's schedule. GitHub treats frequent
schedules as best effort and in practice starts the job only about every 30
minutes, so a question posted in `#pracsys-requests` waits up to half an hour,
and an approval (✅) takes as long to become a Jira ticket.

## Goal
A reply or a created ticket appears about one minute after someone posts in
`#pracsys-requests` or reacts to a draft, with the same rules as today.

## Scope
- In:
  - Slack sends each new message and each reaction in `#pracsys-requests` to
    a small relay on GCP (Cloud Function, 2nd generation, HTTP).
  - The relay checks that the request really comes from Slack (signing
    secret), ignores everything that is not a person's message or reaction
    in the triage channel, and starts the existing "Slack triage" workflow
    through GitHub (`workflow_dispatch`).
  - Bursts collapse: GitHub keeps at most one run going and one waiting, so
    ten messages in a minute lead to one or two runs, and every message is
    still handled.
  - The 5-minute schedule stays as a safety net for any missed event.
  - First deployment on a **demo GCP project**; a runbook covers deploying
    it anywhere.
- Out:
  - No change to the triage rules, the helpers or the approval gate.
  - No change to the daily worklist.
  - Other channels, DMs, @mentions anywhere else.

## Users and surfaces
- Everyone posting in `#pracsys-requests`; the approver reacting to drafts.
- Slack app "PracSys Triage" (Event Subscriptions), GCP (relay), GitHub
  Actions (the existing workflow).

## Constraints
- [ ] Socket payload, role gate, purge, billing, model routing: not touched
- [x] Secrets: Slack signing secret and a GitHub token live in GCP Secret
  Manager, never in the repo or logs
- [x] PII: the relay never logs or forwards message text; it only decides
  whether to start the workflow
- [x] Paid calls: one extra workflow run per burst of activity (Claude cost
  as today); the GCP function itself fits in the free tier at this volume

## Success measure
In the demo, a question posted in `#pracsys-requests` is answered within two
minutes, ten times in a row; the relay's log shows only event type, channel
and outcome.

## Open questions
- Which GCP project and region for the demo (the product owner deploys it).
- When to move from the demo project to the production GCP project.
