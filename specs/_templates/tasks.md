# SIM-<n>: <title> — tasks

- **Design:** `technical-design.md` (same folder)
- **Status:** draft | approved

Ordered implementation steps. Each step is small enough for one commit on the
ticket branch and leaves `make lint` and `make test` green. A step may be
tracked as a Jira Subtask; put its key in the Subtask column, or leave it
blank.

| # | Step | Files | Subtask | Status |
|---|---|---|---|---|
| 1 | <one change, e.g. add the schema field> | `app/socket/schemas.py` | | todo |
| 2 | <the handler change that uses it> | `app/socket/handlers/…` | | todo |
| 3 | <tests for AC-1, AC-2> | `tests/…` | | todo |
| 4 | CHANGELOG entry; payload change called out | `CHANGELOG.md` | | todo |

Status values: `todo`, `in progress`, `done`, `dropped` (say why under
Notes).

## Notes
Order constraints between steps, and anything a step deliberately leaves for
a later ticket.
