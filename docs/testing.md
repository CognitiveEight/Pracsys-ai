# Testing

How the service is tested, what each suite covers, and what a new test must
do. The rules come from `CLAUDE.md`; `.claude/skills/testing-patterns/` has
the patterns in more detail.

## Commands

| Command | What it runs | Cost | Who runs it |
|---|---|---|---|
| `make lint` | `ruff check .` (rules in `pyproject.toml`) | free | anyone, any time |
| `make test` | `python -m pytest -q tests`, then every `tests/offline/verify_*.py` as a script | free, offline | anyone, any time |
| `make e2e` | `python scripts/e2e_local_backend.py` | **real, billed model calls** | a human, before a deploy |

Install the dev tools first: `pip install -r requirements-dev.txt` (pytest,
pytest-asyncio, ruff, pre-commit). `make setup` installs only
`requirements.txt`.

`make test` stops at the first failing offline suite and prints its last 30
lines. For each passing suite it prints only the summary line. Run one suite
on its own with `python tests/offline/verify_<area>.py`; most accept `--list`
to print their check groups.

pytest settings are in `pytest.ini`: `asyncio_mode = auto`,
`testpaths = tests`.

**Settings must load.** Importing `app.core.config` builds `Settings()`, which
requires `SOCKET_URL` and `SECRET_KEY`, and `verify_rag.py` needs
`GOOGLE_API_KEY` to be set (its client is faked, so no call is made). With a
local `.env` these are present. In a clean checkout or a CI job, set dummy
values in the environment:

```bash
SOCKET_URL=http://test.example.invalid SECRET_KEY=test GOOGLE_API_KEY=test make test
```

Because `make test` prints only each suite's summary line, a skipped group
(for example `verify_rag.py` without Qdrant) still shows as passed. Run the
suite on its own to see what was skipped.

## What each suite covers

### pytest (`tests/test_*.py`)

| File | Covers |
|---|---|
| `test_chat_state.py` | The intelligence-chat graph declares every state key its nodes read or write, so LangGraph cannot drop them (citations were lost this way) |
| `test_document_wording_and_dates.py` | Upload wording (Annexure N) and dates taken from file names |
| `test_entity_resolution.py` | Entity resolution: merges must be earned; names whose numbers differ never merge |
| `test_extraction_and_contradictions.py` | Ontology-constrained fact extraction and contradiction detection |
| `test_graph_store.py` | The Neo4j adapter, against a fake driver and session |
| `test_llm_gate0.py` | The Gate0 adapter and `GATE0_ENFORCE` governance, with a fake OpenAI client |
| `test_llm_providers.py` | Request and response mapping of each provider adapter, against fake SDK clients |
| `test_llm_router.py` | Routing, fallback, capability checks, cost and the facade contract; fails if a provider SDK is imported outside `app/llm/providers/` |
| `test_model_selection.py` | Per-case model choice (`aiModel`): routing, overrides, pinned tasks |
| `test_ocr.py` | Scanned-PDF OCR: native-photo path, render fallbacks, reading order, page notes, retries; fake Vision client |
| `test_office_extraction.py` | `.xlsx` and `.pptx` evidence routed by `extension` and read, not misrouted to the DOCX parser |
| `test_ontology_schema.py` | Ontology schema write-time invariants |
| `test_pdf_boilerplate.py` | The running-header strip never deletes body words |
| `test_retrieval.py` | The agentic retrieval loop: plan, route, assemble, critique, verify |
| `test_statute_index.py` | `check_provision` against the India Code section index |
| `test_statutes.py` | Statutory register and resolver; generated Neo4j DDL |
| `test_tables.py` | Spreadsheet evidence end to end: parse, column redaction, render, store, compute, cite |
| `test_temporal.py` | Date normalisation: day-first, precision-aware |
| `test_usage_ledger.py` | Per-case cost ledger: every paid unit reaches its workspace exactly once |

### Offline suites (`tests/offline/verify_*.py`)

These drive the real handlers with the model calls and the socket stubbed.
None needs an API key or a backend.

| File | Covers |
|---|---|
| `verify_document_reload.py` | `document_reload_request`: purge before re-read, identity kept, `currentState` honoured, re-send dedupe and retry after failure |
| `verify_fact_corrections.py` | Chat-driven fact corrections and panel edits, from `on_user_message_request` to `document_feature_response` |
| `verify_ingestion.py` | PDF boilerplate strip and the non-party filter in entity extraction |
| `verify_logging.py` | Static scan of every log call in `app/` (no f-strings, payload dumps, raw URLs, content slices or emoji) and the log sinks |
| `verify_missing_info_edit.py` | `missing_information_edited`: text-only edit, survives regeneration, dedupe and stale edits |
| `verify_objective_change.py` | Objective change: case-summary re-frame limits, quality-score explanation refresh, loader release on failure |
| `verify_rag.py` | Chunking, hybrid search, re-indexing, deletion from both collections, citations, the knowledge-graph layer (fake graph) |
| `verify_run_control.py` | `workspace_stop_request` / `workspace_resume_request`: the stop fence, cancellation, buffer drop, resume emits only what it declared |
| `verify_workspace_share.py` | Sharing: `participants` roster, `askedBy`, a `view` collaborator cannot mutate the case |

## Local services and how tests skip

Nothing in `make test` calls a real model, GCS bucket, backend or Neo4j.

| Needs | Suite | Without it |
|---|---|---|
| Local Qdrant on `localhost:6333` (`make docker-up`) | `verify_rag.py` store checks | Prints "Qdrant not reachable … store checks skipped"; the chunker, citation, metrics and model-client checks still run |
| Local Qdrant (`TEST_QDRANT_URL`, default `localhost:6333`) | `test_tables.py::test_row_store_round_trip_and_both_deletion_paths` | `pytest.mark.skipif`, reason "no local Qdrant" |
| Qdrant, if reachable | `verify_missing_info_edit.py`, `verify_objective_change.py`, `verify_workspace_share.py` | Write-through is used when Qdrant is up and cleaned up afterwards; the checks run either way |
| `fitz` (PyMuPDF), `openpyxl`, `python-pptx` | `test_ocr.py`, `test_tables.py`, `test_office_extraction.py` | `pytest.importorskip` skips the module |

Suites that touch Qdrant use uniquely named collections or ids per run and
delete them at the end. Neo4j is never needed: graph tests use fakes.

## `make e2e`

`scripts/e2e_local_backend.py` starts the real service
(`python -m app.socket.ai_service`) against a local stand-in backend on
`127.0.0.1`, and drives:

1. the v1 handshake (a mis-signed client is refused);
2. `workspace_created` with two fictional evidence files, then waits for all
   seven panels;
3. `ai_heartbeat_ping` → `ai_heartbeat_pong`;
4. `user_message_request` → `ai_response` with citations;
5. `workspace_stop_request` and `workspace_resume_request`;
6. `workspace_deleted` with the purge due now, then checks Qdrant is empty;
7. shutdown, then a scan of the service log for errors and for the fictional
   party name.

It uses a throwaway Qdrant collection and no knowledge graph, and never
touches the real backend. It reads the model keys from `.env`, so the model
calls are **real and billed**. It takes several minutes.

A human runs it. Agents do not: `.claude/settings.json` denies it. It is
required before a deploy when a change touches socket handlers, panel agents
or document processing, because those have no full automated coverage.

## Writing a test

- **Where:** a pure function, parser, router or schema goes in
  `tests/test_<area>.py`. Handler behaviour (emits, stop/resume, purge,
  sharing) goes in a check group in `tests/offline/verify_<area>.py`.
- **Offline suite shape:** follow `verify_run_control.py`: a
  `check(name, ok, detail="")` helper that records instead of asserting, one
  function per group, a `CHECKS` list, `main()` returning 1 on any failure.
  Drive handlers through `event_handler.handle(...)` so run scopes, logging
  context and the stop fence apply.
- **Fakes:** stub model calls at `gemini_client` / the router, and return the
  error sentinel in at least one check so `is_generation_error()` paths run.
  Fake Vision, SDK and graph clients as the existing suites do.
- **A socket double's `emit` must return `True`.** `SocketClient.emit`
  reports delivery. A double that returns `None` reads as "dropped", and the
  handlers buffer the payload for re-delivery and report failure.
- **Pin settings** a check depends on and restore them afterwards, so a
  developer's local `.env` cannot change the result (for example
  `settings.embedding_provider`). Use `.invalid` hostnames for fake
  endpoints.
- **Fictional data only:** no real names, case text, phone numbers or ids.
- A test must fail without the change it covers. Never weaken or delete an
  existing check to make a run pass.
- In a spec, each check maps to an acceptance criterion in
  `specs/acceptance-criteria/SIM-<n>-….md` (see `specs/README.md`).

## CI

`.github/workflows/ci.yml` (SIM-23) runs three required checks on every PR
into `development`, `staging` and `main`: `lint` (`make lint`), `test`
(`make test`, with a real Qdrant service so the store checks run instead of
skipping) and `secrets-scan` (gitleaks over the PR's commits). It is offline
and free: no secrets, no model calls, and never `make e2e`. Run `make lint`
and `make test` locally before handing a branch over;
`docs/development-workflow.md` lists the gates.
