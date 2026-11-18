# SIM-217: Apply chosen formats — acceptance criteria

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | the fictional court format | applied | A4, margins 1/1/1.5/1 in, Normal Times New Roman (incl. East-Asian slot) 12 pt, 1.5 spacing, justified body | `test_court_page_settings_applied` |
| AC-2 | a centred paragraph and a line block with breaks | justified | both keep their alignment | `test_justify_leaves_centred_paragraphs_and_line_blocks` |
| AC-3 | `orientation: landscape`; Legal / Letter | applied | width and height swapped; exact page sizes | `test_landscape_swaps_width_and_height`, `test_page_sizes` |
| AC-4 | margins in in, cm, mm, pt, bare numbers and junk | parsed | valid ones converted, invalid ignored | `test_parse_length` |
| AC-5 | `watermark: CONFIDENTIAL`, applied twice; then `None` | applied | one diagonal watermark in the header; removed by `None` | `test_watermark_in_header`, `test_watermark_none_removes_existing` |
| AC-6 | each page-number position | applied | one PAGE field in footer/header with that alignment; the default "Page X of Y" replaced | `test_page_number_position`, `test_page_number_replaces_the_default_page_x_of_y` |
| AC-7 | an unknown `pageNumber` value | applied | file unchanged there; reported | `test_unknown_page_number_is_ignored_and_reported`, `test_unknown_page_number_leaves_the_existing_footer_as_it_was` |
| AC-8 | the fictional letterhead | applied | borderless 3-column header with Left/empty/Right texts, bold left in Calibri, hidden centre absent, footer text | `test_letterhead_header_table_and_footer` |
| AC-9 | one shown block | applied | a paragraph, not a table | `test_single_block_is_a_paragraph_not_a_table` |
| AC-10 | `headerHtmlLeft` with b/i/u, a script and attributes; `headerHtmlRight: ""` | applied | html replaces text and bold; script and attributes gone; empty html hides the block | `test_html_overrides_text_and_bold_and_is_sanitised`, `test_empty_html_hides_the_plain_text` |
| AC-11 | `pageApplication: first` | applied | `titlePg` set; letterhead only in the first-page header/footer | `test_page_application_first_sets_title_page` |
| AC-12 | a file with its own header/footer | letterhead applied / court applied | replaced / kept | `test_letterhead_replaces_existing_header`, `test_court_format_keeps_the_existing_header` |
| AC-13 | image letterhead; one fetch ok, one fails, one raises, one GIF, no fetcher | applied | ok image embedded; failures counted; no crash | `test_letterhead_image_ok_and_failure_counted`, `test_image_fetch_exception_or_non_image_never_crashes`, `test_no_fetcher_means_images_skipped` |
| AC-14 | the fictional court format | applied | court, jurisdiction, case number, CNR inserted in order; uppercase, bold, centred | `test_court_blocks_inserted_in_order_and_styled` |
| AC-15 | a custom field without `text` | applied | "label: value" | `test_custom_field_without_text_uses_label_and_value` |
| AC-16 | the draft already starts with the court header and case number | applied | not repeated | `test_no_duplicate_when_the_draft_starts_with_the_court_header` |
| AC-17 | the format applied twice | applied | no duplicate court block | `test_applying_twice_does_not_duplicate_the_court_block` |
| AC-18 | a court format that also carries a letterhead | applied | header untouched | `test_court_format_ignores_letterhead_section` |
| AC-19 | invalid values everywhere | applied | valid ones applied, invalid ignored and reported, no exception | `test_invalid_values_ignored_valid_ones_applied` |
| AC-20 | unknown keys | applied | reported by name, never by value | `test_unknown_keys_reported_by_name_only` |
| AC-21 | an unusable layout; unreadable bytes | applied | file unchanged; only bad bytes raise | `test_unusable_layout_returns_file_unchanged`, `test_unreadable_docx_raises` |
| AC-22 | either format | applied | output re-opens; body text unchanged | `test_output_reopens_and_body_text_unchanged` |
| AC-23 | template runs with explicit font and size | applied | body runs follow the format; a deliberately larger run keeps its size | `test_template_direct_formatting_follows_the_chosen_font_and_size` |
| AC-24 | non-https, loopback or localhost image links | fetched | refused without a request | `test_default_fetcher_refuses_non_https_and_private_hosts`, `test_default_fetcher_makes_no_request_for_refused_links` |
| AC-25 | a turn's format | exported | the uploaded file is formatted; without a format it is unchanged; a formatter exception uploads the unformatted file | `test_export_applies_the_turn_format`, `test_export_without_format_is_unchanged`, `test_export_survives_a_formatter_exception` |
| AC-26 | `draftOrigin.formatLayout` with an image link; a later turn without a format | handled | the turn uses it; the session stores it without the link; the later turn uses the stored one | `test_turn_stores_format_from_draft_origin_without_image_links`, `test_turn_without_format_keeps_the_session_format`, `test_later_turn_uses_the_format_the_first_turn_stored` |
