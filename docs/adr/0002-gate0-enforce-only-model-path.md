# 0002: `GATE0_ENFORCE` makes Gate0 the only model path

- **Date:** not recorded
- **Jira:** none recorded

## Context

Gate0 is the organisation's AI gateway, built on the LiteLLM proxy. It holds
the provider keys, budgets, the approved-model list and the spend records for
all apps. The router (ADR 0001) can reach providers directly, which would let
a call skip the gateway's approval list, budgets and spend records.

## Decision

- The service reaches Gate0 through `gate0:<model>` profiles
  (`app/llm/providers/gate0.py`).
- With `GATE0_ENFORCE=true`:
  - startup refuses any profile that is not `gate0:`, across routes,
    fallbacks, grounding and embeddings;
  - the router also refuses non-`gate0:` profiles at runtime, so no call can
    reach a provider directly;
  - if grounding has no Gemini-backed Gate0 profile, grounding is turned off
    (`generate_grounded` returns `("", [])`) rather than sent around the
    gateway.
- Any new model-call path must go through the router, so the check applies.
- Requests carry attribution metadata and tags with ids only (`app`,
  `feature`, `kind`, `workspace_id`, `request_id`, `event`), never names or
  prompt text.
- The cost Gate0 reports is what the service bills for calls through it.

## Consequences

- **Availability.** With enforcement on, a Gate0 outage stops the service's
  AI features. There is no direct fallback, by design. Gate0 needs high
  availability before `GATE0_ENFORCE` is turned on in an environment.
- A budget 429 from Gate0 is final: no retry and no fallback.
- Gate0's own configuration must match the service's needs: model groups
  named by model, a timeout of at least 300 s, `drop_params: false`, and no
  cross-model fallback for this key. These are owned by Gate0, not by this
  repository.
- Vision OCR, Speech-to-Text, Vertex AI Search and Indian Kanoon are not model
  calls and do not pass through Gate0; they keep their own credentials and
  cost tracking.
- Per-case spend can be reconciled against Gate0 with
  `scripts/reconcile_gate0_spend.py`, which only works while every cost is
  recorded through the workspace ledger.

## Status

Accepted (documented retroactively).

## Sources

- `CLAUDE.md`, "Model calls are provider-neutral" (rule "Under
  `GATE0_ENFORCE` nothing may bypass gate0") and "Cost tracking covers more
  than tokens".
- `docs/MODEL_PORTABILITY.md`, "Gate0 (governed gateway)", including item 7
  "High availability before `GATE0_ENFORCE`".
- `app/llm/providers/gate0.py`, `app/llm/router.py`.
- `tests/test_llm_gate0.py`.
