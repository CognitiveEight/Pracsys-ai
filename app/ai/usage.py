"""
Per-turn token usage accumulation.

A single user turn fans out into many ``GeminiClient.generate*`` calls (the
main answer, suggestion chips, strategy refresh, …), and ``gemini_client`` is a
module-level singleton shared across concurrent requests. To attribute token
usage to the right turn without threading a counter through every call site, we
use a :class:`contextvars.ContextVar`: each turn opens a :func:`capture_usage`
scope, every generate call inside it reports via :func:`record_usage`, and the
scope reads the aggregated totals at the end.

``contextvars`` are copied per ``asyncio`` task, so two workspaces generating
concurrently never see each other's counts.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

from loguru import logger


@dataclass
class UsageAccumulator:
    """Running token totals for one captured scope (one user turn)."""

    input_tokens: int = 0
    output_tokens: int = 0
    # Set once the turn's totals have been billed into the workspace cumulative,
    # so a second emit in the same turn (e.g. an error path after the main
    # response) does not double-count the same tokens.
    counted: bool = False

    def add(self, prompt_tokens: int, output_tokens: int) -> None:
        """Add one generation's tokens. Negative/None values are treated as 0."""
        self.input_tokens += max(int(prompt_tokens or 0), 0)
        self.output_tokens += max(int(output_tokens or 0), 0)


# The active accumulator for the current async task, or None when no scope is
# open (in which case record_usage is a harmless no-op).
_current_usage: ContextVar[Optional[UsageAccumulator]] = ContextVar(
    "current_usage", default=None
)


@contextlib.contextmanager
def capture_usage() -> Iterator[UsageAccumulator]:
    """Open a usage-capture scope and yield its accumulator.

    All :func:`record_usage` calls made (directly or transitively) within the
    ``with`` block add to the yielded accumulator. The previous scope, if any,
    is restored on exit so nested scopes do not leak.
    """
    accumulator = UsageAccumulator()
    token = _current_usage.set(accumulator)
    try:
        yield accumulator
    finally:
        _current_usage.reset(token)


def current_usage() -> Optional[UsageAccumulator]:
    """Return the accumulator for the active scope, or None if none is open."""
    return _current_usage.get()


def begin_usage() -> "tuple[object, UsageAccumulator]":
    """Open a usage-capture scope imperatively (for try/finally call sites).

    Returns ``(token, accumulator)``. The caller MUST pass ``token`` to
    :func:`reset_usage` in a ``finally`` block to restore the previous scope.
    Prefer the :func:`capture_usage` context manager where a ``with`` block
    fits; use this only when re-indenting the whole body is impractical.
    """
    accumulator = UsageAccumulator()
    token = _current_usage.set(accumulator)
    return token, accumulator


def reset_usage(token: object) -> None:
    """Restore the scope captured before a matching :func:`begin_usage`."""
    try:
        _current_usage.reset(token)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("reset_usage failed: {}", exc)


def record_usage(prompt_tokens: int, output_tokens: int) -> None:
    """Report one generation's token usage to the active scope, if any.

    Safe to call from anywhere: when no :func:`capture_usage` scope is open
    this does nothing. ``output_tokens`` should already include thinking
    tokens (Gemini bills them as output).
    """
    accumulator = _current_usage.get()
    if accumulator is None:
        return
    try:
        accumulator.add(prompt_tokens, output_tokens)
    except Exception as exc:  # usage tracking must never break generation
        logger.debug("record_usage failed: {}", exc)
