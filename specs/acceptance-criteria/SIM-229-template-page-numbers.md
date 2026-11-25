# SIM-229 Acceptance criteria

| AC | Criterion | Check |
|---|---|---|
| AC1 | A frozen "Page 3 of 16" in a footer (gallery or plain) becomes PAGE and NUMPAGES fields, wording and bold kept | `test_frozen_page_x_of_y_becomes_live_fields` |
| AC2 | A bare "3 of 16" is repaired only inside the page-number gallery | `test_bare_number_is_repaired_only_inside_a_page_number_gallery` |
| AC3 | Other footer text and body text are untouched | `test_other_footer_text_is_never_touched`, `test_body_text_is_never_touched` |
| AC4 | A template draft with a bottom page-number format keeps its "Page X of Y" and shows one number | `test_template_draft_keeps_its_page_x_of_y_and_adds_no_second_number` |
| AC5 | A no-template draft takes the format's number once; emptied content controls are removed | `test_no_template_draft_takes_the_formats_page_number_once` |
| AC6 | `none` removes it; a top position moves it to the header | `test_page_number_none_removes_the_repaired_number`, `test_format_page_number_elsewhere_replaces_the_templates` |
| AC7 | The log summary reports the repair count | `test_summary_reports_the_repair` |
| AC8 | A template draft with no format is repaired; a `defaults` draft is returned as is | `test_template_draft_without_a_format_is_repaired` |
| AC9 (manual) | In ONLYOFFICE, a draft from a template with a flattened "Page X of Y" footer reads Page 1..N of N with one number per page | manual, after deploy |
