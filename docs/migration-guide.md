# Migration guide

What the SimplerToday AI service depends on, where its state lives, and how
to move it to new infrastructure, a new cloud account or a new repository
without losing data or breaking compliance. Values are never written here;
the variable **names** come from `env.example`.

> **Warning.** The data in this service is privileged legal material. Every
> step that copies Qdrant, Neo4j, GCS or `data/` copies client data. Agree
> where it goes, who can access it, and how the source is erased before you
> start. The workspace purge only erases what the *current* configuration
> points at (see section 5).

## 1. Runtime shape

- One long-running process: `python -m app.main` (Docker `CMD`).
- **Outbound only.** It opens a Socket.IO connection to the backend. It
  listens on no port, so there is no load balancer, no ingress and no
  HTTP health check. Liveness is the `ai_heartbeat_ping` round trip, and the
  process exits on a fatal error. Use the orchestrator's restart policy.
- Container: `Dockerfile` (Python 3.12-slim, multi-stage). It installs
  `requirements.lock.txt` (the exact tested versions), adds `ffmpeg`,
  `poppler-utils` and `antiword`, and runs as the unprivileged user `app`
  (uid 10001). It writes to `/app/logs` and `/app/data`. Mount both as
  volumes.
- Socket auth signs each handshake with a timestamp the backend accepts within
  **±300 s**. Keep the host clock NTP-synced, or every connect fails as an
  auth error (`docs/AI_SERVICE_AUTH_MIGRATION.md` §5).
- Per-workspace caches live in process memory and are written through to
  Qdrant. A restart hydrates them from Qdrant on first use.

## 2. External dependencies

| System | Required? | Config (names only) | Notes |
|---|---|---|---|
| Backend Socket.IO server | Yes | `SOCKET_URL`, `SOCKET_PATH`, `SOCKET_NAMESPACE`, `SOCKET_SECRET_KEY`, `SOCKET_AI_PAYLOAD`, `SOCKET_AUTH_SCHEME` | The secret is shared with the backend. Rotate it on both sides together. |
| Qdrant | Yes | `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION_NAME` | Holds all evidence and all persisted state (section 3) |
| Gemini (default model provider) | Yes, unless `LLM_*` routes elsewhere | `GOOGLE_API_KEY`, `GEMINI_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | See `docs/MODEL_PORTABILITY.md` |
| Other model providers | Optional | `LLM_DEFAULT`, `LLM_ROUTES`, `LLM_FALLBACKS`, `OPENAI_*`, `AZURE_*`, `BEDROCK_*`, `SAGEMAKER_ENDPOINTS`, `OPENAI_COMPAT_PROFILES` … | Changing the **embedding** provider, model or dimension means a new collection (section 4) |
| Gate0 (governance and spend) | Optional; mandatory where `GATE0_ENFORCE` is set | `GATE0_BASE_URL`, `GATE0_API_KEY`, `GATE0_ENFORCE`, `GATE0_*` | |
| GCP Vision (OCR) | Needed for scanned PDFs and images | `GOOGLE_APPLICATION_CREDENTIALS` | Service-account file, mounted read-only |
| GCP Speech-to-Text (Chirp 3) + GCS | Needed for audio evidence | `SPEECH_GOOGLE_APPLICATION_CREDENTIALS`, `GCS_BUCKET_NAME`, `CHIRP_REGION`, `AUDIO_*` | Without GCS, transcription works but speaker tags may be missing |
| GCS for DOCX export | For analysis DOCX export | `GCS_DOCX_BUCKET_NAME` | |
| Vertex AI Search | Optional, fallback for laws and case law | `GCP_PROJECT_ID`, `VERTEX_SEARCH_*` | |
| Indian Kanoon | Used directly (scraped) | none | Outbound HTTPS to `indiankanoon.org` must be allowed |
| Neo4j (knowledge graph) | Optional, off unless set | `NEO4J_URL`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_EDITION`, `ONTOLOGY_INGEST_ENABLED`, `RETRIEVAL_PANELS`, `RETRIEVAL_MODE` | If unreachable, the service logs it and runs on hybrid search |
| Redis | **Not used by application code today** | `REDIS_*` | Present in `config.py` and `docker-compose.yml` only. You can leave it out. |

Also: `SECRET_KEY`, the price overrides (`*_PRICE_*`, `LLM_PRICING_OVERRIDES`)
and the feature flags (`TABLE_QUERY_ENABLED`, `ENABLE_*`). Read `env.example`
for every variable with its comment.

## 3. Where state lives

| Store | Contents | Must migrate? |
|---|---|---|
| Qdrant `<QDRANT_COLLECTION_NAME>` (default `simplertoday`) | Per-workspace state caches: roles, cached panel payloads, manifests, participants, token-usage totals, purge bookkeeping | **Yes.** Without it every case loses its role, panels and bill. |
| Qdrant `<name>_evidence_v1[<embedding suffix>]` | Evidence chunks, dense + sparse vectors | **Yes**, or re-index (section 4) |
| Qdrant `<name>_tables_v1` | Spreadsheet rows for exact computation | **Yes** |
| Neo4j | Knowledge-graph facts, linked to evidence `chunk_id`s | If enabled. It can be rebuilt by re-ingesting. |
| Local disk `WORKSPACE_DATA_DIR` (default `./data/workspaces/<workspace_id>/`) | Per-workspace working files | **Yes.** Copy the volume. The purge deletes from here. |
| `data/ontology/`, `data/eval/` | Generated schema DDL, eval golden sets | From the repo or regenerable (`make ontology-ddl`) |
| `logs/`: `app.log` (30 days), `logs/audit/` (1 year), `structured.log` | Operational and **audit** logs | Keep the audit logs for their retention period. Archive them; do not drop them. |
| GCS buckets | Audio staging for transcription, DOCX exports | Per your bucket policy |
| `app/statutes/data/sections.json` | Statutory register (from the repo) | Comes with the code |

## 4. Migration procedures

### A. Moving the service to new infrastructure (same providers)

1. **Freeze.** Agree a window with the backend team. Stop the old process.
   It is a client, so stopping it only disconnects. The backend keeps
   re-sending purge events until `purgeAfter`, so nothing is lost while it
   is down.
2. **Copy Qdrant**: snapshot each of the three collections and restore them
   on the target (Qdrant snapshot API). Collection names must stay exactly
   the same, including the embedding suffix.
3. **Copy volumes**: `data/workspaces/` and `logs/audit/`.
4. **Configure**: create the environment from `env.example`. Get secrets from
   your secret manager, never from another person's `.env`. Mount the GCP
   service-account files read-only and point
   `GOOGLE_APPLICATION_CREDENTIALS` / `SPEECH_GOOGLE_APPLICATION_CREDENTIALS`
   at them.
5. **Build**: `docker build -t simplertoday-ai .`. The image uses
   `requirements.lock.txt`. Do not switch it to `requirements.txt`.
6. **Start one instance** and check the logs:
   - a successful handshake (no `replayed_nonce` or `stale_timestamp`);
   - heartbeats;
   - for one test workspace, `event … finished … status=ok` and
     `result <featureType> delivered=yes`.
7. **Run `make e2e`** from a checkout pointed at the new dependencies. A human
   runs it; it makes billed calls and uses a throwaway Qdrant collection.
8. **Decommission the source**: delete the old Qdrant collections, volumes
   and buckets. Copies left behind are outside the reach of any future
   workspace purge.

### B. Changing the embedding provider, model or dimension

This creates a **new** evidence collection name, because the suffix comes
from `embedding_space_suffix()` in `app/llm/config.py`. Re-index into it
with `scripts/reindex_chunks.py --from-collection <old>`, verify retrieval
with `scripts/eval_rag.py`, then **delete the old collection**. A purge does
not reach the old collection.

### C. Moving to a new cloud account or project

Recreate each GCP dependency in section 2: the service accounts with
least-privilege roles for Vision, Speech, GCS and Vertex AI Search, the
buckets, and the Vertex data store. Update the IDs in the environment and
re-run step A.6. Pricing defaults are env-overridable (`app/ai/pricing.py`)
and stay correct only if the new account has the same rates.

### D. Moving the repository

The `.claude/`, `.mcp.json`, `specs/`, `docs/` and `CLAUDE.md` files are all
committed and move with the repo. You also need to:
- re-create branch protection on `development`, `staging` and `main`;
- set up `pre-commit install` on every clone;
- re-link GitHub for Jira, so the SIM keys in branch names link to Jira;
- update the Confluence space and page ids in
  `.claude/skills/confluence-docs/SKILL.md` if the space moves.

## 5. Checks that are easy to get wrong

- **Erasure scope.** The purge deletes from the collections, disk paths and
  graph that the running configuration names. Any copy you made (old
  cluster, snapshot file, old embedding collection, backup volume) has to be
  deleted by hand.
- **Clock skew** breaks the handshake (±300 s).
- **One process per backend connection, unless confirmed otherwise.** The
  in-memory caches assume a workspace's events reach the process that holds
  its state. Confirm with the backend team how events are routed before you
  run more than one replica.
- **Lock file.** Production must install `requirements.lock.txt`. A fresh
  resolve of `requirements.txt` is untested.
- **Billing continuity.** Cumulative cost per workspace lives in the Qdrant
  state collection. If you skip copying it, every case's `totalPrice` resets
  and stops reconciling with Gate0 (`scripts/reconcile_gate0_spend.py`).
