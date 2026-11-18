# SIM-216: Word file from stored draft text — acceptance criteria

- **Requirements:** `specs/requirements/SIM-216-docx-build-event.md`
- **Design:** `specs/technical-design/SIM-216-docx-build-event.md`

All fixtures are fictional.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a valid Markdown request | handled | one response: `status` and `docxStatus` `ready`, the uploaded link, ids and `version` echoed, `placeholderCount` = distinct `<Label>`s, no `reason`, no `metadata`; the upload is a .docx under the workspace with the draft's table | `verify_docx_build.py` [1] |
| AC-2 | editor HTML with headings, a nested list, a table and placeholders | converted | the Markdown, the parsed blocks and the Word structure equal those of the equivalent Markdown | `tests/test_draft_html.py::test_html_converts_to_the_equivalent_markdown`, `::test_same_blocks_as_markdown_tables_headings_nested_lists`, `::test_same_word_file_structure_as_markdown`; `verify_docx_build.py` [2] |
| AC-3 | HTML with `script`, `style`, `iframe`, `noscript` | converted | their content is gone; surrounding text kept | `tests/test_draft_html.py::test_script_style_iframe_dropped_with_their_content` |
| AC-4 | HTML with links, inline styles, event handlers, images, unknown tags | converted | text kept, no attribute value in the output | `tests/test_draft_html.py::test_no_attribute_passthrough_and_unknown_tags_unwrapped` |
| AC-5 | placeholders written escaped (`&lt;X&gt;`) or raw (`<X>`) | converted | every `<Label>` survives and is counted | `tests/test_draft_html.py::test_placeholders_survive_escaped_or_raw` |
| AC-6 | a re-send under the same `requestId` while the build runs | handled | swallowed: one build, one response | `verify_docx_build.py` [3] |
| AC-7 | a re-send within 10 minutes of the answer (ready or failed) | handled | the same response again, no rebuild | `verify_docx_build.py` [4] |
| AC-8 | missing/invalid fields, unknown `format`, over-long content | handled | `failed` / `invalid_request` with the readable ids; nothing built | `verify_docx_build.py` [5] |
| AC-9 | no readable `requestId` | handled | nothing emitted (logged) | `verify_docx_build.py` [5] |
| AC-10 | blank Markdown, or HTML with no text | handled | `failed` / `empty_content` | `verify_docx_build.py` [6] |
| AC-11 | the upload fails / the render raises / no bucket | handled | `upload_failed` / `render_failed` / `upload_failed`; no link; `placeholderCount` still sent | `verify_docx_build.py` [7] |
| AC-12 | a build longer than `DRAFT_DOCX_BUILD_TIMEOUT_SECONDS` | handled | `failed` / `timeout`, answered at the cap | `verify_docx_build.py` [8] |
| AC-13 | a `templateId` the AI does not hold | handled | built with the standard layout, `ready`, no model call | `verify_docx_build.py` [9] |
| AC-14 | a build that calls the model | handled | `metadata` with tokens, price and `messageId`; absent otherwise | `verify_docx_build.py` [10], [1] |
| AC-15 | a case pending purge | handled | `invalid_request`; the pending purge is not revoked | `verify_docx_build.py` [11] |
| AC-16 | the purge starts while a build runs | the build finishes | the workspace's draft files are deleted again; `invalid_request`; nothing cached | `verify_docx_build.py` [11] |
| AC-17 | a completed build | the workspace is purged | the ledger, with its cached link, is erased | `verify_docx_build.py` [11] |
| AC-18 | a build in flight | the case's run tasks are cancelled (Stop) | the build is not among them and still answers | `verify_docx_build.py` [12] |
| AC-19 | any build | logged | one summary line; no title, draft text or link anywhere in the logs | `verify_docx_build.py` [13], `tests/offline/verify_logging.py` |
| AC-20 | a real backend connection | "Rebuild Word file" on a Markdown draft and on an editor (HTML) draft | both files open in Word with real tables, headings and lists | **manual**: needs a live backend |

## Negative and edge cases
- Malformed HTML (unclosed tags, `ul` inside `p`, 500-deep nesting): text
  kept, no exception (`test_malformed_and_empty_input`).
- An unclosed dropped element (`<iframe>`) drops only what follows it
  (`test_unclosed_dropped_element_drops_only_what_follows`).
- Markdown syntax typed as text in HTML (`1.`, `#`, `|`, `*`) stays text
  (`test_markdown_syntax_in_text_is_escaped`).
- `view` collaborator: n/a; the backend sends `userId` as the owner and
  gates the button.
- Disconnect: the emit's delivery bool is logged; the cached answer is
  replayed to the backend's re-send.

## Non-functional
- No new dependency; no new paid call (the template route's model calls are
  existing ones).
- `tests/offline/verify_logging.py` passes.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] Manual rebuild check against a connected backend (AC-20); `make e2e`
  does not cover this event
- [ ] `CHANGELOG.md` updated; new socket events called out
- [ ] Backend sign-off on the contract
