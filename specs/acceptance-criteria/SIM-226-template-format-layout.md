# SIM-226: Template drafts keep their page layout — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | an indent-laid-out template (0.42/0.08 in margins) and the court or letterhead format | applied with `keep_body_layout` | margins, page size, paragraph indents, alignment, run fonts and sizes unchanged; `body_layout=kept` | `test_template_draft_keeps_page_layout` |
| AC-2 | the same template | letterhead / court format applied | letterhead header present; court block at the top; page number in the footer | `test_template_draft_still_gets_letterhead_court_and_page_number` |
| AC-3 | a draft with no template | court format applied | format margins applied; `body_layout=applied` | `test_no_template_draft_still_gets_format_page_settings` |
| AC-4 | each render route | the chosen format is applied | `keep_body_layout` is false only for `defaults` | `test_chosen_format_keeps_layout_on_every_template_route` |
| AC-5 | any run | summary logged | `body_layout` and `body_skipped` in the summary fields | `test_summary_fields_report_body_layout` |
| AC-6 | the real lease template (manual, not committed) | old vs new formatter | old: margins 1.5/1.0 with 3.12+3.45 in indents (negative width); new: template margins kept | manual check, 2026-11-25 |
