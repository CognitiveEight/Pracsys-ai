# SIM-<n>: <title> — acceptance criteria

- **Requirements:** `specs/requirements/SIM-<n>-….md`
- **Design:** `specs/technical-design/SIM-<n>-….md`

Each criterion is testable and maps to exactly one automated check, or is
marked **manual** with the reason (e.g. needs a live backend).

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a workspace with a validated role | the backend sends `<event>` with `<shape>` | the service emits `<featureType>` with `<field>` | `tests/test_….py::test_…` |
| AC-2 | | | | |

## Negative and edge cases
- Duplicate re-send under the same `requestId`
- Stop (`workspace_stop_request`) arriving mid-run
- Socket disconnect during generation (payload buffered and re-sent)
- `view` collaborator on a shared workspace
- Model error / timeout / safety block

## Non-functional
- No new log line contains payloads, document text, names or URLs
  (`tests/offline/verify_logging.py` passes).
- Cost of the new path appears in the emit `metadata`.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run if the change touches handlers, panels or ingestion
- [ ] `CHANGELOG.md` updated; payload changes called out
