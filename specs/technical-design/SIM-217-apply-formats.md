# SIM-217: Apply chosen formats — technical design

- **Requirements:** `specs/requirements/SIM-217-apply-formats.md`
- **Status:** draft — written with the implementation; needs approval

## Design
`app/processing/draft_format.apply_format_layout(docx_bytes, format_layout,
*, fetch_image=None) -> FormatResult` (**new**) is pure: it opens an existing
.docx, applies `formatting`, and returns the bytes plus counts. It never
changes body text except to insert the court block, and it does not read
Markdown, so it can re-apply a format to a lawyer-edited file later. It
raises only on unreadable .docx bytes; every bad setting is skipped and
reported (`unknown_keys`, `invalid_values`: key paths, never values).

- **Page** (any type). Size (A4/Legal/Letter) and orientation (swap width and
  height) on every section; margins ("1in", "2.54cm", "25mm", "72pt", bare
  number = inches; out of range ignored). The font goes on Normal, the
  heading/Title styles, any style that sets its own font, the document
  defaults and runs with explicit fonts — all four slots (ascii, hAnsi,
  eastAsia, cs), theme fonts removed; symbol fonts are left alone. The size
  goes on Normal and on body runs whose explicit size is the body's usual one
  (deliberately larger/smaller runs keep theirs). Line spacing on Normal and
  on body paragraphs with an explicit line spacing. `justified` justifies
  Normal and body paragraphs that are not headings, not effectively
  centre/right, and have no manual line break (Word stretches those lines);
  `left` turns justified body paragraphs left.
- **Watermark.** Word's own VML text watermark
  (`PowerPlusWaterMarkObject`, rotated 315°, silver, half opacity) in every
  header in use. An existing one is removed first, so re-applying never
  doubles it; `None` removes it.
- **Page number.** Existing page-number-only paragraphs (a PAGE field plus at
  most "Page … of …") are removed from every header/footer, then a PAGE field
  is added at the chosen position in every header (top) or footer (bottom) in
  use. `None` removes; an unknown value leaves the file as it was.
- **Letterhead** (`type: "letterhead"`). Section 0's header and footer
  definitions (default, first, even) are cleared and rewritten; later sections
  link to it; odd/even headers are switched off. One shown block → one
  paragraph; more → a borderless 3-column table keeping Left/Center/Right in
  place. `header…Show…` false hides a block (unset = shown); `header…Html…`
  present (even "") replaces text and bold, through an HTML parser that keeps
  only b/strong, i/em, u, br, p/div and drops script/style content and every
  attribute. `headerType: "image"` uses `headerImageUrl` full width, else the
  per-block `headerImage*`; `footerType: "image"` uses `footerImageUrl`.
  `pageApplication: "first"` writes the first-page header/footer
  (`titlePg`) and leaves the others empty.
- **Court** (`type: "court"`). Blocks in order courtHeader, jurisdiction,
  documentType, caseNumber, customFields (`text`, else "label: value"), each
  with its own font, size, bold, italic, underline, uppercase, line spacing and
  alignment (centre when unset), inserted before the first body element. A
  block is skipped when it already appears in the first 8 (+ block count)
  non-empty body paragraphs: case/punctuation/space-insensitive, contained
  match, `____`/`...` blanks matching anything short. That also makes a
  second application a no-op. The header is not touched.
- **Type.** `court` applies only `court`, `letterhead` only `letterhead`;
  a missing type applies whichever section is present.
- **Images** only through `fetch_image`. The default,
  `app/processing/format_images.fetch_format_image` (**new**): https only, no
  redirects, host must resolve to a public address, 10 s, 5 MB, PNG/JPEG by
  magic bytes; any failure returns None and logs `safe_url` only. The
  formatter re-checks the magic bytes. A failed image skips that block and is
  counted.

**Wiring** (`app/langgraph/drafting_agent.py`):
- `UserMessageRequestPayload.draftOrigin` (**new**, optional, opaque);
  `chat.py` threads `formatLayout` and `draftOrigin` into `request_fields`.
- `_handle_turn` picks the request's format (`formatLayout`, else
  `draftOrigin.formatLayout`), else the session's `format_layout`, into the
  context variable `_TURN_FORMAT_LAYOUT` (reset by `handle_turn`). Before the
  session is saved, the request's format is stored **without image links**
  (`without_image_links`): the links are short-lived signed links and the
  backend sends the format on every message.
- `_export_draft_docx` calls `_apply_chosen_format` after whichever route
  rendered the file (template_fill, llm_diff, heuristic_fill, skeleton_clone,
  defaults) and before `upload_draft_docx`. Running last is what gives the
  agreed precedence: format page settings override the template's and the
  SIM-208 layout, a letterhead replaces the header/footer, the court block
  sits on top. It runs in a worker thread; `CancelledError` is logged and
  re-raised; any other failure is logged and the unformatted file uploaded.
  One `draft_format applied route= type= page= watermark= page_number=
  letterhead_blocks= court_blocks= court_duplicates= images_ok=
  images_failed= unknown_keys= [names] invalid=` line per export.

## Decisions
- Image links are never persisted: a later turn that omits `formatLayout`
  applies the stored format without its images. The backend sends the format
  on every drafting message, so this only affects a turn that omits it.
- Unknown key **names** (schema names, sanitised to `[A-Za-z0-9_.[]]`, at most
  10) are logged to find the cut-off field names; values never are.
- Guessed fields: `headerSpacing*` is read as line spacing
  (`headerLineSpacing*` also accepted); footer blocks accept the same styling
  keys as header blocks (`footerFontFamily*`, …); a court block without
  `align` is centred.

## Checklist against CLAUDE.md
- Role gate, loaders, emits, caches, purge: n/a (session state already lives
  in the purged workspace cache).
- Model calls: none added. Provider SDKs: none.
- Logging: counts, enum values, key names and `safe_url` only; no letterhead
  text, names, addresses or links.
- Draft upload only through `app/storage/draft_files.upload_draft_docx`.

## Rollback
Reverting the commit returns to ignoring `formatLayout`. No data migration;
a stored `format_layout` in a session is simply unused.

## Test plan
`tests/test_draft_format.py` (pure formatter, export path with stubbed upload,
session storage). `make e2e` is not affected by the formatter itself, but the
drafting export path changed: verify one draft per format type manually
against a connected backend and open the files in Word.
