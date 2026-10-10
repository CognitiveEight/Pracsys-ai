"""Schedulers, regeneration pipeline, and objective-change merges.

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
from app.ai.usage import capture_usage, current_usage, meter_usage
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

from app.rag.packing import fit_to_budget
from app.socket.event_utils import (
    EventHandler,
    _STRATEGY_ROLE_LABELS_RE,
    OBJECTIVE_CHANGE_REFRESH_FEATURES,
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


from app.prompts.objective_change_prompts import (
    llm_semantic_keep_indexes_prompt,
    llm_merge_against_previous_prompt,
    llm_merge_strengths_against_previous_prompt,
    llm_merge_dated_entries_against_previous_prompt,
    llm_classify_factual_vs_procedural_prompt,
    merge_applicable_laws_with_previous_prompt,
    llm_resolve_answered_missing_info_prompt,
)


class ObjectiveChangeMixin:
    """Schedulers, regeneration pipeline, and objective-change merges."""

    def _schedule_list_of_dates_and_applicable_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
        *,
        log_context: str = "",
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Run list_of_dates → applicable_laws → procedural_timeline without blocking the
        active socket handler, so user_message_request (intelligence chat) can be
        processed while these emit.

        ``deleted_doc_ids`` is forwarded to the dated-feature regeneration
        helpers so they can purge cached entries that came from documents
        the user just removed (see workspace_sync deletion path). When
        omitted (e.g. upload-driven regeneration) the helpers run in their
        existing lenient mode.
        """
        ctx = log_context or ""

        async def _locked_runner() -> None:
            # Queue behind any in-flight generation chain for this workspace so
            # feature panels always reach the frontend in product order.
            lock = self._workspace_pipeline_lock(workspace_id)
            if lock.locked():
                logger.debug(
                    "pipeline lock: waiting{} for workspace {}",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                )
            try:
                async with lock:
                    logger.debug(
                        "pipeline lock: acquired{} for workspace {}",
                        ctx,
                        workspace_id[:12] if workspace_id else "",
                    )
                    await self._run_list_of_dates_and_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=batch_result,
                        log_context=log_context,
                        deleted_doc_ids=deleted_doc_ids,
                    )
            except asyncio.CancelledError:
                logger.error(
                    "Dates/laws pipeline{} CANCELLED (workspace={})",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                )
                raise
            except Exception as pipeline_err:
                # This runner had no handler at all, so anything raised past the
                # per-step traps inside _run_list_of_dates_and_applicable_laws
                # disappeared into an unretrieved task exception.
                logger.opt(exception=pipeline_err).error(
                    "Dates/laws pipeline{} failed: {}", ctx, pipeline_err
                )

        try:
            self._track_background_task(
                asyncio.create_task(_locked_runner()),
                label=f"dates/laws pipeline {workspace_id[:12]}",
            )
            logger.debug(
                "Scheduled list_of_dates + applicable_laws + procedural_timeline + "
                "relevant_parties + case_laws + drafting_suggestions{} for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule timeline/laws task{}: {}",
                ctx,
                rt_err,
            )

    # ------------------------------------------------------------------
    # Objective-change failure bookkeeping
    #
    # An objective change arms six panel loaders before it starts work, and a
    # loader is cleared only by a document_feature_response for the same
    # featureType. So every way this path can stop early must be (a) named in
    # the log, so the cause is identifiable after the fact, and (b) either
    # resumable or terminal. The 2026-09-17 incident was neither: a mid-flight
    # cancellation produced no log line at all and no emit, and the panels span
    # for the rest of the session.
    # ------------------------------------------------------------------

    def _log_objective_regen_failure(
        self, workspace_id: str, reason: str, detail: str = ""
    ) -> None:
        """One structured line + audit event per objective-regen give-up path."""
        logger.error(
            "Objective regen give-up: reason={} workspace={} detail={}",
            reason,
            str(workspace_id)[:12],
            detail,
        )
        try:
            audit_logger.log_event(
                "objective_change_failed",
                {
                    "workspace_id": workspace_id,
                    "reason": reason,
                    "detail": detail[:500],
                },
            )
        except Exception:
            pass

    def _note_pending_objective_change(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        instruction: str,
    ) -> None:
        """Record an objective change that never produced a payload.

        Unlike a dropped emit there is nothing to re-deliver, so the instruction
        itself is kept and re-run once on reconnect. Capped at one retry so a
        request that fails deterministically cannot loop, and stamped so a stale
        entry is discarded rather than replayed hours later.
        """
        if not workspace_id or not (instruction or "").strip():
            return
        try:
            existing = self._pending_objective_changes.get(workspace_id) or {}
            if not isinstance(existing, dict):
                existing = {}
            attempts = int(existing.get("attempts") or 0)
            marker: Dict[str, Any] = {
                "userId": user_id,
                "workspaceRole": (workspace_role or "ADJUDICATOR").strip().upper(),
                "instruction": instruction,
                "featureTypes": list(OBJECTIVE_CHANGE_REFRESH_FEATURES),
                "attempts": attempts,
                "startedAt": time.time(),
            }
            if self._is_stale_run(workspace_id):
                # Interrupted by a workspace_stop_request, not by the socket.
                # Kept (the new objective exists nowhere else) but HELD: only a
                # workspace_resume_request replays it, never a reconnect.
                marker.update({"heldByStop": True, "heldAt": time.time(), "attempts": 0})
                self._pending_objective_changes[workspace_id] = marker
                logger.warning(
                    "Objective change held for workspace_resume_request — its run "
                    "was stopped (workspace={})",
                    str(workspace_id)[:12],
                )
                return
            self._pending_objective_changes[workspace_id] = marker
            logger.warning(
                "Objective change marked for resume after reconnect "
                "(workspace={}, attempts={})",
                str(workspace_id)[:12],
                attempts,
            )
        except Exception as note_err:
            logger.warning(
                "Could not mark objective change for resume (workspace={}): {}",
                str(workspace_id)[:12],
                note_err,
            )

    # Beyond this, a marker is stale: the lawyer has moved on, and silently
    # rewriting their objective from an hour-old instruction would be worse than
    # doing nothing. Mirrors document_reload's _RELOAD_INFLIGHT_TIMEOUT_SECONDS.
    _OBJECTIVE_RESUME_MAX_AGE_SECONDS = 900.0

    async def _recover_workspace_after_hydrate(
        self, workspace_id: str, user_id: Optional[str] = None
    ) -> None:
        """Pick up interrupted work for one workspace after a cold start.

        The reconnect sweep only sees markers still in memory, so a RESTART
        (rather than a bare reconnect) would otherwise leave an undelivered
        payload buried in Qdrant and its panel spinning. This runs once per
        workspace per process, reads the persisted markers, and delivers/resumes
        what it finds.
        """
        if not workspace_id or not self._socket_client:
            return
        if workspace_id in self._recovered_workspaces:
            return
        self._recovered_workspaces.add(workspace_id)
        try:
            from app.workspace.persistence import CacheKinds, restore_state

            pending_features = await restore_state(
                workspace_id=workspace_id,
                kind=CacheKinds.PENDING_FEATURE_DELIVERY,
            )
            if isinstance(pending_features, dict) and pending_features:
                if hasattr(self._pending_feature_deliveries, "hydrate_from_cache"):
                    # hydrate_from_cache, not [] — a plain assignment would
                    # persist straight back to Qdrant for no reason.
                    self._pending_feature_deliveries.hydrate_from_cache(
                        workspace_id, pending_features
                    )
                else:
                    self._pending_feature_deliveries[workspace_id] = pending_features
                delivered = await self._flush_pending_feature_deliveries(
                    workspace_id
                )
                features = (delivered or {}).get(workspace_id) or []
                if features:
                    logger.success(
                        "Re-delivered payload(s) held from before the restart: "
                        "{} (workspace={})",
                        features,
                        str(workspace_id)[:12],
                    )
                if "strategy_objective" in features:
                    # The regeneration had completed; only delivery failed. The
                    # cascade is what is still owed, so run that instead of
                    # re-running the objective.
                    self._clear_pending_objective_change(workspace_id)
                    self._schedule_objective_change_reanalysis(
                        workspace_id=workspace_id,
                        user_id=user_id or "",
                        log_context=(
                            " (after re-delivering a held objective payload)"
                        ),
                    )
                    return

            pending_objective = await restore_state(
                workspace_id=workspace_id,
                kind=CacheKinds.PENDING_OBJECTIVE_CHANGE,
            )
            if isinstance(pending_objective, dict) and pending_objective.get(
                "instruction"
            ):
                if hasattr(self._pending_objective_changes, "hydrate_from_cache"):
                    self._pending_objective_changes.hydrate_from_cache(
                        workspace_id, pending_objective
                    )
                else:
                    self._pending_objective_changes[workspace_id] = pending_objective
                if pending_objective.get("heldByStop"):
                    # Stopped by the lawyer: restored into memory so a
                    # workspace_resume_request can replay it, never auto-resumed.
                    logger.info(
                        "Objective change held by a stop restored (not resumed) "
                        "for workspace {}",
                        str(workspace_id)[:12],
                    )
                    return
                # Detached: the resume runs a full regeneration and must not
                # delay the handler that triggered hydration.
                self._track_background_task(
                    asyncio.create_task(
                        self._resume_pending_objective_changes(
                            only_workspace_id=workspace_id
                        )
                    ),
                    label=f"objective-change resume {str(workspace_id)[:12]}",
                )
        except Exception as recover_err:
            logger.warning(
                "Post-restart recovery failed for workspace {} (non-fatal): {}",
                str(workspace_id)[:12],
                recover_err,
            )

    async def _resume_pending_objective_changes(
        self, *, only_workspace_id: str = ""
    ) -> int:
        """Re-run objective changes that were interrupted before emitting.

        A cancelled regeneration leaves no payload to re-deliver — only the
        instruction. Re-running it is what turns the 2026-09-17 failure (six
        loaders lit, nothing ever arriving) into a completed update. Capped at
        one retry per marker so a deterministically-failing instruction cannot
        loop, and age-limited so a stale request is dropped, not replayed.
        """
        if not self._socket_client:
            return 0
        resumed = 0
        candidates = (
            [only_workspace_id]
            if only_workspace_id
            else [k for k in list(self._pending_objective_changes.keys()) if k]
        )
        for workspace_id in candidates:
            marker = self._pending_objective_changes.get(workspace_id)
            if not isinstance(marker, dict) or not marker.get("instruction"):
                continue
            if marker.get("heldByStop"):
                # The lawyer stopped this run; only workspace_resume_request may
                # replay it (see handlers/run_control.py).
                continue
            attempts = int(marker.get("attempts") or 0)
            started_at = float(marker.get("startedAt") or 0.0)
            age = time.time() - started_at if started_at else 0.0
            features = marker.get("featureTypes") or list(
                OBJECTIVE_CHANGE_REFRESH_FEATURES
            )
            user_id = str(marker.get("userId") or "")
            role = str(marker.get("workspaceRole") or "ADJUDICATOR")

            if attempts >= 1 or age > self._OBJECTIVE_RESUME_MAX_AGE_SECONDS:
                logger.warning(
                    "Dropping interrupted objective change (workspace={}, "
                    "attempts={}, age={:.0f}s) — releasing its loaders",
                    str(workspace_id)[:12],
                    attempts,
                    age,
                )
                self._log_objective_regen_failure(
                    workspace_id,
                    "resume_abandoned",
                    f"attempts={attempts} age={age:.0f}s",
                )
                self._clear_pending_objective_change(workspace_id)
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    list(features),
                    reason="objective_change_resume_abandoned",
                    workspace_role=role,
                )
                continue

            # Count the attempt BEFORE running it, so a second cancellation
            # cannot re-queue the same work indefinitely.
            self._pending_objective_changes[workspace_id] = {
                **marker,
                "attempts": attempts + 1,
            }
            await self._hydrate_workspace_caches(
                workspace_id, user_id=user_id, recover=False
            )
            current_strategy = self._workspace_strategy_payload.get(workspace_id)
            if not isinstance(current_strategy, dict) or not (
                current_strategy.get("caseSummary") or ""
            ).strip():
                logger.warning(
                    "Cannot resume objective change for workspace {} — no cached "
                    "case summary; releasing its loaders",
                    str(workspace_id)[:12],
                )
                self._clear_pending_objective_change(workspace_id)
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    list(features),
                    reason="objective_change_resume_no_summary",
                    workspace_role=role,
                )
                continue

            logger.info(
                "Resuming interrupted objective change for workspace {} "
                "(attempt {})",
                str(workspace_id)[:12],
                attempts + 1,
            )
            # Re-arm the loaders: this connection may be a fresh page load that
            # never saw the original workspace:refresh.
            await self._emit_workspace_refresh(
                workspace_id=workspace_id,
                user_id=user_id,
                reason="objective_change_regeneration_started",
                feature_types=list(features),
            )
            ok = False
            try:
                ok = await self._regenerate_strategy_for_new_objective(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                    user_objective_instruction=str(marker.get("instruction") or ""),
                    current_strategy=current_strategy,
                )
            except asyncio.CancelledError:
                raise
            except Exception as resume_err:
                logger.opt(exception=resume_err).error(
                    "Objective-change resume raised for workspace {}: {}",
                    str(workspace_id)[:12],
                    resume_err,
                )
            if ok:
                resumed += 1
                self._clear_pending_objective_change(workspace_id)
                self._pending_reemit_workspace_id = workspace_id
                self._pending_reemit_user_id = user_id
                self._schedule_objective_change_reanalysis(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    log_context=" (resumed after reconnect)",
                )
                logger.success(
                    "Interrupted objective change completed on resume "
                    "(workspace={})",
                    str(workspace_id)[:12],
                )
            else:
                self._clear_pending_objective_change(workspace_id)
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    list(features),
                    reason="objective_change_resume_failed",
                    workspace_role=role,
                )
        return resumed

    def _clear_pending_objective_change(self, workspace_id: str) -> None:
        """Drop the resume marker once the change has landed."""
        if not workspace_id:
            return
        try:
            if self._pending_objective_changes.get(workspace_id):
                self._pending_objective_changes[workspace_id] = {}
        except Exception:
            pass

    def _workspace_pipeline_lock(self, workspace_id: str) -> asyncio.Lock:
        """Per-workspace lock serializing the feature generation/emit pipeline
        so overlapping triggers cannot interleave their panel emits."""
        return self._workspace_pipeline_locks.setdefault(workspace_id, asyncio.Lock())

    async def _run_list_of_dates_and_applicable_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
        *,
        log_context: str = "",
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """Sequential pipeline tail: list_of_dates → applicable_laws →
        procedural_timeline → relevant_parties → case_laws → drafting.

        Caller must hold the workspace pipeline lock. Each step traps its own
        errors so one failure never skips the remaining panels.
        """
        ctx = log_context or ""
        # On a document DELETION (deleted_doc_ids present) the web-search-derived
        # panels must REFLECT the reduced document set, not accumulate on top of
        # it. Case laws / applicable laws have no per-document linkage, so they
        # can't be purged selectively like list_of_dates — instead we replace the
        # cached set with the freshly-derived one. Uploads (no deleted_doc_ids)
        # keep the merge/accumulate behaviour ("keep what the user has seen +
        # fold in new").
        replace_derived = bool(deleted_doc_ids)
        try:
            await self._generate_and_emit_list_of_dates(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                deleted_doc_ids=deleted_doc_ids,
            )
        except Exception as lod_err:
            logger.warning(
                "List of dates (background{}) failed: {}",
                ctx,
                lod_err,
            )
        try:
            logger.info(
                "Applicable laws: invoking (background{}) for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
            await self._generate_and_emit_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                merge_with_previous=not replace_derived,
            )
        except Exception as laws_err:
            logger.warning(
                "Applicable laws (background{}) failed: {}",
                ctx,
                laws_err,
            )
        # Order (per product): applicable_laws → upcoming dates
        # (procedural_timeline) → relevant_parties → case_laws → drafting.
        try:
            logger.info(
                "Procedural timeline: invoking (background{}) for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
            await self._generate_and_emit_procedural_timeline(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                deleted_doc_ids=deleted_doc_ids,
            )
        except Exception as pt_err:
            logger.warning(
                "Procedural timeline (background{}) failed: {}",
                ctx,
                pt_err,
            )
        try:
            logger.info(
                "Relevant parties: invoking (background{}) for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
            await self._generate_and_emit_relevant_parties(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
        except Exception as parties_err:
            logger.warning(
                "Relevant parties (background{}) failed: {}",
                ctx,
                parties_err,
            )
        try:
            logger.info(
                "Case laws: invoking (background{}) for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
            await self._generate_and_emit_case_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                merge_with_previous=not replace_derived,
            )
        except Exception as case_err:
            logger.warning(
                "Case laws (background{}) failed: {}",
                ctx,
                case_err,
            )
            # Never leave the panel stuck on "Analysis is in progress":
            # emit the cached-or-empty payload as a terminal state.
            if self._socket_client:
                fallback = self._workspace_case_laws_payload.get(
                    workspace_id
                ) or {"caseLaws": []}
                try:
                    await self._emit_document_feature(
                        feature_type="case_laws",
                        workspace_id=workspace_id,
                        user_id=user_id,
                        payload=fallback,
                        usage=None,
                    )
                except Exception as emit_err:
                    logger.warning(
                        "Case laws fallback emit failed{}: {}", ctx, emit_err
                    )
        try:
            logger.info(
                "Drafting suggestions: invoking (background{}) for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
        except Exception as draft_err:
            logger.warning(
                "Drafting suggestions (background{}) failed: {}",
                ctx,
                draft_err,
            )

    def _schedule_applicable_laws_only(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        *,
        log_context: str = "",
    ) -> None:
        ctx = log_context or ""

        async def _runner() -> None:
            try:
                # Queue behind any in-flight generation chain so this single
                # panel refresh cannot interleave a live pipeline's emits.
                async with self._workspace_pipeline_lock(workspace_id):
                    await self._generate_and_emit_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                    )
            except Exception as laws_err:
                logger.warning(
                    "Applicable laws only (background{}) failed: {}",
                    ctx,
                    laws_err,
                )

        try:
            self._track_background_task(
                asyncio.create_task(_runner()),
                label=f"applicable_laws only {workspace_id[:12]}",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule applicable_laws task{}: {}",
                ctx,
                rt_err,
            )

    async def _emit_workspace_refresh(
        self,
        workspace_id: str,
        user_id: Optional[str] = None,
        *,
        reason: str = "ai_update_started",
        feature_types: Optional[List[str]] = None,
    ) -> None:
        """
        Emit FE loader hint before heavy AI refresh/regeneration starts.
        """
        if not self._socket_client or not workspace_id:
            return
        features = feature_types or [
            "strategy_objective",
            "analysis_report",
            "list_of_dates",
            "applicable_laws",
            "procedural_timeline",
        ]
        # A correlation id for the arm/release pair. The frontend may ignore it;
        # its value here is that the log can be read end to end — which refresh
        # armed which loaders, and whether anything ever released them. The
        # 2026-09-17 incident had to be reconstructed from the ABSENCE of lines.
        refresh_id = uuid.uuid4().hex[:12]
        payload: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "featureTypes": features,
            "reason": reason,
            "refreshId": refresh_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if user_id:
            payload["userId"] = user_id
        try:
            delivered = await self._socket_client.emit("workspace:refresh", payload)
            logger.info(
                "workspace:refresh {} (rid={}, reason={}, features={}) "
                "for workspace {}",
                "emitted" if delivered else "NOT delivered",
                refresh_id,
                reason,
                features,
                workspace_id[:12] if workspace_id else "",
            )
        except Exception as e:
            logger.warning("workspace:refresh emit failed: {}", e)

    @staticmethod
    def _canonical_text_key(text: Any) -> str:
        s = str(text or "").strip().lower()
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _merge_unique_string_lists(old_list: Any, new_list: Any, limit: int = 12) -> List[str]:
        out: List[str] = []
        seen = set()
        for src in (old_list or [], new_list or []):
            if not isinstance(src, list):
                continue
            for item in src:
                text = str(item or "").strip()
                if not text:
                    continue
                key = ObjectiveChangeMixin._canonical_text_key(text)
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
                if len(out) >= limit:
                    return out
        return out

    def _merge_analysis_report_for_objective_change(
        self, old_report: Dict[str, Any], new_report: Dict[str, Any]
    ) -> Dict[str, Any]:
        old_report = old_report or {}
        new_report = new_report or {}
        merged = dict(old_report)

        for key, limit in (
            ("missingInfo", 10),
            ("weaknesses", 8),
            ("risks", 8),
            ("relevantJurisdiction", 6),
            ("pendingActions", 6),
            ("nextSteps", 6),
            ("suggestedDrafts", 6),
        ):
            merged[key] = self._merge_unique_string_lists(
                old_report.get(key), new_report.get(key), limit=limit
            )

        old_strengths = old_report.get("strengths") or []
        new_strengths = new_report.get("strengths") or []
        strengths_map: Dict[str, Dict[str, Any]] = {}
        for s in (old_strengths + new_strengths):
            if not isinstance(s, dict):
                continue
            point = str(s.get("point") or "").strip()
            if not point:
                continue
            key = self._canonical_text_key(point)
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            prev = strengths_map.get(key)
            if prev:
                prev["laws"] = self._merge_unique_string_lists(
                    prev.get("laws"), laws, limit=6
                )
            else:
                strengths_map[key] = {
                    "point": point,
                    "laws": self._merge_unique_string_lists([], laws, limit=6),
                }
        merged["strengths"] = list(strengths_map.values())[:8]

        merged["caseResolutionScore"] = int(
            new_report.get("caseResolutionScore")
            if new_report.get("caseResolutionScore") is not None
            else old_report.get("caseResolutionScore") or 0
        )
        merged["qualityScoreExplanation"] = (
            str(new_report.get("qualityScoreExplanation") or "").strip()
            or str(old_report.get("qualityScoreExplanation") or "").strip()
        )
        merged["reportQualityScoreExplanation"] = (
            str(new_report.get("reportQualityScoreExplanation") or "").strip()
            or str(old_report.get("reportQualityScoreExplanation") or "").strip()
        )
        return merged

    def _merge_applicable_laws_for_objective_change(
        self, old_payload: Dict[str, Any], new_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        def _law_key(law: Dict[str, Any]) -> str:
            act = self._canonical_text_key(law.get("act") or "")
            sec_raw = str(law.get("section") or "").strip().lower()
            sec_raw = re.sub(r"\b(section|sec\.?)\b", " ", sec_raw)
            sec_raw = re.sub(r"[^a-z0-9]", " ", sec_raw)
            sec = re.sub(r"\s+", " ", sec_raw).strip()
            return f"{act}::{sec}"

        old_laws = (old_payload or {}).get("laws") or []
        new_laws = (new_payload or {}).get("laws") or []
        merged_by_key: Dict[str, Dict[str, Any]] = {}

        for law in old_laws:
            if not isinstance(law, dict):
                continue
            act = str(law.get("act") or "").strip()
            sec = str(law.get("section") or "").strip()
            if not act or not sec:
                continue
            merged_by_key[_law_key(law)] = dict(law)

        for law in new_laws:
            if not isinstance(law, dict):
                continue
            act = str(law.get("act") or "").strip()
            sec = str(law.get("section") or "").strip()
            if not act or not sec:
                continue
            k = _law_key(law)
            if k in merged_by_key:
                old = merged_by_key[k]
                merged_by_key[k] = {
                    **old,
                    **law,
                    "description": str(law.get("description") or old.get("description") or "").strip(),
                    "relevance": str(law.get("relevance") or old.get("relevance") or "Medium").strip(),
                }
            else:
                merged_by_key[k] = dict(law)

        merged_laws = list(merged_by_key.values())[:12]
        return {"laws": merged_laws}

    def _merge_list_of_dates_for_objective_change(
        self, old_dates: List[Dict[str, Any]], new_dates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for item in (old_dates or []) + (new_dates or []):
            if not isinstance(item, dict):
                continue
            date_v = str(item.get("date") or "").strip()
            title_v = self._canonical_text_key(item.get("title") or "")
            if not (date_v or title_v):
                continue
            # Dedupe by date + normalized title (ignore source doc IDs to avoid repeats across refreshes)
            key = f"{date_v}::{title_v}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged[:200]

    async def _llm_semantic_keep_indexes(
        self,
        items: List[str],
        *,
        item_type: str,
        max_keep: int,
    ) -> Optional[List[int]]:
        if not items:
            return []
        try:
            from app.ai.gemini_client import gemini_client

            numbered = "\n".join(f"{i+1}. {v}" for i, v in enumerate(items))
            prompt = llm_semantic_keep_indexes_prompt(item_type=item_type, max_keep=max_keep, numbered=numbered)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            keep = parsed.get("keepIndexes") if isinstance(parsed, dict) else None
            if not isinstance(keep, list):
                return None
            cleaned: List[int] = []
            seen = set()
            for idx in keep:
                try:
                    n = int(idx)
                except Exception:
                    continue
                if n < 1 or n > len(items) or n in seen:
                    continue
                seen.add(n)
                cleaned.append(n)
                if len(cleaned) >= max_keep:
                    break
            return cleaned or None
        except Exception as e:
            logger.warning("LLM semantic dedupe failed for {}: {}", item_type, e)
            return None

    async def _llm_semantic_dedupe_strings(
        self, items: List[str], *, item_type: str, max_keep: int
    ) -> List[str]:
        if not items:
            return []
        keep = await self._llm_semantic_keep_indexes(
            items, item_type=item_type, max_keep=max_keep
        )
        if not keep:
            return items[:max_keep]
        out = [items[i - 1] for i in keep if 1 <= i <= len(items)]
        return out[:max_keep]

    async def _llm_merge_against_previous(
        self,
        prev_items: List[str],
        new_items: List[str],
        *,
        item_type: str,
        max_keep: int,
    ) -> List[str]:
        """Single Gemini call that merges a fresh-regeneration list with the
        previously-emitted list, preserving prev verbatim and adding only
        genuinely new items.

        Replaces the older "union + flat dedupe" pattern. The flat dedup didn't
        know which items were old vs new, so when the LLM saw a paraphrased
        pair it sometimes kept the new wording and dropped the old one — which
        the user has been seeing in the UI for hours and now suddenly
        disappears. This call gives the LLM the prev/new split explicitly and
        instructs it to keep prev verbatim, drop new items that are paraphrases
        of any prev item, and append the rest.

        Returns the merged list. Falls back to ``prev_items + (non-empty new
        items not exact-duplicates of prev) [:max_keep]`` on any LLM failure.
        """
        prev_clean = [s.strip() for s in (prev_items or []) if isinstance(s, str) and s.strip()]
        new_clean = [s.strip() for s in (new_items or []) if isinstance(s, str) and s.strip()]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            # Nothing to merge against — just dedupe within new.
            return await self._llm_semantic_dedupe_strings(
                new_clean, item_type=item_type, max_keep=max_keep,
            )
        if not new_clean:
            return prev_clean[:max_keep]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_clean))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_clean))
            prompt = llm_merge_against_previous_prompt(item_type=item_type, max_keep=max_keep, numbered_new=numbered_new, numbered_prev=numbered_prev)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            merged_raw = parsed.get("merged") if isinstance(parsed, dict) else None
            if not isinstance(merged_raw, list) or not merged_raw:
                raise ValueError(f"missing or empty 'merged' field: {parsed!r}")
            merged: List[str] = []
            seen_lower: set = set()
            for x in merged_raw:
                s = str(x).strip()
                if not s:
                    continue
                key = s.lower()
                if key in seen_lower:
                    continue
                seen_lower.add(key)
                merged.append(s)
                if len(merged) >= max_keep:
                    break
            # Sanity: every prev item should be present (the LLM was instructed
            # to preserve them verbatim). If the LLM dropped one, splice it
            # back to honour the contract; the user's already seen it.
            for prev_item in prev_clean:
                if prev_item.lower() not in seen_lower:
                    if len(merged) < max_keep:
                        merged.append(prev_item)
                        seen_lower.add(prev_item.lower())
            logger.info(
                "LLM merge ({}): prev={} new={}  merged={}",
                item_type, len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous failed for {}: {}; falling back to "
                "prev + non-duplicate-by-string new",
                item_type, e,
            )
            # Conservative fallback: keep prev verbatim, append new items that
            # aren't exact case-insensitive matches of any prev item.
            seen_lower = {p.lower() for p in prev_clean}
            fallback = list(prev_clean)
            for n in new_clean:
                if n.lower() in seen_lower:
                    continue
                seen_lower.add(n.lower())
                fallback.append(n)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_resolve_answered_missing_info(
        self,
        workspace_id: str,
        merged_missing: List[str],
        fresh_missing: List[str],
        *,
        log_context: str = "",
    ) -> List[str]:
        """Post-merge resolution pass: drop merged missingInfo items that are
        now ANSWERED by the uploaded documents (the grow-only merge never
        removes items, so an item asking for e.g. a cheque number would stay
        forever after the user uploads the cheque).

        Fail-open: any error returns the input list unchanged. Never drops
        items the fresh regeneration still flags as missing, nor entries the
        user added via added_missing_information.
        """
        items = [s for s in (merged_missing or []) if isinstance(s, str) and s.strip()]
        if not items:
            return merged_missing
        try:
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            # Guard 1: the fresh generation just re-verified these against ALL
            # documents (missing-info verification step) and still flagged
            # them — never contradict it.
            fresh_lower = {
                s.strip().lower() for s in (fresh_missing or []) if isinstance(s, str)
            }
            # Guard 2: user-added entries are only removable by the user via
            # analysis_report_feedback.
            user_added = self._workspace_user_added_missing_info.get(workspace_id) or []

            def _protected(item: str) -> bool:
                if item.strip().lower() in fresh_lower:
                    return True
                return any(
                    self._match_rejected_value(item, ua)
                    for ua in user_added
                    if isinstance(ua, str) and ua.strip()
                )

            candidate_idx = {i for i, s in enumerate(items) if not _protected(s)}
            if not candidate_idx:
                return items

            cached = self._workspace_case_entities_cache.get(workspace_id)
            entities = cached[1] if cached and isinstance(cached[1], dict) else {}
            entities_block = (
                json.dumps(entities, indent=2, ensure_ascii=False)
                if entities
                else "(none)"
            )
            docs = await vector_store.get_all_documents(workspace_id=workspace_id)
            with_content = [d for d in docs if (d.get("content") or "").strip()]
            budget = 40000
            per_doc = budget // max(1, len(with_content))
            doc_parts = []
            for i, d in enumerate(with_content, 1):
                content = fit_to_budget((d.get("content") or "").strip(), per_doc, "general")
                doc_parts.append(
                    f"--- Document {i} (id: {d.get('document_id') or ''}) ---\n{content}"
                )
            document_content = (
                "\n\n".join(doc_parts) or "No document content available."
            )

            numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(items))
            prompt = llm_resolve_answered_missing_info_prompt(
                numbered_items=numbered,
                entities_block=entities_block,
                document_content=document_content,
            )
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=4000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            answered = parsed.get("answered") if isinstance(parsed, dict) else None
            if not isinstance(answered, list):
                return items
            drop: Set[int] = set()
            for a in answered:
                if not isinstance(a, dict):
                    continue
                try:
                    n = int(a.get("index"))
                except (TypeError, ValueError):
                    continue
                # Evidence quote is mandatory — an index without evidence is
                # not a confirmed resolution.
                if not str(a.get("evidence") or "").strip():
                    continue
                if 1 <= n <= len(items) and (n - 1) in candidate_idx:
                    drop.add(n - 1)
            if drop:
                logger.info(
                    "Analysis missingInfo resolution{}: dropping {} answered item(s): {}",
                    log_context,
                    len(drop),
                    [items[i] for i in sorted(drop)],
                )
            return [s for i, s in enumerate(items) if i not in drop]
        except Exception as e:
            logger.warning(
                "missingInfo resolution pass failed{}: {}; keeping grow-only merge result",
                log_context,
                e,
            )
            return merged_missing

    async def _llm_merge_strengths_against_previous(
        self,
        prev_strengths: List[Dict[str, Any]],
        new_strengths: List[Dict[str, Any]],
        *,
        max_keep: int = 8,
    ) -> List[Dict[str, Any]]:
        """Strengths variant of `_llm_merge_against_previous`. Operates on
        ``[{"point": str, "laws": [str, ...]}]`` and asks Gemini to keep prev
        verbatim then append only genuinely-new strengths from the regenerated
        list. Returns the merged list with prev objects preserved as-is.
        """
        prev_clean = [
            s for s in (prev_strengths or [])
            if isinstance(s, dict) and str(s.get("point") or "").strip()
        ]
        new_clean = [
            s for s in (new_strengths or [])
            if isinstance(s, dict) and str(s.get("point") or "").strip()
        ]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            return await self._llm_semantic_dedupe_strengths(new_clean, max_keep=max_keep)
        if not new_clean:
            return prev_clean[:max_keep]

        def _label(s: Dict[str, Any]) -> str:
            point = str(s.get("point") or "").strip()
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            laws_s = ", ".join(str(x).strip() for x in laws if str(x).strip())
            return point if not laws_s else f"{point} | laws: {laws_s}"

        prev_labels = [_label(s) for s in prev_clean]
        new_labels = [_label(s) for s in new_clean]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = llm_merge_strengths_against_previous_prompt(max_keep=max_keep, numbered_new=numbered_new, numbered_prev=numbered_prev)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev_indexes: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_clean):
                    merged.append(prev_clean[idx - 1])
                    included_prev_indexes.add(idx)
                elif src == "new" and 1 <= idx <= len(new_clean):
                    merged.append(new_clean[idx - 1])
                if len(merged) >= max_keep:
                    break

            # Splice back any prev items the LLM dropped — they were already
            # shown to the user; honour the contract.
            for i, prev_item in enumerate(prev_clean, start=1):
                if i not in included_prev_indexes and len(merged) < max_keep:
                    merged.append(prev_item)

            logger.info(
                "LLM merge (strengths): prev={} new={}  merged={}",
                len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous (strengths) failed: {}; "
                "falling back to prev + non-duplicate-by-point new",
                e,
            )
            seen_points = {str(s.get("point") or "").strip().lower() for s in prev_clean}
            fallback = list(prev_clean)
            for s in new_clean:
                pkey = str(s.get("point") or "").strip().lower()
                if pkey in seen_points:
                    continue
                seen_points.add(pkey)
                fallback.append(s)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_semantic_dedupe_strengths(
        self, strengths: List[Dict[str, Any]], *, max_keep: int = 8
    ) -> List[Dict[str, Any]]:
        if not strengths:
            return []
        lines = []
        valid_strengths: List[Dict[str, Any]] = []
        for s in strengths:
            if not isinstance(s, dict):
                continue
            point = str(s.get("point") or "").strip()
            if not point:
                continue
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            laws_s = ", ".join(str(x).strip() for x in laws if str(x).strip())
            lines.append(point if not laws_s else f"{point} | laws: {laws_s}")
            valid_strengths.append(s)
        if not valid_strengths:
            return []
        keep = await self._llm_semantic_keep_indexes(
            lines, item_type="analysis strengths", max_keep=max_keep
        )
        if not keep:
            return valid_strengths[:max_keep]
        return [valid_strengths[i - 1] for i in keep if 1 <= i <= len(valid_strengths)][
            :max_keep
        ]

    async def _llm_merge_dated_entries_against_previous(
        self,
        prev_entries: List[Dict[str, Any]],
        new_entries: List[Dict[str, Any]],
        *,
        item_type: str,
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
        max_keep: int = 200,
    ) -> List[Dict[str, Any]]:
        """LLM-only prev-vs-new merge for dated entries (list_of_dates,
        procedural_timeline). Keeps every prev entry, appends new entries only
        when the LLM judges them a genuinely different event from every prev
        entry on the same date.

        Uses index-based references so the original entry dicts (with
        sourceDocId, pageNumber, eventType, etc.) are preserved verbatim.
        """
        prev_clean = [
            e for e in (prev_entries or [])
            if isinstance(e, dict) and str(e.get(date_field) or "").strip()
        ]
        new_clean = [
            e for e in (new_entries or [])
            if isinstance(e, dict) and str(e.get(date_field) or "").strip()
        ]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            return new_clean[:max_keep]
        if not new_clean:
            return prev_clean[:max_keep]

        def _label(e: Dict[str, Any]) -> str:
            d = str(e.get(date_field) or "").strip()
            t = str(e.get(title_field) or "").strip()
            desc = str(e.get(description_field) or "").strip()
            parts = [d, t]
            if desc:
                parts.append(desc)
            return " — ".join(p for p in parts if p)

        prev_labels = [_label(e) for e in prev_clean]
        new_labels = [_label(e) for e in new_clean]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = llm_merge_dated_entries_against_previous_prompt(item_type=item_type, max_keep=max_keep, numbered_new=numbered_new, numbered_prev=numbered_prev)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_clean):
                    merged.append(prev_clean[idx - 1])
                    included_prev.add(idx)
                elif src == "new" and 1 <= idx <= len(new_clean):
                    merged.append(new_clean[idx - 1])
                if len(merged) >= max_keep:
                    break

            # Splice back any prev entries the LLM dropped — they were already
            # shown to the user; honour the contract.
            for i, prev_item in enumerate(prev_clean, start=1):
                if i not in included_prev and len(merged) < max_keep:
                    merged.append(prev_item)

            logger.info(
                "LLM merge ({}): prev={} new={}  merged={}",
                item_type, len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous ({}) failed: {}; "
                "falling back to prev + new minus exact (date,title) matches",
                item_type, e,
            )
            seen_keys = {
                (
                    str(p.get(date_field) or "").strip(),
                    str(p.get(title_field) or "").strip().lower(),
                )
                for p in prev_clean
            }
            fallback = list(prev_clean)
            for n in new_clean:
                key = (
                    str(n.get(date_field) or "").strip(),
                    str(n.get(title_field) or "").strip().lower(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                fallback.append(n)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_classify_factual_vs_procedural(
        self,
        entries: List[Dict[str, Any]],
        *,
        item_type: str = "list of dates",
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
    ) -> List[Dict[str, Any]]:
        """LLM-based filter that drops procedural / lawyer-side entries from a
        factual-events list, keeping only entries that describe what happened
        in the real world to/between the parties.

        Replaces the earlier hardcoded title-keyword regex. The LLM is given
        a numbered list of entries (date + title + description) and asked to
        classify each as 'factual' or 'procedural'. We keep only the
        'factual' indexes. Generalises across any case type (matrimonial,
        cyber-fraud, immigration, property, employment, etc.) and any
        procedural phrasing the upstream LLM might emit.

        On any LLM failure (no API key, parse error, count mismatch), returns
        the input unchanged — the cheap deterministic filters upstream
        (eventType, future-date, YYYY-01-01) still applied, so falling back
        to the unfiltered list is safe.
        """
        if not entries:
            return []
        # Cheap exit: nothing to classify if all entries are already trivially
        # factual by their normalized eventType (Incident / Agreement /
        # Payment / Communication). The LLM only needs to weigh in on
        # ambiguous types like "Other".
        ambiguous_indexes = [
            i for i, e in enumerate(entries)
            if isinstance(e, dict)
            and str(e.get("eventType") or "").strip() in ("Other", "")
        ]
        if not ambiguous_indexes:
            return entries

        try:
            from app.ai.gemini_client import gemini_client

            lines: List[str] = []
            for i, e in enumerate(entries):
                d = str(e.get(date_field) or "").strip()
                t = str(e.get(title_field) or "").strip()
                desc = str(e.get(description_field) or "").strip()
                lines.append(f"{i+1}. [{d}] {t} — {desc}")
            numbered = "\n".join(lines)

            prompt = llm_classify_factual_vs_procedural_prompt(entries=entries, numbered=numbered)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            classifications = parsed.get("classifications") if isinstance(parsed, dict) else None
            if not isinstance(classifications, list):
                logger.warning(
                    "{}: classifier response missing 'classifications' list (got {}); "
                    "keeping all entries.",
                    item_type, type(classifications).__name__,
                )
                return entries

            kept: List[Dict[str, Any]] = []
            for i, entry in enumerate(entries):
                if i >= len(classifications):
                    # LLM returned fewer items than expected — keep the rest
                    # rather than discard.
                    kept.append(entry)
                    continue
                verdict = str(classifications[i] or "").strip().lower()
                if verdict == "procedural":
                    logger.debug(
                        "List of dates: LLM classifier dropped procedural entry "
                        "(eventType={!r}, date={})",
                        str(entry.get("eventType") or ""),
                        str(entry.get(date_field) or ""),
                    )
                    continue
                # "factual" or any unknown verdict → keep (conservative).
                kept.append(entry)
            return kept
        except Exception as e:
            logger.warning(
                "{}: factual-vs-procedural classifier failed: {}; keeping all entries.",
                item_type, e,
            )
            return entries

    async def _dedupe_dated_entries_semantically(
        self,
        entries: List[Dict[str, Any]],
        *,
        item_type: str,
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
    ) -> List[Dict[str, Any]]:
        """Drop near-duplicate dated entries that share the same date.

        Groups by ``date_field`` and, for each date with ≥2 entries, asks the
        same LLM-semantic dedupe used for analysis_report to keep only the
        meaning-distinct ones. Entries on different dates are never compared,
        so two genuinely separate events on the same day with different titles
        are preserved.

        Used for list_of_dates and procedural_timeline regenerations after a
        workspace_sync — without it, regenerating from edited transcripts
        produces near-duplicate entries with slightly different titles for the
        same date.
        """
        if not entries:
            return []

        grouped: Dict[str, List[int]] = {}
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            d = str(entry.get(date_field) or "").strip()
            if not d:
                continue
            grouped.setdefault(d, []).append(idx)

        # Indexes that survive dedup. Start with everything, then remove
        # within each duplicate group.
        kept_indexes: set[int] = set(range(len(entries)))

        for date_value, idxs in grouped.items():
            if len(idxs) < 2:
                continue

            def _label_for(entry: Dict[str, Any]) -> str:
                title = str(entry.get(title_field) or "").strip()
                desc = str(entry.get(description_field) or "").strip()
                return f"{title} — {desc}" if title and desc else (title or desc)

            labels = [_label_for(entries[i]) for i in idxs]
            if not all(labels):
                continue

            kept_label_indexes = await self._llm_semantic_keep_indexes(
                labels, item_type=f"{item_type} on {date_value}", max_keep=len(idxs)
            )
            if not kept_label_indexes:
                continue  # Dedupe failed; keep all on this date.

            keep_set_for_date = {idxs[i - 1] for i in kept_label_indexes if 1 <= i <= len(idxs)}
            for original_idx in idxs:
                if original_idx not in keep_set_for_date:
                    kept_indexes.discard(original_idx)

        if len(kept_indexes) == len(entries):
            return entries

        before = len(entries)
        deduped = [entries[i] for i in range(len(entries)) if i in kept_indexes]
        logger.info(
            "Dedupe ({}): {} entries  {} after per-date semantic dedup",
            item_type, before, len(deduped),
        )
        return deduped

    def _law_signature(self, law: Dict[str, Any]) -> str:
        """Stable identity for an applicable_laws entry: lowercase act+section."""
        act = str(law.get("act") or "").strip().lower()
        section = str(law.get("section") or "").strip().lower()
        # Normalise away whitespace and punctuation noise.
        act = re.sub(r"\s+", " ", act)
        section = re.sub(r"\s+", " ", section)
        return f"{act}|{section}"

    async def _merge_applicable_laws_with_previous(
        self,
        workspace_id: str,
        new_payload: Dict[str, Any],
        *,
        max_laws: int = 12,
    ) -> Dict[str, Any]:
        """LLM-only prev-vs-new merge for applicable_laws.

        Same explicit prev/new split as analysis_report and list_of_dates: the
        LLM sees the previously-cached laws and the freshly-regenerated laws
        with labels, returns merged indexes, and we map back to the original
        dict entries (preserving act/section/description/relevance verbatim).
        Caps at ``max_laws`` (existing 12 limit) and re-stamps stable law_N ids.
        """
        new_laws_raw = new_payload.get("laws") if isinstance(new_payload, dict) else None
        new_laws: List[Dict[str, Any]] = (
            [law for law in new_laws_raw if isinstance(law, dict)]
            if isinstance(new_laws_raw, list)
            else []
        )

        prev_payload = self._workspace_applicable_laws_payload.get(workspace_id)
        prev_laws: List[Dict[str, Any]] = []
        if isinstance(prev_payload, dict):
            prev_laws_raw = prev_payload.get("laws")
            if isinstance(prev_laws_raw, list):
                prev_laws = [law for law in prev_laws_raw if isinstance(law, dict)]

        # No prior cache → just take new (cap + stamp ids).
        if not prev_laws:
            ordered = new_laws[:max_laws]
            for i, law in enumerate(ordered):
                law["id"] = f"law_{i+1}"
            return {"laws": ordered}
        # No new laws → keep prev verbatim.
        if not new_laws:
            ordered = prev_laws[:max_laws]
            for i, law in enumerate(ordered):
                law["id"] = f"law_{i+1}"
            return {"laws": ordered}

        def _label(law: Dict[str, Any]) -> str:
            act = str(law.get("act") or "").strip()
            section = str(law.get("section") or "").strip()
            desc = str(law.get("description") or "").strip()
            return f"{act} {section} — {desc}".strip(" —")

        prev_labels = [_label(law) for law in prev_laws]
        new_labels = [_label(law) for law in new_laws]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = merge_applicable_laws_with_previous_prompt(max_laws=max_laws, numbered_new=numbered_new, numbered_prev=numbered_prev)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_laws):
                    merged.append(dict(prev_laws[idx - 1]))
                    included_prev.add(idx)
                elif src == "new" and 1 <= idx <= len(new_laws):
                    merged.append(dict(new_laws[idx - 1]))
                if len(merged) >= max_laws:
                    break

            # Splice back any prev entries the LLM dropped.
            for i, prev_law in enumerate(prev_laws, start=1):
                if i not in included_prev and len(merged) < max_laws:
                    merged.append(dict(prev_law))

            logger.info(
                "LLM merge (applicable laws): prev={} new={}  merged={}",
                len(prev_laws), len(new_laws), len(merged),
            )
        except Exception as e:
            logger.warning(
                "LLM merge against previous (applicable laws) failed: {}; "
                "falling back to prev + non-duplicate-by-(act,section) new",
                e,
            )
            merged = [dict(p) for p in prev_laws]
            seen_sigs = {self._law_signature(p) for p in prev_laws if self._law_signature(p)}
            for n in new_laws:
                sig = self._law_signature(n)
                if not sig or sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                merged.append(dict(n))
                if len(merged) >= max_laws:
                    break

        for i, law in enumerate(merged[:max_laws]):
            law["id"] = f"law_{i+1}"
        return {"laws": merged[:max_laws]}

    # Analysis-report text sections that get {id, text, isMatched} tagging, and
    # the currentState key + rejected-memory section each maps to.
    _ANALYSIS_TAGGED_SECTIONS = (
        # (report_field, currentState_key, rejected_section_or_None)
        ("missingInfo", "missingInfo", "missingInfo"),
        ("weaknesses", "weaknesses", "riskOrWeakness"),
        ("risks", "risks", "riskOrWeakness"),
        ("relevantJurisdiction", "relevantJurisdiction", None),
    )

    async def _tag_analysis_report_against_current_state(
        self,
        workspace_id: str,
        analysis_report: Dict[str, Any],
        *,
        log_context: str = "",
    ) -> Dict[str, Any]:
        """Tag the four analysis text sections against the frontend's
        currentState so each item is emitted as ``{id, text, isMatched}``.

        Replaces the old grow-only merge (`_merge_analysis_report_with_previous`)
        and answered-item drop (`_llm_resolve_answered_missing_info`): the fresh
        report is authoritative for content, and reconciliation only stamps
        identity (matched → carry prior id; new → mint id; prev-only → dropped).
        Rejected-item memory is still honoured. Runs on every emit (create and
        sync); with no currentState every item is emitted new with a minted id
        so the shape is uniform from the first emit.
        """
        if not isinstance(analysis_report, dict):
            return analysis_report
        current_state = self._workspace_current_state_payload.get(workspace_id) or {}
        if not isinstance(current_state, dict):
            current_state = {}
        # Gaps created by a document the user deleted are exempt from
        # rejected-item memory. A user often dismisses a missingInfo item
        # *because* a document answered it; once that document is withdrawn the
        # gap is real again, and the permanent suppression below would hide it
        # forever. Derived from the same ledger as the deterministic append in
        # _apply_removed_evidence_missing_info, so the texts match exactly.
        try:
            removed_evidence_texts = {
                item["text"]
                for item in self._removed_evidence_missing_info_items(workspace_id)
            }
        except Exception as ledger_err:
            logger.debug(
                "Removed-evidence exemption unavailable (workspace={}): {}",
                workspace_id,
                ledger_err,
            )
            removed_evidence_texts = set()
        for report_field, cs_key, reject_section in self._ANALYSIS_TAGGED_SECTIONS:
            fresh = _coerce_analysis_text_list(analysis_report.get(report_field))
            # Honour rejected-item memory: never re-surface a dismissed item.
            if reject_section:
                fresh = [
                    s
                    for s in fresh
                    if s in removed_evidence_texts
                    or not self._is_previously_rejected_analysis_item(
                        workspace_id=workspace_id,
                        section=reject_section,
                        candidate=s,
                    )
                ]
            prev_items = current_state.get(cs_key) if current_state else None
            analysis_report[report_field] = await self._reconcile_against_current_state(
                fresh=fresh,
                prev_items=prev_items if isinstance(prev_items, list) else None,
                section=report_field,
            )
        # The lawyer's re-worded items (missing_information_edited) win over the
        # regenerated wording, and keep their ids.
        analysis_report["missingInfo"] = self._apply_missing_info_edits(
            workspace_id, analysis_report.get("missingInfo")
        )
        return analysis_report

    async def _merge_analysis_report_with_previous(
        self,
        workspace_id: str,
        workspace_role: str,
        new_analysis: Dict[str, Any],
        *,
        log_context: str = "",
    ) -> Dict[str, Any]:
        """Merge a freshly-regenerated analysis report against the cached
        previous version: filter rejected items, union prev + new, run LLM
        semantic dedup per list field, preserve identity + score, regenerate
        explanations.

        Used after workspace_sync regenerations (e.g. transcript edit + reupload)
        so duplicates from the previous report don't surface again. Mirrors the
        merge pattern already proven on the strategic-objective-change path
        (`_run_objective_change_incremental_refresh`).

        Returns the merged report. Caller is responsible for caching + emitting.
        """
        ctx = log_context or " (sync merge)"
        prev_raw = self._workspace_analysis_payload.get(workspace_id)
        prev_analysis: Dict[str, Any] = (
            dict(prev_raw) if isinstance(prev_raw, dict) else {}
        )

        # No prior cached version → nothing to dedupe against; first emit.
        if not prev_analysis:
            return new_analysis

        merged: Dict[str, Any] = dict(new_analysis)

        def _str_list(items: Any) -> List[str]:
            return _coerce_analysis_text_list(items)

        def _filter_rejected(section: str, items: List[str]) -> List[str]:
            if not items:
                return items
            return [
                s
                for s in items
                if not self._is_previously_rejected_analysis_item(
                    workspace_id=workspace_id, section=section, candidate=s,
                )
            ]

        new_missing_filtered = _filter_rejected(
            "missingInfo", _str_list(new_analysis.get("missingInfo"))
        )
        new_weak_filtered = _filter_rejected(
            "riskOrWeakness", _str_list(new_analysis.get("weaknesses"))
        )
        new_risks_filtered = _filter_rejected(
            "riskOrWeakness", _str_list(new_analysis.get("risks"))
        )

        # All list fields go through the SAME prev-vs-new LLM merge so the
        # model sees the explicit "this is what the user already saw" / "this
        # is the freshly regenerated candidate" split. Replaces the older
        # "union + flat dedup" which lost that signal and sometimes kept the
        # newly-rephrased copy while dropping the original the user has been
        # seeing in the UI.
        merged["missingInfo"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("missingInfo")),
            new_missing_filtered,
            item_type="missing information items",
            max_keep=10,
        )
        # The merge above is grow-only; drop items now ANSWERED by the
        # uploaded documents (must run before the score block so the score
        # cap sees the cleaned list).
        merged["missingInfo"] = await self._llm_resolve_answered_missing_info(
            workspace_id,
            merged.get("missingInfo") or [],
            new_missing_filtered,
            log_context=ctx,
        )
        merged["weaknesses"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("weaknesses")),
            new_weak_filtered,
            item_type="weaknesses",
            max_keep=8,
        )
        merged["risks"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("risks")),
            new_risks_filtered,
            item_type="risks",
            max_keep=8,
        )
        merged["pendingActions"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("pendingActions")),
            _str_list(new_analysis.get("pendingActions")),
            item_type="pending actions",
            max_keep=6,
        )
        merged["nextSteps"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("nextSteps")),
            _str_list(new_analysis.get("nextSteps")),
            item_type="next steps",
            max_keep=6,
        )
        merged["suggestedDrafts"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("suggestedDrafts")),
            _str_list(new_analysis.get("suggestedDrafts")),
            item_type="suggested drafts",
            max_keep=6,
        )
        merged["relevantJurisdiction"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("relevantJurisdiction")),
            _str_list(new_analysis.get("relevantJurisdiction")),
            item_type="relevant jurisdiction entries",
            max_keep=6,
        )

        prev_strengths = (
            prev_analysis.get("strengths")
            if isinstance(prev_analysis.get("strengths"), list)
            else []
        )
        new_strengths = (
            new_analysis.get("strengths")
            if isinstance(new_analysis.get("strengths"), list)
            else []
        )
        merged["strengths"] = await self._llm_merge_strengths_against_previous(
            prev_strengths, new_strengths, max_keep=8,
        )

        # Identity fields don't shift on a doc-edit re-upload — same case.
        for identity_field in (
            "caseTitle",
            "caseCategory",
            "subcategories",
            "description",
            "caseType",
        ):
            prev_val = prev_analysis.get(identity_field)
            if prev_val not in (None, "", []):
                merged[identity_field] = prev_val

        # Preserve the score (and any user-feedback deltas baked in), then
        # re-cap against the merged list state.
        prev_score_val = prev_analysis.get("caseResolutionScore")
        if prev_score_val is None:
            prev_score_val = prev_analysis.get("case_resolution_score")
        try:
            prev_score_int = (
                int(prev_score_val) if prev_score_val is not None else None
            )
        except (TypeError, ValueError):
            prev_score_int = None
        max_allowed = self._analysis_report_max_allowed_score(report=merged)
        if prev_score_int is not None:
            merged["caseResolutionScore"] = min(max(0, prev_score_int), max_allowed)
        # Guard: when the prior score was missing/None (preservation above
        # skipped) and the fresh analysis came back 0, a fully-resolved report
        # (no pending items, has strengths) must not display 0 — reflect the
        # full allowed score instead.
        try:
            _cur_score = int(merged.get("caseResolutionScore") or 0)
        except (TypeError, ValueError):
            _cur_score = 0
        if _cur_score <= 0 and (
            merged.get("strengths")
            or merged.get("missingInfo")
            or merged.get("weaknesses")
            or merged.get("risks")
        ):
            # A populated analysis report must never display 0%. When neither the
            # previous nor the fresh score is usable, derive one from content
            # (full allowed score if nothing is pending, otherwise the documented
            # deduction logic) rather than leaving the stuck 0.
            if max_allowed >= 100:
                derived_score = max_allowed
            else:
                derived_score = self._derive_case_resolution_score(
                    missing_info=merged.get("missingInfo")
                    or merged.get("missing_info"),
                    weaknesses=merged.get("weaknesses"),
                    risks=merged.get("risks"),
                    max_allowed=max_allowed,
                )
            logger.warning(
                "Analysis sync merge: score 0 for populated report, workspace "
                "{}{}; deriving {}",
                workspace_id,
                ctx,
                derived_score,
            )
            merged["caseResolutionScore"] = derived_score
        prev_report_score = prev_analysis.get("reportQualityScore")
        if prev_report_score is not None:
            try:
                prev_report_score_int = int(prev_report_score)
                max_allowed = self._analysis_report_max_allowed_score(report=merged)
                merged["reportQualityScore"] = min(
                    max(0, prev_report_score_int), max_allowed
                )
            except (TypeError, ValueError):
                pass

        try:
            score_for_expl = int(merged.get("caseResolutionScore") or 0)
        except (TypeError, ValueError):
            score_for_expl = 0
        try:
            fresh_explanation = await self._ensure_quality_score_explanation(
                workspace_role=workspace_role,
                case_resolution_score=score_for_expl,
                missing_info=merged.get("missingInfo") or [],
                weaknesses=merged.get("weaknesses") or [],
                risks=merged.get("risks") or [],
                strengths=merged.get("strengths") or [],
                existing="",
                strategic_objective=self._current_strategic_objective(workspace_id),
            )
        except Exception as expl_err:
            logger.warning(
                "Analysis sync merge: explanation regen failed{}: {}", ctx, expl_err
            )
            fresh_explanation = (
                merged.get("qualityScoreExplanation")
                or prev_analysis.get("qualityScoreExplanation")
                or ""
            )
        if fresh_explanation:
            merged["qualityScoreExplanation"] = fresh_explanation
            merged["reportQualityScoreExplanation"] = (
                self._sanitize_report_quality_score_explanation(fresh_explanation)
            ).strip()

        merged["timestamp"] = datetime.utcnow().isoformat() + "Z"
        self._normalize_analysis_report_lists(
            merged, max_annexure=self._get_annexure_count(workspace_id)
        )

        logger.info(
            "Analysis sync merge{}: prev_missing={} new_missing={} merged_missing={}; "
            "prev_weak={} new_weak={} merged_weak={}; prev_risks={} new_risks={} merged_risks={}",
            ctx,
            len(_str_list(prev_analysis.get("missingInfo"))),
            len(new_missing_filtered),
            len(merged.get("missingInfo") or []),
            len(_str_list(prev_analysis.get("weaknesses"))),
            len(new_weak_filtered),
            len(merged.get("weaknesses") or []),
            len(_str_list(prev_analysis.get("risks"))),
            len(new_risks_filtered),
            len(merged.get("risks") or []),
        )
        return merged

    async def _run_objective_change_incremental_refresh(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        *,
        log_context: str = "",
        emitted: Optional[Set[str]] = None,
    ) -> None:
        """Regenerate the objective-dependent panels and preserve the rest.

        ``emitted`` is the caller's record of which featureTypes reached the
        frontend. Every emit below records itself there so the scheduler's
        terminal release knows what is still owed — without it, a step that
        raises mid-cascade leaves the remaining panels lit with no way to tell
        which ones.
        """
        ctx = log_context or " (objective change)"
        seen: Set[str] = emitted if emitted is not None else set()

        # 1) Analysis report: REGENERATE for the new objective and REPLACE the old
        # one — do NOT merge with the previous report. A change of objective is a
        # change of legal PERSPECTIVE (e.g. jewellery-theft → divorce); merging
        # with the prev report would keep the stale theft strengths/weaknesses/
        # risks and PRESERVE the old quality score, producing a mixed,
        # wrong-perspective analysis. The generator already (a) anchors to the new
        # cached objective, (b) applies OBJECTIVE-ALIGNMENT rules, (c) runs
        # `_normalize_analysis_report_lists` (which also scrubs the brand), and
        # (d) computes a fresh caseResolutionScore — so used directly it yields a
        # fully objective-aligned report with a fresh score and no carryover.
        # (`_merge_analysis_report_with_previous` is unchanged and still used by
        # the document re-upload / sync paths, where incremental merge is correct.)
        new_analysis = None
        try:
            with capture_usage() as _u_obj_analysis:
                new_analysis = await self._generate_analysis_report_from_documents(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=None,
                    is_update=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception as analysis_err:
            # Step 1 used to be unwrapped, so a raise here aborted the whole
            # cascade and left steps 2-7 (list_of_dates, applicable_laws,
            # procedural_timeline, relevant_parties, case_laws, drafting) with
            # their loaders lit and nothing coming. Fall back to the minimal
            # report and carry on: the other panels are independent of this one.
            logger.opt(exception=analysis_err).warning(
                "Objective-change analysis regeneration failed{}: {}; using fallback",
                ctx,
                analysis_err,
            )
            _u_obj_analysis = None
        if not (isinstance(new_analysis, dict) and new_analysis):
            new_analysis = self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )

        # The quality score measures readiness FOR AN OBJECTIVE, so when the
        # objective changes the explanation of that score has to change with
        # it — a lawyer who has just re-pointed the case and sees the same
        # sentences reasonably concludes nothing happened.
        #
        # The regeneration above usually supplies a fresh one, but not always:
        # the report JSON is long and `qualityScoreExplanation` is one of the
        # first fields to be lost to truncation, in which case
        # `_ensure_quality_score_explanation` had already filled it from an
        # objective-blind pass over the gap lists — which can land on text
        # identical to the previous objective's. So force the issue here:
        # missing, or unchanged from the report we are replacing, means
        # regenerate it WITH the new objective in hand.
        if isinstance(new_analysis, dict):
            try:
                _prev_report = self._workspace_analysis_payload.get(workspace_id) or {}
                _prev_expl = str(
                    (_prev_report or {}).get("qualityScoreExplanation") or ""
                ).strip()
                _new_expl = str(
                    new_analysis.get("qualityScoreExplanation") or ""
                ).strip()
                if not _new_expl or _new_expl == _prev_expl:
                    try:
                        _score = int(new_analysis.get("caseResolutionScore") or 0)
                    except (TypeError, ValueError):
                        _score = 0
                    _objective_expl = await self._ensure_quality_score_explanation(
                        workspace_role=workspace_role,
                        case_resolution_score=_score,
                        missing_info=new_analysis.get("missingInfo") or [],
                        weaknesses=new_analysis.get("weaknesses") or [],
                        risks=new_analysis.get("risks") or [],
                        strengths=new_analysis.get("strengths") or [],
                        existing="",
                        strategic_objective=self._current_strategic_objective(
                            workspace_id
                        ),
                    )
                    if _objective_expl:
                        new_analysis["qualityScoreExplanation"] = _objective_expl
                        new_analysis["reportQualityScoreExplanation"] = (
                            self._sanitize_report_quality_score_explanation(
                                _objective_expl
                            )
                        ).strip()
                        logger.info(
                            "Objective-change analysis{}: quality score "
                            "explanation regenerated for the new objective "
                            "(was {})",
                            ctx,
                            "empty" if not _new_expl else "unchanged",
                        )
            except Exception as expl_err:
                logger.warning(
                    "Objective-change quality score explanation refresh "
                    "failed{}: {}; keeping the regenerated report's own text",
                    ctx,
                    expl_err,
                )

        # Tag the objective-aligned report against the frontend's currentState
        # so items that still textually match the previously-curated ones carry
        # their id (isMatched=True) and genuinely new objective-driven items get
        # a minted id (isMatched=False). This is identity-tagging only — content
        # is fully replaced by the objective-driven regeneration above.
        if isinstance(new_analysis, dict):
            try:
                new_analysis = await self._tag_analysis_report_against_current_state(
                    workspace_id, new_analysis, log_context=ctx,
                )
            except Exception as tag_err:
                logger.warning(
                    "Objective-change analysis currentState tagging failed{}: {}; "
                    "emitting untagged",
                    ctx, tag_err,
                )

        self._workspace_analysis_payload[workspace_id] = new_analysis
        if self._socket_client:
            await self._emit_document_feature(
                feature_type="analysis_report",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=new_analysis,
                usage=_u_obj_analysis,
            )
            seen.add("analysis_report")
            logger.success(
                "Objective-change analysis update emitted "
                "(fully regenerated for new objective, prev replaced){}",
                ctx,
            )

        # The new objective text (set by the strategy regen that ran before this
        # cascade) drives the list-of-dates relevance re-score and the applicable
        # laws regeneration below.
        _strat_now = self._workspace_strategy_payload.get(workspace_id) or {}
        new_objective_text = (
            (_strat_now.get("strategicObjective") or "").strip()
            if isinstance(_strat_now, dict)
            else ""
        )

        # 2) List of dates: keep the SAME entries but RE-SCORE their relevance for
        # the new objective/perspective (Mode 2). No new/removed dates, no text
        # rewrite. Falls back to a plain preserve-and-re-emit if the re-eval fails
        # or there is no new objective text to score against.
        relevance_done = False
        if new_objective_text:
            try:
                relevance_done = await self._reeval_list_of_dates_relevance(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    objective_text=new_objective_text,
                )
            except Exception as rel_err:
                logger.warning(
                    "List of dates relevance re-eval failed{}: {}", ctx, rel_err
                )
        if relevance_done:
            seen.add("list_of_dates")
        if not relevance_done:
            preserved_dates = (
                self._workspace_list_of_dates_payload.get(workspace_id) or []
            )
            if not isinstance(preserved_dates, list):
                preserved_dates = []
            self._workspace_list_of_dates_payload[workspace_id] = preserved_dates
            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="list_of_dates",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload={
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "listOfDates": preserved_dates,
                    },
                    usage=None,
                )
                seen.add("list_of_dates")
                logger.success(
                    "Objective-change list_of_dates preserved (relevance re-eval skipped){}",
                    ctx,
                )

        # 3) Applicable laws: REGENERATE for the new objective/perspective (Mode 2)
        # — e.g. divorce → judicial separation changes which statutes apply.
        # objective_context steers the law selection + search query, and
        # merge_with_previous=False replaces stale laws instead of unioning them.
        # Falls back to preserve-and-re-emit on failure.
        laws_done = False
        try:
            await self._generate_and_emit_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                objective_context=new_objective_text or None,
                merge_with_previous=False,
            )
            laws_done = True
            seen.add("applicable_laws")
        except Exception as laws_err:
            logger.warning(
                "Applicable laws regeneration failed{}: {}", ctx, laws_err
            )
        if not laws_done:
            preserved_laws = (
                self._workspace_applicable_laws_payload.get(workspace_id) or {}
            )
            if not isinstance(preserved_laws, dict):
                preserved_laws = {}
            self._workspace_applicable_laws_payload[workspace_id] = preserved_laws
            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="applicable_laws",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=preserved_laws,
                    usage=None,
                )
                seen.add("applicable_laws")
                logger.success(
                    "Objective-change applicable_laws preserved (regeneration skipped){}",
                    ctx,
                )

        # 3b) Upcoming dates (procedural timeline): preserve existing payload on
        # objective change — same preserve-and-re-emit pattern as list_of_dates /
        # applicable_laws above. Only regenerate the timeline when documents are
        # uploaded/deleted, never on an objective change. Runs before case laws so
        # the panel order matches the main flow (upcoming dates → case laws).
        preserved_timeline = (
            self._workspace_procedural_timeline_payload.get(workspace_id) or []
        )
        if not isinstance(preserved_timeline, list):
            preserved_timeline = []
        self._workspace_procedural_timeline_payload[workspace_id] = preserved_timeline
        if self._socket_client:
            await self._emit_document_feature(
                feature_type="procedural_timeline",
                workspace_id=workspace_id,
                user_id=user_id,
                payload={"proceduralTimeline": preserved_timeline},
                usage=None,
            )
            seen.add("procedural_timeline")
            logger.success(
                "Objective-change preserved procedural_timeline re-emitted{}",
                ctx,
            )

        # 3c) Relevant parties: PRESERVE on objective change. Who the parties are
        # does not change with the legal objective; re-emit the cached payload so
        # the panel stays stable (and any FE loader clears). Only regenerate when
        # documents are added/deleted (handled by the document-sync scheduler).
        try:
            await self._emit_relevant_parties(workspace_id, user_id)
            seen.add("relevant_parties")
            logger.success(
                "Objective-change relevant_parties preserved (regeneration skipped){}",
                ctx,
            )
        except Exception as parties_err:
            logger.warning(
                "Objective-change relevant_parties preserve failed{}: {}",
                ctx,
                parties_err,
            )

        # 3d) Case laws: REGENERATE for the new objective so the precedents
        # advance the new legal direction (objective_context steers both the
        # Indian-Kanoon search query and the LLM select/rank pass;
        # merge_with_previous=False replaces the stale set instead of unioning).
        # Falls back to preserve-and-re-emit on failure so the panel never
        # sticks on a loader.
        case_done = False
        try:
            await self._generate_and_emit_case_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                objective_context=new_objective_text or None,
                merge_with_previous=False,
            )
            case_done = True
            seen.add("case_laws")
            logger.success(
                "Objective-change case_laws regenerated for new objective{}",
                ctx,
            )
        except Exception as case_err:
            logger.warning(
                "Objective-change case_laws regeneration failed{}: {}", ctx, case_err
            )
        if not case_done:
            try:
                await self._reemit_case_laws_unchanged(workspace_id, user_id)
                seen.add("case_laws")
            except Exception as case_preserve_err:
                logger.warning(
                    "Objective-change case_laws preserve fallback failed{}: {}",
                    ctx,
                    case_preserve_err,
                )

        # 5) Drafting Assistant suggested drafts: REGENERATE for the new
        # objective. The generator's brief (_build_case_brief_for_drafting)
        # reads the freshly-updated strategicObjective from the strategy cache,
        # so a plain regeneration makes the suggested drafts objective-aware.
        # Falls back to preserve-and-re-emit on failure so the panel loader clears.
        draft_done = False
        try:
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
            draft_done = True
            seen.add("drafting_assistant")
            logger.success(
                "Objective-change drafting regenerated for new objective{}",
                ctx,
            )
        except Exception as draft_err:
            logger.warning(
                "Objective-change drafting regeneration failed{}: {}",
                ctx,
                draft_err,
            )
        if not draft_done:
            try:
                await self._reemit_drafting_unchanged(workspace_id, user_id)
                seen.add("drafting_assistant")
            except Exception as draft_preserve_err:
                logger.warning(
                    "Objective-change drafting preserve fallback failed{}: {}",
                    ctx,
                    draft_preserve_err,
                )

    def _schedule_objective_change_reanalysis(
        self,
        workspace_id: str,
        user_id: str,
        *,
        log_context: str = "",
    ) -> None:
        """
        On strategic objective changes from intelligence chat:
        - regenerate analysis_report
        - preserve/re-emit current list_of_dates and applicable_laws
        (dates/laws regenerate only on document upload/delete flows).
        """
        ctx = log_context or " (objective change)"

        # Resolve role with safe fallbacks so regeneration is always attempted.
        role = "ADJUDICATOR"
        try:
            strategy_payload = self._workspace_strategy_payload.get(workspace_id) or {}
            candidate_role = (strategy_payload.get("role") or "").strip().upper()
            if candidate_role in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = candidate_role
        except Exception:
            pass
        try:
            ws_ctx = workspace_manager.get_workspace(workspace_id)
            if ws_ctx and ws_ctx.workspace_role:
                ctx_role = (ws_ctx.workspace_role or "").strip().upper()
                if ctx_role in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                    role = ctx_role
        except Exception:
            pass

        # The panels this cascade owns. strategy_objective is excluded: the
        # caller already emitted it before scheduling us, so re-emitting it here
        # would overwrite the fresh objective with whatever is cached.
        cascade_features = [
            f
            for f in OBJECTIVE_CHANGE_REFRESH_FEATURES
            if f != "strategy_objective"
        ]

        async def _runner() -> None:
            emitted: Set[str] = set()
            try:
                # Take the per-workspace pipeline lock, as
                # _schedule_strategy_analysis_pipeline already does. This
                # scheduler was the only one that did not, and it performs a
                # full analysis-report REGEN-and-replace — so interleaving it
                # with a document-upload pipeline let one clobber the other's
                # emit for the same feature.
                lock = self._workspace_pipeline_lock(workspace_id)
                if lock.locked():
                    logger.debug(
                        "pipeline lock: objective-change reanalysis{} waiting for "
                        "the in-flight pipeline (workspace={})",
                        ctx,
                        workspace_id[:12] if workspace_id else "",
                    )
                async with lock:
                    await self._run_objective_change_incremental_refresh(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=role,
                        log_context=ctx,
                        emitted=emitted,
                    )
            except asyncio.CancelledError:
                # Cancellation is a BaseException: the `except Exception` below
                # never saw it, so a cascade killed by a reconnect or a shutdown
                # used to vanish without a log line AND without releasing the
                # loaders it was responsible for.
                logger.error(
                    "Objective-change incremental refresh{} CANCELLED "
                    "(workspace={}, emitted={})",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                    sorted(emitted),
                )
                await self._release_refresh_loaders(
                    workspace_id,
                    user_id,
                    cascade_features,
                    skip=emitted,
                    reason="objective_change_cascade_cancelled",
                    workspace_role=role,
                )
                raise
            except Exception as rean_err:
                logger.opt(exception=rean_err).error(
                    "Objective-change incremental refresh{} failed: {}",
                    ctx,
                    rean_err,
                )
                self._log_objective_regen_failure(
                    workspace_id, "cascade_failed", str(rean_err)
                )
            finally:
                # Whatever happened, no panel this cascade armed may be left
                # spinning: anything that did not emit gets its terminal state.
                # A no-op when every step emitted, which is the normal path.
                try:
                    await self._release_refresh_loaders(
                        workspace_id,
                        user_id,
                        cascade_features,
                        skip=emitted,
                        reason="objective_change_cascade_terminal",
                        workspace_role=role,
                    )
                except Exception as rel_err:
                    logger.warning(
                        "Objective-change terminal loader release failed{}: {}",
                        ctx,
                        rel_err,
                    )

        try:
            self._track_background_task(
                asyncio.create_task(_runner()),
                label=f"objective-change cascade {workspace_id[:12]}",
            )
            logger.info(
                "Scheduled objective-change reanalysis{} for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule objective-change reanalysis{}: {}",
                ctx,
                rt_err,
            )

    async def _run_strategy_analysis_timeline_pipeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Dict[str, Any],
        *,
        is_update: bool,
        analysis_log_context: str,
    ) -> None:
        """
        Strategy → analysis report → scheduled list_of_dates + applicable_laws.
        Runs inside asyncio.create_task so workspace_sync / workspace_created return quickly.
        """
        role = await self._resolve_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            preferred_role=workspace_role,
        )

        logger.info("Using workspace role for background AI pipeline: {}", role)
        try:
            # Meter this feature in its own scope: _generate_strategies_from_documents
            # does not self-emit (the pipeline emits below), so capture its tokens
            # here and hand the accumulator to the emit. The accumulator object
            # survives after the scope closes, so the emit still reads the totals.
            with capture_usage() as _u_strategy:
                strategies_result = await self._generate_strategies_from_documents(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                    batch_result=batch_result,
                    is_update=is_update,
                )
            if strategies_result and self._socket_client:
                logger.success("Strategies generated (background), emitting...")
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=strategies_result,
                    usage=_u_strategy,
                )
                self._workspace_strategy_payload[workspace_id] = strategies_result
                chips = strategies_result.get("suggestionChips") or []
                if chips:
                    self._workspace_suggestion_chips[workspace_id] = list(chips)
                    self._last_workspace_id_with_chips = workspace_id
                logger.success("Strategy & Objectives sent to backend (background)")
        except Exception as strategy_error:
            logger.opt(exception=strategy_error).error("Background strategy generation failed: {}", strategy_error)
            if self._socket_client:
                try:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "error": str(strategy_error),
                        },
                    )
                except Exception as emit_error:
                    logger.error("Failed to emit strategy error (bg): {}", emit_error)

        await self._emit_analysis_report(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=role,
            batch_result=batch_result,
            is_update=is_update,
            log_context=analysis_log_context,
            tail_inline=True,
        )

    def _schedule_strategy_analysis_pipeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Dict[str, Any],
        *,
        is_update: bool,
        analysis_log_context: str,
    ) -> None:
        async def _runner() -> None:
            try:
                # Serialize per workspace: with tail_inline=True downstream,
                # this lock covers strategy → analysis → list_of_dates → laws →
                # timeline → parties → case_laws → drafting as ONE critical
                # section, so overlapping triggers cannot interleave emits.
                lock = self._workspace_pipeline_lock(workspace_id)
                if lock.locked():
                    logger.debug(
                        "pipeline lock: waiting (strategy pipeline) for workspace {}",
                        workspace_id[:12] if workspace_id else "",
                    )
                async with lock:
                    logger.debug(
                        "pipeline lock: acquired (strategy pipeline) for workspace {}",
                        workspace_id[:12] if workspace_id else "",
                    )
                    await self._run_strategy_analysis_timeline_pipeline(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=batch_result,
                        is_update=is_update,
                        analysis_log_context=analysis_log_context,
                    )
            except Exception as e:
                logger.exception(
                    "Background strategy/analysis pipeline failed for workspace {}: {}",
                    workspace_id[:12] if workspace_id else "",
                    e,
                )

        try:
            self._track_background_task(
                asyncio.create_task(_runner()),
                label=f"strategy/analysis pipeline {workspace_id[:12]}",
            )
            logger.info(
                "Scheduled background strategy + analysis + timeline/laws for workspace {}",
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as e:
            logger.error("Could not schedule strategy/analysis pipeline: {}", e)

    @meter_usage
    async def _emit_analysis_report(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False,
        log_context: str = "",
        *,
        tail_inline: bool = False,
    ) -> bool:
        """
        Generate analysis report (or use fallback on error), then emit to backend and cache.
        Emit is done outside try/except so it always runs and errors propagate.

        ``tail_inline=True`` runs the list_of_dates→drafting tail inline instead
        of scheduling it as a detached task — only for callers that already hold
        the workspace pipeline lock (the background pipeline runner), so the
        whole strategy→drafting sequence is one critical section. Callers under
        a timeout (e.g. the 35s chat refresh in analysis_report.py) must keep
        the default False.
        """
        if not self._socket_client:
            return False
        try:
            analysis_report = await self._generate_analysis_report_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                is_update=is_update,
            )
            if not analysis_report:
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
        except Exception as e:
            logger.warning(
                "Analysis report generation failed{}: {}; using fallback",
                log_context,
                e,
            )
            analysis_report = self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )
        # Tag the fresh report's text sections against the frontend's
        # currentState so each item carries {id, text, isMatched}: items that
        # match the previously-curated state keep their id (isMatched=True),
        # genuinely new items get a minted id (isMatched=False), and prev-only
        # items are dropped. Runs on every emit (create + sync) so the shape is
        # uniform; with no currentState every item is emitted new.
        if isinstance(analysis_report, dict):
            try:
                analysis_report = await self._tag_analysis_report_against_current_state(
                    workspace_id,
                    analysis_report,
                    log_context=log_context or " (emit)",
                )
            except Exception as tag_err:
                logger.warning(
                    "Analysis report currentState tagging failed{}: {}; "
                    "emitting untagged",
                    log_context, tag_err,
                )
        # NOTE: a `_suppress_analysis_emit_until_ts` guard used to sit here,
        # meant to drop stale background analysis emits during an
        # objective-change regeneration. Nothing ever SET the flag, so the
        # branch was unreachable — and had it been armed it would have returned
        # without emitting, stranding the analysis loader (its only clear ran
        # after a successful emit). The cascade now serializes on the workspace
        # pipeline lock, which is what actually prevents the interleaving.
        # Cache and set pending re-emit before emit so re-emit on reconnect has data if emit fails
        self._workspace_analysis_payload[workspace_id] = analysis_report
        self._pending_reemit_workspace_id = workspace_id
        self._pending_reemit_user_id = user_id
        await self._emit_document_feature(
            feature_type="analysis_report",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=analysis_report,
            usage=current_usage(),
        )
        logger.success("Analysis report sent to backend{}", log_context)
        # Brief yield so the socket can flush before caller continues (reduces risk of disconnect before delivery)
        await asyncio.sleep(0.15)
        # Generate and cache 15-16 suggestion chips for Analysis Report (probing_prompts_request with chatType ANALYSIS_REPORT)
        try:
            role = (workspace_role or "ADJUDICATOR").strip().upper()
            analysis_chips = await self._generate_analysis_report_suggestion_chips(
                workspace_id=workspace_id,
                report=analysis_report,
                role=role,
            )
            if analysis_chips:
                self._workspace_analysis_suggestion_chips[workspace_id] = list(
                    analysis_chips
                )
                logger.info(
                    "Analysis report suggestion chips stored: {} for workspace {}",
                    len(analysis_chips),
                    workspace_id[:12] if workspace_id else "",
                )
        except Exception as chip_err:
            logger.warning("Analysis report chips generation failed: {}", chip_err)
        if tail_inline:
            # Caller (pipeline runner) already holds the workspace pipeline
            # lock; run the tail inline so strategy→drafting emits as one
            # uninterrupted sequence.
            await self._run_list_of_dates_and_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                log_context=log_context or " (after analysis)",
            )
        else:
            # List of Dates + Applicable laws: background so intelligence chat can proceed
            self._schedule_list_of_dates_and_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                log_context=log_context or " (after analysis)",
            )
        return True

    @meter_usage
    async def _generate_and_emit_analysis_report_for_sync(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
    ) -> None:
        """
        Generate analysis report and emit to backend. Used as a background task from
        workspace_sync so the long-running Gemini call is not blocked by other socket events
        (e.g. probing_prompts_request). Ensures analysis report is always emitted.
        """
        try:
            analysis_report = await self._generate_analysis_report_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                is_update=True,
            )
            if not analysis_report:
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
            # Tag the fresh report against the frontend's currentState so items
            # carry {id, text, isMatched} (matched → prior id; new → minted id;
            # prev-only → dropped). Rejection memory is still honoured.
            if isinstance(analysis_report, dict):
                analysis_report = await self._tag_analysis_report_against_current_state(
                    workspace_id,
                    analysis_report,
                    log_context=" (sync)",
                )
            if self._socket_client and analysis_report:
                await self._emit_document_feature(
                    feature_type="analysis_report",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=analysis_report,
                    usage=current_usage(),
                )
                self._workspace_analysis_payload[workspace_id] = analysis_report
                logger.success("Analysis report sent to backend (sync)")
            self._schedule_list_of_dates_and_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                log_context=" (sync helper)",
            )
        except asyncio.CancelledError:
            logger.warning("Analysis report (sync) task cancelled; emitting fallback")
            raise
        except Exception as ar_err:
            logger.warning(
                "Analysis report (sync) failed: {}; emitting fallback", ar_err
            )
            try:
                fallback = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
                if self._socket_client:
                    await self._emit_document_feature(
                        feature_type="analysis_report",
                        workspace_id=workspace_id,
                        user_id=user_id,
                        payload=fallback,
                        usage=current_usage(),
                    )
                    self._workspace_analysis_payload[workspace_id] = fallback
                    logger.success(
                        "Analysis report (fallback) sent to backend (sync)"
                    )
                self._schedule_list_of_dates_and_applicable_laws(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=batch_result,
                    log_context=" (sync fallback)",
                )
            except Exception as e:
                logger.error("Failed to emit analysis report fallback (sync): {}", e)
