---
title: Role selection and the role gate
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Create a workspace and upload evidence
  - Chat question to AI answer
other_side: {}
related_specs: []
---

# Role selection and the role gate

## In plain words

Every case is analysed from one point of view: the judge, the petitioner,
the respondent, or the mediator. The AI service needs to know which one
before it builds the strategy, the analysis report and other panels, because
the same facts read differently depending on whose side you are on. The role
normally comes from the case settings when the case is created. The lawyer
can change it later, and the panels that depend on it are rebuilt. The
lawyer can also tell the chat which party they act for, and the strategy is
re-pointed at that party without changing the facts. On a shared case, only
people with edit access can change the role; a view-only colleague is told
the role was not changed.

## Who uses it

- The case owner, and collaborators with edit access.
- The backend, which sends the role with case creation and case updates.

## What the user sees

A role chosen in the case settings, and in chat a short confirmation when a
role is set by typing it. A role change in the settings shows loading states
on the role-dependent panels until they are rebuilt.

## How it works

1. **Case creation.** `workspace_created` carries `workspaceRole`. It is
   upper-cased and checked against the four valid roles. The role is stored
   in the role cache (in memory, and in Qdrant keyed by workspace and owner),
   the workspace context is created with it, and the role agent graph is
   run once (`initialize_workspace_with_role`): role gate, role selection,
   role context builder, role validation.
2. **Resolving the role for any later work.** `_resolve_workspace_role` takes,
   in order: the role in the current payload, the persisted role cache, the
   in-memory workspace context, and finally `ADJUDICATOR`.
3. **Change in case settings.** A `workspace_sync` whose `workspaceRole`
   differs from the stored role (both valid) is a role change. The service
   arms loaders for strategy, analysis report, list of dates, applicable
   laws and procedural timeline. When no document change is in the same
   sync, it re-emits the document assessments and case details from cache,
   regenerates strategy and the analysis report for the new role,
   re-emits the list of dates and procedural timeline from cache, and
   regenerates applicable laws. If documents also changed, the normal
   document branch regenerates the panels with the new role.
4. **Role typed in chat.** A message that is only a role name sets the role
   for the workspace and the chat, is audited
   (`workspace_role_selected_from_chat`), and gets a confirmation reply.
5. **Party named in chat.** A message such as "we act for X" on a petitioner
   or respondent case is classified as a role reassignment. The confirmed
   party is pinned on the workspace, and the strategic objective, next steps
   and suggestion chips are re-written to advocate for that party. The case
   summary is not re-framed. A reanalysis of the other panels is scheduled.
6. **Read-only askers.** If `resolve_asker` says the asker may not edit,
   steps 4 and 5 are skipped, the turn is answered as an ordinary question,
   and the reply says the role was not changed.

## Rules and limits

- Valid roles: `ADJUDICATOR`, `PETITIONER`, `RESPONDENT`, `MEDIATOR`. Any
  other value is not stored.
- The role cache is keyed by workspace and owner `userId`. A collaborator's
  id never becomes the key.
- Role affects panel outputs (strategy, analysis, drafts) most. Chat stays
  neutral and accuracy-first.
- Only advocacy roles (petitioner, respondent) can be re-pointed at a named
  party; adjudicator and mediator have no side.
- A role reassignment never changes case facts or the case summary.
- A view-only collaborator can never change the role, by settings or chat.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Role empty or invalid on case creation | Panels written from a neutral adjudicator view | Defaults to `ADJUDICATOR` and logs a warning |
| Role agent fails during creation | Nothing visible; panels still build | Logs the error and continues creation |
| Role cannot be found for a case that has documents (chat) | A normal answer, no role prompt | Continues as `ADJUDICATOR` and logs a warning |
| Role change regeneration fails | The previous or fallback strategy and report | Emits a fallback payload so loaders clear |
| View-only collaborator types a role | An answer plus "role not changed" | Drops the selection; audit records the permission |
| Persisting the role to Qdrant fails | Nothing until a restart | Logs a warning; the in-memory role still applies |

## For engineers

### Where the code is

- Role agent graph: `app/langgraph/role_agent.py`, `app/langgraph/nodes.py`,
  `app/langgraph/state.py`, `app/langgraph/integration.py`
- Role enum: `app/agents/role_models.py`
- Role cache and resolution: `app/socket/handlers/state_roles.py`
  (`_remember_workspace_role`, `_restore_workspace_role`, `_resolve_workspace_role`)
- Creation and settings change: `app/socket/handlers/workspace_events.py`
- Chat role selection and reassignment: `app/socket/handlers/chat.py`;
  classifier rules in `app/prompts/strategy_prompts.py` (Rule 13)
- Read-only asker: `app/workspace/participants.py`

### Events

- In: `workspace_created` (`workspaceRole`), `workspace_sync`
  (`workspaceRole`), `workspace_version_restore` (`workspaceRole`),
  `user_message_request`.
- Out: `workspace:refresh`, `document_feature_response`, `ai_response`.
- Full contract: [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

### Data

- `_role_cache_by_user_workspace` (`<workspace>:<owner>`) and
  `_role_cache_by_chat` (`<workspace>:<owner>:<chat>`), persisted with
  `upsert_workspace_role`.
- Workspace context metadata `role`, `role_context`, `role_selected_at`,
  and `confirmed_role_party` for a pinned party.

### Security

- Role selection is the most damaging write a reader could make, so it is
  gated on `mutations_allowed`.
- Audit events: `workspace_role_initialized`, `workspace_role_selected_from_chat`.

### Tests that guard it

- `tests/offline/verify_workspace_share.py` group 7 (a viewer cannot flip the role)
- `tests/offline/verify_objective_change.py` group 8 (reassignment keeps the summaries)

### Known gaps

- The gate is not enforced on the socket paths. `enforce_role_gate` and
  `handle_role_change` exist in `app/langgraph/` but nothing in
  `app/socket/` calls them. In practice every path falls back to
  `ADJUDICATOR`, so panels are never blocked for lack of a role.
- The chat "select a role" prompt in `chat_nodes.py` appears unreachable
  from the socket handler, which marks the role as selected whenever the
  case has documents.
- A role typed in chat is stored but does not rebuild the panels; only a
  role change through `workspace_sync` does.
- The settings-change branch does not regenerate relevant parties, case laws
  or drafting suggestions.
- There is no automated test of the settings-change regeneration or of role
  resolution order.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
