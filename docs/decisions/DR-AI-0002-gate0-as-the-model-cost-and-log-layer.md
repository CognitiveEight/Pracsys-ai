---
title: gate0 as the single model, cost and log layer
type: decision
repo: Pracsys-ai
owner: AI team
status: accepted
audience: everyone; engineers below "What this means"
last_verified: 2026-10-21
jira: SIM-113
number: DR-AI-0002
date: 2026-10-16
decided_by: AI team lead (role)
---

# DR-AI-0002 gate0 as the single model, cost and log layer

## In plain words

gate0 is a shared AI gateway that sits between our apps and the AI model
makers. It holds the provider keys, the list of approved models, the budgets
and the spending records for every app. We decided the AI service sends its
model calls through gate0, and takes gate0's own figure as the cost of each
call. Every call is logged with gate0's reference number, so our records and
gate0's records match one to one, case by case. We can switch on a strict mode
where gate0 is the only way to reach a model. We are leaving that strict mode
off for now: with it on, a gate0 outage would stop every AI feature, so it
waits until gate0 runs with high availability.

## Why we needed to decide

- Model spend for a case was computed from a local price table, and it did not
  match what the gateway charged; background work such as graph ingestion was
  not billed to any case.
- Provider keys, budgets and model approval are a company-wide concern, better
  held in one governed place than in each app.
- Support needed one id to join a SimplerToday log line to the gateway's spend
  record.

## Options we considered

| Option | Pros | Cons |
|---|---|---|
| Keep direct provider calls with local pricing | No dependency on another team's service | Two bills that never reconcile; keys and budgets in every app |
| Route through gate0, keep local pricing and logs separate | Central keys and budgets | Costs still disagree; no join key |
| Route through gate0 and adopt its cost and call id as ours, with optional enforcement | One bill, one log key, central governance; enforcement can be switched on later | Depends on gate0 config (named model groups, 300 s timeout, no cross-model fallback); enforcement makes gate0 a hard dependency |

## What we decided and why

The third option (SIM-29). A `gate0:<model>` profile
(`app/llm/providers/gate0.py`) is one more adapter behind the router from
[DR-AI-0001](DR-AI-0001-one-model-router.md):

- **One bill.** The cost gate0 returns (`x-litellm-response-cost`) is what the
  case is billed, for generations and embeddings.
- **One log key.** Every `llm_call` line carries `cost=` and
  `gateway_call_id=` (gate0's `x-litellm-call-id`).
- **Attribution by id only.** Requests carry `app`, `feature`, `kind`,
  `case_id`, `request_id`, `event`. The key is `case_id`, never
  `workspace_id`, which LiteLLM rejects with a 401.
- **Model features from configuration** (`LLM_MODEL_OPTIONS`), not from the
  model name.
- **Budgets.** A budget 429 is final; a plain rate-limit 429 is retried once.
- **Enforcement (`GATE0_ENFORCE`)** refuses any non-gate0 profile at startup and
  at runtime. Default `False` in `app/core/config.py`, and kept off until gate0
  has high availability.
- **Non-model units** (OCR, Speech-to-Text, search, grounding) can be reported
  to gate0's meter (`GATE0_METER_ENABLED`, default off; the endpoint is not
  deployed yet).

## What this means

- New model call paths must go through the router so the governance check
  applies under enforcement.
- Costs reach a case through the workspace ledger (`app/ai/usage.py`); never add
  to `token_usage` another way, or the case stops reconciling.
- Reconcile with `scripts/reconcile_gate0_spend.py` (model cost only; OCR,
  speech and search never pass through gate0). See
  [Reconcile gate0 spend](../runbooks/reconcile-gate0-spend.md).
- gate0-side configuration this needs is listed in `docs/MODEL_PORTABILITY.md`.
- Guarded by `tests/test_llm_gate0.py` (enforcement, attribution, cost header,
  call id, budget 429), `tests/test_gate0_meter.py`,
  `tests/test_usage_ledger.py`.
- Contract: [Model calls through gate0](../contracts/model-calls-through-gate0.md).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-08 | gate0 gateway mode added as a provider profile (enforcement off by default) | 33d8978 |
| 2026-10-16 | gate0 as the single model, cost and log layer | SIM-29, 2e1242c |
| 2026-10-21 | First version, written from the code | SIM-113 |
