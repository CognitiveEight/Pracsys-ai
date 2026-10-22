---
paths:
  - "app/socket/**/*.py"
---

# Socket handlers

Full detail: CLAUDE.md sections "Architecture", "A lit loader must always be
paired with a terminal emit", "Stop is a fence", "Workspace sharing".

- New event or payload: a mixin in `app/socket/handlers/` plus a schema in
  `app/socket/schemas.py`. Payload changes are additive; anything else needs
  a CHANGELOG entry and a linked backend/frontend ticket.
- Every panel runs for a validated role; resolve it with `_resolve_workspace_role`
  and fall back only to `DEFAULT_WORKSPACE_ROLE` (`app/core/roles.py`), never a
  literal role.
- Long work: `_track_background_task`, never a bare `asyncio.create_task`.
  Cache the result and re-emit it on reconnect.
- Catch `asyncio.CancelledError` before `except Exception`, log, re-raise.
- Every armed `workspace:refresh` loader gets a terminal
  `document_feature_response`; use `_release_refresh_loaders` on exit paths.
- Emit through `self._socket_client` / `_emit_document_feature` only.
- Idempotency: re-sends under the same `requestId` are expected.
- Any workspace-scoped inbound event cancels a pending purge
  (`cancel_pending_purge`).
- Chat paths that write workspace state are gated on `mutations_allowed`.
- These handlers have no full automated coverage: run `make e2e` before
  deploy when you change them.
