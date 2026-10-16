# SIM-29: Gate0 as the single model, cost and log layer

- **Jira:** SIM-29 (epic SIM-13, label `external-gate0`)
- **Owner:** product owner (SimplerToday), with the Gate0 owners for the Gate0 contract changes
- **Status:** draft
- **Target branch flow:** feature/SIM-29-gate0-central-ledger → development → staging → main

## Problem
The firm wants one governed layer (Gate0) that every AI request passes through,
so that cost per case and request logs are complete and agree, whichever model
a lawyer picks. Today:

- Model calls already pass through Gate0, but the cost and the logs do not
  fully line up:
  - embedding cost is a local estimate rather than Gate0's figure;
  - the service's per-call log line cannot be matched to Gate0's record of
    the same call;
  - Gate0's own usage record does not say which case a call was for.
- Non-Gemini models (Claude, GPT, Azure deployments) are second-class through
  Gate0: features they support (reasoning, PDF input) are switched off because
  the adapter recognises Gemini by name only.
- OCR, speech-to-text and case-law search are paid but never reach Gate0, so
  Gate0 shows only part of a case's bill.

## Goal
Gate0 is the one place to see what every case cost and why: every model call
(any provider) goes through it, and every non-model paid unit is reported to
it, with the case, feature and request attached.

## Scope
- In:
  - Embedding cost billed from Gate0's own figure.
  - Every model call's service log line carries Gate0's call id and cost, so the
    two logs join one-to-one.
  - Model features chosen per model family (Gemini, Anthropic, OpenAI) by
    configuration, not by model name, so Claude/GPT/Azure models get the
    features they support through Gate0. Gemini behaviour unchanged.
  - Cohere embeddings get the correct query/document type through Gate0.
  - At startup under `GATE0_ENFORCE`, a configured model Gate0 does not serve
    is reported loudly.
  - OCR pages, speech-to-text seconds, case-law search queries (and grounding
    requests Gate0 did not price) reported to Gate0's meter as usage records
    (decision (b)), behind a flag, once Gate0 offers the endpoint.
  - Gate0-side dependency, owned by the Gate0 owners (separate Gate0 change):
    usage-record schema v1.1 (optional case attribution and non-token units),
    an authenticated usage-record submission endpoint, and the gateway
    callback filling the attribution fields.
- Out (explicitly not doing):
  - Proxying OCR, speech-to-text or Vertex AI Search traffic through Gate0
    (option (c)); only their usage is reported.
  - Changing the embedding model or re-indexing any collection.
  - Wiring Bedrock/Azure credentials or model entries into a Gate0
    deployment (operations task per environment).
  - Any socket payload change; the emit `metadata` keeps its shape.
  - Gate0 budgets/DB mode rollout.

## Users and surfaces
- Every panel emit (`document_feature_response`) and chat reply (`ai_response`):
  their `metadata` cost now uses Gate0's embedding figure. Shape unchanged.
- Operations / finance: Gate0 spend views and `scripts/reconcile_gate0_spend.py`.
- Engineers reading logs: the `llm_call` line.

## Constraints
- [ ] Changes a socket payload or `featureType` (backend/frontend must be told)
- [ ] Role-dependent behaviour (must go through the role gate)
- [x] Touches personal data, PII redaction or the workspace purge (usage
      records leave the service: ids only, never names or text)
- [x] Adds a paid call (model, OCR, STT, search) — cost must be metered
      (changes how existing paid calls are billed and reported)
- [ ] Indian-law output (BNS/BNSS/BSA; no IPC in `applicable_laws`)
- [ ] Shared workspaces: a `view` collaborator must not be able to mutate

## Success measure
- `scripts/reconcile_gate0_spend.py --by-feature` shows no difference beyond
  tolerance for model **and embedding** cost on a test case.
- For a sampled run, every `llm_call` line's `gate0_call_id` is found in Gate0's
  spend logs with the same cost.
- With the meter flag on, Gate0's meter holds one record per OCR/STT/search
  report, attributed to the right `workspace_id`, and their summed cost equals
  the case's `totalServicePrice` minus embedding cost.
- Smoke test passes for one model of each family through Gate0.

## Open questions
1. Gate0 owners: accept usage-record v1.1 (optional `attribution`, `usage_kind`,
   `units`) and a `POST /v1/usage-records` endpoint as MINOR contract changes?
   Who ratifies (VERSIONING.md lists programme lead, first-tool lead,
   Meter0 lead, CTO)?
2. Is Gate0's "server-resolved identity" rule satisfied by resolving
   `team`/`key_alias` from the submitting key while `workspace_id`/`feature`
   stay caller-supplied attribution (not identity)?
3. Which Claude / Azure / OpenAI models go into `LLM_SELECTABLE_MODELS` first?
   Each needs a verified family entry and output cap.
4. Should non-model usage pricing come from the service's rates
   (`service_price_for`) or from a Gate0 price table?
