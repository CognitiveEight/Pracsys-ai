# SIM-219: revise the latest stored version — acceptance criteria (AI side)

- **Requirements:** `specs/requirements/SIM-219-revise-latest-version.md`
- **Design:** `specs/technical-design/SIM-219-revise-latest-version.md`

All fixtures are fictional.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a .docx with no review state | scanned | readable, 0 tracked changes, 0 comments | `tests/test_docx_review_state.py::test_clean_document_has_no_open_changes` |
| AC-2 | a .docx with `w:ins` and `w:del` | scanned | 2 tracked changes, open | `tests/test_docx_review_state.py::test_insertion_and_deletion_are_counted` |
| AC-3 | a move (`w:moveFrom` / `w:moveTo`) | scanned | 2 tracked changes | `tests/test_docx_review_state.py::test_move_is_counted` |
| AC-4 | only a formatting change (`w:rPrChange`) | scanned | 1 tracked change, open | `tests/test_docx_review_state.py::test_formatting_change_alone_is_an_open_change` |
| AC-5 | a tracked change only in a header part | scanned | 1 tracked change; the body alone is clean | `tests/test_docx_review_state.py::test_change_only_in_a_header_is_found` |
| AC-6 | comments only (two `w:comment`) | scanned | 0 tracked, 2 comments, open | `tests/test_docx_review_state.py::test_comment_only_counts_every_comment` |
| AC-7 | "w:ins" in text, or `ins` in another namespace | scanned | 0 tracked changes | `tests/test_docx_review_state.py::test_parsing_is_namespace_aware_not_substring` |
| AC-8 | not a zip / no `word/document.xml` / malformed XML | scanned | unreadable | `tests/test_docx_review_state.py::test_invalid_zip_is_unreadable`, `::test_zip_without_document_part_is_unreadable`, `::test_malformed_xml_is_unreadable` |
| AC-9 | a part over 50 MB uncompressed (zip bomb) | scanned | unreadable `too_large`, not expanded | `tests/test_docx_review_state.py::test_oversized_part_is_unreadable_without_expanding_it`, `::test_oversized_part_is_rejected_before_decompression` |
| AC-10 | a lawyer-version .docx with headings, a table and lists | converted | Markdown keeps `#` headings, a GFM table with separator, list items and bold | `tests/test_draft_revise_from_base.py::test_conversion_keeps_headings_tables_and_text`, `::test_conversion_keeps_lists_and_emphasis`, `::test_conversion_of_a_word_saved_file` |
| AC-11 | a clean base whose text differs from the AI's copy | a revision turn runs | the model's prompt has the lawyer's sentence and not the AI-only one; the result keeps it; `draftId` and `baseVersion` echoed; version = base + 1 | `tests/test_draft_revise_from_base.py::test_clean_base_is_revised_from_the_lawyers_version`, offline `revised` |
| AC-12 | a base with open changes | a turn runs | `draftRefusal {open_changes, counts}`; no `draftDocument`; `draftStatus` ready; no model call (router, render or chips); session unchanged | `tests/test_draft_revise_from_base.py::test_open_changes_refuse_without_a_model_call`, offline `refusal` |
| AC-13 | an unreadable base, or a failed download | a turn runs | `draftRefusal {base_unreadable}`; no model call | `tests/test_draft_revise_from_base.py::test_unreadable_base_refuses_without_a_model_call`, `::test_failed_download_refuses_as_unreadable`, offline `unreadable` |
| AC-14 | no `baseDocumentUrl` | a turn runs | nothing downloaded; revised from the AI's copy; no new fields | `tests/test_draft_revise_from_base.py::test_without_base_url_the_turn_is_unchanged`, offline `legacy` |
| AC-15 | `draftId` without a base | a turn returns a `draftDocument` | `draftId` echoed; the persisted record is unchanged | `tests/test_draft_revise_from_base.py::test_draft_id_is_echoed_without_a_base` |
| AC-16 | a download that is 403, oversize, or not http(s) | fetched | `BaseUnreadable` with `http_403` / `too_large` / `bad_url` | `tests/test_draft_revise_from_base.py::test_download_*` |
| AC-17 | the new request fields in camelCase or snake_case | parsed | both parse; absent fields are None; emitted payloads validate | offline `schema`, every offline group's `payload validates` |
| AC-18 | a real backend sending `baseDocumentUrl` | the lawyer edits in the editor, then asks for a change in chat | the new version keeps the editor change | **manual**: needs the backend change and model calls |

## Negative and edge cases
- Stop / disconnect: drafting chat turns are fenced, not cancelled; a
  cancellation during the read is logged and re-raised.
- Duplicate request: no dedupe on chat turns (unchanged); a re-sent turn
  reads the base again.
- `view` collaborator: unchanged (drafting has no viewer gate today).
- Model error/timeout after a clean base: unchanged handling in
  `_render_and_attach`.
- Base adopted but the router answers in chat: logged `outcome=adopted`; the
  session now holds the lawyer's text.

## Non-functional
- Logs carry counts, outcome and reason codes, and `safe_url` only
  (`tests/offline/verify_logging.py` passes; the offline suite's log was
  checked for the fake link's query string and the lawyer's sentence: 0
  hits).
- No new paid calls.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] Manual drafting check (AC-18) once the backend sends the fields
- [ ] `CHANGELOG.md` updated; payload change called out
