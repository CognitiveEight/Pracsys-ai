"""Ontology schema — the write-time invariants must actually bite."""

import pytest

from app.ontology.schema import (
    EDGE_TYPES,
    EDGES_BY_TYPE,
    NODE_TYPES,
    NODES_BY_LABEL,
    ONTOLOGY_VERSION,
    PROPERTY_ANCHORED,
    OntologyViolation,
    describe,
    has_provenance,
    validate_edge,
    validate_node,
)

PROVENANCE = {
    "workspace_id": "ws-1",
    "source_document_id": "doc-1",
    "chunk_id": "doc-1:c1",
    "page": 3,
    "extraction_confidence": 0.87,
}


def test_section_4_counts():
    """Section 4 defines 20 node types and 21 relationship types."""
    assert len(NODE_TYPES) == 20
    assert len(EDGE_TYPES) == 21


def test_version_is_semver_and_the_schema_is_still_additive_only():
    """
    Node and edge *counts* are fixed by Section 4; properties are extensible.

    1.1.0 added Breach.description, Relief.description and Statute as a
    GOVERNED_BY head, all after real matters produced them and the writes were
    rejected. Additive changes bump the minor version and need no re-ingestion —
    anything that invalidates already-written facts bumps the major.
    """
    major, minor, patch = ONTOLOGY_VERSION.split(".")
    assert major == "1", "a major bump means already-written facts are invalid"
    assert int(minor) >= 1


def test_extensions_learned_from_real_matters():
    assert "description" in NODES_BY_LABEL["Breach"].property_names
    assert "description" in NODES_BY_LABEL["Relief"].property_names
    assert "Statute" in EDGES_BY_TYPE["GOVERNED_BY"].to_labels
    assert "Provision" in EDGES_BY_TYPE["GOVERNED_BY"].to_labels


def test_every_node_carries_the_mandatory_block():
    for spec in NODE_TYPES:
        assert {"workspace_id", "source_document_id", "chunk_id", "extraction_confidence"} <= spec.required_names, spec.label


def test_valid_node_passes():
    validate_node("Party", {"canonical_name": "Meridian Traders", **PROVENANCE})


def test_node_without_provenance_is_rejected():
    """The provenance invariant: a fact with no source does not enter the graph."""
    with pytest.raises(OntologyViolation, match="chunk_id"):
        validate_node(
            "Party",
            {"canonical_name": "X", "workspace_id": "ws-1", "source_document_id": "d",
             "extraction_confidence": 0.9},
        )


def test_node_without_workspace_is_rejected():
    props = {k: v for k, v in PROVENANCE.items() if k != "workspace_id"}
    with pytest.raises(OntologyViolation, match="workspace_id"):
        validate_node("Party", {"canonical_name": "X", **props})


def test_empty_string_counts_as_missing():
    with pytest.raises(OntologyViolation, match="canonical_name"):
        validate_node("Party", {"canonical_name": "   ", **PROVENANCE})


def test_unknown_label_is_rejected():
    with pytest.raises(OntologyViolation, match="Unknown node label"):
        validate_node("Litigant", {**PROVENANCE})


def test_unknown_property_is_rejected():
    """Extend the ontology, not the payload."""
    with pytest.raises(OntologyViolation, match="unknown propert"):
        validate_node("Party", {"canonical_name": "X", "vibe": "litigious", **PROVENANCE})


@pytest.mark.parametrize("confidence", [-0.1, 1.5, "high"])
def test_confidence_out_of_range_is_rejected(confidence):
    props = {**PROVENANCE, "extraction_confidence": confidence}
    with pytest.raises(OntologyViolation, match="extraction_confidence"):
        validate_node("Party", {"canonical_name": "X", **props})


def test_valid_edge_passes():
    validate_edge("IS_PARTY_TO", "Party", "Matter", PROVENANCE)


def test_edge_endpoint_is_enforced():
    with pytest.raises(OntologyViolation, match="tail may be"):
        validate_edge("IS_PARTY_TO", "Forum", "Matter", PROVENANCE)
    with pytest.raises(OntologyViolation, match="head may be"):
        validate_edge("IS_PARTY_TO", "Party", "Forum", PROVENANCE)


def test_multi_endpoint_edge_accepts_each_permitted_label():
    """GOVERNED_BY runs from Matter or Obligation; VIOLATES targets either."""
    validate_edge("GOVERNED_BY", "Matter", "Provision", PROVENANCE)
    validate_edge("GOVERNED_BY", "Obligation", "Provision", PROVENANCE)
    validate_edge("VIOLATES", "Breach", "Obligation", PROVENANCE)
    validate_edge("VIOLATES", "Breach", "Provision", PROVENANCE)


def test_same_type_edge_rejects_mixed_endpoints():
    validate_edge("SUPERSEDES", "Order", "Order", PROVENANCE)
    with pytest.raises(OntologyViolation, match="same type"):
        validate_edge("SUPERSEDES", "Order", "Communication", PROVENANCE)


def test_property_anchored_edge_takes_no_head():
    validate_edge("OCCURRED_ON", "Event", None, PROVENANCE)
    with pytest.raises(OntologyViolation, match="property-anchored"):
        validate_edge("OCCURRED_ON", "Event", "Matter", PROVENANCE)


def test_contradicts_requires_its_domain_properties():
    with pytest.raises(OntologyViolation, match="conflict_kind"):
        validate_edge("CONTRADICTS", "Order", "Order", PROVENANCE)
    validate_edge(
        "CONTRADICTS", "Order", "Order",
        {**PROVENANCE, "conflict_kind": "date", "detector": "temporal"},
    )


def test_evidenced_by_targets_source_document_from_anything_else():
    spec = EDGES_BY_TYPE["EVIDENCED_BY"]
    assert spec.to_labels == ("SourceDocument",)
    assert "SourceDocument" not in spec.from_labels
    assert len(spec.from_labels) == len(NODE_TYPES) - 1


def test_has_provenance_helper():
    assert has_provenance(PROVENANCE)
    assert not has_provenance({k: v for k, v in PROVENANCE.items() if k != "chunk_id"})
    assert has_provenance({**PROVENANCE, "page": None}), "page may be absent in value"


def test_describe_is_serialisable_and_complete():
    described = describe()
    assert described["version"] == ONTOLOGY_VERSION
    assert len(described["nodeTypes"]) == len(NODE_TYPES)
    assert len(described["relationshipTypes"]) == len(EDGE_TYPES)
    anchored = {r["type"] for r in described["relationshipTypes"] if r["propertyAnchored"]}
    assert anchored == set(PROPERTY_ANCHORED)


def test_edge_endpoints_all_reference_real_labels():
    """A typo in from_labels/to_labels would silently make an edge unusable."""
    for spec in EDGE_TYPES:
        for label in spec.from_labels + spec.to_labels:
            assert label in NODES_BY_LABEL, f"{spec.type} references unknown label {label}"


def test_identity_properties_exist_on_their_node():
    for spec in NODE_TYPES:
        for prop in spec.identity:
            assert prop in spec.property_names, f"{spec.label} identity {prop} undeclared"
