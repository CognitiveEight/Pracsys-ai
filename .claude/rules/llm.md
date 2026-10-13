---
paths:
  - "app/llm/**/*.py"
  - "app/ai/**/*.py"
  - "app/langgraph/**/*.py"
  - "app/prompts/**/*.py"
---

# Model calls, cost and prompts

Full detail: CLAUDE.md "Model calls are provider-neutral", "Cost tracking
covers more than tokens", "Indian-law grounding"; `docs/MODEL_PORTABILITY.md`.

- Provider SDKs are imported only in `app/llm/providers/`
  (`tests/test_llm_router.py` enforces it).
- Every call goes through `gemini_client` / the router; check
  `is_generation_error()` on every `generate` result.
- A `blocked` result is final; never retry it on another provider.
- Sync code in a worker thread uses `gemini_client.generate_sync`.
- Meter paid calls once, at the lowest call site (`record_*`), inside the
  event's context so the cost lands on the right workspace.
- An embedding provider/model/dimension change means a new collection and a
  re-index.
- Prompts: Indian law (BNS/BNSS/BSA/IT Act); no IPC rows in
  `applicable_laws`. Chat stays neutral; role shapes panels, not chat.
- Participants (app users) never appear in legal prompts.
