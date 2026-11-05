"""
Provider-neutral model layer.

Feature code never imports this package or any provider SDK: it calls
``app.ai.gemini_client.gemini_client`` (the facade), which routes through
:class:`app.llm.router.ModelRouter` to the adapter in ``app/llm/providers/``
chosen by the LLM_* settings. See docs/MODEL_PORTABILITY.md.
"""

from app.llm.errors import (
    GENERATION_ERROR_PREFIXES,
    EmbeddingError,
    is_generation_error,
)

__all__ = ["GENERATION_ERROR_PREFIXES", "EmbeddingError", "is_generation_error"]
