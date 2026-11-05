---
title: Model calls through gate0
type: contract
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "Messages"
last_verified: 2026-10-21
jira: SIM-113
between: "ai-service, gate0"
owner_ours: AI team
owner_theirs: Gate0 team
version: "OpenAI-compatible API v1 via LiteLLM; usage-record schema 1.1.0 (pending on gate0)"
source_of_truth: app/llm/providers/gate0.py
---

# Model calls through gate0

## In plain words

Gate0 is the company's AI gateway. It holds the keys for every AI vendor,
enforces budgets and keeps one record of every call and its cost. The AI
service can send all of its AI requests through it. With each request it sends
labels: which app, which feature, which case and which request. It never sends
names or case text in those labels. Gate0 answers with the result, the price it
charged and its own record number. The AI service bills the case that exact
price, and writes the record number in its log. Finance can then match every
call on both sides, one to one. For this to work, the gateway must be set up in
a few specific ways, listed below. Some of that setup is still to be agreed.

## How we connect

- **Protocol.** HTTPS, OpenAI-compatible API, served by the LiteLLM proxy. The
  AI service uses the `openai` SDK, inside `app/llm/providers/gate0.py` only.
- **Address and key.** `GATE0_BASE_URL` (ends in `/v1`) and `GATE0_API_KEY` (a
  gate0 virtual key for this service, sent as a bearer token). Both are
  required, or every `gate0:` profile is unavailable.
- **Which calls.** Any profile written `gate0:<model>`, in `LLM_DEFAULT`,
  `LLM_ROUTES`, `LLM_FALLBACKS`, `LLM_GROUNDING_PROFILES` or
  `LLM_SELECTABLE_MODELS`. `EMBEDDING_PROVIDER=gate0` adds embeddings.
  `<model>` is any name gate0 serves: a model group such as `gemini-3.8-flash`,
  or a task class such as `gate0/lite`.
- **Enforcement.** With `GATE0_ENFORCE=true`, gate0 is the only path. Startup
  and the router both refuse any profile that is not `gate0:`.
- **Not through gate0.** Vision OCR, Speech-to-Text, Vertex AI Search and Indian
  Kanoon are separate services. Their usage can be reported to gate0's meter,
  but their traffic does not pass through it.

## Messages

| Event | Direction | When | Key fields | What the receiver must do |
|---|---|---|---|---|
| `POST /v1/chat/completions` | ai-service → gate0 | Every generation and grounded generation | `model`; `messages`; `max_tokens`; `temperature`; `response_format` (JSON mode); `reasoning_effort` when thinking is on; Gemini only: `safety_settings` BLOCK_NONE and `web_search_options` for grounding; `extra_body` from `LLM_MODEL_OPTIONS`; `metadata` (see below) | Route to the named model group without silent parameter drops. Return the completion with `usage`, plus the cost and call-id headers. Never fall back to another model. |
| chat completion response | gate0 → ai-service | Reply to the above | body: `choices[0].message.content`, `annotations[].url_citation` (grounding sources), `finish_reason`, `usage`; headers: `x-litellm-response-cost`, `x-litellm-call-id` | AI service bills the header cost unrounded and logs the call id as `gateway_call_id`. With no cost header, it falls back to the local price table. |
| `POST /v1/embeddings` | ai-service → gate0 | Every embedding batch, when `EMBEDDING_PROVIDER=gate0` | `model`; `input[]`; `dimensions` (unless `send_dimensions: false`); `task_type` (Gemini) or `input_type` (Cohere) in `extra_body`; `metadata` with `feature: embeddings`, `kind: embed` | Return vectors of the requested size, `usage.prompt_tokens`, and the same two headers. |
| `GET /v1/models` | ai-service → gate0 | Once at startup, only under `GATE0_ENFORCE` | none (10 s timeout) | List the models this key may use. The AI service logs an ERROR for each configured model not listed, and never blocks startup. |
| `POST <GATE0_BASE_URL><GATE0_METER_PATH>` (default `/usage-records`) | ai-service → gate0 | Every `GATE0_METER_FLUSH_SECONDS` (default 10 s) or 100 records, only when `GATE0_METER_ENABLED=true` | `{"records": [...]}`, schema 1.1.0: `call_id`, `status`, `requested_model`, `routed_provider`, `routed_model`, `cost_usd`, `usage_kind` (ocr, stt, search, grounding), `units{kind, quantity}`, `attribution{app, workspace_id, feature, request_id}`; token fields null | Validate, fill `team` / `key_alias` from the key, derive `record_id` from `call_id`, dedupe. **Not deployed on gate0 yet.** |
| `GET /spend/logs` | operator script → gate0 | When `scripts/reconcile_gate0_spend.py` is run | `start_date`, `end_date`, `summarize=false`; admin key from an environment variable | Return per-request spend rows with `request_tags`. Needs gate0's database mode. |

### Attribution sent with every chat and embedding request

Sent unless `GATE0_TAG_REQUESTS=false`. All values are ids, never names or text.

| Key in `metadata` | Value |
|---|---|
| `app` | `GATE0_APP_NAME` (default `simplertoday`) |
| `feature` | the route key: the `@meter_usage` feature or the calling function; `embeddings` for embeddings |
| `kind` | `generate`, `chat`, `grounded`, `longform`, `longform_cont<n>`, `json_repair`, `embed` |
| `case_id` | the workspace id from the running event |
| `request_id` | the inbound event's correlation id |
| `event` | the inbound socket event name |
| `tags` | `app:<app>`, `feature:<feature>`, `workspace_id:<id>` |

A key is left out when its value is empty. There is **no `workspace_id`
key** in the body or in `metadata`. LiteLLM 1.105.1 rejects such a request with
401, because `workspace_id` is on its client-credential ban list. The case
travels as `case_id`, and as the `workspace_id:<id>` tag that reconciliation
reads.

## Rules both sides must follow

1. **One call, one record.** Each request yields one gate0 spend record. Its
   `x-litellm-call-id` is that record's id and appears once in the AI service
   log as `gateway_call_id`.
2. **Gate0's price is the bill.** The AI service bills `x-litellm-response-cost`
   for chat and embeddings, unrounded until the emit. Gate0 must return the
   header on both endpoints.
3. **Timeouts.**
   - The AI service bounds each call at `GEMINI_REQUEST_TIMEOUT_SECONDS`
     (default 300 s). Its HTTP client waits that plus 5 s.
   - Gate0's timeout for the groups this service uses must be **at least 300 s**
     and above that setting. Gate0's default is 60 s, and the strategy and
     objective regeneration legitimately takes about 2 minutes.
4. **Retries live in the AI service.** SDK retries are off. The AI service
   retries a transient error (429, 5xx, network) once, then moves down its own
   chain.
5. **Budget blocks are final.** A 429 whose message contains "budget" is never
   retried and never falls back. Gate0 must keep that word in the message.
6. **Content blocks are final.** Gate0 must not reroute a blocked request to
   another model. The AI service never does.
7. **Ids only.** No names, prompt text, document text or URLs in metadata, tags
   or meter records.
8. **Embedding vectors must not change.** `gemini-embedding-001` at 768
   dimensions through gate0 must match direct Gemini (verified cosine 1.0 on
   2026-10-16), or the evidence collection must be re-indexed.

### Gate0-side configuration this service needs

From SIM-29 and `docs/MODEL_PORTABILITY.md`:

1. **Model groups named by model** (for example `gemini-3.8-flash`,
   `gemini-2.5-pro`, `gpt-4o`, `gemini-embedding-001`), so the service can
   choose a model per feature and per case. Gate0's ADR-0003 rejects raw
   names, so this needs a change there.
2. **Timeout of at least 300 s** on those groups.
3. **`drop_params: false`** for this key or these groups. A silently dropped
   `response_format` or `safety_settings` breaks JSON panels or blocks legal
   evidence. The global setting is kept on today. See the open questions.
4. **No fallbacks across models or classes** for this key. The service runs its
   own chain and must know which model answered.
5. **Every provider the service routes to is in `model_list`**: Azure OpenAI,
   Foundry, Bedrock, SageMaker, DeepSeek or Qwen.
6. **Request metadata kept in spend logs**, so gate0's records show case and
   feature. LiteLLM keeps `tags` today. Gate0's own usage-record callback drops
   caller metadata. SIM-29 part G asks gate0 to fill `attribution`.
7. **High availability before `GATE0_ENFORCE`.** With enforcement on, a gate0
   outage stops every AI feature. There is no direct fallback, by design.

## Changing this contract

- Any change to request keys, metadata or tags goes through
  `app/llm/providers/gate0.py` and `tests/test_llm_gate0.py`. In particular,
  `test_requests_never_carry_a_key_litellm_rejects` guards the banned-key rule.
- A new model through gate0 needs an `LLM_MODEL_OPTIONS` entry (`family`,
  `thinking`, `max_output_tokens`, …) and a smoke run of
  `scripts/smoke_llm_providers.py` (billed).
- Model routing and billing changes get the `needs-human-review` PR label.
- The meter schema (1.1.0) is a gate0 contract change. Gate0 ratifies it under
  its `contracts/VERSIONING.md`.

## Open questions with the other team

- **Usage-record v1.1 and `POST /v1/usage-records`** (SIM-29 part G): not
  deployed. Who ratifies it? Until then the meter stays off.
- **Server-resolved identity.** Is it enough that gate0 resolves `team` and
  `key_alias` from the key, while `workspace_id` and `feature` stay
  caller-supplied attribution?
- **`drop_params`.** `docs/MODEL_PORTABILITY.md` says gate0 keeps the global
  `drop_params: true`, and calls that safe because Gemini-only parameters go
  only to Gemini models. Item 3 above still asks for `false`. Confirm which
  holds.
- **Model groups.** `docs/MODEL_PORTABILITY.md` reports the named groups (each
  with `timeout: 300`, no cross-model fallback, approved under
  `named_models`) as done in gate0's config. That cannot be verified from this
  repo.
- **Spend logs** need gate0's database mode, which the dev gateway lacks.
- **Retention.** The case purge does not reach gate0's spend logs or meter
  records. These hold ids only. Compliance sign-off is open.
- **Meter pricing.** Should non-model usage be priced by this service's rates,
  as today, or by a gate0 price table?

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
| 2026-10-16 | Call id and cost on every call; gateway embedding cost; `case_id` replaces the `workspace_id` metadata key; family-driven parameters; served-model check; optional meter | SIM-29, 2e1242c |
