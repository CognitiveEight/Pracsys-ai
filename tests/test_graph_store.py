"""Neo4j adapter — the connection that makes the ontology real."""

from typing import Any, Dict, List

import pytest

from app.graph.store import (
    GraphConfig,
    GraphStore,
    GraphStoreError,
    build_edge_merge,
    build_node_merge,
)
from app.ontology.schema import ONTOLOGY_VERSION, OntologyViolation

PROV = {
    "workspace_id": "ws1",
    "source_document_id": "d1",
    "chunk_id": "c1",
    "page": 2,
    "extraction_confidence": 0.9,
}


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


class FakeSession:
    def __init__(self, driver): self._driver = driver
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return None
    async def run(self, cypher, params):
        self._driver.calls.append((cypher, params))
        return FakeResult(self._driver.rows_for(cypher))


class FakeDriver:
    """Records every statement so the Cypher itself can be asserted on."""

    def __init__(self, rows=None):
        self.calls: List[Any] = []
        self._rows = rows or {}
        self.closed = False

    def session(self, database=None): return FakeSession(self)
    def rows_for(self, cypher):
        for needle, rows in self._rows.items():
            if needle in cypher:
                return rows
        return []
    async def close(self): self.closed = True


def _store(driver=None, edition="community"):
    return GraphStore(
        GraphConfig(url="bolt://x", username="u", password="p", edition=edition),
        driver=driver or FakeDriver(),
    )


# --- statement construction -------------------------------------------------


def test_node_merge_is_idempotent_on_identity():
    cypher, params = build_node_merge("Party", {"canonical_name": "X Ltd", **PROV})
    assert cypher.startswith("MERGE (n:Party")
    assert params["identity"] == {"workspace_id": "ws1", "canonical_name": "X Ltd"}
    assert params["ontology_version"] == ONTOLOGY_VERSION


def test_node_merge_revalidates_the_provenance_invariant():
    """The extractor already checked; a hand-assembled fact has not."""
    with pytest.raises(OntologyViolation, match="chunk_id"):
        build_node_merge("Party", {"canonical_name": "X", "workspace_id": "ws1",
                                   "source_document_id": "d", "extraction_confidence": 0.5})


def test_node_merge_rejects_an_unknown_label():
    with pytest.raises(OntologyViolation, match="Unknown node label"):
        build_node_merge("Litigant", {**PROV})


def test_no_document_value_is_interpolated_into_cypher():
    """Values from extracted text must never reach the query string."""
    cypher, params = build_node_merge(
        "Party", {"canonical_name": "X'); DROP DATABASE neo4j; //", **PROV}
    )
    assert "DROP DATABASE" not in cypher
    assert params["identity"]["canonical_name"] == "X'); DROP DATABASE neo4j; //"


def test_edge_merge_matches_both_endpoints_and_never_creates_them():
    cypher, _ = build_edge_merge(
        "IS_PARTY_TO", "Party", {"workspace_id": "ws1", "canonical_name": "X"},
        "Matter", {"workspace_id": "ws1", "matter_id": "m1"}, PROV,
    )
    assert cypher.count("MATCH") == 2
    assert "MERGE (a)-[r:IS_PARTY_TO]->(b)" in cypher
    assert "MERGE (a:" not in cypher, "an endpoint must never be conjured"


def test_edge_merge_enforces_permitted_endpoints():
    with pytest.raises(OntologyViolation, match="tail may be"):
        build_edge_merge("IS_PARTY_TO", "Forum", {"workspace_id": "ws1"},
                         "Matter", {"workspace_id": "ws1"}, PROV)


# --- schema -----------------------------------------------------------------


async def test_apply_schema_runs_only_create_statements():
    driver = FakeDriver()
    report = await _store(driver).apply_schema()
    assert report["applied"] > 0
    assert all(c[0].strip().startswith("CREATE") for c in driver.calls)
    assert report["ontologyVersion"] == ONTOLOGY_VERSION


async def test_apply_schema_collects_failures_rather_than_aborting():
    class Flaky(FakeDriver):
        def rows_for(self, cypher):
            if "Party" in cypher:
                raise RuntimeError("unsupported constraint")
            return []

    driver = Flaky()
    report = await _store(driver).apply_schema()
    assert report["applied"] > 0, "one failure must not halt the migration"
    assert report["failed"], "and the failure must be reported"


# --- writes -----------------------------------------------------------------


async def test_write_facts_persists_valid_nodes_and_edges():
    driver = FakeDriver()
    result = await _store(driver).write_facts(
        "ws1",
        nodes=[
            {"label": "Party", "properties": {"canonical_name": "X Ltd", **PROV}},
            {"label": "Matter", "properties": {"matter_id": "m1", **PROV}},
        ],
        edges=[{"type": "IS_PARTY_TO",
                "from": {"label": "Party", "key": "X Ltd"},
                "to": {"label": "Matter", "key": "m1"},
                "properties": {**PROV}}],
    )
    assert result.nodes_written == 2 and result.edges_written == 1
    assert result.rejected == []


async def test_a_fact_for_another_workspace_is_rejected():
    result = await _store().write_facts(
        "ws1", nodes=[{"label": "Party",
                       "properties": {"canonical_name": "X", **{**PROV, "workspace_id": "ws2"}}}]
    )
    assert result.nodes_written == 0
    assert "workspace_id does not match" in result.rejected[0]["reason"]


async def test_an_invalid_node_is_rejected_with_its_reason():
    result = await _store().write_facts(
        "ws1", nodes=[{"label": "Party", "properties": {**PROV}}]
    )
    assert result.nodes_written == 0
    assert "canonical_name" in result.rejected[0]["reason"]


async def test_extraction_output_writes_without_translation():
    """The extractor's shape is the store's input shape."""
    import json
    from app.ontology.extraction import extract_facts

    extracted = extract_facts(
        [{"chunk_id": "c1", "document_id": "d1", "text": "t", "page": 1}],
        lambda s, u: json.dumps({"nodes": [
            {"label": "Party", "properties": {"canonical_name": "X Ltd"}, "confidence": 0.9}],
            "edges": []}),
        workspace_id="ws1",
    )
    result = await _store().write_facts("ws1", extracted.nodes, extracted.edges)
    assert result.nodes_written == 1 and result.rejected == []


async def test_composite_identity_endpoint_is_reported_not_mismatched():
    """An Event's identity is three properties; a scalar key cannot address it."""
    result = await _store().write_facts(
        "ws1",
        nodes=[],
        edges=[{"type": "PRECEDES", "from": {"label": "Event", "key": "e1"},
                "to": {"label": "Event", "key": "e2"}, "properties": {**PROV}}],
    )
    assert result.edges_written == 0
    assert "single identity property" in result.rejected[0]["reason"]


# --- reads and lifecycle ----------------------------------------------------


async def test_query_executes_a_named_traversal():
    driver = FakeDriver(rows={"MATCH (p:Party)": [{"party": "X Ltd"}]})
    rows = await _store(driver).query("party_network", "ws1")
    assert rows == [{"party": "X Ltd"}]
    assert driver.calls[0][1]["workspace_id"] == "ws1"


async def test_unknown_query_is_refused():
    with pytest.raises(KeyError, match="Unknown graph query"):
        await _store().query("nope", "ws1")


async def test_withdraw_document_counts_then_detach_deletes():
    """Task 2.11: deletion must be exact and leave no dangling edge."""
    driver = FakeDriver(rows={"count(n)": [{"total": 7}]})
    removed = await _store(driver).withdraw_document("ws1", "d1")
    assert removed == 7
    statements = [c[0] for c in driver.calls]
    assert any("DETACH DELETE" in s for s in statements)
    assert all(c[1] == {"workspace_id": "ws1", "document_id": "d1"} for c in driver.calls)


async def test_health_never_raises():
    class Dead(FakeDriver):
        def rows_for(self, cypher): raise RuntimeError("no server")

    assert await _store(Dead()).health() is False


async def test_close_closes_the_driver():
    driver = FakeDriver()
    store = _store(driver)
    await store.close()
    assert driver.closed


# --- regressions from the first real matter (BCCI, Test Case 4) --------------


def test_absent_identity_component_is_merge_safe():
    """
    Neo4j refuses to MERGE on null, and an absent identity component is the
    normal case: a Breach the document never dated, an injunction with no
    quantum. A real ingest died here with
    ``Cannot merge ... null property value for 'breach_date'``.
    """
    _, params = build_node_merge(
        "Breach",
        {"alleged_by": "Anonymous Observer", "breach_date": None,
         "description": "Misuse of bone density tests", **PROV},
    )
    assert params["identity"] == {
        "workspace_id": "ws1", "alleged_by": "Anonymous Observer", "breach_date": "",
    }
    assert None not in params["identity"].values()


def test_a_present_component_stays_distinct_from_an_absent_one():
    """Normalising to "" must not collapse two genuinely different facts."""
    _, dated = build_node_merge(
        "Breach", {"alleged_by": "A", "breach_date": "2025-11-30", **PROV}
    )
    _, undated = build_node_merge(
        "Breach", {"alleged_by": "A", "breach_date": None, **PROV}
    )
    assert dated["identity"] != undated["identity"]


async def test_one_rejected_node_does_not_abort_the_batch():
    """
    A server-side rejection cost the *whole* batch: one unmergeable node
    discarded every fact after it and every edge. A real ingest lost 26 nodes
    and 12 edges to a single null property.
    """
    from neo4j.exceptions import ClientError

    class Picky(FakeDriver):
        """Rejects the Breach statement the way a real 5.26 server did."""

        def session(self, database=None):
            outer = self

            class S(FakeSession):
                async def run(self, cypher, params):
                    outer.calls.append((cypher, params))
                    if "Breach" in cypher:
                        raise ClientError(
                            "Cannot merge the following node because of null "
                            "property value for 'breach_date'"
                        )
                    return FakeResult([])
            return S(outer)

    driver = Picky()
    result = await _store(driver).write_facts(
        "ws1",
        [
            {"label": "Party", "properties": {"canonical_name": "First", **PROV}},
            {"label": "Breach", "properties": {"alleged_by": "X", "breach_date": None, **PROV}},
            {"label": "Party", "properties": {"canonical_name": "Third", **PROV}},
        ],
        [],
    )
    # The good nodes on *both* sides of the failure survive.
    assert result.nodes_written == 2
    assert len(result.rejected) == 1
    assert result.rejected[0]["label"] == "Breach"
