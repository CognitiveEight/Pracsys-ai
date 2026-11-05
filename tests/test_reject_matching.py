"""Rejecting one Analysis Report item removes that item only.

The old matcher treated any two items sharing two informative words as the
same item, so one reject on a case whose gaps all mention the same documents
removed every one of them, and the rejection memory then suppressed every
later gap sharing those words. Fictional data only; no model or socket calls.
"""

import asyncio
import copy
import uuid

import pytest

from app.socket.schemas import RejectionFeedback
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

    # What the reason generator returns; an exception instance is raised.
    verdict = {"value": "Fictional reason."}

    async def _reason(**_kw):
        if isinstance(verdict["value"], BaseException):
            raise verdict["value"]
        return verdict["value"]

    async def _explanation(**_kw):
        return "Fictional explanation."

    async def _retag(_ws, report):
        return report

    async def _emit(*, feature_type, workspace_id, user_id, payload, usage):
        # A snapshot: rejectionFeedback is removed from the report after the emit.
        emitted.append((feature_type, workspace_id, copy.deepcopy(payload)))
        return True

    monkeypatch.setattr(h, "_hydrate_workspace_caches", _noop)
    monkeypatch.setattr(h, "_rejection_reason", _reason)
    monkeypatch.setattr(h, "_ensure_quality_score_explanation", _explanation)
    monkeypatch.setattr(h, "_retag_analysis_report_safe", _retag)
    monkeypatch.setattr(h, "_emit_document_feature", _emit)
    monkeypatch.setattr(h, "_socket_client", _Socket())
    h.emitted = emitted
    h.verdict = verdict
    yield h
    del h.emitted
    del h.verdict


def _seed(h, ws):
    h._workspace_analysis_payload[ws] = {
        "missingInfo": list(GAPS),
        "weaknesses": [],
        "risks": [],
        "strengths": [],
        "caseResolutionScore": 40,
    }


def _event(value, section="missingInfo"):
    return {
        "section": section,
        "rejected_value": value,
        "user_id": "user-fictional",
        "workspace_role": "PETITIONER",
    }


def _reject(h, ws, value, section="missingInfo"):
    asyncio.run(h._apply_batched_feedback(ws, [_event(value, section)]))


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


# -- why the score did or did not move (rejectionFeedback) -----------------------


def _last_report(h, ws):
    return [p for f, w, p in h.emitted if f == "analysis_report" and w == ws][-1]


def test_reject_reports_its_reason_and_increase(handler):
    ws = f"ws-why-valid-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    handler.verdict["value"] = "This copy is not needed for the refund claim."
    try:
        _reject(handler, ws, GAPS[0])
        sent = _last_report(handler, ws)
        assert sent["caseResolutionScore"] == 45
        assert sent["rejectionFeedback"] == {
            "scoreChange": 5,
            "items": [{
                "section": "missingInfo", "rejectedValue": GAPS[0],
                "isValidRejection": True, "scoreIncrease": 5,
                "reason": "This copy is not needed for the refund claim.",
            }],
        }
        RejectionFeedback.model_validate(sent["rejectionFeedback"])
        # On this emit only: the cache (and so any replay) never carries it.
        assert "rejectionFeedback" not in handler._workspace_analysis_payload[ws]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


WEAKNESS = "No written acknowledgement of the cancellation request from the supplier"
RISK = "The limitation period for the consumer complaint may be argued to have run"


def _seed_mixed(h, ws, score=40):
    h._workspace_analysis_payload[ws] = {
        "missingInfo": list(GAPS),
        "weaknesses": [WEAKNESS],
        "risks": [RISK],
        "strengths": [],
        "caseResolutionScore": score,
    }


@pytest.mark.parametrize("value,increase", [(WEAKNESS, 5), (RISK, 3)])
def test_risk_or_weakness_reject_adds_its_section_weight(handler, value, increase):
    ws = f"ws-weight-{uuid.uuid4().hex[:8]}"
    _seed_mixed(handler, ws)
    try:
        _reject(handler, ws, value, section="riskOrWeakness")
        sent = _last_report(handler, ws)
        assert sent["caseResolutionScore"] == 40 + increase
        assert sent["rejectionFeedback"]["items"][0]["scoreIncrease"] == increase
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_bulk_reject_counts_every_item(handler):
    # Five rejects in one batch: 4 x 5 + 3, no per-batch cap.
    ws = f"ws-bulk-{uuid.uuid4().hex[:8]}"
    _seed_mixed(handler, ws)
    events = [_event(g) for g in GAPS[:4]] + [_event(RISK, "riskOrWeakness")]
    try:
        asyncio.run(handler._apply_batched_feedback(ws, events))
        sent = _last_report(handler, ws)
        assert sent["caseResolutionScore"] == 63
        assert sent["rejectionFeedback"]["scoreChange"] == 23
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_score_is_capped_at_95_until_the_last_item_goes(handler):
    ws = f"ws-cap-{uuid.uuid4().hex[:8]}"
    handler._workspace_analysis_payload[ws] = {
        "missingInfo": [GAPS[0], GAPS[1]], "weaknesses": [], "risks": [],
        "strengths": [], "caseResolutionScore": 93,
    }
    try:
        _reject(handler, ws, GAPS[0])
        assert _last_report(handler, ws)["caseResolutionScore"] == 95
        _reject(handler, ws, GAPS[1])
        assert _last_report(handler, ws)["caseResolutionScore"] == 100
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_reason_failure_still_raises_the_score(handler):
    ws = f"ws-noreason-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    handler.verdict["value"] = RuntimeError("model unavailable")
    try:
        _reject(handler, ws, GAPS[1])
        sent = _last_report(handler, ws)
        assert sent["caseResolutionScore"] == 45
        assert sent["missingInfo"] == [GAPS[0], GAPS[2], GAPS[3]]
        item = sent["rejectionFeedback"]["items"][0]
        assert item["isValidRejection"] is True and item["scoreIncrease"] == 5
        assert item["reason"]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_duplicate_in_one_batch_counts_once(handler):
    ws = f"ws-dup-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    try:
        asyncio.run(handler._apply_batched_feedback(ws, [_event(GAPS[0]), _event(GAPS[0])]))
        sent = _last_report(handler, ws)
        assert sent["caseResolutionScore"] == 45
        increases = sorted(i["scoreIncrease"] for i in sent["rejectionFeedback"]["items"])
        assert increases == [0, 5]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


def test_stale_reject_says_already_removed(handler):
    ws = f"ws-why-stale-{uuid.uuid4().hex[:8]}"
    _seed(handler, ws)
    try:
        _reject(handler, ws, GAPS[0])
        _reject(handler, ws, GAPS[0])  # the frontend re-sent it
        sent = _last_report(handler, ws)
        assert sent["rejectionFeedback"]["scoreChange"] == 0
        assert sent["rejectionFeedback"]["items"][0]["reason"].startswith("This item was already removed")
        assert "rejectionFeedback" not in handler._workspace_analysis_payload[ws]
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
        handler._workspace_rejected_analysis_items.pop(ws, None)


# -- the chat "lawyer-provided updates" marker path uses the same weights ---------

MARKER_GAP = "Proof of payment for the holiday booking"
MARKER_RISK = "The limitation period for the consumer complaint may be argued to have run"


def _seed_marker(h, ws, score):
    h._workspace_analysis_payload[ws] = {
        "missingInfo": [MARKER_GAP, "Signed statement from the travel insurer"],
        "weaknesses": ["No written acknowledgement from the supplier"],
        "risks": [MARKER_RISK, "Supplier may dispute the district forum's jurisdiction"],
        "strengths": [],
        "caseResolutionScore": score,
    }


@pytest.mark.parametrize("old_score,expected", [(40, 48), (92, 95)])
def test_chat_marker_updates_use_the_reject_weights(handler, monkeypatch, old_score, expected):
    # One missing item (5) and one risk (3) at 40% give 48%; items remain, so the
    # score is capped at 95%. The old rule capped the marker delta at 5 (43%).
    async def _refresh(**_kw):
        return True

    monkeypatch.setattr(handler, "_emit_workspace_refresh", _refresh)
    ws = f"ws-marker-{uuid.uuid4().hex[:8]}"
    _seed_marker(handler, ws, old_score)
    text = (
        "Noted.\n"
        f"RESOLVED_MISSING_INFO:\n- {MARKER_GAP}\n"
        f"REMOVED_RISKS:\n- {MARKER_RISK}\n"
    )
    try:
        result = asyncio.run(handler._try_apply_intelligence_chat_panel_updates(
            ws, "user-fictional", text, ignore_strategic_objective=True,
        ))
        assert result["applied"] is True
        sent = _last_report(handler, ws)
        assert sent["missingInfo"] == ["Signed statement from the travel insurer"]
        assert sent["risks"] == ["Supplier may dispute the district forum's jurisdiction"]
        assert sent["caseResolutionScore"] == expected
        assert handler._workspace_analysis_payload[ws]["caseResolutionScore"] == expected
    finally:
        handler._workspace_analysis_payload.pop(ws, None)
