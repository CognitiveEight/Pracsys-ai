---
title: Transfer a workspace to another owner
type: flow
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, admin, backend, ai-service]
other_sides:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4882433
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4620343
  admin: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5177345
related_features: [../features/sharing-transfer-and-revoking-access.md, ../features/drafting-and-templates.md]
related_specs: []
---

# Transfer a workspace to another owner

## In plain words

A case can be handed from one lawyer to another, for example when a colleague
takes over a matter. The documents and the analysis do not change, so the AI
service does not read the files again or rebuild the panels. It only records
that the case now belongs to the new lawyer, sets up the case's role for them,
and forgets the previous owner's short-term settings for that case. The one
panel it rebuilds is the suggested drafts, sent again for the new owner. The
templates themselves do not change: every lawyer uses the same Global Template
Library (SIM-173), so the service only makes sure it is loaded. If the case
is archived, the drafts are not rebuilt until it is next used.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app or admin | Owner or an admin transfers the case to another lawyer | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4620343), [Admin page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5177345) |
| 2 | Backend | Moves ownership and sends `workspace_transferred` with both user ids and `reEmitFeatures` | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4882433) |
| 3 | AI service | Re-keys owner state to the new owner | This page, steps 1-5 |
| 4 | AI service | Makes sure the global template library is loaded (`request_global_template_sync`, only if no snapshot this connection) | This page, step 6 |
| 5 | AI service | Rebuilds suggested drafts and sends `drafting_assistant` | This page, step 6 |
| 6 | Lawyer app | New owner sees the case, with drafts matched to the published global templates | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4620343) |

## The AI service's steps in detail

1. **Accept the event.** The payload is validated as
   `WorkspaceTransferredPayload`. Without `workspaceId` or `newUserId` the event is
   dropped with a warning. Any pending purge is revoked. A transfer with the same
   workspace, both user ids and `transferredAt` seen in the last 180 seconds is
   ignored as a replay.
2. **Point reconnect replay at the new owner.** The workspace is set as the one
   to replay after a reconnect, under the new owner's id.
3. **Resolve the role.** `_resolve_workspace_role` uses the payload role, then
   the workspace context, then the persisted role, then `ADJUDICATOR`.
4. **Re-own the context.** The workspace context is updated (title, status,
   role, file list, time) and `transfer_ownership` changes its `user_id`, which
   `upsert_workspace` alone never does. The role is remembered for the new owner
   and the role agent is run for them (`initialize_workspace_with_role`); a
   failure there is logged and does not stop the transfer.
5. **Clear the previous owner's keys** (only when the owner actually changed):
   in-memory role caches keyed by the previous owner are evicted; drafting
   provenance is re-pointed to the new owner so a template event from the previous
   owner cannot refresh this case; the cached `document_assessments` and
   `workspace_metadata` envelopes get the new `userId`, because they are
   replayed verbatim on reconnect. An audit record `workspace_transferred` is
   written.
6. **Rebuild only what was asked.** The evidence file list is logged as
   informational; nothing is re-read. If `reEmitFeatures` is empty, or the
   status is `archived`, the handler stops here. Otherwise a background task
   handles each listed feature. Only `drafting_assistant` is supported:
   - new owner: `ensure_templates_synced()` waits until the global template
     library is loaded (a no-op once a snapshot was applied this connection),
     then `_generate_and_emit_drafting_suggestions` rebuilds and sends the
     panel;
   - same owner: the cached panel is re-sent, and regenerated only if nothing
     was cached.
   Any other feature name is skipped with a warning.

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 1 | Backend re-sends the transfer on reconnect | Nothing new | Ignored inside the 180-second window |
| 1 | Payload missing the new owner | Nothing changes on the AI side | Event dropped with a warning |
| 4 | Role agent fails | Case works as before | Logged; transfer continues |
| 6 | Case is archived | Drafts reflect the previous owner's templates until the case is used again | Re-emit skipped |
| 6 | Template sync or draft generation fails | Drafts panel may keep the previous suggestions | Warning logged; no retry |
| 6 | Unknown feature in `reEmitFeatures` | That panel is not refreshed | Skipped with a warning |

## For engineers

**Entry and exit.** Entry: `on_workspace_transferred` in
`app/socket/handlers/workspace_events.py`. Exit: one `document_feature_response`
with `featureType: drafting_assistant`, plus a `request_global_template_sync` emit
from `ensure_templates_synced` in `app/socket/handlers/template_sync.py` when no
snapshot has been applied this connection.

**Events and payloads.** `WorkspaceTransferredPayload` (`workspaceId`,
`previousUserId`, `newUserId`, `newUser`, `title`, `workspaceRole`, `status`,
`evidenceFiles`, `reEmitFeatures`, `transferredAt`) in `app/socket/schemas.py`.
Contract: [Backend and AI service socket events](../contracts/backend-ai-socket-events.md).

**State and timing rules.**
- Panel caches are keyed by workspace id only, so they carry over unchanged.
- The case's cumulative cost lives on the workspace context, so it carries over
  to the new owner.
- `transfer_ownership` is in `app/workspace/context.py`.
- `workspace_transferred` counts as new work for stop staleness but is not a
  cancellable run.

**Security checks.** Purge revocation runs first. User ids are logged truncated.

**Tests.** No offline suite covers `on_workspace_transferred`; it is not driven
by `make e2e` either. Validate manually against a connected backend.

**Known gaps.**
- The re-emit runner is started with a bare `asyncio.create_task`, not
  `_track_background_task`, so it is not held, not tied to the run, and cannot be
  cancelled by a stop.
- The sharing roster is not touched; whether the backend sends a fresh
  `participants` list after a transfer was not verified.
- The previous owner's persisted role row in Qdrant is left in place (it is
  user-keyed and never read for the new owner).
- No `workspace:refresh` is sent before the drafts rebuild; whether the lawyer
  app shows a loader for it was not verified.

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
