"""
The shared knowledge base: one platform-wide shelf of legal reference documents
(judgments, statutes, court formats, precedents) that super administrators
manage in the admin panel.

The backend keeps this service in step with three events (handled in
app/socket/handlers/knowledge_base.py):

* ``kb_document_added`` — fetch, read and index one document. The same
  ``documentId`` again replaces it.
* ``kb_document_deleted`` — erase everything held for it, on receipt.
* ``kb_sync`` — the complete shelf, on every connect: erase what it does not
  list, ingest what it lists that is not held. ``documents: []`` erases all.

**Storage.** The shelf lives in the evidence collection under the reserved
workspace id :data:`KB_WORKSPACE_ID`, so it reuses the chunker, embeddings,
hybrid search and — most importantly — ``delete_documents``, which clears the
evidence, tables and graph stores together. Every case query filters on its
own workspace id, so shelf chunks can never be read or cited as a case's
evidence, and a case purge never reaches the shelf. The signed ``fileUrl`` is
used once and never stored; only the extracted, redacted text and the admin's
metadata (title, category, description, MIME type, upload time) are kept.

**Erasure wins every race.** ``erase`` bumps the document's generation and
cancels its ingest. An ingest writes under the document's lock and only if its
generation is unchanged, and a write already under way finishes before the
erase takes the lock — so a document can never be written back after it was
erased.

**Chat.** :meth:`KnowledgeBase.passages_for_chat` returns the few shelf
passages that score as relevant to a question; the chat prompt shows them under
``[K#]`` labels, apart from the case's annexures, and the citation names the
document by its ``title``.

**Panels, for every case.** :meth:`KnowledgeBase.passages_for_case` finds the
shelf passages relevant to a case (its documents' types, subjects and cited
provisions), and :func:`render_panel_block` presents them
to the Strategy, Analysis Report and Applicable Laws prompts as reference
material that is explicitly NOT the case's evidence: law, precedent and format
only, never facts, parties or a document the case holds. One retrieval serves
every panel of a run (cached per case, query and shelf version).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple

from loguru import logger

from app.core.config import settings

#: Reserved workspace id of the shelf in the evidence collection. Not a
#: 24-hex id, so it can never be a real case.
KB_WORKSPACE_ID = "__knowledge_base__"

KB_CATEGORIES = frozenset({"judgment", "statute", "court_format", "precedent", "other"})

#: MIME type → extension, for routing extraction (the title is never used).
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}

#: Leading text of an extraction that produced a message instead of content.
_UNREADABLE_PREFIXES = ("[Error", "[Unsupported", "[No text", "[Could not")

#: Request ids remembered for de-duplicating re-sent ``kb_document_added``.
_SEEN_REQUESTS_MAX = 512

#: Case retrievals remembered, so one run's panels share one search.
_CASE_CACHE_MAX = 64
#: Characters of one passage shown to a panel.
_PANEL_PASSAGE_CHARS = 2_500
#: Documents and characters used to describe a case for the shelf search.
_QUERY_DOCS = 12
_QUERY_CHARS = 1_500

#: Attempts and base backoff (seconds) for an erase before it escalates.
_ERASE_ATTEMPTS = 3
_ERASE_BACKOFF_SECONDS = 2.0

Spawn = Callable[[Awaitable[Any], str], "asyncio.Task[Any]"]


class KnowledgeBaseUnreadable(Exception):
    """The document was fetched but no text could be read from it."""


def _store() -> Any:
    from app.vectorstore.qdrant import vector_store

    return vector_store


def routing_name(document_id: str, mime_type: Optional[str]) -> str:
    """A name for extraction routing: the id plus the MIME type's extension."""
    ext = _MIME_EXTENSIONS.get(str(mime_type or "").split(";")[0].strip().lower(), "")
    return f"kb_{document_id}{ext}"


def is_unreadable(text: Optional[str]) -> bool:
    """True when extraction returned nothing or only a bracketed message."""
    body = (text or "").strip()
    if not body:
        return True
    if body.startswith(_UNREADABLE_PREFIXES):
        return True
    # A lone sentinel such as "[PDF content - PDF processing not available]".
    return body.startswith("[") and body.endswith("]") and "\n" not in body


async def _download(url: str, name: str) -> bytes:
    from app.processing.document_processor import document_processor

    content, _size = await document_processor._download_file(url, name)
    return content


def _extract_sync(content: bytes, name: str) -> Tuple[str, Optional[List[Any]]]:
    """Extract and redact. Spreadsheets are parsed once and redacted by column
    (never ``scan_text`` over rendered table text); everything else carries
    page markers so citations can name the page."""
    from app.processing.file_handlers import file_processor
    from app.processing.pii_detector import scan_text

    if file_processor.is_spreadsheet(name, content):
        from app.processing.tables import parse_workbook, redact_tableset, render_text

        tables = parse_workbook(content)
        redacted, _scan = redact_tableset(tables)
        text, row_index = render_text(redacted)
        if is_unreadable(text):
            raise KnowledgeBaseUnreadable("spreadsheet has no readable cells")
        return text, list(row_index) or None
    text = file_processor.process_file_by_extension(content, name, True)
    if not isinstance(text, str) or is_unreadable(text):
        raise KnowledgeBaseUnreadable("no text could be extracted")
    return scan_text(text.strip()).redacted_text, None


def _metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    category = str(doc.get("category") or "other").strip().lower()
    meta: Dict[str, Any] = {
        "knowledge_base": True,
        "title": str(doc.get("title") or "").strip(),
        "category": category if category in KB_CATEGORIES else "other",
        "mime_type": doc.get("mimeType"),
        "uploaded_at": doc.get("uploadedAt"),
    }
    if doc.get("description"):
        meta["description"] = str(doc["description"]).strip()
    return meta


class KnowledgeBase:
    """The shelf's bookkeeping: what is held, what is being fetched, and the
    generation that lets an erase overrule an ingest still in flight."""

    def __init__(self) -> None:
        self._held: Optional[Set[str]] = None
        self._tasks: Dict[str, "asyncio.Task[Any]"] = {}
        self._generation: Dict[str, int] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._seen_requests: "OrderedDict[str, None]" = OrderedDict()
        self._sync_lock = asyncio.Lock()
        self._semaphore: Optional[asyncio.Semaphore] = None
        # Bumped on every write and erase; keys the case cache, so an erased
        # document is never served from it.
        self._version = 0
        self._case_cache: "OrderedDict[Tuple[str, str, int], List[Dict[str, Any]]]" = OrderedDict()

    # ------------------------------------------------------------ internals

    def _lock(self, document_id: str) -> asyncio.Lock:
        return self._locks.setdefault(document_id, asyncio.Lock())

    def _slots(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(
                max(1, int(settings.knowledge_base_ingest_concurrency))
            )
        return self._semaphore

    async def held_ids(self, *, refresh: bool = False) -> Set[str]:
        """Ids with chunks on the shelf (read from the store once, then kept
        current by ingest and erase; ``refresh`` re-reads it)."""
        if self._held is None or refresh:
            self._held = set(await _store().list_document_ids(KB_WORKSPACE_ID))
        return set(self._held)

    def _note_request(self, request_id: Optional[str]) -> bool:
        """Remember ``request_id``; False when it was already seen."""
        if not request_id:
            return True
        if request_id in self._seen_requests:
            return False
        self._seen_requests[request_id] = None
        while len(self._seen_requests) > _SEEN_REQUESTS_MAX:
            self._seen_requests.popitem(last=False)
        return True

    async def _cancel_ingest(self, document_id: str) -> None:
        task = self._tasks.pop(document_id, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except BaseException:  # its own outcome; the caller only needs it stopped
            pass

    async def _ingest(self, doc: Dict[str, Any], generation: int) -> int:
        """Fetch, read and store one document. Returns the chunks stored."""
        document_id = str(doc["documentId"])
        name = routing_name(document_id, doc.get("mimeType"))
        async with self._slots():
            content = await _download(str(doc.get("fileUrl") or ""), name)
            text, row_index = await asyncio.to_thread(_extract_sync, content, name)
        del content
        async with self._lock(document_id):
            if self._generation.get(document_id, 0) != generation:
                logger.info("Knowledge base ingest of {} dropped: erased meanwhile", document_id)
                return 0
            # A write under way finishes before an erase can take the lock, so
            # it can never land after the erase.
            write = asyncio.ensure_future(
                _store().add_document_chunks(
                    KB_WORKSPACE_ID, document_id, text, _metadata(doc), row_index=row_index
                )
            )
            try:
                stored = await asyncio.shield(write)
            except asyncio.CancelledError:
                try:
                    await write
                except Exception:
                    pass
                raise
            if stored:
                self._version += 1
                if self._held is not None:
                    self._held.add(document_id)
        return stored

    async def _run_ingest(self, doc: Dict[str, Any], generation: int) -> None:
        from app.ai.usage import capture_usage, log_unbilled_usage

        document_id = str(doc["documentId"])
        with capture_usage() as usage:
            try:
                stored = await self._ingest(doc, generation)
                if stored:
                    logger.info(
                        "Knowledge base document {} indexed: chunks={} category={}",
                        document_id,
                        stored,
                        _metadata(doc)["category"],
                    )
            except asyncio.CancelledError:
                logger.info("Knowledge base ingest of {} cancelled", document_id)
                raise
            except KnowledgeBaseUnreadable as unreadable:
                logger.warning(
                    "Knowledge base document {} not indexed: {}", document_id, unreadable
                )
            except Exception as err:
                # A 403 is an expired link: the next kb_sync carries a fresh one.
                logger.opt(exception=err).error(
                    "Knowledge base document {} not indexed: {}", document_id, type(err).__name__
                )
            finally:
                log_unbilled_usage("knowledge base ingest", usage)
                if self._tasks.get(document_id) is asyncio.current_task():
                    self._tasks.pop(document_id, None)

    def _schedule(self, doc: Dict[str, Any], spawn: Spawn) -> None:
        document_id = str(doc["documentId"])
        generation = self._generation.get(document_id, 0)
        self._tasks[document_id] = spawn(
            self._run_ingest(doc, generation), f"kb ingest {document_id}"
        )

    async def _erase_now(self, document_ids: List[str]) -> None:
        """Delete from every store, retrying; escalates if it still fails."""
        last: Optional[BaseException] = None
        for attempt in range(1, _ERASE_ATTEMPTS + 1):
            try:
                await _store().delete_documents(KB_WORKSPACE_ID, document_ids)
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last = err
                if attempt < _ERASE_ATTEMPTS:
                    await asyncio.sleep(_ERASE_BACKOFF_SECONDS * attempt)
        from app.observability.audit import audit_logger

        logger.opt(exception=last).critical(
            "Knowledge base erase FAILED for {} document(s) after {} attempts; "
            "the next kb_sync retries it",
            len(document_ids),
            _ERASE_ATTEMPTS,
        )
        audit_logger.log_event(
            "knowledge_base_erase_failed",
            {"document_ids": list(document_ids), "error": type(last).__name__},
        )
        # Unknown again, so the next sync re-reads the store and retries.
        self._held = None
        raise last  # type: ignore[misc]

    # ---------------------------------------------------------- public API

    async def add(self, doc: Dict[str, Any], *, request_id: Optional[str], spawn: Spawn) -> bool:
        """Start (or restart) the ingest of one document. False for a re-send."""
        if not self._note_request(request_id):
            logger.info("Knowledge base add re-sent; ignored")
            return False
        document_id = str(doc["documentId"])
        # Same id again replaces: the newer link and metadata win.
        await self._cancel_ingest(document_id)
        self._schedule(doc, spawn)
        return True

    async def erase(self, document_ids: Iterable[str]) -> int:
        """Erase every copy of these documents now. Unknown ids are a no-op."""
        ids = sorted({str(d) for d in document_ids if d})
        if not ids:
            return 0
        for document_id in ids:
            self._generation[document_id] = self._generation.get(document_id, 0) + 1
            await self._cancel_ingest(document_id)
        locks = [self._lock(d) for d in ids]
        for lock in locks:
            await lock.acquire()
        try:
            await self._erase_now(ids)
        finally:
            for lock in locks:
                lock.release()
        self._version += 1
        self._case_cache.clear()
        if self._held is not None:
            self._held.difference_update(ids)
        for document_id in ids:
            self._locks.pop(document_id, None)
        return len(ids)

    async def sync(
        self,
        documents: List[Dict[str, Any]],
        *,
        spawn: Spawn,
        keep: Iterable[str] = (),
    ) -> Dict[str, int]:
        """Make the shelf exactly ``documents``. Returns the counts applied.

        ``keep``: ids the snapshot listed but could not be ingested from (a bad
        item); a held copy of those is neither erased nor re-fetched.
        """
        async with self._sync_lock:
            listed = {str(d["documentId"]): d for d in documents}
            held = await self.held_ids(refresh=True)
            in_flight = {d for d, t in self._tasks.items() if not t.done()}
            stale = (held | in_flight) - set(listed) - set(keep)
            erased = await self.erase(stale) if stale else 0
            fresh = [listed[d] for d in listed if d not in held and d not in in_flight]
            for doc in fresh:
                self._schedule(doc, spawn)
            return {
                "listed": len(listed),
                "held": len(held),
                "erased": erased,
                "ingesting": len(fresh),
            }

    async def passages_for_chat(self, question: str) -> List[Dict[str, Any]]:
        """The shelf passages relevant to ``question``, best first. Empty when
        the shelf is empty, chat use is off, or nothing scores as relevant."""
        if not settings.knowledge_base_chat_enabled or not (question or "").strip():
            return []
        if not await self.held_ids():
            return []
        from app.rag.retriever import retrieve_passages

        limit = max(1, int(settings.knowledge_base_chat_passages))
        hits = await retrieve_passages(
            KB_WORKSPACE_ID,
            question,
            limit=limit * 2,
            candidates=20,
            per_document=2,
            use_rewrite=False,
        )
        floor = float(settings.knowledge_base_min_relevance)
        # rerank() keeps its top three when nothing scores: the shelf is
        # supplementary, so an unscored or weak passage stays out.
        relevant = [h for h in hits if (h.get("rerank_score") or 0.0) >= floor][:limit]
        logger.info(
            "Knowledge base passages for chat: {} of {} candidate(s)", len(relevant), len(hits)
        )
        return relevant

    async def passages_for_case(self, workspace_id: str, query: str) -> List[Dict[str, Any]]:
        """The shelf passages relevant to a case, best first, for its panels.

        ``query`` describes the case (see :func:`case_query`). Cached per case,
        query and shelf version, so the panels of one run share one search.
        Empty when the shelf is empty or nothing scores as relevant.
        """
        if not workspace_id or not (query or "").strip():
            return []
        if not await self.held_ids():
            return []
        key = (workspace_id, hashlib.sha256(query.encode()).hexdigest(), self._version)
        if key in self._case_cache:
            self._case_cache.move_to_end(key)
            return list(self._case_cache[key])
        from app.rag.retriever import retrieve_passages

        limit = max(1, int(settings.knowledge_base_panel_passages))
        hits = await retrieve_passages(
            KB_WORKSPACE_ID,
            query,
            limit=limit * 2,
            candidates=24,
            per_document=2,
            use_rewrite=False,
        )
        floor = float(settings.knowledge_base_panel_min_relevance)
        relevant = [h for h in hits if (h.get("rerank_score") or 0.0) >= floor][:limit]
        if key[2] == self._version:  # not erased meanwhile
            self._case_cache[key] = relevant
            while len(self._case_cache) > _CASE_CACHE_MAX:
                self._case_cache.popitem(last=False)
        logger.info(
            "Knowledge base passages for case panels: {} of {} candidate(s)",
            len(relevant),
            len(hits),
        )
        return list(relevant)


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return []


def case_query(docs: Iterable[Dict[str, Any]]) -> str:
    """Describe a case for the shelf search: each document's type, subject and
    cited provisions (falling back to the opening of the text when a document
    has none of those). Never logged.

    Built from the documents alone, so every panel of a run describes the case
    identically and shares one cached search. The objective is left out on
    purpose: the first run writes it between panels, which would split the
    search, and each panel prompt already steers by it.
    """
    lines: List[str] = []
    for doc in list(docs)[:_QUERY_DOCS]:
        meta = doc.get("metadata") or {}
        fields = meta.get("extracted_fields") or {}
        parts = [str(meta.get("document_type") or "").strip()]
        parts.append(str(fields.get("subject") or "").strip())
        parts.extend(
            _as_text_list(fields.get("legal_provisions") or fields.get("legal_provisions_cited"))[:5]
        )
        line = "; ".join(p for p in parts if p and p.lower() != "other")
        if not line:
            line = " ".join(str(doc.get("content") or "").split())[:200]
        if line:
            lines.append(line)
    return "\n".join(lines)[:_QUERY_CHARS]


def render_panel_block(passages: List[Dict[str, Any]], max_chars: int) -> str:
    """Shelf passages as a panel prompt block, marked as reference material
    that is not this case's evidence. Empty when there is nothing to show."""
    blocks: List[str] = []
    used = 0
    for n, chunk in enumerate(passages, 1):
        meta = chunk.get("metadata") or {}
        title = str(meta.get("title") or "Untitled reference").strip()
        category = str(meta.get("category") or "other").replace("_", " ")
        text = str(chunk.get("content") or "").strip()[:_PANEL_PASSAGE_CHARS]
        block = f"[Reference {n}] {title} ({category})\n{text}"
        if not text or used + len(block) > max_chars:
            continue
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    return (
        "=== SHARED LEGAL REFERENCE LIBRARY (platform knowledge base — NOT this "
        "case's documents) ===\n"
        "Judgments, statutes, court formats and precedents the platform keeps for "
        "every case. Use them ONLY for law, precedent, procedure and format: which "
        "provisions and principles apply and how courts have applied them. They are "
        "NOT evidence in this case. Never take facts, parties, dates or amounts from "
        "them; never count them as documents present in this case or as filling a "
        "gap in its evidence; never put their parties' names into the case summary "
        "or any factual finding. When a point relies on one, name it by its title.\n\n"
        + "\n\n".join(blocks)
        + "\n=== END OF SHARED LEGAL REFERENCE LIBRARY ==="
    )


knowledge_base = KnowledgeBase()
