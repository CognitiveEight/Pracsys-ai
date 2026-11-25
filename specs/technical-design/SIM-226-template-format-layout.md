# SIM-226: Technical design

## Root cause
`_Formatter.apply_page` writes `section.{left,right,top,bottom}_margin`,
page size and orientation on every section, and rewrites body fonts, sizes,
spacing and alignment, regardless of how the file was built. Template routes
preserve the template's `w:ind` (paragraph and style level), so the new
margins stack on the old indents.

## Change
- `app/processing/draft_format.py`
  - `FormatResult.body_layout` (`kept` | `applied`) and
    `body_settings_skipped` (count of body-layout keys present but skipped).
  - `_BODY_LAYOUT_KEYS` = font, fontSize, lineSpacing, pageSize, orientation,
    margins, alignment.
  - `_Formatter(keep_body_layout=...)`; `apply_page` returns straight after
    counting skipped keys when it is set. Watermark and page number parsing
    moved to `_header_footer_settings`, used by both paths.
  - `apply_format_layout(..., keep_body_layout=False)`: default keeps the
    SIM-217 behaviour for any other caller.
- `app/langgraph/drafting_agent.py` `_apply_chosen_format`: passes
  `keep_body_layout=route != "defaults"`; log line adds the two fields.

## Alternatives rejected
- Shift every indent by the margin change: right for converted templates but
  wrong for real Word templates whose list and quote indents are relative to
  the margins, and still overrides the template's fonts and spacing.
- Validate the format's margin values: the values were valid; the stacking
  was the defect.

## Risk
A lawyer who relied on the format to restyle a template's body text no
longer gets that; this matches the product rule that the format is the
header/footer frame for template drafts.
