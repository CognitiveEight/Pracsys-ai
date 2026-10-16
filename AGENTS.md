# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `app/`. Use `app/socket/` for Socket.IO integration, `app/langgraph/` for chat and strategy workflows, `app/processing/` for document ingestion, `app/vectorstore/` for Qdrant access, and `app/core/` for configuration and lifecycle setup. Shared prompts are in `app/prompts/`, observability helpers in `app/observability/`, and workspace state in `app/workspace/`.

Operational files sit at the repo root: `requirements.txt`, `env.example`, `docker-compose.yml`, `run.sh`, and `Makefile`. Generated runtime data belongs under `logs/` and `data/workspaces/`; treat both as local artifacts, not source.

## Build, Test, and Development Commands
Use the `Makefile` first:

- `make setup` creates `venv/`, installs dependencies, copies `.env`, and prepares local folders.
- `make run` starts the service with `python -m app.main`.
- `make docker-up` starts local services such as Qdrant and Redis.
- `make docker-down` stops those containers.
- `make lint` runs `ruff check .` (rules in `pyproject.toml`); `pre-commit install` adds the commit gates.
- `make format` runs `black app/` when installed.

For manual setup, use `python3 -m venv venv`, `source venv/bin/activate`, and `pip install -r requirements.txt`.

## Coding Style & Naming Conventions
Follow existing Python style: 4-space indentation, type hints on public functions, short module docstrings, and `snake_case` for functions, variables, and modules. Use `PascalCase` for classes and keep configuration in `app/core/config.py`. Prefer small focused modules over large utility files.

Format with Black and lint with Pylint when available. Keep environment-specific values in `.env`; never hardcode secrets or service credentials.

## Testing Guidelines
There is no dedicated `tests/` package yet. Add new tests under `tests/` using `test_*.py` names so the suite can grow cleanly. Before opening a PR, run the relevant workflow manually with `make run` and validate socket, Qdrant, and document-processing paths you touched.

Note: `make test` currently references `test_setup.py`, which is not present. If you add automated tests, update that target as part of the same change.

## Commit & Pull Request Guidelines
Recent history uses concise Conventional Commit prefixes such as `feat:` and `fix:`. Keep that pattern, for example `fix: handle socket reconnect backoff`.

PRs should describe the user-visible behavior change, list config or schema impacts, and link the relevant issue or task. Include screenshots or payload examples for chat, reports, or socket contract changes.

## Security & Configuration Tips
Use `env.example` as the template for local configuration. Do not commit `.env`, API keys, or credential JSON files. When changing event payloads or auth behavior, update the matching docs under the repo root or `docs/` in the same PR.
