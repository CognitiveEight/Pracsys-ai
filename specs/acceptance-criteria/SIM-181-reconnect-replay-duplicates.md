# SIM-181: Strategy panel no longer appears twice in chat after a socket reconnect — acceptance criteria

- **Requirements:** `specs/requirements/SIM-181-reconnect-replay-duplicates.md`
- **Design:** `specs/technical-design/SIM-181-reconnect-replay-duplicates.md`

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a panel delivered more than 60 s before a disconnect and unchanged since | the reconnect replay runs | it is not re-sent and the skip is logged | `tests/offline/verify_reconnect_replay.py::replay_skips_unchanged`, `::reemit_path_skips_delivered` |
| AC-2 | a panel whose cached payload changed after its last delivery | the reconnect replay runs | it is re-sent | `tests/offline/verify_reconnect_replay.py::replay_sends_changed` |
| AC-3 | a panel whose emit reported delivered=no | the reconnect replay runs | it is delivered | `tests/offline/verify_reconnect_replay.py::replay_sends_never_delivered` |
| AC-4 | a panel handed to the socket within 60 s before the disconnect | the reconnect replay runs | it is re-sent | `tests/offline/verify_reconnect_replay.py::replay_sends_inside_grace` |
| AC-5 | a service restart (no delivery record) | the reconnect replay runs | panels are re-sent as before | `tests/offline/verify_reconnect_replay.py::replay_sends_without_record` |
| AC-6 | delivery records for a workspace | the workspace is purged | the records are gone | `tests/offline/verify_reconnect_replay.py::purge_clears_records` |
| AC-7 | an identical payload from a live (non-replay) run | it is emitted | it is sent | `tests/offline/verify_reconnect_replay.py::live_emit_never_skipped` |
| AC-8 | a real backend and frontend | the socket drops after a run and reconnects | chat shows one strategy message | **manual** — needs the live backend and frontend (`make run` or `make e2e`) |

## Non-functional
- The new log line carries no payload, names or URLs
  (`tests/offline/verify_logging.py` passes).

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run (handler change)
- [ ] `CHANGELOG.md` updated
