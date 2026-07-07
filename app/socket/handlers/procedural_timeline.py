"""Procedural timeline (upcoming dates) generation and feedback.

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
)


from app.prompts.procedural_timeline_prompts import (
    regenerate_procedural_timeline_with_feedback_prompt,
)


class ProceduralTimelineMixin:
    """Procedural timeline (upcoming dates) generation and feedback."""

    async def _regenerate_procedural_timeline_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        emit_result: bool = True,
    ) -> bool:
        """
        Apply a surgical find-and-replace / correction to the EXISTING Procedural
        Timeline (Upcoming Dates) entries (Mode 1) — a NAME, TERM, DATE, or YEAR
        change. Surgical only: applies the instruction to each entry's ``title`` /
        ``description`` (and, for a date/year change, the ISO ``date`` field too)
        and preserves ``type`` and the entry set. Never regenerates the timeline.
        Operates on the cached ``_workspace_procedural_timeline_payload`` and
        re-emits featureType=procedural_timeline.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_procedural_timeline_payload.get(workspace_id)
            if not isinstance(current, list) or not current:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = regenerate_procedural_timeline_with_feedback_prompt(current_json=current_json, instruction=instruction)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.1,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="array")
            if not json_str:
                return False
            edited = json.loads(json_str)
            if not isinstance(edited, list) or len(edited) != len(current):
                logger.warning(
                    "Procedural timeline name edit rejected (count {} != {}); keeping cache",
                    len(edited) if isinstance(edited, list) else "n/a",
                    len(current),
                )
                return False

            edited = scrub_app_brand(edited)
            if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                current, sort_keys=True, ensure_ascii=True
            ):
                return False  # nothing changed

            self._workspace_procedural_timeline_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "procedural_timeline",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {"proceduralTimeline": edited},
                    },
                )
                logger.success(
                    "✅ Procedural timeline name edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Procedural timeline name edit failed: {}", e)
            return False

    async def _generate_procedural_timeline_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        all_docs: List[Dict[str, Any]],
        workspace_role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Delegate to the ProceduralTimelineAgent (LangGraph 3-node pipeline).

        The agent:
          1. Extracts timeline entries per document in parallel
             (self-classifying prompt — one call per document).
          2. Runs a consolidation LLM pass that semantically merges
             cross-document duplicates and drops commercial noise.
          3. Validates formats, normalises types, fuzzy-deduplicates,
             and sorts chronologically.

        Returns list of {title, date, description, type} procedural entries.
        """
        from datetime import datetime as _dt

        if not all_docs:
            return []

        from app.langgraph.procedural_timeline_agent import procedural_timeline_agent

        # Build the document list the agent expects: {document_id, name, content}
        documents = []
        for i, doc in enumerate(all_docs, 1):
            content = (doc.get("content") or "").strip()
            if not content or len(content) < 20:
                continue
            documents.append(
                {
                    "document_id": str(doc.get("document_id") or f"doc_{i}"),
                    "name": str(doc.get("name") or doc.get("filename") or f"Annexure {i}"),
                    "content": content,
                }
            )

        if not documents:
            return []

        try:
            result = await procedural_timeline_agent.extract(
                workspace_id=workspace_id,
                documents=documents,
                today_str=_dt.utcnow().strftime("%Y-%m-%d"),
                document_inventory=self._build_document_inventory(workspace_id),
            )
            logger.info(
                "Procedural timeline: generated {} entries for workspace {}",
                len(result),
                workspace_id[:12] if workspace_id else "",
            )
            # Strip the application's own brand/platform name from every
            # milestone (title / description) before caching/emitting.
            return scrub_app_brand(result)
        except Exception as e:
            logger.warning("Procedural timeline agent failed: {}", e)
            return []

    async def _generate_and_emit_procedural_timeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        batch_result: Optional[Dict[str, Any]] = None,
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Fetch workspace documents, generate procedural/upcoming dates, emit to backend and cache.
        Emits document_feature_response with featureType "procedural_timeline".

        ``deleted_doc_ids`` mirrors the parameter on
        ``_generate_and_emit_list_of_dates``. When deletion is the only
        change (``batch_result is None`` and ``deleted_doc_ids`` is
        non-empty), a fast path is taken: the cached prior list is filtered
        to remove entries from deleted documents and emitted, with no LLM
        regeneration. The remaining documents have not changed, so their
        timeline entries are already in the cache.
        """
        if not self._socket_client:
            return
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("Procedural timeline: could not fetch documents: {}", e)
            all_docs = []

        # ------------------------------------------------------------------
        # FAST PATH: pure-deletion regeneration. See the same block in
        # _generate_and_emit_list_of_dates for rationale.
        # ------------------------------------------------------------------
        deleted_set_fast = {str(x).strip() for x in (deleted_doc_ids or []) if x}
        if deleted_set_fast and not isinstance(batch_result, dict):
            live_doc_ids_fast = {
                str(d.get("document_id") or "").strip()
                for d in (all_docs or [])
                if d.get("document_id")
            }
            prior_fast = (
                self._workspace_procedural_timeline_payload.get(workspace_id) or []
            )
            if not isinstance(prior_fast, list):
                prior_fast = []
            before_count = len(prior_fast)
            prior_fast = [
                e
                for e in prior_fast
                if isinstance(e, dict)
                and str(e.get("sourceDocId") or "").strip() not in deleted_set_fast
            ]
            prior_fast = self._drop_entries_for_deleted_docs(
                prior_fast,
                live_doc_ids_fast,
                drop_unattributed=True,
            )
            prior_fast.sort(key=lambda x: str(x.get("date") or ""))
            self._workspace_procedural_timeline_payload[workspace_id] = prior_fast
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "procedural_timeline",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": {"proceduralTimeline": prior_fast},
                },
            )
            logger.success(
                "✅ Procedural timeline filtered+emitted after deletion (no LLM regen): "
                "{} → {} entries (dropped {})",
                before_count,
                len(prior_fast),
                before_count - len(prior_fast),
            )
            await asyncio.sleep(0.1)
            return

        processed_doc_ids: set = set()
        if isinstance(batch_result, dict):
            for r in batch_result.get("results") or []:
                did = str(r.get("document_id") or "").strip()
                if did:
                    processed_doc_ids.add(did)

        target_docs = all_docs
        if processed_doc_ids:
            target_docs = [
                d for d in (all_docs or [])
                if str(d.get("document_id") or "").strip() in processed_doc_ids
            ]

        timeline = await self._generate_procedural_timeline_from_documents(
            workspace_id=workspace_id,
            user_id=user_id,
            all_docs=target_docs,
            workspace_role=workspace_role,
        )

        # Same full-prior merge pattern as list_of_dates: feed the entire prior
        # cache to the LLM merge so cross-document paraphrases get caught,
        # regardless of which docs were re-processed in this regeneration.
        #
        # IMPORTANT: filter the cached `prior` against the LIVE document ids
        # before merging — the merge preserves prev verbatim, so otherwise
        # entries from deleted documents survive every regeneration. Same
        # bug shape as list_of_dates; same self-healing fix.
        live_doc_ids = {
            str(d.get("document_id") or "").strip()
            for d in (all_docs or [])
            if d.get("document_id")
        }
        prior = self._workspace_procedural_timeline_payload.get(workspace_id) or []
        if not isinstance(prior, list):
            prior = []
        # Deletion context: deterministic pre-pass mirroring the
        # list_of_dates path; see comment there for rationale.
        deleted_set = {str(x).strip() for x in (deleted_doc_ids or []) if x}
        if deleted_set:
            before = len(prior)
            prior = [
                e
                for e in prior
                if isinstance(e, dict)
                and str(e.get("sourceDocId") or "").strip() not in deleted_set
            ]
            if before != len(prior):
                logger.info(
                    "procedural_timeline: explicit-delete pre-pass dropped {} cached entry/entries",
                    before - len(prior),
                )
        prior = self._drop_entries_for_deleted_docs(
            prior,
            live_doc_ids,
            drop_unattributed=bool(deleted_set),
        )
        if prior:
            merged = await self._llm_merge_dated_entries_against_previous(
                prior, timeline or [],
                item_type="procedural timeline",
            )
        else:
            merged = list(timeline or [])
        merged.sort(key=lambda x: str(x.get("date") or ""))
        self._workspace_procedural_timeline_payload[workspace_id] = merged
        timeline = merged

        payload = {
            "proceduralTimeline": timeline,
        }
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "procedural_timeline",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ Procedural timeline sent to backend ({} entries)", len(timeline)
        )
        await asyncio.sleep(0.1)
