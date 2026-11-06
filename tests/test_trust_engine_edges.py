"""SIM-195: adversarial checks on the trust engine (app/trust).

Complements tests/test_trust_pipeline.py with the inputs real litigation files
produce: wording variants, Indian amount forms, recurring events, late
documents, decisions that must outlive new evidence, and the off switch.
Fictional data throughout; no model, Qdrant (except the skipped-without-Qdrant
check), GCS or backend is called.
"""

import asyncio
import json
import random
import uuid

import pytest
from loguru import logger

from app.trust import pipeline as trust
from app.trust.claims import evidence_for_entries
from app.trust.evaluate import TrustParams, evaluate_case
from tests.test_trust_pipeline import (
    TEXT,
    WS,
    FakeGraph,
    _complete_for,
    _local_qdrant_url,
    chunks_for,
    edge,
    facts_of,
    memory,  # noqa: F401 - pytest fixture
    node,
    seed_chunks,
)


def evaluate(nodes, edges=(), decisions=None, relevance=None):
    rows, _ = evidence_for_entries(nodes, list(edges), TEXT)
    return evaluate_case(rows, relevance or {}, decisions or {}, TrustParams())


def kinds(result):
    return sorted(c["conflict_kind"] for c in result["conflicts"])


def event(doc, description, when, *, event_type="agreement", chunk=0, modality="asserted"):
    props = {"event_type": event_type, "description": description}
    if when:
        props["event_date"] = when
    return node("Event", doc, props, chunk=chunk, modality=modality)


REPAY = {"obligation_type": "repayment", "owed_by": "Orion Mills", "owed_to": "Kestrel Fabrics"}


# --- AC-4: dates -------------------------------------------------------------------


def test_ac4_literal_month_vs_day_is_not_a_conflict():
    result = evaluate([event("A", "Goods delivered", "March 2025"), event("B", "Goods delivered", "14 March 2025")])
    assert result["conflicts"] == []
    assert {f["status"] for f in facts_of(result, "Event", "event_date")} == {"trusted"}


def test_ac4_same_day_in_two_notations_corroborates():
    result = evaluate([event("A", "Goods delivered", "03/04/2025"), event("B", "Goods delivered", "3rd April 2025")])
    assert result["conflicts"] == []
    [fact] = facts_of(result, "Event", "event_date")
    assert fact["source_groups"] == 2


def test_ac4_month_vs_day_outside_the_month_conflicts():
    result = evaluate([event("A", "Goods delivered", "March 2025"), event("B", "Goods delivered", "2 April 2025")])
    assert kinds(result) == ["date"]


def test_two_cheques_differing_only_by_number_are_two_events():
    """DEFECT: canonical._text_map merges at token_sort_ratio >= 90, which two
    six-digit cheque numbers differing in the last digit pass. Two separate
    dishonours on two dates become one event 'dated twice'."""
    result = evaluate(
        [
            event("A", "Cheque No. 104512 dishonoured", "05.01.2024", event_type="dishonour"),
            event("B", "Cheque No. 104513 dishonoured", "20.02.2024", event_type="dishonour"),
        ]
    )
    assert result["conflicts"] == [], kinds(result)


def test_recurring_event_on_two_dates_is_not_a_date_conflict():
    """DEFECT: an Event's trust subject is its description alone (claims._subject),
    while its graph identity includes event_date. Two adjournments recorded in
    two order-sheets are two events, not one event with two dates."""
    result = evaluate(
        [
            event("A", "Matter adjourned", "10.01.2024", event_type="hearing"),
            event("B", "Matter adjourned", "15.02.2024", event_type="hearing"),
        ]
    )
    assert result["conflicts"] == [], kinds(result)


def test_first_and_second_notice_stay_apart():
    result = evaluate(
        [
            event("A", "First legal notice sent", "10.01.2024", event_type="notice"),
            event("B", "Second legal notice sent", "15.02.2024", event_type="notice"),
        ]
    )
    assert result["conflicts"] == []


# --- AC-5: amounts ------------------------------------------------------------------


def _relief(doc, quantum):
    return node("Relief", doc, {"relief_type": "refund", "quantum": quantum})


@pytest.mark.parametrize(
    "left,right",
    [
        ("Rs. 4,50,000", "4.5 lakh"),
        ("Rs. 4,50,000/-", "INR 450000"),
        ("Rs. 1.2 crore", "Rs. 1,20,00,000"),
        ("₹ 4,50,000.00", "Rs. 4.5 lakhs"),
    ],
)
def test_ac5_indian_amount_forms_agree(left, right):
    result = evaluate([_relief("A", left), _relief("B", right)])
    assert result["conflicts"] == []
    [fact] = facts_of(result, "Relief", "quantum")
    assert fact["source_groups"] == 2


def test_ac5_literal_lakh_amounts_conflict():
    assert kinds(evaluate([_relief("A", "4.5 lakh"), _relief("B", "5 lakh")])) == ["quantum"]


@pytest.mark.parametrize("variant", ["Rs. 4.5 lacs", "Rs. 4.5 lac"])
def test_lac_spelling_is_the_same_amount(variant):
    """DEFECT: parse_amount (app/ontology/contradictions.py:67) knows lakh/lakhs
    only; 'lac'/'lacs', common in Indian pleadings, parses as 4.5 rupees and
    manufactures a quantum conflict against 'Rs. 4,50,000'."""
    result = evaluate([_relief("A", "Rs. 4,50,000"), _relief("B", variant)])
    assert result["conflicts"] == [], kinds(result)


# --- AC-6: status and succession --------------------------------------------------------


def test_ac6_both_sides_undated_conflict():
    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "outstanding"}),
            node("Obligation", "B", {**REPAY, "status": "paid"}),
        ]
    )
    assert kinds(result) == ["status"] and result["stats"]["successions"] == 0


def test_ac6_overlapping_as_of_is_not_succession():
    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "outstanding"}, as_of="March 2024"),
            node("Obligation", "B", {**REPAY, "status": "paid"}, as_of="15.03.2024"),
        ]
    )
    assert kinds(result) == ["status"]


def test_unpaid_and_outstanding_agree():
    """DEFECT: _status_group (app/ontology/contradictions.py:279) matches by
    substring and checks the 'discharged' group first, so 'unpaid' contains
    'paid' and lands in the discharged group: a false conflict here."""
    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "unpaid"}),
            node("Obligation", "B", {**REPAY, "status": "outstanding"}),
        ]
    )
    assert result["conflicts"] == [], kinds(result)


def test_unpaid_and_paid_conflict():
    """DEFECT (same root cause): 'unpaid' vs 'paid' is missed entirely."""
    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "unpaid"}),
            node("Obligation", "B", {**REPAY, "status": "paid"}),
        ]
    )
    assert kinds(result) == ["status"]


# --- AC-7 / AC-8: denial and hedging -------------------------------------------------------


def test_ac7_denial_matched_through_a_wording_variant():
    result = evaluate(
        [
            event("A", "Advance of Rs 5 lakh paid to the respondent", "02.02.2024", event_type="payment"),
            event("B", "Advance of Rs 5 lakh paid to respondent", "", event_type="payment", modality="denied"),
        ]
    )
    assert kinds(result) == ["denial"]
    assert all(f["document_ids"] == ["A"] for f in result["facts"])


def test_payment_denied_without_its_reference_is_still_a_denial():
    """DEFECT/GAP: a Transaction's subject is its reference when present, else
    date+amount (claims._subject). A reply denying 'Rs. 5,00,000 on 02.02.2024'
    never repeats the complainant's UTR, so the two never meet and the core
    dispute of the case is not reported."""
    asserted = {"reference": "UTR-0001", "amount": "Rs. 5,00,000", "transaction_date": "02.02.2024"}
    denied = {"amount": "Rs. 5,00,000", "transaction_date": "02.02.2024"}
    result = evaluate(
        [node("Transaction", "A", asserted), node("Transaction", "B", denied, modality="denied")]
    )
    assert kinds(result) == ["denial"]


def test_ac7_same_document_assert_and_deny_is_not_a_conflict():
    result = evaluate(
        [
            node("Transaction", "A", {"reference": "UTR-0001", "amount": 500000}),
            node("Transaction", "A", {"reference": "UTR-0001", "amount": 500000}, modality="denied", chunk=1),
        ]
    )
    assert result["conflicts"] == []


def test_ac8_hedged_edge_is_never_evidence():
    ref = {"reference": "UTR-0001", "transaction_date": "02.02.2024", "amount": 500000.0}
    result = evaluate(
        [],
        [edge("PAID_TO", "A", {"label": "Transaction", "key": ref}, {"label": "Party", "key": "Lumen Traders"}, modality="hedged")],
    )
    assert result["facts"] == [] and result["stats"]["evidence"] == 0


# --- AC-9: relations, roles and name variants ---------------------------------------------


def test_ac9_payee_name_variants_are_not_two_payees():
    ref = {"reference": "UTR-0001", "transaction_date": "02.02.2024", "amount": 500000.0}
    result = evaluate(
        [],
        [
            edge("PAID_TO", "A", {"label": "Transaction", "key": ref}, {"label": "Party", "key": "M/s Lumen Traders Pvt Ltd"}),
            edge("PAID_TO", "B", {"label": "Transaction", "key": ref}, {"label": "Party", "key": "Lumen Traders Private Limited"}),
        ],
    )
    assert result["conflicts"] == []


def test_ac9_obligation_debtor_name_variants_share_one_subject():
    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "owed_by": "M/s Orion Mills Pvt Ltd", "status": "outstanding"}),
            node("Obligation", "B", {**REPAY, "owed_by": "Orion Mills Private Limited", "status": "outstanding"}),
        ]
    )
    [fact] = facts_of(result, "Obligation", "status")
    assert fact["source_groups"] == 2 and result["conflicts"] == []


def test_respondent_below_who_appeals_is_not_a_role_conflict():
    """DESIGN DEFECT: party_role is not time-varying and has no forum, so the
    ordinary course of an appeal (respondent below, appellant above) is
    reported as a 'party' conflict on every appealed case."""
    result = evaluate(
        [
            node("Party", "A", {"canonical_name": "Kestrel Fabrics", "party_role": "respondent"}),
            node("Party", "B", {"canonical_name": "Kestrel Fabrics", "party_role": "appellant"}),
        ]
    )
    assert result["conflicts"] == [], kinds(result)


# --- AC-10: decisions -------------------------------------------------------------------------


def test_ac10_keep_b_drops_side_a_and_confirm_overrides():
    nodes = [event("A", "Sale agreement executed", "12.03.2024"), event("B", "Sale agreement executed", "15.04.2024")]
    [conflict] = evaluate(nodes)["conflicts"]
    decided = evaluate(nodes, decisions={conflict["conflict_id"]: "keep_b"})
    by_key = {f["fact_key"]: f for f in decided["facts"]}
    assert by_key[conflict["fact_a"]]["status"] == "dropped"
    assert by_key[conflict["fact_b"]]["status"] != "dropped"
    forced = evaluate(nodes, decisions={conflict["fact_a"]: "confirm_trusted"})
    assert {f["fact_key"]: f for f in forced["facts"]}[conflict["fact_a"]]["status"] == "trusted"
    assert not any(r["fact_key"] == conflict["fact_a"] for r in forced["reviews"])


def test_ac10_low_confidence_fact_gets_one_review_item():
    result = evaluate([node("Relief", "A", {"relief_type": "refund", "quantum": "Rs. 4,50,000"}, conf=0.65)])
    contested = [f for f in result["facts"] if f["status"] == "contested"]
    assert contested
    assert sorted(r["fact_key"] for r in result["reviews"]) == sorted(f["fact_key"] for f in contested)
    assert {r["reason"] for r in result["reviews"]} == {"low_confidence"}


def test_ac10_decision_survives_a_rerun_of_the_pipeline(memory):  # noqa: F811
    nodes = [event("A", "Sale agreement executed", "12.03.2024"), event("B", "Sale agreement executed", "15.04.2024")]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    [conflict] = memory.derived["conflict"]
    asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "keep_a"))
    # Document A is re-read; the case is rebuilt from scratch.
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A"), nodes[:1], []))
    [again] = memory.derived["conflict"]
    assert again["conflict_id"] == conflict["conflict_id"]
    assert again["resolution_status"] == "resolved"
    assert asyncio.run(trust.unresolved_conflicts(WS)) == []


def test_decision_survives_a_new_document_that_restates_the_event():
    """DEFECT: decisions are keyed by conflict_id = hash of fact keys, and fact
    keys embed the case-wide representative wording (canonical._text_map picks
    the most-mentioned variant). A later document that merely restates the
    event more often changes the representative, the conflict gets a new id,
    and the lawyer's keep_a decision is orphaned: the conflict reopens."""
    base = [event("A", "Sale agreement executed", "12.03.2024"), event("B", "Sale agreement executed", "15.04.2024")]
    [conflict] = evaluate(base)["conflicts"]
    decisions = {conflict["conflict_id"]: "keep_a"}
    later = base + [event("C", "The sale agreement executed", "12.03.2024", chunk=i) for i in range(3)]
    result = evaluate(later, decisions=decisions)
    assert [c["resolution_status"] for c in result["conflicts"]] == ["resolved"]


# --- AC-12: determinism ---------------------------------------------------------------------------


def test_ac12_full_result_identical_under_any_evidence_order():
    nodes = [
        node("Party", "A", {"canonical_name": "Kestrel Fabrics", "party_role": "petitioner"}),
        node("Party", "B", {"canonical_name": "M/s Kestrel Fabrics Pvt Ltd", "party_role": "petitioner"}),
        node("Party", "C", {"canonical_name": "Kestrel Fabrics Private Limited", "party_role": "respondent"}),
        event("A", "Sale agreement executed", "12.03.2024"),
        event("B", "The sale agreement executed", "15.04.2024"),
        node("Obligation", "A", {**REPAY, "status": "outstanding"}),
        node("Obligation", "C", {**REPAY, "status": "paid"}),
    ]
    rows, _ = evidence_for_entries(nodes, [], TEXT)

    def snapshot(order):
        result = evaluate_case(order, {}, {}, TrustParams())
        return json.dumps(
            {
                "facts": [{k: v for k, v in f.items() if k != "evidence"} for f in result["facts"]],
                "conflicts": result["conflicts"],
                "reviews": result["reviews"],
            },
            sort_keys=True,
            default=str,
        )

    expected = snapshot(list(rows))
    assert json.loads(expected)["conflicts"], "fixture must produce conflicts"
    for seed in range(15):
        shuffled = random.Random(seed).sample(rows, len(rows))
        assert snapshot(shuffled) == expected, seed


# --- AC-13: deletion re-projects survivors into the graph ------------------------------------------


def test_ac13_delete_rebuilds_and_reprojects_into_the_graph(memory, monkeypatch):  # noqa: F811
    from app.socket.events import event_handler

    graph = FakeGraph()
    monkeypatch.setattr(event_handler, "_graph_store", graph, raising=False)
    refund = {"relief_type": "refund", "quantum": "Rs. 4,50,000"}
    nodes = [
        node("Relief", "A", refund),
        node("Relief", "B", {**refund, "quantum": "4.5 lakh"}),
        event("A", "Sale agreement executed", "12.03.2024"),
        event("B", "Sale agreement executed", "15.04.2024"),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, [], graph_store=graph))
    assert len(memory.derived["conflict"]) == 1

    # What QdrantStore._delete_trust_sync does, then its post-delete hook.
    memory.evidence = {k: v for k, v in memory.evidence.items() if v["document_id"] != "B"}
    graph.writes.clear()
    asyncio.run(trust.on_documents_deleted(WS, ["B"]))

    assert memory.derived["conflict"] == []
    [quantum] = [f for f in memory.derived["fact"] if f["claim"]["attribute"] == "quantum"]
    assert quantum["source_groups"] == 1 and quantum["document_ids"] == ["A"]
    rewritten = [n for nodes_, _ in graph.writes for n in nodes_]
    assert {n["properties"]["source_document_id"] for n in rewritten} == {"A"}
    assert not any(e for _, edges in graph.writes for e in edges if e["type"] == "CONTRADICTS")
    assert graph.cleared[-1] == "trust"


@pytest.mark.skipif(_local_qdrant_url() is None, reason="no local Qdrant")
def test_late_extraction_of_a_deleted_document_is_not_stored(monkeypatch):
    """DEFECT/RISK: extraction runs minutes after upload. A document deleted in
    that window has nothing to delete yet; when its trust pass then lands,
    run_trust_pass stores its evidence and serves its facts and conflicts with
    no later delete to remove them (only a workspace purge would)."""
    from app.core.config import settings
    from app.socket.events import event_handler
    from app.trust import store as trust_store
    from app.vectorstore import qdrant
    from app.vectorstore.qdrant import QdrantStore

    prefix = f"pytest_trust_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "qdrant_url", _local_qdrant_url())
    monkeypatch.setattr(settings, "qdrant_api_key", None)
    monkeypatch.setattr(settings, "trust_pipeline_enabled", True)
    monkeypatch.setattr(event_handler, "_graph_store", None, raising=False)
    vs = QdrantStore()
    vs.collection_name = prefix
    vs.chunks_collection = f"{prefix}_evidence"
    vs.tables_collection = f"{prefix}_tables"
    vs.trust_collection = f"{prefix}_trust"
    monkeypatch.setattr(qdrant, "vector_store", vs)
    vs.set_trust_recompute(trust.on_documents_deleted)

    async def run():
        await vs.initialize()
        try:
            seed_chunks(vs, ["A"])
            await trust.run_trust_pass(WS, chunks_for("A"), [event("A", "Sale agreement executed", "12.03.2024")], [])
            assert (await trust_store.load_inputs(WS))["evidence"], "A must be stored"
            await vs.delete_documents(WS, ["B"])  # deleted while its extraction ran
            await trust.run_trust_pass(WS, chunks_for("B"), [event("B", "Sale agreement executed", "15.04.2024")], [])
            evidence = (await trust_store.load_inputs(WS))["evidence"]
            assert all(e["document_id"] != "B" for e in evidence)
            assert await trust_store.list_records(WS, "conflict") == []
        finally:
            for name in (vs.collection_name, vs.chunks_collection, vs.tables_collection, vs.trust_collection):
                if vs.client.collection_exists(name):
                    vs.client.delete_collection(name)

    asyncio.run(run())


# --- AC-15: CONTRADICTS endpoints for every composite label --------------------------------------------


@pytest.mark.parametrize(
    "label,props,change",
    [
        ("Relief", {"relief_type": "refund", "quantum": "Rs. 4,50,000"}, {"quantum": "Rs. 9,00,000"}),
        ("Transaction", {"reference": "UTR-0001", "amount": "Rs. 5,00,000", "transaction_date": "02.02.2024"}, {"amount": "Rs. 6,00,000"}),
        ("Event", {"event_type": "agreement", "description": "Sale agreement executed", "event_date": "12.03.2024"}, {"event_date": "15.04.2024"}),
        ("CaseReference", {"case_number": "CS 101/2023", "year": "2023"}, {"year": "2024"}),
    ],
)
def test_ac15_contradicts_endpoints_are_addressable(label, props, change):
    from app.graph.store import _endpoint_identity
    from app.trust.pipeline import _contradiction_edges

    result = evaluate([node(label, "A", props), node(label, "B", {**props, **change})])
    assert len(result["conflicts"]) == 1
    [contradiction] = _contradiction_edges(WS, result)
    for end in ("from", "to"):
        ref = contradiction[end]
        assert _endpoint_identity(ref["label"], ref["key"], WS) is not None, (label, end)
    assert contradiction["from"]["key"] != contradiction["to"]["key"]
    assert contradiction["properties"]["detector"] == "trust"


def test_ac15_same_node_conflict_draws_no_self_edge():
    from app.trust.pipeline import _contradiction_edges

    result = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "outstanding"}),
            node("Obligation", "B", {**REPAY, "status": "paid"}),
        ]
    )
    assert len(result["conflicts"]) == 1 and _contradiction_edges(WS, result) == []


# --- AC-17: the off switch -------------------------------------------------------------------------------


class _IngestGraph(FakeGraph):
    async def withdraw_document(self, ws, doc):
        return 0

    async def query(self, name, ws, limit=200):
        return []


def test_ac17_off_switch_does_not_cap_extraction(monkeypatch):
    """DEFECT: cap_chunks_per_document(TRUST_MAX_CHUNKS_PER_DOCUMENT) runs in
    ingest_to_knowledge_plane whether or not the trust pipeline is on, so a
    TRUST_* setting changes ONTOLOGY ingest with the pipeline off."""
    from app.core.config import settings
    from app.ontology.pipeline import ingest_to_knowledge_plane

    monkeypatch.setattr(settings, "trust_pipeline_enabled", False)
    monkeypatch.setattr(settings, "trust_max_chunks_per_document", 1)
    report = asyncio.run(
        ingest_to_knowledge_plane(
            WS, graph_store=_IngestGraph(), chunks=chunks_for("A"), complete=_complete_for({})
        )
    )
    assert report.chunks == 2 and not report.skipped


def test_ac17_off_switch_writes_no_trust_status(monkeypatch):
    from app.core.config import settings
    from app.ontology.pipeline import ingest_to_knowledge_plane

    monkeypatch.setattr(settings, "trust_pipeline_enabled", False)
    graph = _IngestGraph()
    responses = {
        "A_chunk_0": {
            "nodes": [{"label": "Relief", "properties": {"relief_type": "refund", "quantum": "Rs. 4,50,000"}, "confidence": 0.9}],
            "edges": [],
        }
    }
    report = asyncio.run(
        ingest_to_knowledge_plane(WS, graph_store=graph, chunks=chunks_for("A"), complete=_complete_for(responses))
    )
    assert report.nodes_written == 1
    assert graph.trust == [] and graph.cleared == []
    assert not any("trust_status" in n["properties"] for nodes_, _ in graph.writes for n in nodes_)


# --- AC-18: no values in any trust log line -------------------------------------------------------------


def test_ac18_no_values_or_spans_across_every_trust_path(memory, monkeypatch):  # noqa: F811
    from app.rag.chat_context import _add_trust_conflicts
    from app.rag.evidence import EvidencePack
    from app.socket.events import event_handler

    graph = FakeGraph()
    monkeypatch.setattr(event_handler, "_graph_store", graph, raising=False)
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        nodes = [
            node("Party", "A", {"canonical_name": "M/s Kestrel Fabrics Pvt Ltd", "party_role": "petitioner"}),
            node("Party", "B", {"canonical_name": "Kestrel Fabrics Private Limited", "party_role": "respondent"}),
            node("Relief", "A", {"relief_type": "refund", "quantum": "Rs. 4,50,000"}),
            node("Relief", "B", {"relief_type": "refund", "quantum": "Rs. 7.5 lakh"}),
            node("Transaction", "A", {"reference": "UTR-0001", "amount": 500000}),
            node("Transaction", "B", {"reference": "UTR-0001", "amount": 500000}, modality="denied"),
            event("A", "Sale agreement executed", "12.03.2024"),
            event("B", "Sale agreement executed", "15.04.2024", modality="hedged"),
        ]
        asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, [], graph_store=graph))
        conflict = memory.derived["conflict"][0]
        asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "keep_both", decided_by="user-x"))
        asyncio.run(trust.conflict_block(WS, {"A": "complaint.pdf"}))
        chunks = [
            {"document_id": cid, "original_document_id": cid.split("_")[0], "content": text, "metadata": {}}
            for cid, text in TEXT.items()
        ]
        asyncio.run(_add_trust_conflicts(EvidencePack(), {}, chunks, WS, {}, {}))
        memory.evidence = {k: v for k, v in memory.evidence.items() if v["document_id"] != "B"}
        asyncio.run(trust.on_documents_deleted(WS, ["B"]))
    finally:
        logger.remove(sink)
    text = "\n".join(lines)
    assert "Trust recompute" in text and "Trust rebuilt" in text
    for value in (
        "Kestrel", "Orion", "UTR", "4,50,000", "450000", "7.5 lakh", "750000", "500000",
        "refund", "agreement", "12.03.2024", "2024-03-12", "defective cloth", "arbitration",
        "complaint.pdf", "petitioner", "respondent", "user-x",
    ):
        assert value not in text, value
