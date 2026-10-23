---
title: AI service docs – start here
type: index
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone
last_verified: 2026-10-21
jira: SIM-113
---

# AI service docs – start here

## In plain words

SimplerToday has three parts that lawyers never see separately: the lawyer
app, the backend that stores cases and files, and this AI service. The AI
service reads the evidence in a case, fills the analysis panels, answers chat
questions with checked references, and suggests drafts. It talks only to the
backend, never to the lawyer's browser.

These pages explain what the AI service does and how. Each page opens with a
short plain-language summary anyone can read. The detail for engineers comes
after, under "For engineers". The pages are the source of truth. They are
mirrored read-only to the "PracSys Engineering" Confluence space as
"<Title> — AI service" pages, next to the backend, lawyer app and admin pages
for the same feature.

## Where to begin

If you are new, read in this order:

1. [AI service – layered architecture](architecture/ai-service-layered.md):
   the parts and how they connect.
2. [Evidence to analysis panels](flows/evidence-to-analysis-panels.md) and
   [Chat question to AI answer](flows/chat-question-to-ai-answer.md): the two
   journeys that matter most.
3. [Backend <-> AI service socket events](contracts/backend-ai-socket-events.md):
   what crosses the wire.
4. The decisions below, then the feature pages for whatever you are changing.
5. For hands-on work: [Get started with Claude Code](runbooks/get-started-with-claude-code.md)
   and [Run locally and release](runbooks/run-locally-and-release.md).

## How the docs are organised

| Folder | Kind of page | Answers |
|---|---|---|
| `features/` | Feature | What does this do for the lawyer, and how does the AI side work? |
| `flows/` | Flow | What happens, step by step, across the app, backend and AI service? |
| `architecture/` | Architecture | What are the parts and how do they talk? |
| `contracts/` | Contract | What exactly do we exchange with another system? |
| `runbooks/` | Runbook | How do I do this operational task safely? |
| `decisions/` | Decision | Why is it built this way? |
| `policies/` | Policy | Which rules must always hold, and how are they enforced? |

## Features

| Page | What it covers |
|---|---|
| [AI chat](features/ai-chat.md) | Questions about the case, answered with checked citations |
| [Analysis panels](features/analysis-panels.md) | The panels filled from the evidence: assessments, summary and objective, analysis report, dates, laws, timeline, parties |
| [Case laws and applicable laws](features/case-laws-and-applicable-laws.md) | Statutes and precedents, checked against the statute register and the judgment text |
| [Role selection](features/role-selection.md) | The lawyer's side in the case; nothing runs without it |
| [Evidence upload](features/evidence-upload.md) | Reading PDFs, scans, office files, spreadsheets and audio |
| [Drafting and templates](features/drafting-and-templates.md) | Suggested drafts and the lawyer's template library |
| [Sharing, transfer and revoking access](features/sharing-transfer-and-revoking-access.md) | Edit and view collaborators, and moving a case to a new owner |
| [History and checkpoints](features/history-and-checkpoints.md) | Restoring an earlier version of the case documents |
| [Stop and finish update](features/stop-and-finish-update.md) | Stopping a running update and finishing it later |
| [Workspace deletion](features/workspace-deletion.md) | Erasing the AI service's copy of a deleted case |
| [AI cost tracking](features/ai-cost-tracking.md) | The cost of every answer and the running total per case |
| [Model choice](features/model-choice.md) | Choosing which AI model answers |

## Flows

| Page | What it covers |
|---|---|
| [Create a workspace and upload evidence](flows/create-workspace-and-upload-evidence.md) | From a new case to its first panels |
| [Evidence to analysis panels](flows/evidence-to-analysis-panels.md) | How documents become panel results |
| [Chat question to AI answer](flows/chat-question-to-ai-answer.md) | One chat turn, end to end |
| [Share a case and revoke access](flows/share-a-case-and-revoke-access.md) | Adding and removing collaborators |
| [Transfer a workspace to another owner](flows/transfer-a-workspace-to-another-owner.md) | Changing the case owner |
| [Delete a workspace](flows/delete-a-workspace.md) | From the delete click to erasure at the deadline |
| [Change the objective](flows/change-the-objective.md) | A new objective and the panels it refreshes |

## Architecture

| Page | What it covers |
|---|---|
| [AI service – layered architecture](architecture/ai-service-layered.md) | Every part of the service by layer, and how the parts talk |

Diagrams (draw.io, open in diagrams.net or the VS Code extension):

- `system-architecture-layered.drawio`: one page, the layered view above.
- `system-architecture.drawio`: twelve pages (context, containers, components,
  events, ingestion, panels, chat, model routing, data and erasure,
  deployment, security, delivery workflow).
- `SYSTEM_ARCHITECTURE.drawio`: three pages (system, multi-agent layer, data
  pipelines).
- `TARGET_ARCHITECTURE.drawio`: future state (AWS and Azure paths). Not the
  current system.

## Contracts

| Page | What it covers |
|---|---|
| [Backend <-> AI service socket events](contracts/backend-ai-socket-events.md) | Every event in and out, with the rules both sides follow |
| [Model calls through gate0](contracts/model-calls-through-gate0.md) | What the AI service sends to the gate0 gateway and what it relies on |

## Runbooks

| Page | What it covers |
|---|---|
| [Get started with Claude Code](runbooks/get-started-with-claude-code.md) | Setting up Claude Code for this repo |
| [Run locally and release](runbooks/run-locally-and-release.md) | Running the service, the test gates, and promoting a release |
| [Re-index evidence](runbooks/reindex-evidence.md) | Rebuilding the evidence collection, for example after an embedding change |
| [Rebuild the statute index](runbooks/rebuild-the-statute-index.md) | Refreshing the section list used to check applicable laws |
| [Reconcile gate0 spend](runbooks/reconcile-gate0-spend.md) | Comparing each case's model cost with gate0's records |
| [Roll back the handshake auth](runbooks/roll-back-the-handshake-auth.md) | Returning to the legacy socket handshake in an emergency |

## Decisions

AI service decisions are numbered `DR-AI-nnnn`. Backend decisions use their own
`DR-nnnn` numbers; related ones are the AI handshake HMAC v1 (DR-0009,
https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161659), check
every AI reply before trusting it (DR-0013,
https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4227166) and the
backend-owned processing pass (DR-0005,
https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4161617).

| Page | Decision |
|---|---|
| [DR-AI-0001](decisions/DR-AI-0001-one-model-router.md) | All model calls go through one provider-neutral router |
| [DR-AI-0002](decisions/DR-AI-0002-gate0-as-the-model-cost-and-log-layer.md) | gate0 as the single model, cost and log layer; enforcement off until gate0 is highly available |
| [DR-AI-0003](decisions/DR-AI-0003-nothing-is-cited-unless-checked.md) | Nothing is cited unless it was checked |
| [DR-AI-0004](decisions/DR-AI-0004-stop-is-a-fence.md) | Stop fences a run by epoch instead of relying on cancellation |
| [DR-AI-0005](decisions/DR-AI-0005-workspace-deletion-erases-every-copy.md) | Deleting a workspace erases every copy the AI service holds |

## Policies

| Page | What it covers |
|---|---|
| [Logging and personal data](policies/logging-and-personal-data.md) | What may and may not appear in logs, and how that is checked |
| [Workspace erasure](policies/workspace-erasure.md) | The erasure rules for a deleted case |

## Older reference documents

These remain in `docs/` and are still useful for depth. Where they disagree
with the pages above, the pages above and the code win.

| Document | What it holds |
|---|---|
| `architecture.md` | Short code map and list of detailed documents |
| `SYSTEM_ARCHITECTURE.md` | Long narrative of the event flow and multi-agent design, with payload shapes |
| `SOCKET_EVENTS_ARCHITECTURE.md` | Module map of the socket handler mixins and the conventions for adding events and panels |
| `MODEL_PORTABILITY.md` | Model router, provider profiles, gate0 configuration, cost and capability matrix |
| `AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md` | The optional knowledge graph (ontology, Neo4j, retrieval planner) |
| `AI_SERVICE_AUTH_MIGRATION.md` | Handshake signature scheme and failure codes |
| `BACKEND_VERSION_RESTORE_SPEC.md` | Backend-facing spec for version restore and deleted documents |
| `socket-reconnection-and-stability.md` | Why the socket disconnects and how reconnects are handled |
| `onboarding.md` | Reading order, vocabulary and first week for new engineers |
| `claude-code-setup.md` | How Claude Code is configured in this repo |
| `migration-guide.md` | Dependencies, where state lives, and how to move the service |
| `development-workflow.md` | From Jira ticket to release |
| `STRUCTURE.txt` | ASCII tree of the repository |
| `archive/` | Superseded notes kept for history; do not rely on them |

The repository root also holds `CLAUDE.md` (the rules every change must
respect) and `CHANGELOG.md` (feature history).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
