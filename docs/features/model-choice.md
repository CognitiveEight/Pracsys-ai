---
title: Model choice and gate0
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows: []
other_side: {}
related_specs:
  - specs/requirements/SIM-29-gate0-central-ledger.md
  - specs/technical-design/SIM-29-gate0-central-ledger.md
  - specs/acceptance-criteria/SIM-29-gate0-central-ledger.md
---

# Model choice and gate0

## In plain words

The AI service is not tied to one AI vendor. It can use Google's Gemini,
OpenAI, Microsoft Azure, Amazon, or open models such as DeepSeek or Llama.
Settings decide which model does which job. A lawyer can also pick the model
for a case, from a short list the firm has approved. Every answer and panel for
that case then uses it. A lawyer can also pick a model for a single chat
message. A few jobs never follow the lawyer's pick, because changing them
would break the case. The company's AI gateway can sit in front of all models.
It holds the vendor keys, enforces budgets and keeps one record of every call.
When the gateway is required, nothing can reach a vendor directly. If a model
refuses content for safety reasons, the service does not try another model to
get around it.

## Who uses it

- **Lawyers** choose a model per case, or per chat message, in the lawyer app.
- **Engineers and operations** set which models are allowed and which model
  serves each task.
- **The gate0 team** runs the gateway the service calls through.

## What the user sees

A model picker in the lawyer app. It lists the models the firm has approved.
The chosen model answers chat and generates every panel for the case. Each
reply and panel reports the model that did the work in `metadata.aiModel`.
No lawyer-app page for this feature was listed for linking.

## How it works

1. **Every model call goes through one router.** Feature code calls the
   `gemini_client` facade. The facade hands the call to `ModelRouter`. The
   router picks a profile, such as `gemini:<model>`, `openai:<model>`,
   `azure_openai:<model>`, `azure_foundry:<model>`, `bedrock:<model-id>`,
   `sagemaker:<name>`, `openai_compat:<account>:<model>` or `gate0:<model>`.
2. **Each task has a chain.** The task key is the feature name on the
   `llm_call` log line. The chain is the task's entry in `LLM_ROUTES`, or
   `LLM_DEFAULT`, followed by `LLM_FALLBACKS`. With no `LLM_*` settings, every
   call goes to Gemini, as before.
3. **The user's choice heads the chain.** The backend sends `aiModel`:
   - on `workspace_created`, `workspace_sync` or `workspace_version_restore`,
     it sets the case's model. If the field is absent, nothing changes. An
     empty or null value resets the case to the default;
   - on `user_message_request`, it overrides the model for that turn only.
     Background work started by the turn inherits it.

   The chosen profile goes first. The configured chain stays behind it as the
   fallback.
4. **The router tries each profile in order.** It skips:
   - a profile that is unavailable;
   - a profile that `GATE0_ENFORCE` forbids;
   - a profile that lacks a needed capability (images, documents, audio,
     grounding).

   It clamps the output budget to the model's ceiling from
   `LLM_MODEL_OPTIONS`. It applies the request timeout and retries a transient
   error once. It then moves to the next profile.
5. **One log line per call.** The router writes `llm_call` with feature,
   provider, model, status, duration, tokens, `cost=` and `gateway_call_id=`.
   It records the usage priced by the model that served the call.
6. **Through gate0**, the request carries attribution ids. The response carries
   gate0's cost and call id. See
   [Model calls through gate0](../contracts/model-calls-through-gate0.md).

## Rules and limits

- **Only approved ids are honoured.** An `aiModel` value not in
  `LLM_SELECTABLE_MODELS` is logged and ignored, and the case keeps its model.
  The `LLM_DEFAULT` head is always selectable.
- **What never follows the user's choice:**
  - tasks pinned in `LLM_ROUTES`;
  - Google-Search-grounded calls (case-law search), which use
    `LLM_GROUNDING_PROFILES`;
  - embeddings, which use `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL`. A case's
    documents are embedded once, and a model change must not change its vector
    space.
- **The choice is stored per case.** It is saved under the `MODEL_SELECTION`
  cache, restored on a cold workspace, and cleared by the workspace purge.
- **`GATE0_ENFORCE=true` makes gate0 the only path.**
  - Startup refuses any profile that is not `gate0:`, in routes, fallbacks,
    grounding, selectable models or embeddings.
  - The router also refuses such a profile at runtime.
  - With no Gemini model behind gate0, grounding is switched off. It is never
    sent around the gateway.
  - At startup, the service lists gate0's models. It logs an ERROR for each
    configured model gate0 does not serve. It logs a WARNING for a selectable
    non-Gemini model with no `max_output_tokens`. It never blocks startup.
- **A blocked result is final.** A content-filter or safety block is returned
  as is and is never routed to another profile.
- **A gate0 budget block is final.** A 429 whose message says "budget" is not
  retried and does not fall back. A plain rate-limit 429 is retried once.
- **Timeouts.** Each call is bounded by `GEMINI_REQUEST_TIMEOUT_SECONDS`
  (default 300 s; 0 or less turns the bound off). The strategy and objective
  regeneration legitimately takes about 2 minutes. A timeout moves to the next
  profile without a same-profile retry. SDK-level retries are off, so retries
  are not multiplied.
- **Retries.** `LLM_TRANSIENT_RETRIES` (default 1) applies to 429, 5xx and
  network errors, with backoff capped at 8 s.
- **Model features come from configuration**, not model names, for models
  behind gate0. `LLM_MODEL_OPTIONS` sets `family`, `thinking`, `grounding`,
  `supports_documents`, `supports_images`, `max_output_tokens` and
  `extra_body`. A model with no options gets a plain request. A name
  containing "gemini" is still treated as Gemini.
- **Feature code never imports a provider SDK.** Adapters live only in
  `app/llm/providers/`.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| `aiModel` is not an approved id | The case keeps its previous model | Logs a warning and ignores the value. |
| The chosen model's provider fails | Answer comes from the fallback model; `aiModel` shows that model | Retries once if transient, then moves down the chain. |
| Every profile fails or times out | Panel or chat reports a failure | Returns an error sentinel. Callers check it with `is_generation_error()`. |
| Content is blocked by a safety filter | Empty or partial answer | Returns the `blocked` result. Never tries another model. |
| Gate0 budget is spent | Failure message | Final error. No retry, no fallback. |
| Gate0 is down under `GATE0_ENFORCE` | AI features fail | No direct fallback, by design. |
| A configured model is missing from gate0 | Calls to that model fail | Startup logs an ERROR naming the model. |
| A misconfigured explicit profile (missing key or SDK) | Service does not start | Startup raises with the profile and reason. Secret values are never printed. |

## For engineers

### Where the code is

- `app/ai/gemini_client.py`: the facade. `generate`, `generate_grounded`,
  `generate_sync`, embeddings, and `initialize`, which runs startup validation.
- `app/llm/router.py`: `ModelRouter.complete`, `chain_for`,
  `blocked_by_governance`, `gate0_unserved_models`, `embed`, `_log`.
- `app/llm/config.py`: `load_routing`, `selectable_models`, `model_options`,
  `model_family`, `pricing_overrides`.
- `app/llm/errors.py`: `with_timeout`, `request_timeout_seconds`,
  `reraise_if_cancelled`.
- `app/llm/providers/`: one adapter per provider, registered in `__init__.py`.
  `gate0.py` is the gateway adapter.
- `app/workspace/model_selection.py`: case and turn choice, persistence and
  purge.
- `app/socket/events.py`: `_apply_model_selection` reads `aiModel` from the raw
  payload in `handle`.
- `app/core/lifecycle.py`: `_check_gate0_models` runs the startup check.
- Operator guide: `docs/MODEL_PORTABILITY.md`.

### Events

- `aiModel` on `workspace_created`, `workspace_sync`,
  `workspace_version_restore` and `user_message_request`. It is read from the
  raw payload and is not a field of the pydantic schemas. See
  [Backend ↔ AI service — socket events](../contracts/backend-ai-socket-events.md).
- `metadata.aiModel` on every emit.
- Gateway traffic: [Model calls through gate0](../contracts/model-calls-through-gate0.md).

### Data

- `MODEL_SELECTION` cache (Qdrant, write-through), keyed by workspace. It holds
  `{"model": <id>}`. It is not reached by the handler's `__dict__` purge sweep,
  so `_purge_workspace` clears it explicitly.

### Security

- Provider keys are `SecretStr` settings and are never logged.
- Gate0 attribution sends ids only. The case id travels as `case_id`, never as
  a `workspace_id` key.
- `GATE0_ENFORCE` closes every direct-provider path, at startup and at runtime.
- Least privilege for Azure and AWS is described in `docs/MODEL_PORTABILITY.md`.

### Tests that guard it

- `tests/test_model_selection.py`: case choice, turn override, unknown ids,
  absent versus empty, pinned tasks, grounding and embeddings ignore the
  choice, hydration, purge.
- `tests/test_llm_router.py`: chain order and fallback, no fallback after a
  block, retry, timeout, cancellation, capability skips, clamping, the
  no-SDK-outside-adapters boundary.
- `tests/test_llm_providers.py`: request and response mapping per adapter.
- `tests/test_llm_gate0.py`: gate0 mapping, enforcement at startup and
  runtime, budget block, families, startup served-model check, banned keys.
- Live, billed: `scripts/smoke_llm_providers.py`.

### Known gaps

- Prompts were tuned on Gemini. Quality on other models is not checked
  automatically. Run `scripts/eval_rag.py` and `make e2e` before routing a
  panel to a new model.
- Grounding (Google Search) is Gemini-only. Without a Gemini profile it
  returns nothing, and case-law search falls back to its other sources.
- No feature sends image, PDF or audio bytes to a model today, so capability
  skips are not exercised in production.
- Could not verify the lawyer-app model picker or which ids it lists. That is
  outside this repo.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-16 | Model features through gate0 from configuration; startup served-model check; `case_id` attribution key | SIM-29, 2e1242c |
