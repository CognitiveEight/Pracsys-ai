# SIM-229 Requirements: template page numbers count the pages

## Problem
A draft built from an uploaded template showed the same "Page 3 of 16" on
every page in ONLYOFFICE and Word. The template's footer had come from Word's
"Page X of Y" gallery, but its PAGE / NUMPAGES fields had been flattened to the
numbers they showed when the file was saved. With a format that adds a page
number, a second number appeared under it.

## Requirements
- R1. A header/footer paragraph that is nothing but page wording ("Page 3 of
  16", "Page 3", "Page 3/16") and has no field is re-linked to live PAGE and
  NUMPAGES fields, keeping its text and run formatting.
- R2. A bare "3" or "3 of 16" is re-linked only inside Word's page-number
  gallery content control; elsewhere it is left alone.
- R3. Body text and any other header/footer text are never changed.
- R4. A template draft whose own header/footer already numbers pages where the
  chosen format wants the number keeps the template's numbering and gets no
  second number.
- R5. `pageNumber: none` still removes page numbers; a format placing the
  number in the other story (header vs footer) still moves it there.
- R6. Template drafts without a chosen format get R1-R2 too; drafts without a
  template are unchanged.
- R7. A failure never loses the draft. Logs carry counts only.

## Out of scope
Page numbers in text boxes or shapes; numbering formats other than Arabic.
