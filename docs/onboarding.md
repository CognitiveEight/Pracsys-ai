# Onboarding: SimplerToday AI service

Start here if you are new to this repository. This page gives you the order to
read things in, the vocabulary, and the path from a fresh clone to your first
merged change. It links to the detailed documents rather than repeating them.

| If you are… | Read next |
|---|---|
| A developer joining the team | This page, top to bottom |
| Setting up Claude Code on a new account or machine | [`claude-code-setup.md`](claude-code-setup.md) |
| Moving the service to new infrastructure, a new repo or a new cloud account | [`migration-guide.md`](migration-guide.md) |

---

## 1. What the service is, in one paragraph

SimplerToday AI is a Python asyncio **Socket.IO client** with no HTTP API. It
connects *out* to the application backend, receives events when a lawyer
creates or changes a case (a "workspace") or sends a chat message, and emits
AI results back. For each case it reads the uploaded evidence (PDF, scans,
Office files, spreadsheets, audio), stores it in Qdrant, and generates a fixed
set of **panels** (strategy, analysis report, list of dates, applicable laws,
procedural timeline and others). It also answers chat questions with
citations it has checked. Everything is framed for Indian law.

```
Backend (Socket.IO server)
   │  workspace_created / workspace_sync / user_message_request / …
   ▼
app/socket/client.py ─► SocketEventHandler (app/socket/events.py + handlers/*)
   ├─ role gate ─ nothing runs without a validated role
   ├─ ingestion ─ app/processing → app/rag → Qdrant (+ optional Neo4j graph)
   ├─ panels    ─ document_feature_response (one per featureType)
   └─ chat      ─ intelligence chat graph → ai_response with citations
```

## 2. Vocabulary

| Term | Meaning here |
|---|---|
| **Workspace** | One legal case. Keyed by `workspaceId`. `userId` on every event is always the case **owner**. |
| **Evidence files** | The documents uploaded to a workspace. Stored as chunks in the `<collection>_evidence_v1` Qdrant collection. |
| **Role** | The side the lawyer acts for: ADJUDICATOR, PETITIONER, RESPONDENT or MEDIATOR. The **role gate** (`app/langgraph/role_agent.py`) blocks every downstream agent until a role is validated. |
| **Objective** | The lawyer's strategic goal for the case. Changing it re-frames strategy, the summaries and the quality score explanation. |
| **Panel / featureType** | One output section in the UI. Emitted as `document_feature_response` with `featureType` = `document_assessments`, `workspace_metadata`, `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`, `procedural_timeline` and others. |
| **Loader** | A spinner the frontend shows per featureType after `workspace:refresh`. Only a `document_feature_response` for that featureType clears it. |
| **Manifest** | The cached `document_assessments` payload. Every prompt treats it as the ground truth for which documents exist. |
| **Run scope / stop fence** | Each workspace event runs under `(workspace_id, epoch)`. A Stop bumps the epoch, and work from the old epoch can no longer emit. |
| **Purge** | `workspace_deleted`: a compliance erasure of every AI-held copy of a case. |
| **Participants** | Collaborators on a shared case (`edit` or `view`). They are app users, **not** parties to the dispute. |
| **Gate0** | The model-call governance and spend gateway. With `GATE0_ENFORCE` on, every model call must go through the router. |
| **Router** | `app/llm/`: sends each model call to the provider configured for its task. `gemini_client` is a facade over it. |

## 3. Reading order

Do these in order. It takes about half a day.

1. `README.md`: the product view and the external systems.
2. [`architecture.md`](architecture.md): the code map and links to every
   detailed document.
3. `CLAUDE.md`: the **rules**. Read every section whose heading starts with a
   claim ("The role gate is non-bypassable", "Stop is a fence…", "Deleting a
   workspace must erase every copy"). Each one records an incident or a
   compliance requirement. Code review enforces them.
4. [`SOCKET_EVENTS_ARCHITECTURE.md`](SOCKET_EVENTS_ARCHITECTURE.md): how
   `events.py` is assembled from per-feature mixins, and where to add an event
   or a panel.
5. [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md): the full event flow.
6. [`development-workflow.md`](development-workflow.md): how a change moves
   from Jira to production.
7. Diagrams: `system-architecture.drawio` (12 pages) and
   `system-architecture-layered.drawio`. Open them in diagrams.net or the
   draw.io VS Code extension.

Read these only when you work in the area they cover:
[`MODEL_PORTABILITY.md`](MODEL_PORTABILITY.md) (model providers),
[`AI_SERVICE_AUTH_MIGRATION.md`](AI_SERVICE_AUTH_MIGRATION.md) (socket
handshake),
[`AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md`](AGENTIC_ONTOLOGY_RAG_FOUNDATIONS.md)
(knowledge graph), and
`CHANGELOG.md` / `RELEASE.md` (history).

## 4. Local setup

Prerequisites: Python 3.11 or later (the Docker image uses 3.12), Docker
with docker-compose, `ffmpeg` and `poppler-utils` on PATH (audio and scanned
PDFs), and `jq` (used by the Claude Code hooks).

```bash
make setup                               # venv/, deps, .env from env.example, logs/ and data/ folders
source venv/bin/activate                 # some existing checkouts use env/ instead
pip install -r requirements-dev.txt      # ruff, pytest, pre-commit
pre-commit install                       # gitleaks + ruff on every commit
make docker-up                           # Qdrant (and Redis) locally
make lint && make test                   # both must be green before you change anything
```

Fill `.env` from `env.example`. Get the values from the tech lead. **Never
commit, paste or share them.** The minimum for `make run` is `SOCKET_URL`,
`SOCKET_SECRET_KEY`, `SOCKET_AI_PAYLOAD`, `GOOGLE_API_KEY`, `QDRANT_URL` and
`SECRET_KEY`. OCR, audio and Vertex AI Search also need a GCP service account,
which you reference by path through `GOOGLE_APPLICATION_CREDENTIALS`.

`make run` starts the service. It does nothing visible until a backend
connects and sends events. The log lines to look for are listed in
CLAUDE.md under "Reading a run in production".

## 5. How you are expected to work

Every change follows the same path. The full version is in
[`development-workflow.md`](development-workflow.md).

```
Jira ticket (SIM-<n>)
  → specs/ (requirements, technical design, acceptance criteria; design approved)
  → branch feature/SIM-<n>-short-name or fix/SIM-<n>-short-name, from development
  → code + tests, make lint and make test green, CHANGELOG.md updated
  → PR to development (human review; needs-human-review label when required)
  → development → staging → main (release owner)
```

The Jira project key is **SIM**. Each step has a Claude Code command that runs
it: `/spec`, `/implement`, `/verify`, `/review` and `/release-check`. Each step
also leaves a record in the Confluence space **SimplerToday – SDLC
Templates**. See [`claude-code-setup.md`](claude-code-setup.md).

What is enforced, not just documented:
- No commits on `development`, `staging` or `main`, and no `--no-verify`.
- gitleaks runs on every commit.
- `make lint` (ruff) must pass.
- Agents cannot push, merge or run `make e2e`. A human does those.

## 6. Testing: what is covered and what is not

| Layer | How it is checked |
|---|---|
| Ontology, graph, retrieval, statutes, OCR, tables, router, usage ledger | `pytest tests` (fakes, no network) |
| Document reload, fact corrections, ingestion, logging, missing-info edit, objective change, RAG, run control, sharing | `tests/offline/verify_*.py`, plain scripts run by `make test`. `verify_rag.py` needs a local Qdrant. |
| Socket handshake, all seven panels, chat with citations, stop/resume, purge, shutdown | `make e2e`. It makes **real, billed** model calls. A human runs it before a deploy. |
| Most socket handlers, panel agents and document processing | **No automated tests.** Verify by hand with `make run` against a connected backend. |

To run one test: `python -m pytest -q tests/test_ocr.py -k <name>`. To run one
offline suite: `python tests/offline/verify_rag.py`.

## 7. Your first week

1. Get access: the GitHub repository, Jira project SIM, the Confluence
   space, the GCP project (read-only is enough to start), and a `.env` from
   the tech lead.
2. Complete section 4 until `make test` is green.
3. Run the service against the staging backend with a fictional test case.
   In `logs/app.log`, follow one `request_id` from `event … received` to its
   `result <featureType> delivered=yes` lines.
4. Pick a small ticket. Run it through `/spec` → `/implement` → `/verify` →
   `/review` with a reviewer watching.
5. Read the last two entries in `RELEASE.md` to see what recently changed.

## 8. Data you must never handle casually

The service processes privileged legal material. Never put any of the
following in code, tests, specs, commits, logs, chat with an AI assistant, or
tickets:
- case text;
- party, participant or client names;
- phone numbers;
- objective text;
- credentials.

Tests use fictional data only. Credential files sit in the working tree for
local runs (`.env`, `bns-ai-*.json`, `gcp-*.json`,
`application-service-*.json`, `*Creds*.txt`). They are git-ignored and
blocked for Claude Code. Do not open them in shared screens either.

## 9. Known gaps

- CI is not set up yet (GitHub Actions running `make lint` and `make test`),
  and neither is branch protection on GitHub. Local hooks and human review are
  the only gates.
- Several modules are very large (`analysis_report.py`, `chat.py`,
  `workspace_events.py`, `drafting_agent.py`). Splitting them is planned
  work.
- `dev.sh test` calls a `test_setup.py` that no longer exists. Use
  `make test`.
- `AGENTS.md` (for non-Claude agents) is out of date: it says there are no
  tests, and it names Pylint. Trust `CLAUDE.md` and this page where they
  disagree.
