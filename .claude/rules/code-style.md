---
paths:
  - "app/**/*.py"
  - "scripts/**/*.py"
  - "tests/**/*.py"
---

# Python code style

Full detail: CLAUDE.md "Code conventions" and "Logging". Lint rules live in
`pyproject.toml` (`[tool.ruff]`); `make lint` and the pre-commit hook enforce
them, and `.claude/hooks/validate-code.sh` runs ruff after every edit.

- Python 3.11+, 4-space indent, line length 100.
- Type hints on public functions; short module docstrings.
- `snake_case` modules, functions and variables; `PascalCase` classes;
  `UPPER_CASE` module constants.
- Match the surrounding code's comment density and idiom; prefer small,
  focused modules over new utility grab-bags.
- Configuration only through `app/core/config.py` (pydantic-settings). Never
  hardcode secrets, URLs or model names elsewhere.
- Logging: `from loguru import logger`, `{}` placeholders, never f-strings,
  no emoji. Exceptions: one `logger.opt(exception=e).error(...)`.
- Never log payloads, prompts, model output, document text, names, phone
  numbers or URLs. Use `chars(x)` and `safe_url(url)` from
  `app/utils/redact.py`.
- Async: no blocking I/O on the event loop (OCR, rendering, sync SDKs go to a
  worker thread); catch `asyncio.CancelledError` before `except Exception`
  and re-raise it.
- Do not widen the ruff rule set and reformat the tree in the same change;
  one rule family per PR.
