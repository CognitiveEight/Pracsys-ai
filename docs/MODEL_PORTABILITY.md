# Model portability

Every model call in the service can be served by Gemini, OpenAI, Azure OpenAI,
Azure AI Foundry, AWS Bedrock, an AWS SageMaker endpoint, or any
OpenAI-compatible server. That last group covers open-weight models such as
DeepSeek, Qwen, Llama and Mistral, whether self-hosted on vLLM, Ollama or TGI,
or used through a hosted API. Configuration chooses the provider for each task,
and no feature code changes.

With no `LLM_*` settings, every call goes to `gemini:$GEMINI_MODEL` and behaves
exactly as before.

## How a call is routed

```
feature code ──> gemini_client.generate(...)          app/ai/gemini_client.py  (facade, unchanged API)
                    └─> ModelRouter.complete(...)     app/llm/router.py
                          ├─ chain = LLM_ROUTES[task] or LLM_DEFAULT, then LLM_FALLBACKS
                          ├─ skip profiles lacking a needed capability (images, audio, grounding)
                          ├─ clamp max_tokens to the model ceiling (LLM_MODEL_OPTIONS)
                          ├─ timeout, 1 retry on 429/5xx/network, then next profile
                          ├─ one `llm_call` log line + per-model cost
                          └─> adapter.generate(...)   app/llm/providers/*.py
```

- **Feature code never imports a provider SDK.** `tests/test_llm_router.py`
  enforces this.
- **The facade returns the same error sentinels as before.** Check them with
  `is_generation_error()`.
- **JSON mode** (`response_mime_type="application/json"`) is coerced and
  repaired in the same way for every provider. `<think>` blocks from reasoning
  models such as DeepSeek-R1, QwQ and Qwen3 are stripped first.
- **A content block is final.** A Gemini SAFETY finish, an Azure
  `content_filter` or a Bedrock guardrail returns `""` and is **never** retried
  on another provider. Routing around a block would defeat the policy that made
  it.

## Profiles

| Profile | Reaches | Credentials |
|---|---|---|
| `gemini:<model>` | Google AI Studio | `GOOGLE_API_KEY` |
| `openai:<model>` | api.openai.com, or a gateway via `OPENAI_BASE_URL` | `OPENAI_API_KEY` |
| `azure_openai:<model>` | Azure OpenAI. `<model>` is looked up in `AZURE_OPENAI_DEPLOYMENTS`; if absent it is used as the deployment name | `AZURE_OPENAI_API_KEY`, or Entra ID when unset |
| `azure_foundry:<model>` | Azure AI Foundry's OpenAI-compatible endpoint (`…/openai/v1`) | `AZURE_FOUNDRY_API_KEY`, or Entra ID when unset |
| `bedrock:<model-id>` | Bedrock Converse API. Accepts model ids or inference-profile ids (`us.…`, `global.…`) | AWS credential chain only |
| `sagemaker:<name>` | the endpoint named in `SAGEMAKER_ENDPOINTS` | AWS credential chain only |
| `openai_compat:<account>:<model>` | the server for `<account>` in `OPENAI_COMPAT_PROFILES` | env var named by `api_key_env` |
| `gate0:<model>` | the gate0 gateway (LiteLLM proxy): every provider it is configured for | `GATE0_API_KEY` (a gate0 virtual key) |

Model ids may contain colons (`bedrock:…-v1:0`,
`openai_compat:ollama:qwen3:32b`). Only the leading segments are split off.

**Least privilege:**
- For Azure, prefer a managed identity: leave the key unset and grant
  *Cognitive Services OpenAI User*.
- For AWS, grant the service role `bedrock:InvokeModel` on the specific model
  ARNs and `sagemaker:InvokeEndpoint` on the specific endpoints.
- Keys are `SecretStr` settings and are never logged.

## Routing configuration

```bash
LLM_DEFAULT=gemini:gemini-3.8-flash
LLM_ROUTES={"explain_why_node": ["azure_openai:gpt-4.1", "gemini:gemini-3.8-flash"],
            "audio_transliteration": ["openai_compat:qwen:qwen-plus"]}
LLM_FALLBACKS=["gemini:gemini-3.8-flash"]
LLM_GROUNDING_PROFILES=["gemini:gemini-3.8-flash"]
```

**Task keys.** A task key is the `feature=` value printed on every `llm_call`
log line:
- the `@meter_usage` feature, for example `list_of_dates`,
  `applicable_laws`, `procedural_timeline`, `relevant_parties`, `case_laws`
  or `analysis_report_for_sync`;
- otherwise, the calling function, for example `strategies_from_documents`.

To route a feature, read its key off the log:
`grep "llm_call" logs/app.log | grep -o "feature=[^ ]*" | sort | uniq -c`.

For intelligence chat, the answer is `explain_why_node` and the intent
classification is `intent_classifier_node`.

Some tasks are named explicitly:

| Task key | What it is |
|---|---|
| `audio_diarization` | Transcript speaker diarization and roles |
| `audio_transliteration` | Hinglish transliteration |
| `text_filter` | Forbidden-word filter in `app/langgraph/output_filters.py` |
| `law_strip` | Law-citation strip in `app/langgraph/output_filters.py` |

**Startup validation.** `lifecycle.startup` → `gemini_client.initialize()`
checks every configured profile.
- An explicitly configured profile with missing credentials or a missing SDK
  **stops startup** with the profile and reason. Secret values are never
  printed.
- With no `LLM_*` settings, a missing `GOOGLE_API_KEY` only warns, as it always
  has.

**Per-model limits** (`LLM_MODEL_OPTIONS`). Output ceilings differ widely, for
example 8K on many open-weight models and 64K on Gemini. A request above the
ceiling is a hard 400, so set it per model:

```bash
LLM_MODEL_OPTIONS={"deepseek-chat": {"max_output_tokens": 8192},
                   "meta.llama3-3-70b-instruct-v1:0": {"max_output_tokens": 8192, "supports_images": false}}
```

Feature code asks for up to 12K (Gemini adds a 16K floor because its thinking
tokens share the budget), and the router clamps the request to the ceiling.

## Open-weight models (DeepSeek, Qwen, Llama, …)

There are three ways to reach open-weight models:
1. **OpenAI-compatible endpoint**, through `OPENAI_COMPAT_PROFILES`. Either:
   - self-hosted vLLM (`http://vllm:8000/v1`), Ollama (`http://ollama:11434/v1`)
     or TGI; or
   - a hosted API such as DeepSeek, Alibaba DashScope (Qwen), Together, Groq or
     Fireworks.

   ```bash
   OPENAI_COMPAT_PROFILES={
     "deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
                  "max_output_tokens": 8192},
     "qwen":     {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                  "api_key_env": "DASHSCOPE_API_KEY"},
     "local":    {"base_url": "http://vllm:8000/v1", "json_mode": true, "supports_images": false}}
   LLM_ROUTES={"list_of_dates": ["openai_compat:deepseek:deepseek-chat", "gemini:gemini-3.8-flash"]}
   ```

   Check base URLs and model names against each provider's current docs. The
   values above are examples.

   Account options:
   - `base_url`
   - `api_key_env`
   - `json_mode` (default true; sends `response_format: json_object`)
   - `supports_images`
   - `max_output_tokens`
   - `headers`
   - `extra_body` (e.g. vLLM `guided_json`, or Qwen `enable_thinking: false`)
   - `query_prefix` (embeddings)
2. **Bedrock**, e.g. `bedrock:<deepseek/qwen/llama model id>`, using the ids
   enabled in your Bedrock account and region.
3. **Azure AI Foundry**, via `azure_foundry:<deployment>`.

## User-selected model per case (`aiModel`)

The lawyer chooses the model in the frontend, and the backend sends it as
`aiModel`:

| Event | Effect |
|---|---|
| `workspace_created` / `workspace_sync` / `workspace_version_restore` | Sets the **case's** model. Every model call for the case runs on it, including background panels and work resumed after a restart. Absent = no change; `null` / `""` = back to the default. |
| `user_message_request` | Overrides the model for **that chat turn only**. |

```bash
LLM_DEFAULT=gate0:gemini-3.8-flash                     # default + fallback
LLM_SELECTABLE_MODELS={"gemini-3.8-flash":"gate0:gemini-3.8-flash",
                       "gemini-2.5-pro":"gate0:gemini-2.5-pro",
                       "gpt-4o":"gate0:gpt-4o"}         # id the frontend sends -> profile
```

**Rules** (`app/workspace/model_selection.py`, `ModelRouter.chain_for`):
- **Only ids in `LLM_SELECTABLE_MODELS` are honoured.** Anything else is
  logged and ignored, and the case keeps its model. The frontend dropdown
  should list exactly these ids.
- **Fallback.** The chosen model heads the chain, with the default behind it,
  so a failing provider falls back to Gemini.
- **Pinned tasks.** Tasks listed in `LLM_ROUTES` keep their own route.
- **Never affected by the choice:**
  - Google-Search-grounded case-law search, which stays on
    `LLM_GROUNDING_PROFILES` (Gemini);
  - embeddings. A case's documents are embedded once, and a chat model change
    must never change its vector space. Any LLM can read text found by any
    embedding model.
- **Labels.** `metadata.aiModel` on every emit is the model that actually
  answered.
- **Storage.** The choice persists per case (`CacheKinds.MODEL_SELECTION`). It
  is rehydrated on a cold workspace and cleared by the workspace purge.

## Gate0 (governed gateway)

Gate0 is ViitorCloud's AI gateway, built on the LiteLLM proxy. It holds the
provider keys, budgets, the approved-model list and the spend records for all
apps.

SimplerToday talks to it with `gate0:<model>` profiles
(`app/llm/providers/gate0.py`). `<model>` is any name gate0 serves: a model
group such as `gemini-3.8-flash` or `gpt-4.1`, or a task class such as
`gate0/lite`.

```bash
GATE0_BASE_URL=https://<gate0-host>/v1
GATE0_API_KEY=<gate0 virtual key for SimplerToday>
GATE0_ENFORCE=true                     # gate0 is the ONLY path
LLM_DEFAULT=gate0:gemini-3.8-flash
LLM_ROUTES={"explain_why_node": ["gate0:gpt-4.1", "gate0:gemini-3.8-flash"]}
LLM_FALLBACKS=["gate0:gpt-4.1"]
EMBEDDING_PROVIDER=gate0               # embeddings through gate0 too
EMBEDDING_MODEL=gemini-embedding-001
```

**What the adapter adds on top of the OpenAI mapping:**
- **Governance (`GATE0_ENFORCE=true`).**
  - Startup refuses any profile that is not `gate0:`, across routes,
    fallbacks, grounding and embeddings.
  - The router also refuses them at runtime, so no call can reach a provider
    directly.
  - If grounding has no Gemini-backed gate0 profile, it is turned off
    (`generate_grounded` returns `("", [])`) rather than sent around the
    gateway.
- **Attribution.** Every request carries LiteLLM `metadata` and `tags`:
  - fields: `app`, `feature` (the route key), `kind`, `workspace_id`,
    `request_id`, `event`;
  - tags: `app:…`, `feature:…`, `workspace_id:…`.

  These are ids only, never names or prompt text. Turn them off with
  `GATE0_TAG_REQUESTS=false`.
- **One bill.** The cost gate0 computed (the `x-litellm-response-cost` header)
  is what SimplerToday bills, instead of its local price table. Both systems
  report the same figure.
- **Gemini features through the gateway.** These apply to models whose name
  contains `gemini`, or to a task class marked
  `LLM_MODEL_OPTIONS={"gate0/lite": {"gemini": true}}`:
  - `reasoning_effort` (LiteLLM turns it into the thinking level);
  - `safety_settings` BLOCK_NONE (`GATE0_GEMINI_BLOCK_NONE`);
  - Google Search grounding through `web_search_options`, with sources read
    from the `url_citation` annotations;
  - embedding `task_type`;
  - the same 16K output floor as direct Gemini.
- **Budgets.** A 429 that says "budget" is final: no retry, no fallback, and
  the caller reports the failure. A plain rate-limit 429 is retried once.

**Embeddings through gate0 need no re-index.** Use the named group
`gemini-embedding-001` at 768 dimensions (`EMBEDDING_PROVIDER=gate0`). The
gateway calls the same model with the same task type and size: the vectors
were verified identical to a direct call (cosine 1.0), so the evidence
collection keeps its name.

**`drop_params`.** LiteLLM's global `drop_params: true` cannot be overridden
per model, and gate0 keeps it on for its other callers. That is safe for
SimplerToday: it sends Gemini-only parameters (thinking, safety, search) only
to Gemini models, and every approved model supports `response_format`.

**Done in gate0's config:**
- **Named model groups** (`gemini-3.8-flash`, `gemini-2.5-flash`,
  `gemini-2.5-pro`, `gpt-4o`, `gpt-4o-mini`, `gemini-embedding-001`), each
  with `timeout: 300` and no cross-model fallback.
- **Approval.** Each group is approved under `named_models` in
  `approved-models.stack0.yaml`. `check_approved_models.py` now fails on an
  unapproved named group, so the governance check covers them.
- **Templates** for Azure, Bedrock, DeepSeek and Qwen are left commented out.

**Gate0-side configuration SimplerToday needs** (in gate0's `litellm` config):
1. **Model groups named by model**, so SimplerToday chooses the model per
   feature. Task classes alone cannot express that. Gate0's ADR-0003 currently
   rejects raw names, so this needs a change there.
2. **Timeout of at least 300 s** for the deployments SimplerToday uses.
   Gate0's default is 60 s, and the strategy and objective regeneration
   legitimately takes about 2 minutes. Keep it above
   `GEMINI_REQUEST_TIMEOUT_SECONDS`.
3. **`drop_params: false` for this key or these groups.** A silently dropped
   `response_format` or `safety_settings` breaks JSON panels or blocks legal
   evidence.
4. **No fallbacks across classes or models for this key.** SimplerToday runs
   its own chain and must know which model answered. A content block must
   never be rerouted.
5. **Every provider you route to configured in its `model_list`:** Azure
   OpenAI, Foundry, Bedrock, SageMaker, DeepSeek/Qwen (via vLLM, Ollama or
   their APIs).
6. **Keep the request metadata** if gate0's own usage records are to show
   workspace and feature. LiteLLM's spend logs keep the `tags`.
7. **High availability before `GATE0_ENFORCE`.** With enforcement on, a gate0
   outage stops SimplerToday's AI features: there is no direct fallback, by
   design.

**Not covered by gate0:** Vision OCR, Speech-to-Text, Vertex AI Search and
Indian Kanoon are separate Google services and HTTP calls, not model calls.
They keep their own credentials and cost tracking.

## Capability matrix

| | JSON mode | Schema | Images | PDFs | Audio | Grounding | Reasoning knob |
|---|---|---|---|---|---|---|---|
| gemini | native | native | yes | yes | yes | Google Search | `thinking_level` |
| openai / azure_openai | `json_object` | `json_schema` | yes | yes | audio models | – | `reasoning_effort` (o-series, gpt-5) |
| azure_foundry | `json_object` | `json_schema` | per model | – | – | – | – |
| openai_compat | `json_object`, or prompt-only (`json_mode:false`) | – | `supports_images` | – | – | – | – |
| bedrock | prompt + repair | forced tool (Claude, Nova, Mistral Large) | Claude, Nova, Llama 3.2/4 vision | same as images | – | – | – |
| sagemaker | prompt, or `json_object` (`json_mode:true`) | – | – | – | – | – | – |

**Gaps:**
- **Grounding** (Google Search) is Gemini-only. When no profile in
  `LLM_GROUNDING_PROFILES` can serve a call, `generate_grounded` returns
  `("", [])`. The case-law finder then uses Indian Kanoon and Vertex AI Search,
  which it already falls back to.
- **Multimodal inputs.** The router skips profiles without the needed
  capability. Today no feature sends image, PDF or audio bytes to a model: OCR
  and speech-to-text are separate Google Cloud services, and they are not part
  of this layer.

## Embeddings

Embeddings are configured with these settings:
- `EMBEDDING_PROVIDER`: `gemini` | `openai` | `azure_openai` | `azure_foundry`
  | `bedrock` | `openai_compat:<account>`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIMENSIONS`

Supported models:
- Gemini, with task types;
- OpenAI / Azure `text-embedding-3-*` (sends `dimensions`);
- Bedrock Titan v2 (`dimensions`) and Cohere (`input_type`; v4 `output_dimension`);
- OpenAI-compatible servers (e.g. Qwen3-Embedding or BGE on vLLM or TEI).
  Models without task types get `EMBEDDING_QUERY_PREFIX` (or the account's
  `query_prefix`) on queries.

**One collection per embedding space.**
- The evidence collection name gets a suffix built from provider, model and
  dimensions, e.g. `simplertoday_evidence_v1__openai-text-embedding-3-large-1024`.
- The legacy Gemini space (`gemini-embedding-001`, 768) keeps
  `simplertoday_evidence_v1`, so deploying this change re-indexes nothing.
- If an existing collection's vector size differs from `EMBEDDING_DIMENSIONS`,
  the service **refuses to start**.

**Changing the embedding model:**
1. With the new settings, run
   `python scripts/reindex_chunks.py --from-collection simplertoday_evidence_v1`.
   This is billed. It copies every chunk (id, text, metadata, exact-term
   vector) into the new collection and re-embeds only the dense vector.
2. Deploy the new settings.
3. **Delete the old collection.** A workspace purge only reaches the configured
   collections, so a stale copy would keep deleted case data. That is a
   compliance problem, not just clutter.

## Cost

**Per case (workspace).**
- Every paid unit is charged to its workspace the moment it is recorded:
  model tokens, OCR pages, Speech-to-Text seconds, search queries, grounding
  and embeddings. This is the workspace ledger in `app/ai/usage.py`.
- The workspace comes from the running event's logging context. That is the
  same `workspace_id` gate0 tags the request with, so SimplerToday's per-case
  model cost equals LiteLLM's spend for that tag.
- Each emit reports and adds to the case total whatever the ledger holds
  since the previous emit (`drain_for_emit`), so every cost is reported
  exactly once. That includes background work with no emit of its own, such as
  knowledge-graph ingestion and re-evaluations, which before this change never
  reached the case.
- A cost recorded with no workspace in context stays with its scope and is
  reported by that scope's own emit.

**Checking against gate0.**
`GATE0_ADMIN_KEY=<key that can read spend logs> python scripts/reconcile_gate0_spend.py --by-feature`
lists, per workspace, gate0's model spend next to SimplerToday's model-cost
total and flags any difference. It is read-only.
- Only model cost is compared. OCR, Speech-to-Text, Vertex Search and non-gate0
  embeddings never pass through gate0, so SimplerToday's `totalPrice` is that
  model cost plus `totalServicePrice`.
- Workspaces whose totals started before gate0 also include direct-Gemini
  spend that gate0 never saw.

**LiteLLM's Usage page is the global view**: all apps, keys and test calls. For
one case, filter its spend logs on the `workspace_id:<id>` tag, which is what
the reconciliation script does.

**Pricing.**
- Each call is priced by the model that served it, at record time
  (`UsageAccumulator.add(..., model)`). A turn that mixes providers is therefore
  billed correctly.
- Calls through gate0 are billed at gate0's own figure (the
  `x-litellm-response-cost` header) instead of the local table.
- `metadata.aiModel` reports the model that did most of the work.
- Non-Gemini rates are **not** hard-coded. They change, and must come from the
  provider's price page, or from your serving cost for self-hosted models. Set
  them in `LLM_PRICING_OVERRIDES={"model": [input_usd_per_1M, output_usd_per_1M]}`
  (exact name or prefix). An unpriced model uses the default rate and warns once.
- `TOKEN_PRICE_INPUT/OUTPUT_PER_MILLION`, when both are set, still override
  every model.
- Embedding cost uses `EMBED_PRICE_PER_MILLION_TOKENS`. Set it when the
  embedding provider changes.

## Safety and quality

**Content filters.** Legal evidence contains violence, sexual offences and
threats. Gemini runs with BLOCK_NONE, as before. Azure content filters and
Bedrock guardrails are configured server-side: set them to a level that lets
case material through, or expect `blocked` results (logged as
`status=blocked`).

**Quality is not portable by default.** The prompts were tuned on Gemini
against Indian-law material (BNS / BNSS / BSA). Before routing a panel to
another model, run:
1. `python scripts/smoke_llm_providers.py --profile <p> --embed` (billed). This
   checks the connection, valid JSON, tokens and price.
2. `python scripts/eval_rag.py` on the golden set with the route enabled. The
   route must match or beat the baseline.
3. `make e2e` with the route enabled.

## Tests

- `tests/test_llm_router.py` covers:
  - routing and fallback order, with no fallback after a block;
  - transient retry, timeout and cancellation;
  - capability skips and output clamping;
  - grounding;
  - JSON repair, longform continuation and the thread bridge;
  - per-model pricing and embedding validation;
  - the no-SDK-outside-adapters boundary.
- `tests/test_llm_providers.py` checks request/response mapping for every
  adapter against fake SDK clients.
- `tests/test_llm_gate0.py` covers gate0: request mapping, attribution tags,
  the cost header, grounding sources, budget-block handling and
  `GATE0_ENFORCE`.

Both suites are offline.
