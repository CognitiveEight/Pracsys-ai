"""
Knowledge-graph wiring, shared by every entry point.

The service starts two ways — ``app.main`` (via ``app.core.lifecycle``) and
``python -m app.socket.ai_service`` — and both must connect the graph the same
way, or one of them runs ontology extraction and silently discards the facts.
This module is the single place that happens.

Registering the graph also hooks it into the vector store's two deletion
choke points (``delete_documents`` / ``delete_workspace``). Every deletion
path — sync deletes, version restore, document reload, the compliance purge —
already goes through those, so graph facts can never outlive the document or
workspace they were extracted from.
"""

from typing import Any, List, Optional

from loguru import logger

from app.core.config import settings
from app.utils.redact import safe_url


async def setup_knowledge_plane(strict: bool = True) -> Optional[Any]:
    """Connect the legal knowledge graph if configured.

    Args:
        strict: True (``app.main``) raises when the graph is configured but
            unreachable. False logs an error and returns None — a long-running
            socket service keeps serving panels without the graph.

    Returns:
        A connected ``GraphStore``, or None when not configured / unreachable.
    """
    if not settings.graph_enabled:
        logger.info("Knowledge graph not configured (NEO4J_URL unset); retrieval uses hybrid search only")
        return None

    from app.graph.store import GraphConfig, GraphStore

    graph_store = GraphStore(
        GraphConfig(
            url=settings.neo4j_url,
            username=settings.neo4j_username,
            password=settings.neo4j_password or "",
            database=settings.neo4j_database,
            edition=settings.neo4j_edition,
        )
    )
    if not await graph_store.health():
        message = f"NEO4J_URL is set ({safe_url(settings.neo4j_url)}) but the graph is unreachable"
        await graph_store.close()
        if strict:
            raise RuntimeError(message + "; fix it or unset NEO4J_URL to run without a graph")
        logger.error("{} — graph retrieval and ontology ingest are off until it is fixed", message)
        return None

    if settings.graph_apply_schema_on_startup:
        report = await graph_store.apply_schema()
        log = logger.warning if report["failed"] else logger.info
        log(
            "Ontology schema applied: {} statement(s), {} failed",
            report["applied"],
            len(report["failed"]),
        )
    logger.info("Knowledge graph connected ({})", safe_url(settings.neo4j_url))
    return graph_store


def register_knowledge_plane(graph_store: Optional[Any]) -> None:
    """Hand the graph to the socket handler and hook it into deletion."""
    from app.socket.events import event_handler
    from app.vectorstore.qdrant import vector_store

    event_handler.set_graph_store(graph_store)
    vector_store.set_graph_withdrawal(_withdrawal_hook(graph_store) if graph_store else None)
    logger.info("Knowledge plane registered (graph={})", graph_store is not None)


def _withdrawal_hook(graph_store: Any):
    """Callback the vector store runs on every delete.

    ``document_ids=None`` means the whole workspace (the compliance purge):
    a failure there RAISES, so the purge's retry/escalation path runs — a
    deletion that half-works is worse than one that fails loudly. A failure
    on a document delete is logged CRITICAL and audited instead of blocking
    the user's sync.
    """

    async def withdraw(workspace_id: str, document_ids: Optional[List[str]]) -> None:
        from app.observability.audit import audit_logger

        if document_ids is None:
            await graph_store.withdraw_workspace(workspace_id)
            return
        failed = []
        for doc_id in document_ids:
            try:
                await graph_store.withdraw_document(workspace_id, doc_id)
            except Exception as exc:  # noqa: BLE001 - every document gets its attempt
                failed.append(doc_id)
                logger.opt(exception=exc).critical(
                    "Graph facts for deleted document {} were NOT withdrawn", doc_id
                )
        if failed:
            audit_logger.log_event(
                "graph_withdraw_failed",
                {"workspace_id": workspace_id, "document_ids": failed},
                workspace_id=workspace_id,
                severity="ERROR",
            )

    return withdraw
