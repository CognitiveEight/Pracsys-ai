# SIM-202: Drafting Assistant Word export loses tables, headings and placeholders

- **Jira:** SIM-202
- **Owner:** product owner (drafting)
- **Status:** draft
- **Target branch flow:** fix/SIM-202-drafting-docx-fidelity → development → staging → main

## Problem
A draft reads well in chat, but the Word file the lawyer downloads needs
heavy manual repair before it can be sent. In a recent AI-only draft (a
supplemental shareholders' addendum):

- **None of its three tables was a Word table.** The tranche schedule, the
  bank details and the capitalisation table arrived as about 28 lines of
  `| … |` text plus their `|:---|` separator lines.
- **Markdown syntax came through as text:** 24 `---` lines, 7 `####`
  headings and 19 escaped placeholders (`\<Bank Name\>`).
- **Nested `(a)`/`(b)` sub-clauses were flattened.**

The lawyer has to rebuild every table, delete the markup and re-indent the
clauses. That defeats the point of an automated draft.

## Goal
The downloaded .docx is ready for the lawyer to work in. Tables are Word
tables, headings are headings and sub-clauses are indented. Every unfilled
placeholder is clean and highlighted so it is easy to find. The file says
what the chat preview says.

## Scope
- In:
  - deterministic clean-up of the model's draft Markdown (tables,
    placeholders, headings, separators, nested lists, spacing) before it is
    stored, emitted or exported;
  - the AI service's Markdown → .docx renderer: tables, nested lists,
    headings 1–6, section separators, highlighted placeholders, signature
    lines;
  - template path: when the filled template is missing sections or tables
    that the draft has, render the full draft in the template's styles
    instead;
  - a format contract in the draft render prompt;
  - additive `draftDocument` fields `docxStatus` and `placeholderCount`.
- Out (explicitly not doing):
  - the Node backend's own .docx builder. The backend team switches
    download and "Generate final draft" to `draftDocument.downloadUrl`
    (separate backend ticket);
  - Google Docs / Microsoft Word integrations (MCP or API);
  - honouring `formatLayout` (paper size, fonts);
  - reading template tables at ingestion (template text still omits table
    content).

## Users and surfaces
- Drafting Assistant chat: `user_message_request` with
  `chat_type = DRAFTING_ASSISTANT`.
- Outbound `ai_response.draftDocument` (`content`, `downloadUrl`).
- The .docx file at `downloadUrl`.

## Constraints
- [x] Changes a socket payload: additive `draftDocument.docxStatus` and
  `draftDocument.placeholderCount`. `content` keeps its shape and now carries
  normalised Markdown.
- [ ] Role-dependent behaviour
- [ ] Touches personal data, PII redaction or the workspace purge (draft text
  is never logged; only counts are)
- [ ] Adds a paid call (no new model call)
- [ ] Indian-law output
- [ ] Shared workspaces

## Success measure
- The `docx_export` log line reports `tables=`, `placeholders=` and
  `leftover_markup=` for every export. After release, `leftover_markup=0`
  on all exports.
- The golden fixture exports with at least 3 Word tables, 0 pipe-text lines,
  0 `\<` and 0 literal `#` headings.

## Open questions
- Backend: confirm when download and "Generate final draft" switch to
  `downloadUrl`. Until they do, lawyers still receive the Node-built file,
  but its `content` input is cleaner.
