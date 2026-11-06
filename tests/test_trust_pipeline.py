"""SIM-195: the trust pipeline (app/trust). Fictional data throughout."""

import asyncio
import json
import os
import uuid

import pytest
from loguru import logger

from app.ontology.schema import NODES_BY_LABEL
from app.trust import pipeline as trust
from app.trust import store as trust_store
from app.trust.claims import evidence_for_entries
from app.trust.evaluate import TrustParams, evaluate_case
from app.trust.relevance import relevance_scores
from app.trust.router import Bands, route

WS = "ws-trust"


def node(label, doc, props, *, conf=0.95, modality="asserted", as_of="", chunk=0):
    full = {
        **props,
        "workspace_id": WS,
        "source_document_id": doc,
        "chunk_id": f"{doc}_chunk_{chunk}",
        "page": chunk + 1,
        "extraction_confidence": conf,
    }
    return {"label": label, "properties": full, "modality": modality, "as_of": as_of}


def edge(edge_type, doc, tail, head, *, conf=0.95, modality="asserted"):
    props = {
        "workspace_id": WS,
        "source_document_id": doc,
        "chunk_id": f"{doc}_chunk_0",
        "page": 1,
        "extraction_confidence": conf,
    }
    return {
        "type": edge_type,
        "from": tail,
        "to": head,
        "properties": props,
        "modality": modality,
        "as_of": "",
    }


TEXT = {
    "A_chunk_0": "The complainant Kestrel Fabrics states the sale agreement was signed and a "
    "refund of Rs. 4,50,000 is claimed from the respondent Orion Mills for defective cloth.",
    "A_chunk_1": "Later in the complaint the refund of Rs. 4,50,000 is repeated for clarity of "
    "the prayer clause and the schedule of the goods supplied in the lot.",
    "B_chunk_0": "In its reply Orion Mills says the dispute concerns an unrelated purchase "
    "order and that any refund requested must await the arbitration under clause nine.",
    "C_chunk_0": "The complainant Kestrel Fabrics states the sale agreement was signed and a "
    "refund of Rs. 4,50,000 is claimed from the respondent Orion Mills for defective cloth.",
}


def evaluate(nodes, edges=(), relevance=None, decisions=None, text=TEXT):
    rows, counts = evidence_for_entries(nodes, list(edges), text)
    return evaluate_case(rows, relevance or {}, decisions or {}, TrustParams()), counts


def facts_of(result, label, attribute):
    return [
        f
        for f in result["facts"]
        if f["claim"]["label"] == label and f["claim"]["attribute"] == attribute
    ]


REFUND = {"relief_type": "refund", "quantum": "Rs. 4,50,000"}


# --- AC-1 / AC-2 / AC-3: corroboration and source groups ---------------------


def test_ac1_two_documents_corroborate():
    one, _ = evaluate([node("Relief", "A", REFUND)])
    two, _ = evaluate([node("Relief", "A", REFUND), node("Relief", "B", {**REFUND, "quantum": "4.5 lakh"})])
    [single] = facts_of(one, "Relief", "quantum")
    [double] = facts_of(two, "Relief", "quantum")
    assert single["source_groups"] == 1 and double["source_groups"] == 2
    assert double["confidence"] > single["confidence"]
    assert double["status"] == "trusted"


def test_ac2_one_document_is_one_source():
    result, _ = evaluate([node("Relief", "A", REFUND), node("Relief", "A", REFUND, chunk=1)])
    [fact] = facts_of(result, "Relief", "quantum")
    assert fact["source_groups"] == 1 and len(fact["evidence"]) == 2


def test_ac3_verbatim_copy_is_not_independent():
    # C carries A's passage word for word (a petition quoting its annexure).
    result, _ = evaluate([node("Relief", "A", REFUND), node("Relief", "C", REFUND)])
    [fact] = facts_of(result, "Relief", "quantum")
    assert fact["source_groups"] == 1
    assert fact["document_ids"] == ["A", "C"]


# --- AC-4 / AC-5: attribute conflicts -----------------------------------------

AGREEMENT = {"event_type": "agreement", "description": "Sale agreement executed"}


def test_ac4_disjoint_dates_conflict_and_both_sides_contested():
    result, _ = evaluate(
        [
            node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
            node("Event", "B", {**AGREEMENT, "event_date": "15th April 2024"}),
        ]
    )
    [conflict] = [c for c in result["conflicts"] if c["conflict_kind"] == "date"]
    assert conflict["axis"] == "attribute"
    dates = facts_of(result, "Event", "event_date")
    assert len(dates) == 2 and {f["status"] for f in dates} == {"contested"}
    assert all(conflict["conflict_id"] in f["conflict_ids"] for f in dates)


def test_ac4_month_contains_day_is_not_a_conflict():
    result, _ = evaluate(
        [
            node("Event", "A", {**AGREEMENT, "event_date": "March 2024"}),
            node("Event", "B", {**AGREEMENT, "event_date": "12.03.2024"}),
        ]
    )
    assert result["conflicts"] == []


def test_ac5_amount_forms_agree_and_different_amounts_conflict():
    same, _ = evaluate(
        [node("Relief", "A", REFUND), node("Relief", "B", {**REFUND, "quantum": "4.5 lakh"})]
    )
    assert same["conflicts"] == []
    differ, _ = evaluate(
        [node("Relief", "A", REFUND), node("Relief", "B", {**REFUND, "quantum": "Rs. 5 lakh"})]
    )
    assert [c["conflict_kind"] for c in differ["conflicts"]] == ["quantum"]


def test_same_document_disagreeing_with_itself_is_not_a_conflict():
    result, _ = evaluate(
        [
            node("Relief", "A", REFUND),
            node("Relief", "A", {**REFUND, "quantum": "Rs. 5 lakh"}, chunk=1),
        ]
    )
    assert result["conflicts"] == []


# --- AC-6: succession ----------------------------------------------------------

REPAY = {"obligation_type": "repayment", "owed_by": "Orion Mills", "owed_to": "Kestrel Fabrics"}


def test_ac6_status_change_over_time_is_succession():
    result, _ = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "outstanding"}, as_of="01.03.2024"),
            node("Obligation", "B", {**REPAY, "status": "paid"}, as_of="1st June 2024"),
        ]
    )
    assert result["conflicts"] == []
    assert result["stats"]["successions"] == 1


def test_ac6_undated_status_change_conflicts():
    result, _ = evaluate(
        [
            node("Obligation", "A", {**REPAY, "status": "outstanding"}),
            node("Obligation", "B", {**REPAY, "status": "paid"}, as_of="1st June 2024"),
        ]
    )
    assert [c["conflict_kind"] for c in result["conflicts"]] == ["status"]


# --- AC-7 / AC-8: denial and hedging -------------------------------------------

PAYMENT = {"amount": 500000, "currency": "INR", "transaction_date": "02.02.2024", "reference": "UTR-0001"}


def test_ac7_denial_conflicts_and_is_never_a_fact():
    result, counts = evaluate(
        [node("Transaction", "A", PAYMENT), node("Transaction", "B", PAYMENT, modality="denied")]
    )
    assert counts["denied"] == 1
    [conflict] = result["conflicts"]
    assert conflict["axis"] == "denial" and conflict["sides"][1]["denial"] is True
    # Only A supports any fact; B's denial is not one.
    assert all(f["document_ids"] == ["A"] for f in result["facts"])
    # The payment's amount and date are disputed with its existence.
    assert {f["status"] for f in result["facts"]} == {"contested"}


def test_ac8_hedged_statement_is_never_evidence():
    result, counts = evaluate([node("Relief", "A", REFUND, modality="hedged")])
    assert counts["hedged"] == 1 and result["facts"] == [] and result["stats"]["evidence"] == 0


# --- AC-9: relation and counsel conflicts --------------------------------------


def test_ac9_payment_to_two_parties_conflicts():
    ref = {"reference": "UTR-0001", "transaction_date": "02.02.2024", "amount": 500000.0}
    result, _ = evaluate(
        [],
        [
            edge("PAID_TO", "A", {"label": "Transaction", "key": ref}, {"label": "Party", "key": "Kestrel Fabrics"}),
            edge("PAID_TO", "B", {"label": "Transaction", "key": ref}, {"label": "Party", "key": "Lumen Traders"}),
        ],
    )
    [conflict] = result["conflicts"]
    assert conflict["axis"] == "relation"


def test_ac9_one_advocate_for_opposed_parties_conflicts():
    adv = {"label": "Advocate", "key": "R. Varma"}
    result, _ = evaluate(
        [
            node("Party", "A", {"canonical_name": "Kestrel Fabrics", "party_role": "petitioner"}),
            node("Party", "A", {"canonical_name": "Orion Mills", "party_role": "respondent"}),
        ],
        [
            edge("REPRESENTED_BY", "A", {"label": "Party", "key": "Kestrel Fabrics"}, adv),
            edge("REPRESENTED_BY", "B", {"label": "Party", "key": "Orion Mills"}, adv),
        ],
    )
    assert [c["axis"] for c in result["conflicts"]] == ["counsel"]


def test_co_accused_sharing_counsel_is_not_a_conflict():
    adv = {"label": "Advocate", "key": "R. Varma"}
    result, _ = evaluate(
        [
            node("Party", "A", {"canonical_name": "Kestrel Fabrics", "party_role": "accused"}),
            node("Party", "A", {"canonical_name": "Orion Mills", "party_role": "accused"}),
        ],
        [
            edge("REPRESENTED_BY", "A", {"label": "Party", "key": "Kestrel Fabrics"}, adv),
            edge("REPRESENTED_BY", "B", {"label": "Party", "key": "Orion Mills"}, adv),
        ],
    )
    assert result["conflicts"] == []


# --- AC-10: routing, review queue, decisions -----------------------------------


def test_ac10_bands_and_penalty():
    bands = Bands()
    assert route(0.85, in_unresolved_conflict=False, lost_conflict=False, decision=None, bands=bands)["status"] == "trusted"
    assert route(0.84, in_unresolved_conflict=False, lost_conflict=False, decision=None, bands=bands)["status"] == "contested"
    assert route(0.49, in_unresolved_conflict=False, lost_conflict=False, decision=None, bands=bands)["status"] == "dropped"
    conflicted = route(0.6, in_unresolved_conflict=True, lost_conflict=False, decision=None, bands=bands)
    assert conflicted["confidence"] == round(0.6 * 0.65, 4)
    # Penalised below the review band, but a disputed fact is never silently dropped.
    assert conflicted["shadow_band"] == "dropped" and conflicted["status"] == "contested"
    with pytest.raises(ValueError):
        Bands(trusted=0.5, review=0.5)


def test_ac10_one_review_item_per_contested_fact_and_decision_survives():
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    result, _ = evaluate(nodes)
    contested = [f["fact_key"] for f in result["facts"] if f["status"] == "contested"]
    assert sorted(r["fact_key"] for r in result["reviews"]) == sorted(contested)
    assert all(r["reason"] == "unresolved_conflict" for r in result["reviews"])

    [conflict] = result["conflicts"]
    decided, _ = evaluate(nodes, decisions={conflict["conflict_id"]: "keep_a"})
    by_key = {f["fact_key"]: f for f in decided["facts"]}
    assert decided["conflicts"][0]["resolution_status"] == "resolved"
    assert by_key[conflict["fact_b"]]["status"] == "dropped"
    assert by_key[conflict["fact_a"]]["status"] != "dropped"
    assert not any(r["fact_key"] == conflict["fact_b"] for r in decided["reviews"])


# --- AC-11: relevance ------------------------------------------------------------


def test_ac11_relevance_needs_enough_documents():
    vectors = {f"d{i}": [1.0, 0.1 * i, 0.0] for i in range(5)}
    assert set(relevance_scores(vectors, min_docs=10).values()) == {1.0}


def test_ac11_outlier_scores_low_and_average_document_full():
    vectors = {f"d{i}": [1.0, 0.05 * (i % 3), 0.02 * (i % 2)] for i in range(11)}
    vectors["outlier"] = [0.0, 0.1, 1.0]
    scores = relevance_scores(vectors, min_docs=10)
    assert scores["outlier"] < 0.2
    assert sum(1 for k, v in scores.items() if k != "outlier" and v == 1.0) >= 5
    assert min(v for k, v in scores.items() if k != "outlier") > scores["outlier"]


def test_relevance_discounts_but_never_zeroes_evidence():
    full, _ = evaluate([node("Relief", "A", REFUND)])
    off_topic, _ = evaluate([node("Relief", "A", REFUND)], relevance={"A": 0.0})
    a, b = facts_of(full, "Relief", "quantum")[0], facts_of(off_topic, "Relief", "quantum")[0]
    assert 0 < b["confidence"] < a["confidence"]
    assert b["confidence"] == pytest.approx(0.95 * 0.95 * 0.70, abs=1e-3)


# --- AC-12: determinism ----------------------------------------------------------


def test_ac12_recompute_is_deterministic():
    nodes = [
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Relief", "A", REFUND),
    ]
    first, _ = evaluate(nodes)
    second, _ = evaluate(list(reversed(nodes)))
    strip = lambda r: json.dumps(  # noqa: E731
        [{k: v for k, v in f.items() if k != "evidence"} for f in r["facts"]], sort_keys=True
    )
    assert strip(first) == strip(second)
    assert [c["conflict_id"] for c in first["conflicts"]] == [c["conflict_id"] for c in second["conflicts"]]


def test_cross_document_name_variants_corroborate_one_party():
    result, _ = evaluate(
        [
            node("Party", "A", {"canonical_name": "M/s Kestrel Fabrics Pvt Ltd", "party_role": "petitioner"}),
            node("Party", "B", {"canonical_name": "Kestrel Fabrics Private Limited", "party_role": "petitioner"}),
        ]
    )
    [role] = facts_of(result, "Party", "party_role")
    assert role["source_groups"] == 2


# --- pipeline with an in-memory store ---------------------------------------------


class MemoryStore:
    def __init__(self):
        self.evidence, self.profiles, self.decisions, self.derived = {}, {}, {}, {}

    def install(self, monkeypatch):
        monkeypatch.setattr(trust_store, "replace_document_evidence", self.replace_evidence)
        monkeypatch.setattr(trust_store, "put_document_profiles", self.put_profiles)
        monkeypatch.setattr(trust_store, "load_inputs", self.load)
        monkeypatch.setattr(trust_store, "replace_derived", self.replace_derived)
        monkeypatch.setattr(trust_store, "put_decision", self.put_decision)
        monkeypatch.setattr(trust_store, "list_records", self.list_records)

    async def replace_evidence(self, ws, docs, rows):
        self.evidence = {k: v for k, v in self.evidence.items() if v["document_id"] not in docs}
        for row in rows:
            self.evidence[trust_store.evidence_key(row)] = dict(row)
        return len(rows)

    async def put_profiles(self, ws, vectors):
        self.profiles.update(vectors)

    async def load(self, ws):
        return {
            "evidence": list(self.evidence.values()),
            "profiles": dict(self.profiles),
            "decisions": dict(self.decisions),
        }

    async def replace_derived(self, ws, result):
        self.derived = {
            "fact": [trust_store._slim_fact(f) for f in result["facts"]],
            "conflict": list(result["conflicts"]),
            "review": list(result["reviews"]),
        }

    async def put_decision(self, ws, key, action, decided_by="", match=None):
        self.decisions[key] = {"action": action, "match": dict(match or {})}

    async def list_records(self, ws, record):
        return list(self.derived.get(record, []))


class FakeGraph:
    def __init__(self):
        self.writes, self.trust, self.cleared = [], [], []

    async def write_facts(self, ws, nodes=(), edges=()):
        from app.graph.store import WriteResult

        self.writes.append((list(nodes), list(edges)))
        return WriteResult(nodes_written=len(nodes), edges_written=len(edges))

    async def set_trust(self, ws, label, rows):
        self.trust.append((label, rows))
        return len(rows)

    async def clear_contradictions(self, ws, detector):
        self.cleared.append(detector)
        return 0


@pytest.fixture
def memory(monkeypatch):
    from app.core.config import settings
    from app.vectorstore import qdrant

    mem = MemoryStore()
    mem.install(monkeypatch)
    monkeypatch.setattr(settings, "trust_pipeline_enabled", True)

    async def vectors(ws, docs=None):
        return {d: [1.0, 0.0] for d in docs or ()}

    async def present(ws, docs):
        return set(docs)

    monkeypatch.setattr(qdrant.vector_store, "get_document_vectors", vectors)
    monkeypatch.setattr(qdrant.vector_store, "documents_present", present)
    return mem


def chunks_for(*docs):
    return [
        {"chunk_id": cid, "document_id": cid.split("_")[0], "text": text, "page": 1}
        for cid, text in TEXT.items()
        if cid.split("_")[0] in docs
    ]


def test_pass_stores_evidence_and_serves_conflicts(memory):
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    stats = asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    assert stats["unresolvedConflicts"] == 1 and stats["byStatus"] == {"contested": 2, "trusted": 1}
    conflicts = asyncio.run(trust.unresolved_conflicts(WS))
    assert len(conflicts) == 1


def test_resolve_review_routes_through_the_router(memory):
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    [conflict] = memory.derived["conflict"]
    stats = asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "keep_both"))
    assert stats["unresolvedConflicts"] == 0
    with pytest.raises(ValueError):
        asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "delete_everything"))


# --- AC-15: graph projection -------------------------------------------------------


def test_ac15_contradiction_edges_use_full_identity_and_nodes_get_status(memory):
    from app.graph.store import _endpoint_identity

    graph = FakeGraph()
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    stats = asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, [], graph_store=graph))
    assert stats["graph"]["contradictions"] == 1 and graph.cleared == ["trust"]
    [(_, [contradiction])] = [w for w in graph.writes if w[1]]
    for end in ("from", "to"):
        ref = contradiction[end]
        assert set(ref["key"]) == set(NODES_BY_LABEL["Event"].identity)
        assert _endpoint_identity("Event", ref["key"], WS) is not None
    statuses = {row["trust_status"] for _, rows in graph.trust for row in rows}
    assert statuses == {"contested"}


def test_ac15_graph_queries_skip_dropped_and_return_status():
    from app.retrieval.graph_queries import EVIDENCE_QUERIES, build

    for name in EVIDENCE_QUERIES - {"contradictions"}:
        cypher, _ = build(name, WS)
        assert "<> 'dropped'" in cypher and "trust_status" in cypher, name


def test_trusted_status_is_not_rendered_but_contested_is():
    from app.retrieval.assembler import render_graph_fact

    assert "trust_status" not in render_graph_fact({"party": "X", "trust_status": "trusted"})
    assert "trust_status=contested" in render_graph_fact({"party": "X", "trust_status": "contested"})


# --- AC-16: what panels and chat read ------------------------------------------------


def test_ac16_block_names_both_sides_and_sources(memory):
    nodes = [
        node("Transaction", "A", PAYMENT),
        node("Transaction", "B", PAYMENT, modality="denied"),
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    block = asyncio.run(trust.conflict_block(WS, {"A": "complaint.pdf", "B": "reply.pdf"}))
    assert block.startswith("CONFLICTS BETWEEN DOCUMENTS")
    assert "denied in reply.pdf, page 1" in block and "Asserted in complaint.pdf, page 1" in block
    assert "'2024-03-12" not in block and "12.03.2024" in block and "15.04.2024" in block


def test_ac16_chat_brings_both_passages_with_labels(memory):
    from app.rag.chat_context import _add_trust_conflicts
    from app.rag.evidence import EvidencePack

    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    chunks = [
        {
            "document_id": cid,
            "original_document_id": cid.split("_")[0],
            "content": text,
            "metadata": {"page_start": 1, "page_end": 1, "chunk_index": 0},
        }
        for cid, text in TEXT.items()
        if cid in ("A_chunk_0", "B_chunk_0")
    ]
    pack, by_doc = EvidencePack(), {}
    asyncio.run(_add_trust_conflicts(pack, by_doc, chunks, WS, {}, {"A": "a.pdf", "B": "b.pdf"}))
    assert len(pack.conflict_notes) == 1
    labels = sorted(pack.items)
    assert len(labels) == 2 and all(f"[{label}]" in pack.conflict_notes[0] for label in labels)
    assert set(by_doc) == {"A", "B"}


def test_conflict_block_is_empty_when_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "trust_pipeline_enabled", False)
    assert asyncio.run(trust.conflict_block(WS)) == ""


# --- AC-17: the off switch and the ingest wiring ------------------------------------


def _complete_for(responses):
    def complete(system, user):
        for cid, text in TEXT.items():
            if text == user:
                return json.dumps(responses.get(cid, {"nodes": [], "edges": []}))
        return "{}"

    return complete


INGEST_RESPONSES = {
    "A_chunk_0": {
        "nodes": [
            {"label": "Relief", "properties": REFUND, "confidence": 0.9, "modality": "asserted"},
            {
                "label": "Relief",
                "properties": {"relief_type": "damages", "quantum": "Rs. 1 lakh"},
                "confidence": 0.9,
                "modality": "hedged",
            },
        ],
        "edges": [],
    }
}


def test_ac17_off_switch_leaves_ingest_unchanged(monkeypatch):
    from app.core.config import settings
    from app.ontology.pipeline import ingest_to_knowledge_plane

    monkeypatch.setattr(settings, "trust_pipeline_enabled", False)
    called = []

    async def spy(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(trust, "run_trust_pass", spy)
    report = asyncio.run(
        ingest_to_knowledge_plane(
            WS, graph_store=None, chunks=chunks_for("A"), complete=_complete_for(INGEST_RESPONSES)
        )
    )
    assert called == [] and report.trust == {}


def test_trust_runs_without_a_graph_and_hedged_never_reaches_the_graph(memory):
    from app.ontology.pipeline import ingest_to_knowledge_plane

    report = asyncio.run(
        ingest_to_knowledge_plane(
            WS, graph_store=None, chunks=chunks_for("A"), complete=_complete_for(INGEST_RESPONSES)
        )
    )
    assert report.trust["modality"]["hedged"] == 1
    assert {f["claim"]["subject_key"] for f in memory.derived["fact"]} == {"refund"}

    graph = FakeGraph()

    async def withdraw(ws, doc):
        return 0

    async def query(name, ws, limit=200):
        return []

    graph.withdraw_document, graph.query = withdraw, query
    asyncio.run(
        ingest_to_knowledge_plane(
            WS, graph_store=graph, chunks=chunks_for("A"), complete=_complete_for(INGEST_RESPONSES)
        )
    )
    written = [n["properties"]["relief_type"] for nodes, _ in graph.writes for n in nodes]
    assert "refund" in written and "damages" not in written


def test_extraction_prompt_asks_for_modality_and_as_of():
    from app.ontology.extraction import build_extraction_prompt

    prompt = build_extraction_prompt()
    assert '"modality"' in prompt and '"as_of"' in prompt and "denied" in prompt


# --- AC-18: no values in logs ----------------------------------------------------------


def test_ac18_trust_logs_carry_no_values(memory):
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        nodes = [
            node("Transaction", "A", PAYMENT),
            node("Transaction", "B", PAYMENT, modality="denied"),
            node("Party", "A", {"canonical_name": "Kestrel Fabrics", "party_role": "petitioner"}),
        ]
        asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    finally:
        logger.remove(sink)
    text = "\n".join(lines)
    assert "Trust recompute" in text
    for secret in ("Kestrel", "UTR-0001", "500000", "Orion", "refund"):
        assert secret not in text


# --- AC-13 / AC-14: deletion against a real Qdrant ---------------------------------------


def _local_qdrant_url():
    url = os.getenv("TEST_QDRANT_URL", "http://localhost:6333")
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=url, timeout=2).get_collections()
        return url
    except Exception:
        return None


def seed_chunks(vs, docs):
    """One stored evidence chunk per document, so the trust pass sees it exists."""
    from qdrant_client.models import PointStruct

    vs.client.upsert(
        collection_name=vs.chunks_collection,
        points=[
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{WS}:{doc}")),
                vector={"dense": [0.1] * vs.vector_size},
                payload={
                    "workspace_id": WS,
                    "original_document_id": doc,
                    "document_id": f"{doc}_chunk_0",
                    "content": TEXT.get(f"{doc}_chunk_0", ""),
                },
            )
            for doc in docs
        ],
    )


@pytest.mark.skipif(_local_qdrant_url() is None, reason="no local Qdrant")
def test_ac13_ac14_document_delete_and_workspace_purge(monkeypatch):
    from app.core.config import settings
    from app.socket.events import event_handler
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

    nodes = [
        node("Relief", "A", REFUND),
        node("Relief", "B", {**REFUND, "quantum": "4.5 lakh"}),
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]

    async def run():
        await vs.initialize()
        try:
            seed_chunks(vs, ["A", "B"])
            stats = await trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, [])
            assert stats["unresolvedConflicts"] == 1
            [refund] = [
                f
                for f in await trust_store.list_records(WS, "fact")
                if f["claim"]["attribute"] == "quantum"
            ]
            assert refund["source_groups"] == 2

            await vs.delete_documents(WS, ["B"])
            facts = await trust_store.list_records(WS, "fact")
            [refund] = [f for f in facts if f["claim"]["attribute"] == "quantum"]
            assert refund["source_groups"] == 1 and refund["document_ids"] == ["A"]
            assert await trust_store.list_records(WS, "conflict") == []
            assert all("B" not in f["document_ids"] for f in facts)
            evidence = (await trust_store.load_inputs(WS))["evidence"]
            assert evidence and all(e["document_id"] == "A" for e in evidence)

            await trust_store.put_decision(WS, "some-conflict", "dismissed")
            await vs.delete_workspace(WS)
            for record in ("fact", "conflict", "review", "evidence", "doc_profile", "decision"):
                assert await trust_store.list_records(WS, record) == [], record
        finally:
            for name in (
                vs.collection_name,
                vs.chunks_collection,
                vs.tables_collection,
                vs.trust_collection,
            ):
                if vs.client.collection_exists(name):
                    vs.client.delete_collection(name)

    asyncio.run(run())


# --- decisions, purge block, delete race (verify-round fixes) --------------------------


def test_decision_is_refound_when_a_shorter_wording_rekeys_the_facts(memory):
    """A later document with a shorter wording of the subject moves the
    canonical key, so every fact and conflict id changes. The stored signature
    still finds the reviewer's decision."""
    base = [
        node("Event", "A", {**AGREEMENT, "description": "The sale agreement executed", "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "description": "The sale agreement executed", "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), base, []))
    [conflict] = memory.derived["conflict"]
    a_value = conflict["sides"][0]["value_key"]
    asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "keep_a"))

    shorter = node("Event", "C", {**AGREEMENT, "description": "Sale agreement executed", "event_date": "12.03.2024"})
    asyncio.run(trust.run_trust_pass(WS, chunks_for("C") or [{"chunk_id": "C_chunk_0", "document_id": "C", "text": "x"}], [shorter], []))
    [again] = memory.derived["conflict"]
    assert again["conflict_id"] != conflict["conflict_id"]  # the keys did move
    assert again["resolution_status"] == "resolved"
    statuses = {
        f["claim"]["value_key"]: f["status"]
        for f in memory.derived["fact"]
        if f["claim"]["attribute"] == "event_date"
    }
    assert statuses[a_value] != "dropped"
    assert [s for v, s in statuses.items() if v != a_value] == ["dropped"]


def test_decision_records_hold_no_case_text(memory):
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    [conflict] = memory.derived["conflict"]
    asyncio.run(trust.resolve_review(WS, conflict["conflict_id"], "keep_a", decided_by="reviewer-1"))
    stored = json.dumps(memory.decisions)
    assert memory.decisions[conflict["conflict_id"]]["match"]["subjects"]
    for text in ("sale agreement", "2024-03-12", "2024-04-15", "12.03.2024"):
        assert text not in stored


def test_no_trust_write_while_the_workspace_is_being_purged(memory):
    from app.workspace.persistence import allow_workspace_persistence, block_workspace_persistence

    block_workspace_persistence(WS)
    try:
        stats = asyncio.run(trust.run_trust_pass(WS, chunks_for("A"), [node("Relief", "A", REFUND)], []))
    finally:
        allow_workspace_persistence(WS)
    assert stats == {"skipped": "workspace purge in progress"}
    assert memory.evidence == {} and memory.derived == {}


def test_store_writes_check_the_purge_block_inside_the_thread(monkeypatch):
    """The block is re-checked right before the upsert, so a write already in
    its worker thread when the purge starts does not land."""
    from app.workspace.persistence import allow_workspace_persistence, block_workspace_persistence

    calls = []

    class Client:
        def upsert(self, **kwargs):
            calls.append(kwargs)

    point = trust_store._derived(WS, "fact", "k", {"document_ids": ["A"]})
    block_workspace_persistence(WS)
    try:
        trust_store._upsert(Client(), "c", [point])
    finally:
        allow_workspace_persistence(WS)
    assert calls == []
    trust_store._upsert(Client(), "c", [point])
    assert len(calls) == 1


def test_delete_hook_removes_evidence_a_racing_pass_stored(memory, monkeypatch):
    """A pass that checked B before B's delete stores B's evidence after the
    vector store's delete; the hook runs under the same lock afterwards and
    removes it."""
    from app.socket.events import event_handler

    monkeypatch.setattr(event_handler, "_graph_store", None, raising=False)
    removed = []

    async def delete_records(ws, docs):
        removed.extend(docs)
        memory.evidence = {k: v for k, v in memory.evidence.items() if v["document_id"] not in docs}

    monkeypatch.setattr(trust_store, "delete_document_records", delete_records)
    nodes = [
        node("Event", "A", {**AGREEMENT, "event_date": "12.03.2024"}),
        node("Event", "B", {**AGREEMENT, "event_date": "15.04.2024"}),
    ]
    asyncio.run(trust.run_trust_pass(WS, chunks_for("A", "B"), nodes, []))
    asyncio.run(trust.on_documents_deleted(WS, ["B"]))
    assert removed == ["B"]
    assert all(e["document_id"] == "A" for e in memory.evidence.values())
    assert memory.derived["conflict"] == []


def test_undated_denial_of_a_recurring_payment_meets_the_dated_assertion():
    result, _ = evaluate(
        [
            node("Event", "A", {"event_type": "payment", "description": "Advance paid to the respondent", "event_date": "02.02.2024"}),
            node("Event", "B", {"event_type": "payment", "description": "Advance paid to the respondent", "event_date": ""}, modality="denied"),
        ]
    )
    assert [c["axis"] for c in result["conflicts"]] == ["denial"]


def test_two_adjournments_are_two_events_but_one_agreement_is_one():
    hearings, _ = evaluate(
        [
            node("Event", "A", {"event_type": "hearing", "description": "Matter adjourned", "event_date": "10.01.2024"}),
            node("Event", "B", {"event_type": "hearing", "description": "Matter adjourned", "event_date": "15.02.2024"}),
        ]
    )
    assert hearings["conflicts"] == []
    assert len(facts_of(hearings, "Event", "exists")) == 2
