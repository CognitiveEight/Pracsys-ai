"""The shared knowledge base: ``kb_document_added``, ``kb_document_deleted``
and ``kb_sync`` (Server → AI).

The shelf itself, and the rules that keep it exactly in step with the backend,
live in app/rag/knowledge_base.py. These handlers validate the payload, hand it
over, and log ids and counts only: the payloads carry signed links and
admin-written titles, neither of which is logged.

The backend never waits for a reply, and nothing is emitted back. ``kb_sync``
on every connect is the backstop for a push that was lost while this service
was away.
"""

import asyncio
from typing import Any, Awaitable, Dict, List, Set

from loguru import logger

from app.rag.knowledge_base import knowledge_base
from app.socket.schemas import (
    KnowledgeBaseDocument,
    KnowledgeBaseDocumentAddedPayload,
    KnowledgeBaseDocumentDeletedPayload,
    KnowledgeBaseSyncPayload,
)
from app.utils.redact import validation_fields


class KnowledgeBaseMixin:
    """Keep the shared knowledge base in step with the backend."""

    def _spawn_kb_task(self, work: Awaitable[Any], label: str) -> "asyncio.Task[Any]":
        return self._track_background_task(asyncio.ensure_future(work), label=label)

    async def on_kb_document_added(self, data: Dict[str, Any]) -> None:
        """Fetch, read and index one document; the same id again replaces it.

        The fetch runs in the background (the link is valid ~15 minutes, so it
        starts at once), and a failure waits for the next ``kb_sync``.
        """
        try:
            payload = KnowledgeBaseDocumentAddedPayload(**(data or {}))
        except Exception as err:
            logger.warning(
                "kb_document_added: invalid payload (keys={}): {}",
                sorted((data or {}).keys()) if isinstance(data, dict) else "-",
                validation_fields(err),
            )
            return
        doc = payload.model_dump(exclude={"requestId"}, exclude_none=True)
        started = await knowledge_base.add(
            doc, request_id=payload.requestId, spawn=self._spawn_kb_task
        )
        if started:
            logger.info(
                "kb_document_added: ingest started for {} (category={})",
                payload.documentId,
                payload.category,
            )

    async def on_kb_document_deleted(self, data: Dict[str, Any]) -> None:
        """Erase everything held for the document, on receipt — no grace
        period, unlike ``workspace_deleted``. An unknown id is a no-op."""
        try:
            payload = KnowledgeBaseDocumentDeletedPayload(**(data or {}))
        except Exception as err:
            logger.warning(
                "kb_document_deleted: invalid payload (keys={}): {}",
                sorted((data or {}).keys()) if isinstance(data, dict) else "-",
                validation_fields(err),
            )
            return
        await knowledge_base.erase([payload.documentId])
        logger.info("kb_document_deleted: {} erased", payload.documentId)

    async def on_kb_sync(self, data: Dict[str, Any]) -> None:
        """Make the shelf exactly the listed documents (a full replace).

        An item that fails validation (say, its link could not be made) is not
        ingested, but its id still counts as listed, so a copy already held is
        kept rather than erased over a bad item.
        """
        try:
            payload = KnowledgeBaseSyncPayload(**(data or {}))
        except Exception as err:
            # Without a valid list there is nothing to compare: erase nothing.
            logger.warning(
                "kb_sync: invalid payload (keys={}): {}",
                sorted((data or {}).keys()) if isinstance(data, dict) else "-",
                validation_fields(err),
            )
            return
        valid: List[Dict[str, Any]] = []
        kept: Set[str] = set()
        invalid = 0
        for raw in payload.documents:
            try:
                valid.append(
                    KnowledgeBaseDocument(**(raw or {})).model_dump(exclude_none=True)
                )
            except Exception:
                invalid += 1
                doc_id = str((raw or {}).get("documentId") or "").strip()
                if doc_id:
                    kept.add(doc_id)
        counts = await knowledge_base.sync(valid, keep=kept, spawn=self._spawn_kb_task)
        logger.info(
            "kb_sync applied: listed={} invalid={} held={} erased={} ingesting={}",
            counts["listed"],
            invalid,
            counts["held"],
            counts["erased"],
            counts["ingesting"],
        )
