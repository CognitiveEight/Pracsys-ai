# SIM-208: Standard layouts — technical design

- **Requirements:** `specs/requirements/SIM-208-standard-layouts.md`
- **Status:** draft

## Design
- `app/processing/draft_layouts.py` (**new**):
  - `LayoutProfile` and `PROFILES`;
  - `infer_layout(title, document_type)`: the first family whose words
    appear in the title wins (vakalatnama → affidavit → notice → court
    filing → agreement → default);
  - `starts_body`: the salutation, "MOST RESPECTFULLY", "This Agreement is
    made", "I,", "WHEREAS", or the first numbered paragraph;
  - `is_title_line`: short, at least 80% capitals, not a cause-title party
    line;
  - `is_versus`.
- `docx_generator.render_markdown_to_docx(..., title, document_type)`
  infers the profile. The defaults path applies:
  - margins, body size, line spacing and alignment;
  - heading sizes relative to the body size;
  - page numbers;
  - centring of headings and title lines until the body starts, and of
    "VERSUS" lines anywhere.

  The template paths are unchanged.
- An untitled ("Draft") document gets the default profile, which is
  identical to the previous defaults.
- Registry entries 11–16 are added to `DRAFTING_SYSTEM_INSTRUCTION` (prompt
  only).

## Checklist against CLAUDE.md
- Model calls: none added. Only the prompt text grows.
- Loaders, emits, caches, purge, role gate: n/a.
- Logging: none added.

## Rollback
Revert the commit. Drafts titled "Draft" are unaffected by design.

## Test plan
`tests/test_draft_layouts.py`.
