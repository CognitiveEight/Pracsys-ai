"""
The legal ontology — versioned schema for the matter knowledge graph.

This is the schema described in Section 4 of the Agentic Ontology RAG work plan:
twenty node types and nineteen relationship types that turn a general document
store into a legal reasoning substrate. It is authored as code so that the Neo4j
constraints and indexes (``app/ontology/neo4j_ddl.py``) are *generated* from one
definition rather than maintained alongside it.

Two invariants are enforced at write time rather than trusted to the extractor:

**Provenance.** Every node and every relationship carries
``source_document_id``, ``chunk_id``, ``page`` and ``extraction_confidence``.
A fact with no provenance is rejected. This is what makes the graph auditable,
makes citations a lookup rather than a second inference, and makes it safe to
delete a document and have its dependent facts withdraw with it.

**Workspace scoping.** Every node and relationship carries ``workspace_id``.
The matter boundary is a property of the data, not of the query that happens to
be asked.

The schema is expected to change once real matters are processed — Section 9
lists an incomplete ontology as a likely risk and calls for an explicit revision
point. :data:`ONTOLOGY_VERSION` is therefore a semantic version: additive
changes (a new node type, a new optional property) bump the minor version and
require no re-ingestion; anything that invalidates already-written facts bumps
the major version and does.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

#: Semantic version of this schema. Minor = additive, major = breaking.
ONTOLOGY_VERSION = "1.2.0"


class OntologyViolation(ValueError):
    """Raised when a node or relationship does not satisfy the schema."""


# ---------------------------------------------------------------------------
# Property specification
# ---------------------------------------------------------------------------

#: Property value kinds the schema understands. Deliberately small: the graph
#: stores facts, not arbitrary structures.
PROPERTY_TYPES: FrozenSet[str] = frozenset({"string", "string[]", "date", "int", "float", "bool"})


@dataclass(frozen=True)
class PropertySpec:
    """One property on a node or relationship."""

    name: str
    type: str
    required: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.type not in PROPERTY_TYPES:
            raise ValueError(
                f"{self.name}: unknown property type {self.type!r}; "
                f"expected one of {sorted(PROPERTY_TYPES)}"
            )


def _p(
    name: str, type_: str = "string", required: bool = False, description: str = ""
) -> PropertySpec:
    """Shorthand for a PropertySpec; keeps the schema tables readable."""
    return PropertySpec(name=name, type=type_, required=required, description=description)


# ---------------------------------------------------------------------------
# The two hard invariants
# ---------------------------------------------------------------------------

#: Provenance, mandatory on every node and every relationship. ``page`` is
#: optional in value but not in presence: some sources (an audio transcript) have
#: no page, and the schema records that explicitly rather than omitting the key.
PROVENANCE_PROPERTIES: Tuple[PropertySpec, ...] = (
    _p("source_document_id", "string", True, "Ingested document this fact came from"),
    _p("chunk_id", "string", True, "Chunk within the document that supports it"),
    _p("page", "int", False, "1-indexed page; absent for page-less sources"),
    _p(
        "extraction_confidence",
        "float",
        True,
        "Extractor confidence in [0.0, 1.0]; drives the unverified threshold",
    ),
)

#: Tenancy, mandatory on every node and every relationship.
SCOPE_PROPERTIES: Tuple[PropertySpec, ...] = (
    _p("workspace_id", "string", True, "Matter/workspace boundary, enforced server-side"),
)

#: The union, in the order they are written.
MANDATORY_PROPERTIES: Tuple[PropertySpec, ...] = SCOPE_PROPERTIES + PROVENANCE_PROPERTIES

MANDATORY_PROPERTY_NAMES: FrozenSet[str] = frozenset(p.name for p in MANDATORY_PROPERTIES)

#: Names that must be present and non-null on every write.
REQUIRED_PROPERTY_NAMES: FrozenSet[str] = frozenset(
    p.name for p in MANDATORY_PROPERTIES if p.required
)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSpec:
    """
    One node type in the ontology.

    Attributes:
        label: The Neo4j label, e.g. ``"Party"``.
        description: What the node represents, for SME review and tooling.
        identity: Properties that, together with ``workspace_id``, uniquely
            identify the node. Entity resolution canonicalises onto this key,
            and a uniqueness constraint is generated from it.
        properties: Domain properties, excluding the mandatory ones.
        indexed: Properties that get a lookup index.
    """

    label: str
    description: str
    identity: Tuple[str, ...]
    properties: Tuple[PropertySpec, ...] = ()
    indexed: Tuple[str, ...] = ()

    @property
    def all_properties(self) -> Tuple[PropertySpec, ...]:
        """Domain properties plus the mandatory workspace/provenance block."""
        return self.properties + MANDATORY_PROPERTIES

    @property
    def property_names(self) -> FrozenSet[str]:
        return frozenset(p.name for p in self.all_properties)

    @property
    def required_names(self) -> FrozenSet[str]:
        return frozenset(p.name for p in self.all_properties if p.required)


#: Section 4.1. ``canonical_name`` is the entity-resolution target throughout:
#: one real-world thing is one node however it is spelled across documents.
NODE_TYPES: Tuple[NodeSpec, ...] = (
    NodeSpec(
        label="Matter",
        description="The workspace's underlying dispute or transaction",
        identity=("matter_id",),
        properties=(
            _p("matter_id", "string", True),
            _p("cause_title", "string"),
            _p("forum", "string"),
            _p("stage", "string"),
            _p("jurisdiction", "string"),
        ),
        indexed=("cause_title",),
    ),
    NodeSpec(
        label="Party",
        description="Any person or organisation party to the matter",
        identity=("canonical_name",),
        properties=(
            _p("canonical_name", "string", True),
            _p("aliases", "string[]", False, "Every surface form seen across documents"),
            _p("party_role", "string", False, "petitioner/respondent/complainant/accused/…"),
            _p("is_natural_person", "bool"),
        ),
        indexed=("party_role",),
    ),
    NodeSpec(
        label="Advocate",
        description="Counsel on record",
        identity=("canonical_name",),
        properties=(
            _p("canonical_name", "string", True),
            _p("bar_number", "string"),
            _p("appears_for", "string"),
        ),
    ),
    NodeSpec(
        label="Forum",
        description="Court, tribunal, commission or authority",
        identity=("canonical_name",),
        properties=(
            _p("canonical_name", "string", True),
            # Added in 1.2.0: forums are cited by acronym at least as often as
            # by name ("ICC" for the Internal Complaints Committee), and the
            # write was rejected for carrying the surface forms it was given.
            _p("aliases", "string[]", False, "Every surface form seen across documents"),
            _p("forum_type", "string"),
            _p("jurisdiction", "string"),
            _p("seat", "string"),
        ),
        indexed=("forum_type",),
    ),
    NodeSpec(
        label="CaseReference",
        description="A suit, petition, appeal or complaint number",
        identity=("case_number", "year"),
        properties=(
            _p("case_number", "string", True),
            _p("year", "int"),
            _p("forum", "string"),
            _p("status", "string"),
        ),
    ),
    NodeSpec(
        label="Statute",
        description="An act or regulation",
        identity=("canonical_name",),
        properties=(
            _p("canonical_name", "string", True),
            _p("short_title", "string"),
            _p("year", "int"),
            _p("framework", "string", False, "BNS / BNSS / BSA / IT_ACT / OTHER"),
        ),
        indexed=("framework",),
    ),
    NodeSpec(
        label="Provision",
        description="A section, rule or article within a statute",
        identity=("statute_ref", "section_number"),
        properties=(
            _p("section_number", "string", True),
            _p("heading", "string"),
            _p("statute_ref", "string", True, "canonical_name of the containing Statute"),
            _p(
                "resolution_status",
                "string",
                True,
                "resolved | unverified | superseded — see app/statutes/resolver.py",
            ),
        ),
        indexed=("section_number", "resolution_status"),
    ),
    NodeSpec(
        label="Precedent",
        description="A reported judgment relied on",
        identity=("citation",),
        properties=(
            _p("case_title", "string", True),
            _p("citation", "string", True),
            _p("court", "string"),
            _p("decision_date", "date"),
            _p("binding_strength", "string"),
        ),
        indexed=("court",),
    ),
    NodeSpec(
        label="Event",
        description="Anything that happened on a date",
        identity=("event_type", "event_date", "description"),
        properties=(
            _p("event_type", "string", True),
            _p("event_date", "date", True),
            _p(
                "date_precision",
                "string",
                True,
                "day | month | year | approximate — see temporal normalisation",
            ),
            _p("description", "string", True),
        ),
        indexed=("event_date", "event_type"),
    ),
    NodeSpec(
        label="Hearing",
        description="A scheduled or past listing",
        identity=("hearing_date", "purpose"),
        properties=(
            _p("hearing_date", "date", True),
            _p("purpose", "string"),
            _p("outcome", "string"),
            _p("next_date", "date"),
        ),
        indexed=("hearing_date",),
    ),
    NodeSpec(
        label="Filing",
        description="A document filed before a forum",
        identity=("filing_type", "filed_on"),
        properties=(
            _p("filing_type", "string", True),
            _p("filed_on", "date", True),
            _p("filed_by", "string"),
        ),
        indexed=("filed_on",),
    ),
    NodeSpec(
        label="Order",
        description="A direction or judgment of a forum",
        identity=("order_date", "operative_direction"),
        properties=(
            _p("order_date", "date", True),
            _p("operative_direction", "string", True),
            _p("complied", "bool"),
        ),
        indexed=("order_date",),
    ),
    NodeSpec(
        label="Obligation",
        description="A duty arising from contract, statute or order",
        identity=("obligation_type", "owed_by", "owed_to"),
        properties=(
            _p("obligation_type", "string", True),
            _p("owed_by", "string", True),
            _p("owed_to", "string"),
            _p("due_date", "date"),
            _p("status", "string"),
            # Added in 1.2.0, for the same reason as Breach.description: a duty
            # is not conveyed by its type and parties alone.
            _p("description", "string", False, "The duty as stated"),
        ),
        indexed=("status", "due_date"),
    ),
    NodeSpec(
        label="Breach",
        description="An alleged or established failure of an obligation",
        identity=("alleged_by", "breach_date"),
        properties=(
            _p("alleged_by", "string", True),
            _p("breach_date", "date"),
            _p("severity", "string"),
            # Added in 1.1.0: real matters produced a `description` on 3 of 3
            # extracted breaches and the write was rejected each time. A breach
            # with no narrative tells a panel almost nothing.
            _p("description", "string", False, "What was breached, and how"),
        ),
    ),
    NodeSpec(
        label="Relief",
        description="Remedy sought or granted",
        identity=("relief_type", "quantum"),
        properties=(
            _p("relief_type", "string", True),
            _p("quantum", "string"),
            _p("granted", "bool"),
            # Added in 1.1.0, for the same reason as Breach.description: the
            # relief sought is rarely captured by its type and quantum alone.
            _p("description", "string", False, "The remedy as pleaded"),
        ),
    ),
    NodeSpec(
        label="Asset",
        description="Property, account or subject matter in dispute",
        identity=("asset_type", "identifier"),
        properties=(
            _p("asset_type", "string", True),
            _p("identifier", "string"),
            _p("description", "string"),
            _p("value", "float"),
        ),
    ),
    NodeSpec(
        label="Transaction",
        description="A payment or transfer",
        identity=("reference", "transaction_date", "amount"),
        properties=(
            _p("amount", "float", True),
            _p("currency", "string", True),
            _p("transaction_date", "date", True),
            _p("mode", "string"),
            _p("reference", "string"),
        ),
        indexed=("transaction_date",),
    ),
    NodeSpec(
        label="Communication",
        description="Notice, email, letter or recorded call",
        identity=("medium", "sent_on", "sender", "recipient"),
        properties=(
            _p("medium", "string", True),
            # No longer required in 1.2.0. An undated letter is ordinary, and
            # requiring the date discarded the communication entirely rather
            # than recording what the document did say. It stays an identity
            # component; an absent one normalises to "" at merge time.
            _p("sent_on", "date"),
            _p("sender", "string"),
            _p("recipient", "string"),
            _p("description", "string", False, "What was communicated"),
        ),
        indexed=("sent_on",),
    ),
    NodeSpec(
        label="Exhibit",
        description="An annexure or marked exhibit",
        identity=("annexure_ref",),
        properties=(
            _p("annexure_ref", "string", True),
            _p("exhibit_mark", "string"),
            _p("description", "string"),
        ),
    ),
    NodeSpec(
        label="SourceDocument",
        description="An ingested evidence file",
        identity=("document_id",),
        properties=(
            _p("document_id", "string", True),
            _p("file_name", "string", True),
            _p("document_type", "string"),
            _p("page_count", "int"),
        ),
        indexed=("file_name",),
    ),
)

NODES_BY_LABEL: Mapping[str, NodeSpec] = {n.label: n for n in NODE_TYPES}


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeSpec:
    """
    One relationship type in the ontology.

    Attributes:
        type: The Neo4j relationship type, e.g. ``"IS_PARTY_TO"``.
        from_labels: Node labels permitted at the tail.
        to_labels: Node labels permitted at the head. Empty for a
            property-anchored pseudo-edge (see :data:`PROPERTY_ANCHORED`).
        description: The legal meaning, for SME review.
        properties: Domain properties, excluding the mandatory ones.
        derived: True when the edge is computed by the platform rather than
            extracted from a document — chronological ordering and machine
            contradiction detection. Derived edges still carry provenance:
            it points at the evidence the derivation was made from.
    """

    type: str
    from_labels: Tuple[str, ...]
    to_labels: Tuple[str, ...]
    description: str
    properties: Tuple[PropertySpec, ...] = ()
    derived: bool = False

    @property
    def all_properties(self) -> Tuple[PropertySpec, ...]:
        return self.properties + MANDATORY_PROPERTIES

    @property
    def required_names(self) -> FrozenSet[str]:
        return frozenset(p.name for p in self.all_properties if p.required)


#: Relationship types whose "target" is a date property on the tail node rather
#: than another node. Section 4.2 lists OCCURRED_ON this way. It is kept in the
#: ontology because it names a real concept the retrieval layer asks for, but it
#: is NOT emitted as a Neo4j relationship — temporal anchoring lives in the
#: ``event_date`` / ``hearing_date`` / ``filed_on`` properties, and inventing a
#: reified Date node here would fork the schema from the plan.
PROPERTY_ANCHORED: FrozenSet[str] = frozenset({"OCCURRED_ON"})

#: "same type" edges: tail and head must carry the same label. SUPERSEDES only
#: makes sense between two Orders or two Communications, never across the two.
SAME_TYPE_EDGES: FrozenSet[str] = frozenset({"SUPERSEDES"})

EDGE_TYPES: Tuple[EdgeSpec, ...] = (
    EdgeSpec(
        "IS_PARTY_TO",
        ("Party",),
        ("Matter",),
        "Establishes standing and adversarial position",
        properties=(_p("position", "string", False, "petitioner/respondent side"),),
    ),
    EdgeSpec("REPRESENTED_BY", ("Party",), ("Advocate",), "Counsel relationship"),
    EdgeSpec("FILED_BEFORE", ("CaseReference",), ("Forum",), "Where the matter is pending"),
    EdgeSpec("FILED_BY", ("Filing",), ("Party",), "Who filed what"),
    EdgeSpec("PASSED_ORDER", ("Forum",), ("Order",), "Judicial output"),
    EdgeSpec(
        # Statute added as a head in 1.1.0. Section 4.2 lists only Provision, but
        # "this matter is governed by the IT Act" is ordinary legal usage and real
        # extractions produce it; rejecting it discarded a true fact. A
        # section-level link stays strictly more informative where one exists.
        "GOVERNED_BY",
        ("Matter", "Obligation"),
        ("Provision", "Statute"),
        "The legal basis engaged — a specific provision, or the act as a whole",
    ),
    EdgeSpec("CONTAINED_IN", ("Provision",), ("Statute",), "Section-to-act containment"),
    EdgeSpec("CITES", ("Filing", "Order"), ("Precedent",), "Authority relied upon"),
    EdgeSpec("OWES", ("Party",), ("Obligation",), "Who bears the duty"),
    EdgeSpec("OWED_TO", ("Obligation",), ("Party",), "Beneficiary of the duty"),
    EdgeSpec(
        "VIOLATES",
        ("Breach",),
        ("Obligation", "Provision"),
        "The alleged contravention",
    ),
    EdgeSpec("ALLEGES", ("Party",), ("Breach",), "Who asserts it"),
    EdgeSpec("SEEKS", ("Party",), ("Relief",), "Remedy claimed"),
    EdgeSpec(
        "OCCURRED_ON",
        ("Event", "Hearing", "Filing"),
        (),
        "Temporal anchoring — carried as a date property on the tail node",
    ),
    EdgeSpec(
        "PRECEDES",
        ("Event",),
        ("Event",),
        "Derived chronological ordering",
        derived=True,
    ),
    EdgeSpec("TRANSFERRED", ("Transaction",), ("Asset",), "What moved"),
    EdgeSpec("PAID_TO", ("Transaction",), ("Party",), "Counterparty of a payment"),
    EdgeSpec(
        "EVIDENCED_BY",
        tuple(n.label for n in NODE_TYPES if n.label != "SourceDocument"),
        ("SourceDocument",),
        "Provenance — mandatory on every fact",
    ),
    EdgeSpec("MARKED_AS", ("SourceDocument",), ("Exhibit",), "Annexure and exhibit marking"),
    EdgeSpec(
        "CONTRADICTS",
        tuple(n.label for n in NODE_TYPES),
        tuple(n.label for n in NODE_TYPES),
        "Machine-detected conflict between sources",
        properties=(
            _p("conflict_kind", "string", True, "date | quantum | party | status | text"),
            _p("detector", "string", True, "Which detector raised it"),
        ),
        derived=True,
    ),
    EdgeSpec(
        "SUPERSEDES",
        ("Order", "Communication"),
        ("Order", "Communication"),
        "Later instrument displacing an earlier one",
    ),
)

EDGES_BY_TYPE: Mapping[str, EdgeSpec] = {e.type: e for e in EDGE_TYPES}


# ---------------------------------------------------------------------------
# Validation — the write-time gate
# ---------------------------------------------------------------------------


def _missing_required(spec_required: Iterable[str], props: Mapping[str, object]) -> List[str]:
    """Names that are absent or None. Empty string counts as absent for strings."""
    missing = []
    for name in sorted(spec_required):
        value = props.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def _check_confidence(props: Mapping[str, object]) -> Optional[str]:
    """Return an error string if extraction_confidence is out of range."""
    value = props.get("extraction_confidence")
    if value is None:
        return None  # absence is reported by the required-property check
    try:
        conf = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"extraction_confidence must be a number, got {value!r}"
    if not 0.0 <= conf <= 1.0:
        return f"extraction_confidence must be in [0.0, 1.0], got {conf}"
    return None


def validate_node(label: str, properties: Mapping[str, object]) -> None:
    """
    Validate one node against the ontology.

    Args:
        label: The node label.
        properties: The full property map that would be written.

    Raises:
        OntologyViolation: On an unknown label, a missing required or mandatory
            property, an unknown property, or a confidence outside [0, 1].
    """
    spec = NODES_BY_LABEL.get(label)
    if spec is None:
        raise OntologyViolation(
            f"Unknown node label {label!r}; ontology {ONTOLOGY_VERSION} defines "
            f"{', '.join(sorted(NODES_BY_LABEL))}"
        )

    missing = _missing_required(spec.required_names, properties)
    if missing:
        raise OntologyViolation(
            f"{label}: missing required propert{'y' if len(missing) == 1 else 'ies'} "
            f"{', '.join(missing)}"
        )

    unknown = sorted(set(properties) - spec.property_names)
    if unknown:
        raise OntologyViolation(
            f"{label}: unknown propert{'y' if len(unknown) == 1 else 'ies'} "
            f"{', '.join(unknown)} — extend the ontology rather than the payload"
        )

    error = _check_confidence(properties)
    if error:
        raise OntologyViolation(f"{label}: {error}")


def validate_edge(
    edge_type: str,
    from_label: str,
    to_label: Optional[str],
    properties: Mapping[str, object],
) -> None:
    """
    Validate one relationship against the ontology.

    Args:
        edge_type: The relationship type.
        from_label: Label of the tail node.
        to_label: Label of the head node; None for a property-anchored edge.
        properties: The full property map that would be written.

    Raises:
        OntologyViolation: On an unknown type, an endpoint the schema does not
            permit, a same-type edge across two different labels, or a missing
            mandatory property.
    """
    spec = EDGES_BY_TYPE.get(edge_type)
    if spec is None:
        raise OntologyViolation(
            f"Unknown relationship type {edge_type!r}; ontology {ONTOLOGY_VERSION} "
            f"defines {', '.join(sorted(EDGES_BY_TYPE))}"
        )

    if from_label not in spec.from_labels:
        raise OntologyViolation(
            f"{edge_type}: tail may be {', '.join(spec.from_labels)} — got {from_label!r}"
        )

    if edge_type in PROPERTY_ANCHORED:
        if to_label is not None:
            raise OntologyViolation(
                f"{edge_type} is property-anchored: the date lives on the {from_label} "
                "node and no relationship is written"
            )
    else:
        if to_label is None:
            raise OntologyViolation(f"{edge_type}: a head node label is required")
        if to_label not in spec.to_labels:
            raise OntologyViolation(
                f"{edge_type}: head may be {', '.join(spec.to_labels)} — got {to_label!r}"
            )
        if edge_type in SAME_TYPE_EDGES and from_label != to_label:
            raise OntologyViolation(
                f"{edge_type} connects two nodes of the same type — got {from_label} → {to_label}"
            )

    missing = _missing_required(spec.required_names, properties)
    if missing:
        raise OntologyViolation(
            f"{edge_type}: missing required propert{'y' if len(missing) == 1 else 'ies'} "
            f"{', '.join(missing)}"
        )

    error = _check_confidence(properties)
    if error:
        raise OntologyViolation(f"{edge_type}: {error}")


def has_provenance(properties: Mapping[str, object]) -> bool:
    """
    True when the mandatory workspace + provenance block is present and non-empty.

    Cheaper than :func:`validate_node` when a caller only needs the invariant.
    """
    return not _missing_required(REQUIRED_PROPERTY_NAMES, properties)


def describe() -> Dict[str, object]:
    """Machine-readable summary of the schema, for tooling and SME review."""
    return {
        "version": ONTOLOGY_VERSION,
        "nodeTypes": [
            {
                "label": n.label,
                "description": n.description,
                "identity": list(n.identity),
                "properties": [
                    {"name": p.name, "type": p.type, "required": p.required}
                    for p in n.all_properties
                ],
            }
            for n in NODE_TYPES
        ],
        "relationshipTypes": [
            {
                "type": e.type,
                "from": list(e.from_labels),
                "to": list(e.to_labels),
                "description": e.description,
                "derived": e.derived,
                "propertyAnchored": e.type in PROPERTY_ANCHORED,
            }
            for e in EDGE_TYPES
        ],
        "mandatoryProperties": [p.name for p in MANDATORY_PROPERTIES],
    }
