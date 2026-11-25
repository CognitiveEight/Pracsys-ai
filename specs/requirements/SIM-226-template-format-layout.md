# SIM-226: Drafts from an uploaded template keep their page layout when a format is applied

- **Jira:** SIM-226 (Bug)
- **Status:** approved (2026-11-25, in conversation)
- **Target branch flow:** fix/SIM-226-template-format-layout → development → staging → main

## Problem
SIM-217 applies the chosen format's page settings to every draft Word file.
Templates converted from PDF place their text with paragraph indents measured
from near-zero page margins. Replacing those margins with the format's
(1.5 in / 1 in) adds to the indents: the title and short centred lines get a
negative width and render one letter per line in the editor, and the body
shrinks by a third.

## Goal
A draft built from an uploaded template looks like the template. The format
adds only what lives outside the body text: letterhead header/footer, court
heading block, watermark, page number.

## Scope
- In: `apply_format_layout(..., keep_body_layout=True)` skips page size,
  orientation, margins, body font, size, line spacing and alignment.
  `_apply_chosen_format` sets it for every template route (`llm_diff`,
  `template_fill`, `heuristic_fill`, `skeleton_clone`). The `draft_format
  applied` log line gains `body_layout` and `body_skipped`.
- Out: drafts without a template (`defaults`) keep SIM-217 behaviour; the
  frontend sending a letterhead as a court format is SIM-227.

## Non-functional
No socket payload change, no new model call, no new logged data.
