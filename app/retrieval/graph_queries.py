"""
Ontology-aware graph queries — the traversals a similarity search cannot do
(work-plan task 3.3).

Section 5.2's argument: chronology is a global ordering across documents,
obligation chains are multi-hop relational questions, and contradictions require
comparing resolved facts. None of those are similarity problems.

Every query here is **parameterised Cypher** — no string interpolation of
values, ever, which is both Keel's stated store convention and the only safe way
to build a query from extracted text. Every query is also **workspace-scoped in
its first predicate**, matching the indexes the generated DDL creates.

Nothing here connects to a database. Each function returns
``(cypher, parameters)`` so the queries are reviewable and testable without
Neo4j running — which is the whole reason this layer can be built before the
knowledge plane is deployed.
"""

from typing import Any, Dict, Tuple

#: Default cap on rows returned by any single traversal. Prevents one pathological
#: matter from blowing the evidence budget before the assembler ever sees it.
DEFAULT_LIMIT = 200

Query = Tuple[str, Dict[str, Any]]


def _base_params(workspace_id: str, limit: int) -> Dict[str, Any]:
    return {"workspace_id": workspace_id, "limit": int(limit)}


def chronology(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Every dated Event, Hearing and Filing, ordered.

    A UNION over the three labels rather than three round-trips: chronology is
    one ordering, and merging it client-side would order each label separately
    and then have to re-sort anyway.

    Uses the ``CALL () { ... }`` variable-scope form. The bare ``CALL { ... }``
    is deprecated as of Neo4j 5.23 — a live 5.26 server warns on every call and
    Neo4j 6 removes it. The empty scope is correct here: the subquery imports no
    variables, only ``$workspace_id``, which is a parameter rather than a
    scoped variable.

    Args:
        workspace_id: Matter scope.
        limit: Row cap.

    Returns:
        ``(cypher, parameters)``.
    """
    cypher = """
    CALL () {
      MATCH (n:Event) WHERE n.workspace_id = $workspace_id AND n.event_date IS NOT NULL
      RETURN n.event_date AS date, n.date_precision AS precision, n.description AS description,
             'Event' AS kind, n.event_type AS subtype,
             n.source_document_id AS source_document_id, n.chunk_id AS chunk_id, n.page AS page
      UNION ALL
      MATCH (n:Hearing) WHERE n.workspace_id = $workspace_id AND n.hearing_date IS NOT NULL
      RETURN n.hearing_date AS date, 'day' AS precision, n.purpose AS description,
             'Hearing' AS kind, n.outcome AS subtype,
             n.source_document_id AS source_document_id, n.chunk_id AS chunk_id, n.page AS page
      UNION ALL
      MATCH (n:Filing) WHERE n.workspace_id = $workspace_id AND n.filed_on IS NOT NULL
      RETURN n.filed_on AS date, 'day' AS precision, n.filing_type AS description,
             'Filing' AS kind, n.filed_by AS subtype,
             n.source_document_id AS source_document_id, n.chunk_id AS chunk_id, n.page AS page
    }
    RETURN date, precision, description, kind, subtype,
           source_document_id, chunk_id, page
    ORDER BY date ASC
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def party_network(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Parties, their roles, and the advocates representing them.

    ``OPTIONAL MATCH`` on the advocate hop: a party with no counsel on record is
    a normal state, and an inner join would silently drop them from the panel.
    """
    cypher = """
    MATCH (p:Party) WHERE p.workspace_id = $workspace_id
    OPTIONAL MATCH (p)-[r:REPRESENTED_BY]->(a:Advocate)
      WHERE a.workspace_id = $workspace_id
    OPTIONAL MATCH (p)-[:IS_PARTY_TO]->(m:Matter)
      WHERE m.workspace_id = $workspace_id
    RETURN p.canonical_name AS party, p.party_role AS role, p.aliases AS aliases,
           p.is_natural_person AS is_natural_person,
           collect(DISTINCT a.canonical_name) AS advocates,
           m.cause_title AS matter,
           p.source_document_id AS source_document_id, p.chunk_id AS chunk_id, p.page AS page
    ORDER BY p.canonical_name
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def obligation_chain(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Who owes what to whom, what breached it, and which provision it engages.

    The multi-hop question from Section 5.2:
    ``Party -[:OWES]-> Obligation -[:OWED_TO]-> Party``, with the breach and the
    governing provision hung off the obligation.
    """
    cypher = """
    MATCH (debtor:Party)-[:OWES]->(o:Obligation)
      WHERE o.workspace_id = $workspace_id AND debtor.workspace_id = $workspace_id
    OPTIONAL MATCH (o)-[:OWED_TO]->(creditor:Party)
      WHERE creditor.workspace_id = $workspace_id
    OPTIONAL MATCH (b:Breach)-[:VIOLATES]->(o)
      WHERE b.workspace_id = $workspace_id
    OPTIONAL MATCH (alleger:Party)-[:ALLEGES]->(b)
      WHERE alleger.workspace_id = $workspace_id
    OPTIONAL MATCH (o)-[:GOVERNED_BY]->(prov:Provision)
      WHERE prov.workspace_id = $workspace_id
    RETURN debtor.canonical_name AS owed_by, creditor.canonical_name AS owed_to,
           o.obligation_type AS obligation, o.due_date AS due_date, o.status AS status,
           b.breach_date AS breach_date, b.severity AS severity,
           alleger.canonical_name AS alleged_by,
           prov.statute_ref AS statute, prov.section_number AS section,
           o.source_document_id AS source_document_id, o.chunk_id AS chunk_id, o.page AS page
    ORDER BY o.due_date ASC
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def authority_chain(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Precedents relied on, and what relied on them.

    ``CITES`` runs from either a Filing or an Order, so the match is on the
    relationship rather than on a specific tail label.
    """
    cypher = """
    MATCH (source)-[:CITES]->(prec:Precedent)
      WHERE prec.workspace_id = $workspace_id AND source.workspace_id = $workspace_id
    RETURN prec.case_title AS case_title, prec.citation AS citation, prec.court AS court,
           prec.decision_date AS decision_date, prec.binding_strength AS binding_strength,
           labels(source)[0] AS cited_by_type,
           coalesce(source.filing_type, source.operative_direction) AS cited_by,
           prec.source_document_id AS source_document_id, prec.chunk_id AS chunk_id,
           prec.page AS page
    ORDER BY prec.decision_date DESC
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def provisions(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Statutory provisions engaged, with their containing act.

    Filters out ``resolution_status = 'unverified'`` rows by default at the
    caller's discretion — they are returned here with the status attached so the
    panel can decide whether to show them flagged or not at all.
    """
    cypher = """
    MATCH (p:Provision) WHERE p.workspace_id = $workspace_id
    OPTIONAL MATCH (p)-[:CONTAINED_IN]->(s:Statute)
      WHERE s.workspace_id = $workspace_id
    OPTIONAL MATCH (subject)-[:GOVERNED_BY]->(p)
      WHERE subject.workspace_id = $workspace_id
    RETURN p.section_number AS section, p.heading AS heading,
           p.resolution_status AS resolution_status,
           coalesce(s.short_title, p.statute_ref) AS act, s.framework AS framework,
           collect(DISTINCT labels(subject)[0]) AS engaged_by,
           p.source_document_id AS source_document_id, p.chunk_id AS chunk_id, p.page AS page
    ORDER BY act, section
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def orders(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """Orders passed, the forum that passed them, and what they superseded."""
    cypher = """
    MATCH (o:Order) WHERE o.workspace_id = $workspace_id
    OPTIONAL MATCH (f:Forum)-[:PASSED_ORDER]->(o) WHERE f.workspace_id = $workspace_id
    OPTIONAL MATCH (o)-[:SUPERSEDES]->(prior:Order) WHERE prior.workspace_id = $workspace_id
    RETURN o.order_date AS order_date, o.operative_direction AS direction,
           o.complied AS complied, f.canonical_name AS forum,
           prior.order_date AS supersedes_order_dated,
           o.source_document_id AS source_document_id, o.chunk_id AS chunk_id, o.page AS page
    ORDER BY o.order_date DESC
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def reliefs(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """Reliefs sought, by whom, and whether granted."""
    cypher = """
    MATCH (p:Party)-[:SEEKS]->(r:Relief)
      WHERE r.workspace_id = $workspace_id AND p.workspace_id = $workspace_id
    RETURN p.canonical_name AS sought_by, r.relief_type AS relief,
           r.quantum AS quantum, r.granted AS granted,
           r.source_document_id AS source_document_id, r.chunk_id AS chunk_id, r.page AS page
    ORDER BY p.canonical_name
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def contradictions(workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Machine-detected conflicts between documents.

    Matches the CONTRADICTS edges written by
    :mod:`app.ontology.contradictions`. Undirected on purpose — a contradiction
    has no natural direction, and matching one way would report half of them.
    """
    cypher = """
    MATCH (a)-[c:CONTRADICTS]-(b)
      WHERE c.workspace_id = $workspace_id
    RETURN DISTINCT c.conflict_kind AS conflict_kind, c.detector AS detector,
           labels(a)[0] AS left_type, labels(b)[0] AS right_type,
           c.source_document_id AS source_document_id, c.chunk_id AS chunk_id, c.page AS page,
           c.extraction_confidence AS confidence
    ORDER BY conflict_kind
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def existing_entity_names(workspace_id: str, limit: int = 5000) -> Query:
    """
    Canonical names already in the graph, for cross-ingest entity reconciliation.

    Read before writing a new batch so a spelling arriving today resolves onto
    the entity created last week — see
    :func:`app.ontology.entity_resolution.reconcile_with_existing`. Without it,
    resolution is per-ingest and one party becomes several nodes across uploads.
    """
    cypher = """
    MATCH (n) WHERE n.workspace_id = $workspace_id AND n.canonical_name IS NOT NULL
    RETURN DISTINCT labels(n)[0] AS label, n.canonical_name AS canonical_name
    LIMIT $limit
    """
    return cypher.strip(), _base_params(workspace_id, limit)


def facts_for_document(workspace_id: str, document_id: str) -> Query:
    """
    Every node sourced from one document.

    The withdrawal query for task 2.11: deleting a document must retract exactly
    its dependent facts, and provenance is what makes that exact.
    """
    cypher = """
    MATCH (n) WHERE n.workspace_id = $workspace_id AND n.source_document_id = $document_id
    RETURN labels(n)[0] AS label, elementId(n) AS element_id, n.chunk_id AS chunk_id
    """
    return cypher.strip(), {"workspace_id": workspace_id, "document_id": document_id}


def withdraw_document_facts(workspace_id: str, document_id: str) -> Query:
    """
    Detach-delete every fact sourced from one document.

    ``DETACH DELETE`` so a node's relationships go with it — leaving dangling
    edges would corrupt every traversal that crosses them. Task 2.11.
    """
    cypher = """
    MATCH (n) WHERE n.workspace_id = $workspace_id AND n.source_document_id = $document_id
    DETACH DELETE n
    """
    return cypher.strip(), {"workspace_id": workspace_id, "document_id": document_id}


#: Queries that are internal lookups rather than evidence. They are never
#: rendered into a prompt and carry no provenance columns, because nothing
#: cites them — they exist to make a decision, not to support a claim.
LOOKUP_QUERIES = frozenset({"existing_entity_names"})

#: Name -> builder, so a profile's ``graph_query`` string resolves to a callable.
GRAPH_QUERIES = {
    "chronology": chronology,
    "party_network": party_network,
    "obligation_chain": obligation_chain,
    "authority_chain": authority_chain,
    "provisions": provisions,
    "orders": orders,
    "reliefs": reliefs,
    "contradictions": contradictions,
    "existing_entity_names": existing_entity_names,
}


#: Queries whose rows become citable evidence. Every one returns provenance.
EVIDENCE_QUERIES = frozenset(GRAPH_QUERIES) - LOOKUP_QUERIES


def build(name: str, workspace_id: str, limit: int = DEFAULT_LIMIT) -> Query:
    """
    Build a named graph query.

    Args:
        name: Key in :data:`GRAPH_QUERIES`.
        workspace_id: Matter scope.
        limit: Row cap.

    Returns:
        ``(cypher, parameters)``.

    Raises:
        KeyError: On an unknown query name.
    """
    try:
        builder = GRAPH_QUERIES[name]
    except KeyError:
        raise KeyError(
            f"Unknown graph query {name!r}; known: {', '.join(sorted(GRAPH_QUERIES))}"
        ) from None
    return builder(workspace_id, limit)
