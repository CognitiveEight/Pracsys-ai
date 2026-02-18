"""
Qdrant vector store integration for document storage and retrieval.

Note: Qdrant Python client is synchronous (blocking HTTP). We run initialization
in a thread pool so the event loop stays responsive and the socket connection
is not dropped during init/timeouts.
"""
import asyncio
from typing import List, Dict, Any, Optional
from uuid import uuid4
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
)
from qdrant_client.http import models
from loguru import logger

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
                    size=self.vector_size,
                    distance=Distance.COSINE
                )
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
                "Failed to initialize Qdrant",
                details={"error": str(e)}
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
                        size=self.vector_size,
                        distance=Distance.COSINE
                    )
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
        metadata: Optional[Dict[str, Any]] = None
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
            
            point = PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload
            )
            
            # Upsert point (run in thread so event loop is not blocked)
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=[point],
            )
            
            logger.debug(
                "Added document to vector store: {} (workspace: {})",
                document_id,
                workspace_id
            )
            
            return point_id
            
        except Exception as e:
            logger.error("Failed to add document: {}", str(e))
            raise VectorStoreError(
                "Failed to add document",
                details={
                    "workspace_id": workspace_id,
                    "document_id": document_id,
                    "error": str(e)
                }
            )
    
    async def search(
        self,
        workspace_id: str,
        query: str,
        limit: int = 5
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
                        key="workspace_id",
                        match=MatchValue(value=workspace_id)
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
                formatted_results.append({
                    "id": result.id,
                    "score": result.score,
                    "content": result.payload.get("content"),
                    "metadata": result.payload.get("metadata", {}),
                    "document_id": result.payload.get("document_id")
                })
            
            logger.debug(
                "Found {} results for query in workspace {}",
                len(formatted_results),
                workspace_id
            )
            
            return formatted_results
            
        except Exception as e:
            logger.error("Search failed: {}", str(e))
            raise VectorStoreError(
                "Search failed",
                details={"workspace_id": workspace_id, "error": str(e)}
            )
    
    def _get_all_documents_sync(
        self,
        workspace_id: str,
        limit_per_batch: int,
    ) -> List[Dict[str, Any]]:
        """Sync scroll + combine; run via to_thread to avoid blocking the event loop."""
        workspace_filter = Filter(
            must=[
                FieldCondition(
                    key="workspace_id",
                    match=MatchValue(value=workspace_id)
                )
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
                orig_id = payload.get("original_document_id") or payload.get("document_id")
                uploaded_at = payload.get("uploaded_at") or ""
                meta = payload.get("metadata") or {}
                if not orig_id:
                    orig_id = meta.get("original_document_id") or payload.get("document_id")
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
            out.append({
                "document_id": doc_id,
                "content": combined,
                "uploaded_at": uploaded_at,
            })
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
                details={"workspace_id": workspace_id, "error": str(e)}
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
                        key="workspace_id",
                        match=MatchValue(value=workspace_id)
                    ),
                    FieldCondition(
                        key="original_document_id",
                        match=MatchAny(any=document_ids)
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
                details={"workspace_id": workspace_id, "document_ids": document_ids, "error": str(e)}
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
                        key="workspace_id",
                        match=MatchValue(value=workspace_id)
                    )
                ]
            )
            
            # Delete points (run in thread so event loop is not blocked)
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(
                    filter=workspace_filter
                ),
            )
            
            logger.info("Deleted all documents for workspace: {}", workspace_id)
            return True
            
        except Exception as e:
            logger.error("Failed to delete workspace documents: {}", str(e))
            raise VectorStoreError(
                "Failed to delete workspace documents",
                details={"workspace_id": workspace_id, "error": str(e)}
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
                embedding = embedding[:self.vector_size]
            
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


# Global vector store instance
vector_store = QdrantStore()

