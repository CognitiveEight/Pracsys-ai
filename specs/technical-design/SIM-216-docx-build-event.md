# SIM-216: Word file from stored draft text — technical design

- **Requirements:** `specs/requirements/SIM-216-docx-build-event.md`
- **Status:** draft — written with the implementation; needs tech-lead approval
- **Approver:** tech lead

## Summary
A new handler mixin answers `draft_docx_build_request`. It validates the
payload, de-dupes on `requestId`, refuses deleted cases, and schedules a
tracked background build. The build converts the draft to normalised
Markdown (HTML through a new stdlib-only converter), then calls a new public
`DraftingAgent.build_docx_from_text`. That reuses the chat's template lookup
and the render half of `_export_draft_docx`, now split out as
`_render_draft_docx`, and uploads through `upload_draft_docx`. One
`draft_docx_build_response` is emitted, success or failure.

## Affected code
| File | Change |
|---|---|
| `app/socket/handlers/draft_docx_build.py` | **new** `DraftDocxBuildMixin.on_draft_docx_build_request`, `_run_draft_docx_build` |
| `app/processing/draft_html.py` | **new** `html_to_markdown` (stdlib `html.parser`) |
| `app/langgraph/drafting_agent.py` | **new** `DocxBuildResult`, `build_docx_from_text`; render half of `_export_draft_docx` moved verbatim into **new** `_render_draft_docx` (raises; `_export_draft_docx` behaviour unchanged) |
| `app/socket/schemas.py` | **new** `DraftDocxBuildRequestPayload`, `DraftDocxBuildResponsePayload`, `DRAFT_DOCX_BUILD_MAX_CONTENT_CHARS` |
| `app/socket/backend_events.py` | **new** `DRAFT_DOCX_BUILD_REQUEST`, `DRAFT_DOCX_BUILD_RESPONSE` |
| `app/socket/events.py`, `handlers/__init__.py`, `client.py` | mixin composed, `_draft_docx_build_requests` ledger, handler registered, socket shim |
| `app/socket/handlers/run_control.py` | event added to `_UNSCOPED_EVENTS` |
| `app/core/config.py`, `env.example` | **new** `draft_docx_build_timeout_seconds` (120) |
| docs | contract, `docs/features/drafting-and-templates.md`, `docs/SOCKET_EVENTS_ARCHITECTURE.md`, `CHANGELOG.md` |

## Event and payload changes
Both events are **new**; no existing payload changes. Field tables:
`docs/contracts/backend-ai-socket-events.md`, "Drafting Word file".

- Request: `requestId`, `workspaceId`, `userId`, `draftId`, `version`,
  `title`, `content`, `format` (`markdown` | `html`), optional
  `templateId` / `templateVersion` / `templateScope`. `extra="ignore"`.
- Response: echoed ids and `version`, `status`, `reason` (failed only),
  `downloadUrl` (ready only), `docxStatus`, `placeholderCount` (once the
  text was read), `metadata` (only when a model was called).

## Design
1. **Validation.** Pydantic; ids stripped and non-empty, `version >= 0`,
   `format` lower-cased, `content` at most 2,000,000 characters. A failure is
   answered `invalid_request`, echoing whatever ids are readable. With no
   readable `requestId` nothing can be matched, so nothing is sent (logged).
2. **De-dupe.** `_draft_docx_build_requests[workspaceId][requestId] =
   {status, ts, response}` (the `document_reload` pattern). `in_flight` and
   younger than timeout + 60 s: swallow. `done` and younger than 600 s:
   re-emit the cached response. Keyed by workspace so the purge sweep erases
   it, cached links included.
3. **Deleted cases.** `is_workspace_purged`, a pending purge, or blocked
   persistence (purge in progress) → `invalid_request`, not cached. The
   handler does **not** call `cancel_pending_purge` (see Decisions). After
   the build, the same check runs again: if the purge started meanwhile,
   `delete_workspace_draft_files` runs (a failure is logged CRITICAL, the
   purge's own retry covers it) and the answer is `invalid_request`.
4. **Build** (tracked task, `capture_usage()` scope, `asyncio.timeout`):
   - text → Markdown: `html_to_markdown` for `html`, then
     `normalize_if_enabled` (in a worker thread);
   - empty → `empty_content`; else `placeholderCount = count_placeholders`;
   - with `templateId`: role from `_resolve_workspace_role`;
   - `build_docx_from_text`: no bucket → `upload_failed`; template via
     `_resolve_template(..., allow_lazy_fetch=False)` under `version_pins()`
     with the request's version pinned; render via `_render_draft_docx`
     (exception → `render_failed`); upload via `upload_draft_docx`
     (exception → `upload_failed`);
   - `TimeoutError` → `timeout`; any other exception → `render_failed`;
   - `CancelledError` (shutdown only): logged, ledger entry dropped so a
     re-send runs again, re-raised.
5. **HTML → Markdown.** A sanitising tree builder over `html.parser`:
   - dropped with their content: `script`, `style`, `iframe`, `object`,
     `embed`, `noscript`, `template`, `svg`, `math`, `head`, form controls, …;
   - kept: `p`, `h1`–`h6`, `strong/b`, `em/i`, `ul/ol/li` (nested),
     `table/thead/tbody/tfoot/tr/th/td`, `br`, `hr`, `blockquote`, `pre`,
     div-like containers; everything else (`span`, `u`, `s`, `a`, `font`, …)
     is unwrapped;
   - no attribute is read except `ol start` (an integer, top-level lists
     only);
   - `<Label>` survives escaped or raw (a raw `<Bank Name>` is otherwise
     an unknown tag); text is Markdown-escaped outside placeholders;
   - `br` → `<br>` (the renderer's line break), `hr` dropped (the renderer
     drops rules), a table without a header row gets an empty header row
     (the renderer drops it), nesting deeper than 64 is flattened.
6. **Logging.** One summary line per build: `draft_docx_build status=
   reason= format= route= template= chars= placeholders= model_called=
   delivered= duration=`. `chars()` for the content; never the title, text
   or link. Failures log exception type names only.

## Checklist against CLAUDE.md
- Role gate: the template route receives the role resolved by
  `_resolve_workspace_role`, as the drafting chat passes the workspace role.
- Long-running work: `_track_background_task`; the handler returns at once.
- Loaders / `CancelledError`: no `workspace:refresh`, no loader. Caught
  before `except Exception`, logged and re-raised.
- Emits: `self._socket_client.emit`; the bool is logged as `delivered=`. An
  undelivered answer stays cached for the backend's re-send.
- Model calls: only inside existing template-route code (router, gate0).
  `generate` results are already checked there.
- Paid calls metered: `capture_usage()` around the build; the event's
  logging context carries `workspace_id`, so the ledger bills the case;
  `_ingest_usage_metadata` drains it into `metadata` only when tokens were
  recorded.
- New caches and purge: one ledger, workspace-keyed, swept by the purge.
  Files only through `upload_draft_docx` (purged prefix).
- Stop: the event is unscoped, so a stop neither fences nor cancels it, as
  for chat. It emits no panel.
- Logging: as above; `tests/offline/verify_logging.py` passes.

## Risks and rollback
- **Risk:** an editor emits HTML the converter does not know. Unknown tags
  keep their text, so the worst case is lost formatting, not lost text.
- **Risk:** an unclosed `<iframe>` drops everything after it. Accepted: the
  editor never emits one, and dropping is the safe direction.
- **Risk:** the time cap abandons a render thread, which then finishes in
  the background. Its file is not uploaded (the await was cancelled first)
  unless the upload had already started; that file is still under the
  purged prefix.
- **Rollback:** the event is new; reverting the commit removes it. The
  backend falls back to building from `content`. `_export_draft_docx`
  behaviour is unchanged by the refactor.

## Test plan
- `tests/test_draft_html.py` (**new**): HTML gives the same Markdown, the
  same parsed blocks and the same Word structure as the equivalent Markdown;
  script/style/iframe dropped; no attribute passthrough; placeholders kept;
  escaping; header-less and ragged tables; malformed input.
- `tests/offline/verify_docx_build.py` (**new**, real handler through
  `event_handler.handle`, fake socket returning `True`, stubbed upload):
  ready (Markdown, HTML), re-send in flight, cached re-send, invalid,
  empty, upload/render/no-bucket failures, timeout, template not held,
  metadata only with a model call, purge (pending, mid-build, sweep), stop,
  log hygiene.
- `make lint`, `make test`.
- `make e2e` does not cover this event. Manual check against a connected
  backend: one Markdown and one HTML draft rebuilt, opened in Word.

## Implementation tasks
1. Converter + tests.
2. Split `_render_draft_docx`; add `build_docx_from_text`.
3. Schemas, event constants, handler, registration, setting.
4. Offline suite.
5. Contract, feature doc, architecture doc, CHANGELOG.

## Decisions
- **No `cancel_pending_purge`.** A rebuild is the backend serving a stored
  draft, often from a back-fill job (SIM-218), not the lawyer working on the
  case. Letting it revoke a purge would let a background job keep a deleted
  case's data alive. A deleted case is refused instead.
- **Unscoped event.** A stop cancels a running update. A rebuild is not part
  of one, and cancelling it would leave the backend without its answer.
- **Every answer is replayed for 10 minutes, `failed` included**, as the
  contract says. A new attempt uses a new `requestId`.
- **Template not held → standard layout, `ready`.** The request carries no
  `templateUrl`, and fetching a template the store no longer holds would
  resurrect a deleted one (the suggested-draft rule). The summary log
  records `template=missing`.
- **`placeholderCount` is omitted when the text could not be read**
  (`invalid_request`, purge refusal) rather than sent as 0, so the backend
  does not show "0 fill-ins left" for a file that does not exist.
- **No new dependency.** `beautifulsoup4`, `html5lib` and `markdownify` are
  not in `requirements.txt`; `lxml` is only a transitive pin. The stdlib
  parser is enough for editor HTML and keeps the attack surface small.
- **SIM-209 totals check not applied.** The rebuild reproduces the text the
  lawyer saved; only the structural SIM-202 normaliser runs.

## Addendum (2026-11-19): aligned with the backend's merged contract

The backend merged its side (backend #39) with different names and fields;
the AI now follows that contract (branch `fix/SIM-216-align-backend-contract`):

- Events: `draft_file_build_request` / `draft_file_build_response`.
- Request: `{requestId, purpose: rebuild | first_file, workspaceId, userId,
  draftId, content, format, formatLayout | null, timestamp}`. `version` and
  `title` are no longer required; `title`, `templateId`, `templateVersion`
  and `templateScope` are still read when present.
- `formatLayout` is applied through `DraftingAgent._apply_chosen_format`, the
  same step as a drafting turn (`build_docx_from_text(format_layout=...)`).
- Without a title, `_draft_title` takes the draft's first heading, else its
  first few non-empty lines, for the SIM-208 layout inference.
- Response: `{requestId, workspaceId, draftId, status: ok | error,
  downloadUrl?, errorCode?}` plus the additive `placeholderCount` and
  `metadata`. The error codes are unchanged.
- Python names (`DraftDocxBuildMixin`, `DRAFT_DOCX_BUILD_*`,
  `DRAFT_DOCX_BUILD_TIMEOUT_SECONDS`) are kept, so no setting changes.
