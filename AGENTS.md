# AGENTS.md

A short pointer for coding agents other than Claude Code (Codex and others).
**`CLAUDE.md` is authoritative.** Read it before changing anything; if this
file and `CLAUDE.md` disagree, `CLAUDE.md` wins.

## What the service is

SimplerToday AI is a Python 3.11 asyncio **Socket.IO client**, not a server.
It connects out to the application backend, receives workspace and chat
events, and emits AI results back. There is no HTTP API. `python -m app.main`
is the only entry point. Start with `docs/architecture.md`.

## Commands

| Command | What it does |
|---|---|
| `make setup` | Creates `venv/`, installs `requirements.txt`, copies `env.example` to `.env`, makes `logs/` and `data/` |
| `pip install -r requirements-dev.txt` | pytest, ruff and pre-commit (not installed by `make setup`) |
| `pre-commit install` | Commit gates: gitleaks, private-key and large-file checks, ruff |
| `make run` | `python -m app.main` |
| `make lint` | `ruff check .` (rules in `pyproject.toml`) |
| `make test` | `pytest tests`, then every `tests/offline/verify_*.py`; any failure fails the run |
| `make e2e` | `scripts/e2e_local_backend.py` against a local stand-in backend |
| `make docker-up` / `make docker-down` | Local Qdrant and Redis |

`make e2e` makes **real, billed model calls**. A human runs it, before a
deploy, when a change touches socket handlers, panel agents or document
processing. Agents do not run it. Nothing in `make test` may call a real
model, Qdrant collection, GCS bucket, Neo4j or the backend.

## Rules (summary)

The full rules, and why, are in `CLAUDE.md`. The short version:

- **Data:** never read, print, log or commit `.env` or credential files
  (`bns-ai-*.json`, `gcp-*.json` and similar). No case text, names, phone
  numbers or objective text in code, tests, specs, commits or logs. Tests use
  fictional data.
- **Logging:** Loguru with `{}` placeholders. Log ids and counts, never
  payloads, prompts, model output, document text or URLs.
- **Role gate:** nothing downstream runs without a validated role.
- **Socket handlers:** long work through `_track_background_task`; emits
  through `self._socket_client` / `_emit_document_feature` (the stop fence);
  every armed `workspace:refresh` loader gets a terminal emit; catch
  `asyncio.CancelledError` before `except Exception` and re-raise it.
- **Model calls:** only through `gemini_client` (the provider-neutral router).
  No provider SDK import outside `app/llm/providers/`. Check
  `is_generation_error()` on every result. Meter paid calls once, at the
  lowest call site.
- **Storage:** write chunks with `add_document_chunks`; delete only through
  `delete_documents` / `delete_workspace`. Any new per-workspace state must be
  reached by the workspace purge, which is a compliance erasure.
- **Shared workspaces:** chat paths that change workspace state are gated on
  `mutations_allowed`.
- **Indian law:** no IPC rows in `applicable_laws`.

## How work is done

Jira project `SIM`. Every non-trivial change has a spec folder
`specs/SIM-<n>-short-name/` and a branch `feature/SIM-<n>-short-name` (or
`fix/…`) off `development`. Commits: `feat(SIM-<n>): …`, `fix(SIM-<n>): …`,
`docs(SIM-<n>): …`. Never commit on `development`, `staging` or `main`; never
push, merge or use `--no-verify`. See `docs/development-workflow.md` and
`specs/README.md`.
