# SIM-203: Template fidelity — technical design

- **Requirements:** `specs/requirements/SIM-203-template-fidelity.md`
- **Status:** draft
- **Approver:** tech lead

## Summary
A new package, `app/processing/template_fidelity/`, reads any template into
one format model (`ir.TemplateIR`) and rebuilds it as a clean .docx. Drafts
are then written into that .docx by ID-addressed operations. Both stages sit
behind settings that are off by default, and each falls back to today's path
on any failure.

## Affected code
| File | Change |
|---|---|
| `template_fidelity/ir.py` | **new**: format model (paragraphs, runs, tables, images, page setup, header/footer) |
| `template_fidelity/layout.py` | **new**: positioned lines → paragraphs, alignment, indents, labels, spacing, running header/footer, page numbers, borderless tables |
| `template_fidelity/pdf_reader.py` | **new** (SIM-205): PyMuPDF spans, underline strokes, `find_tables`, logo |
| `template_fidelity/scan_reader.py` | **new** (SIM-206): OCR word boxes, de-skew, pixel bold/underline/logo, optional Gemini vision |
| `template_fidelity/docx_builder.py` | **new**: format model → .docx |
| `template_fidelity/docx_map.py`, `docx_fill.py` | **new** (SIM-204): IDs and in-place operations |
| `template_fidelity/ingest.py` | **new**: picks the reader for each upload |
| `template_fidelity/corpus.py`, `pdf_writer.py`, `checklist.py` | **new** (SIM-207): fictional corpus, PDF and scan writer, format checklist |
| `app/langgraph/drafting_agent.py` | ingest uses `build_template_docx` (`TEMPLATE_FIDELITY_INGEST`); export route `template_fill` before `llm_diff` (`TEMPLATE_FILL_V2`) |
| `app/prompts/drafting.py` | **new** `TEMPLATE_MAP_FILL_INSTRUCTION`, `build_template_map_fill_prompt` |
| `app/ai/gemini_client.py` | **new** `generate_with_images` (via the router) |
| `app/processing/ocr.py` | **new** `ocr_word_boxes` |
| `app/core/config.py`, `env.example` | **new** `template_fill_v2`, `template_fidelity_ingest`, `template_scan_vision` |

## Event and payload changes
None. The export still returns `draftDocument.downloadUrl`.

## Design
1. **Ingest (`TEMPLATE_FIDELITY_INGEST`).**
   - **.docx:** used unchanged.
   - **Text PDF** (≥ 40 characters per page): `read_pdf`.
   - **Scanned PDF:** pages are rendered at 200 dpi, then `read_scan`.
   - **Photo:** `read_scan`, assuming A4 in the image's orientation.

   The format model is passed to `build_docx`, uploaded as the template
   skeleton, and filled at export. On None or an error, the existing
   pdf2docx path runs.
2. **Layout recovery.** Every threshold is measured from the document
   itself:
   - **Line pitch:** the smallest recurring pitch after wrapped lines.
   - **Justification:** decided for the whole document — most long lines
     flush right.
   - **Right margin:** mirrors the left margin when the text is ragged.
   - **Running header/footer:** text repeated across pages, or next to a
     top logo on a one-page template; page numbers are turned into a
     format string.
   - **Borderless tables:** columns come from the header row's cells and the
     whitespace channel between them, or from the union of body-cell
     ranges. Wrapped cell lines are joined to their cell.
3. **Scans.**
   - **Geometry:** OCR boxes are de-skewed (median slope between neighbouring
     words; the true box height is solved from the tilt), and the image is
     straightened too.
   - **Font size:** from the glyph-box height (0.94 em), with lines within
     10% snapped to the body size.
   - **Styles from pixels:** bold from ink density (> 1.22 × median),
     underline from a continuous dark row at the baseline, logo from dark
     area that is not text in the top band.
   - **Gemini vision** (optional, one call per page, task
     `template_scan_vision`): tables with cell texts and header rows, plus
     the font family. Its tables are matched to OCR lines by token F1
     ≥ 0.75.
4. **Fill (`TEMPLATE_FILL_V2`).**
   - **Map:** `docx_map.walk` gives IDs to paragraphs (`p#`), tables
     (`t#`), rows (`t#.r#`) and cells (`t#.r#.c#`), including those inside
     content controls. The header and footer are never listed.
   - **Model call:** one `generate_longform`, task `template_fill`. It
     returns JSON lines, so a truncated answer loses at most one operation.
   - **Applying operations:**
     - `replace` keeps the first run's formatting, with markup for
       bold/italic/underline and highlighted `<Label>` placeholders;
     - `insert_after` and `add_row` deep-copy the anchor or `like`
       element (a copied header row loses `tblHeader`);
     - `delete` is applied last.
   - **Numbering:** literal labels are kept; auto-numbered paragraphs (on the
     paragraph or through their style) have a typed number stripped.
   - **Fallback:** nothing applied, or a result that lost the draft's tables
     or sections, sends the export to the `llm_diff` chain.

## Checklist against CLAUDE.md
- Role gate: n/a. Template formatting comes after the gated draft.
- Long-running work: ingest is already a template-sync task; rendering,
  reading and applying run in `asyncio.to_thread`.
- Loaders / `CancelledError`: no new loaders. `CancelledError` is re-raised
  at ingest; nothing else catches `BaseException`.
- Emits: unchanged.
- Model calls: through the router (`generate_longform`,
  `generate_with_images`) with stable task keys `template_fill` and
  `template_scan_vision`. `is_generation_error` is checked, so gate0 applies.
- Paid calls metered: by the router. Vision OCR is metered in
  `ocr._call` (lowest call site).
- New caches and purge: none. Template skeletons use the existing GCS
  location.
- Ingestion/deletion: template ingest only; there is no evidence path.
- Logging: counts only. No template text, file names or URLs.

## Risks and rollback
- **Risk:** a reader mis-measures an unusual layout. Mitigations: everything
  is off by default; every stage falls back; and the checklist runs in CI
  on the corpus.
- **Rollback:** `TEMPLATE_FIDELITY_INGEST=false`, `TEMPLATE_FILL_V2=false`,
  and `TEMPLATE_SCAN_VISION=false` (measured styles only).

## Test plan
- `tests/test_template_fidelity.py` covers:
  - the corpus with all 24 templates per format (builder, PDF reader, scan
    reader);
  - scans with no vision pass, and scans tilted 2°;
  - the map and fill operations;
  - the ingest entry point.
- `tests/test_template_fill_route.py` covers the export route with the model
  call faked.
- `make e2e` and a manual check: upload one .docx, one PDF and one scanned
  template, draft from each, and open the results in Word.

## Decisions
- Numbering labels in rebuilt templates are literal text with a hanging
  indent. A PDF or scan shows the number; it does not record Word's
  numbering definitions, so text is the faithful form.
- A scan's font family comes from the vision pass, or Times New Roman
  without it. Pixels alone cannot reliably tell serif from sans.
