"""
Main document processor coordinating file handling, readability scoring,
and vector storage.
"""
import json
import re
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

    def _extractive_summary_fallback(self, extracted_text: str, max_chars: int = 280) -> str:
        """Build a short extractive summary when AI summary is unavailable (used only as last resort)."""
        if not extracted_text or len(extracted_text.strip()) < 20:
            return "Insufficient extractable content."
        text = extracted_text.strip()
        for sep in (". ", ".\n", "\n\n"):
            parts = text.split(sep, 2)
            if len(parts) >= 2:
                first = (parts[0] + ".").strip()
                if 30 <= len(first) <= max_chars:
                    return first
                if len(first) > max_chars:
                    return first[: max_chars - 1].rsplit(" ", 1)[0] + "…"
        return text[: max_chars - 1] + "…" if len(text) > max_chars else text

    def _salvage_summary_from_truncated_json(self, raw: str) -> str:
        """If Gemini returned truncated JSON (e.g. Unterminated string), extract the summary value."""
        if not raw or "summary" not in raw:
            return ""
        idx = raw.find('"summary"')
        if idx < 0:
            return ""
        # Find the opening quote of the value (after ": or ": )
        colon = raw.find(":", idx + 9)
        if colon < 0:
            return ""
        start_quote = raw.find('"', colon + 1)
        if start_quote < 0:
            return ""
        start = start_quote + 1
        end = start
        while end < len(raw):
            if raw[end] == "\\" and end + 1 < len(raw):
                end += 2
                continue
            if raw[end] == '"':
                return raw[start:end].strip()
            end += 1
        return raw[start:].strip()

    async def _get_english_summary_fallback(self, extracted_text: str) -> str:
        """Get a one-sentence English summary from Gemini when main JSON fails (avoids Hindi/extractive)."""
        if not extracted_text or len(extracted_text.strip()) < 20:
            return "Insufficient extractable content."
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            snippet = (extracted_text.strip()[:800] + "...") if len(extracted_text) > 800 else extracted_text.strip()
            prompt = f"""Summarize the following document excerpt in one short sentence in English only. Output nothing but that single sentence, no quotes, no JSON.

Document excerpt:
{snippet}"""
            out = await gemini_client.generate(prompt, temperature=0.2, max_tokens=150)
            out = (out or "").strip()
            if out and "Error" not in out[:50] and len(out) >= 15:
                return out
        except Exception as e:
            logger.debug("English summary fallback failed: {}", str(e))
        return self._extractive_summary_fallback(extracted_text)

    async def _get_summary_and_evidentiary_strength(self, extracted_text: str, filename: str) -> Dict[str, str]:
        """
        Generate a short summary and evidentiary strength for the document using AI.
        Summary is always requested in English. Returns {"summary": str, "evidentiary_strength": "Low"|"Medium"|"High"}.
        """
        fallback_extractive = self._extractive_summary_fallback(extracted_text)
        if not extracted_text or len(extracted_text.strip()) < 30:
            english_fallback = await self._get_english_summary_fallback(extracted_text)
            return {"summary": english_fallback, "evidentiary_strength": "Low"}
        try:
            from app.ai.gemini_client import gemini_client
            gemini_client.initialize()
            text_snippet = (extracted_text.strip()[:4000] + "...") if len(extracted_text) > 4000 else extracted_text.strip()
            prompt = f"""You are a legal document analyst. For the document excerpt below, output a single JSON object with two keys:
- "summary": 2-3 short sentences in English only summarizing what the document is about and its relevance. Keep the summary under 250 characters. Write in English regardless of the document language.
- "evidentiaryStrength": exactly one of "Low", "Medium", or "High" (how useful as evidence).

Document excerpt:
{text_snippet}

Output only the JSON object, no markdown, no code block, no other text. Example:
{{"summary": "This document is a complaint letter to the police regarding a hacked social media account.", "evidentiaryStrength": "High"}}"""
            raw = await gemini_client.generate(prompt, temperature=0.2, max_tokens=600)
            if not raw or "Error" in (raw[:80] or ""):
                english_fallback = await self._get_english_summary_fallback(extracted_text)
                return {"summary": english_fallback, "evidentiary_strength": "Medium"}
            raw_clean = raw.strip()
            if "```" in raw_clean:
                code = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_clean)
                if code:
                    raw_clean = (code.group(1) or "").strip()
            start = raw_clean.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw_clean)):
                    if raw_clean[i] == "{":
                        depth += 1
                    elif raw_clean[i] == "}":
                        depth -= 1
                        if depth == 0:
                            raw_clean = raw_clean[start : i + 1]
                            break
            if not raw_clean or not raw_clean.strip().startswith("{"):
                logger.debug(
                    "Summary JSON not found (response len={}). First 300 chars: {}",
                    len(raw),
                    (raw[:300] if raw else "")
                )
                english_fallback = await self._get_english_summary_fallback(extracted_text)
                return {"summary": english_fallback, "evidentiary_strength": "Medium"}
            data = json.loads(raw_clean)
            summary = (data.get("summary") or "").strip()
            if not summary:
                summary = await self._get_english_summary_fallback(extracted_text)
            strength = (data.get("evidentiaryStrength") or data.get("evidentiary_strength") or "Medium").strip()
            if strength not in ("Low", "Medium", "High"):
                strength = "Medium"
            return {"summary": summary, "evidentiary_strength": strength}
        except json.JSONDecodeError as e:
            logger.warning(
                "Summary JSON parse failed: {}; raw (first 400 chars): {}",
                str(e),
                (raw[:400] if raw else "")
            )
            # Try to salvage partial summary from truncated JSON (e.g. "Unterminated string")
            salvaged = self._salvage_summary_from_truncated_json(raw)
            if salvaged and len(salvaged) >= 25:
                return {"summary": salvaged, "evidentiary_strength": "Medium"}
            english_fallback = await self._get_english_summary_fallback(extracted_text)
            return {"summary": english_fallback, "evidentiary_strength": "Medium"}
        except Exception as e:
            logger.warning("Summary/evidentiary strength generation failed: {}", str(e))
            english_fallback = await self._get_english_summary_fallback(extracted_text)
            return {"summary": english_fallback, "evidentiary_strength": "Medium"}
    
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
            
            # Generate summary and evidentiary strength for the document
            summary_strength = await self._get_summary_and_evidentiary_strength(extracted_text, filename)
            
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
                "stored_in_vector_db": stored_in_vector_db,
                "summary": summary_strength["summary"],
                "evidentiary_strength": summary_strength["evidentiary_strength"],
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
                "readable": False,
                "summary": "",
                "evidentiary_strength": "N/A",
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

