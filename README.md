# SimplerToday AI Service

SimplerToday is the **AI engine** that connects to your application backend over **Socket.IO**. It reads case documents, builds a **legal intelligence workspace**, and answers lawyer questions in natural language.

---

## In plain language: what does it do?

| Capability | What it means for users |
|------------|-------------------------|
| **Document intelligence** | Uploaded case files are read, assessed for quality, and stored for search so the AI can refer to what is actually in the record. |
| **Strategy & objectives** | The system can draft a **case summary**, **strategic objective**, and **possible next steps** from the documents, aligned to the workspace role (e.g. petitioner, respondent). |
| **Analysis report** | A structured view of **strengths**, **risks**, **missing information**, **score context**, and pointers for improvement. |
| **Document generation** | AI outputs can be transformed into **structured DOCX/report drafts** for legal review and downstream editing workflows. |
| **Important dates** | A **timeline / list of dates** derived from the materials where applicable. |
| **Applicable laws** | Suggestions for **relevant legal frameworks** tied to the matter (for research direction, not as final legal advice). |
| **Intelligence chat** | A **chat** where lawyers ask follow‑ups (explain a risk, compare options, procedural questions, hypotheticals). Replies aim to be **balanced and accurate**; workspace role mainly affects **panel** drafts, not an automatic advocacy slant in chat. |
| **Responsive during generation** | Long jobs (strategy, analysis, dates, laws) run in the background when possible so **chat can still answer** while other outputs are being prepared. |

Nothing here replaces a qualified lawyer or predicts court outcomes. Outputs support **professional judgment**, not **decisions**.

---

## Who should read this?

- **Lawyers, product, and programme managers** — read *In plain language* and *Trust and limitations* below.
- **Engineers and DevOps** — read *Technical overview*, *Configuration*, and *Running the service*. Deeper behaviour is described in [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md).

---

## Trust and limitations

- The AI **must not invent** facts, amounts, dates, or citations that are not grounded in supplied materials or clearly labelled general legal background.
- **Workspace role** (petitioner, respondent, etc.) mainly shapes how **panel outputs** (e.g. strategy blocks) are **generated**. **Intelligence chat** is configured for **neutral, accuracy‑first** analysis unless the user explicitly asks for a one‑sided view.
- Use generated content as **drafting and thinking support**, subject to review by qualified counsel and local rules.

---

## Technical overview

- **Runtime:** Python 3.11+, asyncio.
- **AI model:** Google **Gemini** (configurable).
- **Vectors:** **Qdrant** for embeddings and workspace‑scoped search.
- **Transport:** **Socket.IO client** — listens for workspace and chat events from your backend and emits structured payloads and chat replies.

### Main Socket.IO events (summary)

**Inbound (backend → AI)**

- `workspace_created` — new workspace and evidence files.
- `workspace_sync` — workspace updates (files added, changed, or removed).
- `user_message_request` — user message for **intelligence chat**.
- `probing_prompts_request`, `analysis_report_feedback`, heartbeat events as per your backend contract.

**Outbound (AI → backend)**

- `document_feature_response` — payloads such as `document_assessments`, `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`, `workspace_metadata`, etc.
- `ai_response` — natural language chat reply.

Exact payload shapes are defined in code (`app/socket/schemas.py`) and integration notes under `docs/` where present.

---

## Repository layout

This section matches the idea of [`STRUCTURE.txt`](STRUCTURE.txt) (printable ASCII tree) but is **updated** for the current legal-AI codebase. Use either this README or `STRUCTURE.txt` to find your way around; for deep behaviour, see [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md).

### Top of the repository

```
simplertoday/
├── app/                          # All Python application code (table below)
├── docs/                         # Integration notes (when present)
├── logs/                         # Created when the app runs (gitignored)
├── data/                         # Local/cache data (gitignored)
├── env.example                   # Template → copy to .env
├── requirements.txt
├── docker-compose.yml            # Optional: Qdrant, Redis, etc.
├── Dockerfile                    # Optional container image
├── Makefile                      # make setup | run | docker-up | …
├── run.sh                        # Optional: shell helper to start the service
├── STRUCTURE.txt                 # Detailed folder tree (companion doc)
├── README.md                     # This file
└── SYSTEM_ARCHITECTURE.md       # Event flow & multi-agent design
```

### Inside `app/` — what each folder is for

| Path | In plain terms |
|------|----------------|
| `main.py` | Starts the program and handles graceful shutdown. |
| `socket/` | Socket.IO **client**: connect to your backend, handle events, emit AI results. |
| `langgraph/` | **Strategy generation** and **intelligence chat** workflows (step-by-step AI). |
| `prompts/` | Legal system prompts (chat behaviour, guardrails, role context). |
| `ai/` | **Google Gemini** client: text generation, chat, embeddings. |
| `processing/` | Document download, text extraction, batch processing, readability. |
| `vectorstore/` | **Qdrant**: embeddings and semantic search **per workspace**. |
| `workspace/` | Workspace state and manager (open matter context). |
| `agents/` | Agent types, routing, shared chat helpers. |
| `core/` | Configuration (`config.py`), app lifecycle, security utilities. |
| `observability/` | Logging, audit trail, tracing. |
| `utils/` | Shared exceptions and small helpers. |

### Folders created automatically

| Location | Purpose |
|----------|---------|
| `logs/` | App logs (including `logs/audit/` when enabled). |
| `data/workspaces/` | Per-workspace file cache / downloads when that path is used. |

Do not rely on these being in Git — they appear after the first runs.

### Dependencies (external systems)

| System | Role |
|--------|------|
| **Socket.IO server** | Your app backend; **required** for production use. |
| **Google Gemini** | Primary LLM (`GOOGLE_API_KEY`); see `env.example`. |
| **Qdrant** | Vector database for document search; often run via Docker. |
| **Redis** | Optional caching (see `docker-compose.yml` / `config.py`). |
| **GCP / Vision** | Optional OCR or vision keys if you enable those features. |

### Quick commands

| Goal | Command |
|------|---------|
| Install deps only | `make install` |
| First-time setup (venv, deps, `.env` from template, folders) | `make setup` |
| Run the AI service | `make run` or `python -m app.main` |
| Smoke test | `make test` *(currently expects `test_setup.py`; add tests before relying on this target)* |
| Start Docker services (e.g. Qdrant) | `make docker-up` |
| Stop Docker services | `make docker-down` |

---

## Configuration

1. Copy the environment template to a local `.env` file (settings load from `.env`):

   ```bash
   cp env.example .env
   ```

2. Set at least:

   - **Socket:** `SOCKET_URL`, `SOCKET_SECRET_KEY`, `SOCKET_AI_PAYLOAD` (per your backend).
   - **AI:** `GOOGLE_API_KEY` for Gemini; optional GCP fields for OCR or other Google services.
   - **Vectors:** `QDRANT_URL` (and API key if used).

See `env.example` for all variables and comments.

---

## Running the service

**Qdrant** (if local):

```bash
docker run -p 6333:6333 qdrant/qdrant
```

**Install and run:**

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

---

## Observability

- Console logging (Loguru).
- Log files under `logs/` when configured (see `app/observability/logger.py`).
- Audit and trace helpers under `app/observability/`.

---

## Security notes

- Do **not** commit `.env`, API keys, or Google **service account** JSON; use `env.example` as a template only.
- Socket authentication follows your backend’s **HMAC / secret** contract.

---

## Contributing and support

- **Architecture and event flow:** [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md)
- **Additional docs:** see `docs/` and topic‑specific `*.md` files at the repo root where maintained.

---

## License

MIT