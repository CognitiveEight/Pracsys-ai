---
paths:
  - "tests/**/*.py"
---

# Tests

- `make test` runs `pytest tests` plus every `tests/offline/verify_*.py`; a
  failing suite fails the build. `make lint` runs ruff.
- No real model, Qdrant, GCS or backend calls in tests. `make e2e` is the only
  billed run and is started by a human.
- A socket test double must return `True` from `emit`.
- Pin settings a check depends on (e.g. `settings.embedding_provider`) and
  restore them, so a developer's local `.env` cannot change the result.
- Fictional data only.
