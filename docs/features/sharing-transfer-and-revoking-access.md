---
title: Sharing, transfer and revoking access
type: feature
repo: law-firm-simplertoday-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Share a case and revoke access
  - Transfer a workspace to another owner
other_side:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5341185
  lawyer app: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4390962
related_specs: []
---

# Sharing, transfer and revoking access

## In plain words

A case owner can share a case with colleagues, with either edit or view
access, and can take that access away again. The backend decides who may open
the case. The AI service only needs to know one thing: when someone types in
the case chat, may they change the case, or only read it? A colleague with
view access gets the same answers as anyone else. But if they ask the AI to
change a panel, change the objective or change the case role, nothing changes,
and the reply says so. Ownership can also move to another lawyer. The AI then
treats the new owner as the owner from that moment, and rebuilds the drafting
suggestions against the new owner's own template library. The case analysis
itself is kept.

## Who uses it

- Case owners who share, un-share or transfer a case.
- Collaborators who ask questions in a shared case.
- Administrators who transfer cases between lawyers.

## What the user sees

Collaborators use the same chat. A view-only collaborator who asks for a change
gets the answer plus a short notice that nothing was changed and that the case
owner must make it. After a transfer, the new owner sees the same panels and a
refreshed Drafting Assistant. See the lawyer app page in `other_side`.

## How it works

1. **The roster arrives on sync.** `workspace_sync.participants` carries
   `[{userId, name, permission}]`, with `permission` of `edit` or `view`. The
   field is absent when the case is not shared. `userId` on every payload
   stays the case owner.
2. **Absent means un-shared, but only on sync.** On `workspace_sync` and
   `workspace_resume_request`, an absent field clears the roster (this is how
   revoking access reaches us). On `workspace_version_restore` and
   `document_reload_request`, an absent field leaves the roster alone, because
   those events say nothing about sharing. The roster is updated even when a
   sync is otherwise skipped as a duplicate.
3. **The roster is stored per case.** It is kept in memory, written through to
   the Qdrant state cache, and reloaded on a cold process.
4. **Chat names the asker.** `user_message_request.askedBy` (`{userId, name}`)
   is present only when someone other than the owner typed the message.
5. **`resolve_asker` decides.** In order:
   1. No `askedBy`, or it equals the owner: the owner, full rights.
   2. On the roster: their permission decides. `edit` (and synonyms such as
      `write`, `manage`, `admin`, `owner`) may change; anything else is
      read-only.
   3. Not on a roster we have: read-only.
   4. No roster cached at all: allowed, and logged. The sharing sync may not
      have reached this process yet.
6. **Mutations are gated.** With `mutations_allowed` false, the chat skips
   panel edits and fact corrections, the objective-change cascade, AI-marker
   panel updates and role selection, and appends a view-only notice. The same
   check gates `missing_information_edited` on its `editedBy`.
7. **Transfer re-keys the owner.** `workspace_transferred` moves owner-scoped
   state from `previousUserId` to `newUserId`: the workspace context, role
   memory and role gate, drafting provenance, and the cached
   `document_assessments` and `workspace_metadata` wrappers. The previous
   owner's in-memory role cache is evicted. Documents are not re-read and the
   full suite is not re-run.
8. **Transfer re-emits only what was asked.** For each entry in
   `reEmitFeatures` (today only `drafting_assistant`), the new owner's
   template library is force-synced, then the suggestions are rebuilt. An
   archived case is re-keyed only.

## Rules and limits

- `userId` is always the owner. Collaborators never become a cache key.
- Participants are app users, not parties to the dispute. They never enter
  legal prompts. Their only prompt appearance is a short "who is asking" line
  in chat context, which states that access level changes nothing about the
  analysis.
- Unknown permission values are read-only.
- Any chat path that writes case state must check `mutations_allowed`.
- A transfer re-sent with the same fingerprint within 180 seconds is ignored.
- A transfer, like any live traffic, revokes a pending workspace deletion.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| View-only collaborator asks for an edit | Answer plus "view-only access, nothing changed" | Skips the mutation; logs the asker's permission, not their name |
| Access revoked, sync not yet received | Former editor could still change the case via chat | Uses the cached roster until the next sync clears it |
| Collaborator asks before any roster is cached | Edits are allowed | Logs a warning; fails open by design |
| Asker not on a known roster | Treated as view-only | Logs a warning |
| Transfer to an archived case | Panels unchanged | Re-keys ownership; drafting re-emit waits for the next sync |
| Template sync for the new owner is slow | Drafting suggestions arrive late | Waits for the full snapshot before matching |
| Transfer step fails (context, role, cache patch) | Usually nothing visible | Each step is isolated and logged; the rest continue |

## For engineers

### Where the code is

- `app/workspace/participants.py` — roster, `set_participants`,
  `resolve_asker`, `AskerContext`, `clear_participants`.
- `app/socket/handlers/workspace_events.py` — `on_workspace_sync`,
  `on_workspace_version_restore`, `on_workspace_transferred`.
- `app/socket/handlers/chat.py` — asker resolution and every
  `mutations_allowed` gate.
- `app/socket/handlers/analysis_report.py` — `on_missing_information_edited`.
- `app/socket/handlers/run_control.py`, `app/socket/handlers/document_reload.py`
  — roster handling on resume and reload.

### Events

Inbound: `workspace_sync` (`participants`), `workspace_version_restore`,
`document_reload_request`, `workspace_resume_request`, `user_message_request`
(`askedBy`), `missing_information_edited` (`editedBy`), `workspace_transferred`.
Outbound after transfer: `document_feature_response` (`drafting_assistant`),
`request_template_sync`. See
[the socket contract](../contracts/backend-ai-socket-events.md).

### Data

- Roster: module-level dict plus the Qdrant `PARTICIPANTS` state cache.
- Transfer de-dupe: `_last_workspace_transfer_fingerprint`.

### Security

- Participant names are personal data. They are never logged; logs use labels
  such as `collaborator/view`.
- The roster is module-level, so the workspace purge clears it by name
  (`clear_participants`); the `self.__dict__` sweep would miss it.
- The role gate is the most damaging write a reader could make, so role
  selection is the first thing blocked for view-only askers.

### Tests that guard it

- `tests/offline/verify_workspace_share.py` — roster set, absent means
  un-shared, restore, hydrate, purge, asker resolution and gated mutations.
- `tests/offline/verify_missing_info_edit.py` — `editedBy` gating.
- `tests/offline/verify_document_reload.py`, `tests/offline/verify_run_control.py`
  — roster kept or applied on reload and resume.

### Known gaps

- Drafting chat (`DRAFTING_ASSISTANT`) runs before the gates and is not
  restricted for view-only collaborators.
- Transfer does not touch the roster. If the new owner was a collaborator, or
  the previous owner should lose access, the AI relies on the next sync.
- The transfer re-emit runs in a bare `asyncio.create_task`, not
  `_track_background_task`, so a stop cannot cancel it.
- Role rows persisted for the previous owner stay in Qdrant after a transfer;
  they are user-keyed and never read for the new owner.
- No automated test covers `workspace_transferred`.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
