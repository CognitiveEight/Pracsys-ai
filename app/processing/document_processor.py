"""
Main document processor coordinating file handling, readability scoring,
and vector storage.
"""
from typing import Dict, Any, List
from pathlib import Path
import httpx
from loguru import logger

from app.processing.file_handlers import file_processor
from app.processing.readability import readability_scorer
from app.vectorstore.qdrant import vector_store
from app.observability.audit import audit_logger
from app.observability.trace import tracer
from app.utils.exceptions import FileProcessingError


class DocumentProcessor:
    """
    Main document processor handling:
    - File download from URLs
    - Text extraction (PDF, DOCX, MD, TXT, images)
    - Readability scoring
    - Vector storage (user-scoped)
    """
    
    def __init__(self):
        self.http_client: httpx.AsyncClient = None
        self.max_file_size = 500 * 1024 * 1024  # 500MB
    
    async def initialize(self):
        """Initialize HTTP client and vector store."""
        self.http_client = httpx.AsyncClient(
            timeout=300.0,  # 5 minutes for large files
            follow_redirects=True
        )
        
        # Initialize vector store
        try:
            await vector_store.initialize()
            logger.success("✅ Vector store initialized")
        except Exception as e:
            logger.warning(f"⚠️ Vector store initialization failed: {e}")
            logger.warning("Documents will be processed but not stored in vector DB")
        
        logger.info("Document processor initialized")
    
    async def cleanup(self):
        """Cleanup resources."""
        if self.http_client:
            await self.http_client.aclose()
        logger.info("Document processor cleaned up")
    
    @tracer.trace("process_document")
    async def process_document(
        self,
        file_url: str,
        filename: str,
        workspace_id: str,
        user_id: str,
        document_id: str,
        metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Process a single document: download, extract, score, store.
        
        Args:
            file_url: URL to download file from
            filename: Original filename
            workspace_id: Workspace identifier
            user_id: User identifier
            document_id: Document identifier
            metadata: Additional metadata
            
        Returns:
            Processing result with readability score
        """
        logger.info(
            "Processing document: {} for user {} in workspace {}",
            filename,
            user_id,
            workspace_id
        )
        
        try:
            # Download file
            file_content, file_size = await self._download_file(file_url, filename)
            
            # Extract text using GCP-powered file processor
            extracted_text = file_processor.process_file_by_extension(file_content, filename)
            
            # Create extraction metadata
            extraction_metadata = {
                "extraction_method": "gcp_vision" if any(ext in filename.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']) else "direct",
                "has_errors": extracted_text.startswith("[Error") or extracted_text.startswith("["),
                "text_length": len(extracted_text)
            }
            
            # Calculate readability score
            readability_result = readability_scorer.calculate_score(
                extracted_text,
                extraction_metadata,
                file_size
            )
            
            # Store in vector database (user-scoped)
            stored_in_vector_db = False
            if extracted_text and readability_result["readable"]:
                try:
                    await self._store_in_vector_db(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        document_id=document_id,
                        text=extracted_text,
                        filename=filename,
                        metadata={
                            **(metadata or {}),
                            **extraction_metadata,
                            "readability": readability_result,
                            "file_size": file_size
                        }
                    )
                    stored_in_vector_db = True
                except Exception as vector_error:
                    logger.warning(f"Could not store in vector DB: {vector_error}")
                    # Continue processing even if vector storage fails
                    stored_in_vector_db = False
            
            # Prepare result
            result = {
                "success": True,
                "document_id": document_id,
                "filename": filename,
                "file_size": file_size,
                "extracted_text_length": len(extracted_text),
                "readability_score": readability_result["total_score"],
                "readability_rating": readability_result["rating"],
                "readable": readability_result["readable"],
                "metadata": extraction_metadata,
                "readability_breakdown": readability_result,
                "stored_in_vector_db": stored_in_vector_db
            }
            
            # Log audit event
            await audit_logger.log_event(
                event_type="document_processed",
                data=result,
                workspace_id=workspace_id,
                user_id=user_id
            )
            
            logger.success(
                "Document processed: {} - Readability: {:.2f}% ({})",
                filename,
                readability_result["total_score"],
                readability_result["rating"]
            )
            
            return result
            
        except Exception as e:
            logger.error("Failed to process document {}: {}", filename, str(e))
            
            # Log error
            await audit_logger.log_error(
                error_type="document_processing",
                error_message=str(e),
                data={
                    "filename": filename,
                    "document_id": document_id
                },
                workspace_id=workspace_id,
                user_id=user_id
            )
            
            return {
                "success": False,
                "document_id": document_id,
                "filename": filename,
                "error": str(e),
                "readability_score": 0,
                "readable": False
            }
    
    @tracer.trace("process_batch")
    async def process_batch(
        self,
        files: List[Dict[str, Any]],
        workspace_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Process multiple documents in batch.
        
        Args:
            files: List of file dictionaries with url, filename, _id
            workspace_id: Workspace identifier
            user_id: User identifier
            
        Returns:
            Batch processing results
        """
        logger.info(
            "Processing batch of {} documents for user {} in workspace {}",
            len(files),
            user_id,
            workspace_id
        )
        
        results = []
        success_count = 0
        total_score = 0.0
        
        for file_info in files:
            result = await self.process_document(
                file_url=file_info["url"],
                filename=file_info["filename"],
                workspace_id=workspace_id,
                user_id=user_id,
                document_id=file_info["_id"],
                metadata={
                    "uploaded_at": file_info.get("uploadedAt"),
                    "path": file_info.get("path")
                }
            )
            
            results.append(result)
            
            if result["success"]:
                success_count += 1
                total_score += result["readability_score"]
        
        avg_score = total_score / success_count if success_count > 0 else 0
        
        batch_result = {
            "total_files": len(files),
            "successful": success_count,
            "failed": len(files) - success_count,
            "average_readability_score": round(avg_score, 2),
            "results": results
        }
        
        logger.success(
            "Batch processing complete: {}/{} successful, avg score: {:.2f}%",
            success_count,
            len(files),
            avg_score
        )
        
        return batch_result
    
    async def _download_file(self, url: str, filename: str) -> tuple[bytes, int]:
        """
        Download file from URL.
        
        Returns:
            Tuple of (content, size)
        """
        if not self.http_client:
            await self.initialize()
        
        try:
            # Clean URL (remove trailing quotes if present)
            clean_url = url.rstrip('"')
            
            logger.debug("Downloading file from: {}", clean_url)
            
            response = await self.http_client.get(clean_url)
            response.raise_for_status()
            
            content = response.content
            size = len(content)
            
            # Check size limit
            if size > self.max_file_size:
                raise FileProcessingError(
                    f"File too large: {size / 1024 / 1024:.2f}MB (max: {self.max_file_size / 1024 / 1024}MB)",
                    details={"filename": filename, "size": size}
                )
            
            logger.debug(
                "Downloaded file: {} ({:.2f}MB)",
                filename,
                size / 1024 / 1024
            )
            
            return content, size
            
        except httpx.HTTPError as e:
            logger.error("HTTP error downloading {}: {}", filename, str(e))
            raise FileProcessingError(
                f"Failed to download file: {str(e)}",
                details={"url": url, "filename": filename}
            )
        except Exception as e:
            logger.error("Error downloading {}: {}", filename, str(e))
            raise FileProcessingError(
                f"Failed to download file: {str(e)}",
                details={"url": url, "filename": filename}
            )
    
    async def _store_in_vector_db(
        self,
        workspace_id: str,
        user_id: str,
        document_id: str,
        text: str,
        filename: str,
        metadata: Dict[str, Any]
    ):
        """
        Store document in vector database (user-scoped).
        
        Chunks large documents for better retrieval.
        """
        logger.debug("Storing document in vector DB: {}", filename)
        
        # Chunk text if too large (every 1000 words)
        words = text.split()
        chunk_size = 1000
        chunks = []
        
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i + chunk_size])
            chunks.append(chunk)
        
        logger.debug("Split document into {} chunks", len(chunks))
        
        # Store each chunk
        for idx, chunk in enumerate(chunks):
            chunk_id = f"{document_id}_chunk_{idx}"
            
            await vector_store.add_document(
                workspace_id=workspace_id,
                document_id=chunk_id,
                content=chunk,
                metadata={
                    **metadata,
                    "user_id": user_id,  # USER-SCOPED
                    "original_document_id": document_id,
                    "filename": filename,
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    "is_chunk": len(chunks) > 1
                }
            )
        
        logger.success(
            "Stored {} chunks for document {} (user: {})",
            len(chunks),
            filename,
            user_id
        )


# Global document processor instance
document_processor = DocumentProcessor()

