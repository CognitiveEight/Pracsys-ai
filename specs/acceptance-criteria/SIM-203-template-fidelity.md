# SIM-203: Template fidelity — acceptance criteria

- **Requirements:** `specs/requirements/SIM-203-template-fidelity.md`
- **Design:** `specs/technical-design/SIM-203-template-fidelity.md`

All templates are fictional (`template_fidelity/corpus.py`). "Format kept"
means `checklist.check_format` reports no failure. It checks:
- body font and size;
- title text and alignment;
- headings;
- numbering labels in order;
- table count, columns and cell text;
- letterhead text and logo;
- page numbers;
- margins;
- line spacing;
- underlines;
- signature blocks;
- no leftover markup.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | 24 corpus templates (6 types × 4 layouts) | built as .docx | format kept | `test_builder_reproduces_template` |
| AC-2 | the same templates as text PDFs | read by `read_pdf` and rebuilt | format kept for all 24 | `test_pdf_reader_reproduces_template` |
| AC-3 | the same templates as image-only scans (tilt 0.6°, blur, noise) | read by `read_scan` | format kept for all 24 | `test_scan_reader_reproduces_template` |
| AC-4 | scans tilted 2° | read | format kept | `test_scan_tilted_two_degrees` |
| AC-5 | a scan with no vision pass | read | title, headings, numbering, underline, letterhead, logo, page numbers, margins, spacing and size kept | `test_scan_without_vision_still_measures_layout` |
| AC-6 | the vision pass raises | a scan is read | measured result returned | `test_scan_vision_failure_is_not_fatal` |
| AC-7 | a template | mapped | paragraphs, tables, rows (header marked) and labels listed; header/footer not listed | `test_map_lists_paragraphs_rows_and_labels` |
| AC-8 | fill operations (replace, cell, add_row, insert_after with like, delete, unknown ID) | applied | content changed; inserted paragraph has the like-paragraph's indents; new row is not a header; font, letterhead, page numbers, margins and table columns kept; unknown ID counted | `test_fill_keeps_template_format` |
| AC-9 | a literal-label paragraph and an auto-numbered one | replaced without and with a number | literal label kept; Word number not doubled | `test_fill_keeps_literal_label_and_strips_auto_number` |
| AC-10 | any fill | applied | header and footer XML unchanged | `test_fill_never_touches_header_or_footer` |
| AC-11 | three inserts after one anchor | applied | order kept | `test_inserts_after_one_anchor_keep_their_order` |
| AC-12 | `TEMPLATE_FILL_V2` on and the model returns operations | export | route `template_fill`, task key `template_fill` | `test_template_fill_applies_model_ops` |
| AC-13 | the model call errors, or no operation applies | export | falls back (None) | `test_template_fill_falls_back_on_model_error`, `…_when_nothing_applies` |
| AC-14 | a text PDF upload | `build_template_docx` | clean .docx with format kept; .docx passes through; unsupported type gives None | `test_ingest_text_pdf_builds_clean_docx` |
| AC-15 | real .docx, PDF and scanned templates | uploaded and drafted against a live backend | the result follows each template in Word | **manual**: needs a live backend, Vision and Gemini |

## Negative and edge cases
- Unknown operation IDs, bad JSON lines and a raw tab inside JSON: skipped
  or tolerated (`test_parse_ops_formats`).
- Ligatures in PDFs are expanded (`test_pdf_ligatures_expanded`).
- A failure in any stage falls back to the previous behaviour; nothing is
  lost.

## Non-functional
- Logs carry counts only.
- The vision and fill model calls go through the router and are metered.

## Definition of done
- [ ] Every AC above passes, or has a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run, plus the manual AC-15
- [ ] `CHANGELOG.md` updated
