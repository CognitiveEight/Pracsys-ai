"""
Qdrant vector store integration for document storage and retrieval.
"""
from typing import List, Dict, Any, Optional
from uuid import uuid4
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue
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
    
    async def initialize(self) -> None:
        """Initialize Qdrant client and create collection if needed."""
        try:
            logger.info("Initializing Qdrant client...")
            
            # Initialize client
            if settings.qdrant_api_key:
                self.client = QdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key
                )
            else:
                self.client = QdrantClient(url=settings.qdrant_url)
            
            # Create collection if it doesn't exist
            await self._ensure_collection_exists()
            
            logger.success("Qdrant client initialized")
            
        except Exception as e:
            logger.error("Failed to initialize Qdrant: {}", str(e))
            raise VectorStoreError(
                "Failed to initialize Qdrant",
                details={"error": str(e)}
            )
    
    async def _ensure_collection_exists(self) -> None:
        """Create collection if it doesn't exist."""
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
            
            # Create point
            point_id = str(uuid4())
            payload = {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "content": content,
                "metadata": metadata or {}
            }
            
            point = PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload
            )
            
            # Upsert point
            self.client.upsert(
                collection_name=self.collection_name,
                points=[point]
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
            
            # Search
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_embedding,
                query_filter=workspace_filter,
                limit=limit
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
            
            # Delete points
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(
                    filter=workspace_filter
                )
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

