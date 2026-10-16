"""
Operation timing.

``@tracer.trace("name")`` times a function and writes one structured record
(``trace=True``) to ``logs/structured.log``: operation, duration, status, a
unique ``trace_id`` and the parent operation's id, plus the request context
(request_id / event / workspace_id) bound for the inbound event. Text logs get
a DEBUG line only, so timings never drown the readable log.

The current trace id lives in a contextvar, so nested traced calls record their
parent and child tasks inherit it.
"""

import asyncio
import contextvars
import functools
import time
import uuid
from typing import Any, Callable, Optional

from loguru import logger

from app.core.config import settings

_current_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "simplertoday_trace_id", default=None
)


def current_trace_id() -> Optional[str]:
    return _current_trace.get()


class Tracer:
    """Times decorated functions; no-op when ENABLE_TRACE is false."""

    def __init__(self):
        self.enabled = settings.enable_trace

    def trace(self, operation_name: str):
        def decorator(func: Callable) -> Callable:
            if asyncio.iscoroutinefunction(func):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs):
                    if not self.enabled:
                        return await func(*args, **kwargs)
                    trace_id, parent, token = self._start()
                    start = time.perf_counter()
                    try:
                        result = await func(*args, **kwargs)
                    except BaseException as exc:
                        self._finish(operation_name, trace_id, parent, start, exc)
                        raise
                    finally:
                        _current_trace.reset(token)
                    self._finish(operation_name, trace_id, parent, start, None)
                    return result

                return async_wrapper

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                if not self.enabled:
                    return func(*args, **kwargs)
                trace_id, parent, token = self._start()
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                except BaseException as exc:
                    self._finish(operation_name, trace_id, parent, start, exc)
                    raise
                finally:
                    _current_trace.reset(token)
                self._finish(operation_name, trace_id, parent, start, None)
                return result

            return sync_wrapper

        return decorator

    @staticmethod
    def _start():
        trace_id = uuid.uuid4().hex[:16]
        parent = _current_trace.get()
        token = _current_trace.set(trace_id)
        return trace_id, parent, token

    @staticmethod
    def _finish(operation, trace_id, parent, start, exc: Optional[BaseException]) -> None:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if exc is None:
            status = "ok"
        elif isinstance(exc, asyncio.CancelledError):
            status = "cancelled"
        else:
            status = "error"
        record: dict[str, Any] = {
            "trace_id": trace_id,
            "parent_trace_id": parent,
            "operation": operation,
            "duration_ms": duration_ms,
            "status": status,
        }
        if exc is not None and status == "error":
            record["error"] = type(exc).__name__
        logger.bind(trace=True, **record).info("trace {}", operation)
        logger.debug("{} {} in {}ms", operation, status, duration_ms)


tracer = Tracer()
