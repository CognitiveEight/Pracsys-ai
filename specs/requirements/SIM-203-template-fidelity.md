# SIM-203: Template-fidelity drafting — follow any uploaded template

- **Jira:** SIM-203 (epic) with SIM-204 (.docx fill), SIM-205 (text PDF),
  SIM-206 (scans/photos), SIM-207 (test corpus)
- **Owner:** product owner (drafting)
- **Status:** draft
- **Target branch flow:** feature/SIM-203-template-fidelity → development → staging → main

## Problem
A lawyer who drafts from their own template expects the result to look like
that template: same fonts, headings, numbering, tables, letterhead,
header/footer and spacing. Today:
- a .docx template keeps its format, but only its placeholders are filled,
  so clauses or table rows the draft adds are lost;
- a text PDF becomes positioned text boxes, which cannot be edited
  paragraph by paragraph;
- a scan or photo becomes plain text, and every bit of formatting is lost.

Templates differ from lawyer to lawyer, so nothing may be built for one
template or one document type.

## Goal
Any uploaded template — .docx, text PDF, scanned PDF or photo — produces
drafts in that template's own format, with all of the draft's content.

## Scope
- In:
  - **Reading the format.** One neutral format model is read from any
    source: fonts, sizes, bold/italic/underline, alignment, indents,
    hanging numbering labels, spacing, line spacing, headings, tables
    (widths, merged cells, header rows, borders), letterhead and logo,
    header/footer, page numbers, margins and page size.
  - **Rebuilding.** The format model is rebuilt as a clean, editable .docx
    template.
  - **Filling.** The draft is written *into* the template: the model returns
    operations per paragraph, row or cell ID, and code applies them in place.
  - **Testing.** A fictional template corpus (6 document types × 4 layouts ×
    3 formats) and a format checklist.
- Out:
  - handwriting;
  - multi-column page layouts (newsletter style);
  - text boxes and shapes inside templates;
  - the frontend editor;
  - recognising stamps and signatures as such (they stay images or blanks).

## Users and surfaces
- Template upload: global template sync → `ingest_template`.
- Drafting Assistant export: `draftDocument.downloadUrl`.

## Constraints
- [ ] Changes a socket payload: no change.
- [ ] Role-dependent behaviour
- [ ] Touches personal data, PII redaction or the workspace purge: template
  files only; no case data is logged.
- [x] Adds a paid call. The template fill uses one routed model call per
  export; the scan vision pass uses one routed Gemini call per page; Vision
  OCR is billed as today. All are metered.
- [ ] Indian-law output
- [ ] Shared workspaces

## Success measure
- The format checklist passes for every corpus template, from .docx, PDF and
  scan alike (CI).
- In production, two log lines report every result as counts only:
  - `template fidelity built source= paragraphs= tables= …` at ingest;
  - `template_fill ops= applied= inserted= …` at export.
- A lawyer review on real templates, once real samples are available.

## Open questions
- Real lawyer templates are needed to extend the corpus beyond fictional
  layouts. They will be added as local-only cases.
