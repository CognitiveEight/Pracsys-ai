# SIM-191: A new blank draft in the Drafting Assistant no longer fails — acceptance criteria

- **Jira:** SIM-191 (bug; one-line fix, no requirements or design file)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a new blank draft (no template, no saved session) | the lawyer sends the first message | the turn completes without the error reply and the session is saved | `tests/test_drafting_blank_draft.py::test_blank_draft_without_session_does_not_crash` |
| AC-2 | a draft with a saved session naming a template | a message without `templateId` arrives | the session's template and version are pinned, as before | `tests/test_drafting_blank_draft.py::test_blank_draft_with_session_pins_the_session_template` |
| AC-3 | the staging AI service | a blank draft is started in the app | it drafts normally | **manual** — needs the deployed service |

## Definition of done
- [ ] AC-1 and AC-2 pass; AC-3 checked on staging after deploy
- [ ] `make lint` and `make test` pass
- [ ] `CHANGELOG.md` updated
