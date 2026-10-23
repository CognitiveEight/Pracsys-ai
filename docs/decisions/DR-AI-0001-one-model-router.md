---
title: All model calls go through one provider-neutral router
type: decision
repo: Pracsys-ai
owner: AI team
status: accepted
audience: everyone; engineers below "What this means"
last_verified: 2026-10-21
jira: SIM-113
number: DR-AI-0001
date: 2026-10-08
decided_by: AI team lead (role)
---

# DR-AI-0001 All model calls go through one provider-neutral router

## In plain words

The AI service used to talk to one model maker, Google, directly from many
places in the code. That tied every feature to one vendor and spread the
handling of errors, time limits and costs across dozens of call sites. We
decided that every request to an AI model goes through one switchboard. The
switchboard picks the model for each task from configuration, retries or falls
back when a model fails, writes one log line, and records the cost. Features
never talk to a model maker themselves. This lets us move to another provider,
or to a different model for one task, by changing settings rather than code.
If a model refuses content on safety grounds, we accept that answer and do not
try another provider.

## Why we needed to decide

- Feature code called the Gemini SDK through a single client, but timeouts,
  retries, JSON clean-up and usage recording were not uniform, and some
  failures surfaced as misleading "no JSON" errors.
- The business needed a path to AWS and Azure hosting and to open-weight models
  (see `docs/TARGET_ARCHITECTURE.drawio`), plus a per-user model choice.
- Cost per call had to be priced by the model that actually answered, not by a
  single configured model name.

## Options we considered

| Option | Pros | Cons |
|---|---|---|
| Keep calling Gemini directly from features | No migration work | Vendor lock-in; behaviour on failure differs per call site; no per-task model choice |
| Adopt a third-party framework's model abstraction everywhere | Many providers out of the box | Another large dependency in every feature; still no single place for our cost and log rules |
| One in-house router behind the existing client facade, thin adapters per provider | Feature code unchanged; timeout, retry, fallback, JSON repair, logging and cost in one place; new provider = one adapter | We own the router and its tests |

## What we decided and why

We built `ModelRouter` (`app/llm/router.py`) behind the existing
`gemini_client` facade (`app/ai/gemini_client.py`), with one adapter per
provider in `app/llm/providers/` (Gemini, OpenAI and OpenAI-compatible, Azure
OpenAI and AI Foundry, AWS Bedrock and SageMaker, gate0). The facade kept its
API and its error sentinels, so feature code did not change. With no `LLM_*`
settings every call still goes to Gemini, which made the change safe to ship.

Rules that come with it:
- No provider SDK is imported outside `app/llm/providers/`.
- Adapters only translate. Timeout, retry, fallback, JSON repair, the
  `llm_call` log line and usage recording live in the router, once.
- A `blocked` result (safety finish, content filter, guardrail) is final and
  never routed to another provider.
- The route key is the `feature=` on the `llm_call` line.
- The user's selected model heads the chain, except for grounding, embeddings
  and pinned routes.

## What this means

- A new provider is one adapter plus a registry entry; a new model for one task
  is an `LLM_ROUTES` entry.
- Sync code in a worker thread must use `gemini_client.generate_sync`, which
  runs on the main loop so cost lands in the right scope.
- Every `generate` result must be checked with `is_generation_error()`.
- Changing the embedding provider, model or dimension means a new Qdrant
  collection and a re-index (`scripts/reindex_chunks.py --from-collection`).
- Enforced by `tests/test_llm_router.py`
  (`test_no_provider_sdk_is_imported_outside_the_adapters`,
  `test_blocked_is_final_and_never_routed_around`,
  `test_default_routing_is_unchanged_gemini`) and
  `tests/test_llm_providers.py`. Live, billed check:
  `scripts/smoke_llm_providers.py`.
- Reference: `docs/MODEL_PORTABILITY.md`;
  [AI service – layered architecture](../architecture/ai-service-layered.md);
  [Model choice](../features/model-choice.md).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-08 | Router and provider adapters landed ("model portability integration") | 33d8978 |
| 2026-10-13 | Model portability completed | 387aa5a |
| 2026-10-21 | First version, written from the code | SIM-113 |
