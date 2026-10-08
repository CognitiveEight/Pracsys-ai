"""
Failure conventions shared by every model provider.

``generate()`` reports failure by RETURNING one of the sentinel strings below
rather than raising, so that a caller expecting prose still gets something
printable. The cost is that a 429 / 5xx / safety block is indistinguishable
from real content unless the caller checks — a structured (JSON-mode) caller
then just fails to parse it and reports "no JSON", hiding the actual cause.
Every caller that branches on failure must use :func:`is_generation_error`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

from app.core.config import settings

GENERATION_ERROR_PREFIXES = (
    "Error generating response:",
    "Error: Gemini model not available",
    "Error: model not available",
    "Error generating long-form response:",
    "Error in chat:",
)


def is_generation_error(text: Optional[str]) -> bool:
    """True when ``text`` is a generate*() failure sentinel rather than model output."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in GENERATION_ERROR_PREFIXES)


class EmbeddingError(RuntimeError):
    """An embedding could not be produced. Callers decide the fallback."""


class ProviderError(RuntimeError):
    """A provider call failed. ``transient`` marks 429 / 5xx / network errors,
    which the router may retry on the same profile before falling back."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class ProviderNotConfigured(RuntimeError):
    """A routed provider is missing its credentials or SDK."""


def request_timeout_seconds() -> Optional[float]:
    """Configured per-request ceiling, or None when disabled (<= 0)."""
    try:
        value = float(getattr(settings, "gemini_request_timeout_seconds", 300.0) or 0.0)
    except (TypeError, ValueError):
        return 300.0
    return value if value > 0 else None


async def with_timeout(coro: Any, *, label: str) -> Any:
    """Await ``coro`` under the configured ceiling.

    A stalled generation is the worst failure mode a model client has: the
    long feature generations are awaited inline by socket handlers, so an
    unbounded request hangs the whole turn with no output and no error. Raises
    asyncio.TimeoutError so the caller's failure path runs.
    """
    timeout = request_timeout_seconds()
    if timeout is None:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Model request timed out after {:.0f}s ({})", timeout, label)
        raise


def reraise_if_cancelled() -> None:
    """Re-raise a cancellation that arrived disguised as an ordinary error.

    Called first in every ``except Exception`` around a model call. A stop
    (``workspace_stop_request``) or shutdown cancels the task, but an SDK can
    surface that as another exception (google-genai raised ``AttributeError``
    against aiohttp 3.9). Caught as a plain failure, the cancellation was lost
    and a stopped pipeline kept running on the fallback path.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


def is_transient_status(status: Optional[int]) -> bool:
    """HTTP statuses worth one more attempt on the same profile."""
    return status in (408, 409, 429) or (status is not None and 500 <= status < 600)
