---
title: AI cost tracking
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows: []
other_side:
  backend: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4816920
  admin: https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/5177363
related_specs:
  - specs/requirements/SIM-29-gate0-central-ledger.md
  - specs/technical-design/SIM-29-gate0-central-ledger.md
  - specs/acceptance-criteria/SIM-29-gate0-central-ledger.md
---

# AI cost tracking

## In plain words

Every time the AI does work for a case, it costs money. The bill is mostly
for the AI model itself. Reading scanned pages, transcribing audio and
searching for case law are paid services too. The AI service adds up all of
these costs for each case. It attaches two figures to every answer and every
panel it sends back: what that piece of work cost, and what the whole case has
cost so far. Work that runs quietly in the background is charged to its case
as well. It shows up with the next answer or panel. When model calls go
through the company's AI gateway, the case is billed the gateway's own price
for each call. Each call can be matched to the gateway's record. The figures
are an upper bound: free monthly allowances and volume discounts are not
counted.

## Who uses it

- **Firm admins and finance** read per-case cost in the admin app (see the
  admin page linked in the front matter).
- **The backend** stores each cost record and keeps the running total per
  case (see the backend page).
- **Operations** compares each case's cost with the AI gateway's spend records.
- Lawyers do not see cost in the lawyer app today. No lawyer-app page exists
  for this feature.

## What the user sees

Nothing changes in the lawyer's workflow. Admins see a per-case total, built
from the cost block that the AI service sends with every reply and panel. The
backend and admin pages describe the screens.

## How it works

1. **Every paid unit is recorded where it is spent.** Each model call reports
   its tokens and price. Scanned-page OCR reports one unit per image or page.
   Speech-to-text reports billed audio seconds. Vertex AI Search reports
   queries. Google Search grounding reports requests. Embeddings report input
   tokens. Each is reported once, at the lowest call site.
2. **Each unit is charged to its case at once.** The case comes from the
   inbound event's logging context. The unit goes into that case's ledger. It
   also goes into the running usage scope, which is one chat turn or one panel
   generation.
3. **Each emit drains the ledger.** When a panel (`document_feature_response`)
   or chat reply (`ai_response`) is sent, the service takes everything charged
   to the case since its previous emit. It prices that spend, puts it in the
   `metadata` block and adds it to the case's running totals. Each charge is
   therefore reported by exactly one emit.
4. **Background work is billed to its case.** Knowledge-graph ingestion,
   re-evaluations and other work with no emit of its own still charge the
   ledger. That spend appears on the case's next emit.
5. **Gate0 calls are billed at gate0's price.** When a call goes through gate0
   (the AI gateway), the service reads the cost gate0 computed and bills that
   figure, unrounded, instead of the local price table. This covers chat and
   embeddings. The same response carries gate0's call id, which is printed on
   the `llm_call` log line.
6. **Totals persist.** Running totals live in the workspace context's
   `token_usage` metadata. They are saved with the workspace cache, so they
   survive a restart.
7. **Re-emits add nothing.** A reconnect replay or cached re-send reports zero
   for this response, plus the current running totals.

## Rules and limits

- `price` is the whole cost of the response: input tokens + output tokens +
  `servicePrice`. `totalPrice` is the whole cost of the case so far, by the
  same rule.
- `servicePrice` is the sum of `ocrPrice`, `sttPrice`, `searchPrice`,
  `groundingPrice` and `embedPrice`. Each has a matching unit count
  (`ocrUnits`, `sttSeconds`, `searchQueries`, `groundingRequests`,
  `embedTokens`) and a cumulative `total*` field.
- Every metadata block carries a fresh `messageId`. The backend keys each cost
  record on it and drops a record without one.
- `aiModel` in the block is the model that did most of the work in that scope.
- Each model call is priced by the model that served it. A turn that mixes
  models is billed correctly.
- Free tiers and volume discounts are not modelled. Every figure is an upper
  bound on the real invoice.
- Embedding tokens are the provider's own count when the response reports
  one. Otherwise, as on direct Gemini, they are an estimate of about four
  characters per token.
- Rates can be changed without a deploy, using these environment variable names:
  - `LLM_PRICING_OVERRIDES`: per-model input and output rates in USD per 1M
    tokens, by exact name or prefix. This is how non-Gemini models are priced.
  - `TOKEN_PRICE_INPUT_PER_MILLION` and `TOKEN_PRICE_OUTPUT_PER_MILLION`: when
    both are set, they override every model's rate.
  - `OCR_PRICE_PER_1K_UNITS`, `STT_PRICE_PER_MINUTE`,
    `SEARCH_PRICE_PER_1K_QUERIES`, `GROUNDING_PRICE_PER_1K_REQUESTS`,
    `EMBED_PRICE_PER_MILLION_TOKENS`.
- A model with no price entry is priced at a default rate, with one warning
  per model.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Paid work runs with no case and no usage scope | Cost missing from every case | Logs a warning once per kind ("consumed outside any capture_usage scope"). The cost is billed to nobody. |
| Paid work runs with a scope but no case in context | Cost appears on that scope's own emit only | Keeps it as unattributed spend on the scope. The scope's emit reports it. |
| Work is user-scoped, not case-scoped (template library ingestion) | No case cost | Prices the spend and logs it as not billed to a workspace. |
| Gate0 returns no cost header | Cost is still shown | Falls back to the local price table or embedding estimate. Never bills zero. |
| A model has no price entry | Cost may be off | Uses the default rate and warns once per model. |
| Building the cost block fails | Reply or panel still arrives, with an empty `metadata` | Logs a warning. Pricing never blocks a response. |
| The emit is dropped (socket down) | Panel arrives after reconnect | Spend is still added to the case total before the emit. The envelope is buffered and re-sent. |
| The case is deleted | Cost history leaves the AI service | The purge drops any unreported ledger spend for the case. |

## For engineers

### Where the code is

- `app/ai/usage.py`: `UsageAccumulator`, `capture_usage`, `@meter_usage`, the
  `record_*` helpers, the workspace ledger (`_record`, `drain_for_emit`,
  `forget_workspace_usage`), `llm_price`, `primary_model`,
  `log_unbilled_usage`.
- `app/ai/pricing.py`: `price_for` (tokens) and `service_price_for` (OCR, STT,
  search, grounding, embeddings). Rate resolution order: both
  `TOKEN_PRICE_*` set, then `LLM_PRICING_OVERRIDES`, then the table, then
  longest prefix, then the default.
- `app/socket/handlers/chat.py`: `_token_usage_metadata` (drain, price, add to
  totals), `_cumulative_usage_metadata` (re-emit), `_emit_document_feature`
  (the panel emit chokepoint), `_warn_unknown_metadata_keys`.
- `app/workspace/context.py`: `add_token_usage` keeps the running totals.
- `app/llm/router.py`: `_record` calls `record_usage` per model call. `_log`
  writes the `llm_call` line with `cost=` and `gateway_call_id=`.
- `app/llm/providers/gate0.py`: reads `x-litellm-response-cost` and
  `x-litellm-call-id` for chat and embeddings.
- `app/ai/gate0_meter.py`: optional usage records to gate0 for OCR, STT, search
  and unpriced grounding (`GATE0_METER_ENABLED`, off by default).
- `scripts/reconcile_gate0_spend.py`: read-only comparison of each case's model
  cost with gate0's spend logs. It filters on the `workspace_id:<id>` tag.
  Options: `--by-feature`, `--workspace`, `--since`, `--until`,
  `--tolerance`. It reads an admin key from the environment variable named by
  `--key-env` (default `GATE0_ADMIN_KEY`).

### Events

- Every `document_feature_response` and `ai_response` carries the `metadata`
  block. Its field list is `AIResponseMetadata` in `app/socket/schemas.py`.
  See [Backend ↔ AI service — socket events](../contracts/backend-ai-socket-events.md).
- How costs travel to gate0 and back:
  [Model calls through gate0](../contracts/model-calls-through-gate0.md).

### Data

- In memory: the per-workspace ledger in `app/ai/usage.py`, drained on emit.
- Persisted: `token_usage` in the workspace context metadata, saved through the
  `WORKSPACE_META` cache in Qdrant.
- Meter outbox (only when the meter is on): NDJSON under
  `GATE0_METER_OUTBOX_DIR`. It holds ids and quantities, no case content.

### Security

- Cost records and gate0 attribution carry ids only: app, feature, case id,
  request id. Never names, prompt text or document text.
- `llm_call` lines carry cost and an opaque gateway id, never prompt or output.
- The reconcile script reads its key from an environment variable, never from
  the command line.

### Tests that guard it

- `tests/test_usage_ledger.py`: ledger attribution, one report per charge,
  workspace isolation, purge, emit metadata totals, gateway embedding cost.
- `tests/test_llm_gate0.py`: gateway cost replaces the table price, embedding
  cost and fallback, `llm_call` carries `cost` and `gateway_call_id`.
- `tests/test_gate0_meter.py`: meter record shape, batching, outbox, cancel.
- `tests/test_llm_router.py`: per-model pricing.
- `tests/offline/verify_logging.py`: no payloads or text in log lines.

### Known gaps

- The default grounding rate is chosen from the configured Gemini model
  setting, not from the model that served the grounded call.
- Direct-Gemini embedding tokens are an estimate, not a provider count.
- Per-call table prices are rounded to 6 decimal places. Only gate0 costs stay
  unrounded until the emit.
- The gate0 usage meter needs gate0's usage-record endpoint, which is not
  deployed yet. It stays off.
- Reconciliation needs gate0's spend logs (its database mode). Totals that
  started before gate0 also include direct-Gemini spend gate0 never saw.
- The purge does not remove meter records already sent to gate0, or the
  outbox. This retention needs compliance sign-off (SIM-29 open risk).
- Could not verify from this repo how the backend and admin app store or
  display these fields.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-16 | Workspace ledger; gate0 cost for chat and embeddings; gate0 call id on `llm_call`; reconcile script; optional usage meter | SIM-29, 2e1242c |
| 2026-07-27 | Running totals kept across restarts | 7e819d5 |
| 2026-07-24 | Cost metadata with `messageId` on every panel emit | 590707d |
| 2026-07-23 | Per-response and running cost on chat replies | 3351cc3 |
