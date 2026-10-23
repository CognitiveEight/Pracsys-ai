---
title: Share a case and revoke access
type: flow
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
participants: [lawyer app, admin, backend, ai-service]
other_sides:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4292628
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4882451
  admin: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4685922
related_features: [../features/sharing-transfer-and-revoking-access.md, ../features/ai-chat.md]
related_specs: []
---

# Share a case and revoke access

## In plain words

A case owner can share a case with colleagues, each with either edit or view
access. The AI service does not decide who may open a case; the backend does
that. The service only needs to know one thing: when someone types in the case
chat, may that person change the case, or only read it? So it keeps a list of
the people the case is shared with and their access level. Anyone with access
gets the same full answers. A view-only colleague cannot change panels, correct
facts, change the objective or change the role; if they ask for one of those,
the reply says plainly that nothing was changed. When the owner stops sharing,
the list is dropped at once. The case still belongs to the owner throughout, so
cost and saved panels stay on the owner's case. The list is erased when the
case is deleted.

## The whole flow at a glance

| # | Who | What happens | Where it's documented |
|---|-----|--------------|-----------------------|
| 1 | Lawyer app or admin | Owner (or an admin) shares the case and sets edit or view per person | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4882451), [Admin page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4685922) |
| 2 | Backend | Grants access and sends `workspace_sync` with `participants`; `userId` stays the owner | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4292628) |
| 3 | AI service | Stores the roster | This page, step 1 |
| 4 | Lawyer app | A collaborator types in the case chat | [Lawyer app page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4882451) |
| 5 | Backend | Sends `user_message_request` with the owner's `userId` and `askedBy` naming the collaborator | [Backend page](https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4292628) |
| 6 | AI service | Decides edit or read-only, answers, skips any write for a reader | This page, steps 2-4 |
| 7 | Lawyer app, admin, backend | Owner or admin revokes access; backend sends `workspace_sync` without `participants` (if no one is left) or with the shorter list | Other sides' pages |
| 8 | AI service | Drops or replaces the roster | This page, step 5 |

## The AI service's steps in detail

1. **Store the roster.** On every `workspace_sync`, `set_participants(...,
   authoritative=True)` replaces the roster with the `participants` list:
   `userId`, `name`, `permission` per person. This runs before the sync's replay
   guard, so a share-only re-sync still updates the roster without rebuilding any
   panel. The roster is held in memory and written through to Qdrant as
   `CacheKinds.PARTICIPANTS`. `workspace_version_restore` and
   `document_reload_request` also carry `participants` but pass
   `authoritative=False`, so an absent field there leaves the roster alone.
2. **Resolve the asker.** `resolve_asker(workspace_id, userId, askedBy)` runs
   after cache hydration, so a cold process still sees the persisted roster:
   - no `askedBy`, or `askedBy` equal to the owner: owner, may edit;
   - on the roster: `edit` (also `write`, `manage`, `admin`, `owner`) may edit;
     anything else, including an unknown value, is read-only;
   - not on a roster that exists: read-only, logged;
   - no roster cached at all: allowed to edit, logged. The sharing sync may not
     have reached this process yet, and failing closed would break every
     collaborator on that case.
3. **Answer the same way.** The analysis is identical for any asker. For a
   collaborator, a short `WHO IS ASKING` block names them and their access and
   tells the model that access changes nothing about the answer and that they are
   not a party to the dispute.
4. **Skip writes for a reader.** With `mutations_allowed` false, the service
   skips role selection, surgical panel edits, fact corrections, the objective
   change cascade and panel updates from AI markers. When the message asked for
   one of those, the reply starts with a notice such as "You have view-only
   access to this case, so I have not changed any panel. Ask the case owner to
   make this edit." The audit record notes the asker and their permission.
   `missing_information_edited` applies the same check to `editedBy`.
5. **Revoke.** A `workspace_sync` with `participants` absent (not an empty list)
   means the case is no longer shared: the roster is dropped and the change is
   persisted. A shorter list replaces the roster, so a removed person is no longer
   on it. If a roster still exists for other people, a removed person is read-only
   from then on.
6. **Erase on delete.** The roster is module-level state, outside the purge's
   sweep of the handler's caches, so `_purge_workspace` clears it by name
   (`clear_participants`).

## What can go wrong

| Step | Situation | User sees | System does |
|------|-----------|-----------|-------------|
| 1 | Unknown permission value from the backend | Collaborator gets read-only behaviour | Any value not in the write set is read-only (missing becomes `view`); the sync is not rejected |
| 2 | Collaborator chats before the share sync reached this process | Their edits apply | No roster cached, so allowed to edit; logged |
| 2 | Removed collaborator still has a chat open while others remain shared | Their edit requests are refused | Not on the roster, so read-only |
| 4 | Read-only collaborator asks for a panel edit | Full answer, preceded by the "not changed" notice | Edit skipped |
| 4 | Read-only collaborator types only a role name | Normal reply with the notice | Role selection dropped |
| 5 | Last collaborator removed | No visible change | `participants` absent, roster dropped |

## For engineers

**Entry and exit.** Roster in: `on_workspace_sync` in
`app/socket/handlers/workspace_events.py`. Check: `on_user_message_request` in
`app/socket/handlers/chat.py` and `on_missing_information_edited` in
`app/socket/handlers/analysis_report.py`. Store: `app/workspace/participants.py`.

**Events and payloads.** `WorkspaceParticipant`, `WorkspaceSyncPayload.participants`,
`ChatAskedBy`, `UserMessageRequestPayload.askedBy` in `app/socket/schemas.py`.
Contract: [Backend and AI service socket events](../contracts/backend-ai-socket-events.md).
`userId` is the owner on every event; never re-key owner caches by the asker.

**State and timing rules.** The roster is hydrated on a cold workspace in
`_hydrate_workspace_caches` (`app/socket/handlers/state_roles.py`). Hydration
never overwrites a live in-memory roster.

**Security checks.** Participant names are personal data: they are not logged
(counts only), not put in legal prompts other than the attribution block, and
cleared on purge. Any new chat path that writes workspace state must be gated
on `mutations_allowed`.

**Tests.** `tests/offline/verify_workspace_share.py` (schemas, roster, resolve
rules, read-only replies, attribution, purge).

**Known gaps.**
- `analysis_report_feedback` and `added_missing_information` are not gated by
  `resolve_asker` in this service; whether the backend blocks view-only users
  from sending them was not verified.
- If this process misses the un-share sync, the old roster (in memory or in
  Qdrant) stays, so a former collaborator keeps their old permission here until
  the next `workspace_sync`. The backend's own access check is the real barrier.
- The roster is per process; multiple AI service instances would each need the
  sync.

## History

| Date | Change | Spec / commit |
|------|--------|---------------|
| 2026-10-21 | First version, written from the code | SIM-113 |
