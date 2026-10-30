"""
Gate0 usage meter: reports the paid units that never pass through gate0.

Model calls and embeddings already go through gate0, which meters them itself.
Vision OCR, Speech-to-Text, Vertex AI Search and (unpriced) Google Search
grounding do not, so without this gate0 sees only part of a case's bill. Each
such unit, as it is recorded in ``app/ai/usage.py``, becomes one gate0 usage
record (schema 1.1.0: the frozen 1.0.0 fields plus ``usage_kind``, ``units``
and ``attribution``). The record is queued here and sent in batches to gate0's
usage-record endpoint.

Rules:
- ``report`` never blocks and never raises. It runs on worker threads (OCR,
  STT) as well as the event loop, so the queue is lock-guarded.
- Ids and quantities only. ``attribution`` holds the app, workspace id,
  feature and request id from the logging context, exactly what the gate0
  adapter already sends with every model call. Never names, text or URLs.
- ``team``, ``key_alias`` and ``record_id`` are not sent. gate0 resolves the
  caller from its key (server-resolved identity) and derives ``record_id``
  from ``call_id``, so a re-sent record is deduplicated.
- Nothing is lost while gate0 is down. A batch gate0 does not accept goes to
  an NDJSON outbox, re-sent on the next start; on shutdown the queue is
  written there too.
- The service's own billing is unchanged. These units are still priced into
  the case's emit ``metadata`` by the usage ledger; this only makes them
  visible in gate0.

Off unless ``GATE0_METER_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings

SCHEMA_VERSION = "1.1.0"
_BATCH_SIZE = 100
_SEND_ATTEMPTS = 3
_OUTBOX_FILE = "outbox.ndjson"
_OUTBOX_WARN_RECORDS = 10_000

# usage.py recorder label -> (usage_kind, unit kind, service label, service_price_for kwarg)
_KINDS: Dict[str, tuple] = {
    "OCR": ("ocr", "ocr_page", "google-vision", "ocr_units"),
    "STT": ("stt", "stt_second", "google-speech-v2", "stt_seconds"),
    "Vertex AI Search": ("search", "search_query", "vertex-ai-search", "search_queries"),
    "Google Search grounding": (
        "grounding", "grounding_request", "google-search-grounding", "grounding_requests",
    ),
}

_queue: List[Dict[str, Any]] = []
_queue_lock = threading.Lock()
_task: Optional[asyncio.Task] = None
_provider = None
_outbox_records = 0
# Indirection so tests can shorten the waits without patching asyncio itself.
_pause = asyncio.sleep


def enabled() -> bool:
    return bool(getattr(settings, "gate0_meter_enabled", False))


def metered_kind(label: str) -> bool:
    """Whether a usage.py recorder label is reported to gate0."""
    return label in _KINDS


# -- building records ------------------------------------------------------------------
def _log_context() -> Dict[str, Any]:
    try:
        import loguru._logger as _loguru

        return dict(_loguru.context.get() or {})
    except Exception:  # internal API; attribution is best-effort
        return {}


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if text and text not in ("-", "unknown", "None") else None


def build_record(label: str, quantity: float) -> Optional[Dict[str, Any]]:
    """One usage record for ``quantity`` units of ``label``; None if not metered."""
    spec = _KINDS.get(label)
    if spec is None:
        return None
    usage_kind, unit_kind, service, price_arg = spec
    from app.ai.pricing import service_price_for
    from app.ai.usage import current_feature

    qty: float = float(quantity) if usage_kind == "stt" else int(quantity)
    cost = service_price_for(**{price_arg: qty})["servicePrice"]
    ctx = _log_context()
    return {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "call_id": uuid.uuid4().hex,
        "task_class": None,
        "task_source": None,
        "classifier_confidence": None,
        "requested_model": service,
        "routed_provider": "google",
        "routed_model": service,
        "status": "success",
        "error_code": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
        "cost_usd": cost,
        "latency_ms": None,
        "stream": False,
        "fallback_depth": None,
        "start_time": None,
        "end_time": None,
        "usage_kind": usage_kind,
        "units": {"kind": unit_kind, "quantity": qty},
        "attribution": {
            "app": _clean(getattr(settings, "gate0_app_name", None)),
            "workspace_id": _clean(ctx.get("workspace_id")),
            "feature": _clean(current_feature()),
            "request_id": _clean(ctx.get("request_id")),
        },
    }


def report(label: str, quantity: float) -> None:
    """Queue one record for a paid unit just recorded. Never raises."""
    if not enabled():
        return
    try:
        if not quantity or quantity <= 0:
            return
        record = build_record(label, quantity)
        if record is None:
            return
        with _queue_lock:
            _queue.append(record)
    except Exception as exc:  # metering must never break the work it measures
        logger.debug("gate0 meter report failed ({}): {}", label, exc)


def pending() -> int:
    with _queue_lock:
        return len(_queue)


def _take(limit: Optional[int]) -> List[Dict[str, Any]]:
    with _queue_lock:
        if limit is None:
            batch = list(_queue)
            _queue.clear()
        else:
            batch = _queue[:limit]
            del _queue[:limit]
    return batch


# -- delivery --------------------------------------------------------------------------
def _get_provider():
    global _provider
    if _provider is None:
        from app.llm.providers.gate0 import Gate0Provider

        _provider = Gate0Provider()
    return _provider


async def _send(batch: List[Dict[str, Any]]) -> bool:
    """POST one batch, retrying transient failures. False: keep it for later."""
    from app.llm.errors import ProviderError

    for attempt in range(1, _SEND_ATTEMPTS + 1):
        try:
            await _get_provider().submit_usage_records(batch)
            return True
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if not exc.transient:
                logger.error(
                    "gate0 meter rejected {} usage record(s); kept in the outbox: {}",
                    len(batch), exc,
                )
                return False
            if attempt < _SEND_ATTEMPTS:
                await _pause(min(8.0, 1.0 * (2 ** (attempt - 1))))
                continue
            logger.warning(
                "gate0 meter unreachable after {} attempts; {} record(s) kept in the outbox: {}",
                _SEND_ATTEMPTS, len(batch), exc,
            )
        except Exception as exc:
            logger.opt(exception=exc).error("gate0 meter send failed")
            return False
    return False


async def flush() -> int:
    """Send everything queued; what gate0 does not take goes to the outbox.
    Returns the number of records gate0 accepted."""
    sent = 0
    batch = _take(_BATCH_SIZE)
    while batch:
        if not await _send(batch):
            _write_outbox(batch + _take(None))
            break
        sent += len(batch)
        batch = _take(_BATCH_SIZE)
    if sent:
        logger.info("gate0 meter sent {} usage record(s)", sent)
    return sent


def _outbox_path() -> Path:
    return Path(getattr(settings, "gate0_meter_outbox_dir", "data/gate0_meter_outbox")) / _OUTBOX_FILE


def _write_outbox(records: List[Dict[str, Any]]) -> None:
    global _outbox_records
    if not records:
        return
    path = _outbox_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        _outbox_records += len(records)
        log = logger.warning if _outbox_records >= _OUTBOX_WARN_RECORDS else logger.info
        log("gate0 meter outbox holds {} record(s)", _outbox_records)
    except Exception as exc:
        # Last resort: the records stay in the service's own billing either way.
        logger.opt(exception=exc).error(
            "gate0 meter could not write {} record(s) to the outbox", len(records)
        )


def _replay_outbox() -> int:
    """Move outbox records back onto the queue (they are re-sent; gate0 dedupes)."""
    global _outbox_records
    path = _outbox_path()
    if not path.exists():
        return 0
    replay = path.with_name(f"replay-{uuid.uuid4().hex}.ndjson")
    try:
        path.rename(replay)
    except OSError as exc:
        logger.warning("gate0 meter could not open its outbox: {}", exc)
        return 0
    records: List[Dict[str, Any]] = []
    bad = 0
    with replay.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    with _queue_lock:
        _queue[:0] = records
    replay.unlink(missing_ok=True)
    _outbox_records = 0
    if records or bad:
        logger.info(
            "gate0 meter re-queued {} record(s) from its outbox ({} unreadable)", len(records), bad
        )
    return len(records)


# -- lifecycle -------------------------------------------------------------------------
async def _run() -> None:
    interval = max(1.0, float(getattr(settings, "gate0_meter_flush_seconds", 10.0) or 10.0))
    try:
        _replay_outbox()
        while True:
            await flush()
            await _pause(interval)
    except asyncio.CancelledError:
        # Shutdown: no network on the way out; the next start re-sends.
        _write_outbox(_take(None))
        raise


async def start() -> None:
    """Start the background flusher (no-op unless GATE0_METER_ENABLED)."""
    global _task
    if not enabled() or _task is not None:
        return
    reason = _get_provider().unavailable_reason()
    if reason:
        logger.error("gate0 meter enabled but gate0 is not configured: {}", reason)
        return
    _task = asyncio.create_task(_run(), name="gate0-meter")
    logger.info("gate0 usage meter started")


async def stop() -> None:
    """Stop the flusher; anything still queued is kept in the outbox."""
    global _task
    task, _task = _task, None
    if task is None:
        _write_outbox(_take(None))
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
