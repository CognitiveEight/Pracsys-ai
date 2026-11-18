# SIM-202: Drafting Assistant Word export fidelity — acceptance criteria

- **Requirements:** `specs/requirements/SIM-202-drafting-docx-fidelity.md`
- **Design:** `specs/technical-design/SIM-202-drafting-docx-fidelity.md`

All fixtures are fictional.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | draft Markdown whose table rows are separated by blank lines | normalised | the rows form one contiguous GFM table with one separator row | `tests/test_draft_markdown.py::test_table_rows_joined` |
| AC-2 | a table with no separator row, or rows with too few or too many cells | normalised | a separator is inserted and every row has the header's column count | `tests/test_draft_markdown.py::test_table_separator_and_padding` |
| AC-3 | escaped placeholders `\<Bank Name\>` | normalised | they read `<Bank Name>` | `tests/test_draft_markdown.py::test_placeholders_unescaped` |
| AC-4 | `####` and deeper headings, and stand-alone `---` lines | normalised | headings are `###` and the `---` lines are gone | `tests/test_draft_markdown.py::test_headings_and_rules` |
| AC-5 | sub-clauses written as `  - (a) …` or `  (a) …` under a list item | normalised and rendered | they are nested list items at level 2 in the .docx | `tests/test_draft_markdown.py::test_nested_subclauses`, `tests/test_docx_generator.py::test_nested_list_levels` |
| AC-6 | any draft | normalised twice | the output equals the first normalisation | `tests/test_draft_markdown.py::test_idempotent` |
| AC-7 | fenced code containing `|` and `---` | normalised | the fenced block is unchanged | `tests/test_draft_markdown.py::test_fenced_code_untouched` |
| AC-8 | Markdown with a GFM table | rendered (defaults path) | the .docx contains a Word table with the cell text, a bold repeating header row and full-width columns | `tests/test_docx_generator.py::test_table_rendered_defaults` |
| AC-9 | Markdown with a GFM table and a template | rendered (skeleton path) | the .docx contains a Word table | `tests/test_docx_generator.py::test_table_rendered_template_skeleton` |
| AC-10 | a draft with `<Label>` placeholders | rendered | each placeholder is its own highlighted run, and `placeholderCount` counts the distinct labels | `tests/test_docx_generator.py::test_placeholders_highlighted`, `tests/test_draft_markdown.py::test_count_placeholders` |
| AC-11 | heading levels 4–6 in Markdown | rendered without normalisation | they use Heading 4–6 styles, and no paragraph starts with `#` | `tests/test_docx_generator.py::test_deep_headings_styled` |
| AC-12 | a signature block using hard line breaks | rendered | the lines stay separate (Word line breaks) | `tests/test_docx_generator.py::test_hard_breaks_kept` |
| AC-13 | a `## SCHEDULE 1` heading after body text | rendered | that heading paragraph has page-break-before | `tests/test_docx_generator.py::test_schedule_page_break` |
| AC-14 | the golden addendum fixture (3 tables, schedules, signature page, model-style defects) | normalised and rendered | the .docx has ≥ 3 tables, 0 paragraphs starting with `|` or `#`, 0 `---`, 0 `\<` | `tests/test_docx_generator.py::test_golden_addendum` |
| AC-15 | a filled template with fewer tables than the draft | structure checked | the export falls back to `skeleton_clone` | `tests/test_docx_generator.py::test_template_structure_check` |
| AC-16 | `DRAFT_MARKDOWN_NORMALIZE=false` | a draft is rendered | the Markdown is not normalised | `tests/test_draft_markdown.py::test_setting_off` |
| AC-17 | a real backend connection | an AI-only draft and a template draft are produced | both files open in Word with real tables and no Markdown markup | **manual**: needs a live backend and model calls |

## Negative and edge cases
- Duplicate `requestId`, Stop, disconnect, `view` collaborator: unchanged by
  this ticket. The normaliser runs inside the existing drafting turn.
- Model error/timeout: unchanged. The normaliser only runs on a successful
  render.
- Export failure: `docxStatus = "failed"`, `downloadUrl` absent, and the
  Markdown still ships.
- A single pipe line in prose (not a table): left as it is.

## Non-functional
- The new `docx_export` log line carries counts only
  (`tests/offline/verify_logging.py` passes).
- No new paid calls.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run (handlers touched) plus a manual drafting check (AC-17)
- [ ] `CHANGELOG.md` updated; payload change called out
