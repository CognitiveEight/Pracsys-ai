# SIM-210: Draft editor — acceptance criteria

Verified in the backend and frontend repositories. This service has no
code change.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a delivered draft (`docxStatus: ready`) | the owner clicks "Open editor" | the .docx opens in the embedded editor with its tables, numbering and letterhead | **manual**: frontend |
| AC-2 | the editor is open | the lawyer deletes and inserts text | the changes show as tracked (struck through / underlined, with author) and can be accepted or rejected | **manual** |
| AC-3 | a passage is selected | the lawyer adds a comment and a reply | the comment thread is saved with the document | **manual** |
| AC-4 | changes were made | the editor closes (callback status 2) | the backend stores a new version and replies `{"error":0}` | backend test |
| AC-5 | a callback with a missing or invalid JWT | received | 403; nothing stored | backend test |
| AC-6 | a `view` collaborator | opens the editor | read-only: no editing, review or comments | backend test + **manual** |
| AC-7 | a stored version with changes | downloaded and opened in Microsoft Word | the tracked changes and comments appear | **manual** |
| AC-8 | a workspace is deleted | the purge runs | the document server's cached copy is removed | backend test |
