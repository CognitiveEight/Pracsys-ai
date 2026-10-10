"""
Neo4j adapter — the connection that makes the ontology real.

Until this existed, `app/ontology/` produced validated facts that went nowhere
and `app/retrieval/graph_queries.py` produced Cypher nobody ran. This is the
driver layer that closes both ends: it applies the generated schema migration,
writes extraction output, executes the named traversals, and withdraws a
document's facts when it is deleted.

Four properties it enforces, each because the alternative is a silent corruption
rather than a loud failure:

* **The provenance invariant is re-checked at the boundary.** Every node and
  edge is validated against :mod:`app.ontology.schema` again before it is
  written. The extractor already validated it; a caller assembling facts by hand
  has not, and the store is the last place to catch that.
* **Writes are idempotent.** ``MERGE`` on the identity properties, so replaying
  an ingest updates rather than duplicating — which is what makes Keel's
  "re-run from the top on retry" model safe for the graph too.
* **Every statement is workspace-scoped and parameterised.** No value is ever
  interpolated into Cypher, including values that came out of a document.
* **Deletion is exact.** ``withdraw_document`` removes precisely the facts whose
  provenance names that document, and ``DETACH`` so no dangling edge survives.

**Architectural note.** Section 3.2 gives storage mechanics to Keel, so the
long-term home for this write path is Keel's ingestion pipeline. It lives here
because the legal ontology is a SimplerToday concern and this adapter is
deliberately thin — it owns no domain logic, so moving it is a cheap change.

The driver is optional at import time: ``neo4j`` may not be installed in an
environment that only runs the pure-logic layers, and importing this module must
not break them.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from app.ontology.neo4j_ddl import generate_schema_statements
from app.ontology.schema import (
    NODES_BY_LABEL,
    ONTOLOGY_VERSION,
    OntologyViolation,
    validate_edge,
    validate_node,
)
from app.retrieval.graph_queries import build as build_query

try:  # pragma: no cover - exercised by the import-guard test
    from neo4j import AsyncGraphDatabase
    from neo4j.exceptions import Neo4jError

    NEO4J_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncGraphDatabase = None  # type: ignore[assignment]
    Neo4jError = Exception  # type: ignore[assignment,misc]
    NEO4J_AVAILABLE = False


class GraphStoreError(RuntimeError):
    """A graph operation failed."""


@dataclass
class GraphConfig:
    """
    Connection settings for the legal knowledge graph.

    Attributes:
        url: Bolt URL, e.g. ``bolt://localhost:7687``.
        username / password: Credentials. Never logged.
        database: Neo4j database name.
        edition: ``enterprise`` or ``community`` — selects which constraints the
            migration can create. Getting this wrong makes ``apply_schema``
            fail on constraints the edition does not support.
    """

    url: str
    username: str
    password: str
    database: str = "neo4j"
    edition: str = "community"

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("GraphConfig.url is required")


@dataclass
class WriteResult:
    """
    What one write produced.

    ``rejected`` is never empty-by-omission: a fact that fails validation is
    recorded with its reason rather than dropped, for the same reason the
    extractor records its rejections.
    """

    nodes_written: int = 0
    edges_written: int = 0
    rejected: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodesWritten": self.nodes_written,
            "edgesWritten": self.edges_written,
            "rejected": self.rejected,
        }


def _merge_key(value: Any) -> Any:
    """
    Make one identity component safe to MERGE on.

    ``None`` becomes ``""``; everything else passes through untouched. See
    :func:`build_node_merge` for why absent components are normalised rather
    than rejected.
    """
    return "" if value is None else value


def _identity_clause(label: str) -> Tuple[str, Tuple[str, ...]]:
    """
    Build the MERGE key for a label: workspace plus its identity properties.

    Returns:
        ``(cypher_fragment, property_names)``.
    """
    spec = NODES_BY_LABEL[label]
    keys = ("workspace_id",) + spec.identity
    fragment = ", ".join(f"{key}: $identity.{key}" for key in keys)
    return fragment, keys


def build_node_merge(label: str, properties: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Build an idempotent MERGE for one node.

    ``MERGE`` on the identity key then ``SET`` the rest, so replaying an ingest
    updates the node instead of creating a second one. The label is interpolated
    — it cannot be parameterised in Cypher — but it is validated against the
    ontology first, so only the twenty known labels can ever reach the string.

    Args:
        label: A known ontology label.
        properties: The full property map, provenance included.

    Returns:
        ``(cypher, parameters)``.

    Raises:
        OntologyViolation: If the label is unknown or a required property is
            missing.
    """
    validate_node(label, properties)
    fragment, keys = _identity_clause(label)
    # Neo4j refuses to MERGE on a null property, and an absent identity
    # component is the normal case rather than the exception: a Relief of
    # "injunction" has no `quantum`, a Breach the document never dated has no
    # `breach_date`. Rejecting those would discard real legal facts, so an
    # absent component is normalised to the empty string — MERGE stays legal,
    # the identity stays stable across a replay, and a fact that *has* the
    # component remains distinct from one that does not.
    identity = {key: _merge_key(properties.get(key)) for key in keys}
    rest = {k: v for k, v in properties.items() if k not in identity}
    cypher = (
        f"MERGE (n:{label} {{{fragment}}}) "
        "SET n += $properties, n.ontology_version = $ontology_version "
        "RETURN elementId(n) AS element_id"
    )
    return cypher, {
        "identity": identity,
        "properties": rest,
        "ontology_version": ONTOLOGY_VERSION,
    }


def build_edge_merge(
    edge_type: str,
    from_label: str,
    from_key: Mapping[str, Any],
    to_label: str,
    to_key: Mapping[str, Any],
    properties: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Build an idempotent MERGE for one relationship.

    Both endpoints are matched, never created: an edge to a node that does not
    exist is a broken extraction, and silently conjuring the endpoint would hide
    it behind a node with no properties.

    Args:
        edge_type: A known ontology relationship type.
        from_label / to_label: Endpoint labels.
        from_key / to_key: Identity property maps for the endpoints.
        properties: Edge properties, provenance included.

    Returns:
        ``(cypher, parameters)``.

    Raises:
        OntologyViolation: If the type or an endpoint is not permitted.
    """
    validate_edge(edge_type, from_label, to_label, properties)
    from_fragment = ", ".join(f"{k}: $from_key.{k}" for k in from_key)
    to_fragment = ", ".join(f"{k}: $to_key.{k}" for k in to_key)
    cypher = (
        f"MATCH (a:{from_label} {{{from_fragment}}}) "
        f"MATCH (b:{to_label} {{{to_fragment}}}) "
        f"MERGE (a)-[r:{edge_type}]->(b) "
        "SET r += $properties "
        "RETURN elementId(r) AS element_id"
    )
    return cypher, {
        "from_key": dict(from_key),
        "to_key": dict(to_key),
        "properties": dict(properties),
    }


def _endpoint_identity(label: str, key: Any, workspace_id: str) -> Optional[Dict[str, Any]]:
    """
    Turn an extraction edge's ``key`` into an identity map for MATCH.

    Extraction emits a scalar key for a single-identity label (a Party's
    canonical_name, a Statute's name) and a **key map** for a composite-identity
    one — ``app.ontology.extraction._address_endpoint`` resolves the model's
    scalar reference against the nodes of the same pass and hands over the full
    identity. Before that existed, every composite endpoint returned None here
    and the edge was dropped, which orphaned Breach, Relief, Obligation, Event
    and Order from the graph entirely.

    A key map is still checked against the schema: it must cover exactly the
    label's identity, so a stale or partial map is refused rather than matching
    an arbitrary node.
    """
    spec = NODES_BY_LABEL.get(label)
    if spec is None:
        return None

    if isinstance(key, Mapping):
        if set(key) != set(spec.identity):
            return None
        # Absent components were normalised to "" on write; match them the same
        # way or the MATCH silently finds nothing.
        return {"workspace_id": workspace_id, **{k: _merge_key(v) for k, v in key.items()}}

    if key in (None, ""):
        return None
    if len(spec.identity) != 1:
        return None
    return {"workspace_id": workspace_id, spec.identity[0]: key}


class GraphStore:
    """
    Async Neo4j adapter for the legal knowledge graph.

    One instance per process — it owns a driver and therefore a connection pool.
    """

    def __init__(self, config: GraphConfig, driver: Any = None) -> None:
        if driver is None and not NEO4J_AVAILABLE:
            raise GraphStoreError(
                "The 'neo4j' package is not installed; install it or inject a driver"
            )
        self._config = config
        self._driver = driver or AsyncGraphDatabase.driver(
            config.url, auth=(config.username, config.password)
        )

    async def __aenter__(self) -> "GraphStore":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the driver's connection pool."""
        await self._driver.close()

    def _session(self):
        return self._driver.session(database=self._config.database)

    async def _run(self, cypher: str, parameters: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Execute one statement and materialise its rows."""
        try:
            async with self._session() as session:
                result = await session.run(cypher, dict(parameters))
                return [dict(record) async for record in result]
        except Neo4jError as exc:  # pragma: no cover - needs a live server
            raise GraphStoreError(f"Neo4j rejected the statement: {exc}") from exc

    # -- schema ------------------------------------------------------------

    async def apply_schema(self) -> Dict[str, Any]:
        """
        Apply the generated constraints and indexes (work-plan task 2.2).

        Every statement is ``IF NOT EXISTS``, so this is idempotent and safe to
        run on every startup. Statements are applied one at a time and failures
        are collected rather than aborting: one unsupported constraint should
        not leave the schema half-applied.

        Returns:
            ``{applied, failed, edition, ontologyVersion}``.
        """
        statements = [
            s
            for s in generate_schema_statements(self._config.edition)
            if s.strip().startswith("CREATE")
        ]
        applied, failed = 0, []
        for statement in statements:
            try:
                await self._run(statement, {})
                applied += 1
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad: the guarantee this method makes is that one
                # unsupported constraint cannot leave the schema half-applied, and
                # a driver-level error is no less a reason to keep going than a
                # Neo4j one. Every failure is reported, never swallowed.
                failed.append({"statement": statement, "error": str(exc)})

        logger.info(
            "Graph schema (ontology {}): {} of {} statements applied on {}",
            ONTOLOGY_VERSION,
            applied,
            len(statements),
            self._config.edition,
        )
        if failed:
            logger.warning(
                "{} schema statement(s) failed — the graph is running without "
                "those guarantees; application-side validation still applies",
                len(failed),
            )
        return {
            "applied": applied,
            "failed": failed,
            "edition": self._config.edition,
            "ontologyVersion": ONTOLOGY_VERSION,
        }

    # -- writes ------------------------------------------------------------

    async def write_facts(
        self,
        workspace_id: str,
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]] = (),
    ) -> WriteResult:
        """
        Persist extraction output.

        Takes :class:`app.ontology.extraction.ExtractionResult` shapes directly —
        ``{"label", "properties"}`` for nodes and
        ``{"type", "from", "to", "properties"}`` for edges — so the extractor's
        output needs no translation.

        Nodes are written before edges, since an edge MATCHes both endpoints and
        will not create them.

        Args:
            workspace_id: Tenant scope; cross-checked against every fact.
            nodes: Node entries.
            edges: Edge entries.

        Returns:
            A :class:`WriteResult`, with every rejected fact and its reason.
        """
        result = WriteResult()

        for node in nodes:
            label = str(node.get("label") or "")
            properties = dict(node.get("properties") or {})
            if properties.get("workspace_id") != workspace_id:
                result.rejected.append(
                    {"label": label, "reason": "workspace_id does not match the write scope"}
                )
                continue
            try:
                cypher, parameters = build_node_merge(label, properties)
            except (OntologyViolation, KeyError) as exc:
                result.rejected.append({"label": label, "reason": str(exc)})
                continue
            # Per-statement, like apply_schema: a server-side rejection costs
            # the one fact that caused it. Letting it propagate aborted the
            # whole batch, so a single unmergeable node discarded every fact
            # after it *and* every edge — a real ingest lost 26 nodes and 12
            # edges to one null identity property.
            try:
                await self._run(cypher, parameters)
            except GraphStoreError as exc:
                result.rejected.append({"label": label, "reason": str(exc)})
                continue
            result.nodes_written += 1

        for edge in edges:
            edge_type = str(edge.get("type") or "")
            from_ref = edge.get("from") or {}
            to_ref = edge.get("to")
            properties = dict(edge.get("properties") or {})
            if not isinstance(to_ref, Mapping):
                result.rejected.append(
                    {"type": edge_type, "reason": "property-anchored edges are not written"}
                )
                continue
            if properties.get("workspace_id") != workspace_id:
                result.rejected.append(
                    {"type": edge_type, "reason": "workspace_id does not match the write scope"}
                )
                continue

            from_label = str(from_ref.get("label") or "")
            to_label = str(to_ref.get("label") or "")
            from_identity = _endpoint_identity(from_label, from_ref.get("key"), workspace_id)
            to_identity = _endpoint_identity(to_label, to_ref.get("key"), workspace_id)
            if from_identity is None or to_identity is None:
                result.rejected.append(
                    {
                        "type": edge_type,
                        "reason": (
                            "an endpoint could not be addressed by a single identity "
                            "property; composite-identity endpoints need an explicit key map"
                        ),
                    }
                )
                continue

            try:
                cypher, parameters = build_edge_merge(
                    edge_type, from_label, from_identity, to_label, to_identity, properties
                )
            except (OntologyViolation, KeyError) as exc:
                result.rejected.append({"type": edge_type, "reason": str(exc)})
                continue
            try:
                await self._run(cypher, parameters)
            except GraphStoreError as exc:
                result.rejected.append({"type": edge_type, "reason": str(exc)})
                continue
            result.edges_written += 1

        logger.info(
            "Graph write: {} nodes, {} edges, {} rejected (workspace {})",
            result.nodes_written,
            result.edges_written,
            len(result.rejected),
            workspace_id[:12],
        )
        return result

    # -- reads -------------------------------------------------------------

    async def query(self, name: str, workspace_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Execute a named traversal from :mod:`app.retrieval.graph_queries`.

        Args:
            name: Query name, e.g. ``"chronology"``.
            workspace_id: Matter scope.
            limit: Row cap.

        Returns:
            The rows, each carrying its provenance columns.

        Raises:
            KeyError: On an unknown query name.
        """
        cypher, parameters = build_query(name, workspace_id, limit)
        rows = await self._run(cypher, parameters)
        logger.debug("Graph query {} returned {} rows", name, len(rows))
        return rows

    # -- lifecycle ---------------------------------------------------------

    async def withdraw_document(self, workspace_id: str, document_id: str) -> int:
        """
        Remove exactly the facts sourced from one document (task 2.11).

        This is the payoff of the provenance invariant: because every node
        records the document it came from, deletion is a precise query rather
        than a re-derivation. ``DETACH`` so no dangling edge survives to corrupt
        a later traversal.

        Args:
            workspace_id: Matter scope.
            document_id: The deleted document.

        Returns:
            Number of nodes removed.
        """
        counted = await self._run(
            "MATCH (n) WHERE n.workspace_id = $workspace_id "
            "AND n.source_document_id = $document_id RETURN count(n) AS total",
            {"workspace_id": workspace_id, "document_id": document_id},
        )
        total = int(counted[0]["total"]) if counted else 0

        await self._run(
            "MATCH (n) WHERE n.workspace_id = $workspace_id "
            "AND n.source_document_id = $document_id DETACH DELETE n",
            {"workspace_id": workspace_id, "document_id": document_id},
        )
        logger.info(
            "Withdrew {} fact(s) sourced from document {} in workspace {}",
            total,
            document_id,
            workspace_id[:12],
        )
        return total

    async def withdraw_workspace(self, workspace_id: str) -> int:
        """
        Remove every fact belonging to one matter.

        The workspace-scoping invariant's counterpart to
        :meth:`withdraw_document`: because every node carries ``workspace_id``,
        deleting a matter is one query rather than an enumeration of its
        documents — which matters, since the documents are already gone by the
        time a workspace deletion is processed.

        Args:
            workspace_id: The deleted matter.

        Returns:
            Number of nodes removed.
        """
        counted = await self._run(
            "MATCH (n) WHERE n.workspace_id = $workspace_id RETURN count(n) AS total",
            {"workspace_id": workspace_id},
        )
        total = int(counted[0]["total"]) if counted else 0

        await self._run(
            "MATCH (n) WHERE n.workspace_id = $workspace_id DETACH DELETE n",
            {"workspace_id": workspace_id},
        )
        logger.info(
            "Withdrew {} fact(s) for deleted workspace {}", total, workspace_id[:12]
        )
        return total

    async def health(self) -> bool:
        """True when the graph answers. Never raises."""
        try:
            await self._run("RETURN 1 AS ok", {})
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Graph health check failed: {}", exc)
            return False
