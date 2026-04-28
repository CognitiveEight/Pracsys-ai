"""
Qdrant vector store integration for document storage and retrieval.

Note: Qdrant Python client is synchronous (blocking HTTP). We run initialization
in a thread pool so the event loop stays responsive and the socket connection
is not dropped during init/timeouts.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_DNS, uuid4, uuid5

from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.models import (Distance, FieldCondition, Filter, MatchAny,
                                  MatchValue, PointStruct, VectorParams)

from app.core.config import settings
from app.utils.exceptions import VectorStoreError


class QdrantStore:
    """
    Qdrant vector store wrapper for managing document embeddings.
    """

    def __init__(self):
        self.client: Optional[QdrantClient] = None
        self.collection_name = settings.qdrant_collection_name
        self.vector_size = 768  # Google Gemini embedding size (text-embedding-004)

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
            # Generate embedding (placeholder - will implement with actual embedding model)
            embedding = await self._generate_embedding(content)

            # Create point (original_document_id for filtering: chunks use same doc id)
            point_id = str(uuid4())
            meta = metadata or {}
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

    async def search(
        self, workspace_id: str, query: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Search for similar documents in a workspace.

        Args:
            workspace_id: Workspace identifier
            query: Search query
            limit: Maximum number of results

        Returns:
            List of search results
        """
        try:
            # Generate query embedding
            query_embedding = await self._generate_embedding(query)

            # Create filter for workspace
            workspace_filter = Filter(
                must=[
                    FieldCondition(
                        key="workspace_id", match=MatchValue(value=workspace_id)
                    )
                ]
            )

            # Search (run in thread so event loop is not blocked)
            results = await asyncio.to_thread(
                self.client.search,
                collection_name=self.collection_name,
                query_vector=query_embedding,
                query_filter=workspace_filter,
                limit=limit,
            )

            # Format results
            formatted_results = []
            for result in results:
                formatted_results.append(
                    {
                        "id": result.id,
                        "score": result.score,
                        "content": result.payload.get("content"),
                        "metadata": result.payload.get("metadata", {}),
                        "document_id": result.payload.get("document_id"),
                    }
                )

            logger.debug(
                "Found {} results for query in workspace {}",
                len(formatted_results),
                workspace_id,
            )

            return formatted_results

        except Exception as e:
            logger.error("Search failed: {}", str(e))
            raise VectorStoreError(
                "Search failed", details={"workspace_id": workspace_id, "error": str(e)}
            )

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
                doc_chunks[orig_id].append((uploaded_at, content))
            if next_offset is None:
                break
            offset = next_offset
        out = []
        for doc_id, chunks in doc_chunks.items():
            chunks_sorted = sorted(chunks, key=lambda x: (x[0] or "", x[1]))
            combined = " ".join(c for _, c in chunks_sorted)
            uploaded_at = chunks_sorted[0][0] if chunks_sorted else ""
            out.append(
                {
                    "document_id": doc_id,
                    "content": combined,
                    "uploaded_at": uploaded_at,
                }
            )
        out.sort(key=lambda x: (x["uploaded_at"] or "", x["document_id"]))
        return out

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

            # Delete points (run in thread so event loop is not blocked)
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=workspace_filter),
            )

            logger.info("Deleted all documents for workspace: {}", workspace_id)
            return True

        except Exception as e:
            logger.error("Failed to delete workspace documents: {}", str(e))
            raise VectorStoreError(
                "Failed to delete workspace documents",
                details={"workspace_id": workspace_id, "error": str(e)},
            )

    async def _generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for text using Google Gemini.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (768 dimensions for Google embeddings)
        """
        try:
            from app.ai.gemini_client import gemini_client

            # Initialize if needed
            if not gemini_client.is_available():
                gemini_client.initialize()

            # Generate embedding with Gemini
            embedding = await gemini_client.embed_text(text)

            # Google embeddings are 768 dimensions
            # Pad or truncate to match vector_size if needed
            if len(embedding) < self.vector_size:
                # Pad with zeros
                embedding.extend([0.0] * (self.vector_size - len(embedding)))
            elif len(embedding) > self.vector_size:
                # Truncate
                embedding = embedding[: self.vector_size]

            return embedding

        except Exception as e:
            logger.error("Error generating embedding with Gemini: {}", str(e))

            # Fallback: Return zero vector
            logger.warning("Using fallback zero vector for embedding")
            return [0.0] * self.vector_size

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
