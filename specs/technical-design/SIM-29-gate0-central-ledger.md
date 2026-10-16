# SIM-29: Gate0 as the single model, cost and log layer — technical design

- **Requirements:** `specs/requirements/SIM-29-gate0-central-ledger.md`
- **Status:** draft
- **Approver:** tech lead (SimplerToday); Gate0 owners for part G

## Summary
Model traffic already flows through Gate0 (`Gate0Provider`, enforced by
`GATE0_ENFORCE`). This change closes the gaps between what Gate0 sees and what
the service bills and logs.

Five service changes (parts A to E):
- bill embeddings at Gate0's own cost (header);
- stamp every `llm_call` line with Gate0's call id and cost;
- choose Gate0 request features per model family from `LLM_MODEL_OPTIONS`;
- send Cohere's query/document type;
- check at startup that Gate0 serves every configured model.

Part F reports non-model paid units (OCR, STT, search, unpriced grounding) to
Gate0's meter through a new, flag-gated outbox. Part G is the Gate0-side
contract change it depends on, owned by the Gate0 owners.

## Facts this design rests on
Verified against the live Gate0 dev gateway (LiteLLM 1.105.1) on 2026-10-16:
- Both `/v1/chat/completions` **and `/v1/embeddings`** return
  `x-litellm-response-cost` and `x-litellm-call-id`.
- Embeddings through Gate0 (`gemini-embedding-001`, 768 dims) are identical to
  direct Gemini, cosine 1.000000. No re-index is involved anywhere here.

From the Gate0 repo:
- `contracts/schemas/usage-record.v1.schema.json` is frozen at 1.0.0 with
  `additionalProperties: false` and has token fields only.
- `contracts/openapi/gate0-api.v1.yaml` has no usage-submission endpoint.
- `gateway/src/gate0_gateway/callbacks.py` drops caller metadata
  (`workspace_id`, `feature`, `request_id`) from the record it spools.

## Affected code
| File | Change |
|---|---|
| `app/llm/providers/gate0.py` | A: return the embedding cost and call id. B: read `x-litellm-call-id`. C: family-driven `capabilities` / `build_params`. D: Cohere `input_type`. E: **new** `served_models()` |
| `app/llm/providers/openai_compat.py` | A: `OpenAIChatProvider.embed` gains a hook to return headers (raw response) without changing other providers' behaviour |
| `app/llm/types.py` | B: `LLMResult` gains **new** `gateway_call_id: Optional[str]` |
| `app/llm/router.py` | B: `_log` adds `cost` and `gateway_call_id` to the `llm_call` line. A: `_embed_once` emits an `llm_call embed …` line and records the gateway embedding cost |
| `app/llm/config.py` | C: **new** `model_family(model)` reading `LLM_MODEL_OPTIONS[model].family`, falling back to the name for `gemini` |
| `app/ai/usage.py` | A: `record_embed_tokens(tokens, cost=None)`; `UsageAccumulator` gains **new** `embed_gateway_tokens`, `embed_gateway_cost`; `merge_usage` / `drain_for_emit` carry them. F: `_record` calls the meter hook |
| `app/ai/pricing.py` | A: `service_price_for(..., embed_gateway_tokens=0, embed_gateway_cost=0.0)` prices only locally-estimated tokens at the rate and adds the gateway cost |
| `app/ai/gate0_meter.py` | F: **new**. Builds usage records, queues them, flushes in batches, keeps a durable outbox |
| `app/ai/gemini_client.py` | E: `initialize()` runs the served-model check under `GATE0_ENFORCE` |
| `app/core/lifecycle.py` | F: start the meter flusher in `startup`, flush in `shutdown` |
| `app/core/config.py` | F: **new** `gate0_meter_enabled` (default `False`), `gate0_meter_flush_seconds` (default 10), `gate0_meter_outbox_dir` (default `data/gate0_meter_outbox`) |
| `env.example`, `docs/MODEL_PORTABILITY.md`, `CHANGELOG.md` | Document the above |
| `tests/test_llm_gate0.py`, `tests/test_usage_ledger.py`, **new** `tests/test_gate0_meter.py` | Tests (see Test plan) |

## Event and payload changes
None. `document_feature_response.metadata` and `ai_response.metadata` keep
their shape. `embedPrice` / `servicePrice` / `price` values change (they become
Gate0's exact embedding cost instead of an estimate). That is a value change,
not a shape change. It is called out in the PR and CHANGELOG for the backend
team.

## Design

### A. Embedding cost from Gate0
- `Gate0Provider.embed` uses `client.embeddings.with_raw_response.create(...)`
  and reads `x-litellm-response-cost` and `x-litellm-call-id`, like `_create`
  does for chat.
- It records `record_embed_tokens(tokens, cost=<header>)`.
- With a cost, the accumulator adds `tokens` to `embed_tokens` (all tokens, as
  reported today) **and** to `embed_gateway_tokens`, and adds the cost to
  `embed_gateway_cost`.
- `service_price_for` prices `embed_tokens - embed_gateway_tokens` at
  `EMBED_PRICE_PER_MILLION_TOKENS` and adds `embed_gateway_cost`. A missing or
  unparseable header falls back to today's estimate, so nothing is billed at 0.
- Other providers are unchanged (their `embed` passes no cost).

### B. One-to-one log join
- `Gate0Provider._result` sets `LLMResult.gateway_call_id` from
  `x-litellm-call-id`.
- `ModelRouter._log` appends `cost={} gateway_call_id={}` to the existing
  `llm_call` line (`-` when absent) and binds both as structured fields
  (`llm_cost`, `llm_gateway_call_id`) for `structured.log`.
- Embeddings get one `llm_call embed feature=embeddings …` line per batch from
  `_embed_once`, with the same fields. There is still one line per model call
  and no prompt or output text.
- `Gate0Provider._metadata` already sends `request_id`, `workspace_id` and
  `feature`. Joining is then by `gateway_call_id` (per call) or `request_id`
  (per event).

### C. Model features from configuration, never from model names
Any model Gate0 serves (Gemini, Claude, GPT, Azure deployments, or open-source
Llama, Qwen, DeepSeek or Mistral on Bedrock, vLLM or Ollama) is described in
`LLM_MODEL_OPTIONS[<model>]`:

| Key | Effect |
|---|---|
| `max_output_tokens` | router cap (unchanged) |
| `supports_documents`, `supports_images` | capabilities (unchanged mechanism) |
| `thinking` | **new**: `true` sends `reasoning_effort`, `false` suppresses it (even for an OpenAI reasoning-model name) |
| `grounding` | **new**: `true` sends `web_search_options` |
| `extra_body` | **new**: per-model backend parameters, merged under the adapter's own keys |
| `family` | **new**: only `gemini` and `anthropic` change behaviour |

- `model_family()` (`app/llm/config.py`) returns `family` when set. Otherwise
  it applies the legacy rule existing setups rely on: the `gemini` option, else
  a name containing "gemini". Everything else is `other`, a plain OpenAI request.
- No other name is inferred.
- Gemini behaviour is unchanged.
- `anthropic` drops `temperature` with thinking, applies an 8,192-token floor,
  and turns thinking off in JSON mode. LiteLLM forces a tool call for JSON on
  Claude, and that cannot be combined with thinking.

### D. Cohere input type through Gate0
`Gate0Provider._embed_extra_body` adds
`input_type: "search_query" | "search_document"` when the model name contains
`cohere`, or `LLM_MODEL_OPTIONS[<model>].cohere_input_type` is true (for a
Gate0 group whose name hides the backend). This mirrors
`app/llm/providers/aws.py`.

### E. Startup served-model check
- Under `GATE0_ENFORCE=true`, `gemini_client.initialize()` calls
  `Gate0Provider.served_models()` once (`GET /v1/models`, 10 s timeout).
- It compares the result with the profiles the router uses: `routing`,
  `selectable_models()` and `embedding_profile()`.
- Each missing model is logged at ERROR (`gate0 does not serve model {}`).
  Startup is not refused: a gateway outage at boot must not take the service
  down, and per-call failures already surface.
- A non-Gemini selectable model without `max_output_tokens` is logged at
  WARNING.

### F. Non-model usage to Gate0's meter (decision (b))
- **Hook:** `app/ai/usage.py::_record` already sees every paid unit, with its
  workspace, at the lowest call site. After applying the unit, when
  `gate0_meter_enabled` it calls `gate0_meter.report(kind, quantity)` for kinds
  `ocr`, `stt`, `search`, `grounding`.
  - Embeddings are not reported: Gate0 already meters them.
  - Model tokens are never reported here.
- **Record:** built from the v1.1 shape (part G):
  - `call_id`: a new UUID4;
  - `status: success`;
  - `requested_model`, `routed_provider`, `routed_model`: a fixed service label
    (`google-vision`, `google-chirp3`, `vertex-ai-search`,
    `google-search-grounding`) / `google` / the same label;
  - token fields `null`;
  - `cost_usd` from `service_price_for` for that unit;
  - **new** `usage_kind`, `units {kind, quantity}`, and
    `attribution {app, workspace_id, feature, request_id}`, taken from the
    logging context the way `Gate0Provider._metadata` does.
  - Ids only: never names, text or URLs.
  - `team`, `key_alias` and `record_id` are **not** sent. Gate0 resolves them
    from the submitting key, per its server-resolved-identity rule.
- **Delivery:**
  - `report()` only appends to an in-memory queue. It never blocks and never
    raises.
  - A background task started in `lifecycle.startup` flushes every
    `gate0_meter_flush_seconds`, or at 100 records, with
    `POST {gate0_base_url}/usage-records` and the service's Gate0 key.
  - Transient failures retry with backoff (3 attempts).
  - Anything still unsent is appended to an NDJSON outbox in
    `gate0_meter_outbox_dir`, replayed on the next start, and flushed on
    shutdown.
  - Gate0 dedupes by `record_id = UUIDv5(call_id + status)`, so replays are
    safe.
- **Service cost is unchanged.** These units are still billed in the service
  ledger as today. F only makes them visible in Gate0.
- **Off by default** until Gate0 ships part G. With the flag off, no record is
  built.

### G. Gate0-side dependency (Gate0 repo, separate change, owned by Gate0 owners)
- `usage-record.v1.schema.json` → **1.1.0 (MINOR)**, adding optional:
  - `attribution` (object: `app`, `workspace_id`, `feature`, `request_id`, all
    nullable strings);
  - `usage_kind` (enum: `model`, `embedding`, `ocr`, `stt`, `search`,
    `grounding`);
  - `units` (object: `kind`, `quantity`).
- `gate0-api.v1.yaml` → **MINOR**, new `POST /v1/usage-records`:
  - authenticated by virtual key;
  - validates against the schema;
  - fills `team` / `key_alias` from the key and derives `record_id`;
  - appends to the spool;
  - idempotent; returns per-record accept/reject.
- `callbacks.py` fills `attribution` from LiteLLM request metadata and sets
  `usage_kind` to `model` / `embedding`.
- Ratification per `contracts/VERSIONING.md`.

## Checklist against CLAUDE.md
- Role gate: n/a. No new feature behaviour; billing, logging and transport only.
- Long-running work: the meter flusher is a process-level task started in
  `lifecycle.startup` (like the knowledge plane), not per workspace. It is not
  a `_track_background_task` (not run-scoped), so a workspace Stop must not
  cancel it.
- Loaders and `CancelledError`: n/a for loaders. The flusher catches
  `asyncio.CancelledError` first, flushes to the outbox, and re-raises.
- Emits via `self._socket_client` / `_emit_document_feature`: n/a, no new emit.
- Model calls through the router, `is_generation_error()` checked: unchanged.
  - The served-model probe in E is not a model call; it goes through
    `Gate0Provider` (the only place the SDK lives).
  - All calls work with `GATE0_ENFORCE=true`.
- Paid calls metered at the lowest call site: yes.
  - A uses the existing embed call site.
  - F hooks `_record`, the single point every `record_*` passes through, so
    nothing is reported twice.
- New caches reached by the workspace purge: the meter queue and outbox hold
  billing records (ids and quantities, no case content).
  - The purge does not delete them, matching Gate0's spend logs, which the
    purge also does not reach.
  - **Needs compliance sign-off** (listed under Risks).
- Ingestion/deletion path: n/a.
- Chat path writing state gated on `mutations_allowed`: n/a.
- Logging: new fields are a cost figure and an opaque gateway id. No payloads,
  text, names or URLs. `safe_url` is not needed, because the meter logs the
  endpoint path only.

## Risks and rollback
| Risk | Mitigation / switch-off |
|---|---|
| Embedding cost header missing on some deployment | Falls back to today's estimate per call; no zero billing |
| Family mapping turns on a parameter a model rejects | Per-model option; remove `thinking`/`family` from `LLM_MODEL_OPTIONS` to revert to plain requests. Smoke-test each family before adding to `LLM_SELECTABLE_MODELS` |
| Served-model probe slow or failing at boot | 10 s timeout, logged, never blocks startup |
| Meter endpoint down | Outbox on disk, replayed; `GATE0_METER_ENABLED=false` stops it entirely |
| Outbox growth while Gate0 is down for long | Size logged at each flush; WARNING above 10k records |
| Billing records outliving a workspace purge | Ids only, no content; compliance to confirm retention (open question) |
| Part G not ratified | A to E ship independently; F stays off |

## Test plan
- `tests/test_llm_gate0.py` (extend; fake OpenAI client, no network):
  - embed reads cost and call id;
  - `llm_call` fields;
  - family table rows (Gemini unchanged: existing tests must still pass);
  - Cohere `input_type`;
  - `served_models()` diff logging.
- `tests/test_usage_ledger.py` (extend):
  - gateway embed cost replaces the estimate;
  - mixed gateway and estimated tokens;
  - `merge_usage` / `drain_for_emit` carry the new fields.
- `tests/test_gate0_meter.py` (**new**):
  - record shape (no names/text/URLs, attribution from the logging context);
  - nothing built when disabled;
  - batching, retry, outbox write, replay and dedupe;
  - `CancelledError` handling;
  - no report for embeddings or model tokens.
- `tests/offline/verify_logging.py` must still pass (new log fields).
- Live, manual: `scripts/smoke_llm_providers.py --embed` with one profile per
  family through Gate0; `scripts/reconcile_gate0_spend.py --by-feature` on a
  test case.
- `make e2e` required before deploy: billing values in every emit change.
  A human runs it (billed).

## Implementation tasks
1. `LLMResult.gateway_call_id`; read it in `Gate0Provider._result`; extend `ModelRouter._log`. Tests.
2. Gateway embedding cost: `UsageAccumulator` fields, `record_embed_tokens(cost=)`, `service_price_for`, `Gate0Provider.embed` raw response; `llm_call embed` line. Tests.
3. `model_family()` and the family-driven `capabilities` / `build_params`. Gemini regression tests unchanged plus new rows.
4. Cohere `input_type` in `_embed_extra_body`. Test.
5. `served_models()` and the startup check in `gemini_client.initialize()`. Test.
6. `app/ai/gate0_meter.py`, config flags, `_record` hook, lifecycle start/stop. Tests.
7. Docs: `env.example`, `docs/MODEL_PORTABILITY.md`, `CHANGELOG.md`.
8. Gate0 repo (Gate0 owners): schema 1.1.0, `POST /v1/usage-records`, callback attribution, contract tests.

## Decisions
- **C is configuration-driven (product decision during implementation).** The
  draft keyed features on vendor families inferred from model names. That was
  replaced so open-source and any future model are configured, not coded.
- **E runs in `lifecycle.startup`**, not `gemini_client.initialize()`. The
  check is an async HTTP call and `initialize()` is synchronous.
  - Method: `ModelRouter.gate0_unserved_models()`, wrapped by
    `ApplicationLifecycle._check_gate0_models()`.
- **Attribution key renamed `workspace_id` → `case_id` (bug found in live
  testing).**
  - LiteLLM 1.105.1 rejects with 401 any request whose body or `metadata`
    carries `workspace_id`; it is on its client-credential ban list.
  - So every case-scoped call through Gate0 failed. This predates SIM-29.
  - The `workspace_id:<id>` tag is kept, because reconciliation reads it.
  - A test asserts no request carries a banned key.
- **Gateway costs are kept unrounded** until the emit. Per-call 6-dp rounding
  billed a $0.00000975 call as $0.00001 (seen live against the Gate0 spool).
- **The `llm_call embed` line replaces the DEBUG `embed_call` line.**
- **Meter service label for STT is `google-speech-v2`.**
- **Gate0 spend-log lookups need Gate0's DB mode.** The local dev Gate0 runs
  without it (`/spend/logs` returns 500), so the live join was verified against
  Gate0's usage spool (`call_id`).
