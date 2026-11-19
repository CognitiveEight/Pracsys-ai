# SIM-219: revise the latest stored version — technical design (AI side)

- **Requirements:** `specs/requirements/SIM-219-revise-latest-version.md`
- **Status:** draft (written together with the implementation; needs approval)
- **Approver:** tech lead

## Summary
When a drafting turn carries `baseDocumentUrl`, the drafting agent reads that
.docx once, before any model call. Open tracked changes or comments, or an
unreadable file, end the turn with a `draftRefusal` and no model call.
Otherwise the file's body is converted back to the drafting Markdown and
replaces the session's draft text, and the turn continues down the existing
path, so a revision is built from the lawyer's version. No model calls are
added.

## Affected code
| File | Change |
|---|---|
| `app/processing/docx_review_state.py` | **new**: `scan_review_state(bytes) -> ReviewState` (pure; contract list; namespace-aware lxml parse; 50 MB uncompressed cap) |
| `app/processing/docx_markdown.py` | **new**: `docx_to_draft_markdown(bytes) -> str` (headings, GFM tables, lists, bold/italic, line breaks) |
| `app/processing/draft_revision_base.py` | **new**: `fetch_base_docx` (streamed httpx, timeout + size cap), `load_revision_base(url) -> RevisionBase` (outcome, markdown, counts), refusal messages |
| `app/core/config.py` | **new** `draft_base_download_timeout_seconds` (30), `draft_base_max_bytes` (25 MB) |
| `app/langgraph/drafting_agent.py` | `_handle_turn`: adopt the base before any branch; echo `draftId` / `baseVersion` on a copy of `draftDocument`; **new** `_adopt_revision_base`, `_as_int`, `_first_heading` |
| `app/socket/handlers/chat.py` | `_handle_drafting_assistant_message`: pass the three fields; put `draftRefusal` on `ai_response` (and drop `draftDocument`); skip the chips model call on a refusal |
| `app/socket/schemas.py` | `UserMessageRequestPayload.draftId/baseVersion/baseDocumentUrl` (snake_case aliases); `DraftDocumentPayload.draftId/baseVersion`; **new** `DraftRefusal`; `AIResponsePayload.draftRefusal` |
| docs | `docs/contracts/backend-ai-socket-events.md`, `docs/socket-events.md`, `docs/features/drafting-and-templates.md` |

## Event and payload changes
Inbound `user_message_request` (DRAFTING_ASSISTANT), all optional, additive:
`draftId: str`, `baseVersion: int`, `baseDocumentUrl: str`.

Outbound `ai_response`, all optional, additive:
- `draftDocument.draftId`: echo of the request's `draftId` (any turn that
  returns a `draftDocument`).
- `draftDocument.baseVersion`: the version the draft was built from; only
  on a delivery turn that adopted a base.
- `draftRefusal: {code: "open_changes" | "base_unreadable", trackedChanges?,
  comments?}`: instead of `draftDocument`. `draftStatus` is `ready`,
  `content` is the lawyer-facing message, no `suggestionChips`.

## Design
1. **Where it hooks.** `DraftingAgent._handle_turn` loads the session, pins
   the template version, then (new) calls `_adopt_revision_base` when
   `baseDocumentUrl` is present. This is before the greeting fast-path, the
   LLM router (`_llm_route_turn`) and every handler, so a refusal makes no
   model call and leaves the session unsaved and unchanged.
2. **Read.** `load_revision_base` → `fetch_base_docx`: http(s) only, one
   streamed GET under `asyncio.timeout`, status must be 200, aborts past
   `draft_base_max_bytes` (declared length or bytes read). Any failure is
   `base_unreadable` with a fixed reason code. The URL is only in
   `request_fields`; it is never put in `session_state` and is logged only
   via `safe_url`.
3. **Check.** `scan_review_state` (in a worker thread): reads only the
   contract's parts, bounded to 50 MB uncompressed in total (each read is
   itself capped, so a lying zip header cannot expand further). lxml parser
   with entities, DTDs and network off. Counts elements by fully qualified
   tag, so text that merely says "w:ins" or an `ins` in another namespace
   does not count. Not a zip, no `word/document.xml`, malformed XML or over
   the cap → unreadable.
4. **Convert.** `docx_to_draft_markdown`: body blocks in reading order
   (content controls opened). `Heading N`/`Title` → `#`; tables → GFM (first
   row as header, cell paragraphs joined with `<br>`, `|` escaped, a
   horizontally merged cell written once); `List Bullet` styles or bullet
   numbering → `-`; Word numbering → its visible marker as text (reusing
   `FileProcessor._docx_list_marker`); bold/italic runs → `**`/`*`; manual
   breaks → newlines. Headers, footers, footnotes and images are not
   carried. An empty body is `base_unreadable`.
5. **Adopt.** The converted text (through `normalize_if_enabled`) replaces
   `last_draft_markdown`; `current_draft_version` becomes `baseVersion`, so
   the next AI version is vN+1; a missing `draft_title` comes from the first
   heading; a status outside the active set becomes `ready`. The turn then
   runs unchanged: the router previews the lawyer's text, `revise_draft` →
   `_render_and_attach(mode_override="revision")` sends it as the previous
   draft, and `_restore_regressed_values` uses it as the source of truth.
   Finalize and translate also work from it.
6. **Echo.** At the end of `_handle_turn`, `draftDocument` is copied before
   `draftId` / `baseVersion` are added, so the persisted session record is
   unchanged.
7. **Logging.** One line per turn carrying a base:
   `draft_revise_from_base outcome=revised|adopted|open_changes|base_unreadable tracked= comments= chars=`
   (refusals add `reason=`, a fixed code). Counts only.

## Checklist against CLAUDE.md
- Role gate: n/a. The drafting path's role handling is unchanged.
- Long-running work: the download is bounded (30 s default); the scan and
  conversion run in `asyncio.to_thread`. No new tasks.
- Loaders / `CancelledError`: no loaders. `_adopt_revision_base` catches
  `asyncio.CancelledError` around the read, logs it and re-raises;
  `fetch_base_docx` re-raises it explicitly before its catch-all.
- Emits: unchanged path (`emit_ai_response` from the chat handler).
- Model calls: none added; a refusal also skips the chips call.
  `is_generation_error`: no new `generate` call sites.
- Paid calls metered: n/a (no paid API; the GET is to the backend's store).
- New caches and purge: none. `session_state` gains `base_version` (an int)
  inside the existing drafting-session cache, which the purge already
  reaches.
- Ingestion/deletion: n/a.
- `mutations_allowed`: drafting does not consult it today; unchanged.
- Logging: counts, outcome codes and `safe_url` only. No text, no titles, no
  full URL.

## Risks and rollback
- **Risk:** the Word → Markdown read loses formatting the lawyer applied
  (alignment, fonts, underline, images, headers). The text, headings, tables
  and lists survive; the AI's renderer re-applies its own layout, as it does
  for every version today. Format-preserving revision is SIM-220's domain.
- **Risk:** a lawyer-saved file without heading styles (direct bold
  formatting) reads as bold paragraphs, not headings. The draft still
  revises correctly; only the Markdown structure is flatter.
- **Risk:** a resolved comment blocks revision. Intended until SIM-214/215.
- **Rollback:** the backend stops sending `baseDocumentUrl`; every turn then
  behaves exactly as before. Revert the commit if needed.

## Test plan
- `tests/test_docx_review_state.py` (**new**): clean; `w:ins`/`w:del`; a
  move; `w:rPrChange` only; change only in a header; comments only; empty
  comments part; namespace awareness; invalid zip; no document part;
  malformed XML; a 60 MB zip bomb; configurable cap; empty bytes.
- `tests/test_draft_revise_from_base.py` (**new**): conversion (headings,
  tables, lists, emphasis, a python-docx-saved file); drafting path with the
  download and model stubbed (clean, open changes, unreadable, failed
  download, no base, `draftId` only); `fetch_base_docx` with
  `httpx.MockTransport` (ok, 403, oversize, bad scheme).
- `tests/offline/verify_draft_revise_from_base.py` (**new**): the real
  `_handle_drafting_assistant_message` with a fake socket; schema aliases;
  the emitted `ai_response` validates against `AIResponsePayload`.
- `make lint`, `make test`. `make e2e` does not cover drafting; a manual
  check against a connected backend is needed once it sends the fields.

## Implementation tasks
1. Scanner + tests.
2. Converter + fetch + tests.
3. Agent hook, chat handler, schemas.
4. Offline suite.
5. Docs, CHANGELOG.

## Decisions
- **No `view`-collaborator gate added.** The brief asked to keep the existing
  drafting behaviour for view collaborators. Today
  `_handle_drafting_assistant_message` is dispatched before
  `mutations_allowed` is consulted and applies no gate, so drafting is open
  to every participant; that stays as is. Whether the backend lets a viewer
  create a new version is the backend's call.
- **An extra log outcome, `adopted`.** The base is adopted on every turn
  that carries it, but only a delivery turn is a revision. A turn the router
  answers in chat logs `outcome=adopted` rather than claiming `revised`.
- **The base is adopted for every action, not only `revise_draft`.** If the
  backend sends the link on every turn, chat answers, finalize and translate
  must also see the lawyer's text.
- **`baseVersion` is echoed only on the delivery turn.** On other turns the
  returned `draftDocument` is the AI's earlier record, not built from the
  base.
- **Own download helper.** `DocumentProcessor._download_file` has a 500 MB
  cap, a 300 s timeout and 5 retries — right for evidence, wrong for a
  short-lived link read inside a chat turn. `fetch_base_docx` streams with a
  25 MB / 30 s bound and no retry (an expired link does not heal).
- **New converter.** The existing .docx readers (`extract_text_from_docx`,
  `_docx_marked_text`) flatten tables to ` | ` lines and drop heading levels.
  `docx_to_draft_markdown` is the inverse of `_render_with_defaults` and
  reuses `FileProcessor`'s list-marker logic.
- **The refusal turn is not saved.** Nothing in the session changes, so the
  next turn starts from the same state.

## Addendum (2026-11-19): `latestVersion` from the backend

The backend sends `user_message_request.latestVersion {version, url,
expiresAt, openChanges, sha256}` instead of the proposed `baseDocumentUrl` /
`baseVersion` (branch `fix/SIM-216-align-backend-contract`):

- `_revision_base_fields` (`app/socket/handlers/chat.py`) maps it to the
  agent's `baseDocumentUrl`, `baseVersion`, plus `baseRequired`,
  `baseOpenChanges` and `baseSha256`. The old fields are read only when
  `latestVersion` is absent.
- With `latestVersion` the base is required: no `url` refuses with
  `base_unreadable` without a download, so the AI's own copy is never used.
- `openChanges` (read as bool, count or list) refuses with `open_changes`
  before any download; the refusal carries no counts.
- `sha256` (hex or base64) must match the downloaded bytes, else
  `base_unreadable` (`sha256_mismatch` in the log).
- `expiresAt` is not checked locally: an expired link fails the download and
  refuses the same way, without clock-skew false refusals.
