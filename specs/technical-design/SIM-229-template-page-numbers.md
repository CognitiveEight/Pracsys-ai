# SIM-229 Technical design

`app/processing/draft_format.py`
- `repair_frozen_page_numbers(doc) -> int` walks every header/footer part
  (`_all_stories`). A paragraph qualifies when its children are only `pPr`,
  runs, bookmarks and proofing marks, its runs hold only `rPr` and `t`, and its
  text matches `_FROZEN_PAGE_RE` (requires "page"), or `_FROZEN_GALLERY_RE`
  (word optional) inside a `docPartGallery` "Page Numbers" content control.
  The runs are rebuilt as text / PAGE field / text / NUMPAGES field / text,
  each taking the `rPr` of the character it starts at; field results show the
  old digits until the viewer updates them.
- `repair_frozen_page_numbers_bytes(bytes) -> (bytes, n)`: unchanged bytes
  when `n == 0`.
- `apply_format_layout` runs the repair right after opening the file, so the
  existing page-number detection sees the re-linked paragraph.
- New `_Formatter.has_page_numbers(vertical)`. With `keep_body_layout` and a
  page number requested where the template already has one, nothing is removed
  or added and `page_number="template"`.
- `_remove_paragraph` now drops every content control it leaves empty, from
  the inside out (the gallery nests two).
- `FormatResult.page_numbers_repaired`, reported by `summary_fields`.

`app/langgraph/drafting_agent.py`
- `_apply_chosen_format` with no format and a template route calls
  `_repair_template_page_numbers`, which runs the repair in a thread, logs
  `page number repair route= paragraphs=` and returns the original bytes on
  any error.

No socket payload, config or dependency change.
