"""Per-case cost ledger: every paid unit reaches its workspace exactly once.

The regression this guards: knowledge-graph ingestion ran in its own
@meter_usage scope that no emit ever reported, so its model spend reached
gate0/LiteLLM but never the case total (case showed $0.24, gateway $0.30).
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from app.ai import usage as usage_mod
from app.ai.usage import (
    capture_usage,
    drain_for_emit,
    forget_workspace_usage,
    llm_price,
    meter_usage,
    pending_workspace_usage,
    record_ocr_units,
    record_usage,
)


@pytest.fixture(autouse=True)
def _clean_ledger():
    usage_mod._ledger.clear()
    yield
    usage_mod._ledger.clear()


def test_spend_is_charged_to_the_events_workspace():
    with logger.contextualize(workspace_id="ws-1"):
        with capture_usage():
            record_usage(1000, 500, model="m", cost=0.25)
    pending = pending_workspace_usage("ws-1")
    assert pending is not None and llm_price(pending)["price"] == pytest.approx(0.25)


def test_each_charge_is_reported_by_exactly_one_emit():
    with logger.contextualize(workspace_id="ws-1"):
        with capture_usage() as scope:
            record_usage(10, 5, model="m", cost=0.1)
            first = drain_for_emit("ws-1", scope)
            second = drain_for_emit("ws-1", scope)  # e.g. the error-path emit
    assert llm_price(first)["price"] == pytest.approx(0.1)
    assert llm_price(second)["price"] == 0 and second.input_tokens == 0


async def test_background_work_without_an_emit_reaches_the_next_emit():
    @meter_usage
    async def ingest_to_knowledge_plane():
        record_usage(100, 50, model="m", cost=0.0621)

    with logger.contextualize(workspace_id="ws-1"):
        await ingest_to_knowledge_plane()  # own scope; nothing emits it
        with capture_usage() as panel:
            record_usage(100, 50, model="m", cost=0.2368)
            report = drain_for_emit("ws-1", panel)
    assert llm_price(report)["price"] == pytest.approx(0.2989)


def test_spend_without_a_scope_is_no_longer_billed_to_nobody():
    with logger.contextualize(workspace_id="ws-1"):
        record_usage(10, 5, model="m", cost=0.02)
        record_ocr_units(3)
    report = drain_for_emit("ws-1")
    assert llm_price(report)["price"] == pytest.approx(0.02) and report.ocr_units == 3


def test_scope_spend_with_no_workspace_is_reported_by_its_own_emit():
    with capture_usage() as scope:  # no workspace bound (e.g. startup recovery)
        record_usage(10, 5, model="m", cost=0.03)
    report = drain_for_emit("ws-1", scope)
    assert llm_price(report)["price"] == pytest.approx(0.03)
    assert llm_price(drain_for_emit("ws-1", scope))["price"] == 0


def test_workspaces_are_kept_apart():
    with logger.contextualize(workspace_id="ws-a"):
        record_usage(10, 5, model="m", cost=0.1)
    with logger.contextualize(workspace_id="ws-b"):
        record_usage(10, 5, model="m", cost=0.2)
    assert llm_price(drain_for_emit("ws-a"))["price"] == pytest.approx(0.1)
    assert llm_price(drain_for_emit("ws-b"))["price"] == pytest.approx(0.2)


async def test_worker_threads_charge_the_workspace_that_started_them():
    with logger.contextualize(workspace_id="ws-1"):
        with capture_usage():
            await asyncio.gather(*(asyncio.to_thread(record_ocr_units, 1) for _ in range(8)))
    assert drain_for_emit("ws-1").ocr_units == 8


def test_purge_forgets_unreported_spend():
    with logger.contextualize(workspace_id="ws-1"):
        record_usage(10, 5, model="m", cost=0.1)
    forget_workspace_usage("ws-1")
    assert pending_workspace_usage("ws-1") is None


def test_emit_metadata_reports_and_totals_the_drained_spend(monkeypatch):
    from app.socket.handlers.chat import ChatMixin
    from app.workspace import context as context_mod

    added = []

    class _Ctx:
        def __init__(self):
            self.totals = {"totalPrice": 0.0}

        def add_token_usage(self, in_t, out_t, price, service=None, service_units=None):
            added.append(price["price"] + (service or {}).get("servicePrice", 0.0))
            self.totals["totalPrice"] += added[-1]
            return dict(self.totals)

        def get_metadata(self, key):
            return dict(self.totals)

    ctx = _Ctx()
    monkeypatch.setattr(context_mod.context_manager, "get_context", lambda ws: ctx)
    mixin = ChatMixin()
    with logger.contextualize(workspace_id="ws-1"):
        with capture_usage() as ingest:
            record_usage(100, 50, model="m", cost=0.0621)  # never emitted itself
        with capture_usage() as panel:
            record_usage(100, 50, model="m", cost=0.2368)
            first = mixin._token_usage_metadata("ws-1", panel)
            again = mixin._token_usage_metadata("ws-1", panel)
    assert first["price"] == pytest.approx(0.2989) and first["totalPrice"] == pytest.approx(0.2989)
    assert again["price"] == 0 and again["totalPrice"] == pytest.approx(0.2989)
    assert added == [pytest.approx(0.2989)]  # cumulative incremented once
    assert ingest is not None


# -- SIM-29: embeddings billed at gate0's own figure -----------------------------------


def test_embed_price_mixes_gateway_cost_and_estimate(monkeypatch):
    from app.ai.pricing import service_price_for
    from app.core.config import settings

    monkeypatch.setattr(settings, "embed_price_per_million_tokens", 1.0)
    # 3,000 tokens: 1,000 gate0-priced at $0.5, 2,000 estimated at $1/1M.
    price = service_price_for(embed_tokens=3000, embed_gateway_tokens=1000, embed_gateway_cost=0.5)
    assert price["embedPrice"] == pytest.approx(0.5 + 2000 / 1_000_000)
    # Gateway tokens can never exceed the total (no negative estimate).
    assert service_price_for(embed_tokens=10, embed_gateway_tokens=50, embed_gateway_cost=0.1)[
        "embedPrice"] == pytest.approx(0.1)


def test_gateway_embed_cost_survives_merge_and_drain():
    from app.ai.usage import record_embed_tokens

    with logger.contextualize(workspace_id="ws-embed"):
        with capture_usage():
            record_embed_tokens(100, 0.002)
            record_embed_tokens(40)  # estimate, no gateway cost
        with capture_usage():
            record_embed_tokens(60, 0.003)
    drained = drain_for_emit("ws-embed")
    assert drained.embed_tokens == 200
    assert drained.embed_gateway_tokens == 160
    assert drained.embed_gateway_cost == pytest.approx(0.005)
    # Drained exactly once.
    assert drain_for_emit("ws-embed").embed_tokens == 0
