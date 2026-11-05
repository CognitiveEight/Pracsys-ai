"""Rejecting every analysis item, then uploading, still yields analysis items.

Before: the model was never told what the lawyer dismissed, so it re-created
the same gaps, the rejection filter removed every one, and the panel came back
with empty Missing Information, Weaknesses and Risks. Now the dismissed items
are in the prompt and a section the filter empties is refilled. The real
generator runs; documents, entity extraction and the model are fakes.
Fictional data only.
"""

import asyncio
import json
import uuid

import pytest

from app.core.config import settings

OLD_MISSING = [
    "Copy of the booking confirmation email sent to the customer",
    "Proof of payment for the holiday booking",
]
OLD_WEAK = ["No written acknowledgement of the cancellation request from the supplier"]
OLD_RISK = ["The limitation period for the consumer complaint may be argued to have run"]
NEW_MISSING = ["Bank statement showing the refund was never credited"]
NEW_WEAK = ["The supplier's reply letter disputes the cancellation date"]
NEW_RISK = ["The supplier may claim the cancellation fee under clause 9"]

# The refill call for sections the rejection filter (or the model) left empty.
REFILL = "Return ONLY one valid JSON object with keys:\nmissingInfo"

DOCS = [{
    "document_id": "doc-fictional-1",
    "filename": "booking.pdf",
    "content": "A fictional booking dispute between a customer and a travel supplier. " * 5,
    "metadata": {},
}]


@pytest.fixture
def gen(monkeypatch):
    from app.ai.gemini_client import gemini_client as client
    from app.vectorstore import qdrant
    from app.socket.events import event_handler as h
    from app.workspace import persistence

    monkeypatch.setattr(persistence, "persist_state", lambda **_kw: None)
    monkeypatch.setattr(settings, "citations_enabled", False)
    prompts = []
    script = {"main": {}, "refill": {}}

    async def _docs(workspace_id=None, **_kw):
        return list(DOCS)

    async def _empty(*_a, **_kw):
        return ""

    async def _entities(**_kw):
        return {"parties": []}

    async def _juris(items, **_kw):
        return items or ["Consumer Disputes Redressal Commission, fictional district"]

    async def _generate(*a, prompt="", **_kw):
        p = prompt or (a[0] if a else "")
        prompts.append(p)
        if p.startswith(REFILL):
            refill = script["refill"]
            return refill if isinstance(refill, str) else json.dumps(refill)
        if "senior legal analyst" in p:
            return json.dumps(script["main"])
        return "{}"

    monkeypatch.setattr(qdrant.vector_store, "get_all_documents", _docs)
    monkeypatch.setattr(h, "_graph_evidence_block", _empty)
    monkeypatch.setattr(h, "_extract_case_entities", _entities)
    monkeypatch.setattr(h, "_ensure_structured_relevant_jurisdiction", _juris)
    monkeypatch.setattr(client, "initialize", lambda: None)
    monkeypatch.setattr(client, "is_available", lambda: True)
    monkeypatch.setattr(client, "generate", _generate)

    ws = f"ws-rejected-regen-{uuid.uuid4().hex[:8]}"
    for v in OLD_MISSING:
        h._record_rejected_analysis_item(workspace_id=ws, section="missingInfo", rejected_value=v)
    for v in OLD_WEAK + OLD_RISK:
        h._record_rejected_analysis_item(workspace_id=ws, section="riskOrWeakness", rejected_value=v)

    def _run():
        return asyncio.run(h._generate_analysis_report_from_documents(
            workspace_id=ws, user_id="user-fictional", workspace_role="PETITIONER",
            batch_result=None, is_update=True,
        ))

    class G:
        pass
    g = G()
    g.h, g.ws, g.prompts, g.script, g.run = h, ws, prompts, script, _run
    yield g
    h._workspace_rejected_analysis_items.pop(ws, None)


def _strengths():
    return [{"point": "The booking confirmation records the paid amount and dates "
                      "in full, which supports the refund claim. It is signed.", "laws": []},
            {"point": "The customer's cancellation email is dated and was sent before "
                      "the supplier's deadline. It is on record.", "laws": []}]


def test_prompt_lists_what_the_lawyer_dismissed(gen):
    gen.script["main"] = {"missingInfo": NEW_MISSING, "weaknesses": NEW_WEAK,
                          "risks": NEW_RISK, "strengths": _strengths(),
                          "caseResolutionScore": 60}
    gen.run()
    main = next(p for p in gen.prompts if "senior legal analyst" in p)
    assert "DISMISSED BY THE LAWYER" in main
    for v in OLD_MISSING + OLD_WEAK + OLD_RISK:
        assert v in main


def test_dismissed_items_regenerated_are_replaced_by_the_refill(gen):
    # The model ignores the instruction and returns only dismissed items.
    gen.script["main"] = {"missingInfo": OLD_MISSING, "weaknesses": OLD_WEAK,
                          "risks": OLD_RISK, "strengths": _strengths(),
                          "caseResolutionScore": 60}
    gen.script["refill"] = {"missingInfo": NEW_MISSING, "weaknesses": NEW_WEAK,
                            "risks": NEW_RISK, "relevantJurisdiction": []}
    report = gen.run()
    assert report["missingInfo"] == NEW_MISSING
    assert report["weaknesses"] == NEW_WEAK
    assert report["risks"] == NEW_RISK
    refill = next(p for p in gen.prompts if p.startswith(REFILL))
    assert "DISMISSED BY THE LAWYER" in refill


def test_new_items_survive_beside_dismissed_ones(gen):
    gen.script["main"] = {"missingInfo": OLD_MISSING + NEW_MISSING,
                          "weaknesses": OLD_WEAK + NEW_WEAK, "risks": OLD_RISK + NEW_RISK,
                          "strengths": _strengths(), "caseResolutionScore": 60}
    report = gen.run()
    assert report["missingInfo"] == NEW_MISSING
    assert report["weaknesses"] == NEW_WEAK
    assert report["risks"] == NEW_RISK
    # Nothing was emptied, so no refill call was needed.
    assert not any(p.startswith(REFILL) for p in gen.prompts)


def test_refill_that_repeats_dismissed_items_is_filtered_too(gen):
    # The refill ignores the DISMISSED block as well: nothing dismissed comes
    # back, and weaknesses / risks fall to the deterministic defaults.
    dismissed = {"missingInfo": OLD_MISSING, "weaknesses": OLD_WEAK,
                 "risks": OLD_RISK}
    gen.script["main"] = {**dismissed, "strengths": _strengths(),
                          "caseResolutionScore": 60}
    gen.script["refill"] = {**dismissed, "relevantJurisdiction": []}
    report = gen.run()
    shown = report["missingInfo"] + report["weaknesses"] + report["risks"]
    assert not set(shown) & set(OLD_MISSING + OLD_WEAK + OLD_RISK)
    assert report["weaknesses"] and report["risks"]


def test_refill_model_error_leaves_no_dismissed_item(gen):
    # The refill returns the error sentinel (a 429, say): no crash, and the
    # dismissed items the main call re-created stay out.
    gen.script["main"] = {"missingInfo": OLD_MISSING, "weaknesses": OLD_WEAK,
                          "risks": OLD_RISK, "strengths": _strengths(),
                          "caseResolutionScore": 60}
    gen.script["refill"] = "Error generating response: 429 quota (fictional)"
    report = gen.run()
    shown = report["missingInfo"] + report["weaknesses"] + report["risks"]
    assert not set(shown) & set(OLD_MISSING + OLD_WEAK + OLD_RISK)
    assert any(p.startswith(REFILL) for p in gen.prompts)


def test_no_rejections_means_no_block(gen):
    gen.h._workspace_rejected_analysis_items.pop(gen.ws, None)
    assert gen.h._build_rejected_items_block(gen.ws) == ""
