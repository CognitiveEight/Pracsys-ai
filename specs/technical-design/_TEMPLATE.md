# SIM-<n>: <title> — technical design

- **Requirements:** `specs/requirements/SIM-<n>-….md`
- **Status:** draft | approved
- **Approver:** <tech lead role>

## Summary
The approach in three or four sentences.

## Affected code
| File | Change |
|---|---|
| `app/socket/handlers/…` | |
| `app/socket/schemas.py` | |

## Event and payload changes
Inbound/outbound events, new or changed fields, and whether each change is
additive. A non-additive change needs a backend/frontend ticket linked here.

## Design
Data flow, new caches (`CacheKinds.…`), background tasks, model calls.

## Checklist against CLAUDE.md
Answer each; "n/a" is a valid answer, a blank is not.
- Role gate: does anything run before a validated role?
- Long-running work: scheduled via `_track_background_task`, cached, and
  re-emitted on reconnect?
- Every armed `workspace:refresh` loader paired with a terminal emit, and
  `asyncio.CancelledError` caught before `except Exception`?
- Emits go through `self._socket_client` / `_emit_document_feature` (stop fence)?
- Model calls go through the router; `is_generation_error()` checked?
- Paid calls metered at the lowest call site?
- New caches reached by the workspace purge (`_clear_workspace_memory`)?
- New ingestion/deletion path wires the manifest + removed-evidence helpers and
  deletes through `delete_documents` / `delete_workspace`?
- Chat path that writes state gated on `mutations_allowed`?
- Logging: no payloads, document text, names or URLs?

## Risks and rollback
What can go wrong in production and how to switch it off (flag, env var).

## Test plan
Which pytest tests, which `tests/offline/verify_*.py` group, and whether
`make e2e` must be run before deploy.

## Decisions
Choices made during implementation that differ from the above, with reasons.
