# SIM-29: Gate0 as the single model, cost and log layer — acceptance criteria

- **Requirements:** `specs/requirements/SIM-29-gate0-central-ledger.md`
- **Design:** `specs/technical-design/SIM-29-gate0-central-ledger.md`

Each criterion is testable and maps to exactly one automated check, or is
marked **manual** with the reason.

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | embeddings routed to `gate0:<model>` and Gate0 returns `x-litellm-response-cost` | a batch is embedded | the case is billed that cost for the batch, not the per-token estimate | `tests/test_llm_gate0.py::test_embedding_bills_gateway_cost` |
| AC-2 | the embedding response has no cost header | a batch is embedded | the batch is billed at today's estimate (never 0) | `tests/test_llm_gate0.py::test_embedding_without_cost_header_falls_back_to_estimate` |
| AC-3 | a scope with gateway-priced and estimated embedding tokens | `service_price_for` runs | `embedPrice` = estimated tokens × rate + gateway cost; `embedTokens` counts all tokens | `tests/test_usage_ledger.py::test_embed_price_mixes_gateway_cost_and_estimate` |
| AC-4 | gateway embedding cost recorded in two scopes | `merge_usage` / `drain_for_emit` run | the new fields are summed and drained exactly once | `tests/test_usage_ledger.py::test_gateway_embed_cost_survives_merge_and_drain` |
| AC-5 | a chat call through Gate0 returning `x-litellm-call-id` | the router logs it | the `llm_call` line has `cost=` and `gateway_call_id=` matching the headers | `tests/test_llm_gate0.py::test_llm_call_line_carries_gateway_id_and_cost` |
| AC-6 | an embedding batch through Gate0 | it completes | exactly one `llm_call embed` line with `gateway_call_id` and `cost` | `tests/test_llm_gate0.py::test_embed_logs_one_llm_call_line` |
| AC-7 | a model with no `family` option whose name contains `gemini` | a request is built | parameters are identical to today's (thinking, safety, 16k floor, grounding) | existing `tests/test_llm_gate0.py::test_gemini_requests_carry_thinking_safety_and_attribution` (unchanged, must pass) |
| AC-8 | `LLM_MODEL_OPTIONS` sets `family: anthropic, thinking: true` | a request is built | `reasoning_effort` sent, no `temperature`, 8,192 floor, no Gemini parameters; in JSON mode no `reasoning_effort` | `tests/test_llm_gate0.py::test_anthropic_family_gets_reasoning_without_gemini_parameters` |
| AC-9 | a non-Gemini model (open-source or proprietary) with no options | a request is built | a plain request: `temperature`, no `reasoning_effort`, no Gemini parameters, no documents/grounding/thinking capability | `tests/test_llm_gate0.py::test_unconfigured_model_gets_a_plain_request` |
| AC-9b | an open-source model configured with `supports_documents`, `thinking`, `max_output_tokens`, `extra_body` | capabilities and a request are built | each option takes effect; the adapter's own `metadata` wins over `extra_body` | `tests/test_llm_gate0.py::test_open_source_model_is_configured_entirely_by_options` |
| AC-9c | `thinking: false` on an OpenAI reasoning-model name | a request is built | no `reasoning_effort`; `temperature` restored | `tests/test_llm_gate0.py::test_thinking_false_overrides_the_reasoning_model_name_rule` |
| AC-10 | a non-Gemini model | grounding is requested | no grounding capability unless `grounding: true`, which sends `web_search_options` and drops `response_format` | `tests/test_llm_gate0.py::test_grounding_only_where_configured` |
| AC-11 | a Cohere embedding group | a query and a document are embedded | `input_type` is `search_query` and `search_document` respectively | `tests/test_llm_gate0.py::test_cohere_input_type_through_gate0` |
| AC-12 | `GATE0_ENFORCE=true` and a selectable model Gate0 does not list | the client initialises | one ERROR line names the missing model; startup continues | `tests/test_llm_gate0.py::test_startup_reports_models_gate0_does_not_serve` |
| AC-13 | Gate0 `/v1/models` times out at boot | the client initialises | a warning is logged within the timeout and startup continues | `tests/test_llm_gate0.py::test_served_model_probe_failure_does_not_block_startup` |
| AC-14 | `GATE0_METER_ENABLED=false` | OCR/STT/search units are recorded | no usage record is built or sent; service billing unchanged | `tests/test_gate0_meter.py::test_disabled_meter_builds_nothing` |
| AC-15 | meter enabled, inside a workspace event | one OCR page is recorded | one record is queued with `usage_kind=ocr`, `units={ocr_page, 1}`, `cost_usd` from `service_price_for`, attribution from the logging context, null token fields | `tests/test_gate0_meter.py::test_ocr_unit_becomes_one_attributed_record` |
| AC-16 | meter enabled | model tokens or embedding tokens are recorded | no meter record (Gate0 already has them) | `tests/test_gate0_meter.py::test_model_and_embedding_usage_not_reported` |
| AC-17 | queued records and a healthy endpoint | the flusher runs | records are POSTed in batches of ≤100 with the Gate0 key; queue empties | `tests/test_gate0_meter.py::test_flush_batches_and_empties_queue` |
| AC-18 | the endpoint fails 3 times | the flusher runs | records go to the outbox file; a restart replays them; a duplicate `call_id` is sent once more, unchanged (Gate0 dedupes) | `tests/test_gate0_meter.py::test_failed_flush_goes_to_outbox_and_replays` |
| AC-19 | records queued | shutdown or task cancellation | the queue is written to the outbox before `CancelledError` is re-raised | `tests/test_gate0_meter.py::test_cancel_flushes_queue_to_outbox` |
| AC-23 | a call made inside a workspace event | the request is built (chat and embeddings) | no body or `metadata` key is on LiteLLM's ban list (`workspace_id` …); the case is attributed as `case_id` and tag `workspace_id:<id>` | `tests/test_llm_gate0.py::test_requests_never_carry_a_key_litellm_rejects` |
| AC-20 | Gate0 dev gateway with one model per family configured | the smoke script runs with `--embed` | every profile reports `ok`, valid JSON, a gateway price, and 768-dim embeddings | **manual**: live billed calls (`scripts/smoke_llm_providers.py`) |
| AC-21 | a test case run end to end through Gate0 | reconciliation runs | model and embedding cost per workspace match Gate0's within tolerance | **manual**: needs Gate0 spend logs (`scripts/reconcile_gate0_spend.py --by-feature`) |
| AC-22 | Gate0 part G deployed and meter enabled | a test case with an OCR'd PDF and an audio file is processed | Gate0's meter holds the OCR and STT records attributed to that `workspace_id` | **manual**: depends on the Gate0 change (part G) |

## Negative and edge cases
- **Duplicate re-send under the same `requestId`:** each paid unit is a fresh
  `call_id`. A re-run is real spend and is reported again, same as today's
  billing. Outbox replays reuse `call_id`, so Gate0 dedupes them (AC-18).
- **Stop mid-run:** the flusher is process-level, so a workspace Stop does not
  cancel it, and units already recorded are still reported (AC-17). Covered by
  the existing `verify_run_control.py`, which must pass.
- **Socket disconnect:** unaffected. The meter does not use the socket.
- **`view` collaborator:** n/a, no state mutation.
- **Model error / timeout / safety block:** no cost header means no gateway
  cost; the existing router behaviour is unchanged (existing tests in
  `tests/test_llm_router.py`).
- **Outside any workspace event:** the record's `attribution.workspace_id` is
  null. Same as today's unattributed warning path (`_warn_unscoped_once`).

## Non-functional
- No new log line contains payloads, document text, names or URLs
  (`tests/offline/verify_logging.py` passes).
- Meter records contain ids and quantities only; asserted in AC-15.
- Meter `report()` never blocks or raises in the request path (AC-15, AC-17).
- Cost of every path still appears in the emit `metadata`, with the same shape.

## Definition of done
- [ ] Every AC above has a passing check, or a signed-off manual result
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run (billing values change in every emit)
- [ ] `CHANGELOG.md` updated; emit cost value change called out
- [ ] PR labelled `needs-human-review` (billing + model routing)
- [ ] Gate0 part G tracked in the Gate0 repo; F stays off until it ships
