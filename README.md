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
| **Procedural timeline** | A dedicated **procedural timeline** feature that models the case journey (events, filings, hearings, orders) and emits structured timeline entries for downstream UI/workflows. |
| **Applicable laws** | Suggestions for **relevant legal frameworks** tied to the matter (for research direction, not as final legal advice). |
| **Intelligence chat** | A **chat** where lawyers ask follow‑ups (explain a risk, compare options, procedural questions, hypotheticals). Replies aim to be **balanced and accurate** and evaluate both **civil-law** and **criminal-law** implications when both are factually live; workspace role mainly affects **panel** drafts, not an automatic advocacy slant in chat. |
| **Drafting assistant** | Prepares case-specific **pleadings, applications, legal notices, and replies** — from a **Blank Draft**, an **AI-suggested draft**, a reused **uploaded template** (whose structure is preserved verbatim, only case facts substituted), or a **Custom AI Draft** built straight from case context. A multi-turn chat first **confirms every fill-in value with the lawyer** (it never silently auto-fills), then renders the full draft. Applicable templates are always offered as suggestions. |
| **Edit panels from chat** | From the Strategy & Objective chat a lawyer can **correct a name / word / line** (applied across all panels' existing content with nothing regenerated) or **change the objective / case perspective** (e.g. recovery → divorce), which **re-derives** the strategic objective, analysis report, applicable laws, and drafting suggestions for the new objective. |
| **Responsive during generation** | Long jobs (strategy, analysis, dates, laws) run in the background when possible so **chat can still answer** while other outputs are being prepared. |

Nothing here replaces a qualified lawyer or predicts court outcomes. Outputs support **professional judgment**, not **decisions**.

---

## Who should read this?

- **Lawyers, product, and programme managers** — read *In plain language* and *Trust and limitations* below.
- **Engineers and DevOps** — read *Technical overview*, *Configuration*, and *Running the service*. Deeper behaviour is described in [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md).

---

## Trust and limitations

- The AI **must not invent** facts, amounts, dates, or citations that are not grounded in supplied materials or clearly labelled general legal background.
- **Workspace role** (petitioner, respondent, etc.) mainly shapes how **panel outputs** (e.g. strategy blocks) are **generated**. **Intelligence chat** is configured for **neutral, accuracy‑first** analysis unless the user explicitly asks for a one‑sided view.
- For case-specific legal reasoning, the system applies a **civil + criminal dual-track check** when facts support both branches; if facts support only one branch, it remains in that branch.
- All panel and chat output is produced in **English** (non-English names are transliterated), in normal Title Case, and never references the SimplerToday platform itself as a case party.
- When the **objective changes**, downstream panels (analysis report, applicable laws, quality score, drafting) are re-derived for the **new** objective so they stay consistent with it.
- For drafting, the lawyer **confirms every fill-in value** before a draft is rendered — the assistant does not silently auto-fill facts, amounts, or party details from case context. When a template is reused, its structure and language are preserved and only case-specific facts are substituted.
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

- `document_feature_response` — payloads such as `document_assessments`, `strategy_objective`, `analysis_report`, `list_of_dates`, `procedural_timeline`, `applicable_laws`, `workspace_metadata`, etc.
- `ai_response` — natural language chat reply.

Exact payload shapes are defined in code (`app/socket/schemas.py`) and integration notes under `docs/` where present.

---

## Repository layout

This section matches the idea of [`docs/STRUCTURE.txt`](docs/STRUCTURE.txt) (printable ASCII tree) but is **updated** for the current legal-AI codebase. Use either this README or `docs/STRUCTURE.txt` to find your way around; for deep behaviour, see [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md).

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
├── docs/STRUCTURE.txt            # Detailed folder tree (companion doc)
├── README.md                     # This file
└── docs/SYSTEM_ARCHITECTURE.md   # Event flow & multi-agent design
```

### Inside `app/` — what each folder is for

| Path | In plain terms |
|------|----------------|
| `main.py` | Starts the program and handles graceful shutdown. |
| `socket/` | Socket.IO **client**: connect to your backend, handle events, emit AI results. |
| `langgraph/` | **Strategy generation**, **intelligence chat**, **procedural timeline**, and the **drafting assistant** workflows (step-by-step AI). |
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
| **Google Gemini** | Primary LLM (`GOOGLE_API_KEY`); model set via `GEMINI_MODEL` (e.g. `gemini-3-flash-preview`). All generation runs on the `google-genai` SDK with explicit **thinking-budget control** (gemini-3 reasoning tokens are billed against the output budget, so a bounded thinking level keeps responses fast and complete); it falls back to `google-generativeai` if the new SDK is unavailable. See `env.example`. |
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
   - **AI:** `GOOGLE_API_KEY` for Gemini; `GEMINI_MODEL` to pick the model (default `gemini-3-flash-preview`); optional GCP fields for OCR or other Google services.
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

- **Architecture and event flow:** [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md)
- **Additional docs:** see `docs/` and topic‑specific `*.md` files at the repo root where maintained.

---

## License

MIT