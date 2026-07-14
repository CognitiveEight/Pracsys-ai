"""Connection lifecycle, reconnect re-emit, workspace_created/sync.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import json
import re
import time
import uuid
from datetime import date as date_type
from datetime import datetime
from html import unescape
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus

import httpx
from loguru import logger

from app.agents.role_models import RoleEnum
from app.core.config import settings
from app.langgraph.integration import workspace_role_integration
from app.observability.audit import audit_logger
from app.processing.audio_transcription import (
    _has_devanagari as _audio_has_devanagari,
    transliterate_formatted_transcript_to_hinglish,
)
from app.processing.document_processor import document_processor
from app.search.vertex_ai_search import search_indiankanoon as vertex_search_indiankanoon
from app.search.case_law_finder import (
    fetch_judgment_text,
    find_case_law_sources,
    scrape_indiankanoon,
)
from app.prompts.applicable_acts import (
    TARGETED_INDIAN_ACTS,
    format_targeted_acts_for_prompt,
)
from app.prompts.legal_intelligence_system import (
    ANNEXURE_UPLOAD_GATE_MESSAGE,
    APP_BRAND_EXCLUSION_DIRECTIVE,
    ENGLISH_ONLY_OUTPUT_DIRECTIVE,
    NAME_CAPITALISATION_DIRECTIVE,
    get_legal_intelligence_system_instruction,
    get_structured_document_feature_system_instruction,
)
from app.langgraph.chat_nodes import (
    _last_ai_message_from_history,
    is_user_affirmation_to_continue,
    merge_intelligence_chat_history,
)
from app.langgraph.list_of_dates_agent import (
    list_of_dates_temporal_agent,
    snap_hallucinated_year,
)
from app.socket.schemas import (
    AIResponseMetadata,
    AIResponsePayload,
    AnalysisDocxGenerateRequestPayload,
    ApplicableLawRelevanceRequestPayload,
    TemplateSyncPayload,
    TemplateSyncResponsePayload,
    UserMessageRequestPayload,
    WorkspaceCreatedPayload,
    WorkspaceSyncPayload,
)
from app.utils.exceptions import SocketEventError
from app.workspace.custom_instructions import (build_custom_instruction_suffix,
                                               hydrate_custom_instructions_from_cache,
                                               set_custom_instructions)
from app.workspace.manager import workspace_manager
from app.workspace.persistence import (CacheKinds, PersistentWorkspaceDict,
                                       hydrate_workspace_state, persist_state,
                                       restore_state)
from agent import process_analysis_docx_request

from app.socket.event_utils import (
    EventHandler,
    _STRATEGY_ROLE_LABELS_RE,
    OBJECTIVE_UPDATE_OFFER_QUESTION,
    CHAT_SMALL_WORKSPACE_DOC_THRESHOLD,
    CHAT_SEARCH_CHUNK_LIMIT,
    CHAT_MAX_DOCS_INCLUDED,
    CHAT_MAX_CHARS_PER_DOC,
    CHAT_MAX_TOTAL_CHARS,
    CHAT_MIN_SCORE_THRESHOLD,
    _ANNEXURE_REF_RE,
    _ROMAN_VALUES,
    SPEAKER_LABEL_GUARDRAIL,
    _ENUM_PREFIX_RE,
    _UI_TRUNCATION_RE,
    _APP_BRAND_RE,
    _STRENGTH_POINT_MIN_LEN,
    _ANNEXURE_SINGLE_RE,
    _ANNEXURE_PLURAL_RE,
    _OVERVIEW_CONTACTED_DATE_RE,
    _OVERVIEW_TO_PHONE_RE,
    _roman_or_int,
    _derive_report_case_summary,
    _coerce_analysis_text_item,
    _strip_enum_prefix,
    _strip_ui_truncated_tokens,
    scrub_app_brand,
    _coerce_analysis_text_list,
    _strip_strength_point_statute_boilerplate,
    _annexure_wording_for_next_steps,
    _clamp_annexure_references,
    _format_document_for_prompt,
    _normalize_report_mobile_from_overview,
    _overview_conversation_date_to_iso,
    extract_overview_conversation_report_contact_meta,
    drafting_payload_to_wire,
)


class WorkspaceEventsMixin:
    """Connection lifecycle, reconnect re-emit, workspace_created/sync."""

    async def _do_reemit_after_reconnect(self) -> None:
        """Re-emit cached document_feature_response payloads for the pending workspace after reconnect.
        This covers: document_assessments, workspace_metadata, strategy_objective, analysis_report, list_of_dates.
        Clears pending state so it only runs once per disconnect/reconnect cycle."""
        if (
            not self._had_disconnect
            or not self._pending_reemit_workspace_id
            or not self._socket_client
        ):
            return
        workspace_id = self._pending_reemit_workspace_id
        user_id = self._pending_reemit_user_id or ""
        self._had_disconnect = False
        self._pending_reemit_workspace_id = None
        self._pending_reemit_user_id = None
        try:
            # 1) Document assessments (if available). Heal any cached
            # Devanagari transcripts to Hinglish first so workspaces processed
            # before the Hinglish step was added auto-upgrade on the next
            # re-emit. Idempotent — once healed, subsequent calls are no-ops.
            try:
                await self._heal_devanagari_in_document_assessments(workspace_id)
            except Exception as heal_err:
                logger.warning(
                    "Devanagari heal failed for workspace {} (non-fatal): {}",
                    workspace_id[:12] if workspace_id else "",
                    heal_err,
                )
            doc_assess = self._workspace_document_assessments_payload.get(workspace_id)
            if doc_assess:
                await self._socket_client.emit(
                    "document_feature_response",
                    doc_assess,
                )
                logger.success(
                    "✅ Document assessments re-emitted to backend after reconnect"
                )
                await asyncio.sleep(0.1)
            # 2) Workspace metadata (if available)
            metadata = self._workspace_metadata_payload.get(workspace_id)
            if metadata:
                await self._socket_client.emit(
                    "document_feature_response",
                    metadata,
                )
                logger.success(
                    "✅ Workspace metadata re-emitted to backend after reconnect"
                )
                await asyncio.sleep(0.1)
            # 3) Strategy & objectives
            strategies_result = self._workspace_strategy_payload.get(workspace_id)
            if strategies_result:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": strategies_result,
                    },
                )
                logger.success("✅ Strategy re-emitted to backend after reconnect")
                await asyncio.sleep(0.2)
            # 4) Analysis report
            analysis_report = self._workspace_analysis_payload.get(workspace_id)
            if analysis_report:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": analysis_report,
                    },
                )
                logger.success(
                    "✅ Analysis report re-emitted to backend after reconnect"
                )
            # 5) List of dates timeline
            list_of_dates = self._workspace_list_of_dates_payload.get(workspace_id)
            if list_of_dates is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": list_of_dates,
                        },
                    },
                )
                logger.success("✅ List of dates re-emitted to backend after reconnect")
                self._log_list_of_dates_outbound(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    list_of_dates=list_of_dates,
                    context="reconnect-reemit",
                )
            # 6) Applicable laws
            applicable_laws = self._workspace_applicable_laws_payload.get(workspace_id)
            if applicable_laws is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "applicable_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": applicable_laws,
                    },
                )
                logger.success(
                    "✅ Applicable laws re-emitted to backend after reconnect"
                )
            # 6b) Case laws
            case_laws = self._workspace_case_laws_payload.get(workspace_id)
            if case_laws is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "case_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": case_laws,
                    },
                )
                logger.success(
                    "✅ Case laws re-emitted to backend after reconnect"
                )
            # 6c) Relevant parties
            relevant_parties = self._workspace_relevant_parties_payload.get(workspace_id)
            if relevant_parties is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "relevant_parties",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": relevant_parties,
                    },
                )
                logger.success(
                    "✅ Relevant parties re-emitted to backend after reconnect"
                )
            # 7) Procedural timeline
            procedural_timeline = self._workspace_procedural_timeline_payload.get(workspace_id)
            if procedural_timeline is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "procedural_timeline",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {"proceduralTimeline": procedural_timeline},
                    },
                )
                logger.success(
                    "✅ Procedural timeline re-emitted to backend after reconnect"
                )
            # 8) Drafting Assistant — proactive suggestions panel
            drafting_payload = self._workspace_drafting_payload.get(workspace_id)
            if drafting_payload is not None:
                # Cache holds the internal card shape; serialize to the
                # matches/matchPercent wire contract on re-emit.
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "drafting_assistant",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": drafting_payload_to_wire(drafting_payload),
                    },
                )
                logger.success(
                    "✅ Drafting suggestions re-emitted to backend after reconnect"
                )
        except Exception as e:
            logger.warning("Failed to re-emit after reconnect: {}", e)

    async def on_connect(self, data: Any) -> None:
        """Handle connection event. Re-emit pending document_feature_response if we had disconnected during workspace init."""
        logger.info("Socket connected")
        audit_logger.log_event("socket_connect", {"status": "connected"})
        await self._do_reemit_after_reconnect()

    async def on_connected(self, data: Any) -> None:
        """
        Handle 'connected' event from server.
        Confirms successful connection with userId and clientType.
        If we had a disconnect right after emitting analysis report, re-emit from cache so backend gets the update.
        """
        logger.success("✅ Server confirmed connection as AI_Maverick")
        logger.info(f"Connection details: {data}")
        audit_logger.log_event(
            "socket_connected_confirmed", {"status": "confirmed", "data": data}
        )
        await self._do_reemit_after_reconnect()
        # Re-emit last ai_response if we disconnected right after sending (so backend receives the chat response)
        if self._had_disconnect and self._pending_ai_response and self._socket_client:
            try:
                await self._socket_client.emit_ai_response(self._pending_ai_response)
                logger.success(
                    "✅ AI chat response re-emitted to backend after reconnect (chat: {})",
                    self._pending_ai_response.get("chatId"),
                )
            except Exception as e:
                logger.warning("Failed to re-emit ai_response after reconnect: {}", e)
            finally:
                self._pending_ai_response = None
        if self._had_disconnect:
            self._had_disconnect = False

    async def on_disconnect(self, data: Any) -> None:
        """Handle disconnection event. Mark that we disconnected (so on_connected can re-emit if needed)."""
        self._had_disconnect = True
        logger.info("Socket disconnected")
        audit_logger.log_event("socket_disconnect", {"status": "disconnected"})

    async def on_error(self, data: Any) -> None:
        """Handle error event."""
        logger.error("Socket error received: {}", data)
        audit_logger.log_event("socket_error", {"error": str(data)})

    async def on_workspace_created(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_created event (Server → AI_Maverick).

        Payload:
        {
            workspaceId: str,
            userId: str,
            title: str,
            workspaceRole: str,
            evidenceFiles: [...],
            createdAt: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Initialize role-based agent with workspaceRole
        3. Process evidence files
        4. Store in vector database
        5. Log audit event

        Args:
            data: Workspace creation payload
        """
        try:
            # Validate and parse payload
            payload = WorkspaceCreatedPayload(**data)

            # Store custom instructions if provided (structure, tone, description)
            custom = data.get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get(
                "description"
            )
            if structure or tone or description:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                logger.info(
                    "Custom instructions set: structure={}, tone={}, description={}",
                    structure,
                    tone,
                    (
                        description[:50] + "..."
                        if description and len(description) > 50
                        else description
                    ),
                )

            logger.info(
                "📦 Workspace created: {} - {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.title,
                payload.userId,
                payload.workspaceRole,
            )
            # Set early so re-emit on reconnect works even if socket drops during document_assessments/metadata/strategy/analysis/list_of_dates
            self._pending_reemit_workspace_id = str(payload.workspaceId)
            self._pending_reemit_user_id = str(payload.userId)

            # STEP 1: Validate role (convert to uppercase for validation)
            workspace_role = (
                payload.workspaceRole.upper()
                if payload.workspaceRole
                else "ADJUDICATOR"
            )
            if not RoleEnum.validate(workspace_role):
                logger.warning(
                    "Invalid workspace role '{}', defaulting to ADJUDICATOR",
                    payload.workspaceRole,
                )
                workspace_role = "ADJUDICATOR"
            await self._remember_workspace_role(
                str(payload.workspaceId), str(payload.userId), workspace_role
            )

            logger.info("Initializing workspace with role: {}", workspace_role)

            # Ensure workspace context exists so chat and strategy can resolve role (get_workspace)
            from datetime import datetime as dt

            from app.workspace.context import context_manager as ctx_mgr

            try:
                created_at = (
                    dt.fromisoformat(payload.createdAt.replace("Z", "+00:00"))
                    if getattr(payload, "createdAt", None)
                    else dt.utcnow()
                )
            except Exception:
                created_at = dt.utcnow()
            ctx_mgr.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status="active",
                workspace_role=workspace_role,
                evidence_files=(
                    list(payload.evidenceFiles) if payload.evidenceFiles else []
                ),
                updated_at=created_at,
            )

            # STEP 2: Initialize role-based agent
            try:
                state = await workspace_role_integration.initialize_workspace_with_role(
                    workspace_id=payload.workspaceId,
                    user_id=payload.userId,
                    initial_role=workspace_role,
                )

                logger.success(
                    "✅ Role-based agent initialized for workspace {} with role {}",
                    payload.workspaceId,
                    workspace_role,
                )
            except Exception as role_error:
                logger.error("Failed to initialize role agent: {}", str(role_error))
                # Continue with workspace creation even if role init fails

            # STEP 3: Process evidence files
            if payload.evidenceFiles:
                logger.info(
                    "Processing {} evidence files for workspace {}",
                    len(payload.evidenceFiles),
                    payload.workspaceId,
                )

                # Prepare files for batch processing
                files_to_process = []
                for file in payload.evidenceFiles:
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt,
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file.id:
                        file_dict["_id"] = file.id
                    files_to_process.append(file_dict)

                try:
                    # Process files in batch
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId,
                    )

                    logger.success(
                        "✅ Document processing complete: {}/{} successful, avg readability: {:.2f}%",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0),
                        batch_result.get("average_readability_score", 0),
                    )

                    # Send processing results back to backend (each section isolated so one failure doesn't block the rest — same idea as ex_event)
                    if self._socket_client:
                        # 1) Document assessments — own try/except
                        try:
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get(
                                        "readability_score", 0
                                    ),
                                    "readabilityRating": result.get(
                                        "readability_rating", "Unknown"
                                    ),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get(
                                        "extracted_text_length", 0
                                    ),
                                    "storedInVectorDb": result.get(
                                        "stored_in_vector_db", False
                                    ),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get(
                                        "evidentiary_strength", "N/A"
                                    ),
                                    # Document classification + structured fields.
                                    # documentType: snake_case enum (e.g. aadhaar_card,
                                    # bank_statement, fir, court_order, other).
                                    # extractedFields: masked / safe values for display.
                                    # sensitiveFields: full raw IDs for sealed encrypted
                                    # storage on the backend — must NOT be displayed.
                                    "documentType": result.get(
                                        "document_type", "other"
                                    ),
                                    "extractedFields": result.get(
                                        "extracted_fields", {}
                                    ),
                                    "sensitiveFields": result.get(
                                        "sensitive_fields", {}
                                    ),
                                }
                                # Audio: attach transcript + transcriptStatus.
                                # When transcription has not produced text yet ("pending")
                                # or has errored ("failed"), emit a minimal entry to match
                                # the backend contract (no quality / summary fields).
                                if result.get("is_audio"):
                                    status = result.get("transcript_status", "failed")
                                    transcript_text = result.get("transcript", "") or ""
                                    if status == "ready" and transcript_text:
                                        doc_result["transcript"] = transcript_text
                                        doc_result["transcriptStatus"] = "ready"
                                    else:
                                        # Drop quality/summary fields for not-ready audio
                                        doc_result = {
                                            "documentId": result.get("document_id"),
                                            "fileName": result.get("filename"),
                                            "transcriptStatus": status if status in ("pending", "failed") else "failed",
                                        }
                                if not result.get("success") and "transcriptStatus" not in doc_result:
                                    doc_result["error"] = result.get(
                                        "error", "Unknown error"
                                    )
                                document_results.append(doc_result)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get(
                                            "total_files", 0
                                        ),
                                        "successfulFiles": batch_result.get(
                                            "successful", 0
                                        ),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get(
                                            "average_readability_score", 0
                                        ),
                                    },
                                },
                            }
                            logger.info(
                                f"📤 Emitting document_feature_response (document_assessments) with {len(document_results)} documents"
                            )
                            await self._socket_client.emit(
                                "document_feature_response", document_assessment_payload
                            )
                            # Cache for reliable re-emit after reconnect when socket drops mid-workspace_created
                            self._workspace_document_assessments_payload[
                                payload.workspaceId
                            ] = document_assessment_payload
                            logger.success(
                                "✅ Document assessments sent (or cached for re-emit) to backend"
                            )
                        except Exception as e:
                            logger.error("Failed to emit document_assessments: {}", e)

                        # 2) Workspace metadata — own try/except (same as ex_event)
                        if batch_result.get("successful", 0) > 0:
                            try:
                                meta = (
                                    await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                )
                                workspace_metadata_payload = {
                                    "featureType": "workspace_metadata",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": {
                                        "workspaceTitle": meta.get("caseTitle") or "",
                                        "updateTitle": True,
                                        "caseCategory": meta.get(
                                            "caseCategory", "General"
                                        ),
                                        "subcategories": meta.get("subcategories", []),
                                        "description": meta.get("description", ""),
                                    },
                                }
                                await self._socket_client.emit(
                                    "document_feature_response",
                                    workspace_metadata_payload,
                                )
                                # Cache for reliable re-emit alongside other workspace payloads
                                self._workspace_metadata_payload[
                                    payload.workspaceId
                                ] = workspace_metadata_payload
                                logger.success(
                                    "✅ Workspace metadata sent (or cached for re-emit)"
                                )
                            except Exception as meta_err:
                                logger.warning(
                                    "Failed to emit workspace_metadata: {}",
                                    str(meta_err),
                                )

                        # 3–4) Strategy, analysis, list_of_dates, applicable_laws: background so chat events are not blocked
                        if batch_result.get("successful", 0) > 0:
                            logger.info(
                                "🎯 Scheduling strategy + analysis + list_of_dates + "
                                "applicable_laws + procedural_timeline (background)…"
                            )
                            self._schedule_strategy_analysis_pipeline(
                                workspace_id=str(payload.workspaceId),
                                user_id=str(payload.userId),
                                workspace_role=workspace_role,
                                batch_result=batch_result,
                                is_update=False,
                                analysis_log_context="",
                            )
                        else:
                            # When no files succeeded, still emit analysis report (fallback) so backend always receives it
                            if self._socket_client and batch_result is not None:
                                try:
                                    fallback = self._analysis_report_fallback(
                                        (workspace_role or "ADJUDICATOR")
                                        .strip()
                                        .upper()
                                    )
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        {
                                            "featureType": "analysis_report",
                                            "workspaceId": str(payload.workspaceId),
                                            "userId": str(payload.userId),
                                            "payload": fallback,
                                        },
                                    )
                                    self._workspace_analysis_payload[
                                        payload.workspaceId
                                    ] = fallback
                                    logger.info(
                                        "✅ Analysis report (fallback, 0 successful docs) sent to backend"
                                    )
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        {
                                            "featureType": "list_of_dates",
                                            "workspaceId": str(payload.workspaceId),
                                            "userId": str(payload.userId),
                                            "payload": {
                                                "workspaceId": str(payload.workspaceId),
                                                "userId": str(payload.userId),
                                                "listOfDates": [],
                                            },
                                        },
                                    )
                                    self._workspace_list_of_dates_payload[
                                        payload.workspaceId
                                    ] = []
                                    self._schedule_list_of_dates_and_applicable_laws(
                                        workspace_id=str(payload.workspaceId),
                                        user_id=str(payload.userId),
                                        workspace_role=workspace_role,
                                        batch_result=None,
                                        log_context=" (created, 0 docs success)",
                                    )
                                except Exception as e:
                                    logger.error(
                                        "Failed to emit analysis report fallback (0 success): {}",
                                        e,
                                    )

                except Exception as doc_error:
                    logger.error("Error processing documents: {}", str(doc_error))

                    # Send error notification to backend (per backend spec)
                    if self._socket_client:
                        try:
                            error_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "error": str(doc_error),
                            }
                            await self._socket_client.emit(
                                "document_feature_response", error_payload
                            )
                            logger.error("❌ Document processing error sent to backend")
                        except Exception as emit_error:
                            logger.error(
                                f"Failed to emit error notification: {emit_error}"
                            )

            # When workspace has no evidence files, still emit analysis report (fallback) and empty list of dates
            if not payload.evidenceFiles and self._socket_client:
                try:
                    workspace_role = (
                        (payload.workspaceRole or "ADJUDICATOR").strip().upper()
                    )
                    fallback = self._analysis_report_fallback(workspace_role)
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": payload.workspaceId,
                            "userId": payload.userId,
                            "payload": fallback,
                        },
                    )
                    self._workspace_analysis_payload[payload.workspaceId] = fallback
                    logger.info(
                        "✅ Analysis report (fallback, no docs) sent to backend"
                    )
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": payload.workspaceId,
                            "userId": payload.userId,
                            "payload": {
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "listOfDates": [],
                            },
                        },
                    )
                    self._workspace_list_of_dates_payload[payload.workspaceId] = []
                    logger.info("✅ List of dates (empty, no docs) sent to backend")
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (created, no docs)",
                    )
                except Exception as e:
                    logger.error("Failed to emit analysis report (no docs): {}", e)

            # STEP 4: Log audit event
            audit_logger.log_event(
                "workspace_created_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "title": payload.title,
                    "role": workspace_role,
                    "evidence_files_count": len(payload.evidenceFiles),
                },
            )

            logger.success(
                "🎉 Workspace fully initialized: {} with role {}",
                payload.workspaceId,
                workspace_role,
            )

        except Exception as e:
            logger.error("Error processing workspace_created event: {}", str(e))
            logger.exception(e)
            raise

    async def on_workspace_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_sync event (Server → AI_Maverick).

        Payload:
        {
            workspaceId: str,
            userId: str,
            title?: str,
            status?: str,
            evidenceFiles?: [...],
            updatedAt: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Update workspace metadata
        3. If deletedDocumentIds: remove from vector store, regenerate strategy from remaining docs
        4. Process new evidence files if any (strategy then based on all docs in workspace)
        5. Log audit event

        Args:
            data: Workspace sync payload
        """
        try:
            # Log raw websocket input for debugging (delete flags, evidenceFiles, etc.)
            try:
                import json

                raw_preview = json.dumps(data, default=str) if data else "null"
                if len(raw_preview) > 1500:
                    raw_preview = raw_preview[:1500] + "..."
                logger.info("📥 workspace_sync RAW input: {}", raw_preview)
            except Exception:
                logger.info("📥 workspace_sync RAW input (repr): {}", repr(data))

            # Validate and parse payload
            payload = WorkspaceSyncPayload(**data)

            # -----------------------------------------------------------------
            # Idempotency guard: skip duplicate/replayed workspace_sync payloads.
            # This prevents duplicate strategy/analysis/list generation when
            # the same sync event is replayed around reconnect.
            # -----------------------------------------------------------------
            try:
                import json

                workspace_id_for_fp = str(payload.workspaceId)
                file_facts = []
                for f in payload.evidenceFiles or []:
                    fid = str(getattr(f, "id", "") or "")
                    file_facts.append(
                        {
                            "id": fid,
                            "isDeleted": bool(
                                getattr(f, "isDeleted", False)
                                or getattr(f, "is_deleted", False)
                            ),
                            "uploadedAt": str(getattr(f, "uploadedAt", "") or ""),
                        }
                    )
                file_facts.sort(
                    key=lambda x: (x.get("id") or "", x.get("uploadedAt") or "")
                )
                fp_obj = {
                    "workspaceId": workspace_id_for_fp,
                    "updatedAt": str(payload.updatedAt or ""),
                    "deletedDocumentIds": sorted(
                        [str(x) for x in (payload.deletedDocumentIds or [])]
                    ),
                    "files": file_facts,
                    "workspaceRole": str(payload.workspaceRole or ""),
                    "title": str(payload.title or ""),
                    "status": str(payload.status or ""),
                }
                fingerprint = json.dumps(fp_obj, sort_keys=True, default=str)
                now_ts = time.time()
                last = self._last_workspace_sync_fingerprint.get(workspace_id_for_fp)
                # 180s window to avoid replay duplicates while still allowing real updates.
                if (
                    last
                    and last.get("fingerprint") == fingerprint
                    and (now_ts - float(last.get("ts") or 0.0)) < 180.0
                ):
                    logger.info(
                        "Skipping duplicate/replayed workspace_sync for workspace {} (fingerprint matched within 180s)",
                        workspace_id_for_fp,
                    )
                    return
                self._last_workspace_sync_fingerprint[workspace_id_for_fp] = {
                    "fingerprint": fingerprint,
                    "ts": now_ts,
                }
            except Exception as fp_err:
                logger.debug(
                    "workspace_sync idempotency guard skipped due to fingerprint error: {}",
                    fp_err,
                )

            # Update custom instructions if provided (structure, tone, description)
            custom = (data or {}).get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get(
                "description"
            )
            if structure is not None or tone is not None or description is not None:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                if structure or tone or description:
                    logger.info(
                        "Custom instructions updated: structure={}, tone={}",
                        structure,
                        tone,
                    )

            # Normalize deleted IDs: from deletedDocumentIds and from evidenceFiles with isDeleted: true
            deleted_ids = list(payload.deletedDocumentIds or [])
            if not deleted_ids and isinstance(data, dict):
                deleted_ids = list(
                    data.get("deleted_document_ids")
                    or data.get("deletedDocumentIds")
                    or []
                )
            # Backend may send isDeleted: true on each evidence file instead of/in addition to deletedDocumentIds
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", None) or (
                        getattr(file, "is_deleted", None)
                        if hasattr(file, "is_deleted")
                        else None
                    )
                    if is_deleted:
                        file_id = getattr(file, "id", None) or (
                            getattr(file, "_id", None) if hasattr(file, "_id") else None
                        )
                        if file_id and str(file_id) not in deleted_ids:
                            deleted_ids.append(str(file_id))

            # Backend often sends only the remaining evidenceFiles after a delete (the removed file is omitted)
            # without deletedDocumentIds or isDeleted. Those IDs would stay in the vector store and skip logic below
            # would leave strategy/analysis/list-of-dates/applicable-laws stale (files_to_process becomes empty).
            if payload.evidenceFiles is not None:
                try:
                    non_deleted_evidence = [
                        f
                        for f in payload.evidenceFiles
                        if not (
                            getattr(f, "isDeleted", False)
                            or getattr(f, "is_deleted", False)
                        )
                    ]
                    id_from_file = []
                    for f in non_deleted_evidence:
                        fid = getattr(f, "id", None) or getattr(f, "_id", None)
                        if fid:
                            id_from_file.append(str(fid).strip())
                    # Only infer when every remaining file has an id (avoid false orphan deletion).
                    if non_deleted_evidence and len(id_from_file) == len(
                        non_deleted_evidence
                    ):
                        from app.vectorstore.qdrant import vector_store

                        current_id_set = set(id_from_file)
                        existing_docs = await vector_store.get_all_documents(
                            workspace_id=payload.workspaceId
                        )
                        existing_id_set = {
                            str(d.get("document_id"))
                            for d in (existing_docs or [])
                            if d.get("document_id")
                        }
                        orphan_ids = [
                            i for i in existing_id_set if i not in current_id_set
                        ]
                        for oid in orphan_ids:
                            if oid not in deleted_ids:
                                deleted_ids.append(oid)
                        if orphan_ids:
                            logger.info(
                                "🗑️ Inferred {} removed document(s) (in vector store but not in evidenceFiles): {}",
                                len(orphan_ids),
                                orphan_ids,
                            )
                except Exception as orphan_err:
                    logger.warning(
                        "Vector vs evidenceFiles orphan detection failed: {}",
                        orphan_err,
                    )

            if deleted_ids:
                logger.info(
                    "🗑️ Document IDs to remove from vector store (explicit + inferred): {}",
                    deleted_ids,
                )

            logger.info(
                "🔄 Workspace sync: {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.userId,
                payload.workspaceRole or "unchanged",
            )

            # Create or update workspace context so chat and strategy use correct role (e.g. Mediator from Edit Workspace)
            from datetime import datetime as dt

            from app.workspace.context import context_manager

            updated_at = None
            try:
                if payload.updatedAt:
                    updated_at = dt.fromisoformat(
                        payload.updatedAt.replace("Z", "+00:00")
                    )
            except Exception:
                updated_at = dt.utcnow()
            evidence_for_context = []
            if payload.evidenceFiles:
                for f in payload.evidenceFiles:
                    if getattr(f, "isDeleted", False) or getattr(
                        f, "is_deleted", False
                    ):
                        continue
                    evidence_for_context.append(f)
            # Capture the previously-stored role BEFORE _remember_workspace_role
            # overwrites the cache, so we can detect a role-only change below.
            previous_role: Optional[str] = None
            if payload.workspaceRole:
                try:
                    previous_role = await self._restore_workspace_role(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                    )
                except Exception as prev_err:
                    logger.debug(
                        "Could not load previous workspace role: {}", prev_err
                    )
            if payload.workspaceRole:
                await self._remember_workspace_role(
                    str(payload.workspaceId),
                    str(payload.userId),
                    str(payload.workspaceRole),
                )
            # Resolve the role BEFORE upserting the context: a doc-upload sync
            # carries no workspaceRole, and upserting first on cold memory
            # would fabricate an ADJUDICATOR context that shadows the
            # Qdrant-persisted role during resolution.
            resolved_workspace_role = await self._resolve_workspace_role(
                workspace_id=str(payload.workspaceId),
                user_id=str(payload.userId),
                preferred_role=payload.workspaceRole,
            )
            logger.info(
                "Resolved workspace role for sync generation: {}",
                resolved_workspace_role,
            )
            context_manager.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status=payload.status,
                workspace_role=payload.workspaceRole or resolved_workspace_role,
                evidence_files=evidence_for_context,
                updated_at=updated_at,
            )
            _valid_roles_for_change = {"ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"}
            _prev_upper = (previous_role or "").strip().upper()
            _new_upper = (resolved_workspace_role or "").strip().upper()
            role_changed = (
                _prev_upper in _valid_roles_for_change
                and _new_upper in _valid_roles_for_change
                and _prev_upper != _new_upper
            )
            if role_changed:
                logger.info(
                    "🔁 Workspace role changed: {} → {} (workspace {})",
                    _prev_upper,
                    _new_upper,
                    str(payload.workspaceId)[:12],
                )
                # Early workspace:refresh — mirrors the objective-change pattern
                # (see _on_user_message_request, "objective_change_regeneration_started").
                # Fires BEFORE the deletion / files / skip_reprocess / role-change
                # branches below so the FE shows loading state on role-dependent
                # panels immediately. feature_types matches objective-change exactly.
                if self._socket_client:
                    try:
                        await self._emit_workspace_refresh(
                            workspace_id=str(payload.workspaceId),
                            user_id=str(payload.userId),
                            reason="role_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                            ],
                        )
                    except Exception as refresh_err:
                        logger.warning(
                            "workspace:refresh emit on role-change (early) failed (non-fatal): {}",
                            refresh_err,
                        )

            # Update workspace metadata (for logging)
            updates = {}
            if payload.title:
                updates["title"] = payload.title
            if payload.status:
                updates["status"] = payload.status
            if payload.workspaceRole:
                updates["workspaceRole"] = payload.workspaceRole

            logger.info(f"Workspace updates: {updates}")

            # Handle document deletion: remove from vector store and regenerate strategy from remaining docs
            if deleted_ids:
                from app.vectorstore.qdrant import vector_store

                logger.info(
                    "🗑️ Deleting {} document(s) from vector store: {}",
                    len(deleted_ids),
                    deleted_ids,
                )
                delete_ok = False
                try:
                    await vector_store.delete_documents(
                        workspace_id=payload.workspaceId,
                        document_ids=deleted_ids,
                    )
                    delete_ok = True
                except Exception as del_err:
                    logger.error("Vector store delete failed: {}", str(del_err))
                    logger.exception(del_err)
                if delete_ok:
                    # Resolve role once; list_of_dates + applicable_laws must run after delete even if strategy emit fails.
                    workspace_role = resolved_workspace_role

                    await self._emit_workspace_refresh(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        reason="ai_update_started",
                        feature_types=[
                            "strategy_objective",
                            "analysis_report",
                            "list_of_dates",
                            "applicable_laws",
                            "procedural_timeline",
                            # Document delete regenerates these too (facts changed),
                            # so show their loaders as well.
                            "relevant_parties",
                            "case_laws",
                            "drafting_assistant",
                        ],
                    )

                    # Strategy: generate or use fallback so backend always receives strategy_objective (e.g. when all docs deleted)
                    strategies_result = None
                    try:
                        strategies_result = (
                            await self._generate_strategies_from_documents(
                                workspace_id=payload.workspaceId,
                                user_id=payload.userId,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as strategy_err:
                        logger.warning(
                            "Strategy generation after delete failed: {}; using fallback",
                            strategy_err,
                        )
                    if not strategies_result:
                        strategies_result = self._strategy_objective_fallback(
                            payload.workspaceId,
                            payload.userId,
                            workspace_role,
                            is_update=True,
                        )
                    try:
                        if self._socket_client:
                            await self._socket_client.emit(
                                "document_feature_response",
                                {
                                    "featureType": "strategy_objective",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": strategies_result,
                                },
                            )
                            self._workspace_strategy_payload[payload.workspaceId] = (
                                strategies_result
                            )
                            logger.success(
                                "🎉 Strategy & Objectives sent after document deletion"
                            )
                    except Exception as strat_emit_err:
                        logger.warning(
                            "Strategy emit after delete failed: {}", strat_emit_err
                        )

                    # Analysis report: always emit valid payload (never null); use fallback when no docs or on error
                    analysis_report = None
                    try:
                        analysis_report = (
                            await self._generate_analysis_report_from_documents(
                                workspace_id=payload.workspaceId,
                                user_id=payload.userId,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as ar_err:
                        logger.warning(
                            "Analysis report after delete failed: {}; using fallback",
                            ar_err,
                        )
                    if not analysis_report or not isinstance(analysis_report, dict):
                        analysis_report = self._analysis_report_fallback(
                            (workspace_role or "ADJUDICATOR").strip().upper()
                        )
                    # Document deletion REPLACES the analysis report with the
                    # freshly-regenerated one (derived from the remaining docs)
                    # instead of unioning with the previous version. Analysis
                    # items are free-text with no linkage to a source document,
                    # so a merge would keep strengths/weaknesses/risks derived
                    # from the just-deleted document (same grow-only-on-delete
                    # issue as case_laws / applicable_laws). Uploads / other
                    # regenerations still merge (see the sync path below).
                    logger.info(
                        "Analysis report: replacing (not merging) after document "
                        "deletion for workspace {}",
                        str(payload.workspaceId)[:12],
                    )
                    try:
                        if self._socket_client:
                            await self._socket_client.emit(
                                "document_feature_response",
                                {
                                    "featureType": "analysis_report",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": analysis_report,
                                },
                            )
                            self._workspace_analysis_payload[payload.workspaceId] = (
                                analysis_report
                            )
                            logger.success(
                                "✅ Analysis report sent after document deletion"
                            )
                    except Exception as ar_emit_err:
                        logger.warning(
                            "Analysis report emit after delete failed: {}", ar_emit_err
                        )

                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (after document deletion)",
                        deleted_doc_ids=list(deleted_ids),
                    )

            # Process new evidence files if any (skip files that are deleted: isDeleted true or in deleted_ids)
            files_to_process = []
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", False) or getattr(
                        file, "is_deleted", False
                    )
                    if is_deleted:
                        logger.info(
                            "Skipping evidence file (isDeleted=true): {} ({})",
                            getattr(file, "filename", ""),
                            getattr(file, "id", ""),
                        )
                        continue
                    file_id = (
                        getattr(file, "id", None)
                        or (
                            file.__dict__.get("id")
                            if hasattr(file, "__dict__")
                            else None
                        )
                    ) or None
                    if file_id and str(file_id) in deleted_ids:
                        logger.info(
                            "Skipping evidence file (in deleted list): {} ({})",
                            getattr(file, "filename", ""),
                            file_id,
                        )
                        continue
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt,
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file_id:
                        file_dict["_id"] = file_id
                    files_to_process.append(file_dict)

                # Prevent duplicate processing on delete syncs/reconnect:
                # keep only files that are NOT already present in vector store.
                # (If a file already exists and wasn't changed, processing it again causes
                # duplicate strategy/analysis/list-of-dates regeneration.)
                try:
                    from app.vectorstore.qdrant import vector_store

                    existing_docs_after_sync = await vector_store.get_all_documents(
                        workspace_id=payload.workspaceId
                    )
                    existing_ids_after_sync = {
                        str(d.get("document_id"))
                        for d in (existing_docs_after_sync or [])
                        if d.get("document_id")
                    }
                    original_count = len(files_to_process)
                    filtered_files = []
                    for f in files_to_process:
                        fid = str(f.get("_id") or f.get("id") or "").strip()
                        # If file id is known and already in vector store, skip re-processing.
                        if fid and fid in existing_ids_after_sync:
                            continue
                        filtered_files.append(f)
                    files_to_process = filtered_files
                    if original_count != len(files_to_process):
                        logger.info(
                            "Filtered {} already-indexed file(s) from sync batch; remaining to process: {}",
                            original_count - len(files_to_process),
                            len(files_to_process),
                        )
                except Exception as dedupe_err:
                    logger.debug(
                        "Could not de-duplicate sync files against vector store: {}",
                        dedupe_err,
                    )

                if not files_to_process:
                    logger.info(
                        "No new/updated evidence files to process (all skipped or empty list)"
                    )
                else:
                    logger.info(
                        "Processing {} new/updated evidence files",
                        len(files_to_process),
                    )
                    # Fire workspace:refresh IMMEDIATELY — before document
                    # processing, strategy generation, analysis report, etc.
                    # — so the FE shows the loading state on every panel as
                    # soon as the new doc is detected, instead of waiting for
                    # the heavy pipeline to start ~10–20s later. Gated on
                    # has_prior_panels so first-time uploads don't show a
                    # spurious "refreshing" badge for panels that don't exist
                    # yet.
                    has_prior_panels = bool(
                        self._workspace_strategy_payload.get(payload.workspaceId)
                        or self._workspace_analysis_payload.get(payload.workspaceId)
                        or self._workspace_list_of_dates_payload.get(payload.workspaceId)
                        or self._workspace_applicable_laws_payload.get(payload.workspaceId)
                        or self._workspace_procedural_timeline_payload.get(payload.workspaceId)
                    )
                    if has_prior_panels and self._socket_client:
                        try:
                            await self._emit_workspace_refresh(
                                workspace_id=str(payload.workspaceId),
                                user_id=str(payload.userId),
                                reason="ai_update_started",
                                feature_types=[
                                    # Re-uploads rebuild the assessments payload
                                    # below, so surface its loader too.
                                    "document_assessments",
                                    "strategy_objective",
                                    "analysis_report",
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                    # New-document upload regenerates these too
                                    # (facts changed), so show their loaders as well.
                                    "relevant_parties",
                                    "case_laws",
                                    "drafting_assistant",
                                ],
                            )
                        except Exception as refresh_err:
                            logger.warning(
                                "Early workspace:refresh emit failed (non-fatal): {}",
                                refresh_err,
                            )

            # Skip re-processing if nothing was deleted and all current files are already in the vector store
            # (e.g. backend sent workspace_sync right after workspace_created with same files)
            skip_reprocess = False
            if not deleted_ids and files_to_process:
                try:
                    from app.vectorstore.qdrant import vector_store

                    existing_docs = await vector_store.get_all_documents(
                        workspace_id=payload.workspaceId
                    )
                    existing_ids = {
                        str(d.get("document_id"))
                        for d in existing_docs
                        if d.get("document_id")
                    }
                    file_ids = [
                        str(f.get("_id") or f.get("id")) or "" for f in files_to_process
                    ]
                    if (
                        len(file_ids) == len(files_to_process)
                        and all(fid.strip() for fid in file_ids)
                        and all(fid in existing_ids for fid in file_ids)
                    ):
                        skip_reprocess = True
                        logger.info(
                            "No new or changed documents (all {} file(s) already in vector store); skipping re-processing and strategy/analysis regeneration",
                            len(files_to_process),
                        )
                except Exception as vec_err:
                    logger.debug(
                        "Could not check existing documents for skip: {}", vec_err
                    )

            if payload.evidenceFiles and files_to_process and not skip_reprocess:
                try:
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId,
                    )

                    logger.success(
                        "✅ Document sync complete: {}/{} successful",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0),
                    )

                    # Send processing results back to backend
                    if self._socket_client:
                        try:
                            # Prepare document results (with summary and evidentiary strength)
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get(
                                        "readability_score", 0
                                    ),
                                    "readabilityRating": result.get(
                                        "readability_rating", "Unknown"
                                    ),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get(
                                        "extracted_text_length", 0
                                    ),
                                    "storedInVectorDb": result.get(
                                        "stored_in_vector_db", False
                                    ),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get(
                                        "evidentiary_strength", "N/A"
                                    ),
                                    "documentType": result.get(
                                        "document_type", "other"
                                    ),
                                    "extractedFields": result.get(
                                        "extracted_fields", {}
                                    ),
                                    "sensitiveFields": result.get(
                                        "sensitive_fields", {}
                                    ),
                                }
                                # Audio: attach transcript + transcriptStatus.
                                if result.get("is_audio"):
                                    status = result.get("transcript_status", "failed")
                                    transcript_text = result.get("transcript", "") or ""
                                    if status == "ready" and transcript_text:
                                        doc_result["transcript"] = transcript_text
                                        doc_result["transcriptStatus"] = "ready"
                                    else:
                                        doc_result = {
                                            "documentId": result.get("document_id"),
                                            "fileName": result.get("filename"),
                                            "transcriptStatus": status if status in ("pending", "failed") else "failed",
                                        }
                                if not result.get("success") and "transcriptStatus" not in doc_result:
                                    doc_result["error"] = result.get(
                                        "error", "Unknown error"
                                    )
                                document_results.append(doc_result)

                            # Emit document_feature_response for document assessments (per backend spec)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get(
                                            "total_files", 0
                                        ),
                                        "successfulFiles": batch_result.get(
                                            "successful", 0
                                        ),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get(
                                            "average_readability_score", 0
                                        ),
                                    },
                                },
                            }

                            await self._socket_client.emit(
                                "document_feature_response", document_assessment_payload
                            )
                            logger.success(
                                "✅ Document assessments (sync) sent to backend"
                            )

                            # Emit workspace_metadata on sync: do NOT send title (send empty) so backend keeps existing title unchanged
                            if batch_result.get("successful", 0) > 0:
                                try:
                                    meta = await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                    workspace_metadata_payload = {
                                        "featureType": "workspace_metadata",
                                        "workspaceId": payload.workspaceId,
                                        "userId": payload.userId,
                                        "payload": {
                                            "workspaceTitle": "",
                                            "updateTitle": False,
                                            "caseCategory": meta.get(
                                                "caseCategory", "General"
                                            ),
                                            "subcategories": meta.get(
                                                "subcategories", []
                                            ),
                                            "description": meta.get("description", ""),
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload,
                                    )
                                    logger.success(
                                        "✅ Workspace metadata (sync) sent: title=unchanged, category={}, subcategories={}, description={}",
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
                                        (meta.get("description") or "")[:50],
                                    )
                                except Exception as meta_err:
                                    logger.warning(
                                        "Failed to emit workspace_metadata (sync): {}",
                                        str(meta_err),
                                    )

                            if batch_result.get("successful", 0) > 0:
                                workspace_role = resolved_workspace_role
                                logger.info(
                                    "Using workspace role for strategies (sync): {}",
                                    workspace_role,
                                )
                                logger.info(
                                    "🎯 Scheduling strategy + analysis + list_of_dates + "
                                    "applicable_laws + procedural_timeline on sync (background)…"
                                )
                                # NOTE: workspace:refresh is now emitted earlier
                                # in this handler — right after detecting new
                                # files to process — so the FE loader fires
                                # immediately instead of after document
                                # processing completes here.
                                self._schedule_strategy_analysis_pipeline(
                                    workspace_id=str(payload.workspaceId),
                                    user_id=str(payload.userId),
                                    workspace_role=workspace_role,
                                    batch_result=batch_result,
                                    is_update=True,
                                    analysis_log_context=" (sync)",
                                )
                            else:
                                # When no files succeeded on sync, still emit analysis report (fallback)
                                if self._socket_client:
                                    try:
                                        workspace_role = resolved_workspace_role
                                        fallback = self._analysis_report_fallback(
                                            workspace_role
                                        )
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            {
                                                "featureType": "analysis_report",
                                                "workspaceId": payload.workspaceId,
                                                "userId": payload.userId,
                                                "payload": fallback,
                                            },
                                        )
                                        self._workspace_analysis_payload[
                                            payload.workspaceId
                                        ] = fallback
                                        logger.info(
                                            "✅ Analysis report (fallback, 0 successful sync) sent to backend"
                                        )
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            {
                                                "featureType": "list_of_dates",
                                                "workspaceId": payload.workspaceId,
                                                "userId": payload.userId,
                                                "payload": {
                                                    "workspaceId": payload.workspaceId,
                                                    "userId": payload.userId,
                                                    "listOfDates": [],
                                                },
                                            },
                                        )
                                        self._workspace_list_of_dates_payload[
                                            payload.workspaceId
                                        ] = []
                                        self._schedule_list_of_dates_and_applicable_laws(
                                            workspace_id=str(payload.workspaceId),
                                            user_id=str(payload.userId),
                                            workspace_role=workspace_role,
                                            batch_result=None,
                                            log_context=" (sync, 0 docs success)",
                                        )
                                    except Exception as e:
                                        logger.error(
                                            "Failed to emit analysis report fallback (sync 0 success): {}",
                                            e,
                                        )
                        except Exception as emit_error:
                            logger.error(f"Failed to emit sync results: {emit_error}")
                except Exception as doc_error:
                    logger.error("Error syncing documents: {}", str(doc_error))

            # When we skipped re-processing (no new/changed/deleted docs), re-emit from cache only.
            # Do NOT regenerate — only when user adds/updates/deletes documents do we regenerate (paths above).
            elif skip_reprocess and files_to_process and self._socket_client:
                workspace_id = payload.workspaceId
                user_id = payload.userId
                workspace_role = resolved_workspace_role
                # Re-emit strategy from cache (no regeneration); then analysis_report. Single emit block, no try/except around emits.
                strategies_result = self._workspace_strategy_payload.get(workspace_id)
                if strategies_result:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": strategies_result,
                        },
                    )
                    logger.success(
                        "✅ Strategy & Objectives re-emitted to backend (from cache)"
                    )
                    await asyncio.sleep(
                        0.2
                    )  # Space out emits so backend can process each; reduces disconnect risk
                else:
                    try:
                        strategies_result = (
                            await self._generate_strategies_from_documents(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as strategy_err:
                        logger.warning(
                            "Failed to generate strategy on skip_reprocess: {}",
                            strategy_err,
                        )
                        strategies_result = None
                    if strategies_result:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "strategy_objective",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": strategies_result,
                            },
                        )
                        self._workspace_strategy_payload[workspace_id] = (
                            strategies_result
                        )
                        logger.success(
                            "✅ Strategy & Objectives emitted to backend (skip_reprocess, generated once)"
                        )

                analysis_report = self._workspace_analysis_payload.get(workspace_id)
                if analysis_report:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": analysis_report,
                        },
                    )
                    logger.success(
                        "✅ Analysis report re-emitted to backend (from cache)"
                    )
                    await asyncio.sleep(
                        0.15
                    )  # Allow socket to flush before audit/sync complete
                    list_of_dates = self._workspace_list_of_dates_payload.get(
                        workspace_id
                    )
                    if list_of_dates is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "list_of_dates",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": {
                                    "workspaceId": workspace_id,
                                    "userId": user_id,
                                    "listOfDates": list_of_dates,
                                },
                            },
                        )
                        logger.success(
                            "✅ List of dates re-emitted to backend (from cache)"
                        )
                    applicable_laws = self._workspace_applicable_laws_payload.get(
                        workspace_id
                    )
                    if applicable_laws is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "applicable_laws",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": applicable_laws,
                            },
                        )
                        logger.success(
                            "✅ Applicable laws re-emitted to backend (from cache)"
                        )
                    case_laws = self._workspace_case_laws_payload.get(workspace_id)
                    if case_laws is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "case_laws",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": case_laws,
                            },
                        )
                        logger.success(
                            "✅ Case laws re-emitted to backend (from cache)"
                        )
                    relevant_parties = self._workspace_relevant_parties_payload.get(
                        workspace_id
                    )
                    if relevant_parties is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "relevant_parties",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": relevant_parties,
                            },
                        )
                        logger.success(
                            "✅ Relevant parties re-emitted to backend (from cache)"
                        )
                    # Procedural timeline ("Upcoming Dates"): re-emit from
                    # cache if present, then schedule a refresh so the FE
                    # reflects any newly-uploaded docs even when the
                    # vector-store said "no new content". Without this the
                    # user saw the four other panels refresh and "Upcoming
                    # Dates" stayed stale or empty.
                    procedural_timeline = (
                        self._workspace_procedural_timeline_payload.get(workspace_id)
                    )
                    if procedural_timeline is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "procedural_timeline",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": {
                                    "proceduralTimeline": procedural_timeline,
                                },
                            },
                        )
                        logger.success(
                            "✅ Procedural timeline re-emitted to backend (from cache)"
                        )
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (skip_reprocess full-refresh)",
                    )
                else:
                    await self._emit_analysis_report(
                        workspace_id,
                        user_id,
                        workspace_role,
                        batch_result=None,
                        is_update=True,
                        log_context=" (skip_reprocess)",
                    )

            # Role-only change (no new files, no deletions): regenerate role-dependent
            # features (strategy, analysis, applicable_laws) and re-emit role-independent
            # features (list_of_dates, procedural_timeline, document_assessments,
            # workspace_metadata) from cache. CLAUDE.md L39: "role changes invalidate
            # all derived intelligence" — this branch implements that contract for
            # the socket path. Reached only when none of the file/delete/skip_reprocess
            # branches above ran, but the user changed the role on Edit Workspace.
            elif role_changed and self._socket_client:
                workspace_id = payload.workspaceId
                user_id = payload.userId
                workspace_role = resolved_workspace_role
                log_ctx = " (role change)"

                # Hydrate in-memory caches from Qdrant before the re-emit guards
                # below check `.get(workspace_id)`. Without this, a process
                # restart between workspace_created and the role-change sync
                # leaves _workspace_list_of_dates_payload and
                # _workspace_procedural_timeline_payload empty in memory, so the
                # `if cached_X is not None` checks silently skip the re-emits.
                # The helper is idempotent (warm-cache short-circuit).
                try:
                    await self._hydrate_workspace_caches(
                        str(workspace_id), user_id=str(user_id)
                    )
                except Exception as hydrate_err:
                    logger.debug(
                        "Role-change branch: cache hydration failed (non-fatal): {}",
                        hydrate_err,
                    )

                # workspace:refresh was already emitted early (right after
                # role-change detection) to mirror the objective-change pattern;
                # don't re-emit here.

                # Re-emit role-independent panels from cache so the UI stays
                # populated while role-dependent panels regenerate.
                cached_assessments = self._workspace_document_assessments_payload.get(
                    workspace_id
                )
                if cached_assessments:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "document_assessments",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": cached_assessments,
                        },
                    )
                    logger.success(
                        "✅ Document assessments re-emitted{} (from cache)", log_ctx
                    )

                cached_metadata = self._workspace_metadata_payload.get(workspace_id)
                if cached_metadata:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "workspace_metadata",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": cached_metadata,
                        },
                    )
                    logger.success(
                        "✅ Workspace metadata re-emitted{} (from cache)", log_ctx
                    )

                # NOTE: list_of_dates and procedural_timeline are re-emitted
                # from cache AFTER analysis_report (below) to mirror the
                # skip_reprocess branch (L12506+) and the objective-change flow's
                # order: workspace:refresh → strategy → analysis → list_of_dates
                # → procedural_timeline. Emitting them up here (before the
                # strategy/analysis regens) caused the FE to drop the
                # list_of_dates payload, because the early workspace:refresh
                # told the FE to expect a fresh list_of_dates AFTER the
                # role-dependent panels finished regenerating.

                # 3) Regenerate strategy_objective for the new role, emit + cache.
                strategies_result = None
                try:
                    strategies_result = await self._generate_strategies_from_documents(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        is_update=True,
                    )
                except Exception as strategy_err:
                    logger.warning(
                        "Strategy regeneration{} failed: {}", log_ctx, strategy_err
                    )
                if not strategies_result:
                    strategies_result = self._strategy_objective_fallback(
                        workspace_id, user_id, workspace_role, is_update=True
                    )
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": strategies_result,
                    },
                )
                self._workspace_strategy_payload[workspace_id] = strategies_result
                logger.success("🎉 Strategy & Objectives regenerated{}", log_ctx)
                await asyncio.sleep(0.15)

                # 4) Regenerate analysis_report for the new role, emit + cache.
                # Do NOT use self._emit_analysis_report — it auto-schedules
                # list_of_dates/applicable_laws/procedural_timeline regeneration,
                # which would re-run the role-independent features we just
                # re-emitted from cache. Mirror the deletion-path pattern instead.
                analysis_report = None
                try:
                    analysis_report = (
                        await self._generate_analysis_report_from_documents(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=workspace_role,
                            batch_result=None,
                            is_update=True,
                        )
                    )
                except Exception as ar_err:
                    logger.warning(
                        "Analysis report regeneration{} failed: {}",
                        log_ctx,
                        ar_err,
                    )
                if not analysis_report or not isinstance(analysis_report, dict):
                    analysis_report = self._analysis_report_fallback(
                        (workspace_role or "ADJUDICATOR").strip().upper()
                    )
                if isinstance(analysis_report, dict):
                    try:
                        analysis_report = (
                            await self._merge_analysis_report_with_previous(
                                workspace_id=str(workspace_id),
                                workspace_role=workspace_role,
                                new_analysis=analysis_report,
                                log_context=log_ctx,
                            )
                        )
                    except Exception as merge_err:
                        logger.warning(
                            "Analysis report merge{} failed: {}; emitting unmerged",
                            log_ctx,
                            merge_err,
                        )
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": analysis_report,
                    },
                )
                self._workspace_analysis_payload[workspace_id] = analysis_report
                logger.success("✅ Analysis report regenerated{}", log_ctx)
                # Allow socket to flush analysis_report before the next emit
                # (mirrors skip_reprocess branch pattern).
                await asyncio.sleep(0.15)

                # 5) Re-emit list_of_dates from cache (role-independent).
                #    Placed AFTER analysis_report so the FE state machine
                #    accepts it — matches the order used by the skip_reprocess
                #    branch (L12506+) and the objective-change flow.
                cached_list_of_dates = self._workspace_list_of_dates_payload.get(
                    workspace_id
                )
                if cached_list_of_dates is not None:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "listOfDates": cached_list_of_dates,
                            },
                        },
                    )
                    logger.success(
                        "✅ List of dates re-emitted{} (from cache)", log_ctx
                    )

                # 6) Re-emit procedural_timeline from cache (role-independent).
                cached_procedural_timeline = (
                    self._workspace_procedural_timeline_payload.get(workspace_id)
                )
                if cached_procedural_timeline is not None:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "procedural_timeline",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "proceduralTimeline": cached_procedural_timeline,
                            },
                        },
                    )
                    logger.success(
                        "✅ Procedural timeline re-emitted{} (from cache)", log_ctx
                    )

                # 7) Regenerate applicable_laws only (do not touch list_of_dates
                #    or procedural_timeline — they are role-independent).
                self._schedule_applicable_laws_only(
                    workspace_id=str(workspace_id),
                    user_id=str(user_id),
                    workspace_role=workspace_role,
                    log_context=log_ctx,
                )

            # When sync has no evidence files (e.g. user deleted all docs), emit both strategy and analysis
            # with empty/fallback payload so backend receives both and can show empty state (not null).
            elif not payload.evidenceFiles and self._socket_client:
                try:
                    workspace_id = payload.workspaceId
                    user_id = payload.userId
                    # Explicit [] means "no documents in workspace" — wipe vector DB so re-uploads are clean.
                    # evidenceFiles omitted (None) may be a partial sync; do not delete vectors in that case.
                    if (
                        payload.evidenceFiles is not None
                        and len(payload.evidenceFiles) == 0
                    ):
                        try:
                            from app.vectorstore.qdrant import vector_store

                            await vector_store.delete_workspace(
                                workspace_id=workspace_id
                            )
                            logger.info(
                                "🗑️ Cleared all vectors for workspace {} (evidenceFiles is empty list)",
                                workspace_id,
                            )
                        except Exception as clear_err:
                            logger.warning(
                                "Could not clear vector store for empty evidence list: {}",
                                clear_err,
                            )
                    workspace_role = (
                        resolved_workspace_role
                    )
                    strategy_fallback = self._strategy_objective_fallback(
                        workspace_id, user_id, workspace_role, is_update=True
                    )
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": strategy_fallback,
                        },
                    )
                    self._workspace_strategy_payload[workspace_id] = strategy_fallback
                    analysis_fallback = self._analysis_report_fallback(workspace_role)
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": analysis_fallback,
                        },
                    )
                    self._workspace_analysis_payload[workspace_id] = analysis_fallback
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "listOfDates": [],
                            },
                        },
                    )
                    self._workspace_list_of_dates_payload[workspace_id] = []
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (sync, no documents)",
                    )
                    logger.success(
                        "✅ Strategy & Analysis report & list of dates (empty) sent to backend (no documents); applicable laws scheduled"
                    )
                except Exception as e:
                    logger.error(
                        "Failed to emit strategy/analysis fallbacks (no docs): {}", e
                    )

            # Log audit event
            audit_logger.log_event(
                "workspace_sync_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "updates": updates,
                    "new_files_count": (
                        len(payload.evidenceFiles) if payload.evidenceFiles else 0
                    ),
                    "deleted_document_ids_count": len(deleted_ids),
                    "processed_files_count": len(files_to_process),
                },
            )

            logger.success("✅ Workspace sync completed: {}", payload.workspaceId)

        except Exception as e:
            logger.error("Error processing workspace_sync event: {}", str(e))
            logger.exception(e)
            raise

    async def on_workspace_updated(self, data: Dict[str, Any]) -> None:
        """Handle workspace update event (legacy)."""
        try:
            logger.debug("Processing legacy workspace_updated event")
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace updated (legacy): {workspace_id}")

            audit_logger.log_event(
                "workspace_updated_legacy", {"workspace_id": workspace_id}
            )
        except Exception as e:
            logger.error(f"Error processing workspace_updated: {e}")
            raise

    async def on_workspace_deleted(self, data: Dict[str, Any]) -> None:
        """Handle workspace deletion event (legacy)."""
        try:
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace deleted (legacy): {workspace_id}")

            # Delete the workspace
            await workspace_manager.delete_workspace(workspace_id)

            audit_logger.log_event("workspace_deleted", {"workspace_id": workspace_id})
        except Exception as e:
            logger.error(f"Error processing workspace_deleted: {e}")
            raise
