# SIM-220: AI revisions as tracked changes — acceptance criteria (AI side)

- **Requirements:** `specs/requirements/SIM-220-tracked-change-revisions.md`
- **Design:** `specs/technical-design/SIM-220-tracked-change-revisions.md`

All fixtures are fictional.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a base and a revision differing by one word | written | exactly that word in `w:del` and its replacement in `w:ins`; the rest of the paragraph untouched | `tests/test_docx_track_changes.py::test_one_word_changed_is_tracked_alone` |
| AC-2 | a paragraph replaced by a new one | written | the old paragraph's runs and mark deleted; the new paragraph's runs and mark inserted | `::test_paragraph_inserted_and_deleted` |
| AC-3 | a table cell changed, a row added, a row removed | written | cell word diff; `w:trPr/w:ins` row; `w:trPr/w:del` row with its text deleted | `::test_table_cell_changed_row_added_and_row_removed` |
| AC-4 | inserted words next to a Georgia 13 pt run, with `**bold**` | written | inserted runs carry Georgia 13 pt; the bold word has `w:b` | `::test_inserted_runs_copy_neighbour_font_and_markdown_bold` |
| AC-5 | an inserted `<Label>` placeholder | written | inserted as text | `::test_placeholder_is_inserted_as_text` |
| AC-6 | a Word-numbered list with an item changed and one added | written | the new item copies the list style; no typed number | `::test_word_numbered_list_keeps_word_numbering` |
| AC-7 | a base with header, footer, comments part, section properties | written | every other part byte-identical; body `sectPr` identical | `::test_headers_footers_comments_and_section_properties_are_untouched` |
| AC-8 | a mixed revision | every change accepted / rejected | accept-all text = revision text; reject-all text = base text | `::test_accept_all_gives_the_revision_and_reject_all_gives_the_base` |
| AC-9 | the written file | scanned by SIM-219 | tracked changes counted, open (next revision refused) | `::test_output_is_counted_as_open_changes_by_the_scanner` |
| AC-10 | the written file | opened | re-opens with python-docx; every `w:ins`/`w:del` has a unique id, author "SimplerToday AI", ISO UTC date | `::test_output_reopens_and_every_change_has_unique_id_author_and_date` |
| AC-11 | a revision unrelated to the base, or a table's column count changed, or not a .docx | written | no file; `too_many_changes` / `table_columns_changed` / `not_a_docx` | `::test_completely_different_text_falls_back`, `::test_column_count_change_falls_back`, `::test_not_a_docx_falls_back` |
| AC-12 | 10 blocks, cut-off 0.4 | 4 / 5 blocks changed | 4 applied; 5 falls back; 5 applied at 0.5 | `::test_changed_ratio_threshold_both_sides` |
| AC-13 | B has a totals marker the file lacks | one word revised | only that word tracked | `::test_base_markdown_differences_are_not_shown_as_changes`; path: `tests/test_draft_tracked_revision.py::test_setting_on_uploads_tracked_changes` |
| AC-14 | setting on, revision from a base | the turn runs | the tracked file is uploaded through `upload_draft_docx`; `trackedChanges: true`; no clean render; payload validates; base not kept after the turn | `tests/test_draft_tracked_revision.py::test_setting_on_uploads_tracked_changes` |
| AC-15 | the tracked path vs any other | the revision prompt is built | the verbatim instruction is present only on the tracked path | `::test_verbatim_instruction_only_on_the_tracked_path` |
| AC-16 | setting off | the turn runs | the SIM-219 clean file; no `trackedChanges` | `::test_setting_off_is_the_sim219_clean_file` |
| AC-17 | the writer raises or returns no file | the turn runs | clean file; no `trackedChanges`; no notice | `::test_tracked_step_raising_falls_back_to_the_clean_file`, `::test_tracked_step_returning_no_file_falls_back` |
| AC-18 | a full rewrite | the turn runs | clean file; the reply ends with the notice (not in `draftDocument.content`) | `::test_full_rewrite_comes_as_a_clean_file_with_a_notice` |
| AC-19 | open changes | refused | the message says they may include the AI's own suggested edits | `::test_open_changes_message_mentions_the_ais_own_edits` |
| AC-20 | real drafts in ONLYOFFICE and Word | the lawyer accepts / rejects | the editor shows the AI's changes as a colleague's; tables and numbering render | **manual** |

## Negative and edge cases
- Translation request: every block changes → `too_many_changes`, clean file
  and notice (by design).
- No change at all: the base file is returned unchanged, no `trackedChanges`.
- Stop / disconnect: drafting turns are fenced, not cancelled; a
  cancellation in the tracked step is logged and re-raised.
- Upload failure or no bucket: falls back to the clean export path.

## Non-functional
- Logs: one `draft_tracked_changes` line per revision with counts and fixed
  codes; no text, titles or URLs (`tests/offline/verify_logging.py` passes).
- No new paid calls.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] Manual editor check (AC-20) before enabling the setting
- [ ] `CHANGELOG.md` updated; payload change called out
