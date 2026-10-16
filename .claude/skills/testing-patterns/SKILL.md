---
name: testing-patterns
description: How to write tests for the SimplerToday AI service - pytest unit tests and tests/offline/verify_*.py handler suites with fake socket, Qdrant and model clients. Use when adding or changing tests, turning acceptance criteria into checks, or when a change touches socket handlers, panels, ingestion, the router or the purge.
---

# Testing patterns for this repo

`make test` runs `python -m pytest -q tests` and then every
`tests/offline/verify_*.py` as a script; any failure fails the build.
Nothing in `make test` may call a real model, Qdrant collection, GCS bucket,
Neo4j or the backend. `make e2e` is the only billed run, and a human starts it.

## Which kind of test

| Change | Put the check in |
|---|---|
| Pure function, parser, router, pricing, schema | `tests/test_<area>.py` (pytest, `asyncio_mode = auto`) |
| Socket handler behaviour, emits, stop/resume, purge, sharing | `tests/offline/verify_<area>.py`, a new check group |
| Needs a local Qdrant | Mark it skipped when Qdrant is unreachable, like `tests/test_tables.py` |

## Offline verify suite shape

Follow `tests/offline/verify_fact_corrections.py` and `verify_run_control.py`:

- Insert the repo root on `sys.path`, then import the real modules.
- `RESULTS = []` plus a `check(name, ok, detail="")` helper that prints
  `PASS` / `FAIL`. Never `assert`, so every check runs and reports.
- One function per check group; a `CHECKS = [("group-name", fn), ...]` list;
  `--list` prints the group names; `main()` returns 1 on any failure.
- Drive handlers through `event_handler.handle(...)` so run scopes, logging
  context and the stop fence apply, as production does.

## Fakes that must behave like the real thing

- **Socket double: `emit` returns `True`.** `SocketClient.emit` reports
  delivery. A double returning `None` reads as "dropped", and the handlers
  buffer for re-delivery and report failure.

  ```python
  class _FakeSocket:
      async def emit(self, event, payload):
          EMITS.append((event, payload))
          return True

      async def emit_ai_response(self, payload):
          EMITS.append(("ai_response", payload))
          return True
  ```
- **Model calls:** stub at `gemini_client` / the router, and return the
  error sentinel in at least one check so `is_generation_error()` paths run.
- **Persistence:** record writes (see `_record_persist` in
  `verify_run_control.py`) so a check can assert what was persisted, not only
  what is in memory.

## Settings

Pin every setting a check depends on and restore it afterwards, so a
developer's local `.env` cannot change the result:

```python
old = settings.embedding_provider
settings.embedding_provider = "gemini"
try:
    ...
finally:
    settings.embedding_provider = old
```

Use `.invalid` hostnames for fake endpoints (`https://gate0.example.invalid/v1`).

## Cases every handler change should cover

- Duplicate re-send under the same `requestId`.
- `workspace_stop_request` mid-run: nothing stale reaches the socket.
- Disconnect during generation: payload buffered and re-sent.
- Every armed `workspace:refresh` loader gets a terminal emit.
- `view` collaborator on a shared workspace: no mutation.
- Model error, timeout and safety block.

## Rules

- A test must fail without the change it covers. If it would pass on the old
  code, say so.
- Fictional data only: no real names, case text, phone numbers or ids.
- Never weaken or delete an existing check to make a run pass; report it.
- `tests/offline/verify_logging.py` must stay green: no new log line with
  payloads, text, names or URLs.
