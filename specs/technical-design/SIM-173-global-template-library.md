# SIM-173: Global Template Library feed — technical design

- **Requirements:** `specs/requirements/SIM-173-global-template-library.md`
- **Status:** draft — for review (implemented on the ticket branch)
- **Contract:** Confluence AI brief for SIM-58, version 2

## Summary
The routing lives in the template store, so the 8,000-line drafting agent keeps
calling with the lawyer's `user_id` and changes in only a few places.

## Template store (`app/langgraph/template_store.py`)
- Reserved owner `__global__` holds each global template's CURRENT version,
  with `scope="global"`, `version` and `global_status` (published | retired).
- Reserved owner `__global_draft__` holds admin drafts being profiled, so a
  draft is never suggested before it is published.
- Older versions are kept apart in `_global_versions[(id, version)]`. Writing
  a new current version moves the old one there, so a draft started on v3 keeps
  reading v3.
- `list_for_user(lawyer)` returns only the **published** global templates, the
  same for every lawyer.
- `get(lawyer, id)` checks the lawyer's bucket first, then the global library,
  retired templates included, so a draft already started keeps working. A
  lawyer's bucket now holds only a template lazily fetched for a draft that
  predates the global library (`templateUrl` with no `templateScope`); it is
  never listed. Inside
  a drafting turn, `version_pins()` / `pin_template_version()` (contextvar) make
  `get` return the pinned version, or None so the turn lazy-fetches it.
- `put(lawyer, …)` of a payload with `scope="global"` (a drafting write-back,
  such as classification or a skeleton backfill) goes back to the entry for its
  own version, and never changes `global_status`.
- `is_suggestable` (listed now; excludes retired), `global_active`,
  `global_ids`, `set_global_status` and `forget_global` serve the handlers.

## Handlers (`app/socket/handlers/template_sync.py`)
- `on_connect` starts `request_global_template_sync` as a tracked task: it
  sends `{requestId: "gts-…"}` and waits, bounded by
  `TEMPLATE_SYNC_TIMEOUT_SECONDS`, in `_pending_template_sync`.
- `on_global_template_sync_response` applies every item as `published`, retires
  every held id the snapshot omits, resolves the waiter, and refreshes the
  drafting panels.
- `on_global_template_sync`:
  - `published`: the same version and hash is a no-op; a new version with the
    same hash reuses the extraction (current entry, or the profiled draft);
    otherwise the file is downloaded and profiled; presets are stored without
    a file.
  - `retired`: `set_global_status(retired)`.
  - `deleted`: `forget_global`.
- An empty `formatting.blueprint` (`{}`) is dropped before ingest, so it is
  never taken as an approved blueprint.
- `ensure_templates_synced(*, timeout, force)` and `_prewarm_templates(*, timeout)`
  no longer take a `user_id`: a no-op once a snapshot has been applied this
  connection (`_global_templates_synced`), otherwise they request the global
  snapshot or join the one in flight, bounded as before.
- Removed: `on_template_sync`, `on_template_sync_response`, the per-lawyer
  ingest and proposal helpers, `request_template_sync`, the
  `TemplateSync*` schemas, the `TemplateAction` constants, and the drafting
  agent's `apply_approved_blueprint` / `rename_template` (only the per-lawyer
  handler used them). An approved blueprint now arrives on `published`.
- Every `request_blueprint_proposal` is global (`scope` optional) and ingests under `__global_draft__`, reusing
  a same-hash extraction. It does not profile twice on first upload, and
  forces a re-profile when the draft is already held ("Re-profile"). It always
  replies, `status="failed"` on failure, with `scope`, the echoed `fileHash`
  and no `userId`.
- Drafting refresh: every lawyer with a built drafting panel goes through the
  existing `_maybe_reemit_drafting_on_templates` count check.

## Drafting agent (`app/langgraph/drafting_agent.py`)
- `handle_turn` wraps the turn in `version_pins()`. After the session loads,
  it pins the request's `templateId`/`templateVersion`, else the session's
  `template_id`/`template_version`. The kickoff stores `template_version` in
  the session.
- `_resolve_template`: with `templateScope="global"`, a version we do not hold
  is lazy-fetched even on a suggested-draft click (it is not a deleted
  template), and ingested with `scope`/`version`.
- `ingest_template(scope=, version=)` marks global payloads. Global DOCX
  skeletons live at `templates/global/{id}/v{version}.docx`, not under a
  lawyer.
- Re-match self-heal checks `is_suggestable`, so a card matched to a retired
  template is demoted.

## Schemas and wiring
- `GlobalTemplateItem`, `GlobalTemplateSyncPayload` and
  `GlobalTemplateSyncResponsePayload`.
- `UserMessageRequestPayload.templateScope` / `templateVersion`, passed through
  `request_fields`.
- Bindings in `client.py`, registration in `events.py`, constants in
  `backend_events.py`.

## Cost
Global ingest and profiling are priced and logged with `log_unbilled_usage`,
as per-lawyer template work is today (no case to bill).

## Risks
- The first connect after a restart downloads and profiles every published
  file template (same as the per-lawyer snapshot today). An approved blueprint
  in the feed skips the profiler.
- Deploying before the backend sends the global feed leaves the Drafting
  Assistant with no templates to suggest (no per-lawyer fallback).
