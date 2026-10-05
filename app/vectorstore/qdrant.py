"""
Qdrant vector store integration for document storage and retrieval.

Note: Qdrant Python client is synchronous (blocking HTTP). We run initialization
in a thread pool so the event loop stays responsive and the socket connection
is not dropped during init/timeouts.
"""

import asyncio
from datetime import datetime
import bisect
from typing import Any, Dict, List, Optional, Sequence
from uuid import NAMESPACE_DNS, uuid5

from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.models import (Distance, FieldCondition, Filter,
                                  FusionQuery, Fusion, IsEmptyCondition,
                                  MatchAny, MatchValue, Modifier, PayloadField,
                                  PayloadSchemaType, PointStruct, Prefetch,
                                  SparseVector, SparseVectorParams,
                                  VectorParams)

from app.core.config import settings
from app.rag.chunker import chunk_text, reassemble
from app.rag.sparse import encode_document, encode_query
from app.utils.exceptions import VectorStoreError

#: Named vectors in the evidence-chunk collection.
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "lexical"
#: Points per upsert request.
_UPSERT_BATCH = 64
#: Spreadsheet rows per upsert request (no vectors, so far smaller points).
_TABLE_UPSERT_BATCH = 256
#: Kinds of point in the tables collection.
TABLE_MANIFEST = "manifest"
TABLE_ROW = "row"
#: Row sets kept in memory for repeated calculations in one conversation.
_TABLE_ROW_CACHE_SIZE = 16


#: Per-chunk keys that are not properties of the document as a whole.
_CHUNK_ONLY_KEYS = frozenset(
    {"chunk_index", "total_chunks", "is_chunk", "embedding_failed"}
)


def _chunk_position(payload: Dict[str, Any], meta: Dict[str, Any]) -> int:
    """A chunk's position in its document: metadata.chunk_index, else the
    ``_chunk_N`` suffix of its id, else 0 (single-chunk documents)."""
    idx = meta.get("chunk_index")
    if isinstance(idx, int):
        return idx
    try:
        return int(str(idx))
    except (TypeError, ValueError):
        pass
    doc_id = str(payload.get("document_id") or "")
    marker = doc_id.rfind("_chunk_")
    if marker >= 0:
        try:
            return int(doc_id[marker + len("_chunk_"):])
        except ValueError:
            pass
    return 0


def _document_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (meta or {}).items() if k not in _CHUNK_ONLY_KEYS}


def _chunk_row_metadata(chunk: Any, row_index: Optional[Sequence[Any]]) -> Dict[str, Any]:
    """Spreadsheet location of one chunk: its sheet, row range, and the row of
    each line as ``[offset_in_chunk, row, sheet]`` (see EvidenceItem.row_at)."""
    if not row_index:
        return {}
    offsets = [r.offset for r in row_index]
    lo = bisect.bisect_left(offsets, chunk.char_start)
    hi = bisect.bisect_left(offsets, chunk.char_end)
    inside = row_index[lo:hi]
    if not inside:
        return {}
    sheet = inside[0].sheet
    rows = [r.row for r in inside if r.sheet == sheet]
    return {
        "sheet": sheet,
        "row_start": min(rows),
        "row_end": max(rows),
        "row_offsets": [[r.offset - chunk.char_start, r.row, r.sheet] for r in inside],
    }


class QdrantStore:
    """
    Qdrant vector store wrapper for managing document embeddings.
    """

    def __init__(self):
        self.client: Optional[QdrantClient] = None
        self.collection_name = settings.qdrant_collection_name
        # Must equal the embedding model's requested output size; a mismatch is
        # rejected, never padded or truncated (see _generate_embedding).
        self.vector_size = int(settings.embedding_dimensions or 768)
        self.chunks_collection = (
            settings.qdrant_chunks_collection
            or f"{self.collection_name}_evidence_v1"
        )
        # Spreadsheet rows and per-workbook manifests (app/processing/tables.py).
        self.tables_collection = (
            settings.qdrant_tables_collection
            or f"{self.collection_name}_tables_v1"
        )
        # (workspace, document, sheet_index, evidence_version) -> rows.
        self._table_row_cache: Dict[tuple, List[Dict[str, Any]]] = {}
        # Workspaces whose legacy chunks have been re-indexed into the chunk
        # collection in this process (see ensure_workspace_indexed).
        self._indexed_workspaces: set = set()
        self._index_locks: Dict[str, asyncio.Lock] = {}
        # Bumped on every evidence write/delete, so caches over a workspace's
        # chunks (app/rag/attribution.py) know when they are stale.
        self._evidence_versions: Dict[str, int] = {}
        # Graph-fact withdrawal run on every delete (see
        # app/core/knowledge_plane.py). None when no graph is configured.
        self._graph_withdrawal = None

    def set_graph_withdrawal(self, callback) -> None:
        """``async callback(workspace_id, document_ids_or_None)``; None disables."""
        self._graph_withdrawal = callback

    def _initialize_sync(self) -> None:
        """
        Synchronous init: create client and ensure collection.
        Called from a thread so the event loop is not blocked (Qdrant client is sync).
        """
        if settings.qdrant_api_key:
            self.client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=settings.qdrant_timeout_seconds,
            )
        else:
            self.client = QdrantClient(
                url=settings.qdrant_url,
                timeout=settings.qdrant_timeout_seconds,
            )
        collections = self.client.get_collections().collections
        collection_names = [col.name for col in collections]
        if self.collection_name not in collection_names:
            logger.info("Creating collection: {}", self.collection_name)
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.vector_size, distance=Distance.COSINE
                ),
            )
            logger.success("Collection created: {}", self.collection_name)
        else:
            logger.debug("Collection already exists: {}", self.collection_name)
        self._ensure_payload_indexes_sync()
        self._ensure_chunks_collection_sync()
        self._ensure_tables_collection_sync()

    def _ensure_chunks_collection_sync(self) -> None:
        """Evidence chunks: a dense vector for meaning and a sparse vector with
        server-side IDF for exact legal tokens (sections, case numbers)."""
        if not self.client.collection_exists(self.chunks_collection):
            logger.info("Creating evidence chunk collection: {}", self.chunks_collection)
            self.client.create_collection(
                collection_name=self.chunks_collection,
                vectors_config={
                    DENSE_VECTOR: VectorParams(
                        size=self.vector_size, distance=Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR: SparseVectorParams(modifier=Modifier.IDF)
                },
            )
        self._ensure_payload_indexes_sync(self.chunks_collection)

    def _ensure_tables_collection_sync(self) -> None:
        """Spreadsheet rows: payload only. They are never searched by vector;
        the evidence chunks do that. Rows are read whole for computation."""
        if not self.client.collection_exists(self.tables_collection):
            logger.info("Creating spreadsheet table collection: {}", self.tables_collection)
            self.client.create_collection(
                collection_name=self.tables_collection, vectors_config={}
            )
        self._ensure_payload_indexes_sync(self.tables_collection)
        for field_name, schema in (
            ("record", PayloadSchemaType.KEYWORD),
            ("sheet_index", PayloadSchemaType.INTEGER),
        ):
            try:
                self.client.create_payload_index(
                    collection_name=self.tables_collection,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception as index_err:
                logger.debug("Payload index on {} not created: {}", field_name, index_err)

    def _ensure_payload_indexes_sync(self, collection: Optional[str] = None) -> None:
        """Index the fields every query filters on. Idempotent; best-effort.

        Without an index, every workspace-filtered search and scroll is a full
        collection scan, which grows with every workspace in the deployment.
        """
        for field in ("workspace_id", "original_document_id"):
            try:
                self.client.create_payload_index(
                    collection_name=collection or self.collection_name,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception as index_err:  # already exists, or server refuses
                logger.debug("Payload index on {} not created: {}", field, index_err)

    async def initialize(self) -> None:
        """
        Initialize Qdrant client and create collection if needed.
        Runs blocking Qdrant calls in a thread so the event loop (and socket) stay responsive.
        """
        try:
            logger.info("Initializing Qdrant client...")
            await asyncio.to_thread(self._initialize_sync)
            logger.success("Qdrant client initialized")
        except Exception as e:
            logger.error("Failed to initialize Qdrant: {}", str(e))
            raise VectorStoreError(
                "Failed to initialize Qdrant", details={"error": str(e)}
            )

    async def _ensure_collection_exists(self) -> None:
        """Create collection if it doesn't exist. Used when client already exists (e.g. after lazy init)."""
        try:
            collections = self.client.get_collections().collections
            collection_names = [col.name for col in collections]

            if self.collection_name not in collection_names:
                logger.info("Creating collection: {}", self.collection_name)

                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size, distance=Distance.COSINE
                    ),
                )

                logger.success("Collection created: {}", self.collection_name)
            else:
                logger.debug("Collection already exists: {}", self.collection_name)

        except Exception as e:
            logger.error("Failed to ensure collection exists: {}", str(e))
            raise

    async def add_document(
        self,
        workspace_id: str,
        document_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Add a document to the vector store.

        Args:
            workspace_id: Workspace identifier
            document_id: Document identifier
            content: Document content
            metadata: Optional metadata

        Returns:
            Point ID in vector store
        """
        try:
            meta = dict(metadata or {})
            try:
                embedding = await self._generate_embedding(content)
                meta.pop("embedding_failed", None)
            except Exception as embed_err:
                # Keep the text: full-context features read it from here, and
                # dropping it would silently remove evidence. The zero vector
                # is flagged and excluded from every search (_search_filter),
                # so it can neither match nor be mistaken for a real hit.
                logger.error(
                    "Embedding failed for {} (workspace {}); stored as "
                    "embedding_failed and excluded from search: {}",
                    document_id,
                    workspace_id,
                    embed_err,
                )
                embedding = [0.0] * self.vector_size
                meta["embedding_failed"] = True

            # Deterministic id: re-ingesting the same chunk overwrites it instead
            # of adding a duplicate that doubles its weight in every search.
            point_id = str(uuid5(NAMESPACE_DNS, f"chunk:{workspace_id}:{document_id}"))
            payload = {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "content": content,
                "metadata": meta,
                "original_document_id": meta.get("original_document_id", document_id),
                "uploaded_at": meta.get("uploaded_at"),
            }

            point = PointStruct(id=point_id, vector=embedding, payload=payload)

            # Upsert point (run in thread so event loop is not blocked)
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=[point],
            )

            logger.debug(
                "Added document to vector store: {} (workspace: {})",
                document_id,
                workspace_id,
            )

            return point_id

        except Exception as e:
            logger.error("Failed to add document: {}", str(e))
            raise VectorStoreError(
                "Failed to add document",
                details={
                    "workspace_id": workspace_id,
                    "document_id": document_id,
                    "error": str(e),
                },
            )

    @staticmethod
    def _search_filter(workspace_id: str) -> Filter:
        """Document chunks of one workspace, excluding cache rows and chunks
        whose embedding failed (both carry zero vectors)."""
        return Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                IsEmptyCondition(is_empty=PayloadField(key="cache_kind")),
            ],
            must_not=[
                FieldCondition(
                    key="metadata.embedding_failed", match=MatchValue(value=True)
                ),
            ],
        )

    async def search(
        self,
        workspace_id: str,
        query: str,
        limit: int = 5,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search over a workspace's evidence chunks.

        Dense (meaning) and sparse (exact terms) candidates are fused with
        reciprocal-rank fusion. ``score_threshold`` applies to the dense
        candidates only — a fused rank score is not comparable to a cosine
        similarity, and an exact-term hit must not be dropped for being
        semantically distant.

        Returns chunk dicts: ``id``, ``score``, ``content``, ``metadata``
        (including ``page_start``/``page_end``/``heading``), ``document_id``
        (the chunk id) and ``original_document_id``.
        """
        try:
            if self.client is None:
                await self.initialize()
            if not await self.ensure_workspace_indexed(workspace_id):
                return await self._legacy_search(workspace_id, query, limit, score_threshold)
            return await self._hybrid_search(
                workspace_id, query, limit=limit, score_threshold=score_threshold
            )
        except VectorStoreError:
            raise
        except Exception as e:
            logger.error("Search failed: {}", str(e))
            raise VectorStoreError(
                "Search failed", details={"workspace_id": workspace_id, "error": str(e)}
            )

    @staticmethod
    def _chunk_filter(
        workspace_id: str,
        document_ids: Optional[List[str]] = None,
        exclude_failed_embeddings: bool = False,
    ) -> Filter:
        must: List[Any] = [
            FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))
        ]
        if document_ids:
            must.append(
                FieldCondition(
                    key="original_document_id", match=MatchAny(any=list(document_ids))
                )
            )
        must_not: List[Any] = []
        if exclude_failed_embeddings:
            must_not.append(
                FieldCondition(
                    key="metadata.embedding_failed", match=MatchValue(value=True)
                )
            )
        return Filter(must=must, must_not=must_not or None)

    @staticmethod
    def _format_hit(point: Any) -> Dict[str, Any]:
        payload = point.payload or {}
        meta = payload.get("metadata", {}) or {}
        return {
            "id": point.id,
            "score": point.score,
            "content": payload.get("content"),
            "metadata": meta,
            "document_id": payload.get("document_id"),
            "original_document_id": payload.get("original_document_id")
            or meta.get("original_document_id")
            or payload.get("document_id"),
        }

    async def _hybrid_search(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = 20,
        prefetch_limit: Optional[int] = None,
        score_threshold: Optional[float] = None,
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        prefetch_limit = max(limit, prefetch_limit or limit * 2)
        indices, values = encode_query(query)
        prefetch: List[Prefetch] = []
        try:
            dense = await self._generate_embedding(query, task_type="RETRIEVAL_QUERY")
            prefetch.append(
                Prefetch(
                    query=dense,
                    using=DENSE_VECTOR,
                    filter=self._chunk_filter(
                        workspace_id, document_ids, exclude_failed_embeddings=True
                    ),
                    score_threshold=score_threshold,
                    limit=prefetch_limit,
                )
            )
        except Exception as embed_err:
            # Exact-term search still works without an embedding.
            logger.warning(
                "Query embedding failed; searching lexically only: {}", embed_err
            )
        if indices:
            prefetch.append(
                Prefetch(
                    query=SparseVector(indices=indices, values=values),
                    using=SPARSE_VECTOR,
                    filter=self._chunk_filter(workspace_id, document_ids),
                    limit=prefetch_limit,
                )
            )
        if not prefetch:
            return []
        response = await asyncio.to_thread(
            self.client.query_points,
            collection_name=self.chunks_collection,
            prefetch=prefetch,
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=self._chunk_filter(workspace_id, document_ids),
            limit=limit,
            with_payload=True,
        )
        hits = [self._format_hit(p) for p in (response.points or [])]
        logger.debug(
            "Hybrid search: {} hit(s) for workspace {} ({} candidate list(s))",
            len(hits),
            workspace_id,
            len(prefetch),
        )
        return hits

    async def _legacy_search(
        self,
        workspace_id: str,
        query: str,
        limit: int,
        score_threshold: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Dense search on the legacy collection, used only while a workspace
        has not been re-indexed."""
        query_embedding = await self._generate_embedding(query, task_type="RETRIEVAL_QUERY")
        response = await asyncio.to_thread(
            self.client.query_points,
            collection_name=self.collection_name,
            query=query_embedding,
            query_filter=self._search_filter(workspace_id),
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [self._format_hit(p) for p in (response.points or [])]

    def _get_all_documents_sync(
        self,
        workspace_id: str,
        limit_per_batch: int,
    ) -> List[Dict[str, Any]]:
        """Sync scroll + combine; run via to_thread to avoid blocking the event loop."""
        workspace_filter = Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))
            ]
        )
        seen_point_ids = set()
        doc_chunks: Dict[str, List[tuple]] = {}
        offset = None
        while True:
            result, next_offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=workspace_filter,
                limit=limit_per_batch,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in result:
                if point.id in seen_point_ids:
                    continue
                seen_point_ids.add(point.id)
                payload = point.payload or {}
                content = (payload.get("content") or "").strip()
                if not content or len(content) < 20:
                    continue
                orig_id = payload.get("original_document_id") or payload.get(
                    "document_id"
                )
                uploaded_at = payload.get("uploaded_at") or ""
                meta = payload.get("metadata") or {}
                if not orig_id:
                    orig_id = meta.get("original_document_id") or payload.get(
                        "document_id"
                    )
                if orig_id not in doc_chunks:
                    doc_chunks[orig_id] = []
                doc_chunks[orig_id].append(
                    (_chunk_position(payload, meta), uploaded_at, content, meta)
                )
            if next_offset is None:
                break
            offset = next_offset
        out = []
        for doc_id, chunks in doc_chunks.items():
            # Reading order is chunk_index. Sorting by text (the old key) put a
            # long document's chunks in alphabetical order, scrambling it for
            # every feature that reads the whole document.
            chunks_sorted = sorted(chunks, key=lambda x: (x[0], x[2]))
            combined = " ".join(c for _, _, c, _ in chunks_sorted)
            uploaded_at = next((u for _, u, _, _ in chunks_sorted if u), "")
            doc_meta = _document_metadata(chunks_sorted[0][3] if chunks_sorted else {})
            out.append(
                {
                    "document_id": doc_id,
                    "content": combined,
                    "uploaded_at": uploaded_at,
                    # Document-level fields (type, extracted fields, filename) —
                    # previously dropped, which left every prompt's per-document
                    # header (_format_document_for_prompt) empty.
                    "metadata": doc_meta,
                    "filename": doc_meta.get("filename") or "",
                }
            )
        out.sort(key=lambda x: (x["uploaded_at"] or "", x["document_id"]))
        return out

    def _scroll_sync(self, collection: str, flt: Filter, limit: int) -> List[Any]:
        points: List[Any] = []
        offset = None
        while True:
            batch, offset = self.client.scroll(
                collection_name=collection,
                scroll_filter=flt,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points.extend(batch)
            if offset is None:
                return points

    def _get_all_chunk_documents_sync(
        self, workspace_id: str, limit_per_batch: int
    ) -> List[Dict[str, Any]]:
        """Whole documents rebuilt from the chunk collection, overlap removed."""
        by_doc: Dict[str, List[Any]] = {}
        for point in self._scroll_sync(
            self.chunks_collection, self._chunk_filter(workspace_id), limit_per_batch
        ):
            payload = point.payload or {}
            doc_id = payload.get("original_document_id") or payload.get("document_id")
            if doc_id:
                by_doc.setdefault(str(doc_id), []).append(payload)
        out = []
        for doc_id, payloads in by_doc.items():
            payloads.sort(key=lambda pl: _chunk_position(pl, pl.get("metadata") or {}))
            content = reassemble(
                [
                    {
                        "content": pl.get("content") or "",
                        "overlap_prev": (pl.get("metadata") or {}).get("overlap_prev"),
                    }
                    for pl in payloads
                ]
            ).strip()
            if len(content) < 20:
                continue
            doc_meta = _document_metadata(payloads[0].get("metadata") or {})
            pages = [
                p
                for pl in payloads
                for p in ((pl.get("metadata") or {}).get("page_start"),
                          (pl.get("metadata") or {}).get("page_end"))
                if isinstance(p, int)
            ]
            out.append(
                {
                    "document_id": doc_id,
                    "content": content,
                    "uploaded_at": next(
                        (pl.get("uploaded_at") for pl in payloads if pl.get("uploaded_at")),
                        "",
                    ),
                    "metadata": doc_meta,
                    "filename": doc_meta.get("filename") or "",
                    "page_count": max(pages) if pages else None,
                    "chunk_count": len(payloads),
                }
            )
        out.sort(key=lambda x: (x["uploaded_at"] or "", x["document_id"]))
        return out

    def evidence_version(self, workspace_id: str) -> int:
        return self._evidence_versions.get(workspace_id, 0)

    def _bump_evidence_version(self, workspace_id: str) -> None:
        self._evidence_versions[workspace_id] = self._evidence_versions.get(workspace_id, 0) + 1

    async def add_document_chunks(
        self,
        workspace_id: str,
        document_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        row_index: Optional[Sequence[Any]] = None,
    ) -> int:
        """Chunk, embed and store one document in the evidence collection.

        Replaces any chunks already stored for ``document_id`` — a re-read
        document must not keep stale tail chunks from its previous version.
        Embedding failures keep the chunk (flagged ``embedding_failed``) so
        whole-document reads still see the text; only dense search skips it.

        ``row_index`` (spreadsheets: ``RenderedRow`` offsets from
        ``app/processing/tables.py``) gives each chunk its ``sheet``,
        ``row_start``/``row_end`` and the row of every line, so a citation names
        the spreadsheet row. Spreadsheet chunks carry no overlap: repeated rows
        would be counted twice by anything that reads two adjacent chunks.

        Returns the number of chunks stored.
        """
        if self.client is None:
            await self.initialize()
        is_table = bool(row_index) or (metadata or {}).get("format") == "spreadsheet"
        chunks = chunk_text(text, with_overlap=not is_table)
        if not chunks:
            return 0
        doc_meta = {k: v for k, v in (metadata or {}).items() if k not in _CHUNK_ONLY_KEYS}
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.chunks_collection,
            points_selector=models.FilterSelector(
                filter=self._chunk_filter(workspace_id, [document_id])
            ),
        )

        from app.ai.gemini_client import gemini_client

        if not gemini_client.is_available():
            gemini_client.initialize()
        dense: List[Optional[List[float]]] = []
        try:
            dense = list(await gemini_client.embed_texts([c.text for c in chunks]))
        except Exception as batch_err:
            logger.error(
                "Batch embedding failed for document {} (workspace {}); retrying "
                "chunk by chunk: {}",
                document_id,
                workspace_id,
                batch_err,
            )
            for c in chunks:
                try:
                    dense.append(await gemini_client.embed_text(c.text))
                except Exception:
                    dense.append(None)

        points: List[PointStruct] = []
        failed = 0
        for chunk, vector in zip(chunks, dense):
            ok = vector is not None and len(vector) == self.vector_size
            failed += 0 if ok else 1
            indices, values = encode_document(chunk.text)
            vectors: Dict[str, Any] = {
                DENSE_VECTOR: vector if ok else [0.0] * self.vector_size
            }
            if indices:
                vectors[SPARSE_VECTOR] = SparseVector(indices=indices, values=values)
            chunk_meta = {
                **doc_meta,
                **chunk.as_metadata(),
                **_chunk_row_metadata(chunk, row_index),
                "original_document_id": document_id,
                "total_chunks": len(chunks),
                "is_chunk": len(chunks) > 1,
            }
            if not ok:
                chunk_meta["embedding_failed"] = True
            chunk_id = f"{document_id}_chunk_{chunk.index}"
            points.append(
                PointStruct(
                    id=str(uuid5(NAMESPACE_DNS, f"evidence:{workspace_id}:{chunk_id}")),
                    vector=vectors,
                    payload={
                        "workspace_id": workspace_id,
                        "document_id": chunk_id,
                        "original_document_id": document_id,
                        "content": chunk.text,
                        "metadata": chunk_meta,
                        "uploaded_at": doc_meta.get("uploaded_at"),
                    },
                )
            )
        for i in range(0, len(points), _UPSERT_BATCH):
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.chunks_collection,
                points=points[i : i + _UPSERT_BATCH],
            )
        self._bump_evidence_version(workspace_id)
        if failed:
            logger.error(
                "{} of {} chunk(s) of document {} have no embedding and are "
                "excluded from semantic search (still searchable by exact terms)",
                failed,
                len(points),
                document_id,
            )
        logger.debug(
            "Stored {} evidence chunk(s) for document {} (workspace {})",
            len(points),
            document_id,
            workspace_id,
        )
        return len(points)

    # ------------------------------------------------------------------
    # Spreadsheet rows (app/processing/tables.py, app/rag/table_query.py)
    # ------------------------------------------------------------------

    def _drop_table_cache(self, workspace_id: str) -> None:
        for key in [k for k in self._table_row_cache if k[0] == workspace_id]:
            del self._table_row_cache[key]

    def _table_filter(
        self,
        workspace_id: str,
        document_id: Optional[str] = None,
        record: Optional[str] = None,
        sheet_index: Optional[int] = None,
    ) -> Filter:
        must = [FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))]
        if document_id is not None:
            must.append(
                FieldCondition(key="original_document_id", match=MatchValue(value=document_id))
            )
        if record is not None:
            must.append(FieldCondition(key="record", match=MatchValue(value=record)))
        if sheet_index is not None:
            must.append(FieldCondition(key="sheet_index", match=MatchValue(value=sheet_index)))
        return Filter(must=must)

    async def replace_document_tables(
        self,
        workspace_id: str,
        document_id: str,
        manifest: Dict[str, Any],
        rows: Sequence[Dict[str, Any]],
        filename: str = "",
    ) -> int:
        """Store a workbook's manifest and rows, replacing any earlier version.

        The old rows are deleted first, so a re-read workbook with fewer rows
        leaves no stale tail. The manifest is written LAST: a reader that finds
        it can trust the rows are complete, and an interrupted write leaves no
        manifest, so the workbook is simply not computable rather than
        computed over half its rows.
        """
        if self.client is None:
            await self.initialize()
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.tables_collection,
            points_selector=models.FilterSelector(
                filter=self._table_filter(workspace_id, document_id)
            ),
        )
        self._drop_table_cache(workspace_id)
        base = {"workspace_id": workspace_id, "original_document_id": document_id}

        def _pid(key: str) -> str:
            return str(uuid5(NAMESPACE_DNS, f"table:{workspace_id}:{document_id}:{key}"))

        points = [
            PointStruct(
                id=_pid(f"{r['sheet_index']}:{r['row']}"),
                vector={},
                payload={**base, "record": TABLE_ROW, **r},
            )
            for r in rows
        ]
        for i in range(0, len(points), _TABLE_UPSERT_BATCH):
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.tables_collection,
                points=points[i : i + _TABLE_UPSERT_BATCH],
            )
        await asyncio.to_thread(
            self.client.upsert,
            collection_name=self.tables_collection,
            points=[
                PointStruct(
                    id=_pid("manifest"),
                    vector={},
                    payload={
                        **base,
                        "record": TABLE_MANIFEST,
                        "filename": filename,
                        "row_count": len(rows),
                        "manifest": manifest,
                    },
                )
            ],
        )
        self._bump_evidence_version(workspace_id)
        logger.debug(
            "Stored {} spreadsheet row(s) for document {} (workspace {})",
            len(points),
            document_id,
            workspace_id,
        )
        return len(points)

    async def delete_document_tables(self, workspace_id: str, document_id: str) -> None:
        """Remove one document's rows and manifest (no-op when it has none)."""
        if self.client is None:
            await self.initialize()
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.tables_collection,
            points_selector=models.FilterSelector(
                filter=self._table_filter(workspace_id, document_id)
            ),
        )
        self._drop_table_cache(workspace_id)

    async def get_table_manifests(self, workspace_id: str) -> List[Dict[str, Any]]:
        """``[{document_id, filename, row_count, manifest}]`` for every stored
        workbook of the workspace. Empty when it has none."""
        if self.client is None:
            await self.initialize()
        points = await asyncio.to_thread(
            self._scroll_sync,
            self.tables_collection,
            self._table_filter(workspace_id, record=TABLE_MANIFEST),
            100,
        )
        out = []
        for p in points:
            pl = p.payload or {}
            out.append(
                {
                    "document_id": str(pl.get("original_document_id") or ""),
                    "filename": pl.get("filename") or "",
                    "row_count": pl.get("row_count") or 0,
                    "manifest": pl.get("manifest") or {},
                }
            )
        return out

    async def get_table_rows(
        self, workspace_id: str, document_id: str, sheet_index: int
    ) -> List[Dict[str, Any]]:
        """Every stored row of one sheet, in spreadsheet order."""
        if self.client is None:
            await self.initialize()
        key = (workspace_id, document_id, sheet_index, self.evidence_version(workspace_id))
        cached = self._table_row_cache.get(key)
        if cached is not None:
            return cached
        points = await asyncio.to_thread(
            self._scroll_sync,
            self.tables_collection,
            self._table_filter(workspace_id, document_id, TABLE_ROW, sheet_index),
            1000,
        )
        rows = sorted((p.payload or {} for p in points), key=lambda r: int(r.get("row") or 0))
        if len(self._table_row_cache) >= _TABLE_ROW_CACHE_SIZE:
            self._table_row_cache.pop(next(iter(self._table_row_cache)))
        self._table_row_cache[key] = rows
        return rows

    async def get_document_chunks(
        self, workspace_id: str, document_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Stored chunks (payload dicts) in reading order, for evidence packing."""
        if self.client is None:
            await self.initialize()
        if not await self.ensure_workspace_indexed(workspace_id):
            return []
        points = await asyncio.to_thread(
            self._scroll_sync,
            self.chunks_collection,
            self._chunk_filter(workspace_id, document_ids),
            200,
        )
        payloads = [p.payload or {} for p in points]
        payloads.sort(
            key=lambda pl: (
                str(pl.get("original_document_id") or ""),
                _chunk_position(pl, pl.get("metadata") or {}),
            )
        )
        return payloads

    async def ensure_workspace_indexed(self, workspace_id: str) -> bool:
        """Re-index a workspace's legacy chunks into the evidence collection.

        Runs once per workspace per process. A document is migrated when it has
        chunks in the legacy collection but none in the evidence collection;
        its legacy chunks are rebuilt in chunk_index order and re-chunked.
        Legacy documents have no page markers, so they gain page numbers only
        when re-read (a new upload or document_reload_request).

        Returns False if migration could not complete, so callers fall back to
        the legacy collection instead of silently missing documents.
        """
        if not workspace_id:
            return True
        if workspace_id in self._indexed_workspaces:
            return True
        lock = self._index_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            if workspace_id in self._indexed_workspaces:
                return True
            try:
                legacy = await asyncio.to_thread(
                    self._scroll_sync,
                    self.collection_name,
                    self._search_filter_all_chunks(workspace_id),
                    200,
                )
                current_ids = {
                    str((p.payload or {}).get("original_document_id") or "")
                    for p in await asyncio.to_thread(
                        self._scroll_sync,
                        self.chunks_collection,
                        self._chunk_filter(workspace_id),
                        200,
                    )
                }
                by_doc: Dict[str, List[Dict[str, Any]]] = {}
                for p in legacy:
                    pl = p.payload or {}
                    meta = pl.get("metadata") or {}
                    doc_id = str(
                        pl.get("original_document_id")
                        or meta.get("original_document_id")
                        or pl.get("document_id")
                        or ""
                    )
                    if doc_id and doc_id not in current_ids:
                        by_doc.setdefault(doc_id, []).append(pl)
                for doc_id, payloads in by_doc.items():
                    payloads.sort(key=lambda pl: _chunk_position(pl, pl.get("metadata") or {}))
                    text = reassemble([{"content": pl.get("content") or ""} for pl in payloads])
                    if not text.strip():
                        continue
                    meta = dict(payloads[0].get("metadata") or {})
                    meta.setdefault("uploaded_at", payloads[0].get("uploaded_at"))
                    meta["reindexed_from_legacy"] = True
                    await self.add_document_chunks(workspace_id, doc_id, text, meta)
                if by_doc:
                    logger.info(
                        "Re-indexed {} legacy document(s) into {} for workspace {}",
                        len(by_doc),
                        self.chunks_collection,
                        workspace_id,
                    )
                self._indexed_workspaces.add(workspace_id)
                return True
            except Exception as index_err:
                logger.error(
                    "Re-indexing workspace {} failed; serving from the legacy "
                    "collection: {}",
                    workspace_id,
                    index_err,
                )
                return False

    @staticmethod
    def _search_filter_all_chunks(workspace_id: str) -> Filter:
        """Every document chunk of a workspace in the legacy collection (cache
        rows excluded), including chunks whose embedding failed."""
        return Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                IsEmptyCondition(is_empty=PayloadField(key="cache_kind")),
            ]
        )

    async def get_all_documents(
        self,
        workspace_id: str,
        limit_per_batch: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve all document contents for a workspace, grouped by original_document_id.
        Returns list of {"document_id", "content", "uploaded_at"} sorted by uploaded_at (oldest first).

        Args:
            workspace_id: Workspace identifier
            limit_per_batch: Scroll batch size

        Returns:
            List of document dicts with combined content per document, sorted by upload order
        """
        if self.client is None:
            await self.initialize()
        try:
            if await self.ensure_workspace_indexed(workspace_id):
                out = await asyncio.to_thread(
                    self._get_all_chunk_documents_sync, workspace_id, limit_per_batch
                )
            else:
                out = await asyncio.to_thread(
                    self._get_all_documents_sync,
                    workspace_id,
                    limit_per_batch,
                )
            logger.debug(
                "Retrieved {} documents for workspace {}",
                len(out),
                workspace_id,
            )
            return out
        except Exception as e:
            logger.error("get_all_documents failed: {}", str(e))
            raise VectorStoreError(
                "get_all_documents failed",
                details={"workspace_id": workspace_id, "error": str(e)},
            )

    async def delete_documents(
        self,
        workspace_id: str,
        document_ids: List[str],
    ) -> bool:
        """
        Delete all chunks for the given document IDs in a workspace.

        Args:
            workspace_id: Workspace identifier
            document_ids: List of document IDs (original_document_id) to remove

        Returns:
            True if successful
        """
        if not document_ids:
            return True
        if self.client is None:
            await self.initialize()
        try:
            delete_filter = Filter(
                must=[
                    FieldCondition(
                        key="workspace_id", match=MatchValue(value=workspace_id)
                    ),
                    FieldCondition(
                        key="original_document_id", match=MatchAny(any=document_ids)
                    ),
                ]
            )
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=delete_filter),
            )
            # The evidence copy too — a deleted document must leave no chunks
            # anywhere, or it reappears in retrieval and panels.
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.chunks_collection,
                points_selector=models.FilterSelector(
                    filter=self._chunk_filter(workspace_id, document_ids)
                ),
            )
            # And the spreadsheet rows: a deleted workbook must not stay
            # computable.
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.tables_collection,
                points_selector=models.FilterSelector(
                    filter=self._chunk_filter(workspace_id, document_ids)
                ),
            )
            self._drop_table_cache(workspace_id)
            self._bump_evidence_version(workspace_id)
            if self._graph_withdrawal is not None:
                await self._graph_withdrawal(workspace_id, list(document_ids))
            logger.info(
                "Deleted documents {} for workspace {}",
                document_ids,
                workspace_id,
            )
            return True
        except Exception as e:
            logger.error("Failed to delete documents: {}", str(e))
            raise VectorStoreError(
                "Failed to delete documents",
                details={
                    "workspace_id": workspace_id,
                    "document_ids": document_ids,
                    "error": str(e),
                },
            )

    async def delete_workspace(self, workspace_id: str) -> bool:
        """
        Delete all documents for a workspace.

        Args:
            workspace_id: Workspace identifier

        Returns:
            True if successful
        """
        try:
            # Create filter for workspace
            workspace_filter = Filter(
                must=[
                    FieldCondition(
                        key="workspace_id", match=MatchValue(value=workspace_id)
                    )
                ]
            )

            if self.client is None:
                await self.initialize()
            # Every collection: the legacy one holds the state caches, the
            # evidence one the document chunks, the tables one the spreadsheet
            # rows. A compliance purge that misses any leaves client data behind.
            for collection in (
                self.collection_name,
                self.chunks_collection,
                self.tables_collection,
            ):
                await asyncio.to_thread(
                    self.client.delete,
                    collection_name=collection,
                    points_selector=models.FilterSelector(filter=workspace_filter),
                )
            self._indexed_workspaces.discard(workspace_id)
            self._drop_table_cache(workspace_id)
            self._bump_evidence_version(workspace_id)
            if self._graph_withdrawal is not None:
                # Raises on failure: the compliance purge must retry, not
                # report success with facts left in the graph.
                await self._graph_withdrawal(workspace_id, None)

            logger.info("Deleted all documents for workspace: {}", workspace_id)
            return True

        except Exception as e:
            logger.error("Failed to delete workspace documents: {}", str(e))
            raise VectorStoreError(
                "Failed to delete workspace documents",
                details={"workspace_id": workspace_id, "error": str(e)},
            )

    async def _generate_embedding(
        self, text: str, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[float]:
        """
        Embed text with the configured model and task type.

        Raises instead of padding, truncating or substituting a zero vector:
        each of those silently degrades retrieval, and the caller is the one
        who knows what a safe fallback is (see add_document).
        """
        from app.ai.gemini_client import gemini_client

        if not gemini_client.is_available():
            gemini_client.initialize()
        embedding = await gemini_client.embed_text(text, task_type=task_type)
        if len(embedding) != self.vector_size:
            raise VectorStoreError(
                "Embedding size does not match the collection",
                details={"expected": self.vector_size, "got": len(embedding)},
            )
        return embedding

    async def close(self) -> None:
        """Close Qdrant client connection."""
        if self.client:
            # Qdrant client doesn't require explicit close
            self.client = None
            logger.info("Qdrant client closed")

    async def upsert_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        role: str,
        *,
        chat_id: Optional[str] = None,
    ) -> bool:
        """
        Persist workspace-role selection in Qdrant so it survives process restarts.

        Writes:
        - user/workspace default role point
        - optional chat-specific role point
        """
        if self.client is None:
            await self.initialize()
        role_upper = (role or "").strip().upper()
        if role_upper not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            return False

        def _point_id(scope: str) -> str:
            return str(uuid5(NAMESPACE_DNS, f"role-cache:{workspace_id}:{user_id}:{scope}"))

        now_iso = datetime.utcnow().isoformat()
        points: List[PointStruct] = []

        user_payload = {
            "workspace_id": workspace_id,
            "document_id": "__workspace_role_cache__",
            "original_document_id": "__workspace_role_cache__",
            "content": "",
            "uploaded_at": now_iso,
            "cache_kind": "workspace_role_cache",
            "cache_scope": "user_workspace",
            "cache_user_id": str(user_id),
            "cache_chat_id": "__default__",
            "cache_role": role_upper,
            "metadata": {
                "kind": "workspace_role_cache",
                "scope": "user_workspace",
                "user_id": str(user_id),
                "chat_id": "__default__",
                "role": role_upper,
                "updated_at": now_iso,
            },
        }
        points.append(
            PointStruct(id=_point_id("__default__"), vector=[0.0] * self.vector_size, payload=user_payload)
        )

        if chat_id:
            chat_payload = {
                **user_payload,
                "cache_scope": "chat",
                "cache_chat_id": str(chat_id),
                "metadata": {
                    **user_payload["metadata"],
                    "scope": "chat",
                    "chat_id": str(chat_id),
                },
            }
            points.append(
                PointStruct(
                    id=_point_id(str(chat_id)),
                    vector=[0.0] * self.vector_size,
                    payload=chat_payload,
                )
            )

        try:
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=points,
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to upsert workspace role cache (workspace={}, user={}): {}",
                workspace_id,
                user_id,
                e,
            )
            return False

    async def get_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        *,
        chat_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Read workspace role from Qdrant cache (chat-specific first, then user/workspace).
        """
        if self.client is None:
            await self.initialize()

        async def _read(scope_chat_id: str) -> Optional[str]:
            filt = Filter(
                must=[
                    FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                    FieldCondition(key="cache_kind", match=MatchValue(value="workspace_role_cache")),
                    FieldCondition(key="cache_user_id", match=MatchValue(value=str(user_id))),
                    FieldCondition(key="cache_chat_id", match=MatchValue(value=scope_chat_id)),
                ]
            )
            try:
                result, _next = await asyncio.to_thread(
                    self.client.scroll,
                    collection_name=self.collection_name,
                    scroll_filter=filt,
                    limit=1,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                return None
            if not result:
                return None
            payload = result[0].payload or {}
            role = str(payload.get("cache_role") or "").strip().upper()
            if role in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                return role
            return None

        if chat_id:
            role = await _read(str(chat_id))
            if role:
                return role
        return await _read("__default__")

    # ------------------------------------------------------------------
    # Generic per-workspace state cache (analysis report, procedural
    # timeline, applicable laws, workspace meta, etc.)
    #
    # Each entry is a payload-only Qdrant point keyed deterministically by
    # (workspace_id, cache_kind, scope), where scope is "__default__" for
    # workspace-level caches or the chat_id string for chat-scoped caches.
    # Payload shape mirrors `upsert_workspace_role` so we can reuse the
    # collection without a schema migration: the JSON-serialized data lives
    # in `cache_data` and a `cache_kind` filter isolates rows.
    # ------------------------------------------------------------------

    async def upsert_workspace_state_cache(
        self,
        workspace_id: str,
        kind: str,
        data: Any,
        *,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Persist arbitrary workspace state as a JSON payload in Qdrant.

        The data is serialized to JSON. A scope of "__default__" is used for
        workspace-level caches; a chat_id scope is used for chat-level caches.
        Subsequent upserts overwrite the previous point.
        """
        if not workspace_id or not kind:
            return False
        if self.client is None:
            await self.initialize()

        scope = str(chat_id) if chat_id else "__default__"

        try:
            import json as _json

            cache_data_json = _json.dumps(data, default=str)
        except Exception as e:
            logger.warning(
                "State cache serialization failed (workspace={}, kind={}): {}",
                workspace_id,
                kind,
                e,
            )
            return False

        point_id = str(
            uuid5(NAMESPACE_DNS, f"state-cache:{workspace_id}:{kind}:{scope}")
        )
        now_iso = datetime.utcnow().isoformat()
        payload = {
            "workspace_id": workspace_id,
            "document_id": "__workspace_state_cache__",
            "original_document_id": "__workspace_state_cache__",
            "content": "",
            "uploaded_at": now_iso,
            "cache_kind": str(kind),
            "cache_scope": "chat" if chat_id else "workspace",
            "cache_chat_id": scope,
            "cache_data": cache_data_json,
            "metadata": {
                "kind": str(kind),
                "scope": "chat" if chat_id else "workspace",
                "chat_id": scope,
                "updated_at": now_iso,
            },
        }
        point = PointStruct(
            id=point_id, vector=[0.0] * self.vector_size, payload=payload
        )
        try:
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=[point],
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to upsert workspace state cache (workspace={}, kind={}): {}",
                workspace_id,
                kind,
                e,
            )
            return False

    async def get_workspace_state_cache(
        self,
        workspace_id: str,
        kind: str,
        *,
        chat_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Read a previously-persisted workspace state payload from Qdrant.

        Returns the JSON-decoded data, or None when the cache row is missing
        or the payload is malformed.
        """
        if not workspace_id or not kind:
            return None
        if self.client is None:
            await self.initialize()

        scope = str(chat_id) if chat_id else "__default__"
        filt = Filter(
            must=[
                FieldCondition(
                    key="workspace_id", match=MatchValue(value=workspace_id)
                ),
                FieldCondition(key="cache_kind", match=MatchValue(value=str(kind))),
                FieldCondition(
                    key="cache_chat_id", match=MatchValue(value=scope)
                ),
            ]
        )
        try:
            result, _next = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=filt,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning(
                "Failed to read workspace state cache (workspace={}, kind={}): {}",
                workspace_id,
                kind,
                e,
            )
            return None
        if not result:
            return None
        payload = result[0].payload or {}
        raw = payload.get("cache_data")
        if raw is None:
            return None
        try:
            import json as _json

            return _json.loads(raw)
        except Exception:
            # Backwards-compatibility: return raw value if it isn't JSON.
            return raw

    # ------------------------------------------------------------------
    # Per-user (lawyer-scoped) state cache.
    #
    # Used by the Drafting Assistant for template-library storage (full
    # extracted template text, keyed by template_id) so a single template
    # survives across all of a lawyer's workspaces and across restarts.
    #
    # Same metadata-only / zero-vector trick as the workspace state cache,
    # but partitioned on `cache_user_id` instead of `workspace_id`. We
    # reuse the existing collection without a schema migration.
    # ------------------------------------------------------------------

    async def upsert_user_state_cache(
        self,
        user_id: str,
        kind: str,
        scope_id: str,
        data: Any,
    ) -> bool:
        """Persist arbitrary lawyer-scoped state as JSON in Qdrant.

        scope_id discriminates rows within the same kind (e.g. template_id
        when kind="template"). Re-upserts overwrite the existing point.
        """
        if not user_id or not kind or not scope_id:
            return False
        if self.client is None:
            await self.initialize()

        try:
            import json as _json

            cache_data_json = _json.dumps(data, default=str)
        except Exception as e:
            logger.warning(
                "User state cache serialization failed (user={}, kind={}, scope={}): {}",
                user_id,
                kind,
                scope_id,
                e,
            )
            return False

        point_id = str(
            uuid5(NAMESPACE_DNS, f"user-state:{user_id}:{kind}:{scope_id}")
        )
        now_iso = datetime.utcnow().isoformat()
        payload = {
            # Reuse the workspace_id column with a sentinel so existing
            # workspace-filtered queries do not pick these rows up.
            "workspace_id": "__user_state_cache__",
            "document_id": "__user_state_cache__",
            "original_document_id": "__user_state_cache__",
            "content": "",
            "uploaded_at": now_iso,
            "cache_kind": str(kind),
            "cache_scope": "user",
            "cache_user_id": str(user_id),
            "cache_scope_id": str(scope_id),
            "cache_data": cache_data_json,
            "metadata": {
                "kind": str(kind),
                "scope": "user",
                "user_id": str(user_id),
                "scope_id": str(scope_id),
                "updated_at": now_iso,
            },
        }
        point = PointStruct(
            id=point_id, vector=[0.0] * self.vector_size, payload=payload
        )
        try:
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=[point],
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to upsert user state cache (user={}, kind={}, scope={}): {}",
                user_id,
                kind,
                scope_id,
                e,
            )
            return False

    async def get_user_state_cache(
        self,
        user_id: str,
        kind: str,
        scope_id: str,
    ) -> Optional[Any]:
        """Read a single lawyer-scoped state payload. Returns None if missing."""
        if not user_id or not kind or not scope_id:
            return None
        if self.client is None:
            await self.initialize()
        filt = Filter(
            must=[
                FieldCondition(
                    key="cache_user_id", match=MatchValue(value=str(user_id))
                ),
                FieldCondition(key="cache_kind", match=MatchValue(value=str(kind))),
                FieldCondition(
                    key="cache_scope_id", match=MatchValue(value=str(scope_id))
                ),
            ]
        )
        try:
            result, _next = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=filt,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning(
                "Failed to read user state cache (user={}, kind={}, scope={}): {}",
                user_id,
                kind,
                scope_id,
                e,
            )
            return None
        if not result:
            return None
        payload = result[0].payload or {}
        raw = payload.get("cache_data")
        if raw is None:
            return None
        try:
            import json as _json

            return _json.loads(raw)
        except Exception:
            return raw

    async def delete_user_state_cache(
        self,
        user_id: str,
        kind: str,
        scope_id: str,
    ) -> bool:
        """Delete a single lawyer-scoped state entry."""
        if not user_id or not kind or not scope_id:
            return False
        if self.client is None:
            await self.initialize()
        delete_filter = Filter(
            must=[
                FieldCondition(
                    key="cache_user_id", match=MatchValue(value=str(user_id))
                ),
                FieldCondition(key="cache_kind", match=MatchValue(value=str(kind))),
                FieldCondition(
                    key="cache_scope_id", match=MatchValue(value=str(scope_id))
                ),
            ]
        )
        try:
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=delete_filter),
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to delete user state cache (user={}, kind={}, scope={}): {}",
                user_id,
                kind,
                scope_id,
                e,
            )
            return False

    async def list_user_state_cache(
        self,
        user_id: str,
        kind: str,
        *,
        limit: int = 256,
    ) -> List[Dict[str, Any]]:
        """List all lawyer-scoped entries for a given kind.

        Returns a list of {scope_id, data} dicts. Used to enumerate a
        lawyer's templates when populating chat context.
        """
        if not user_id or not kind:
            return []
        if self.client is None:
            await self.initialize()
        filt = Filter(
            must=[
                FieldCondition(
                    key="cache_user_id", match=MatchValue(value=str(user_id))
                ),
                FieldCondition(key="cache_kind", match=MatchValue(value=str(kind))),
            ]
        )
        try:
            result, _next = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=filt,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            logger.warning(
                "Failed to list user state cache (user={}, kind={}): {}",
                user_id,
                kind,
                e,
            )
            return []
        out: List[Dict[str, Any]] = []
        import json as _json

        for point in result or []:
            payload = point.payload or {}
            scope_id = payload.get("cache_scope_id")
            raw = payload.get("cache_data")
            try:
                data = _json.loads(raw) if raw is not None else None
            except Exception:
                data = raw
            if scope_id:
                out.append({"scope_id": scope_id, "data": data})
        return out

    async def delete_workspace_state_cache(
        self,
        workspace_id: str,
        kind: Optional[str] = None,
    ) -> bool:
        """Remove every cache point for a workspace, optionally narrowed by kind.

        Used during workspace deletion so cached state does not survive
        the parent workspace.
        """
        if not workspace_id:
            return False
        if self.client is None:
            await self.initialize()

        must: List[Any] = [
            FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))
        ]
        if kind:
            must.append(
                FieldCondition(key="cache_kind", match=MatchValue(value=str(kind)))
            )
        else:
            # Limit deletion to known cache kinds so document chunks remain.
            must.append(
                FieldCondition(
                    key="cache_kind",
                    match=MatchAny(
                        any=[
                            "workspace_role_cache",
                            "workspace_meta",
                            "analysis_report",
                            "procedural_timeline",
                            "applicable_laws",
                            "custom_instructions",
                            "drafting_suggestions",
                            "drafting_session",
                        ]
                    ),
                )
            )
        delete_filter = Filter(must=must)
        try:
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=delete_filter),
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to delete workspace state cache (workspace={}, kind={}): {}",
                workspace_id,
                kind,
                e,
            )
            return False


# Global vector store instance
vector_store = QdrantStore()
