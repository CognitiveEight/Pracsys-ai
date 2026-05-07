"""
Main document processor coordinating file handling, readability scoring,
and vector storage.
"""

import asyncio
import os
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.observability.audit import audit_logger
from app.observability.trace import tracer
from app.processing.document_classifier import classify_and_extract
from app.processing.file_handlers import file_processor
from app.processing.pii_detector import PIIScanResult, scan_text
from app.processing.readability import readability_scorer
from app.utils.exceptions import FileProcessingError
from app.vectorstore.qdrant import vector_store


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
            timeout=300.0, follow_redirects=True  # 5 minutes for large files
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

    def _extractive_summary_fallback(
        self, extracted_text: str, max_chars: int = 280
    ) -> str:
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

    async def _get_english_summary_fallback(self, extracted_text: str) -> str:
        """Get a one-sentence English summary from Gemini when main JSON fails (avoids Hindi/extractive)."""
        if not extracted_text or len(extracted_text.strip()) < 20:
            return "Insufficient extractable content."
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            snippet = (
                (extracted_text.strip()[:800] + "...")
                if len(extracted_text) > 800
                else extracted_text.strip()
            )
            prompt = f"""Summarize the following document excerpt in one short sentence in English only. Output nothing but that single sentence, no quotes, no JSON.

Document excerpt:
{snippet}"""
            out = await gemini_client.generate(prompt, temperature=0.2, max_tokens=12000)
            out = (out or "").strip()
            if out and "Error" not in out[:50] and len(out) >= 15:
                return out
        except Exception as e:
            logger.debug("English summary fallback failed: {}", str(e))
        return self._extractive_summary_fallback(extracted_text)

    async def _classify_document(
        self, redacted_text: str, filename: str
    ) -> Dict[str, Any]:
        """Run the unified classifier on PII-redacted text and fall back to
        an English summary if the classifier returns nothing usable.

        Returns a dict with keys:
            summary, evidentiary_strength, document_type, extracted_fields.
        """
        if not redacted_text or len(redacted_text.strip()) < 30:
            english_fallback = await self._get_english_summary_fallback(redacted_text)
            return {
                "summary": english_fallback,
                "evidentiary_strength": "Low",
                "document_type": "other",
                "extracted_fields": {},
            }

        result = await classify_and_extract(redacted_text, filename)
        if not result.get("summary"):
            result["summary"] = await self._get_english_summary_fallback(
                redacted_text
            )
        return result

    def _infer_format_from_filename(self, filename: str) -> str:
        """Infer a coarse format type used by readability scoring."""
        suffix = Path(filename or "").suffix.lower().lstrip(".")
        if not suffix:
            return "unknown"
        if suffix in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}:
            return "image"
        if suffix in {"pdf"}:
            return "pdf"
        if suffix in {"doc", "docx", "rtf"}:
            return "doc"
        if suffix in {"txt", "md"}:
            return "text"
        return suffix

    @tracer.trace("process_document")
    async def process_document(
        self,
        file_url: str,
        filename: str,
        workspace_id: str,
        user_id: str,
        document_id: str,
        metadata: Dict[str, Any] = None,
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
            workspace_id,
        )

        try:
            # Download file
            file_content, file_size = await self._download_file(file_url, filename)

            # Audio path: transcribe + diarize via Chirp 3, then treat the formatted
            # transcript as the document's extracted text so summary/strength/vector
            # store all keep working unchanged.
            audio_extras: Dict[str, Any] = {}
            if file_processor.is_audio_file(filename):
                audio_result = await self._transcribe_audio_bytes(
                    file_content, filename
                )
                if audio_result.success:
                    extracted_text = audio_result.transcript or audio_result.raw_transcript
                    audio_extras = {
                        "is_audio": True,
                        "transcript": audio_result.transcript,
                        "transcript_status": "ready",
                        "audio_segments": audio_result.segments,
                        "audio_speaker_source": audio_result.speaker_source,
                        "audio_languages": audio_result.detected_languages,
                    }
                else:
                    extracted_text = ""
                    audio_extras = {
                        "is_audio": True,
                        "transcript": "",
                        "transcript_status": "failed",
                        "audio_error": audio_result.error or "transcription_failed",
                    }
            else:
                # Extract text using GCP-powered file processor
                extracted_text = file_processor.process_file_by_extension(
                    file_content, filename
                )

            # Create extraction metadata
            extraction_metadata = {
                "extraction_method": (
                    "audio_transcription"
                    if audio_extras.get("is_audio")
                    else (
                        "gcp_vision"
                        if any(
                            ext in filename.lower()
                            for ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp"]
                        )
                        else "direct"
                    )
                ),
                "has_errors": extracted_text.startswith("[Error")
                or extracted_text.startswith("["),
                "text_length": len(extracted_text),
                # Readability scorer expects these fields (it uses them for extraction_score).
                "character_count": len(extracted_text),
                "word_count": len(extracted_text.split()),
                "format": self._infer_format_from_filename(filename),
            }

            # Calculate readability score (uses raw text length, not redacted)
            readability_result = readability_scorer.calculate_score(
                extracted_text, extraction_metadata, file_size
            )

            # PII redaction: scan the raw extracted text for Indian
            # identifier patterns (Aadhaar, PAN, GSTIN, Udyam, passport,
            # EPIC, DL, bank account, mobile). The redacted copy is what
            # leaves this function — vector DB, Gemini calls, logs, and
            # the public ``summary`` / ``extracted_fields`` payload all
            # use ``redacted_text``. Full values stay only in ``sensitive_fields``
            # which downstream is expected to encrypt at rest.
            pii_scan: PIIScanResult = scan_text(extracted_text)
            redacted_text = pii_scan.redacted_text
            sensitive_fields = pii_scan.sensitive_fields()
            masked_pii_fields = pii_scan.masked_fields()

            # Classify document type, extract structured fields, and produce
            # the legal-analyst summary in a single Gemini call. Runs on the
            # REDACTED text so PII never reaches the LLM. We classify BEFORE
            # vector storage so document_type / extracted_fields can be
            # persisted in chunk metadata — downstream feature agents can
            # then ground their reasoning on a deterministic document
            # inventory rather than re-inferring type from OCR text.
            classification = await self._classify_document(redacted_text, filename)

            # Merge identifier matches from regex extraction into the LLM's
            # extracted_fields output. Regex wins on numeric IDs (deterministic);
            # LLM provides names, dates, addresses, and free-form fields.
            extracted_fields: Dict[str, Any] = dict(
                classification.get("extracted_fields") or {}
            )
            for field_type, masked_values in masked_pii_fields.items():
                # Single-value fields: take first match. List fields kept as-is.
                if field_type in ("aadhaar_number", "pan_number",
                                   "udyam_number", "passport_number",
                                   "voter_id_number", "driving_licence_number"):
                    extracted_fields.setdefault(field_type, masked_values[0])
                else:
                    extracted_fields.setdefault(field_type, masked_values)

            # Store in vector database (user-scoped). Use REDACTED text so
            # raw IDs never land in chunk content. Persist document_type and
            # the masked extracted_fields in chunk metadata so retrievers
            # downstream see the structured signal alongside the OCR content.
            stored_in_vector_db = False
            if redacted_text and readability_result["readable"]:
                try:
                    await self._store_in_vector_db(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        document_id=document_id,
                        text=redacted_text,
                        filename=filename,
                        metadata={
                            **(metadata or {}),
                            **extraction_metadata,
                            "readability": readability_result,
                            "file_size": file_size,
                            "document_type": classification["document_type"],
                            "extracted_fields": extracted_fields,
                        },
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
                "stored_in_vector_db": stored_in_vector_db,
                "summary": classification["summary"],
                "evidentiary_strength": classification["evidentiary_strength"],
                "document_type": classification["document_type"],
                "extracted_fields": extracted_fields,
                "sensitive_fields": sensitive_fields,
            }
            if audio_extras:
                # Replace raw transcript with the redacted copy so cached /
                # re-emitted transcripts do not leak IDs spoken on a call.
                if audio_extras.get("transcript"):
                    audio_extras["transcript"] = redacted_text
                result.update(audio_extras)

            # Log audit event — strip sensitive_fields so raw IDs never reach
            # the audit sink. Audit is allowed to know *that* PII was found
            # (counts per type), not the values.
            audit_safe = {
                k: v for k, v in result.items() if k != "sensitive_fields"
            }
            audit_safe["sensitive_field_counts"] = {
                ftype: len(values) for ftype, values in sensitive_fields.items()
            }
            audit_logger.log_event(
                event_type="document_processed",
                data=audit_safe,
                workspace_id=workspace_id,
                user_id=user_id,
            )

            logger.success(
                "Document processed: {} - Readability: {:.2f}% ({})",
                filename,
                readability_result["total_score"],
                readability_result["rating"],
            )

            return result

        except Exception as e:
            logger.error("Failed to process document {}: {}", filename, str(e))

            # Log error
            await audit_logger.log_error(
                error_type="document_processing",
                error_message=str(e),
                data={"filename": filename, "document_id": document_id},
                workspace_id=workspace_id,
                user_id=user_id,
            )

            failure_result: Dict[str, Any] = {
                "success": False,
                "document_id": document_id,
                "filename": filename,
                "error": str(e),
                "readability_score": 0,
                "readable": False,
                "summary": "",
                "evidentiary_strength": "N/A",
                "document_type": "other",
                "extracted_fields": {},
                "sensitive_fields": {},
            }
            if file_processor.is_audio_file(filename):
                failure_result["is_audio"] = True
                failure_result["transcript"] = ""
                failure_result["transcript_status"] = "failed"
            return failure_result

    async def _transcribe_audio_bytes(self, file_content: bytes, filename: str):
        """Write audio bytes to a temp file and run Chirp 3 transcription.

        Always returns an ``AudioTranscriptionResult`` (never raises) so the
        caller can branch on ``.success`` and emit ``transcript_status:
        "failed"`` for the document_assessments payload.
        """
        from app.processing.audio_transcription import (
            AudioTranscriptionResult,
            transcribe_audio,
        )

        suffix = Path(filename).suffix.lower() or ".audio"
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="audio_", suffix=suffix, delete=False
            ) as fh:
                fh.write(file_content)
                tmp_path = fh.name
            logger.info("Audio transcription starting: {}", filename)
            result = await transcribe_audio(tmp_path)
            if result.success:
                logger.success(
                    "Audio transcription succeeded: {} (source={}, segs={})",
                    filename,
                    result.speaker_source,
                    len(result.segments),
                )
            else:
                logger.warning(
                    "Audio transcription failed: {} ({})",
                    filename,
                    result.error,
                )
            return result
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Audio transcription crashed for {}: {}", filename, exc)
            return AudioTranscriptionResult(
                success=False, error=f"transcription_crashed: {exc}"
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @tracer.trace("process_batch")
    async def process_batch(
        self, files: List[Dict[str, Any]], workspace_id: str, user_id: str
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
            workspace_id,
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
                    "path": file_info.get("path"),
                },
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
            "results": results,
        }

        logger.success(
            "Batch processing complete: {}/{} successful, avg score: {:.2f}%",
            success_count,
            len(files),
            avg_score,
        )

        return batch_result

    async def _download_file(self, url: str, filename: str) -> tuple[bytes, int]:
        """
        Download file from URL. Validates URL, retries on transient failures,
        and raises with a clear error message.

        Returns:
            Tuple of (content, size)
        """
        if not self.http_client:
            await self.initialize()

        if not url or not isinstance(url, str):
            msg = "Download URL is missing or invalid"
            logger.error("{} (filename: {})", msg, filename)
            raise FileProcessingError(msg, details={"filename": filename})

        clean_url = (url or "").strip().rstrip('"')
        if not clean_url.startswith(("http://", "https://")):
            msg = (
                f"Invalid download URL scheme (must be http/https): {clean_url[:80]}..."
            )
            logger.error("{} (filename: {})", msg, filename)
            raise FileProcessingError(
                msg, details={"filename": filename, "url": clean_url[:200]}
            )

        max_attempts = (
            5  # Extra attempts for transient DNS/network failures (e.g. Errno -3)
        )
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "Downloading file from: {} (attempt {}/{})",
                    clean_url[:100],
                    attempt,
                    max_attempts,
                )
                response = await self.http_client.get(clean_url)
                response.raise_for_status()
                content = response.content
                size = len(content)

                if size > self.max_file_size:
                    raise FileProcessingError(
                        f"File too large: {size / 1024 / 1024:.2f}MB (max: {self.max_file_size / 1024 / 1024}MB)",
                        details={"filename": filename, "size": size},
                    )

                logger.debug(
                    "Downloaded file: {} ({:.2f}MB)", filename, size / 1024 / 1024
                )
                return content, size

            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                reason = (e.response.text or "")[:200]
                err_msg = f"HTTP {status} ({e.response.reason_phrase or 'Error'})"
                if reason:
                    err_msg += f" — {reason}"
                logger.error("HTTP error downloading {}: {}", filename, err_msg)
                # Retry on server errors (5xx) and rate limit (429)
                if attempt < max_attempts and (status >= 500 or status == 429):
                    await asyncio.sleep(2 * attempt)
                    continue
                raise FileProcessingError(
                    f"Failed to download file: {err_msg}",
                    details={
                        "url": clean_url[:200],
                        "filename": filename,
                        "status_code": status,
                    },
                )
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                OSError,
                socket.gaierror,
            ) as e:
                last_error = e
                err_msg = str(e) or type(e).__name__
                is_dns_temp = (
                    "name resolution" in err_msg
                    or "Errno -3" in err_msg
                    or "Temporary failure" in err_msg
                    or isinstance(e, socket.gaierror)
                )
                # For transient DNS/network issues during intermediate attempts, log as WARNING not ERROR
                if is_dns_temp and attempt < max_attempts:
                    logger.warning(
                        "DNS/network temporary failure downloading {} (attempt {}/{}): {}",
                        filename,
                        attempt,
                        max_attempts,
                        err_msg,
                    )
                else:
                    logger.error(
                        "Connection/timeout error downloading {}: {}", filename, err_msg
                    )
                if is_dns_temp and attempt < max_attempts:
                    # Longer backoff for DNS/connection errors (3, 6, 9, 12 sec)
                    delay = 3 * attempt
                    await asyncio.sleep(delay)
                    continue
                if attempt < max_attempts:
                    # Non-DNS connection issue, still retry with shorter backoff
                    delay = 2 * attempt
                    await asyncio.sleep(delay)
                    continue
                # Out of retries – raise a clear error
                raise FileProcessingError(
                    f"Failed to download file due to network/DNS error: {err_msg}",
                    details={"url": clean_url[:200], "filename": filename},
                )
            except httpx.HTTPError as e:
                last_error = e
                err_msg = str(e) or type(e).__name__
                logger.error("HTTP error downloading {}: {}", filename, err_msg)
                raise FileProcessingError(
                    f"Failed to download file: {err_msg}",
                    details={"url": clean_url[:200], "filename": filename},
                )
            except Exception as e:
                last_error = e
                err_msg = str(e) or type(e).__name__
                is_dns_temp = (
                    "name resolution" in err_msg
                    or "Errno -3" in err_msg
                    or "Temporary failure" in err_msg
                    or isinstance(e, socket.gaierror)
                )
                if is_dns_temp and attempt < max_attempts:
                    logger.warning(
                        "DNS/network temporary failure (generic) downloading {} (attempt {}/{}): {}",
                        filename,
                        attempt,
                        max_attempts,
                        err_msg,
                    )
                else:
                    logger.error("Error downloading {}: {}", filename, err_msg)
                if attempt < max_attempts:
                    delay = 3 * attempt if is_dns_temp else 2 * attempt
                    await asyncio.sleep(delay)
                    continue
                raise FileProcessingError(
                    f"Failed to download file: {err_msg}",
                    details={"url": clean_url[:200], "filename": filename},
                )

        msg = str(last_error) if last_error else "Download failed after retries"
        raise FileProcessingError(
            f"Failed to download file: {msg}",
            details={"filename": filename, "url": clean_url[:200]},
        )

    async def download_and_extract_text(
        self, url: str, filename: str, max_text_chars: int = 50000
    ) -> str:
        """
        Download a file from URL and extract text only (no vector store, no scoring).
        Used for chat attachments so the user can ask questions about the uploaded document.
        Returns PII-redacted extracted text, truncated to max_text_chars; empty
        string on failure. Redaction matches the workspace evidence path so
        chat context never carries raw Aadhaar/PAN/etc. into the LLM prompt.
        """
        try:
            file_content, _ = await self._download_file(url, filename)
            text = file_processor.process_file_by_extension(file_content, filename)
            if not text or not isinstance(text, str):
                return ""
            text = scan_text(text.strip()).redacted_text
            if len(text) > max_text_chars:
                text = (
                    text[:max_text_chars]
                    + "\n\n[Content truncated for context length.]"
                )
            return text
        except Exception as e:
            logger.warning(
                "Chat attachment download/extract failed ({}): {}", filename, e
            )
            return ""

    async def _store_in_vector_db(
        self,
        workspace_id: str,
        user_id: str,
        document_id: str,
        text: str,
        filename: str,
        metadata: Dict[str, Any],
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
            chunk = " ".join(words[i : i + chunk_size])
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
                    "is_chunk": len(chunks) > 1,
                },
            )

        logger.success(
            "Stored {} chunks for document {} (user: {})",
            len(chunks),
            filename,
            user_id,
        )


# Global document processor instance
document_processor = DocumentProcessor()
