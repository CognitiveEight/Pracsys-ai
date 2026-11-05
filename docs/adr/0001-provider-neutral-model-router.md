# 0001: Provider-neutral model router behind `gemini_client`

- **Date:** not recorded
- **Jira:** none recorded

## Context

The service started on Gemini only, and feature code called it through
`gemini_client` (`app/ai/gemini_client.py`). The service then needed to run
each task on a provider chosen by configuration: Gemini, OpenAI, Azure OpenAI,
Azure AI Foundry, Bedrock, SageMaker, or any OpenAI-compatible server
(DeepSeek, Qwen, Llama and others). Feature code is large and calls the model
from many places. Changing every call site, or letting each feature handle
timeouts, retries, fallback and JSON repair itself, would have spread provider
logic across the codebase.

## Decision

- `gemini_client` keeps its name and API, and becomes a facade over a
  `ModelRouter` in `app/llm/`.
- The router picks a profile chain per task: `LLM_ROUTES[task]` or
  `LLM_DEFAULT`, then `LLM_FALLBACKS`. The route key is the `feature=` value
  on the `llm_call` log line (the `@meter_usage` label, else the calling
  function, or an explicit `task=`).
- Provider SDKs are imported only in `app/llm/providers/`. A new provider is
  one adapter plus a registry entry.
- Adapters only translate. Timeout, retry, fallback, JSON repair, the
  `llm_call` line and usage recording live once, in the router.
- A `blocked` result (content filter or safety block) is final and is never
  routed to another provider.
- With no `LLM_*` settings, every call goes to Gemini as before.

## Consequences

- Choosing a provider per task is configuration, not code.
- Feature code still checks failure with `is_generation_error()`, because the
  facade returns the same error sentinels as before.
- Sync code in a worker thread must use `gemini_client.generate_sync`, so cost
  lands in the caller's scope; it may not call an SDK directly.
- The user's model choice (`aiModel`, `app/workspace/model_selection.py`)
  heads the chain; only ids in `LLM_SELECTABLE_MODELS` are honoured.
  Grounding, embeddings and pinned tasks do not follow it.
- An embedding provider, model or dimension change means a new collection and
  a re-index.
- `tests/test_llm_router.py` fails the build if a provider SDK is imported
  outside `app/llm/providers/`.

## Status

Accepted (documented retroactively).

## Sources

- `CLAUDE.md`, "Model calls are provider-neutral".
- `docs/MODEL_PORTABILITY.md`, "How a call is routed", "Routing
  configuration", "User-selected model per case".
- `app/llm/router.py`, `app/llm/providers/`, `app/ai/gemini_client.py`.
- `tests/test_llm_router.py`, `tests/test_llm_providers.py`.
