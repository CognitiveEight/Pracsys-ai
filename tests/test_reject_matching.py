"""Rejecting one Analysis Report item removes that item only.

The old matcher treated any two items sharing two informative words as the
same item, so one reject on a case whose gaps all mention the same documents
removed every one of them, and the rejection memory then suppressed every
later gap sharing those words. Fictional data only; no model or socket calls.
"""

import asyncio
import uuid

import pytest

from app.socket.handlers.analysis_report import (
    _indexes_of_rejected_item,
    _same_item_score,
)

# Distinct gaps on one fictional case. Several share "copy", "booking" or
# "email", which is normal and must not make them the same item.
GAPS = [
    "Copy of the booking confirmation email sent to the customer",
    "Proof of payment for the holiday booking",
    "Email correspondence with the supplier about the cancellation",
    "Copy of the supplier's terms and conditions for the booking",
]


def test_one_reject_removes_one_item_not_its_word_neighbours():
    assert _indexes_of_rejected_item(GAPS, GAPS[0]) == [0]
    assert _indexes_of_rejected_item(GAPS, GAPS[3]) == [3]


def test_same_text_ignoring_case_and_punctuation_is_removed():
    assert _indexes_of_rejected_item(GAPS, "  copy of the BOOKING confirmation email, sent to the customer. ") == [0]


def test_every_same_text_copy_is_removed():
    items = [GAPS[1], GAPS[0], GAPS[1]]
    assert _indexes_of_rejected_item(items, GAPS[1]) == [0, 2]


def test_dict_items_match_on_point():
    items = [{"point": g, "id": f"missingInfo_{i}"} for i, g in enumerate(GAPS)]
    assert _indexes_of_rejected_item(items, GAPS[2]) == [2]


def test_a_near_identical_rewording_removes_only_the_closest_item():
    # The regenerated report re-worded the item slightly since the lawyer saw it.
    reworded = "Copy of booking confirmation email sent to customer by the agent"
    assert _indexes_of_rejected_item(GAPS, reworded) == [0]


def test_an_unrelated_or_partial_value_removes_nothing():
    assert _indexes_of_rejected_item(GAPS, "Medical certificate of the passenger") == []
    assert _indexes_of_rejected_item(GAPS, "booking email") == []
    assert _indexes_of_rejected_item([], GAPS[0]) == []


def test_word_neighbours_score_zero():
    assert _same_item_score(GAPS[3], GAPS[0]) == 0
    assert _same_item_score(GAPS[1], GAPS[0]) == 0


# -- the handler: reject, then the rejection memory on the next rebuild -----------


class _Socket:
    async def emit(self, event, payload):  # a test double reports delivery
        return True


@pytest.fixture
def handler(monkeypatch):
    from app.socket.events import event_handler as h
    from app.workspace import persistence

    emitted = []
    # The per-workspace caches write through to Qdrant; keep that in memory.
    monkeypatch.setattr(persistence, "persist_state", lambda **_kw: None)

    async def _noop(*_a, **_kw):
        return None

    async def _delta(**_kw):
        return (True, 1, "")

    async def _explanation(**_kw):
        return "Fictional explanation."

    async def _retag(_ws, report):
        return report

    async def _emit(*, feature_type, workspace_id, user_id, payload, usage):
        emitted.append((feature_type, workspace_id, payload))
        return True

    monkeypatch.setattr(h, "_hydrate_workspace_caches", _noop)
    monkeypatch.setattr(h, "_compute_rejection_score_delta", _delta)
    monkeypatch.setattr(h, "_ensure_quality_score_explanation", _explanation)
    monkeypatch.setattr(h, "_retag_analysis_report_safe", _retag)
    monkeypatch.setattr(h, "_emit_document_feature", _emit)
    monkeypatch.setattr(h, "_socket_client", _Socket())
    h.emitted = emitted
    yield h
    del h.emitted


def _seed(h, ws):
    h._workspace_analysis_payload[ws] = {
        "missingInfo": list(GAPS),
        "weaknesses": [],
        "risks": [],
        "strengths": [],
        "caseResolutionScore": 40,
    }


def _reject(h, ws, value):
    event = {
        "section": "missingInfo",
        "rejected_value": value,
        "user_id": "user-fictional",
        "workspace_role": "PETITIONER",
    }
    asyncio.run(h._apply_batched_feedback(ws, [event]))


def test_rejecting_one_gap_keeps_the_other_three(handler):
    ws = f"ws-reject-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    try:
        _reject(handler, ws, GAPS[0])
        sent = [p for f, w, p in handler.emitted if f == "analysis_report" and w == ws]
        assert len(sent) == 1
        assert sent[0]["missingInfo"] == GAPS[1:]
        assert handler._workspace_analysis_payload[ws]["missingInfo"] == GAPS[1:]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_rejection_memory_suppresses_only_the_rejected_gap(handler):
    ws = f"ws-memory-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    try:
        _reject(handler, ws, GAPS[0])
        rejected = [
            g
            for g in GAPS
            if handler._is_previously_rejected_analysis_item(
                workspace_id=ws, section="missingInfo", candidate=g
            )
        ]
        assert rejected == [GAPS[0]]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_reworded_item_is_resolved_by_id(handler):
    # A rebuild re-worded our copy of an item the frontend already had; the
    # frontend still shows (and rejects) its own wording under the same id.
    ws = f"ws-alias-{uuid.uuid4().hex[:8]}"
    ours = "Signed copy of the supplier contract governing the holiday booking and refunds"
    handler._workspace_analysis_payload[ws] = {
        "missingInfo": [
            {"id": "missingInfo_aaa", "text": ours, "isMatched": True},
            {"id": "missingInfo_bbb", "text": GAPS[1], "isMatched": True},
        ],
        "weaknesses": [], "risks": [], "strengths": [], "caseResolutionScore": 40,
    }
    handler._workspace_current_state_payload[ws] = {
        "missingInfo": [
            {"id": "missingInfo_aaa", "text": GAPS[3], "status": "pending"},
            {"id": "missingInfo_bbb", "text": GAPS[1], "status": "pending"},
        ],
    }
    try:
        _reject(handler, ws, GAPS[3])
        sent = [p for f, w, p in handler.emitted if f == "analysis_report" and w == ws]
        assert len(sent) == 1
        assert [i if isinstance(i, str) else i.get("text") for i in sent[0]["missingInfo"]] == [GAPS[1]]
        # Both wordings are remembered, so a rebuild cannot bring either back.
        for wording in (ours, GAPS[3]):
            assert handler._is_previously_rejected_analysis_item(
                workspace_id=ws, section="missingInfo", candidate=wording)
        assert not handler._is_previously_rejected_analysis_item(
            workspace_id=ws, section="missingInfo", candidate=GAPS[1])
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_current_state_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_no_current_state_falls_back_to_text(handler):
    ws = f"ws-nocs-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    try:
        assert handler._rejected_item_aliases(ws, "missingInfo", GAPS[0],
                                              handler._workspace_analysis_payload[ws]) == []
        _reject(handler, ws, GAPS[0])
        assert handler._workspace_analysis_payload[ws]["missingInfo"] == GAPS[1:]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)

