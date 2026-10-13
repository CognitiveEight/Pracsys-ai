# SIM-<n>: <title> — test cases

- **Acceptance criteria:** `acceptance-criteria.md` (same folder)

One row per check. Every AC in `acceptance-criteria.md` has at least one row.
Fictional data only: no real names, case text, phone numbers or ids.

| ID | AC | Type | Steps | Expected | Check |
|---|---|---|---|---|---|
| TC-1 | AC-1 | unit | <input and call> | <result> | `tests/test_….py::test_…` |
| TC-2 | AC-2 | offline | drive `event_handler.handle("<event>", …)` with fake socket, store and model | <emits, cache state> | `tests/offline/verify_….py::<group>` |
| TC-3 | AC-3 | manual | <steps against a connected backend> | <what the reviewer sees> | manual: <reason it cannot be automated> |

Types:

| Type | Runs in | Notes |
|---|---|---|
| unit | `make test` (pytest) | `tests/test_<area>.py` |
| offline | `make test` | a check group in `tests/offline/verify_<area>.py` |
| e2e | `make e2e` | real, billed model calls; a human runs it |
| manual | by a person | say why it is not automated |
