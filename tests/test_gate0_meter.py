"""Gate0 usage meter (SIM-29): non-model paid units reported to gate0.

Offline: a fake gate0 provider records what would be POSTed. Fictional ids only.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from loguru import logger

from app.ai import gate0_meter
from app.ai import usage as usage_mod
from app.ai.usage import (
    record_embed_tokens,
    record_grounding_requests,
    record_ocr_units,
    record_search_queries,
    record_stt_seconds,
    record_usage,
)
from app.core.config import settings
from app.llm.errors import ProviderError

_SETTINGS = ("gate0_meter_enabled", "gate0_meter_outbox_dir", "gate0_meter_flush_seconds",
             "gate0_app_name")
_FORBIDDEN_KEYS = {"team", "key_alias", "record_id", "prompt", "text", "name", "url"}


class _FakeGate0:
    def __init__(self, failures=0, transient=True):
        self.batches: list = []
        self.failures = failures
        self.transient = transient

    def unavailable_reason(self):
        return None

    async def submit_usage_records(self, records):
        if self.failures:
            self.failures -= 1
            raise ProviderError("gate0 unavailable", transient=self.transient)
        self.batches.append(list(records))


@pytest.fixture(autouse=True)
def _meter(tmp_path, monkeypatch):
    saved = {k: getattr(settings, k) for k in _SETTINGS}
    settings.gate0_meter_enabled = True
    settings.gate0_meter_outbox_dir = str(tmp_path / "outbox")
    settings.gate0_app_name = "simplertoday"
    gate0_meter._take(None)
    gate0_meter._outbox_records = 0
    usage_mod._ledger.clear()
    fake = _FakeGate0()
    monkeypatch.setattr(gate0_meter, "_provider", fake)

    async def _no_wait(_seconds):
        await asyncio.sleep(0)  # yield, so a flusher loop never starves the event loop

    monkeypatch.setattr(gate0_meter, "_pause", _no_wait)
    yield fake
    gate0_meter._take(None)
    usage_mod._ledger.clear()
    for k, v in saved.items():
        setattr(settings, k, v)


def _queued() -> list:
    with gate0_meter._queue_lock:
        return list(gate0_meter._queue)


def test_disabled_meter_builds_nothing():
    settings.gate0_meter_enabled = False
    with logger.contextualize(workspace_id="ws-1"):
        record_ocr_units(3)
        record_stt_seconds(12.5)
    assert _queued() == []
    # Service billing is unchanged.
    assert usage_mod.pending_workspace_usage("ws-1").ocr_units == 3


def test_ocr_unit_becomes_one_attributed_record():
    with logger.contextualize(workspace_id="ws-1", request_id="req-1"):
        record_ocr_units(1)
    [record] = _queued()
    assert record["usage_kind"] == "ocr"
    assert record["units"] == {"kind": "ocr_page", "quantity": 1}
    assert record["attribution"]["workspace_id"] == "ws-1"
    assert record["attribution"]["request_id"] == "req-1"
    assert record["attribution"]["app"] == "simplertoday"
    assert record["schema_version"] == "1.1.0" and record["status"] == "success"
    assert record["prompt_tokens"] is None and record["completion_tokens"] is None
    assert record["cost_usd"] > 0
    # Ids and quantities only; identity is resolved by gate0 from the key.
    assert not (_FORBIDDEN_KEYS & set(record))
    json.dumps(record)  # serialisable as sent


def test_each_service_unit_kind_is_reported():
    with logger.contextualize(workspace_id="ws-1"):
        record_stt_seconds(30.5)
        record_search_queries(2)
        record_grounding_requests(1)
    kinds = {(r["usage_kind"], r["units"]["kind"], r["units"]["quantity"]) for r in _queued()}
    assert kinds == {("stt", "stt_second", 30.5), ("search", "search_query", 2),
                     ("grounding", "grounding_request", 1)}


def test_model_and_embedding_usage_not_reported():
    with logger.contextualize(workspace_id="ws-1"):
        record_usage(100, 50, model="m", cost=0.01)
        record_embed_tokens(500, 0.0001)
        record_embed_tokens(500)
    assert _queued() == []


def test_unit_outside_a_workspace_is_still_reported_unattributed():
    record_ocr_units(1)
    [record] = _queued()
    assert record["attribution"]["workspace_id"] is None


async def test_flush_batches_and_empties_queue(_meter):
    with logger.contextualize(workspace_id="ws-1"):
        for _ in range(250):
            record_ocr_units(1)
    assert await gate0_meter.flush() == 250
    assert [len(b) for b in _meter.batches] == [100, 100, 50]
    assert _queued() == []


async def test_failed_flush_goes_to_outbox_and_replays(_meter):
    _meter.failures = 3  # every attempt of the first batch fails
    with logger.contextualize(workspace_id="ws-1"):
        record_ocr_units(1)
        record_stt_seconds(5)
    sent_ids = {r["call_id"] for r in _queued()}
    assert await gate0_meter.flush() == 0
    assert _queued() == []
    outbox = gate0_meter._outbox_path()
    assert len(outbox.read_text().splitlines()) == 2

    # Next start: replayed with the same call ids (gate0 dedupes on them).
    assert gate0_meter._replay_outbox() == 2
    assert not outbox.exists()
    assert await gate0_meter.flush() == 2
    assert {r["call_id"] for r in _meter.batches[-1]} == sent_ids


async def test_rejected_batch_is_kept_not_retried(_meter):
    _meter.failures = 1
    _meter.transient = False  # e.g. 404 / 400 from gate0
    with logger.contextualize(workspace_id="ws-1"):
        record_ocr_units(1)
    assert await gate0_meter.flush() == 0
    assert _meter.failures == 0  # one attempt only
    assert len(gate0_meter._outbox_path().read_text().splitlines()) == 1


async def test_cancel_flushes_queue_to_outbox(monkeypatch):
    settings.gate0_meter_flush_seconds = 3600

    async def _block_forever(_seconds):
        await asyncio.Event().wait()

    monkeypatch.setattr(gate0_meter, "_pause", _block_forever)
    await gate0_meter.start()
    try:
        await asyncio.sleep(0)
        with logger.contextualize(workspace_id="ws-1"):
            record_ocr_units(1)
            record_ocr_units(1)
    finally:
        await gate0_meter.stop()
    assert _queued() == []
    assert len(gate0_meter._outbox_path().read_text().splitlines()) == 2
