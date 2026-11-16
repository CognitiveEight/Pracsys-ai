# SIM-181: Strategy panel no longer appears twice in chat after a socket reconnect — technical design

- **Requirements:** `specs/requirements/SIM-181-reconnect-replay-duplicates.md`
- **Status:** approved
- **Approver:** tech lead

## Summary
`_emit_document_feature` records a fingerprint (sha256 of the final payload)
and a timestamp for every delivered panel. The service records when the socket
last dropped. The reconnect replay calls the emit with `replay=True`, which
skips a payload whose fingerprint matches the last delivery when that delivery
was more than `REPLAY_DELIVERY_GRACE_SECONDS` (60 s) before the disconnect.

## Affected code
| File | Change |
|---|---|
| `app/socket/handlers/chat.py` | `REPLAY_DELIVERY_GRACE_SECONDS`, `_payload_fingerprint`, `replay=` on `_emit_document_feature`; record on delivered=yes |
| `app/socket/handlers/workspace_events.py` | replay emits pass `replay=True`; `on_disconnect` records `_last_disconnect_at` |
| `app/socket/client.py` | an emit failure that marks a disconnect also records `_last_disconnect_at` |
| `app/socket/events.py` | `_delivered_feature_fingerprints`, `_last_disconnect_at` state |

## Event and payload changes
None.

## Design
- Key `"<workspace_id>:<featureType>"` → `(fingerprint, delivered_at)`,
  in memory only. After a restart there is no record and the replay behaves
  as before.
- The fingerprint is taken after the fact-correction and citation passes,
  which run on every emit, so a replay of the same cached payload produces
  the same hash.
- `emit()` returning True means handed to the socket, not acknowledged. A
  connection can be dead for up to ping_interval + ping_timeout before the
  drop is noticed, so a delivery within 60 s of the disconnect is replayed.
- A skipped replay returns True (the backend has it) and logs one line.

## Checklist against CLAUDE.md
- Role gate: n/a, no generation.
- Long-running work: n/a; the replay is still scheduled as before.
- Loaders: the replay arms none; a skip returns True like a delivery.
- Emits: unchanged path through `_emit_document_feature` / `self._socket_client`.
- Model calls: none.
- Paid calls: none.
- Purge: keys are prefixed `"<workspace_id>:"`, reached by `_clear_workspace_memory`.
- Ingestion/deletion: n/a.
- Chat mutation gate: n/a.
- Logging: the skip line logs the featureType and an age in seconds only.

## Risks and rollback
If a delivery was lost more than 60 s before the drop was noticed, the panel
is not replayed. The pending-delivery buffer still covers every emit that
reported a failure. Rollback is a revert; no config or data migration.

## Test plan
`tests/offline/verify_reconnect_replay.py`. `make e2e` before deploy
(socket handler change).

## Decisions
None beyond the above.
