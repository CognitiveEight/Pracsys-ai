"""Ontology-constrained extraction (2.3) and contradiction detection (2.9)."""

import json

import pytest

from app.ontology.contradictions import (
    CONFLICT_DATE,
    CONFLICT_PARTY,
    CONFLICT_QUANTUM,
    CONFLICT_STATUS,
    detect_contradictions,
    detect_date_conflicts,
    detect_party_role_conflicts,
    detect_quantum_conflicts,
    detect_status_conflicts,
    parse_amount,
)
from app.ontology.extraction import (
    build_extraction_prompt,
    extract_facts,
    parse_model_json,
)
from app.ontology.schema import NODE_TYPES


def _responder(payload):
    return lambda system, user: json.dumps(payload)


CHUNK = [{"chunk_id": "c1", "document_id": "d1", "text": "some legal text", "page": 2}]


# --- prompt + parsing -------------------------------------------------------


def test_prompt_is_generated_from_the_live_ontology():
    """A new node type must become extractable without editing a prompt string."""
    prompt = build_extraction_prompt()
    for spec in NODE_TYPES:
        assert spec.label in prompt


def test_prompt_can_be_narrowed_to_specific_labels():
    prompt = build_extraction_prompt(labels=["Party"])
    assert "- Party:" in prompt and "- Precedent:" not in prompt


@pytest.mark.parametrize("raw", [
    '{"nodes":[{"label":"Party"}],"edges":[]}',
    'Sure!\n```json\n{"nodes":[{"label":"Party"}],"edges":[]}\n```',
    'Here you go: {"nodes":[{"label":"Party"}],"edges":[]} hope that helps',
])
def test_model_json_survives_the_usual_wrappers(raw):
    assert parse_model_json(raw)["nodes"] == [{"label": "Party"}]


@pytest.mark.parametrize("raw", ["", "   ", "total nonsense", "[1,2,3]", '{"nodes": "not a list"}'])
def test_unparseable_response_degrades_to_empty_not_a_crash(raw):
    assert parse_model_json(raw) == {"nodes": [], "edges": []}


# --- the validation gate ----------------------------------------------------


def test_valid_node_is_kept_with_provenance_stamped():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Party", "properties": {"canonical_name": "X Ltd"},
                               "confidence": 0.9}], "edges": []}),
        workspace_id="ws1",
    )
    assert len(result.nodes) == 1
    props = result.nodes[0]["properties"]
    assert props["workspace_id"] == "ws1"
    assert props["source_document_id"] == "d1"
    assert props["chunk_id"] == "c1"
    assert props["page"] == 2
    assert props["extraction_confidence"] == 0.9


def test_unknown_label_is_rejected_with_a_reason():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Litigant", "properties": {"x": 1}}], "edges": []}),
        workspace_id="ws1",
    )
    assert result.nodes == []
    assert "Unknown node label" in result.rejected[0]["reason"]


def test_node_missing_a_required_property_is_rejected():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Party", "properties": {"party_role": "accused"}}],
                    "edges": []}),
        workspace_id="ws1",
    )
    assert result.nodes == []
    assert "canonical_name" in result.rejected[0]["reason"]


def test_edge_with_an_illegal_endpoint_is_rejected():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [], "edges": [
            {"type": "IS_PARTY_TO", "from": {"label": "Forum", "key": "f"},
             "to": {"label": "Matter", "key": "m"}}]}),
        workspace_id="ws1",
    )
    assert result.edges == []
    assert "tail may be" in result.rejected[0]["reason"]


def test_legacy_citation_is_carried_forward_not_dropped():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Provision",
                               "properties": {"statute_ref": "IPC", "section_number": "420"},
                               "confidence": 0.9}], "edges": []}),
        workspace_id="ws1",
    )
    props = result.nodes[0]["properties"]
    assert props["statute_ref"] == "BNS"
    assert props["section_number"] == "318"
    assert props["resolution_status"] == "resolved"
    assert props["heading"] == "Cheating"


def test_unmappable_citation_is_rejected_not_emitted():
    """Section 4.3 rule 3: an unresolvable citation must never ship as fact."""
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Provision",
                               "properties": {"statute_ref": "IPC", "section_number": "511"}}],
                    "edges": []}),
        workspace_id="ws1",
    )
    assert result.nodes == []
    assert "could not be resolved" in result.rejected[0]["reason"]


def test_dates_are_normalised_with_precision():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Event", "properties": {
            "event_type": "return", "event_date": "14.03.2025", "description": "Cheque returned"},
            "confidence": 0.9}], "edges": []}),
        workspace_id="ws1",
    )
    props = result.nodes[0]["properties"]
    assert props["event_date"] == "2025-03-14"
    assert props["date_precision"] == "day"


def test_aliases_are_canonicalised_in_nodes_and_edge_keys():
    result = extract_facts(
        CHUNK,
        _responder({
            "nodes": [
                {"label": "Party", "properties": {"canonical_name": "M/s Meridian Traders"},
                 "confidence": 0.9},
                {"label": "Party",
                 "properties": {"canonical_name": "Meridian Traders Private Limited"},
                 "confidence": 0.9},
            ],
            "edges": [{"type": "IS_PARTY_TO", "from": {"label": "Party", "key": "M/s Meridian Traders"},
                       "to": {"label": "Matter", "key": "m1"}, "confidence": 0.9}],
        }),
        workspace_id="ws1",
    )
    names = {n["properties"]["canonical_name"] for n in result.nodes}
    assert names == {"Meridian Traders Private Limited"}
    assert result.edges[0]["from"]["key"] == "Meridian Traders Private Limited"


def test_low_confidence_is_flagged_not_discarded():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [{"label": "Party", "properties": {"canonical_name": "X"},
                               "confidence": 0.1}], "edges": []}),
        workspace_id="ws1",
    )
    assert len(result.nodes) == 1
    assert result.nodes[0]["unverified"] is True
    assert len(result.unverified) == 1


def test_a_model_error_on_one_chunk_does_not_sink_the_document():
    def flaky(system, user):
        if "boom" in user:
            raise RuntimeError("model exploded")
        return json.dumps({"nodes": [{"label": "Party",
                                      "properties": {"canonical_name": "X"}, "confidence": 0.9}],
                           "edges": []})

    result = extract_facts(
        [{"chunk_id": "c1", "document_id": "d1", "text": "boom"},
         {"chunk_id": "c2", "document_id": "d1", "text": "fine"}],
        flaky,
        workspace_id="ws1",
    )
    assert len(result.nodes) == 1
    assert any("model error" in r.get("reason", "") for r in result.rejected)


def test_rejection_rate_is_reported():
    result = extract_facts(
        CHUNK,
        _responder({"nodes": [
            {"label": "Party", "properties": {"canonical_name": "X"}, "confidence": 0.9},
            {"label": "Nope", "properties": {}},
        ], "edges": []}),
        workspace_id="ws1",
    )
    assert result.rejection_rate == 0.5


# --- contradictions ---------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Rs. 4,50,000", 450000.0), ("4.5 lakh", 450000.0), ("₹2 crore", 20000000.0),
    ("INR 1,000", 1000.0), ("450000", 450000.0), ("no amount", None),
])
def test_indian_amount_parsing(raw, expected):
    assert parse_amount(raw) == expected


def test_same_event_on_two_dates_is_a_conflict():
    found = detect_date_conflicts([
        {"description": "Cheque returned", "date": "14.03.2025", "source_document_id": "d1"},
        {"description": "cheque returned", "date": "20.03.2025", "source_document_id": "d2"},
    ])
    assert len(found) == 1 and found[0].conflict_kind == CONFLICT_DATE


def test_imprecision_is_not_a_conflict():
    """'March 2025' and '28.03.2025' overlap — reporting that would bury real findings."""
    assert detect_date_conflicts([
        {"description": "Notice issued", "date": "March 2025", "source_document_id": "d1"},
        {"description": "Notice issued", "date": "28.03.2025", "source_document_id": "d2"},
    ]) == []


def test_one_document_disagreeing_with_itself_is_not_cross_document():
    assert detect_date_conflicts([
        {"description": "Notice", "date": "14.03.2025", "source_document_id": "d1"},
        {"description": "Notice", "date": "20.03.2025", "source_document_id": "d1"},
    ]) == []


def test_equivalent_amounts_are_not_a_conflict():
    assert detect_quantum_conflicts([
        {"subject": "cheque amount", "amount": "Rs. 4,50,000", "source_document_id": "d1"},
        {"subject": "cheque amount", "amount": "4.5 lakh", "source_document_id": "d2"},
    ]) == []


def test_different_amounts_are_a_conflict():
    found = detect_quantum_conflicts([
        {"subject": "damages", "amount": "Rs 2 crore", "source_document_id": "d1"},
        {"subject": "damages", "amount": "Rs 50,00,000", "source_document_id": "d2"},
    ])
    assert len(found) == 1 and found[0].conflict_kind == CONFLICT_QUANTUM


def test_opposed_statuses_conflict_but_synonyms_do_not():
    assert len(detect_status_conflicts([
        {"subject": "repayment", "status": "discharged", "source_document_id": "d1"},
        {"subject": "repayment", "status": "outstanding", "source_document_id": "d2"},
    ])) == 1
    assert detect_status_conflicts([
        {"subject": "repayment", "status": "discharged", "source_document_id": "d1"},
        {"subject": "repayment", "status": "satisfied", "source_document_id": "d2"},
    ]) == []


def test_opposed_party_roles_conflict():
    found = detect_party_role_conflicts([
        {"canonical_name": "R Nair", "party_role": "accused", "source_document_id": "d1"},
        {"canonical_name": "R Nair", "party_role": "complainant", "source_document_id": "d2"},
    ])
    assert len(found) == 1 and found[0].conflict_kind == CONFLICT_PARTY


def test_unrelated_roles_do_not_conflict():
    assert detect_party_role_conflicts([
        {"canonical_name": "R Nair", "party_role": "witness", "source_document_id": "d1"},
        {"canonical_name": "R Nair", "party_role": "deponent", "source_document_id": "d2"},
    ]) == []


def test_contradiction_edges_are_ontology_shaped():
    out = detect_contradictions("ws1", events=[
        {"description": "Notice", "date": "14.03.2025", "source_document_id": "d1", "chunk_id": "c1"},
        {"description": "Notice", "date": "20.03.2025", "source_document_id": "d2", "chunk_id": "c2"},
    ])
    edge = out["edges"][0]
    assert edge["type"] == "CONTRADICTS"
    assert edge["properties"]["conflict_kind"] == CONFLICT_DATE
    assert edge["properties"]["workspace_id"] == "ws1"
    assert edge["properties"]["source_document_id"] == "d1"
    assert edge["counterpartSource"] == "d2"


def test_contradiction_edges_validate_against_the_ontology():
    from app.ontology.schema import validate_edge

    out = detect_contradictions("ws1", events=[
        {"description": "Notice", "date": "14.03.2025", "source_document_id": "d1", "chunk_id": "c1"},
        {"description": "Notice", "date": "20.03.2025", "source_document_id": "d2", "chunk_id": "c2"},
    ])
    edge = out["edges"][0]
    validate_edge("CONTRADICTS", edge["from"]["label"], edge["to"]["label"], edge["properties"])
