"""Legal ontology: the versioned schema and the Neo4j DDL generated from it."""

from app.ontology.contradictions import detect_contradictions
from app.ontology.entity_resolution import resolve_entities, resolve_parties
from app.ontology.extraction import extract_facts
from app.ontology.schema import (
    EDGE_TYPES,
    EDGES_BY_TYPE,
    MANDATORY_PROPERTIES,
    NODE_TYPES,
    NODES_BY_LABEL,
    ONTOLOGY_VERSION,
    EdgeSpec,
    NodeSpec,
    OntologyViolation,
    PropertySpec,
    describe,
    has_provenance,
    validate_edge,
    validate_node,
)
from app.ontology.temporal import build_chronology, parse_date

__all__ = [
    "EDGE_TYPES",
    "EDGES_BY_TYPE",
    "MANDATORY_PROPERTIES",
    "NODE_TYPES",
    "NODES_BY_LABEL",
    "ONTOLOGY_VERSION",
    "EdgeSpec",
    "NodeSpec",
    "OntologyViolation",
    "PropertySpec",
    "describe",
    "has_provenance",
    "validate_edge",
    "validate_node",
    "build_chronology",
    "detect_contradictions",
    "extract_facts",
    "parse_date",
    "resolve_entities",
    "resolve_parties",
]
