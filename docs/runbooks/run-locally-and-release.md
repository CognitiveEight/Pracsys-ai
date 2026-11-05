---
title: Run locally and prepare a release
type: runbook
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Before you start"
last_verified: 2026-10-21
jira: SIM-113
risk: medium
environments: [local, staging, production]
---

# Run locally and prepare a release

## In plain words

This is how an engineer runs the AI service on a laptop and how a change
travels to customers. Locally, the service needs a database for documents,
a set of private settings and keys, and the automated checks passing. It does
nothing visible on its own: it waits for the main application to connect and
send it work. A change moves through three stages, from the shared development
line to a staging copy and then to production, and a person approves each move.
Before a release, one extra test runs the real service end to end on a small
made-up case. That test calls the paid AI models, so only a person starts it.
The risk is medium: a skipped check can ship a broken panel or a billing error.

## Before you start

- Python 3.11 or later (the Docker image uses 3.12), Docker with
  `docker-compose`, `ffmpeg` and `poppler-utils` on PATH, and `jq`.
- The `.env` values, from the tech lead. Never commit, paste or share them.
- For a release: you are the release owner, and every change being promoted
  has merged to `development` through a reviewed PR.

### Environment variables (names only)

Required to start (`app/core/lifecycle.py` refuses to start without the first
two, and `SECRET_KEY` must be set in production):

| Name | Purpose |
|---|---|
| `SOCKET_URL` | Backend Socket.IO server to connect to |
| `SOCKET_SECRET_KEY` | Shared secret that signs the handshake |
| `SOCKET_AUTH_SCHEME` | `v1` (default). `legacy` is rollback only, see [roll-back-the-handshake-auth.md](roll-back-the-handshake-auth.md) |
| `SOCKET_AI_PAYLOAD` | Needed only when `SOCKET_AUTH_SCHEME=legacy` |
| `GOOGLE_API_KEY` | Gemini, the default model provider |
| `QDRANT_URL` | Vector store (`QDRANT_API_KEY`, `QDRANT_COLLECTION_NAME` optional) |
| `SECRET_KEY` | Service secret |

Optional groups, per feature: `GOOGLE_APPLICATION_CREDENTIALS` (OCR, a file
path), `SPEECH_GOOGLE_APPLICATION_CREDENTIALS`, `GCS_BUCKET_NAME`,
`CHIRP_REGION`, `AUDIO_*` (audio), `GCS_DOCX_BUCKET_NAME` (DOCX export),
`GCP_PROJECT_ID`, `VERTEX_SEARCH_*` (law search fallback), `NEO4J_URL`,
`NEO4J_PASSWORD`, `ONTOLOGY_INGEST_ENABLED`, `RETRIEVAL_PANELS`,
`RETRIEVAL_MODE` (knowledge graph), `GATE0_*` and `LLM_*` (model routing and
spend), `LOG_LEVEL`, `LOG_DIAGNOSE`, `ENABLE_AUDIT_LOG`. `env.example` lists
every name with a comment. Redis variables exist but no application code uses
Redis today.

## Steps

### 1. Set up a fresh clone (once)

```bash
make setup                              # venv/, deps, .env from env.example, logs/audit and data/workspaces
source venv/bin/activate                # older checkouts use env/ instead
pip install -r requirements-dev.txt     # pytest, ruff, pre-commit
pre-commit install                      # gitleaks + ruff on every commit
```

Expected: `Setup complete!` and a `.env` file. Fill `.env` from the tech lead's
values.

### 2. Start the local databases

```bash
make docker-up          # Qdrant and Redis from docker-compose.yml
make graph-up           # optional: Neo4j, needs NEO4J_PASSWORD
```

Expected: `docker-compose ps` shows `qdrant` (and `redis`) up.

### 3. Lint and test

```bash
make lint               # ruff check .
make test               # pytest tests, then every tests/offline/verify_*.py
```

Expected: both exit 0. `make test` prints one summary line per offline suite
and stops at the first failing one. `tests/offline/verify_rag.py` needs the
local Qdrant from step 2.

One test or suite:

```bash
python -m pytest -q tests/test_ocr.py -k <name>
python tests/offline/verify_rag.py
```

### 4. Run the service

```bash
make run                # python -m app.main
```

Expected: the startup log, then `Socket connected (handshake #1, scheme=v1, …)`
once a backend is reachable at `SOCKET_URL`. Nothing else happens until the
backend sends events. Follow one run in `logs/app.log` by its `request_id`,
from `event <name> received` to `result <featureType> delivered=yes`.

### 5. Before opening a PR

- `make lint` and `make test` green.
- `CHANGELOG.md` has an entry under `[Unreleased]`. Call out any socket payload
  change.
- If socket handlers, panels or document processing changed, a human runs
  `make e2e` (step 6).
- Label the PR `needs-human-review` if it changes a socket payload, the role
  gate, the workspace purge, PII handling, billing or model routing.

### 6. Pre-deploy end-to-end run (human only, billed)

```bash
make e2e                # python scripts/e2e_local_backend.py
```

It starts the real service against a local stand-in backend, with a throwaway
Qdrant collection and no knowledge graph. It checks the signed handshake, all
seven panels, heartbeat, chat with citations, stop and resume, the purge and
a clean shutdown, then scans the service log for errors and for the fictional
party name. It reads model keys from `.env`, so the model calls are real and
billed (a few cents). It takes about 5 to 15 minutes. Options: `--keep`,
`--feature-timeout`, `--chat-timeout`, `--log-dir`, `--evidence`.

Expected: every check passes. Claude Code is blocked from running it.

### 7. Prepare the release

The branch flow is:

```
feature/SIM-<n>-… or fix/SIM-<n>-…  ──PR──►  development  ──PR──►  staging  ──PR──►  main
```

In Claude Code:

```
/release-check staging        # later: /release-check main
```

Expected: the commits being promoted with their Jira keys (commits without a
key flagged), lint and test results, a CHANGELOG check, whether `make e2e` is
required, and a checklist. The release owner then:

- runs `make e2e` if required;
- confirms `needs-human-review` PRs were approved by the tech lead;
- runs the gate0 spend check on staging
  ([reconcile-gate0-spend.md](reconcile-gate0-spend.md));
- checks `requirements.lock.txt` is what was tested (the Docker image installs
  it, not `requirements.txt`);
- moves the `[Unreleased]` entries in `CHANGELOG.md` under a version and date,
  and adds a section to `RELEASE.md` (newest first, `# Release vX.Y.Z - <date>`);
- opens and merges the promotion PRs, and deploys.

## How to check it worked

- Locally: `make lint` and `make test` exit 0, and `make run` logs a v1
  handshake.
- After a deploy: the log shows `Socket connected … scheme=v1`, heartbeats,
  and for one test workspace `event … finished … status=ok` followed by
  `result <featureType> delivered=yes` lines.

## If something goes wrong / how to undo

| Symptom | Likely cause | Fix |
|---|---|---|
| Startup stops with `SOCKET_URL is required` or `SOCKET_SECRET_KEY is required…` | `.env` incomplete | Fill the value |
| Every handshake refused | Clock drift over 300 s, or a secret mismatch | Check NTP; see [roll-back-the-handshake-auth.md](roll-back-the-handshake-auth.md) |
| `verify_rag.py` fails to connect | Local Qdrant not running | `make docker-up` |
| A suite passes alone but `make test` fails | `make test` stops at the first failing offline suite | Read the last 30 lines it prints |
| Bad release in production | A regression shipped | Redeploy the previous image tag. Payload changes may also need the backend team. |

`make docker-down` stops the local databases. `make clean` removes caches and
`*.log` files.

### Known gaps

- No CI and no GitHub branch protection. Local hooks and review are the only
  gates.
- `make e2e` does not cover most socket handlers, the panel agents or
  document processing. Check those by hand with `make run` against a backend.
- How the service is deployed (image registry, tags, where `.env` lives in
  production) is not in this repo; this page could not verify it.
- `make help` and `dev.sh test` are out of date (`dev.sh test` calls a missing
  `test_setup.py`). Use the commands above.
- `make e2e` starts `python -m app.socket.ai_service`, while `make run` and the
  Docker image start `python -m app.main`.

## Who to call (roles)

- Tech lead: `.env` values, `needs-human-review` approvals.
- Release owner: promotion PRs and deploys.
- Backend team: handshake refusals and payload changes.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
