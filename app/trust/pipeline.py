"""
The trust pipeline's orchestration: store, recompute, project, serve.

* :func:`run_trust_pass` — after ontology extraction of some documents: store
  their evidence and profiles, then recompute the case.
* :func:`recompute_workspace` — rebuild every fact, conflict and review item
  from the stored evidence (stages 1, 3-6), write them, and project trust onto
  the graph when one is connected.
* :func:`on_documents_deleted` — the vector store's post-delete hook.
* :func:`conflict_block` / :func:`unresolved_conflicts` — what panels and chat
  read.
* :func:`resolve_review` — record a reviewer's decision and recompute.

Passes for one case are serialised by a per-workspace lock: two documents of
one upload finish extraction concurrently, and a recompute reading the evidence
while the other writes would route over half the case.

Nothing here raises into the socket flow. The pipeline improves facts; it
never gates a panel or an upload.
"""

import asyncio
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.trust import store
from app.trust.claims import evidence_for_entries
from app.trust.evaluate import TrustParams, evaluate_case
from app.trust.relevance import relevance_scores
from app.trust.router import (
    CONFLICT_ACTIONS,
    FACT_ACTIONS,
    STATUS_CONTESTED,
    STATUS_DROPPED,
    STATUS_TRUSTED,
    conflict_signature,
    fact_signature,
)

#: CONTRADICTS edges written by this pipeline carry this ``detector``.
GRAPH_DETECTOR = "trust"

_STATUS_RANK = {STATUS_DROPPED: 0, STATUS_CONTESTED: 1, STATUS_TRUSTED: 2}

_locks: Dict[str, asyncio.Lock] = {}


def _lock(workspace_id: str) -> asyncio.Lock:
    return _locks.setdefault(workspace_id, asyncio.Lock())


def enabled() -> bool:
    from app.core.config import settings

    return bool(settings.trust_pipeline_enabled)


def _summary_pages(chunks: Sequence[Mapping[str, Any]]) -> Dict[str, frozenset]:
    """``document_id -> paper-book summary pages`` (index, list of dates, synopsis).

    Uses ``app.rag.page_kinds.avoid_pages`` where it exists; without it no page
    is treated as a summary and the weighting simply does not apply.
    """
    try:
        from app.rag.page_kinds import avoid_pages  # type: ignore[import-not-found]
    except ImportError:
        return {}
    texts: Dict[str, List[str]] = {}
    for chunk in chunks:
        texts.setdefault(str(chunk.get("document_id")), []).append(str(chunk.get("text") or ""))
    out = {}
    for doc_id, parts in texts.items():
        try:
            out[doc_id] = frozenset(avoid_pages("\n".join(parts)))
        except Exception:  # noqa: BLE001 - a weighting hint, never a failure
            out[doc_id] = frozenset()
    return out


async def run_trust_pass(
    workspace_id: str,
    chunks: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    graph_store: Any = None,
    rewrite_nodes: bool = False,
) -> Dict[str, Any]:
    """Store one extraction batch's evidence and recompute the case.

    Args:
        workspace_id: The case.
        chunks: The extraction chunks (``chunk_id``, ``document_id``, ``text``).
        nodes / edges: ``extract_facts`` output, hedged and denied included —
            this is where they are handled.
        graph_store: Connected graph, or None.
        rewrite_nodes: The ingest withdrew these documents' previous graph
            facts (a re-read), so re-write every surviving fact's node.

    Returns:
        The recompute's stats, plus ``modality`` counts. Never raises.
    """
    document_ids = sorted({str(c.get("document_id")) for c in chunks if c.get("document_id")})
    if store._blocked(workspace_id):  # the case is being purged
        return {"skipped": "workspace purge in progress"}
    try:
        rows, counts = evidence_for_entries(
            nodes, edges, {str(c.get("chunk_id")): str(c.get("text") or "") for c in chunks}
        )
        summary = _summary_pages(chunks)
        for row in rows:
            row["on_summary_page"] = row.get("page") in summary.get(row["document_id"], ())
        async with _lock(workspace_id):
            from app.vectorstore.qdrant import vector_store

            # Extraction takes minutes. A document deleted meanwhile had no
            # evidence to delete then, and nothing would ever delete what this
            # pass stores now: keep only documents the evidence store still
            # holds. (A delete racing past this check is caught by
            # on_documents_deleted, which re-deletes under this same lock.)
            present = await vector_store.documents_present(workspace_id, document_ids)
            gone = sorted(set(document_ids) - present)
            if gone:
                logger.info("Trust pass skipped {} deleted document(s)", len(gone))
                rows = [r for r in rows if r["document_id"] in present]
            vectors = await vector_store.get_document_vectors(workspace_id, sorted(present))
            await store.put_document_profiles(workspace_id, vectors)
            await store.replace_document_evidence(workspace_id, sorted(present), rows)
            stats = await _recompute_locked(workspace_id, graph_store, rewrite_nodes)
        stats["modality"] = counts
        return stats
    except asyncio.CancelledError:
        logger.warning("Trust pass cancelled for {} document(s)", len(document_ids))
        raise
    except Exception as exc:  # noqa: BLE001 - never gates ingestion
        logger.opt(exception=exc).error("Trust pass failed for {} document(s)", len(document_ids))
        return {"error": type(exc).__name__}


async def recompute_workspace(workspace_id: str, graph_store: Any = None) -> Dict[str, Any]:
    """Rebuild the case's facts, conflicts and review items from its evidence."""
    async with _lock(workspace_id):
        return await _recompute_locked(workspace_id, graph_store)


async def _recompute_locked(
    workspace_id: str, graph_store: Any, rewrite_nodes: bool = False
) -> Dict[str, Any]:
    from app.core.config import settings

    inputs = await store.load_inputs(workspace_id)
    relevance = relevance_scores(inputs["profiles"], settings.trust_relevance_min_docs)
    result = evaluate_case(
        inputs["evidence"], relevance, inputs["decisions"], TrustParams.from_settings()
    )
    await store.replace_derived(workspace_id, result)
    stats = dict(result["stats"])
    stats["documents"] = len(relevance)
    stats["lowRelevanceDocuments"] = sum(1 for s in relevance.values() if s < 0.5)
    if graph_store is not None:
        try:
            stats["graph"] = await project_to_graph(
                workspace_id, result, graph_store, rewrite_nodes=rewrite_nodes
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the graph is optional
            logger.warning("Trust projection to the graph failed: {}", type(exc).__name__)
            stats["graph"] = {"error": type(exc).__name__}
    logger.info(
        "Trust recompute: {} document(s), {} evidence, {} fact(s) {}, {} conflict(s) {} "
        "({} unresolved), {} succession(s), {} denial(s), {} review item(s), "
        "{} low-relevance document(s)",
        stats["documents"],
        stats["evidence"],
        stats["facts"],
        stats["byStatus"],
        stats["conflicts"],
        stats["conflictsByKind"],
        stats["unresolvedConflicts"],
        stats.get("successions", 0),
        stats["denials"],
        stats["reviews"],
        stats["lowRelevanceDocuments"],
    )
    return stats


# ---------------------------------------------------------------------------
# Graph projection
# ---------------------------------------------------------------------------


def _node_identity(node: Mapping[str, Any]) -> Optional[tuple]:
    from app.ontology.schema import NODES_BY_LABEL

    label = str(node.get("label") or "")
    spec = NODES_BY_LABEL.get(label)
    if spec is None:
        return None
    props = node.get("properties") or {}
    return label, tuple((k, "" if props.get(k) is None else props.get(k)) for k in spec.identity)


def _node_trust(facts: Sequence[Mapping[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Worst status, lowest confidence and most source groups per graph node."""
    out: Dict[tuple, Dict[str, Any]] = {}
    for fact in facts:
        for row in fact.get("evidence") or ():
            node = row.get("node")
            ident = _node_identity(node) if node else None
            if ident is None:
                continue
            current = out.get(ident)
            if current is None:
                out[ident] = {
                    "trust_status": fact["status"],
                    "trust_confidence": fact["confidence"],
                    "source_count": fact["source_groups"],
                    "node": node,
                }
                continue
            if _STATUS_RANK[fact["status"]] < _STATUS_RANK[current["trust_status"]]:
                current["trust_status"] = fact["status"]
            current["trust_confidence"] = min(current["trust_confidence"], fact["confidence"])
            current["source_count"] = max(current["source_count"], fact["source_groups"])
    return out


def _contradiction_edges(
    workspace_id: str, result: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """CONTRADICTS edges between the two sides' nodes, addressed by full identity.

    The old batch detector addressed composite-identity nodes (Event, Relief,
    Obligation) by one scalar, which the writer rejects, so its date, amount
    and status findings never reached the graph. A conflict whose two sides
    are one node (an obligation whose status two documents disagree on) has
    no edge to draw; the node's ``trust_status`` carries it instead.
    """
    by_key = {fact["fact_key"]: fact for fact in result.get("facts") or ()}

    def endpoint(fact_key: str) -> Optional[Dict[str, Any]]:
        fact = by_key.get(fact_key)
        for row in (fact or {}).get("evidence") or ():
            ident = _node_identity(row["node"]) if row.get("node") else None
            if ident is not None:
                return {"label": ident[0], "key": dict(ident[1]), "row": row}
        return None

    edges = []
    for conflict in result.get("conflicts") or ():
        if conflict.get("resolution_status") != "unresolved" or not conflict.get("fact_b"):
            continue
        a, b = endpoint(conflict["fact_a"]), endpoint(conflict["fact_b"])
        if a is None or b is None or (a["label"], a["key"]) == (b["label"], b["key"]):
            continue
        row = a["row"]
        edges.append(
            {
                "type": "CONTRADICTS",
                "from": {"label": a["label"], "key": a["key"]},
                "to": {"label": b["label"], "key": b["key"]},
                "properties": {
                    "conflict_kind": conflict["conflict_kind"],
                    "detector": GRAPH_DETECTOR,
                    "workspace_id": workspace_id,
                    "source_document_id": row.get("document_id"),
                    "chunk_id": row.get("chunk_id"),
                    "page": row.get("page"),
                    "extraction_confidence": float(row.get("extraction_confidence") or 0.5),
                },
            }
        )
    return edges


async def project_to_graph(
    workspace_id: str,
    result: Mapping[str, Any],
    graph_store: Any,
    rewrite_nodes: bool = False,
) -> Dict[str, int]:
    """Write trust status onto the graph, and its conflicts as CONTRADICTS edges.

    Args:
        rewrite_nodes: Re-write every surviving fact's node and edge first —
            after a document delete, whose graph withdrawal removed every node
            that document wrote last, including ones another document still
            supports.
    """
    facts = result.get("facts") or []
    trust = _node_trust(facts)
    written = {"nodes": 0, "edges": 0, "statuses": 0, "contradictions": 0}

    if rewrite_nodes:
        nodes = [entry["node"] for entry in trust.values()]
        edges = []
        seen = set()
        for fact in facts:
            if fact["status"] == STATUS_DROPPED:
                continue
            for row in fact.get("evidence") or ():
                edge = row.get("edge")
                if edge and fact["fact_key"] not in seen:
                    seen.add(fact["fact_key"])
                    edges.append(edge)
        report = await graph_store.write_facts(workspace_id, nodes, edges)
        written["nodes"], written["edges"] = report.nodes_written, report.edges_written

    by_label: Dict[str, List[Dict[str, Any]]] = {}
    for (label, identity), entry in trust.items():
        by_label.setdefault(label, []).append(
            {
                "identity": dict(identity),
                "trust_status": entry["trust_status"],
                "trust_confidence": entry["trust_confidence"],
                "source_count": entry["source_count"],
            }
        )
    for label in sorted(by_label):
        written["statuses"] += await graph_store.set_trust(workspace_id, label, by_label[label])

    await graph_store.clear_contradictions(workspace_id, GRAPH_DETECTOR)
    edges = _contradiction_edges(workspace_id, result)
    if edges:
        report = await graph_store.write_facts(workspace_id, nodes=[], edges=edges)
        written["contradictions"] = report.edges_written
    return written


async def on_documents_deleted(workspace_id: str, document_ids: Sequence[str]) -> None:
    """The vector store's post-delete hook: rebuild the case without them.

    The deleted documents' evidence and every derived record naming them are
    already gone (``QdrantStore._delete_trust_sync``). This rebuilds what the
    remaining documents support and re-projects it into the graph. Never
    raises: a failure leaves the case under-reported, never holding the
    deleted document's data.
    """
    if not enabled():
        return
    from app.socket.events import event_handler

    graph_store = getattr(event_handler, "_graph_store", None)
    try:
        async with _lock(workspace_id):
            # A trust pass that checked these documents before the delete may
            # have stored their evidence since; it held this lock, so it is
            # done now, and this removes what it wrote.
            await store.delete_document_records(workspace_id, list(document_ids))
            stats = await _recompute_locked(workspace_id, graph_store, rewrite_nodes=True)
        logger.info(
            "Trust rebuilt after deleting {} document(s): {} fact(s)",
            len(document_ids),
            stats.get("facts", 0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=exc).error(
            "Trust rebuild after deleting {} document(s) failed", len(document_ids)
        )


# ---------------------------------------------------------------------------
# Review decisions
# ---------------------------------------------------------------------------


async def resolve_review(
    workspace_id: str, key: str, action: str, decided_by: str = "", graph_store: Any = None
) -> Dict[str, Any]:
    """Record a reviewer's decision on a conflict or a fact, then recompute.

    ``key`` is a ``conflict_id`` for ``keep_a`` / ``keep_b`` / ``keep_both`` /
    ``dismissed`` and a ``fact_key`` for ``confirm_trusted`` /
    ``confirm_dropped``. The decision is an input to the router, which stays
    the only writer of a fact's status.
    """
    if action not in CONFLICT_ACTIONS + FACT_ACTIONS:
        raise ValueError(f"unknown review action {action!r}")
    match: Dict[str, Any] = {}
    if action in CONFLICT_ACTIONS:
        for conflict in await store.list_records(workspace_id, store.RECORD_CONFLICT):
            if conflict.get("conflict_id") == key:
                match = conflict_signature(conflict)
    else:
        for fact in await store.list_records(workspace_id, store.RECORD_FACT):
            if fact.get("fact_key") == key:
                match = fact_signature(fact, fact.get("subjects") or ())
    await store.put_decision(workspace_id, key, action, decided_by, match=match)
    return await recompute_workspace(workspace_id, graph_store)


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------

#: Conflicts one prompt block may carry; the rest are counted, not listed.
MAX_BLOCK_CONFLICTS = 25

_KIND_LABEL = {
    "date": "dates differ",
    "quantum": "amounts differ",
    "status": "status differs",
    "party": "parties or roles differ",
    "text": "values differ",
    "denial": "one document asserts it, another denies it",
}


async def unresolved_conflicts(workspace_id: str) -> List[Dict[str, Any]]:
    """The case's unresolved conflicts, or [] when the pipeline is off."""
    if not enabled() or not workspace_id:
        return []
    try:
        rows = await store.list_records(workspace_id, store.RECORD_CONFLICT)
    except Exception as exc:  # noqa: BLE001 - a prompt aid, never a gate
        logger.warning("Trust conflicts unavailable: {}", type(exc).__name__)
        return []
    return [r for r in rows if r.get("resolution_status") == "unresolved"]


def _source_ref(source: Mapping[str, Any], names: Mapping[str, str]) -> str:
    doc = str(source.get("document_id") or "")
    name = names.get(doc) or doc
    page = source.get("page")
    return f"{name}, page {page}" if page else name


def render_conflict(conflict: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """One conflict as a line a model can reason from: both sides, both sources."""
    left, right = conflict["sides"][0], conflict["sides"][1]
    subject = conflict.get("subject") or conflict.get("label") or "a fact"
    attribute = str(conflict.get("attribute") or "").replace("_", " ")
    what = _KIND_LABEL.get(conflict.get("conflict_kind"), "values differ")
    a_src = "; ".join(_source_ref(s, names) for s in left.get("sources") or [])
    b_src = "; ".join(_source_ref(s, names) for s in right.get("sources") or [])
    if right.get("denial"):
        return f"- {subject}: {what}. Asserted in {a_src}; denied in {b_src}."
    return (
        f"- {subject} ({attribute}): {what}. "
        f"'{left.get('value')}' per {a_src}; '{right.get('value')}' per {b_src}."
    )


async def conflict_block(workspace_id: str, names: Optional[Mapping[str, str]] = None) -> str:
    """A prompt block listing the case's unresolved cross-document conflicts.

    Empty when the pipeline is off or nothing conflicts. ``names`` maps
    document ids to file names for the source references.
    """
    conflicts = await unresolved_conflicts(workspace_id)
    if not conflicts:
        return ""
    names = names or {}
    lines = [render_conflict(c, names) for c in conflicts[:MAX_BLOCK_CONFLICTS]]
    more = len(conflicts) - len(lines)
    if more > 0:
        lines.append(f"- … and {more} more.")
    return (
        "CONFLICTS BETWEEN DOCUMENTS (machine-detected from the case's own documents; "
        "each names both sources). Where one bears on your output, state both "
        "versions and their sources; never silently pick one:\n" + "\n".join(lines)
    )
