"""
Distributed tracing and performance monitoring.
"""

import functools
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from loguru import logger

from app.core.config import settings


class Tracer:
    """
    Simple tracer for monitoring function execution and performance.
    """

    def __init__(self):
        self.enabled = settings.enable_trace
        self._active_traces: Dict[str, Dict[str, Any]] = {}

    def trace(self, operation_name: str):
        """
        Decorator to trace function execution.

        Args:
            operation_name: Name of the operation being traced
        """

        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                if not self.enabled:
                    return await func(*args, **kwargs)

                trace_id = f"{operation_name}_{int(time.time() * 1000)}"
                start_time = time.time()

                logger.debug("Starting trace: {} ({})", operation_name, trace_id)

                try:
                    result = await func(*args, **kwargs)
                    duration = time.time() - start_time

                    self._log_trace(
                        trace_id=trace_id,
                        operation_name=operation_name,
                        duration=duration,
                        status="success",
                    )

                    return result

                except Exception as e:
                    duration = time.time() - start_time

                    self._log_trace(
                        trace_id=trace_id,
                        operation_name=operation_name,
                        duration=duration,
                        status="error",
                        error=str(e),
                    )

                    raise

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                if not self.enabled:
                    return func(*args, **kwargs)

                trace_id = f"{operation_name}_{int(time.time() * 1000)}"
                start_time = time.time()

                logger.debug("Starting trace: {} ({})", operation_name, trace_id)

                try:
                    result = func(*args, **kwargs)
                    duration = time.time() - start_time

                    self._log_trace(
                        trace_id=trace_id,
                        operation_name=operation_name,
                        duration=duration,
                        status="success",
                    )

                    return result

                except Exception as e:
                    duration = time.time() - start_time

                    self._log_trace(
                        trace_id=trace_id,
                        operation_name=operation_name,
                        duration=duration,
                        status="error",
                        error=str(e),
                    )

                    raise

            # Return appropriate wrapper based on function type
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper

        return decorator

    @asynccontextmanager
    async def span(self, operation_name: str, **attributes):
        """
        Context manager for creating trace spans.

        Args:
            operation_name: Name of the operation
            **attributes: Additional span attributes
        """
        if not self.enabled:
            yield
            return

        trace_id = f"{operation_name}_{int(time.time() * 1000)}"
        start_time = time.time()

        logger.debug("Starting span: {} ({})", operation_name, trace_id)

        try:
            yield trace_id
            duration = time.time() - start_time

            self._log_trace(
                trace_id=trace_id,
                operation_name=operation_name,
                duration=duration,
                status="success",
                attributes=attributes,
            )

        except Exception as e:
            duration = time.time() - start_time

            self._log_trace(
                trace_id=trace_id,
                operation_name=operation_name,
                duration=duration,
                status="error",
                error=str(e),
                attributes=attributes,
            )

            raise

    def _log_trace(
        self,
        trace_id: str,
        operation_name: str,
        duration: float,
        status: str,
        error: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log trace information.

        Args:
            trace_id: Trace identifier
            operation_name: Name of the operation
            duration: Duration in seconds
            status: Trace status (success, error)
            error: Optional error message
            attributes: Optional trace attributes
        """
        trace_data = {
            "trace_id": trace_id,
            "operation": operation_name,
            "duration_ms": round(duration * 1000, 2),
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if error:
            trace_data["error"] = error

        if attributes:
            trace_data["attributes"] = attributes

        if status == "success":
            logger.info(
                "Trace completed: {} ({:.2f}ms)", operation_name, duration * 1000
            )
        else:
            logger.error(
                "Trace failed: {} ({:.2f}ms) - {}",
                operation_name,
                duration * 1000,
                error,
            )

        # Log structured trace data
        logger.bind(trace=True).debug(trace_data)


# Import asyncio for checking coroutine functions
import asyncio

# Global tracer instance
tracer = Tracer()
