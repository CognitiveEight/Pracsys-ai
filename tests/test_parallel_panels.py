"""Upload pipeline with concurrent panels (SIM-163 Phase 1, steps 2, 3, 14).

Strategy -> analysis -> drafting is the priority lane; dates, laws, timeline,
parties and case laws generate alongside it and are sent only after it. The
real emit chokepoint runs (the hold lives there); generators, the socket and
persistence are fakes. Fictional data only; no model, Qdrant or socket calls.
"""

import asyncio
import uuid

import pytest

from app.core.config import settings
from app.socket.event_utils import gather_bounded
from app.socket.handlers.extraction import document_set_key

PRIORITY = ["strategy_objective", "analysis_report", "drafting_assistant"]
TAIL = ["list_of_dates", "applicable_laws", "procedural_timeline",
        "relevant_parties", "case_laws"]
TAIL_GENERATORS = {
    "list_of_dates": "_generate_and_emit_list_of_dates",
    "applicable_laws": "_generate_and_emit_applicable_laws",
    "procedural_timeline": "_generate_and_emit_procedural_timeline",
    "relevant_parties": "_generate_and_emit_relevant_parties",
    "case_laws": "_generate_and_emit_case_laws",
    "drafting_assistant": "_generate_and_emit_drafting_suggestions",
}
OWNER = "user-fictional"


class _Socket:
    def __init__(self, sent):
        self.sent = sent

    async def emit(self, event, payload):  # a test double reports delivery
        if event == "document_feature_response":
            self.sent.append(payload.get("featureType"))
        return True


class _Run:
    """Scripted generators plus what they saw."""

    def __init__(self):
        self.sent = []
        self.started = {}
        self.finished = {}
        self.cancelled = []
        self.delay = {f: 0.01 for f in TAIL + PRIORITY}
        self.fail = set()
        self.block = {}


@pytest.fixture
def run(monkeypatch):
    from app.socket.events import event_handler as h
    from app.workspace import persistence

    r = _Run()
    monkeypatch.setattr(persistence, "persist_state", lambda **_kw: None)
    monkeypatch.setattr(settings, "citations_enabled", False)
    monkeypatch.setattr(settings, "pipeline_concurrent_panels", True)
    monkeypatch.setattr(h, "_socket_client", _Socket(r.sent))

    async def _work(feature):
        r.started[feature] = asyncio.get_running_loop().time()
        try:
            if feature in r.block:
                await r.block[feature].wait()
            await asyncio.sleep(r.delay[feature])
        except asyncio.CancelledError:
            r.cancelled.append(feature)
            raise
        r.finished[feature] = asyncio.get_running_loop().time()
        if feature in r.fail:
            raise RuntimeError(f"{feature} failed (fictional)")

    def _gen(feature):
        async def _generate(workspace_id, user_id, workspace_role=None, **_kw):
            await _work(feature)
            await h._emit_document_feature(
                feature_type=feature, workspace_id=workspace_id, user_id=user_id,
                payload={"note": "fresh"}, usage=None,
            )
        return _generate

    async def _strategy(**_kw):
        await _work("strategy_objective")
        return {"caseSummary": "A fictional booking dispute.",
                "strategicObjective": "Recover the deposit.", "suggestionChips": []}

    async def _analysis(**_kw):
        await _work("analysis_report")
        return {"missingInfo": [], "caseResolutionScore": 50}

    async def _resolve(workspace_id, user_id, preferred_role=None, **_kw):
        return "PETITIONER"

    async def _tag(workspace_id, report, *, log_context=""):
        return report

    async def _chips(**_kw):
        return []

    for feature, name in TAIL_GENERATORS.items():
        monkeypatch.setattr(h, name, _gen(feature))
    monkeypatch.setattr(h, "_generate_strategies_from_documents", _strategy)
    monkeypatch.setattr(h, "_generate_analysis_report_from_documents", _analysis)
    monkeypatch.setattr(h, "_resolve_workspace_role", _resolve)
    monkeypatch.setattr(h, "_tag_analysis_report_against_current_state", _tag)
    monkeypatch.setattr(h, "_generate_analysis_report_suggestion_chips", _chips)
    r.h = h
    r.ws = f"ws-parallel-{uuid.uuid4().hex[:8]}"
    yield r
    for cache in (h._workspace_strategy_payload, h._workspace_analysis_payload,
                  h._pending_reemits):
        cache.pop(r.ws, None)


def _pipeline(r, *, is_update):
    return r.h._run_strategy_analysis_timeline_pipeline(
        workspace_id=r.ws, user_id=OWNER, workspace_role="PETITIONER",
        batch_result={"results": []}, is_update=is_update,
        analysis_log_context=" (test)",
    )


def test_priority_three_emit_first(run):
    run.delay["strategy_objective"] = 0.2  # every tail panel is ready long before
    asyncio.run(_pipeline(run, is_update=True))
    assert run.sent[:3] == PRIORITY
    assert sorted(run.sent[3:]) == sorted(TAIL)


def test_tail_overlaps_strategy(run):
    run.delay["strategy_objective"] = 0.2
    asyncio.run(_pipeline(run, is_update=True))
    strategy_done = run.finished["strategy_objective"]
    assert all(run.finished[f] < strategy_done for f in TAIL)


def test_sequential_flag(run, monkeypatch):
    monkeypatch.setattr(settings, "pipeline_concurrent_panels", False)
    asyncio.run(_pipeline(run, is_update=False))
    assert run.sent == ["strategy_objective", "analysis_report", *TAIL, "drafting_assistant"]
    assert run.started["list_of_dates"] > run.finished["analysis_report"]


def test_first_build_refreshes_drafting(run):
    asyncio.run(_pipeline(run, is_update=False))
    assert run.sent[:3] == PRIORITY
    assert run.sent[-1] == "drafting_assistant"
    assert run.sent.count("drafting_assistant") == 2
    assert set(run.sent[3:-1]) == set(TAIL)


def test_update_drafts_once(run):
    asyncio.run(_pipeline(run, is_update=True))
    assert run.sent.count("drafting_assistant") == 1


def test_failed_tail_panel_settles(run):
    run.fail.add("applicable_laws")
    asyncio.run(_pipeline(run, is_update=True))
    assert run.sent[:3] == PRIORITY
    # The failed panel still gets its one terminal emit (from cache, or empty),
    # and no other panel is cancelled by the failure.
    assert sorted(run.sent[3:]) == sorted(TAIL)
    assert run.cancelled == []


def test_lane_failure_opens_hold(run, monkeypatch):
    async def _broken(**_kw):
        raise RuntimeError("analysis emit failed (fictional)")

    monkeypatch.setattr(run.h, "_emit_analysis_report", _broken)
    asyncio.run(asyncio.wait_for(_pipeline(run, is_update=True), timeout=5))
    assert run.sent[0] == "strategy_objective"
    assert sorted(run.sent[1:]) == sorted(TAIL)


def test_cancel_cancels_children(run):
    run.block["case_laws"] = asyncio.Event()  # never set
    run.delay["strategy_objective"] = 10

    async def _go():
        task = asyncio.create_task(_pipeline(run, is_update=True))
        while "case_laws" not in run.started or "strategy_objective" not in run.started:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(_go(), timeout=5))
    assert "strategy_objective" in run.cancelled
    assert "case_laws" in run.cancelled
    # Panels that finished generating were still held: nothing reached the wire.
    assert run.sent == []


# -- entity cache keyed by the document set -------------------------------------


def test_entity_cache_document_set(monkeypatch):
    from app.socket.events import event_handler as h

    calls = []

    def _sub(name):
        async def _run(workspace_id, content):
            calls.append(name)
            return {}
        return _run

    monkeypatch.setattr(h, "_extract_parties_subagent", _sub("parties"))
    monkeypatch.setattr(h, "_extract_timeline_subagent", _sub("timeline"))
    monkeypatch.setattr(h, "_extract_assets_and_law_subagent", _sub("assets"))
    ws = f"ws-entities-{uuid.uuid4().hex[:8]}"
    docs = [{"document_id": "doc-1", "content": "A fictional booking email."},
            {"document_id": "doc-2", "content": "A fictional refund request."}]

    async def _go():
        # Strategy and analysis pack the same documents differently.
        await h._extract_case_entities(ws, "packed for strategy" * 50,
                                       cache_key=document_set_key(docs))
        await h._extract_case_entities(ws, "packed for analysis" * 50,
                                       cache_key=document_set_key(docs))
        assert len(calls) == 3
        reread = [docs[0], {**docs[1], "content": "A fictional refund request, re-read."}]
        await h._extract_case_entities(ws, "packed for analysis" * 50,
                                       cache_key=document_set_key(reread))
        assert len(calls) == 6

    try:
        asyncio.run(_go())
    finally:
        h._workspace_case_entities_cache.pop(ws, None)


# -- bounded per-document and per-date calls ------------------------------------


def test_dates_pass_bounded_same_result():
    in_flight = {"now": 0, "max": 0}

    async def _per_doc(item):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01 * (7 - item))  # later documents finish first
        in_flight["now"] -= 1
        return [f"event-{item}"]

    got = asyncio.run(gather_bounded(4, range(6), _per_doc))
    assert got == [[f"event-{i}"] for i in range(6)]
    assert in_flight["max"] == 4
    assert asyncio.run(gather_bounded(0, [1, 2], _per_doc)) == [["event-1"], ["event-2"]]


def test_dedupe_bounded_same_result(monkeypatch):
    from app.socket.events import event_handler as h

    in_flight = {"now": 0, "max": 0}

    async def _keep(labels, *, item_type, max_keep):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return [1]  # keep the first entry on each date

    monkeypatch.setattr(h, "_llm_semantic_keep_indexes", _keep)
    entries = []
    for day in range(1, 7):
        for n in range(2):
            entries.append({"date": f"2026-01-0{day}", "title": f"Fictional event {day}-{n}",
                            "description": "A fictional booking step."})
    entries.append({"date": "2026-02-01", "title": "Only event", "description": "Alone."})

    def _dedupe(limit):
        monkeypatch.setattr(settings, "list_of_dates_concurrency", limit)
        return asyncio.run(h._dedupe_dated_entries_semantically(
            [dict(e) for e in entries], item_type="list of dates"))

    sequential = _dedupe(1)
    assert in_flight["max"] == 1
    in_flight["max"] = 0
    concurrent = _dedupe(4)
    assert in_flight["max"] == 4
    assert concurrent == sequential
    assert [e["title"] for e in concurrent] == (
        [f"Fictional event {d}-0" for d in range(1, 7)] + ["Only event"]
    )
