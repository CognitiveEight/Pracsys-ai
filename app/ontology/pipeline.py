"""
The ingest bridge — documents in, knowledge graph out.

This is the composition point the Phase 1/2 tasks converge on. Everything it
calls is already built and tested in isolation; this is what makes them a
pipeline rather than a collection:

``evidence chunks -> extract (2.3) -> resolve entities (2.4) -> normalise statutes
(2.5/2.6) -> normalise dates (2.8) -> reconcile against the graph (2.4) ->
withdraw the document's old facts -> write (2.2) -> detect contradictions (2.9)``

**Chunks come from the evidence store** (``vector_store.get_document_chunks``),
not from a separate chunker: every fact carries the same ``chunk_id``
(``{doc}_chunk_{i}``) and physical ``page`` as the passage the citation checker
quotes, so a graph fact and a cited passage always point at the same text.

The reconciliation step is easy to miss and load-bearing. Extraction resolves
entities *within* one pass; documents arrive across many. Without a read of the
names already in the graph, "M/s Meridian Traders" uploaded today becomes a
second node beside the "Meridian Traders Private Limited" created last week —
one party, two nodes, and every traversal that crosses them is wrong.

Three properties, each chosen because the alternative fails quietly:

* **Every stage is optional and reports its absence.** No graph configured means
  extraction still runs and its output is returned for inspection — it simply
  is not persisted. A pipeline that refused to run without Neo4j would make the
  logic layers untestable against real documents.
* **Ingest never fails the socket flow.** Document processing, the assessments
  emit and the feature suite must proceed even if extraction breaks. Every
  failure is caught, reported in the result, and logged — never raised into the
  handler.
* **The whole pass runs inside one usage scope.** Extraction is a per-chunk LLM
  call and is easily the most expensive thing here; billing it to nobody is the
  exact bug ``app/ai/usage.py`` exists to prevent.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.ai.usage import meter_usage
from app.ontology.contradictions import detect_contradictions
from app.ontology.entity_resolution import reconcile_with_existing
from app.ontology.extraction import extract_facts

#: Concurrent extraction calls across ALL documents being ingested — a large
#: upload must not open hundreds of model calls at once and be rate-limited.
EXTRACTION_CONCURRENCY = 4
_extraction_slots: Optional[asyncio.Semaphore] = None


def _slots() -> asyncio.Semaphore:
    global _extraction_slots
    if _extraction_slots is None:
        _extraction_slots = asyncio.Semaphore(EXTRACTION_CONCURRENCY)
    return _extraction_slots


@dataclass
class IngestReport:
    """What one ingest pass did (returned, and logged as one summary line)."""

    documents: int = 0
    chunks: int = 0
    extraction: Dict[str, Any] = field(default_factory=dict)
    nodes_written: int = 0
    edges_written: int = 0
    withdrawn: int = 0
    contradictions: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "extraction": self.extraction,
            "nodesWritten": self.nodes_written,
            "edgesWritten": self.edges_written,
            "withdrawn": self.withdrawn,
            "contradictions": self.contradictions,
            "errors": self.errors,
            "skipped": self.skipped,
        }


def chunks_for_extraction(stored: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Evidence-store chunk payloads in the shape ``extract_facts`` reads."""
    out = []
    for payload in stored:
        meta = payload.get("metadata") or {}
        text = str(payload.get("content") or "").strip()
        if not text or meta.get("is_low_content_marker"):
            continue
        out.append(
            {
                "chunk_id": str(payload.get("document_id") or ""),
                "document_id": str(payload.get("original_document_id") or ""),
                "text": text,
                "page": meta.get("page_start"),
            }
        )
    return out


async def _extract_responses(chunks: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Run the extraction prompt per chunk; ``chunk_id -> raw JSON text``.

    LOW thinking at temperature 0: the schema does the constraining, and a
    model reasoning at length tends to infer facts the passage does not state
    (rule 1 of the extraction prompt). Each call is metered and logged as an
    ``llm_call`` by the client.
    """
    from app.ai.gemini_client import gemini_client, is_generation_error
    from app.ontology.extraction import build_extraction_prompt

    system = build_extraction_prompt()
    responses: Dict[str, str] = {}

    async def one(chunk: Mapping[str, Any]) -> None:
        async with _slots():
            raw = await gemini_client.generate(
                prompt=str(chunk.get("text") or ""),
                system_instruction=system,
                temperature=0.0,
                thinking_level="LOW",
                response_mime_type="application/json",
            )
            responses[str(chunk["chunk_id"])] = "" if is_generation_error(raw) else raw

    await asyncio.gather(*(one(c) for c in chunks))
    return responses


@meter_usage
async def ingest_to_knowledge_plane(
    workspace_id: str,
    graph_store: Any = None,
    document_ids: Optional[Sequence[str]] = None,
    chunks: Optional[Sequence[Mapping[str, Any]]] = None,
    complete: Optional[Callable[[str, str], str]] = None,
) -> IngestReport:
    """Extract ontology facts for documents and write them to the graph.

    Args:
        workspace_id: Matter scope, written onto every fact.
        graph_store: A connected ``GraphStore``, or None to extract without
            persisting (the report says so).
        document_ids: Documents to (re)ingest. Their previous facts are
            withdrawn first, so a re-read document never keeps stale facts.
        chunks: Pre-built extraction chunks (tests); default reads the
            evidence store.
        complete: Override the extraction LLM (tests).

    Returns:
        An :class:`IngestReport`. Never raises — ingest must not break the
        socket flow; failures are recorded and logged.
    """
    report = IngestReport(documents=len(document_ids or []))
    try:
        if chunks is None:
            from app.vectorstore.qdrant import vector_store

            stored = await vector_store.get_document_chunks(
                workspace_id, list(document_ids) if document_ids else None
            )
            chunks = chunks_for_extraction(stored)
        chunks = list(chunks)
    except Exception as exc:  # noqa: BLE001
        report.errors.append({"stage": "read_chunks", "error": str(exc)})
        logger.opt(exception=exc).warning("Ontology ingest could not read evidence chunks")
        return report
    report.chunks = len(chunks)
    if not report.documents:
        report.documents = len({c["document_id"] for c in chunks})
    if not chunks:
        report.skipped.append("no evidence chunks for these documents")
        logger.warning("Ontology ingest: no evidence chunks found; nothing to extract")
        return report

    # --- extract -------------------------------------------------------------
    try:
        if complete is None:
            responses = await _extract_responses(chunks)
            extracted = extract_facts(
                chunks,
                complete=lambda system, user, _r=responses: _r.get(_chunk_id_for(chunks, user), ""),
                workspace_id=workspace_id,
            )
        else:
            extracted = extract_facts(chunks, complete=complete, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        report.errors.append({"stage": "extraction", "error": str(exc)})
        logger.opt(exception=exc).error("Ontology extraction failed")
        return report
    _refine_fact_pages(extracted.nodes, chunks)
    report.extraction = {
        **extracted.stats,
        "rejectionRate": extracted.rejection_rate,
        "rejected": extracted.rejected[:20],
    }

    if graph_store is None:
        report.skipped.append("no graph configured; extracted facts were not persisted")
        return report

    # --- replace this batch's documents ---------------------------------------
    try:
        for doc_id in sorted({c["document_id"] for c in chunks}):
            report.withdrawn += int(await graph_store.withdraw_document(workspace_id, doc_id) or 0)
    except Exception as exc:  # noqa: BLE001
        report.errors.append({"stage": "withdraw_previous", "error": str(exc)})
        logger.opt(exception=exc).warning("Could not withdraw previous facts; writing anyway")

    # --- reconcile against what is already in the graph ----------------------
    try:
        nodes, edges = await _reconcile_against_graph(
            workspace_id, extracted.nodes, extracted.edges, graph_store
        )
    except Exception as exc:  # noqa: BLE001 - reconciliation improves, it does not gate
        report.errors.append({"stage": "reconciliation", "error": str(exc)})
        logger.warning("Entity reconciliation failed; writing unreconciled: {}", exc)
        nodes, edges = extracted.nodes, extracted.edges

    try:
        written = await graph_store.write_facts(workspace_id, nodes, edges)
        report.nodes_written = written.nodes_written
        report.edges_written = written.edges_written
        if written.rejected:
            report.errors.extend(
                {"stage": "graph_write", "error": str(item)} for item in written.rejected[:20]
            )
    except Exception as exc:  # noqa: BLE001
        report.errors.append({"stage": "graph_write", "error": str(exc)})
        logger.opt(exception=exc).error("Graph write failed")
        return report

    # --- contradictions --------------------------------------------------------
    try:
        detected = detect_contradictions(workspace_id, **_facts_for_contradictions(nodes))
        if detected["edges"]:
            conflict_write = await graph_store.write_facts(workspace_id, nodes=[], edges=detected["edges"])
            report.edges_written += conflict_write.edges_written
        report.contradictions = detected["total"]
    except Exception as exc:  # noqa: BLE001
        report.errors.append({"stage": "contradictions", "error": str(exc)})
        logger.warning("Contradiction detection failed: {}", exc)

    logger.info(
        "Ontology ingest: {} chunk(s) -> {} node(s), {} edge(s), {} contradiction(s), "
        "{} previous fact(s) replaced, {} error(s)",
        report.chunks,
        report.nodes_written,
        report.edges_written,
        report.contradictions,
        report.withdrawn,
        len(report.errors),
    )
    return report


_PAGE_MARKER = re.compile(r"---\s*Page\s+(\d+)\s*---", re.I)
#: Properties whose value is a phrase from the passage — where the fact "is".
_ANCHOR_PROPS = ("canonical_name", "description", "reference", "relief_type", "title", "name")


def _refine_fact_pages(nodes: Sequence[Dict[str, Any]], chunks: Sequence[Mapping[str, Any]]) -> None:
    """Set each fact's page to the page its own text is on.

    A chunk can span pages, and extraction stamps the chunk's FIRST page. The
    fact's anchor text (a party name, an event description) is located in the
    chunk and the nearest ``--- Page N ---`` marker before it wins, so a fact
    from page 3 of a chunk starting on page 1 says page 3.
    """
    text_by_chunk = {str(c.get("chunk_id")): str(c.get("text") or "") for c in chunks}
    for node in nodes:
        props = node.get("properties") or {}
        text = text_by_chunk.get(str(props.get("chunk_id") or ""))
        if not text:
            continue
        lowered = text.lower()
        for key in _ANCHOR_PROPS:
            needle = str(props.get(key) or "").strip().lower()
            at = lowered.find(needle[:80]) if len(needle) >= 3 else -1
            if at >= 0:
                markers = [m for m in _PAGE_MARKER.finditer(text) if m.start() <= at]
                if markers:
                    props["page"] = int(markers[-1].group(1))
                break


def schedule_document_ingest(workspace_id: str, document_ids: Sequence[str]) -> None:
    """Queue ontology extraction for freshly stored documents.

    Called after a document's chunks are written. No-op unless
    ``ONTOLOGY_INGEST_ENABLED`` and a graph is connected. Runs as a tracked
    background task, so it inherits the event's log context and run scope — a
    ``workspace_stop_request`` cancels it like any other work of that run.
    """
    from app.core.config import settings

    if not settings.ontology_ingest_enabled or not document_ids:
        return
    from app.socket.events import event_handler

    graph_store = getattr(event_handler, "_graph_store", None)
    if graph_store is None:
        return
    try:
        event_handler._track_background_task(
            asyncio.create_task(
                ingest_to_knowledge_plane(
                    workspace_id, graph_store=graph_store, document_ids=list(document_ids)
                )
            ),
            label="ontology ingest",
        )
    except RuntimeError as exc:  # no running loop
        logger.warning("Could not schedule ontology ingest: {}", exc)


def _facts_for_contradictions(
    nodes: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Project written nodes into the shapes the four detectors expect."""
    events, claims, obligations, parties = [], [], [], []
    for node in nodes:
        label = node.get("label")
        props = node.get("properties") or {}
        provenance = {
            "source_document_id": props.get("source_document_id"),
            "chunk_id": props.get("chunk_id"),
            "page": props.get("page"),
            "confidence": props.get("extraction_confidence"),
        }
        if label == "Event":
            events.append(
                {
                    "description": props.get("description"),
                    "date": props.get("event_date"),
                    **provenance,
                }
            )
        elif label == "Relief":
            claims.append(
                {"subject": props.get("relief_type"), "amount": props.get("quantum"), **provenance}
            )
        elif label == "Transaction":
            claims.append(
                {"subject": props.get("reference"), "amount": props.get("amount"), **provenance}
            )
        elif label == "Obligation":
            obligations.append(
                {
                    "subject": props.get("obligation_type"),
                    "status": props.get("status"),
                    **provenance,
                }
            )
        elif label == "Party":
            parties.append(
                {
                    "canonical_name": props.get("canonical_name"),
                    "party_role": props.get("party_role"),
                    **provenance,
                }
            )
    return {"events": events, "claims": claims, "obligations": obligations, "parties": parties}


async def _reconcile_against_graph(
    workspace_id: str,
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    graph_store: Any,
) -> tuple:
    """
    Rewrite this batch's entity names onto entities already in the graph.

    Reads the workspace's existing canonical names, maps each new surface form
    onto one where it matches, and applies the mapping to both node properties
    and edge endpoint keys — an edge left pointing at the old spelling would
    MATCH nothing and be rejected at write time.

    Args:
        workspace_id: Matter scope.
        nodes / edges: Extraction output.
        graph_store: The store to read existing names from.

    Returns:
        ``(nodes, edges)`` with names reconciled. Returns the input unchanged
        when the graph holds no entities yet.
    """
    resolved_labels = ("Party", "Advocate", "Forum")
    rows = await graph_store.query("existing_entity_names", workspace_id)
    existing = [
        str(row.get("canonical_name"))
        for row in rows
        if row.get("canonical_name") and row.get("label") in resolved_labels
    ]
    if not existing:
        return list(nodes), list(edges)

    incoming = [
        str(n["properties"]["canonical_name"])
        for n in nodes
        if n.get("label") in resolved_labels and (n.get("properties") or {}).get("canonical_name")
    ]
    mapping = reconcile_with_existing(incoming, existing)
    if not mapping:
        return list(nodes), list(edges)

    reconciled_nodes = []
    for node in nodes:
        if node.get("label") in resolved_labels:
            properties = dict(node.get("properties") or {})
            name = properties.get("canonical_name")
            if name in mapping:
                properties["canonical_name"] = mapping[name]
                node = {**node, "properties": properties}
        reconciled_nodes.append(node)

    reconciled_edges = []
    for edge in edges:
        updated = dict(edge)
        for end in ("from", "to"):
            ref = updated.get(end)
            if isinstance(ref, Mapping) and ref.get("label") in resolved_labels:
                key = ref.get("key")
                if key in mapping:
                    updated[end] = {**ref, "key": mapping[key]}
        reconciled_edges.append(updated)

    return reconciled_nodes, reconciled_edges


def _chunk_id_for(chunks: Sequence[Mapping[str, Any]], text: str) -> str:
    """Map an extractor callback's user text back to its chunk id."""
    for chunk in chunks:
        if str(chunk.get("text")) == text:
            return str(chunk.get("chunk_id"))
    return ""
