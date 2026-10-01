"""
Neo4j schema migration generated from the legal ontology.

Work plan task 2.2: *generate Neo4j constraints and indexes from the schema;
enforce workspace scoping on every node and edge.* Generating rather than
hand-writing is the point — a property added to ``app/ontology/schema.py``
cannot drift from the constraint that protects it.

What is generated, per node type:

* a **node key** over ``workspace_id`` plus the type's identity properties, so
  one real-world entity is one node inside a matter and entity resolution has
  something to canonicalise onto;
* **existence constraints** on the mandatory workspace and provenance
  properties, which is the write-time half of the provenance invariant — the
  other half is :func:`app.ontology.schema.validate_node`, which gives a legible
  error before the driver ever sees the write;
* **range indexes** on the properties the retrieval layer filters by.

For relationships, existence constraints are generated for the mandatory
properties and for any required domain property.

Node-key and relationship-property-existence constraints are Neo4j Enterprise
features. :func:`generate_schema_statements` takes an ``edition`` argument:
under ``"community"`` the unavailable constraints are emitted as uniqueness
constraints plus indexes where possible, and the rest are reported by
:func:`unsupported_on_community` so the gap is visible rather than silent.

Nothing here connects to a database. It emits Cypher for review and for the
migration runner to apply.
"""

from typing import Dict, List

from app.ontology.schema import (
    EDGE_TYPES,
    NODE_TYPES,
    ONTOLOGY_VERSION,
    PROPERTY_ANCHORED,
    EdgeSpec,
    NodeSpec,
)

#: Neo4j editions this generator knows how to target.
EDITIONS = ("enterprise", "community")


def _constraint_name(prefix: str, label: str, suffix: str) -> str:
    """Stable, readable constraint name; Neo4j requires uniqueness across the DB."""
    return f"{prefix}_{label.lower()}_{suffix}".replace("__", "_")


def node_key_statement(spec: NodeSpec) -> str:
    """
    Node key over ``workspace_id`` + the identity properties (Enterprise).

    The workspace comes first deliberately: it makes the constraint's index
    useful for the workspace-scoped lookups every query performs.
    """
    props = ", ".join(f"n.{p}" for p in ("workspace_id",) + spec.identity)
    name = _constraint_name("nk", spec.label, "identity")
    return (
        f"CREATE CONSTRAINT {name} IF NOT EXISTS "
        f"FOR (n:{spec.label}) REQUIRE ({props}) IS NODE KEY;"
    )


def node_unique_statement(spec: NodeSpec) -> str:
    """
    Uniqueness constraint standing in for a node key on Community edition.

    Uniqueness does not imply existence, so the identity properties are also
    given existence checks by the application-side validator. Single-property
    uniqueness is all Community supports, so composite identities fall back to a
    composite index and are reported by :func:`unsupported_on_community`.
    """
    props = ", ".join(f"n.{p}" for p in ("workspace_id",) + spec.identity)
    name = _constraint_name("uq", spec.label, "identity")
    return (
        f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{spec.label}) REQUIRE ({props}) IS UNIQUE;"
    )


def node_existence_statements(spec: NodeSpec) -> List[str]:
    """Existence constraints for every mandatory and required property."""
    statements = []
    for prop in sorted(spec.required_names):
        name = _constraint_name("ex", spec.label, prop)
        statements.append(
            f"CREATE CONSTRAINT {name} IF NOT EXISTS "
            f"FOR (n:{spec.label}) REQUIRE n.{prop} IS NOT NULL;"
        )
    return statements


def node_index_statements(spec: NodeSpec) -> List[str]:
    """
    Range indexes for the properties queries filter on.

    Every node type gets a ``workspace_id`` index whether or not it declares
    one: the matter boundary is the first predicate of essentially every query.
    """
    statements = [
        f"CREATE INDEX {_constraint_name('ix', spec.label, 'workspace')} IF NOT EXISTS "
        f"FOR (n:{spec.label}) ON (n.workspace_id);"
    ]
    for prop in spec.indexed:
        name = _constraint_name("ix", spec.label, prop)
        statements.append(
            f"CREATE INDEX {name} IF NOT EXISTS "
            f"FOR (n:{spec.label}) ON (n.workspace_id, n.{prop});"
        )
    # Provenance is queried directly when a document is deleted and its
    # dependent facts must withdraw with it (task 2.11).
    statements.append(
        f"CREATE INDEX {_constraint_name('ix', spec.label, 'source_doc')} IF NOT EXISTS "
        f"FOR (n:{spec.label}) ON (n.workspace_id, n.source_document_id);"
    )
    return statements


def edge_existence_statements(spec: EdgeSpec) -> List[str]:
    """
    Existence constraints on a relationship's mandatory properties (Enterprise).

    Property-anchored types are skipped: they are never written as
    relationships.
    """
    if spec.type in PROPERTY_ANCHORED:
        return []
    statements = []
    for prop in sorted(spec.required_names):
        name = _constraint_name("ex_rel", spec.type, prop)
        statements.append(
            f"CREATE CONSTRAINT {name} IF NOT EXISTS "
            f"FOR ()-[r:{spec.type}]-() REQUIRE r.{prop} IS NOT NULL;"
        )
    return statements


def edge_index_statements(spec: EdgeSpec) -> List[str]:
    """Relationship index on the workspace and the source document."""
    if spec.type in PROPERTY_ANCHORED:
        return []
    return [
        f"CREATE INDEX {_constraint_name('ix_rel', spec.type, 'workspace')} IF NOT EXISTS "
        f"FOR ()-[r:{spec.type}]-() ON (r.workspace_id);",
        f"CREATE INDEX {_constraint_name('ix_rel', spec.type, 'source_doc')} IF NOT EXISTS "
        f"FOR ()-[r:{spec.type}]-() ON (r.source_document_id);",
    ]


def unsupported_on_community() -> List[str]:
    """
    Constraints the schema wants that Neo4j Community cannot enforce.

    Returned so the migration reports the gap explicitly. Each of these is
    enforced application-side by :mod:`app.ontology.schema` instead; that is a
    weaker guarantee (a write that bypasses the validator bypasses the check)
    and the deployment should know which it is running under.
    """
    gaps: List[str] = []
    for spec in NODE_TYPES:
        if len(("workspace_id",) + spec.identity) > 1:
            gaps.append(
                f"{spec.label}: composite node key over "
                f"{', '.join(('workspace_id',) + spec.identity)} "
                "(Community supports single-property uniqueness only)"
            )
        for prop in sorted(spec.required_names):
            gaps.append(f"{spec.label}.{prop}: property existence")
    for spec in EDGE_TYPES:
        if spec.type in PROPERTY_ANCHORED:
            continue
        for prop in sorted(spec.required_names):
            gaps.append(f"[:{spec.type}].{prop}: relationship property existence")
    return gaps


def generate_schema_statements(edition: str = "enterprise") -> List[str]:
    """
    Generate the full ordered migration for the current ontology version.

    Args:
        edition: ``"enterprise"`` (default) or ``"community"``.

    Returns:
        Cypher statements, each terminated with a semicolon, ordered
        constraints-before-indexes so an index is never created on a property a
        constraint is about to index anyway.

    Raises:
        ValueError: On an unknown edition.
    """
    if edition not in EDITIONS:
        raise ValueError(f"Unknown edition {edition!r}; expected one of {EDITIONS}")

    enterprise = edition == "enterprise"
    statements: List[str] = [
        f"// Legal ontology schema migration — ontology version {ONTOLOGY_VERSION}",
        f"// Target: Neo4j {edition}. Generated from app/ontology/schema.py — do not hand-edit.",
        "",
        "// ---- Node identity ----",
    ]

    for spec in NODE_TYPES:
        statements.append(node_key_statement(spec) if enterprise else node_unique_statement(spec))

    if enterprise:
        statements += ["", "// ---- Node property existence (provenance invariant) ----"]
        for spec in NODE_TYPES:
            statements += node_existence_statements(spec)

    statements += ["", "// ---- Node indexes ----"]
    for spec in NODE_TYPES:
        statements += node_index_statements(spec)

    if enterprise:
        statements += ["", "// ---- Relationship property existence ----"]
        for spec in EDGE_TYPES:
            statements += edge_existence_statements(spec)

    statements += ["", "// ---- Relationship indexes ----"]
    for spec in EDGE_TYPES:
        statements += edge_index_statements(spec)

    if not enterprise:
        statements += [
            "",
            "// ---- NOT ENFORCED BY THIS EDITION ----",
            "// The following are enforced application-side by app/ontology/schema.py only:",
        ]
        statements += [f"//   {gap}" for gap in unsupported_on_community()]

    return statements


def generate_schema_script(edition: str = "enterprise") -> str:
    """Render :func:`generate_schema_statements` as one runnable ``.cypher`` file."""
    return "\n".join(generate_schema_statements(edition)) + "\n"


def statement_counts(edition: str = "enterprise") -> Dict[str, int]:
    """Count constraints, indexes and comments, for the migration report."""
    counts = {"constraints": 0, "indexes": 0, "comments": 0}
    for line in generate_schema_statements(edition):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("//"):
            counts["comments"] += 1
        elif stripped.startswith("CREATE CONSTRAINT"):
            counts["constraints"] += 1
        elif stripped.startswith("CREATE INDEX"):
            counts["indexes"] += 1
    return counts


if __name__ == "__main__":
    import sys

    edition = sys.argv[1] if len(sys.argv) > 1 else "enterprise"
    sys.stdout.write(generate_schema_script(edition))
