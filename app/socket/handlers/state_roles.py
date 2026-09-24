"""Workspace cache hydration and role memory/restore/resolve.

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
from app.workspace.participants import hydrate_participants_from_cache
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


class StateRolesMixin:
    """Workspace cache hydration and role memory/restore/resolve."""

    async def _hydrate_workspace_caches(
        self,
        workspace_id: str,
        *,
        user_id: Optional[str] = None,
        recover: bool = True,
    ) -> None:
        """Repopulate in-memory per-workspace caches from Qdrant.

        Runs at the start of chat and document-feature handlers so that an AI
        service restart does not silently degrade chat with empty annexures,
        missing analysis report and missing applicable laws. No-ops when the
        in-memory cache already has data for this workspace (the live process
        is the source of truth).
        """
        if not workspace_id:
            return
        # A restart loses the in-memory recovery markers, so the reconnect sweep
        # in _recover_interrupted_work finds nothing. Hydration is the first
        # moment we know this workspace exists again, so it is also where a
        # payload that was generated-but-never-delivered gets picked up.
        # ``recover=False`` is passed by the recovery path itself to avoid
        # re-entering here.
        if recover:
            try:
                await self._recover_workspace_after_hydrate(workspace_id, user_id)
            except Exception as rec_err:
                logger.debug(
                    "Post-hydrate recovery skipped (workspace={}): {}",
                    workspace_id,
                    rec_err,
                )
        try:
            # Workspace context (title, status, role, evidence files, metadata)
            from app.workspace.context import context_manager

            existing_ctx = context_manager.get_context(workspace_id)
            need_meta_hydration = (
                existing_ctx is None
                or not (existing_ctx.evidence_files or [])
                or not existing_ctx.metadata
            )
        except Exception:
            existing_ctx = None
            need_meta_hydration = True

        # Skip work entirely when every cache we care about is already warm.
        all_warm = (
            not need_meta_hydration
            and bool(self._workspace_analysis_payload.get(workspace_id))
            and bool(self._workspace_applicable_laws_payload.get(workspace_id))
            and bool(self._workspace_procedural_timeline_payload.get(workspace_id))
            and bool(self._workspace_list_of_dates_payload.get(workspace_id))
        )
        if all_warm:
            return

        try:
            cached_kinds = await hydrate_workspace_state(workspace_id)
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug(
                "Workspace cache hydration failed (workspace={}): {}",
                workspace_id,
                e,
            )
            cached_kinds = {}

        if not cached_kinds:
            return

        meta_payload = cached_kinds.get(CacheKinds.WORKSPACE_META)
        if isinstance(meta_payload, dict):
            try:
                from app.workspace.context import context_manager

                if existing_ctx is not None:
                    existing_ctx.apply_cache_dict(meta_payload)
                else:
                    await context_manager.hydrate_context_from_cache(
                        workspace_id=workspace_id, user_id=user_id
                    )
            except Exception as e:
                logger.debug(
                    "Workspace context hydration failed (workspace={}): {}",
                    workspace_id,
                    e,
                )

        # Hydrate per-kind in-memory dicts only when nothing is in memory yet.
        kind_to_dict = {
            CacheKinds.ANALYSIS_REPORT: self._workspace_analysis_payload,
            CacheKinds.APPLICABLE_LAWS: self._workspace_applicable_laws_payload,
            CacheKinds.CASE_LAWS: self._workspace_case_laws_payload,
            CacheKinds.RELEVANT_PARTIES: self._workspace_relevant_parties_payload,
            CacheKinds.DELETED_PARTIES: self._workspace_deleted_parties,
            CacheKinds.REMOVED_DOCUMENTS: self._workspace_removed_documents,
            CacheKinds.PROCEDURAL_TIMELINE: self._workspace_procedural_timeline_payload,
            CacheKinds.LIST_OF_DATES: self._workspace_list_of_dates_payload,
            CacheKinds.WORKSPACE_METADATA_PAYLOAD: self._workspace_metadata_payload,
            CacheKinds.DOCUMENT_ASSESSMENTS: self._workspace_document_assessments_payload,
            CacheKinds.STRATEGY_PAYLOAD: self._workspace_strategy_payload,
            CacheKinds.REJECTED_ANALYSIS_ITEMS: self._workspace_rejected_analysis_items,
            CacheKinds.USER_ADDED_MISSING_INFO: self._workspace_user_added_missing_info,
            CacheKinds.USER_ASSERTED_FACTS: self._workspace_user_facts,
            CacheKinds.DRAFTING_SUGGESTIONS: self._workspace_drafting_payload,
            CacheKinds.CURRENT_STATE: self._workspace_current_state_payload,
            # Recovery state: an undelivered panel payload and an unfinished
            # objective change must both survive a restart, since the
            # disconnect that interrupted them is often what restarted us.
            CacheKinds.PENDING_FEATURE_DELIVERY: self._pending_feature_deliveries,
            CacheKinds.PENDING_OBJECTIVE_CHANGE: self._pending_objective_changes,
        }
        for kind, target in kind_to_dict.items():
            payload = cached_kinds.get(kind)
            if payload is None:
                continue
            if target.get(workspace_id):
                continue
            # Defensive normalization: historical analysis-report payloads
            # may still contain dict-shaped risks / weaknesses / missingInfo
            # entries that would otherwise render as Python dict literals
            # in the FE. Clean them up at hydration time so the in-memory
            # cache (and every downstream emit) stays well-formed.
            if kind == CacheKinds.ANALYSIS_REPORT and isinstance(payload, dict):
                try:
                    self._normalize_analysis_report_lists(
                        payload,
                        max_annexure=self._get_annexure_count(workspace_id),
                    )
                except Exception as norm_err:
                    logger.debug(
                        "Analysis report hydration normalization skipped "
                        "(workspace={}): {}",
                        workspace_id,
                        norm_err,
                    )
            try:
                if isinstance(target, PersistentWorkspaceDict):
                    target.hydrate_from_cache(workspace_id, payload)
                else:
                    target[workspace_id] = payload
            except Exception as e:
                logger.debug(
                    "Cache rehydration skipped (kind={}, workspace={}): {}",
                    kind,
                    workspace_id,
                    e,
                )

        ci_payload = cached_kinds.get(CacheKinds.CUSTOM_INSTRUCTIONS)
        if isinstance(ci_payload, dict):
            try:
                hydrate_custom_instructions_from_cache(workspace_id, ci_payload)
            except Exception:
                pass

        # Collaborator roster (workspace sharing). Hydrated like custom
        # instructions because it lives in a module-level dict, not in one of
        # the handler's per-workspace caches above. Without it, a chat carrying
        # askedBy after a restart would resolve against an empty roster.
        participants_payload = cached_kinds.get(CacheKinds.PARTICIPANTS)
        if participants_payload:
            try:
                hydrate_participants_from_cache(workspace_id, participants_payload)
            except Exception:
                pass

        logger.info(
            "Hydrated workspace caches from Qdrant: {} (kinds={})",
            workspace_id,
            sorted(cached_kinds.keys()),
        )

    async def _remember_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        role: str,
        *,
        chat_id: Optional[str] = None,
    ) -> None:
        role_upper = (role or "").strip().upper()
        if role_upper not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            return
        ws_user_key = f"{workspace_id}:{user_id}"
        self._role_cache_by_user_workspace[ws_user_key] = role_upper
        if chat_id:
            self._role_cache_by_chat[f"{workspace_id}:{user_id}:{chat_id}"] = role_upper
        try:
            from app.vectorstore.qdrant import vector_store

            await vector_store.upsert_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                role=role_upper,
                chat_id=chat_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to persist role cache in vector DB (workspace={}, user={}): {}",
                workspace_id,
                user_id,
                e,
            )

    async def _restore_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        *,
        chat_id: Optional[str] = None,
    ) -> Optional[str]:
        if chat_id:
            in_mem_chat = self._role_cache_by_chat.get(f"{workspace_id}:{user_id}:{chat_id}")
            if in_mem_chat:
                return in_mem_chat
        in_mem_user = self._role_cache_by_user_workspace.get(f"{workspace_id}:{user_id}")
        if in_mem_user:
            return in_mem_user
        try:
            from app.vectorstore.qdrant import vector_store

            from_vector_db = await vector_store.get_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                chat_id=chat_id,
            )
            role_upper = str(from_vector_db or "").strip().upper()
            if role_upper in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                self._role_cache_by_user_workspace[f"{workspace_id}:{user_id}"] = role_upper
                if chat_id:
                    self._role_cache_by_chat[f"{workspace_id}:{user_id}:{chat_id}"] = role_upper
                return role_upper
        except Exception as e:
            logger.warning(
                "Failed to restore role cache from vector DB (workspace={}, user={}): {}",
                workspace_id,
                user_id,
                e,
            )
        return None

    async def _resolve_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        *,
        preferred_role: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> str:
        """
        Resolve the effective workspace role before generation.

        Priority:
        1) Explicit role from current payload
        2) Persisted role cache (authoritative: in-memory role cache, then Qdrant)
        3) In-memory workspace context (may be a fabricated ADJUDICATOR default)
        4) ADJUDICATOR fallback

        The persisted role cache MUST win over the in-memory workspace context:
        `upsert_workspace` and `hydrate_context_from_cache` (app/workspace/context.py)
        default a role-less context to ADJUDICATOR, and that fabricated default
        would otherwise shadow the user's real selection on paths that carry no
        explicit role (e.g. a document-upload workspace_sync).
        """
        valid_roles = {"ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"}
        normalized_preferred = (preferred_role or "").strip().upper()
        if normalized_preferred in valid_roles:
            logger.debug(
                "Resolved workspace role {} (source=payload, workspace={})",
                normalized_preferred,
                str(workspace_id)[:12],
            )
            return normalized_preferred

        # (2) Authoritative persisted role cache. _restore_workspace_role checks
        # the in-memory role-cache dicts first, then reads Qdrant only on a cold
        # miss, so this is cheap on the warm path.
        restored_role = await self._restore_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            chat_id=chat_id,
        )
        if restored_role in valid_roles:
            # Keep in-memory context aligned after process restarts/reconnects.
            try:
                from app.workspace.context import context_manager

                context_manager.upsert_workspace(
                    workspace_id,
                    user_id,
                    workspace_role=restored_role,
                )
            except Exception:
                pass
            logger.debug(
                "Resolved workspace role {} (source=restored, workspace={})",
                restored_role,
                str(workspace_id)[:12],
            )
            return restored_role

        # (3) Fall back to the in-memory context only when nothing is persisted.
        try:
            from app.workspace.context import context_manager

            ctx = context_manager.get_context(workspace_id)
            if ctx and ctx.workspace_role:
                ctx_role = (ctx.workspace_role or "").strip().upper()
                if ctx_role in valid_roles:
                    logger.debug(
                        "Resolved workspace role {} (source=context, workspace={})",
                        ctx_role,
                        str(workspace_id)[:12],
                    )
                    return ctx_role
        except Exception:
            pass

        logger.info(
            "Workspace role unresolved; defaulting to ADJUDICATOR (source=fallback, "
            "workspace={}, user={})",
            str(workspace_id)[:12],
            str(user_id)[:12],
        )
        return "ADJUDICATOR"
