# SIM-228: .doc templates — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | `soffice` writes a valid .docx | `convert_doc_to_docx` | returns those bytes | `test_converts_with_soffice` |
| AC-2 | a renamed .docx | converted | returned unchanged, no LibreOffice call | `test_a_renamed_docx_is_returned_unchanged` |
| AC-3 | no `soffice`; setting off; empty/oversized input | converted | `None` | `test_no_soffice_returns_none`, `test_disabled_returns_none`, `test_empty_or_oversized_input_returns_none` |
| AC-4 | LibreOffice exits non-zero / writes nothing / writes junk | converted | `None` | `test_failed_conversion_returns_none[*]` |
| AC-5 | LibreOffice hangs with a child process; or exits leaving a child; or the converter is busy | timeout / exit / lock wait | `None` within seconds and the child killed; a leftover child after success killed; a busy converter gives up after the timeout | `test_timeout_kills_the_process_group`, `test_leftover_child_is_killed_after_a_normal_exit`, `test_busy_converter_gives_up_after_the_timeout`, `test_conversions_run_one_at_a_time`, `test_temp_dirs_are_removed[*]` |
| AC-6 | a `.doc` template | ingested | text extracted from the .docx (line breaks kept); .docx stored as skeleton; payload `extension=doc`, `doc_conversion=ok` | `test_doc_template_is_converted_and_stored_as_docx` |
| AC-7 | conversion fails | ingested | today's behaviour; no skeleton; `doc_conversion=failed` | `test_failed_conversion_keeps_todays_behaviour` |
| AC-8 | a `.doc` cached before SIM-228 | next sync with a converter (incl. the connect-time re-send of the same version and hash) | re-downloaded, converted, skeleton stored, re-profiled once, not again | `test_cached_doc_without_skeleton_is_reingested_once`, `test_reingested_doc_is_not_reingested_again`, `test_global_sync_reingests_a_cached_doc_once`, `test_needs_doc_conversion` |
| AC-9 | no converter, or already `ok`/`failed` | next sync | cached text reused, nothing downloaded | `test_cached_doc_is_not_reingested[*]` |
| AC-10 | a `.doc` template with no skeleton at draft time | lazy backfill | converted .docx stored; failure → `None` | `test_lazy_backfill_converts_a_doc`, `test_lazy_backfill_gives_up_when_conversion_fails` |
| AC-11 | LibreOffice installed | real .docx → .doc → .docx | text survives | `test_real_round_trip` (skipped without LibreOffice) |
| AC-12 | the two reference `.doc` templates (manual, not committed) | converted in `python:3.12-slim` + LibreOffice | both convert in < 2 s; SHA: 786 lines, 7 tables, 219 numbered paragraphs; will: 251 lines, 2 tables, 84 numbered | manual check, 2026-11-25 |
