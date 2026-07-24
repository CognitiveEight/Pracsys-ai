"""Drafting Assistant suggestions and template-driven refresh.

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
from app.ai.usage import current_usage, meter_usage
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


class DraftingMixin:
    """Drafting Assistant suggestions and template-driven refresh."""

    async def _evidence_brief_for_drafting(
        self, workspace_id: str, cap: int = 40000
    ) -> str:
        """Concatenate the workspace's uploaded documents (capped) so the
        drafting agent can auto-fill variables from the raw evidence — not just
        the panel summaries. Reuses the per-doc-cap + `_format_document_for_prompt`
        pattern used by strategy/analysis. Returns '' on failure / no docs."""
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning(
                "drafting evidence fetch failed (workspace={}): {}", workspace_id, e
            )
            return ""
        valid = [
            d for d in (all_docs or [])
            if (d.get("content") or "").strip()
            and len((d.get("content") or "").strip()) >= 20
        ]
        if not valid:
            return ""
        per_doc = max(800, cap // max(1, len(valid)))
        parts: List[str] = []
        for i, doc in enumerate(valid, 1):
            content = (doc.get("content") or "").strip()
            if len(content) > per_doc:
                content = content[:per_doc] + "\n[... truncated for length ...]"
            doc_clip = dict(doc)
            doc_clip["content"] = content
            parts.append(_format_document_for_prompt(doc_clip, i, len(valid)))
        out = "\n\n".join(parts)
        if len(out) > cap:
            out = out[:cap] + "\n[... content truncated ...]"
        logger.info(
            "drafting evidence brief built (workspace={}, docs={}, chars={})",
            workspace_id,
            len(valid),
            len(out),
        )
        return out

    def _build_case_brief_for_drafting(
        self, workspace_id: str
    ) -> Dict[str, str]:
        """Compose the case-context strings the drafting agent needs.

        Pulled from already-cached panels (analysis_report, strategy, etc.)
        so a single LLM call generates focused, grounded suggestions.
        """
        case_summary = ""
        applicable_laws_brief = ""
        strategy_brief = ""
        analysis_brief = ""
        list_of_dates_brief = ""

        try:
            strat = self._workspace_strategy_payload.get(workspace_id) or {}
            if isinstance(strat, dict):
                case_summary = (strat.get("caseSummary") or "").strip()
                strategic = (strat.get("strategicObjective") or "").strip()
                next_steps = strat.get("possibleNextSteps") or []
                if isinstance(next_steps, list):
                    next_steps_text = "; ".join(
                        str(s).strip() for s in next_steps[:5] if s
                    )
                else:
                    next_steps_text = ""
                strategy_brief = "\n".join(
                    line for line in [
                        f"Strategic objective: {strategic}" if strategic else "",
                        f"Possible next steps: {next_steps_text}" if next_steps_text else "",
                    ] if line
                )
        except Exception:
            pass

        try:
            analysis = self._workspace_analysis_payload.get(workspace_id) or {}
            if isinstance(analysis, dict):
                weaknesses = analysis.get("weaknesses") or []
                missing = analysis.get("missingInfo") or []
                pending = analysis.get("pendingActions") or []
                strengths = analysis.get("strengths") or []
                lines: List[str] = []
                if isinstance(strengths, list) and strengths:
                    strength_pts = [
                        (s.get("point") or s.get("text") or "") if isinstance(s, dict) else str(s)
                        for s in strengths[:4]
                    ]
                    strength_pts = [p.strip() for p in strength_pts if p and p.strip()]
                    if strength_pts:
                        lines.append("Strengths: " + " | ".join(strength_pts))
                if isinstance(weaknesses, list) and weaknesses:
                    # Items are now {id,text,isMatched} dicts (were plain
                    # strings) — coerce to text so we never render dict reprs.
                    weak_pts = [
                        s for s in _coerce_analysis_text_list(weaknesses[:4]) if s
                    ]
                    if weak_pts:
                        lines.append("Weaknesses: " + " | ".join(weak_pts))
                if isinstance(missing, list) and missing:
                    miss_pts = [
                        s for s in _coerce_analysis_text_list(missing[:4]) if s
                    ]
                    if miss_pts:
                        lines.append("Missing info: " + " | ".join(miss_pts))
                if isinstance(pending, list) and pending:
                    pend_pts = [str(p).strip() for p in pending[:4] if p]
                    if pend_pts:
                        lines.append("Pending actions: " + " | ".join(pend_pts))
                analysis_brief = "\n".join(lines)
        except Exception:
            pass

        try:
            laws_payload = (
                self._workspace_applicable_laws_payload.get(workspace_id) or {}
            )
            if isinstance(laws_payload, dict):
                laws = laws_payload.get("applicableLaws") or laws_payload.get("laws") or []
                if isinstance(laws, list) and laws:
                    parts: List[str] = []
                    for law in laws[:8]:
                        if not isinstance(law, dict):
                            continue
                        act = (law.get("act") or "").strip()
                        section = (law.get("section") or "").strip()
                        if act and section:
                            parts.append(f"{act} §{section}")
                        elif act:
                            parts.append(act)
                    if parts:
                        applicable_laws_brief = "; ".join(parts)
        except Exception:
            pass

        try:
            dates = self._workspace_list_of_dates_payload.get(workspace_id) or []
            if isinstance(dates, list) and dates:
                lines = []
                for d in dates[:6]:
                    if not isinstance(d, dict):
                        continue
                    dt = (d.get("date") or "").strip()
                    title = (d.get("title") or d.get("eventType") or "").strip()
                    if dt and title:
                        lines.append(f"{dt} — {title}")
                if lines:
                    list_of_dates_brief = "\n".join(lines)
        except Exception:
            pass

        return {
            "case_summary": case_summary,
            "applicable_laws_brief": applicable_laws_brief,
            "strategy_brief": strategy_brief,
            "analysis_brief": analysis_brief,
            "list_of_dates_brief": list_of_dates_brief,
        }

    @meter_usage
    async def _generate_and_emit_drafting_suggestions(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """
        Generate the proactive Drafting Assistant panel suggestions and emit
        them as a document_feature_response (featureType="drafting_assistant").

        Cached in `_workspace_drafting_payload` for reconnect re-emit.
        """
        if not self._socket_client:
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent

            role_upper = (workspace_role or "ADJUDICATOR").strip().upper() or "ADJUDICATOR"
            # Make sure the lawyer's template library is loaded into the
            # in-memory store BEFORE pre-matching. The backend only pushes
            # template_sync on upload/rename/delete, so after an AI restart
            # the store is cold — request a full snapshot and wait for it.
            await self.ensure_templates_synced(user_id)
            brief = self._build_case_brief_for_drafting(workspace_id)
            suggestions = await drafting_agent.suggest(
                role=role_upper,
                case_summary=brief["case_summary"],
                applicable_laws_brief=brief["applicable_laws_brief"],
                strategy_brief=brief["strategy_brief"],
                analysis_brief=brief["analysis_brief"],
                list_of_dates_brief=brief["list_of_dates_brief"],
                # Pre-match each suggestion against the lawyer's template
                # library so the response carries templateId / templateName
                # per card. Frontend can display "Uses <template>" badge
                # and pass templateId straight through on click, skipping
                # a second matcher round-trip server-side.
                user_id=user_id,
            )

            payload = {"suggestedDrafts": suggestions or []}
            self._workspace_drafting_payload[workspace_id] = payload

            # Record provenance so the panel can self-heal if a template
            # finishes ingesting after this point (slow scanned-PDF OCR that
            # missed the template-sync window). `lib_count` is the template
            # count these suggestions were pre-matched against; a later growth
            # triggers a regenerate. See _maybe_reemit_drafting_on_templates.
            try:
                from app.langgraph.template_store import template_store

                lib_count = await template_store.count_for_user(user_id=user_id)
            except Exception:
                lib_count = 0
            self._drafting_meta_by_workspace[workspace_id] = {
                "user_id": user_id,
                "role": role_upper,
                "lib_count": lib_count,
            }

            # Log exactly what we're about to send back per suggestion —
            # title + the pre-matched template fields the frontend uses to
            # render the "Uses <template>" badge and pass templateId on click.
            logger.info(
                "📤 drafting_assistant suggestions payload (workspace={}, user={}): "
                "{} suggestion(s)",
                workspace_id,
                (user_id or "")[:12],
                len(payload["suggestedDrafts"]),
            )
            for idx, s in enumerate(payload["suggestedDrafts"]):
                if not isinstance(s, dict):
                    logger.info("   ✏️ [{}] (non-dict: {})", idx, type(s).__name__)
                    continue
                logger.info(
                    "   ✏️ [{}] title={!r} relevance={} matchScore={} matchMode={} "
                    "templateId={} templateName={!r} matchedDrafts={}",
                    idx,
                    s.get("title"),
                    s.get("relevance"),
                    s.get("matchScore"),
                    s.get("matchMode"),
                    s.get("templateId"),
                    s.get("templateName"),
                    len(s.get("matchedDrafts") or []),
                )

            # Cache keeps the internal shape (drives freeze/rematch on growth);
            # the wire goes out in the narrower matches/matchPercent contract.
            response_body = {
                "featureType": "drafting_assistant",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": drafting_payload_to_wire(payload),
            }
            # Full dump of the exact response body sent to the backend for
            # end-to-end verification (titles/descriptions/templateId/
            # templateName only — no credentials, so no redaction needed).
            try:
                logger.info(
                    "📦 drafting_assistant RESPONSE body to backend:\n{}",
                    json.dumps(response_body, indent=2, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.warning("Drafting suggestions: could not dump response body: {}", e)

            await self._emit_document_feature(
                feature_type="drafting_assistant",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=drafting_payload_to_wire(payload),
                usage=current_usage(),
            )
            logger.success(
                "✅ Drafting suggestions sent to backend ({} item(s))",
                len(payload["suggestedDrafts"]),
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Drafting suggestions emit failed: {}", e)

    async def _reemit_drafting_unchanged(
        self, workspace_id: str, user_id: str
    ) -> None:
        """Re-emit the cached drafting suggestions (clears a FE loader) without
        regenerating — used on objective-only changes, where the suggested
        drafts must be preserved (they only regenerate on document add/delete).

        The cache holds the rich internal card shape; the wire goes out in the
        narrower matches/matchPercent contract via ``drafting_payload_to_wire``,
        matching the reconnect re-emit path."""
        if not self._socket_client:
            return
        cached = self._workspace_drafting_payload.get(workspace_id)
        if cached is None:
            return
        await self._emit_document_feature(
            feature_type="drafting_assistant",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=drafting_payload_to_wire(cached),
            usage=None,
        )
        logger.success(
            "✅ Objective-change drafting suggestions preserved (regeneration skipped)"
        )

    async def _maybe_reemit_drafting_on_templates(self, user_id: str) -> None:
        """Self-heal the Drafting Assistant panel after the template library
        CHANGES — a template lands late (upload) OR a template is removed
        (delete).

        On UPLOAD a card that had no match can pick up the new template; on
        DELETE a card matched to the now-gone template is demoted (its stale
        lock released) and re-matched against the current library — so a
        deleted template can never keep driving a suggestion. Matched cards
        whose template still exists stay frozen. The ``lib_count`` comparison
        fires on any COUNT CHANGE (grow or shrink) and debounces no-op
        snapshots (unchanged count) so reconnect re-sends don't churn.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return
        try:
            from app.langgraph.template_store import template_store

            current_count = await template_store.count_for_user(user_id=user_id)
        except Exception as e:
            logger.warning(
                "drafting re-emit: count_for_user failed (user={}): {}",
                user_id[:12],
                e,
            )
            return

        for workspace_id, meta in list(self._drafting_meta_by_workspace.items()):
            if (meta or {}).get("user_id") != user_id:
                continue
            prev_count = int((meta or {}).get("lib_count") or 0)
            if current_count == prev_count:
                continue  # library unchanged — no-op snapshot, nothing to do
            role = (meta or {}).get("role") or "ADJUDICATOR"
            logger.info(
                "🔁 Template library changed ({}→{}) — refreshing drafting "
                "suggestions for workspace={} (user={}) — matched cards whose "
                "template still exists stay frozen; deleted-template cards are "
                "demoted and re-matched",
                prev_count,
                current_count,
                workspace_id,
                user_id[:12],
            )
            # Bump the recorded count up front so a burst of template events
            # can't queue duplicate refreshes for the same change.
            self._drafting_meta_by_workspace[workspace_id] = {
                **(meta or {}),
                "lib_count": current_count,
            }
            asyncio.create_task(
                self._refresh_drafting_matches_on_growth(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                )
            )

    @meter_usage
    async def _refresh_drafting_matches_on_growth(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """Re-emit a workspace's drafting panel after the library grew,
        FREEZING already-matched cards.

        Unlike ``_generate_and_emit_drafting_suggestions`` (which re-rolls
        the LLM suggestions and would lose each card's advertised template),
        this preserves the previously-emitted cards: matched cards keep their
        template verbatim and only unmatched cards are re-matched against the
        larger library — so a freshly-uploaded template can fill in a card
        that had no match, but can never hijack a card that already advertised
        a different template.

        Falls back to a full regeneration when there is no prior payload to
        refresh (cold workspace).
        """
        if not self._socket_client:
            return
        prior_payload = self._workspace_drafting_payload.get(workspace_id) or {}
        prior_cards = prior_payload.get("suggestedDrafts") or []
        if not prior_cards:
            # Nothing to freeze — fall back to a full generation.
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent

            role_upper = (workspace_role or "ADJUDICATOR").strip().upper() or "ADJUDICATOR"
            brief = self._build_case_brief_for_drafting(workspace_id)
            refreshed = await drafting_agent.rematch_unmatched_suggestions(
                user_id=user_id,
                suggestions=prior_cards,
                role=role_upper,
                case_summary=brief["case_summary"],
                applicable_laws_brief=brief["applicable_laws_brief"],
                strategy_brief=brief["strategy_brief"],
                analysis_brief=brief["analysis_brief"],
                list_of_dates_brief=brief["list_of_dates_brief"],
            )
            payload = {"suggestedDrafts": refreshed or []}
            self._workspace_drafting_payload[workspace_id] = payload

            for idx, s in enumerate(payload["suggestedDrafts"]):
                if not isinstance(s, dict):
                    continue
                logger.info(
                    "   ✏️ [{}] title={!r} matchScore={} matchMode={} templateId={} "
                    "templateName={!r}",
                    idx,
                    s.get("title"),
                    s.get("matchScore"),
                    s.get("matchMode"),
                    s.get("templateId"),
                    s.get("templateName"),
                )

            await self._emit_document_feature(
                feature_type="drafting_assistant",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=drafting_payload_to_wire(payload),
                usage=current_usage(),
            )
            logger.success(
                "✅ Drafting panel refreshed (frozen matched cards) — "
                "{} item(s) re-emitted (workspace={})",
                len(payload["suggestedDrafts"]),
                workspace_id,
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Drafting suggestions refresh failed: {}", e)
