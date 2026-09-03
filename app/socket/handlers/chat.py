"""Intelligence chat, drafting-assistant chat, probing prompts.

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


# Edit verbs recognised by the cheap panel-edit precheck in
# ``on_user_message_request``. Matched on WORD BOUNDARIES: a bare substring test
# fires on ordinary prose ("edit" inside "credited", "add" inside "address",
# "name" inside "filename", "fix" inside "prefix"), which sent innocent questions
# to the LLM classifier and occasionally produced a spurious panel edit.
_PANEL_EDIT_VERB_RE = re.compile(
    r"\b(?:remove|delete|drop|change|replace|edit|modify|rewrite|rephrase"
    r"|add|append|insert|expand|shorten|fix|correct|update)\b",
    re.IGNORECASE,
)

# A CONTRASTIVE CORRECTION: the user names a wrong value and supplies the right
# one ("the disputed amount is 6 lakh USD, not 16 lakh", "it's showing 16 lakh,
# should be 6"). Such a message often contains NO edit verb at all, so the verb
# gate above never sees it and the correction silently did nothing.
#
# This is deliberately a shape, not a noun list. Adding generic nouns ("amount",
# "figure", "panel", "everything") to _panel_words was measured to take the
# classifier hit-rate from ~7% to ~27% of messages, almost all of them ordinary
# questions — an LLM call and ~$0.003 per turn for nothing. Requiring a
# contrast cue WITHIN ~40 chars of a number/date/section token is far more
# specific and costs nothing.
_CORRECTION_SHAPE_RE = re.compile(
    r"\b(?:not|instead\s+of|rather\s+than|should\s+be|it'?s\s+showing"
    r"|is\s+showing|showing|actually|in\s+fact)\b[^.?!]{0,40}?"
    r"(?:\d[\d,]*(?:\.\d+)?\s*"
    r"(?:lakhs?|lacs?|crores?|cr|k|mn|%|rs\.?|inr|usd)?"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\bsections?\s*\d+)",
    re.IGNORECASE,
)


class ChatMixin:
    """Intelligence chat, drafting-assistant chat, probing prompts."""

    def _token_usage_metadata(
        self, workspace_id: str, usage: Any
    ) -> Dict[str, Any]:
        """Build the usage/price metadata fields for one ai_response.

        Prices this turn's accumulated tokens with the active model's rate AND the
        paid non-LLM work it triggered (Vision OCR units, Speech-to-Text seconds,
        Vertex AI Search queries, Google Search grounding requests, embedding
        tokens), increments the workspace's cumulative totals (persisted to
        Qdrant), and returns a dict merged into ``AIResponseMetadata``.

        ``price`` is the FULL cost of this response — ``inputPrice + outputPrice
        + servicePrice`` — with each component also exposed on its own field so
        the backend can break the bill down by source.

        Best-effort: returns ``{}`` if anything fails so a pricing hiccup never
        blocks a response. Call exactly once per emitted response.
        """
        try:
            from app.ai.pricing import price_for, service_price_for
            from app.workspace.context import context_manager

            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            model = settings.gemini_model
            price = price_for(model, input_tokens, output_tokens)
            total_tokens = input_tokens + output_tokens

            # Paid non-LLM units consumed inside the same capture scope.
            service_units: Dict[str, Any] = {
                "ocrUnits": int(getattr(usage, "ocr_units", 0) or 0),
                "sttSeconds": round(float(getattr(usage, "stt_seconds", 0.0) or 0.0), 3),
                "searchQueries": int(getattr(usage, "search_queries", 0) or 0),
                "groundingRequests": int(
                    getattr(usage, "grounding_requests", 0) or 0
                ),
                "embedTokens": int(getattr(usage, "embed_tokens", 0) or 0),
            }
            service = service_price_for(
                ocr_units=service_units["ocrUnits"],
                stt_seconds=service_units["sttSeconds"],
                search_queries=service_units["searchQueries"],
                grounding_requests=service_units["groundingRequests"],
                embed_tokens=service_units["embedTokens"],
            )

            fields: Dict[str, Any] = {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
                "tokensUsed": total_tokens,
                "inputPrice": price["inputPrice"],
                "outputPrice": price["outputPrice"],
                # Full cost of this response: LLM tokens + paid non-LLM APIs.
                "price": round(price["price"] + service["servicePrice"], 6),
            }
            fields.update(service_units)
            fields.update(service)

            ctx = context_manager.get_context(workspace_id)
            if ctx is not None:
                already_counted = bool(getattr(usage, "counted", False))
                if already_counted:
                    # Second emit in the same turn — don't bill again; report the
                    # totals as they already stand.
                    totals = ctx.get_metadata("token_usage") or {}
                else:
                    totals = ctx.add_token_usage(
                        input_tokens,
                        output_tokens,
                        price,
                        service=service,
                        service_units=service_units,
                    )
                    try:
                        usage.counted = True
                    except Exception:
                        pass
                fields.update(
                    {
                        "totalInputTokens": totals.get("totalInputTokens", 0),
                        "totalOutputTokens": totals.get("totalOutputTokens", 0),
                        "totalInputPrice": totals.get("totalInputPrice", 0.0),
                        "totalOutputPrice": totals.get("totalOutputPrice", 0.0),
                        "totalPrice": totals.get("totalPrice", 0.0),
                    }
                )
                fields.update(self._cumulative_service_fields(totals))
            else:
                logger.debug(
                    "No workspace context for {}; emitting per-response token "
                    "usage without cumulative totals",
                    workspace_id,
                )
            return fields
        except Exception as exc:  # pricing must never break a response
            logger.warning("Failed to build token usage metadata: {}", exc)
            return {}

    def _ingest_usage_metadata(self, workspace_id: str, usage: Any) -> Dict[str, Any]:
        """Usage metadata for a HAND-BUILT ``document_feature_response`` payload.

        ``_emit_document_feature`` supplies ``messageId`` / ``aiModel`` around
        ``_token_usage_metadata``; the few payloads assembled as dict literals
        (the ingestion ``document_assessments`` emits) do not go through it, so
        this wrapper adds the same two fields. Without ``messageId`` the backend
        ``WorkspaceCostService`` drops the cost record entirely.

        Idempotent per accumulator: once a scope's spend has been reported the
        accumulator is flagged ``counted``, and any further call degrades to the
        cumulative-only block. That makes this safe to call from both the success
        emit and the ingestion error path — whichever runs first reports the real
        (possibly partial) spend, and the other cannot double-report it.
        """
        if usage is None or bool(getattr(usage, "counted", False)):
            return self._cumulative_usage_metadata(workspace_id)
        metadata = self._token_usage_metadata(workspace_id, usage)
        if isinstance(metadata, dict):
            metadata.setdefault("messageId", uuid.uuid4().hex)
            metadata.setdefault("aiModel", settings.gemini_model)
            # Hand-built payloads bypass _emit_document_feature's check, so run it
            # here too — same reason: an unknown key means an unrecorded cost.
            self._warn_unknown_metadata_keys("ingestion payload", metadata)
        return metadata

    @staticmethod
    def _warn_unknown_metadata_keys(label: str, metadata: Dict[str, Any]) -> None:
        """Warn when a metadata block carries a key the backend contract has no field for.

        ``document_feature_response`` is a hand-built dict with no pydantic model,
        so its ``metadata`` mirrors ``AIResponseMetadata`` by convention only — a
        typo'd key ships silently and the backend ``WorkspaceCostService`` never
        sees that cost. ``AIResponseMetadata`` (app/socket/schemas.py) is the single
        source of truth for the field list; nothing is dropped or renamed here, the
        mismatch is only surfaced in the logs.
        """
        try:
            from app.socket.schemas import AIResponseMetadata

            known = set(AIResponseMetadata.model_fields.keys())
            unknown = sorted(set(metadata.keys()) - known)
            if unknown:
                logger.warning(
                    "Metadata for {} carries key(s) absent from AIResponseMetadata: "
                    "{} — the backend will ignore them (typo?)",
                    label,
                    ", ".join(unknown),
                )
        except Exception as exc:  # a naming check must never block an emit
            logger.debug("Metadata key check failed for {}: {}", label, exc)

    @staticmethod
    def _cumulative_service_fields(totals: Dict[str, Any]) -> Dict[str, Any]:
        """Read the cumulative paid non-LLM totals out of a ``token_usage`` snapshot.

        Every lookup defaults to 0 because workspaces whose ``token_usage`` was
        cached before service costing was added rehydrate without these keys.
        """
        return {
            "totalOcrUnits": totals.get("totalOcrUnits", 0),
            "totalOcrPrice": totals.get("totalOcrPrice", 0.0),
            "totalSttSeconds": totals.get("totalSttSeconds", 0.0),
            "totalSttPrice": totals.get("totalSttPrice", 0.0),
            "totalSearchQueries": totals.get("totalSearchQueries", 0),
            "totalSearchPrice": totals.get("totalSearchPrice", 0.0),
            "totalGroundingRequests": totals.get("totalGroundingRequests", 0),
            "totalGroundingPrice": totals.get("totalGroundingPrice", 0.0),
            "totalEmbedTokens": totals.get("totalEmbedTokens", 0),
            "totalEmbedPrice": totals.get("totalEmbedPrice", 0.0),
            "totalServicePrice": totals.get("totalServicePrice", 0.0),
        }

    def _cumulative_usage_metadata(self, workspace_id: str) -> Dict[str, Any]:
        """Usage metadata for a RE-EMIT (no new LLM/paid API call this time).

        Per-response fields are zero; cumulative fields report the workspace's
        current running totals WITHOUT incrementing them (reconnect replay,
        cached/no-op re-emit). Best-effort: returns ``{}`` if anything fails so a
        re-emit is never blocked.
        """
        try:
            from app.workspace.context import context_manager

            fields: Dict[str, Any] = {
                # Unique per emit so the backend WorkspaceCostService records a
                # distinct cost entry (it keys on messageId and skips without one).
                "messageId": uuid.uuid4().hex,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "tokensUsed": 0,
                "inputPrice": 0.0,
                "outputPrice": 0.0,
                "price": 0.0,
                # No paid non-LLM work happened on a re-emit either.
                "ocrUnits": 0,
                "ocrPrice": 0.0,
                "sttSeconds": 0.0,
                "sttPrice": 0.0,
                "searchQueries": 0,
                "searchPrice": 0.0,
                "groundingRequests": 0,
                "groundingPrice": 0.0,
                "embedTokens": 0,
                "embedPrice": 0.0,
                "servicePrice": 0.0,
            }
            ctx = context_manager.get_context(workspace_id)
            if ctx is not None:
                totals = ctx.get_metadata("token_usage") or {}
                fields.update(
                    {
                        "totalInputTokens": totals.get("totalInputTokens", 0),
                        "totalOutputTokens": totals.get("totalOutputTokens", 0),
                        "totalInputPrice": totals.get("totalInputPrice", 0.0),
                        "totalOutputPrice": totals.get("totalOutputPrice", 0.0),
                        "totalPrice": totals.get("totalPrice", 0.0),
                    }
                )
                fields.update(self._cumulative_service_fields(totals))
            return fields
        except Exception as exc:
            logger.warning("Failed to build cumulative usage metadata: {}", exc)
            return {}

    async def _emit_document_feature(
        self,
        *,
        feature_type: str,
        workspace_id: str,
        user_id: str,
        payload: Any,
        usage: Any = None,
    ) -> None:
        """Emit a ``document_feature_response`` with a top-level token-usage metadata block.

        ``usage`` is the ``capture_usage()`` accumulator for a FRESH generation —
        its tokens are priced and the workspace cumulative is incremented exactly
        once (guarded by the accumulator's ``counted`` flag). Pass ``usage=None``
        for a re-emit (reconnect replay, cached / no-op re-emit): the block reports
        per-response zeros plus the current cumulative, with no increment.

        The metadata block uses the same field names as ``AIResponseMetadata`` so
        the backend reads it identically to the chat ``ai_response`` contract.
        Best-effort throughout: a pricing hiccup never blocks the emit.

        The metadata block is built BEFORE the socket-client check: building it is
        what folds the spend into the workspace cumulative, and money already
        burned must be billed even when there is no socket to emit on.
        """
        # Deterministic enforcement of the lawyer's fact corrections, at the one
        # chokepoint every panel emit passes through. The AUTHORITATIVE USER
        # CORRECTIONS prompt block is a soft nudge across nine generators; this
        # is what makes "the old figure can never come back" true regardless of
        # whether a given model call complied. Mutates the cached payload in
        # place on purpose, so the cache converges too. Only allowlisted text
        # fields are touched — never an ISO `date`, never a verbatim
        # `evidenceSnippet`. Mirrors _apply_removed_evidence_missing_info.
        try:
            self._apply_fact_corrections_to_payload(
                workspace_id, payload, label=feature_type
            )
        except Exception as fact_err:
            logger.warning(
                "Fact-correction pass failed for {} (non-fatal): {}",
                feature_type,
                fact_err,
            )

        try:
            if usage is not None:
                metadata = self._token_usage_metadata(workspace_id, usage)
            else:
                metadata = self._cumulative_usage_metadata(workspace_id)
            if isinstance(metadata, dict):
                # The backend WorkspaceCostService keys the cost record on
                # messageId and skips the record entirely without one. A fresh
                # (usage-based) metadata block has no messageId, so mint a unique
                # one per emit here. aiModel mirrors the ai_response contract.
                metadata.setdefault("messageId", uuid.uuid4().hex)
                metadata.setdefault("aiModel", settings.gemini_model)
                self._warn_unknown_metadata_keys(feature_type, metadata)
        except Exception as exc:
            logger.warning(
                "Feature usage metadata build failed for {} (non-fatal): {}",
                feature_type,
                exc,
            )
            metadata = {}
        if not self._socket_client:
            logger.info(
                "No socket client — {} emit dropped for workspace {} (spend still "
                "billed to the workspace cumulative)",
                feature_type,
                str(workspace_id)[:12],
            )
            return
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": feature_type,
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
                "metadata": metadata,
            },
        )

    async def _handle_drafting_assistant_message(
        self,
        *,
        payload: UserMessageRequestPayload,
        workspace_id: str,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Drive one turn of the Drafting Assistant conversation.

        Builds a lightweight case-context dict from already-cached panels,
        invokes the drafting agent, and emits an augmented `ai_response`
        with `draftStatus`, `openQuestions`, and (when ready) `draftDocument`.
        """
        actionable_id = (payload.actionableId or "").strip()
        if not actionable_id:
            # Without an actionableId we cannot keep session state coherent.
            # Generate a stable fallback so the conversation can still run
            # within the same chat.
            actionable_id = f"user_draft_chat_{chat_id}"

        # Resolve role from workspace context (same logic as the main chat
        # path, but minimised because drafting is role-aware not gated).
        role = "ADJUDICATOR"
        try:
            from app.workspace.manager import workspace_manager

            ctx = workspace_manager.get_workspace(workspace_id)
            if ctx and getattr(ctx, "workspace_role", None):
                role = (ctx.workspace_role or "ADJUDICATOR").upper()
        except Exception:
            pass
        cached_role = await self._restore_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            chat_id=chat_id,
        )
        if cached_role:
            role = cached_role

        # Compose case context for the drafting agent.
        brief = self._build_case_brief_for_drafting(workspace_id)
        case_context = {
            "role": role,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "case_summary": brief.get("case_summary") or "",
            "case_facts_brief": "\n".join(
                line for line in [
                    brief.get("analysis_brief") or "",
                    brief.get("list_of_dates_brief") or "",
                ] if line
            ),
            "applicable_laws_brief": brief.get("applicable_laws_brief") or "",
            "strategy_brief": brief.get("strategy_brief") or "",
        }

        # On kickoff-type turns, enrich the context with the RAW uploaded
        # evidence (not just panel summaries) so the drafting agent can auto-fill
        # variables whose values live only in the documents (invoice numbers,
        # marketing claims, warranty terms, etc.) instead of asking the lawyer
        # for facts the case already contains. Non-fatal — falls back to briefs.
        _kickoffish = (
            (payload.source or "").strip().lower()
            in ("template", "suggesteddraft", "blank")
            or bool(payload.templateId)
            or bool(payload.suggestedDraftId)
        )
        if _kickoffish:
            evidence = await self._evidence_brief_for_drafting(workspace_id)
            if evidence:
                case_context["evidence_brief"] = evidence
                case_context["case_facts_brief"] = "\n\n".join(
                    x for x in [
                        case_context.get("case_facts_brief") or "",
                        "UPLOADED CASE DOCUMENTS (raw evidence — extract any "
                        "case-variable values from here):\n" + evidence,
                    ] if x
                )

        conversation_history: List[Dict[str, Any]] = []
        for item in payload.conversation_history or []:
            role_str = getattr(item, "role", None) or (
                item.get("role") if isinstance(item, dict) else None
            )
            content_str = getattr(item, "content", None) or (
                item.get("content") if isinstance(item, dict) else None
            )
            if role_str and content_str is not None:
                conversation_history.append(
                    {"role": str(role_str).upper(), "content": str(content_str)}
                )

        request_fields = {
            "actionableId": actionable_id,
            "userId": user_id,
            "workspaceId": workspace_id,
            "chatId": chat_id,
            "source": (payload.source or "").strip() or None,
            "suggestedDraftId": payload.suggestedDraftId,
            "suggestionTitle": payload.suggestionTitle,
            "suggestionDescription": payload.suggestionDescription,
            "templateId": payload.templateId,
            "templateUrl": payload.templateUrl,
            "templateOriginalName": payload.templateOriginalName,
            "templateMimeType": payload.templateMimeType,
            "draftMode": (payload.draftMode or "").strip().lower() or None,
            # Reserved layout hint — threaded through for future layout-aware
            # rendering; the drafting agent does not consume it yet.
            "formatLayout": payload.formatLayout,
        }

        # When the turn may need to resolve/match a template (a suggested-draft
        # or explicit template kickoff), make sure the in-memory store is warm
        # first — no-op if already populated this session.
        _src = (payload.source or "").strip().lower()
        if _src in ("suggesteddraft", "template") or payload.templateId:
            await self.ensure_templates_synced(user_id)

        try:
            from app.langgraph.drafting_agent import drafting_agent

            turn = await drafting_agent.handle_turn(
                user_message=payload.content or "",
                case_context=case_context,
                request_fields=request_fields,
                conversation_history=conversation_history,
            )
        except Exception as e:
            logger.error("Drafting agent failed: {}", e)
            logger.exception(e)
            turn = {
                "content": (
                    "I ran into an issue while preparing the draft. "
                    "Please retry, or share any additional details that may help."
                ),
                "draftStatus": "gathering_info",
                "openQuestions": [],
                "draftDocument": None,
            }

        # Emit the augmented ai_response.
        if not self._socket_client:
            logger.error("Socket client unavailable; cannot emit drafting ai_response")
            return

        from app.ai.usage import current_usage

        ai_response_payload: Dict[str, Any] = {
            "chatId": chat_id,
            "workspaceId": workspace_id,
            "userId": user_id,
            "chat_type": "DRAFTING_ASSISTANT",
            "actionableId": actionable_id,
            "content": (turn.get("content") or "").strip(),
            "isDraft": bool(turn.get("isDraft")),
            "draftStatus": turn.get("draftStatus"),
            "openQuestions": turn.get("openQuestions") or [],
            "draftDocument": turn.get("draftDocument"),
            "metadata": {
                "messageId": chat_id,
                "aiModel": settings.gemini_model,
                # Per-response token/price + incremented workspace totals for
                # this drafting turn (scope opened in on_user_message_request).
                **self._token_usage_metadata(workspace_id, current_usage()),
            },
        }
        if ai_response_payload["draftDocument"] is None:
            ai_response_payload.pop("draftDocument", None)

        # Generate drafting-specific chips so the bottom strip in the
        # Drafting Assistant panel shows draft-actionable prompts ("Adjust
        # the Prayer", "Strengthen Grounds", …) instead of the stale
        # intelligence-chat chips that were leaking through before this fix.
        try:
            import random as _random

            chips = await self._generate_drafting_chips(
                workspace_id=workspace_id,
                actionable_id=actionable_id,
                draft_status=turn.get("draftStatus") or "",
                draft_title=(turn.get("draftDocument") or {}).get("title")
                or (turn.get("draftDocument") or {}).get("draftTitle")
                or "",
                open_questions=turn.get("openQuestions") or [],
                last_assistant_message=ai_response_payload["content"],
                last_draft_markdown=(turn.get("draftDocument") or {}).get("content")
                or "",
            )
            if chips:
                # Cache under both keys so on_probing_prompts_request can find
                # them whether BE polls with actionableId or not.
                if actionable_id:
                    self._workspace_drafting_suggestion_chips[actionable_id] = list(chips)
                if workspace_id:
                    self._workspace_drafting_suggestion_chips[workspace_id] = list(chips)
                ai_response_payload["suggestionChips"] = _random.sample(
                    chips, min(4, len(chips))
                )
        except Exception as chip_err:
            logger.warning("Drafting chips emit failed (non-fatal): {}", chip_err)
        # Cache the assistant text so a short "yes" follow-up resolves correctly.
        if ai_response_payload["content"]:
            self._intelligence_last_ai_by_chat[chat_id] = ai_response_payload[
                "content"
            ]
        # _pending_ai_response represents an emit that did NOT make it to BE
        # and must be retried on the next reconnect. If we buffer the payload
        # unconditionally (the old behaviour) every disconnect/reconnect cycle
        # re-emits the last delivered message, producing duplicate drafts in
        # the chat. Detect a confirmed delivery via (no exception thrown) +
        # (socket still connected after the await) — the second guard catches
        # the silent-skip path where emit_ai_response waited for reconnect and
        # then returned without raising.
        emit_succeeded = False
        try:
            await self._socket_client.emit_ai_response(ai_response_payload)
            emit_succeeded = bool(
                getattr(self._socket_client, "is_connected", True)
            )
        except Exception as e:
            logger.error("Failed to emit drafting ai_response: {}", e)

        if emit_succeeded:
            # Drop any stale pending payload from a previous failed emit so
            # reconnect doesn't re-emit it after we just delivered fresh content.
            self._pending_ai_response = None
            logger.success(
                "✅ Drafting ai_response emitted (status={}, version={})",
                ai_response_payload.get("draftStatus"),
                (turn.get("draftDocument") or {}).get("version"),
            )
        else:
            # Emit raised or socket was silently disconnected — keep the
            # payload buffered so on_connected can retry once we reconnect.
            self._pending_ai_response = ai_response_payload
            logger.warning(
                "Drafting ai_response not confirmed delivered — will retry on reconnect"
            )

        audit_logger.log_event(
            "user_drafting_message_processed",
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "actionable_id": actionable_id,
                "draft_status": turn.get("draftStatus"),
                "open_questions": len(turn.get("openQuestions") or []),
                "draft_ready": bool(turn.get("draftDocument")),
            },
        )

    async def on_user_message_request(self, data: Dict[str, Any]) -> None:
        """
        Handle user_message_request event (Server → AI_Maverick).

        Payload:
        {
            message: str,
            chat_type: ChatType,
            conversation_history: [{role: 'USER'|'AI', content: str}],
            workspaceId?: str,
            userId?: str,
            chatId?: str,
            timestamp: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Build chat context
        3. Process with Intelligence Chat Agent
        4. Emit ai_response back to server

        Args:
            data: User message request payload
        """
        # Open a per-turn token-usage capture scope. Every GeminiClient.generate*
        # call made while this is open (agent workflow, suggestion chips, role
        # detection, drafting agent, …) accumulates here via ContextVar, and the
        # single ai_response emitted for this turn reads the totals. Reset in the
        # finally so the scope never leaks into another task/turn.
        from app.ai.usage import begin_usage, current_usage, reset_usage

        usage_token, _usage_acc = begin_usage()
        try:
            # Validate and parse payload
            payload = UserMessageRequestPayload(**data)

            logger.info(
                "💬 User message request: Chat {} (Type: {}, Workspace: {})",
                payload.chatId,
                payload.chat_type,
                payload.workspaceId,
            )
            logger.debug(f"Message: {payload.content[:100]}...")

            # Import here to avoid circular dependencies
            from app.langgraph.chat_models import ChatContext
            from app.langgraph.intelligence_chat_agent import \
                intelligence_chat_agent
            from app.langgraph.role_agent import role_agent

            # STEP 1: Get workspace context (role, documents count) from workspace manager
            workspace_id = payload.workspaceId or "unknown"
            user_id = payload.userId or "unknown"
            chat_id = payload.chatId or f"chat_{datetime.utcnow().timestamp()}"

            # Repopulate in-memory caches from Qdrant when the AI service was
            # restarted between the original workspace_sync and this message.
            # No-op if the cache is already warm or workspace_id is missing.
            if workspace_id and workspace_id != "unknown":
                await self._hydrate_workspace_caches(workspace_id, user_id=user_id)

            # Drafting Assistant short-circuit: when chat_type is
            # DRAFTING_ASSISTANT we hand the turn to the drafting agent
            # instead of running it through the general intelligence chat.
            # The drafting agent owns its own session state keyed by
            # `actionableId` and emits the augmented `ai_response` payload
            # (draftStatus / openQuestions / draftDocument).
            if (payload.chat_type or "").strip().upper() == "DRAFTING_ASSISTANT":
                await self._handle_drafting_assistant_message(
                    payload=payload,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                return

            role_context = {
                "role": "ADJUDICATOR",
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
            role_aliases = {
                "ADJUDICATOR": "ADJUDICATOR",
                "PETITIONER": "PETITIONER",
                "RESPONDENT": "RESPONDENT",
                "MEDIATOR": "MEDIATOR",
            }
            raw_user_text = (payload.content or "").strip()
            cleaned_for_role = re.sub(r"[^A-Za-z]", "", raw_user_text).upper()
            explicit_role_selection = role_aliases.get(cleaned_for_role)
            workspace_state = {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "documents_count": 0,
                "role_selected": False,
            }
            ctx = None
            try:
                from app.workspace.manager import workspace_manager

                ctx = workspace_manager.get_workspace(workspace_id)
                if ctx:
                    workspace_role = (ctx.workspace_role or "ADJUDICATOR").upper()
                    if workspace_role not in (
                        "ADJUDICATOR",
                        "PETITIONER",
                        "RESPONDENT",
                        "MEDIATOR",
                    ):
                        workspace_role = "ADJUDICATOR"
                    role_context["role"] = workspace_role
                    await self._remember_workspace_role(
                        workspace_id, user_id, workspace_role, chat_id=chat_id
                    )
                    role_context_from_meta = ctx.get_metadata("role_context")
                    role_from_meta = ""
                    if isinstance(role_context_from_meta, dict):
                        role_from_meta = str(
                            role_context_from_meta.get("role") or ""
                        ).strip()
                    # Treat role as selected if either workspace_role is set or role metadata exists.
                    workspace_state["role_selected"] = bool(
                        (ctx.workspace_role or "").strip() or role_from_meta
                    )
                    if isinstance(
                        role_context_from_meta, dict
                    ) and role_context_from_meta.get("role"):
                        role_context.update(role_context_from_meta)
                    docs_count = (
                        ctx.get_evidence_file_count()
                        if hasattr(ctx, "get_evidence_file_count")
                        else len(getattr(ctx, "evidence_files", []))
                    )
                    workspace_state["documents_count"] = docs_count
            except Exception as ws_err:
                logger.warning(
                    "Could not load workspace for chat (using defaults): {}",
                    str(ws_err),
                )

            # Restore role from persistent cache (workspace+user and optional chat) when context was cold/reloaded.
            cached_role = await self._restore_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                chat_id=chat_id,
            )
            if cached_role:
                role_context["role"] = cached_role
                workspace_state["role_selected"] = True
                if not ctx:
                    try:
                        from app.workspace.context import context_manager

                        ctx = context_manager.upsert_workspace(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=cached_role,
                        )
                    except Exception:
                        ctx = None
                if ctx:
                    ctx.workspace_role = cached_role
                    role_ctx_meta = ctx.get_metadata("role_context", {})
                    if not isinstance(role_ctx_meta, dict):
                        role_ctx_meta = {}
                    role_ctx_meta = {**role_ctx_meta, "role": cached_role}
                    ctx.set_metadata("role", cached_role)
                    ctx.set_metadata("role_context", role_ctx_meta)
                    role_context.update(role_ctx_meta)

            # Safety: if documents exist but role flag is still false (cache miss/race),
            # don't block the user with role picker again; continue in neutral mode.
            if workspace_state.get("documents_count", 0) > 0 and not workspace_state.get(
                "role_selected"
            ):
                workspace_state["role_selected"] = True
                role_context["role"] = (
                    (role_context.get("role") or "ADJUDICATOR").strip().upper()
                )
                logger.warning(
                    "Role not found for workspace {} with existing documents; bypassing gate with role={}",
                    workspace_id[:12] if workspace_id else "",
                    role_context["role"],
                )

            # If the user explicitly sends a role name in chat, persist it on workspace context.
            if explicit_role_selection:
                try:
                    if not ctx:
                        from app.workspace.context import context_manager

                        ctx = context_manager.upsert_workspace(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=explicit_role_selection,
                        )
                    ctx.workspace_role = explicit_role_selection
                    role_ctx_meta = ctx.get_metadata("role_context", {})
                    if not isinstance(role_ctx_meta, dict):
                        role_ctx_meta = {}
                    role_ctx_meta = {**role_ctx_meta, "role": explicit_role_selection}
                    ctx.set_metadata("role", explicit_role_selection)
                    ctx.set_metadata("role_context", role_ctx_meta)
                    ctx.set_metadata("role_selected_at", datetime.utcnow().isoformat())
                    role_context.update(role_ctx_meta)
                    role_context["role"] = explicit_role_selection
                    workspace_state["role_selected"] = True
                    await self._remember_workspace_role(
                        workspace_id,
                        user_id,
                        explicit_role_selection,
                        chat_id=chat_id,
                    )
                    logger.info(
                        "Role selected from chat and stored on workspace context: {}",
                        explicit_role_selection,
                    )

                    # If user message is only the role selection word, acknowledge and return immediately.
                    if cleaned_for_role in role_aliases:
                        if self._socket_client:
                            ai_response = AIResponsePayload(
                                chatId=chat_id,
                                workspaceId=workspace_id,
                                userId=user_id,
                                content=(
                                    f"Role confirmed as {explicit_role_selection.title()}. "
                                    "I will keep this role for this workspace. "
                                    "You can now ask your legal question."
                                ),
                                metadata=AIResponseMetadata(
                                    messageId=chat_id,
                                    aiModel=settings.gemini_model,
                                    **self._token_usage_metadata(
                                        workspace_id, current_usage()
                                    ),
                                ),
                            )
                            payload_dict = ai_response.dict(by_alias=True)
                            await self._socket_client.emit_ai_response(payload_dict)
                            # Do NOT populate _pending_ai_response here — the
                            # role-confirmation ack does not need reconnect
                            # retry, and leaving a stale buffer here caused
                            # this acknowledgement to be re-emitted on the
                            # next disconnect cycle (duplicate-message bug).
                            self._intelligence_last_ai_by_chat[chat_id] = (
                                ai_response.content
                            )
                        audit_logger.log_event(
                            "workspace_role_selected_from_chat",
                            {
                                "workspace_id": workspace_id,
                                "user_id": user_id,
                                "chat_id": chat_id,
                                "role": explicit_role_selection,
                            },
                        )
                        return
                except Exception as role_sel_err:
                    logger.warning(
                        "Failed to persist role selection from chat: {}",
                        str(role_sel_err),
                    )

            # Fetch annexure text from vector store for chat (drafts, answers, etc.)
            try:
                from app.vectorstore.qdrant import vector_store

                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
                id_to_annexure_name: Dict[str, str] = {}
                if ctx and getattr(ctx, "evidence_files", None):
                    for ef in ctx.evidence_files:
                        fid = getattr(ef, "id", None)
                        if fid is None:
                            continue
                        disp = (getattr(ef, "filename", None) or "").strip()
                        if disp:
                            id_to_annexure_name[str(fid)] = disp
                # Build a stable annexure-number index from the FULL upload
                # order (oldest first). Annexure N always means the Nth uploaded
                # document, regardless of which subset we end up including.
                doc_by_id: Dict[str, Dict[str, Any]] = {}
                for n, doc in enumerate(all_docs or [], 1):
                    did = doc.get("document_id")
                    if did is None:
                        continue
                    doc_by_id[str(did)] = {
                        "content": (doc.get("content") or "").strip(),
                        "annexure_number": n,
                    }
                total_docs = len(all_docs or [])

                # Retrieval query: the user's message, widened with the previous
                # user turn for short/vague follow-ups ("what about the second one?").
                retrieval_query = (payload.content or "").strip()
                try:
                    prior_user = [
                        (it.content or "")
                        for it in (payload.conversation_history or [])
                        if str(getattr(it, "role", "") or "").upper() == "USER"
                        and getattr(it, "content", None)
                    ]
                    if len(retrieval_query) < 25 and prior_user:
                        retrieval_query = (prior_user[-1] + " " + retrieval_query).strip()
                except Exception:
                    pass

                # Force-include any annexure the user references explicitly, by
                # number/roman numeral or by filename — even if it scores low.
                forced_ids: Set[str] = set()
                um_lower = (payload.content or "").lower()
                for m in _ANNEXURE_REF_RE.finditer(payload.content or ""):
                    num = _roman_or_int(m.group(1))
                    if num is None:
                        continue
                    for did, info in doc_by_id.items():
                        if info["annexure_number"] == num:
                            forced_ids.add(did)
                for did, name in id_to_annexure_name.items():
                    if name and name.lower() in um_lower and did in doc_by_id:
                        forced_ids.add(did)

                # Select which documents to include.
                selected_ids: List[str] = []
                if total_docs <= CHAT_SMALL_WORKSPACE_DOC_THRESHOLD:
                    # Small workspace: include everything (no retrieval-miss risk).
                    selected_ids = [
                        str(d.get("document_id"))
                        for d in (all_docs or [])
                        if d.get("document_id") is not None
                    ]
                else:
                    # Large workspace: semantic search as a document selector.
                    results: List[Dict[str, Any]] = []
                    try:
                        results = await vector_store.search(
                            workspace_id=workspace_id,
                            query=retrieval_query or (payload.content or ""),
                            limit=CHAT_SEARCH_CHUNK_LIMIT,
                        )
                    except Exception as se:
                        logger.warning(
                            "Chat retrieval search failed for workspace {}, "
                            "falling back to upload order: {}",
                            workspace_id,
                            str(se),
                        )
                        results = []

                    seen: Set[str] = set()
                    for r in results:
                        orig = str(r.get("original_document_id") or "")
                        if not orig or orig not in doc_by_id or orig in seen:
                            continue
                        seen.add(orig)
                        selected_ids.append(orig)
                        if len(selected_ids) >= CHAT_MAX_DOCS_INCLUDED:
                            break

                # Merge forced docs (explicit references) into the selection.
                for did in forced_ids:
                    if did not in selected_ids:
                        selected_ids.append(did)

                # Fallback: never send empty annexure context. Degrade to the
                # first N docs by upload order (old behaviour) rather than nothing.
                if not selected_ids:
                    logger.warning(
                        "Chat retrieval produced no documents for workspace {} "
                        "(docs={}); falling back to first {} by upload order",
                        workspace_id,
                        total_docs,
                        CHAT_MAX_DOCS_INCLUDED,
                    )
                    selected_ids = [
                        str(d.get("document_id"))
                        for d in (all_docs or [])[:CHAT_MAX_DOCS_INCLUDED]
                        if d.get("document_id") is not None
                    ]

                # Assemble: ascending annexure order, full combined content per
                # doc (chunks have no overlap, so a figure can straddle a boundary),
                # truncated to the per-doc and total budgets.
                selected_ids = sorted(
                    dict.fromkeys(selected_ids),
                    key=lambda d: doc_by_id.get(d, {}).get("annexure_number", 1_000_000),
                )
                total = 0
                doc_snippets = []
                for did in selected_ids:
                    info = doc_by_id.get(did)
                    if not info or not info["content"]:
                        continue
                    content = info["content"]
                    snippet = content[:CHAT_MAX_CHARS_PER_DOC] + (
                        "..." if len(content) > CHAT_MAX_CHARS_PER_DOC else ""
                    )
                    if total + len(snippet) > CHAT_MAX_TOTAL_CHARS:
                        snippet = snippet[: max(0, CHAT_MAX_TOTAL_CHARS - total)]
                        if not snippet:
                            break
                    entry = {
                        "document_id": did,
                        "content": snippet,
                        "annexure_number": info["annexure_number"],
                    }
                    ann_name = id_to_annexure_name.get(str(did))
                    if ann_name:
                        entry["fileName"] = ann_name
                    doc_snippets.append(entry)
                    total += len(snippet)
                    if total >= CHAT_MAX_TOTAL_CHARS:
                        break
                if doc_snippets:
                    workspace_state["uploaded_documents"] = doc_snippets
                logger.info(
                    "Chat doc context: workspace={} total_docs={} mode={} "
                    "included={} chars={}",
                    workspace_id,
                    total_docs,
                    "small" if total_docs <= CHAT_SMALL_WORKSPACE_DOC_THRESHOLD else "retrieval",
                    len(doc_snippets),
                    total,
                )
            except Exception as doc_err:
                logger.warning(
                    "Could not load documents for chat context: {}", str(doc_err)
                )

            # If user attached a document to this message, download and extract text; cache by chat_id so follow-ups see it
            attachment = getattr(payload, "attachment", None)
            if attachment and getattr(attachment, "url", None):
                try:
                    from app.processing.document_processor import \
                        document_processor

                    filename = (
                        getattr(attachment, "fileName", None) or "attachment"
                    ).strip() or "attachment"
                    extracted = await document_processor.download_and_extract_text(
                        url=attachment.url,
                        filename=filename,
                        max_text_chars=12000,
                    )
                    if extracted:
                        workspace_state["chat_attachment"] = [
                            {"fileName": filename, "content": extracted}
                        ]
                        self._chat_attachment_by_chat[chat_id] = workspace_state[
                            "chat_attachment"
                        ]
                        logger.info(
                            "Chat attachment loaded and cached for chat {}: {} ({} chars)",
                            chat_id,
                            filename,
                            len(extracted),
                        )
                except Exception as att_err:
                    logger.warning("Could not process chat attachment: {}", att_err)
            else:
                # No attachment this message: use cached attachment for this chat so follow-up questions still have the document
                cached = self._chat_attachment_by_chat.get(chat_id)
                if cached:
                    workspace_state["chat_attachment"] = cached
                    logger.debug(
                        "Using cached chat attachment for follow-up (chat_id={}, {} doc(s))",
                        chat_id,
                        len(cached),
                    )

            # -------------------------------------------------------------
            # INTELLIGENCE CHAT PANEL MUTATIONS (USER-DRIVEN)
            # If the lawyer sends update markers, apply them immediately
            # to the cached Strategy/Objectives + Analysis Report and re-emit
            # updated panels to the backend.
            # STRATEGY_OBJECTIVE: new objective text → regenerate objective + next steps + chips;
            # case summary unchanged; emit strategy_objective (skip long chat memo).
            # Informal discussion → full answer may append an offer; user "yes" → regen + analysis refresh.
            # -------------------------------------------------------------
            chat_type_upper = (payload.chat_type or "").strip().upper()
            chat_history_resolved = merge_intelligence_chat_history(
                payload.conversation_history,
                current_user_content=payload.content or "",
                cached_last_ai=self._intelligence_last_ai_by_chat.get(chat_id),
            )
            update_result = {"applied": False}
            objective_regen_ok = False
            objective_ack_kind: Optional[str] = None
            implicit_objective_shift_text: Optional[str] = None
            # Defined here so the ack-builder below can read them even if the
            # try-block raises before the pre-dispatcher initialises them.
            panel_edit_applied: List[str] = []
            panel_edit_unchanged: List[str] = []
            panel_edit_failed: List[str] = []
            panel_edit_instructions: Dict[str, str] = {}
            panel_edit_is_rename = False
            # Hoisted deliberately. `_rename_instr` used to be assigned only
            # inside `if _is_rename:`, and its consumer downstream was safe only
            # because `and` short-circuits. Widening that guard to cover fact
            # corrections would evaluate an unbound local, raising inside the
            # outer try — which is swallowed as a warning AFTER the editors have
            # already mutated the cache but BEFORE the coalesced emit, leaving a
            # changed-but-never-emitted panel and a spinner lit forever.
            classification: Dict[str, Any] = {}
            _rename_instr = ""
            _fact_instr = ""
            _fact_corrections: List[Dict[str, str]] = []
            # Every featureType whose FE loader gets lit. Hoisted for the same
            # reason as the instructions above: the terminal-emit guard reads it
            # and must never see an unbound local.
            _refresh_features: List[str] = []
            _is_rename = False
            _is_fact_correction = False
            _is_workspace_wide = False
            try:
                combined_text_for_updates = payload.content or ""
                # Include attachment text if present (so updates can be pasted into a file)
                chat_attachment = workspace_state.get("chat_attachment") or []
                if isinstance(chat_attachment, list) and chat_attachment:
                    for att in chat_attachment:
                        if isinstance(att, dict):
                            combined_text_for_updates += "\n\n" + (
                                att.get("content") or ""
                            )

                um_early = (payload.content or "").strip()

                # -------------------------------------------------------------
                # Pre-dispatcher: free-form panel-edit detector for ANY chat_type.
                # Fires for messages like "remove the 3rd line from case summary",
                # "add a step about WhatsApp chats to possible next steps",
                # "change the date in strategic objective", etc.
                # Cheap keyword precheck avoids the LLM classifier call on every
                # message; only when the message contains at least one edit verb
                # AND one panel-name token do we ask the classifier.
                # -------------------------------------------------------------
                panel_edit_applied = []
                # Per-panel edit instructions that actually applied — feeds the
                # confirmation message so the chat reply can say WHAT changed
                # instead of a bare "Updated: <panel>." one-liner.
                panel_edit_instructions = {}
                # Panels we attempted but whose content did not actually change
                # (the corrected value simply was not in that panel) vs panels
                # whose editor errored. Kept apart so the chat ack can say
                # "no other panel referenced that figure" instead of either
                # claiming a change or reporting a failure.
                panel_edit_unchanged = []
                panel_edit_failed = []
                panel_edit_is_rename = False
                # Verb list lives at module scope as ``_PANEL_EDIT_VERB_RE`` so it
                # is compiled once and matched on word boundaries.
                _panel_words = (
                    "case summary", "report summary", "objective", "strategy",
                    "next step", "action item", " step", " steps", "summary",
                    "wording", "wordings", "phrasing", "language",
                    "analysis report", "analysis", "report", "strength",
                    "strengths", "weakness", "weaknesses", "risk", "risks",
                    "missing info", "missing information",
                    # Rename / find-and-replace cues — a workspace-wide rename
                    # ("correct the name X with Y") names no panel but must still
                    # reach the classifier (rule 12 fans it out to all panels).
                    "name", "spelling", "rename", "everywhere", "workspace",
                )
                _um_lower = um_early.lower()
                # Second branch: a contrastive fact correction, which frequently
                # carries no edit verb. Guarded by the existing (free)
                # informational-query check and a minimum length so a bare
                # "not 6" cannot open the gate.
                _looks_like_correction = (
                    bool(um_early)
                    and len(um_early.strip()) >= 12
                    and bool(_CORRECTION_SHAPE_RE.search(um_early))
                    and not self._is_pure_informational_query(um_early)
                )
                _maybe_edit = _looks_like_correction or (
                    bool(um_early)
                    # Word-boundary match: "credited" must not count as "edit".
                    and bool(_PANEL_EDIT_VERB_RE.search(um_early))
                    and (
                        any(w in _um_lower for w in _panel_words)
                        # When the chat is explicitly scoped to the Strategy &
                        # Objective panel, the panel is implied — an edit verb
                        # alone (e.g. "change the wordings", "add a few words")
                        # is enough to ask the classifier. Without this, such
                        # messages skip the surgical panel-edit path and fall
                        # through to the full-rewrite regenerator.
                        or chat_type_upper == "STRATEGY_OBJECTIVE"
                    )
                )
                cur_strat_for_edit = (
                    self._workspace_strategy_payload.get(workspace_id) or {}
                )
                if (
                    _maybe_edit
                    and isinstance(cur_strat_for_edit, dict)
                    and (cur_strat_for_edit.get("caseSummary") or "").strip()
                ):
                    try:
                        classification = await self._llm_classify_panel_edit_intent(
                            um_early,
                            current_strategy=cur_strat_for_edit,
                            default_panel=(
                                "strategicObjective"
                                if chat_type_upper == "STRATEGY_OBJECTIVE"
                                else None
                            ),
                        )
                    except Exception as cls_err:
                        logger.warning(
                            "Panel-edit classification failed (non-fatal): {}", cls_err
                        )
                        classification = {}

                    # Re-read the strategy cache AFTER the classifier call. The
                    # snapshot above was taken before a ~2-3s Gemini round-trip;
                    # a document upload completing in that window writes a new
                    # strategy payload, and every editor below merges onto the
                    # snapshot it was handed — so the sync's work would be
                    # silently discarded. Cheap, and closes most of the race.
                    _fresh_strat = self._workspace_strategy_payload.get(workspace_id)
                    if isinstance(_fresh_strat, dict) and (
                        _fresh_strat.get("caseSummary") or ""
                    ).strip():
                        if _fresh_strat is not cur_strat_for_edit:
                            logger.debug(
                                "Strategy cache changed during classification; "
                                "editing the fresh payload (workspace={})",
                                workspace_id[:12] if workspace_id else "",
                            )
                        cur_strat_for_edit = _fresh_strat

                    if classification and not classification.get("isQuestionLike"):
                        # ADVOCACY-ROLE REASSIGNMENT (Rule 13): the user corrected
                        # WHICH named party holds a litigation role (e.g. "the
                        # respondent is Pradeep Mehra" / "we represent X"). This is
                        # NOT a cosmetic rename — the whole strategy must be
                        # RE-ORIENTED to advocate for the correct party. Route the
                        # objective through the full-regeneration path (which flips
                        # advocacy) instead of the word-for-word surgical editor,
                        # persist the mapping so later syncs keep it, and short-
                        # circuit: the classifier already forced every panel
                        # shouldEdit=false + isRename=false, so the surgical
                        # dispatch below harmlessly no-ops.
                        if (
                            classification.get("isRoleReassignment")
                            and (classification.get("reassignParty") or "").strip()
                        ):
                            _ws_role = (
                                role_context.get("role") or "ADJUDICATOR"
                            ).strip().upper()
                            _reassign_role = (
                                classification.get("reassignRole") or ""
                            ).strip().upper()
                            _reassign_party = (
                                classification.get("reassignParty") or ""
                            ).strip()
                            # Only advocacy roles re-orient; ADJUDICATOR/MEDIATOR
                            # are neutral and have no party to advocate for.
                            if _ws_role in ("PETITIONER", "RESPONDENT"):
                                _pet_side = {
                                    "PETITIONER", "APPELLANT", "COMPLAINANT", "PLAINTIFF",
                                }
                                _res_side = {"RESPONDENT", "DEFENDANT"}
                                _ws_side = (
                                    _pet_side if _ws_role == "PETITIONER" else _res_side
                                )
                                # Did the user name OUR side (a confirmable party)
                                # or the OPPOSING side?
                                _names_our_side = (
                                    _reassign_role in ("", "CLIENT")
                                    or _reassign_role in _ws_side
                                )
                                _confirmed_party = (
                                    _reassign_party if _names_our_side else None
                                )
                                if _confirmed_party:
                                    _reorient_instr = (
                                        f"The {_ws_role} in this matter is "
                                        f"{_confirmed_party}. Re-orient the ENTIRE "
                                        f"strategic objective, possible next steps and "
                                        f"suggestion chips to advocate FOR "
                                        f"{_confirmed_party} as the {_ws_role} in the "
                                        f"live/dominant proceeding. Do NOT change any "
                                        f"case facts — only change whose side the "
                                        f"advocacy is written for."
                                    )
                                else:
                                    _reorient_instr = (
                                        f"{_reassign_party} is the "
                                        f"{_reassign_role or 'opposing party'}. Therefore "
                                        f"the {_ws_role} we represent is the party "
                                        f"OPPOSING {_reassign_party}. Re-orient the "
                                        f"ENTIRE strategic objective, possible next steps "
                                        f"and suggestion chips to advocate FOR the "
                                        f"{_ws_role} (the party opposing "
                                        f"{_reassign_party}) in the live/dominant "
                                        f"proceeding. Do NOT change any case facts."
                                    )
                                # Persist the confirmed role->party so later
                                # regenerations (e.g. a document-upload sync) keep
                                # advocating for the pinned party.
                                if _confirmed_party:
                                    try:
                                        from app.workspace.context import (
                                            context_manager as _cm,
                                        )

                                        _rctx = _cm.get_context(workspace_id)
                                        if _rctx is not None:
                                            _rctx.set_metadata(
                                                "confirmed_role_party",
                                                {
                                                    "role": _ws_role,
                                                    "party": _confirmed_party,
                                                },
                                            )
                                            _rctx._persist_meta()
                                    except Exception as _persist_err:
                                        logger.warning(
                                            "Persist confirmed role->party failed (non-fatal): {}",
                                            _persist_err,
                                        )
                                _cur_strat_reassign = (
                                    self._workspace_strategy_payload.get(workspace_id)
                                    or cur_strat_for_edit
                                )
                                if isinstance(_cur_strat_reassign, dict) and (
                                    _cur_strat_reassign.get("caseSummary") or ""
                                ).strip():
                                    await self._emit_workspace_refresh(
                                        workspace_id=workspace_id,
                                        user_id=user_id,
                                        reason="objective_change_regeneration_started",
                                        feature_types=[
                                            "strategy_objective",
                                            "analysis_report",
                                            "list_of_dates",
                                            "applicable_laws",
                                            "procedural_timeline",
                                            "drafting_assistant",
                                        ],
                                    )
                                    _reassign_ok = await self._regenerate_strategy_objective_preserving_case_summary(
                                        workspace_id=workspace_id,
                                        user_id=user_id,
                                        workspace_role=_ws_role,
                                        user_objective_instruction=_reorient_instr,
                                        current_strategy=_cur_strat_reassign,
                                        confirmed_party=_confirmed_party,
                                    )
                                    if _reassign_ok:
                                        objective_regen_ok = True
                                        objective_ack_kind = "direct"
                                        self._schedule_objective_change_reanalysis(
                                            workspace_id=workspace_id,
                                            user_id=user_id,
                                            log_context=(
                                                " (role reassignment re-orientation)"
                                            ),
                                        )
                                        logger.success(
                                            "✅ Role reassignment re-oriented objective: {}={} (workspace={})",
                                            _ws_role,
                                            _confirmed_party
                                            or f"opposing {_reassign_party}",
                                            workspace_id[:12] if workspace_id else "",
                                        )
                                    else:
                                        logger.warning(
                                            "Role reassignment re-orientation returned False (workspace={})",
                                            workspace_id[:12] if workspace_id else "",
                                        )
                        # WORKSPACE-WIDE RENAME: a name change must replace the
                        # party's name in the EXISTING content of EVERY panel
                        # (no regeneration). Don't rely on the LLM flagging each
                        # panel — force the SAME rename instruction onto all of
                        # them so coverage is exhaustive and deterministic.
                        _is_rename = bool(classification.get("isRename"))
                        panel_edit_is_rename = _is_rename
                        # A FACT CORRECTION fans out the same way a rename does —
                        # the corrected value can sit in any panel — but its
                        # instruction is synthesised deterministically below
                        # rather than taken from a per-panel classifier slot, so
                        # every panel receives byte-identical text.
                        _fact_corrections = [
                            c for c in (classification.get("factCorrections") or [])
                            if isinstance(c, dict) and c.get("now") and c.get("description")
                        ]
                        _is_fact_correction = bool(
                            getattr(settings, "enable_fact_correction_fanout", False)
                            and classification.get("isFactCorrection")
                            and _fact_corrections
                        )
                        _is_workspace_wide = _is_rename or _is_fact_correction
                        if _is_fact_correction:
                            _fact_instr = "; ".join(
                                f"The {c['description']} is {c['now']}"
                                + (f", NOT {c['was']}" if c.get("was") else "")
                                for c in _fact_corrections
                            ) + (
                                ". Correct this value wherever it appears in this "
                                "panel, including any figure written in words, in a "
                                "different currency or numeric notation, and any "
                                "calculation that depends on it. Preserve every other "
                                "word exactly; change nothing else. If this panel does "
                                "not mention the value at all, return it UNCHANGED, "
                                "byte for byte."
                            )
                            # Pin the correction BEFORE any editor runs, so it
                            # survives even if every one of them fails — and so
                            # the next document-driven regeneration cannot revert
                            # it. This is the half of the fix that makes the
                            # correction durable rather than cosmetic.
                            for _c in _fact_corrections:
                                try:
                                    self.add_user_asserted_fact(
                                        workspace_id,
                                        was=_c.get("was") or "",
                                        now=_c["now"],
                                        description=_c["description"],
                                    )
                                except Exception as _pin_err:
                                    logger.warning(
                                        "Pinning user-asserted fact failed (non-fatal): {}",
                                        _pin_err,
                                    )
                            for k in (
                                "caseSummary",
                                "reportCaseSummary",
                                "strategicObjective",
                                "possibleNextSteps",
                                "analysisReport",
                            ):
                                classification[k] = {
                                    "shouldEdit": True,
                                    "instruction": _fact_instr,
                                }
                            # listOfDates / applicable_laws / procedural_timeline
                            # are NOT sent to an LLM editor for a fact
                            # correction: their edit prompts only understand
                            # NAME / TERM / DATE / YEAR, and pushing an amount
                            # correction through them risks rewriting an ISO
                            # `date` or an `evidenceSnippet` — a verbatim quote
                            # from the evidence. They are corrected
                            # deterministically by
                            # _apply_fact_corrections_to_payload instead.
                            classification["listOfDates"] = {
                                "shouldEdit": False, "instruction": "",
                            }
                        if _is_rename:
                            _rename_instr = next(
                                (
                                    (classification.get(k) or {}).get("instruction")
                                    for k in (
                                        "strategicObjective",
                                        "caseSummary",
                                        "reportCaseSummary",
                                        "possibleNextSteps",
                                        "analysisReport",
                                        "listOfDates",
                                    )
                                    if (classification.get(k) or {}).get("instruction")
                                ),
                                "",
                            ) or (um_early or "").strip()
                            if _rename_instr:
                                for k in (
                                    "caseSummary",
                                    "reportCaseSummary",
                                    "strategicObjective",
                                    "possibleNextSteps",
                                    "analysisReport",
                                    "listOfDates",
                                ):
                                    classification[k] = {
                                        "shouldEdit": True,
                                        "instruction": _rename_instr,
                                    }
                        # Emit a single workspace:refresh up-front so the FE
                        # shows the loading state across all four strategy
                        # fields plus the cascaded panels (analysis_report,
                        # list_of_dates, applicable_laws) that will refresh
                        # after the strategy regen completes. We always
                        # emit BEFORE the first regen call so the FE loader
                        # is visible while Gemini works, and we always use
                        # the FE-recognised reason `objective_change_regeneration_started`
                        # so the loading panel actually appears in the UI.
                        _strategy_should_edit = any(
                            (classification.get(k) or {}).get("shouldEdit") is True
                            for k in (
                                "caseSummary",
                                "reportCaseSummary",
                                "strategicObjective",
                                "possibleNextSteps",
                            )
                        )
                        _analysis_should_edit = (
                            (classification.get("analysisReport") or {}).get(
                                "shouldEdit"
                            )
                            is True
                        )
                        _any_should_edit = (
                            _strategy_should_edit or _analysis_should_edit
                        )
                        # The terminal-emit guard after the editors re-emits the
                        # preserved payload for each lit feature that did not
                        # produce a completion emit, so a spinner is never left on.
                        if _any_should_edit:
                            # Loader for the panels we actually re-emit. A normal
                            # surgical edit touches only strategy_objective and/or
                            # analysis_report. A RENAME touches every panel (name
                            # replaced in existing content), so the loader covers
                            # them all.
                            if _strategy_should_edit:
                                _refresh_features.append("strategy_objective")
                            if _analysis_should_edit:
                                _refresh_features.append("analysis_report")
                            # A workspace-wide edit (rename or fact correction)
                            # can touch the doc-derived panels too. Lighting
                            # their loaders here is also what makes a fact
                            # correction reach them: the terminal-emit guard
                            # re-emits each preserved payload, and every emit
                            # passes through _apply_fact_corrections_to_payload.
                            if _is_workspace_wide:
                                for _ft in (
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                ):
                                    if _ft not in _refresh_features:
                                        _refresh_features.append(_ft)
                            try:
                                await self._emit_workspace_refresh(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    reason="objective_change_regeneration_started",
                                    feature_types=_refresh_features,
                                )
                            except Exception as rf_err:
                                logger.warning(
                                    "workspace:refresh emit (panel edit) failed (non-fatal): {}",
                                    rf_err,
                                )

                        so_slot = classification.get("strategicObjective") or {}
                        if so_slot.get("shouldEdit") and so_slot.get("instruction"):
                            # Surgical edit: preserve everything except what
                            # the user named. The legacy
                            # _regenerate_strategy_objective_preserving_case_summary
                            # is intentionally NOT used here — its prompt is
                            # biased toward a full rewrite (labels current
                            # objective "to replace", caps output at 5-6
                            # sentences, forbids repetition) which causes
                            # surgical removes to collapse unrelated content.
                            _o = {"status": "failed"}
                            ok = await self._regenerate_strategic_objective_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=so_slot["instruction"],
                                current_strategy=cur_strat_for_edit,
                                # Suppress per-editor emit: the strategy payload is
                                # emitted ONCE after all strategy fields are edited
                                # (a rename touches 3 fields → one emit, not three
                                # partial "Previous version" snapshots in the UI).
                                emit_result=False,
                                outcome=_o,
                            )
                            if not ok:
                                if _o.get("status") == "unchanged":
                                    panel_edit_unchanged.append("strategic objective")
                                else:
                                    panel_edit_failed.append("strategic objective")
                            if ok:
                                panel_edit_applied.append("strategic objective")
                                panel_edit_instructions["strategic objective"] = (
                                    so_slot["instruction"]
                                )
                                cur_strat_for_edit = (
                                    self._workspace_strategy_payload.get(workspace_id)
                                    or cur_strat_for_edit
                                )
                            else:
                                logger.warning(
                                    "Strategic objective edit returned False — instruction={!r}",
                                    (so_slot.get("instruction") or "")[:200],
                                )

                        cs_slot = classification.get("caseSummary") or {}
                        rcs_slot = classification.get("reportCaseSummary") or {}
                        if cs_slot.get("shouldEdit") or rcs_slot.get("shouldEdit"):
                            _combined_instr = " ".join(
                                x
                                for x in [
                                    cs_slot.get("instruction"),
                                    rcs_slot.get("instruction"),
                                ]
                                if x
                            ).strip()
                            # Defensive: require at least one non-empty per-panel
                            # instruction. The classifier's _slot rejects empty
                            # instructions, so this should always be non-empty
                            # when we enter this branch — but guard anyway so
                            # we never pass the full multi-panel user message
                            # to the case-summary regen by accident.
                            if _combined_instr:
                                _o = {"status": "failed"}
                                ok = await self._regenerate_case_summary_with_feedback(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_feedback=_combined_instr,
                                    current_strategy=cur_strat_for_edit,
                                    emit_result=False,
                                    outcome=_o,
                                )
                                if not ok:
                                    if _o.get("status") == "unchanged":
                                        panel_edit_unchanged.append("case summary")
                                    else:
                                        panel_edit_failed.append("case summary")
                                if ok:
                                    panel_edit_applied.append("case summary")
                                    panel_edit_instructions["case summary"] = (
                                        _combined_instr
                                    )
                                    cur_strat_for_edit = (
                                        self._workspace_strategy_payload.get(workspace_id)
                                        or cur_strat_for_edit
                                    )
                                else:
                                    logger.warning(
                                        "Case summary edit returned False — instruction={!r}",
                                        _combined_instr[:200],
                                    )

                        ns_slot = classification.get("possibleNextSteps") or {}
                        if ns_slot.get("shouldEdit") and ns_slot.get("instruction"):
                            _o = {"status": "failed"}
                            ok = await self._regenerate_next_steps_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=ns_slot["instruction"],
                                current_strategy=cur_strat_for_edit,
                                emit_result=False,
                                outcome=_o,
                            )
                            if not ok:
                                if _o.get("status") == "unchanged":
                                    panel_edit_unchanged.append("possible next steps")
                                else:
                                    panel_edit_failed.append("possible next steps")
                            if ok:
                                panel_edit_applied.append("possible next steps")
                                panel_edit_instructions["possible next steps"] = (
                                    ns_slot["instruction"]
                                )
                            else:
                                logger.warning(
                                    "Possible next steps edit returned False — instruction={!r}",
                                    (ns_slot.get("instruction") or "")[:200],
                                )

                        ar_slot = classification.get("analysisReport") or {}
                        if ar_slot.get("shouldEdit") and ar_slot.get("instruction"):
                            cur_report_for_edit = (
                                self._workspace_analysis_payload.get(workspace_id)
                                or {}
                            )
                            if (
                                isinstance(cur_report_for_edit, dict)
                                and cur_report_for_edit
                            ):
                                _o = {"status": "failed"}
                                ok = await self._regenerate_analysis_report_with_feedback(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_feedback=ar_slot["instruction"],
                                    current_report=cur_report_for_edit,
                                    outcome=_o,
                                )
                                if not ok:
                                    if _o.get("status") == "unchanged":
                                        panel_edit_unchanged.append("analysis report")
                                    else:
                                        panel_edit_failed.append("analysis report")
                                if ok:
                                    panel_edit_applied.append("analysis report")
                                    panel_edit_instructions["analysis report"] = (
                                        ar_slot["instruction"]
                                    )
                                else:
                                    logger.warning(
                                        "Analysis report edit returned False — instruction={!r}",
                                        (ar_slot.get("instruction") or "")[:200],
                                    )

                        # Mode 1 — name change propagates to List of Dates: replace
                        # the person's name in the EXISTING dated events only (no
                        # regeneration, no new/removed dates, relevance untouched).
                        # listOfDates is flagged ONLY by the rename / name-assignment
                        # rule (Rule 12), never by a general line edit.
                        lod_slot = classification.get("listOfDates") or {}
                        if lod_slot.get("shouldEdit") and lod_slot.get("instruction"):
                            _o = {"status": "failed"}
                            ok = await self._regenerate_list_of_dates_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=lod_slot["instruction"],
                                outcome=_o,
                            )
                            if not ok:
                                if _o.get("status") == "unchanged":
                                    panel_edit_unchanged.append("list of dates")
                                else:
                                    panel_edit_failed.append("list of dates")
                            if ok:
                                panel_edit_applied.append("list of dates")
                                panel_edit_instructions["list of dates"] = (
                                    lod_slot["instruction"]
                                )
                            else:
                                logger.warning(
                                    "List of dates name edit returned False — instruction={!r}",
                                    (lod_slot.get("instruction") or "")[:200],
                                )

                        # Mode 1 (rename only) — propagate the name change to the
                        # EXISTING Applicable Laws descriptions and the EXISTING
                        # Upcoming Dates (Procedural Timeline) entries. Name-only,
                        # no regeneration, no new/removed laws or milestones. Gated
                        # on isRename so a normal line edit never touches them.
                        if _is_rename and _rename_instr:
                            _o = {"status": "failed"}
                            ok = await self._regenerate_applicable_laws_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=_rename_instr,
                                outcome=_o,
                            )
                            if not ok:
                                if _o.get("status") == "unchanged":
                                    panel_edit_unchanged.append("applicable laws")
                                else:
                                    panel_edit_failed.append("applicable laws")
                            if ok:
                                panel_edit_applied.append("applicable laws")
                                panel_edit_instructions["applicable laws"] = _rename_instr
                            _o = {"status": "failed"}
                            ok = await self._regenerate_procedural_timeline_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=_rename_instr,
                                outcome=_o,
                            )
                            if not ok:
                                if _o.get("status") == "unchanged":
                                    panel_edit_unchanged.append("upcoming dates")
                                else:
                                    panel_edit_failed.append("upcoming dates")
                            if ok:
                                panel_edit_applied.append("upcoming dates")
                                panel_edit_instructions["upcoming dates"] = _rename_instr

                        # Coalesced strategy emit: the three strategy editors above
                        # suppressed their own emits (emit_result=False), so the FE
                        # now receives ONE strategy_objective update for the whole
                        # edit instead of a partial snapshot per field — which is
                        # what surfaced as several "Previous version" states during a
                        # rename. (The analysis report is a separate featureType and
                        # emits itself once.)
                        _strategy_panels = (
                            "strategic objective",
                            "case summary",
                            "possible next steps",
                        )
                        if (
                            any(p in panel_edit_applied for p in _strategy_panels)
                            and self._socket_client
                        ):
                            _final_strategy = self._workspace_strategy_payload.get(
                                workspace_id
                            )
                            if isinstance(_final_strategy, dict) and _final_strategy:
                                await self._emit_document_feature(
                                    feature_type="strategy_objective",
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    payload=_final_strategy,
                                    usage=None,
                                )
                                logger.success(
                                    "✅ Strategy panel emitted once after edits {} (workspace={})",
                                    [p for p in panel_edit_applied if p in _strategy_panels],
                                    workspace_id[:12] if workspace_id else "",
                                )

                        # Terminal-emit guard. The workspace:refresh above turned
                        # the FE spinner ON for every featureType in
                        # _refresh_features. Each _regenerate_*_with_feedback helper
                        # emits ONLY when it actually changed the payload and returns
                        # False WITHOUT emitting on a no-op (the corrected token is
                        # often absent from a given panel). Without a completion emit
                        # the spinner never clears. So for EVERY panel whose spinner
                        # we lit but which did not emit, re-emit the preserved cached
                        # payload — the same preserve-and-re-emit contract as
                        # _run_objective_change_incremental_refresh. This runs for
                        # every edit, not just renames: a plain surgical edit whose
                        # editor returns False used to hang its loader forever.
                        if _refresh_features and self._socket_client:
                            _panel_to_feature = {
                                "strategic objective": "strategy_objective",
                                "case summary": "strategy_objective",
                                "possible next steps": "strategy_objective",
                                "analysis report": "analysis_report",
                                "list of dates": "list_of_dates",
                                "applicable laws": "applicable_laws",
                                "upcoming dates": "procedural_timeline",
                            }
                            _emitted_features = {
                                _panel_to_feature[p]
                                for p in panel_edit_applied
                                if p in _panel_to_feature
                            }
                            for _ft in _refresh_features:
                                if _ft in _emitted_features:
                                    continue
                                try:
                                    if _ft == "strategy_objective":
                                        _pres = (
                                            self._workspace_strategy_payload.get(
                                                workspace_id
                                            )
                                        )
                                        if not isinstance(_pres, dict) or not _pres:
                                            continue
                                        _payload = _pres
                                    elif _ft == "analysis_report":
                                        _pres = (
                                            self._workspace_analysis_payload.get(
                                                workspace_id
                                            )
                                        )
                                        if not isinstance(_pres, dict) or not _pres:
                                            continue
                                        _payload = _pres
                                    elif _ft == "list_of_dates":
                                        _pres = (
                                            self._workspace_list_of_dates_payload.get(
                                                workspace_id
                                            )
                                            or []
                                        )
                                        _payload = {
                                            "workspaceId": workspace_id,
                                            "userId": user_id,
                                            "listOfDates": (
                                                _pres if isinstance(_pres, list) else []
                                            ),
                                        }
                                    elif _ft == "applicable_laws":
                                        _pres = (
                                            self._workspace_applicable_laws_payload.get(
                                                workspace_id
                                            )
                                            or {}
                                        )
                                        _payload = (
                                            _pres if isinstance(_pres, dict) else {}
                                        )
                                    elif _ft == "procedural_timeline":
                                        _pres = (
                                            self._workspace_procedural_timeline_payload.get(
                                                workspace_id
                                            )
                                            or []
                                        )
                                        _payload = {
                                            "proceduralTimeline": (
                                                _pres if isinstance(_pres, list) else []
                                            )
                                        }
                                    else:
                                        continue
                                    await self._emit_document_feature(
                                        feature_type=_ft,
                                        workspace_id=workspace_id,
                                        user_id=user_id,
                                        payload=_payload,
                                        usage=None,
                                    )
                                    logger.success(
                                        "✅ Preserved {} re-emitted (no-op edit) workspace={}",
                                        _ft,
                                        workspace_id[:12] if workspace_id else "",
                                    )
                                except Exception as _preserve_err:
                                    logger.warning(
                                        "Preserve-and-re-emit fallback failed for {} (non-fatal): {}",
                                        _ft,
                                        _preserve_err,
                                    )

                        if panel_edit_applied or panel_edit_unchanged or panel_edit_failed:
                            # Reuse the existing skip-chat-agent gate. The ack
                            # block below has a dedicated branch for
                            # objective_ack_kind == "panel_edit_direct".
                            #
                            # Note this fires even when NOTHING applied. The user
                            # asked for an edit; if every panel came back
                            # unchanged or errored they need to be told that, and
                            # falling through to the general chat agent (which
                            # knows nothing about the attempt) produced a
                            # conversational reply that quietly ignored the
                            # request.
                            objective_regen_ok = True
                            objective_ack_kind = "panel_edit_direct"
                            logger.success(
                                "✅ Panel edits from free-form chat — applied={} unchanged={} failed={} (workspace={})",
                                panel_edit_applied,
                                panel_edit_unchanged,
                                panel_edit_failed,
                                workspace_id[:12] if workspace_id else "",
                            )
                            # NOTE: intentionally NO _schedule_objective_change_reanalysis
                            # here. These are SURGICAL edits — the strategy fields and
                            # the analysis report were already edited in place above.
                            # Running the doc-based reanalysis would regenerate the
                            # analysis report from documents (clobbering the surgical
                            # edit and re-introducing whatever the user just corrected,
                            # e.g. a misspelled name). list_of_dates, applicable_laws,
                            # procedural_timeline and document assessments are
                            # deliberately left untouched by panel edits.

                # User said yes after we asked whether to update the objective (informal discussion flow)
                if (
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and is_user_affirmation_to_continue(um_early)
                    and not panel_edit_applied
                ):
                    last_ai_for_offer = _last_ai_message_from_history(
                        chat_history_resolved
                    )
                    if not last_ai_for_offer:
                        last_ai_for_offer = (
                            self._intelligence_last_ai_by_chat.get(chat_id) or ""
                        )
                    if self._last_ai_offered_objective_update(last_ai_for_offer):
                        thread_txt = self._format_thread_for_objective_extraction(
                            chat_history_resolved, um_early
                        )
                        instr = await self._summarize_objective_instruction_from_thread(
                            thread_txt,
                            (role_context.get("role") or "ADJUDICATOR"),
                        )
                        cur_strat = self._workspace_strategy_payload.get(workspace_id)
                        if (
                            instr
                            and len(instr.strip()) >= 15
                            and isinstance(cur_strat, dict)
                            and (cur_strat.get("caseSummary") or "").strip()
                        ):
                            await self._emit_workspace_refresh(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                reason="objective_change_regeneration_started",
                                feature_types=[
                                    "strategy_objective",
                                    "analysis_report",
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                    "drafting_assistant",
                                ],
                            )
                            regen_ok = (
                                await self._regenerate_strategy_objective_preserving_case_summary(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_objective_instruction=instr,
                                    current_strategy=cur_strat,
                                )
                            )
                            if regen_ok:
                                objective_regen_ok = True
                                objective_ack_kind = "followup_ok"
                                self._schedule_objective_change_reanalysis(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    log_context=(
                                        " (after user confirmed objective update from discussion)"
                                    ),
                                )
                            else:
                                objective_regen_ok = True
                                objective_ack_kind = "followup_clarify"
                        else:
                            objective_regen_ok = True
                            objective_ack_kind = "followup_clarify"

                objective_instruction: Optional[str] = None
                if not objective_regen_ok and chat_type_upper == "STRATEGY_OBJECTIVE":
                    prep = self._extract_intelligence_chat_panel_updates(
                        combined_text_for_updates
                    )
                    objective_instruction = prep.get("strategicObjective")
                    if not objective_instruction:
                        intent_cls = await self._llm_classify_objective_change_intent(
                            combined_text_for_updates,
                            chat_type_upper=chat_type_upper,
                        )
                        mode = str(intent_cls.get("mode") or "none")
                        norm_obj = str(intent_cls.get("objectiveText") or "").strip()
                        is_question_like = bool(
                            intent_cls.get("isQuestionLike") is True
                        )
                        logger.info(
                            "Objective intent classifier: mode={}, isQuestionLike={}, updateRequestedNow={}, objectiveTextLen={}",
                            mode,
                            is_question_like,
                            bool(intent_cls.get("updateRequestedNow") is True),
                            len(norm_obj),
                        )
                        if mode == "direct_update" and not is_question_like:
                            objective_instruction = norm_obj or combined_text_for_updates
                        elif mode == "ask_confirmation" and not is_question_like:
                            # Scoped Strategy & Objective chat: a clear objective
                            # statement here is the user setting the objective, so
                            # apply it DIRECTLY (regenerate + full 5-panel refresh)
                            # rather than only offering "do you want to update?".
                            # Questions are already filtered by
                            # _is_pure_informational_query, so this won't fire on a
                            # query. Without this, an ask_confirmation objective
                            # change skipped the refresh and dates/laws/timeline
                            # never showed the loader.
                            objective_instruction = (
                                norm_obj or combined_text_for_updates
                            )
                            implicit_objective_shift_text = objective_instruction
                if (
                    not objective_regen_ok
                    and objective_instruction
                    and chat_type_upper == "STRATEGY_OBJECTIVE"
                    and not self._is_pure_informational_query(um_early)
                ):
                    cur_strat = self._workspace_strategy_payload.get(workspace_id)
                    if isinstance(cur_strat, dict) and (
                        cur_strat.get("caseSummary") or ""
                    ).strip():
                        await self._emit_workspace_refresh(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            reason="objective_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                                "drafting_assistant",
                            ],
                        )
                        objective_regen_ok = (
                            await self._regenerate_strategy_objective_preserving_case_summary(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_objective_instruction=objective_instruction,
                                current_strategy=cur_strat,
                            )
                        )
                        if objective_regen_ok:
                            objective_ack_kind = "direct"
                            self._schedule_objective_change_reanalysis(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                log_context=" (from direct objective instruction)",
                            )

                update_result = await self._try_apply_intelligence_chat_panel_updates(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    combined_text=combined_text_for_updates,
                    ignore_strategic_objective=bool(objective_regen_ok),
                )
                if update_result.get("applied"):
                    logger.success("✅ Panel updates applied from chat message")
            except Exception as upd_err:
                logger.warning(
                    "Panel update attempt failed; continuing with generic chat. err={}",
                    str(upd_err),
                )

            chat_type_to_module = {
                "ANALYSIS_REPORT": "analysis_report",
                "STRATEGY_OBJECTIVE": "strategy_objective",
                "APPLICABLE_LAWS": "applicable_laws",
                "DRAFTING_ASSISTANT": "drafting_assistant",
                "TEMPLATES_CASE_LAWS": "case_laws",
                "CASE_LAWS": "case_laws",
                "RELEVANT_PARTIES": "relevant_parties",
            }
            active_module = chat_type_to_module.get(chat_type_upper)
            cached_analysis = self._workspace_analysis_payload.get(workspace_id)
            if cached_analysis and isinstance(cached_analysis, dict):
                workspace_state["analysis_report"] = dict(cached_analysis)

            # Surface the cached Case Laws panel to the chat agent so a
            # Case-Laws-scoped conversation can reason over the precedents shown.
            cached_case_laws_payload = self._workspace_case_laws_payload.get(
                workspace_id
            )
            case_laws_for_chat: List[Dict[str, Any]] = []
            if isinstance(cached_case_laws_payload, dict):
                cl = cached_case_laws_payload.get("caseLaws")
                if isinstance(cl, list):
                    case_laws_for_chat = [c for c in cl if isinstance(c, dict)]

            # Surface the cached Relevant Parties panel to the chat agent.
            cached_parties_payload = self._workspace_relevant_parties_payload.get(
                workspace_id
            )
            parties_for_chat: List[Dict[str, Any]] = []
            if isinstance(cached_parties_payload, dict):
                pl = cached_parties_payload.get("parties")
                if isinstance(pl, list):
                    parties_for_chat = [p for p in pl if isinstance(p, dict)]

            custom_chat_suffix = build_custom_instruction_suffix(workspace_id) or None

            # STEP 2 & 3: Build chat context (history merged above for objective follow-up + graph)
            chat_context = ChatContext(
                workspace_id=workspace_id,
                user_id=user_id,
                role_context=role_context,
                workspace_state=workspace_state,
                active_module=active_module,
                case_laws=case_laws_for_chat,
                relevant_parties=parties_for_chat,
                custom_instruction_suffix=custom_chat_suffix,
                chat_history=chat_history_resolved,
            )

            # STEP 4: Process with Intelligence Chat Agent (skip when objective-only regen ran)
            skip_intelligence_llm = bool(objective_regen_ok)
            import random

            ai_update_result: Dict[str, Any] = {"applied": False}
            start_time = datetime.utcnow()
            if skip_intelligence_llm:
                logger.info(
                    "Skipping intelligence chat LLM (strategy objective regen emitted)"
                )
                ack_parts: List[str] = []
                if objective_ack_kind == "panel_edit_direct" and (
                    panel_edit_applied or panel_edit_unchanged or panel_edit_failed
                ):
                    # Free-form panel edit. The panel itself was already emitted
                    # via document_feature_response; the chat reply must still
                    # tell the lawyer WHAT changed, not just name the panel.
                    ack_text = await self._build_panel_edit_ack(
                        workspace_id=workspace_id,
                        user_message=um_early,
                        panels=panel_edit_applied,
                        instructions=panel_edit_instructions,
                        is_rename=panel_edit_is_rename,
                        unchanged=panel_edit_unchanged,
                        failed=panel_edit_failed,
                    )
                    if not ack_text:
                        # Templated fallback, used whenever the ack LLM call
                        # fails. It must obey the same rule as the prompt: never
                        # claim a panel that did not actually change.
                        _parts: List[str] = []
                        if panel_edit_applied:
                            _parts.append(
                                "I've updated the "
                                + ", ".join(panel_edit_applied)
                                + " as you asked."
                            )
                        if panel_edit_unchanged:
                            _parts.append(
                                "The "
                                + ", ".join(panel_edit_unchanged)
                                + " did not contain the content you named, so "
                                "nothing there changed."
                            )
                        if panel_edit_failed:
                            _parts.append(
                                "I could not update the "
                                + ", ".join(panel_edit_failed)
                                + " — please try again."
                            )
                        if not _parts:
                            _parts.append(
                                "I could not find the content you named in any panel, "
                                "so nothing was changed."
                            )
                        _parts.append("Let me know if you'd like any further adjustments.")
                        ack_text = " ".join(_parts)
                    ack_parts.append(ack_text)
                elif objective_ack_kind == "followup_clarify":
                    ack_parts.append(
                        "I could not infer a clear new strategic objective from our discussion. "
                        "Please briefly state the priority you want (for example venue, relief, or focus), "
                        "and I will update the strategy and analysis panels."
                    )
                elif objective_ack_kind == "followup_ok":
                    ack_parts.append(
                        "I have updated the strategic objective and possible next steps. "
                        "I am refreshing the analysis report, list of dates, and applicable laws "
                        "to align with this objective based on our discussion."
                    )
                elif objective_ack_kind == "direct" or objective_regen_ok:
                    # Keep chat response short to avoid duplicating strategy panel payload
                    # that is already emitted via document_feature_response.
                    ack_parts.append(
                        "I have updated the strategic objective and possible next steps. "
                        "I am refreshing the analysis report, list of dates, and applicable laws "
                        "to align with this objective based on our discussion."
                    )
                if isinstance(update_result, dict) and update_result.get("applied"):
                    extra = (update_result.get("response_text") or "").strip()
                    if extra:
                        ack_parts.append(extra)
                response_text_to_emit = " ".join(ack_parts).strip() or "Panel updated."
                from types import SimpleNamespace

                response = SimpleNamespace(
                    response_text=response_text_to_emit,
                    message_id=chat_id,
                    tokens_used=0,
                    confidence=SimpleNamespace(value="high"),
                    detected_intent="strategy_objective_refresh",
                )
                processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
                suggestion_chips_for_payload = []
                try:
                    ws_chips = self._workspace_suggestion_chips.get(workspace_id) or []
                    if ws_chips:
                        suggestion_chips_for_payload = random.sample(
                            ws_chips, min(4, len(ws_chips))
                        )
                except Exception:
                    pass
            else:
                logger.info("🤖 Processing message with Intelligence Chat Agent...")
                response = await intelligence_chat_agent.process_message(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    message_id=chat_id,
                    user_message=payload.content,
                    chat_context=chat_context,
                )
                # If downstream chat intent says STRATEGY_CHANGE in Strategy & Objective chat,
                # force the same direct objective-refresh pipeline (refresh -> strategy regen -> reanalysis).
                detected_intent_upper = str(
                    getattr(response, "detected_intent", "") or ""
                ).upper()
                if (
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and detected_intent_upper == "STRATEGY_CHANGE"
                    and not self._is_pure_informational_query(payload.content or "")
                ):
                    cur_strat = self._workspace_strategy_payload.get(workspace_id)
                    if isinstance(cur_strat, dict) and (
                        cur_strat.get("caseSummary") or ""
                    ).strip():
                        await self._emit_workspace_refresh(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            reason="objective_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                                "drafting_assistant",
                            ],
                        )
                        regen_ok = (
                            await self._regenerate_strategy_objective_preserving_case_summary(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_objective_instruction=(payload.content or "").strip(),
                                current_strategy=cur_strat,
                            )
                        )
                        if regen_ok:
                            self._schedule_objective_change_reanalysis(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                log_context=" (promoted from strategy_change intent)",
                            )
                            objective_regen_ok = True
                            objective_ack_kind = "direct"
                            response_text_to_emit = (
                                "I have updated the strategic objective and possible next steps. "
                                "I am refreshing the analysis report, list of dates, and applicable laws "
                                "to align with this objective based on our discussion."
                            )
                            from types import SimpleNamespace

                            response = SimpleNamespace(
                                response_text=response_text_to_emit,
                                message_id=chat_id,
                                tokens_used=0,
                                confidence=SimpleNamespace(value="high"),
                                detected_intent="strategy_objective_refresh",
                            )
                            processing_time = (
                                datetime.utcnow() - start_time
                            ).total_seconds() * 1000
                            suggestion_chips_for_payload = []
                            try:
                                ws_chips = (
                                    self._workspace_suggestion_chips.get(workspace_id)
                                    or []
                                )
                                if ws_chips:
                                    suggestion_chips_for_payload = random.sample(
                                        ws_chips, min(4, len(ws_chips))
                                    )
                            except Exception:
                                pass
                # If assistant produced panel-update markers, apply them as well.
                if objective_regen_ok:
                    ai_update_result = {"applied": False}
                elif chat_type_upper != "STRATEGY_OBJECTIVE":
                    try:
                        ai_update_result = await self._try_apply_intelligence_chat_panel_updates(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            combined_text=response.response_text or "",
                        )
                        if ai_update_result.get("applied"):
                            logger.success(
                                "✅ Panel updates applied from AI response markers"
                            )
                    except Exception as ai_upd_err:
                        logger.warning(
                            "AI response panel update attempt failed; continuing. err={}",
                            str(ai_upd_err),
                        )
                else:
                    logger.debug(
                        "Skipping AI response marker application for STRATEGY_OBJECTIVE chat to avoid duplicate objective refresh flows"
                    )
                # If we applied deterministic panel updates, override the chat text
                if objective_regen_ok:
                    pass
                elif isinstance(update_result, dict) and update_result.get("applied"):
                    response_text_to_emit = update_result.get("response_text")
                elif isinstance(ai_update_result, dict) and ai_update_result.get(
                    "applied"
                ):
                    cleaned = self._strip_panel_update_markers_from_text(
                        response.response_text or ""
                    )
                    response_text_to_emit = (
                        cleaned
                        if cleaned
                        else ai_update_result.get("response_text")
                        or response.response_text
                    )
                else:
                    response_text_to_emit = response.response_text
                # Informal strategic discussion: offer to update panels (user can reply yes next turn)
                if (
                    not objective_regen_ok
                    and
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and OBJECTIVE_UPDATE_OFFER_QUESTION.lower()
                    not in (response_text_to_emit or "").lower()
                ):
                    try:
                        should_append_offer = bool(
                            implicit_objective_shift_text
                        ) or await self._should_offer_objective_update_after_discussion(
                            payload.content or "",
                            response_text_to_emit or "",
                        )
                        if should_append_offer:
                            response_text_to_emit = (
                                (response_text_to_emit or "").rstrip()
                                + "\n\n"
                                + OBJECTIVE_UPDATE_OFFER_QUESTION
                            )
                    except Exception as offer_err:
                        logger.warning(
                            "Objective update offer append failed: {}", offer_err
                        )
                if not objective_regen_ok:
                    processing_time = (
                        datetime.utcnow() - start_time
                    ).total_seconds() * 1000

                    # Generate 10-12 contextual chips, store for this workspace; send random 4 in response (backend polls every few sec and we return random 4 each time)
                    suggestion_chips_for_payload = []
                    try:
                        role_for_chips = (role_context.get("role") or "ADJUDICATOR").upper()
                        generated = await self._generate_contextual_suggestion_chips(
                            workspace_id=workspace_id,
                            role=role_for_chips,
                            user_message=payload.content,
                            ai_response_text=response_text_to_emit,
                            max_chips=12,
                        )
                        if generated:
                            self._workspace_chat_suggestion_chips[workspace_id] = list(
                                generated
                            )
                            suggestion_chips_for_payload = random.sample(
                                generated, min(4, len(generated))
                            )
                            logger.info(
                                "Stored {} contextual chips for workspace; sending random 4 in ai_response (each 4s poll will return random 4 from these {})",
                                len(generated),
                                len(generated),
                            )
                    except Exception as chip_err:
                        logger.warning(
                            "Suggestion chips generation failed: {}", str(chip_err)
                        )

            # STEP 5: Build AI response payload (suggestionChips = random 4 from 10-12; click sends chip as next message)
            ai_response = AIResponsePayload(
                chatId=chat_id,
                workspaceId=workspace_id,
                userId=user_id,
                content=response_text_to_emit,
                metadata=AIResponseMetadata(
                    messageId=response.message_id,
                    aiModel=settings.gemini_model,
                    responseTime=processing_time,
                    confidence=(
                        response.confidence.value
                        if hasattr(response.confidence, "value")
                        else str(response.confidence)
                    ),
                    detectedIntent=response.detected_intent,
                    # Per-response token/price + incremented workspace totals.
                    # tokensUsed is set here too (total tokens for this response).
                    **self._token_usage_metadata(workspace_id, current_usage()),
                ),
                suggestionChips=(
                    suggestion_chips_for_payload
                    if suggestion_chips_for_payload
                    else None
                ),
            )

            # STEP 6: Emit ai_response back to server. _pending_ai_response is
            # the buffer that on_connected re-emits after a reconnect — we
            # populate it ONLY when the emit was not confirmed delivered, so a
            # successful emit does not get duplicated on the next disconnect
            # cycle. See _handle_drafting_assistant_message for the same fix.
            if self._socket_client:
                payload_dict = ai_response.dict(by_alias=True)
                if payload_dict.get("suggestionChips") is None:
                    payload_dict.pop("suggestionChips", None)
                emit_succeeded = False
                try:
                    await self._socket_client.emit_ai_response(payload_dict)
                    emit_succeeded = bool(
                        getattr(self._socket_client, "is_connected", True)
                    )
                except Exception as emit_err:
                    logger.error("Failed to emit ai_response: {}", emit_err)
                if emit_succeeded:
                    self._pending_ai_response = None
                    logger.success("✅ AI response sent successfully")
                else:
                    self._pending_ai_response = payload_dict
                    logger.warning(
                        "ai_response not confirmed delivered — will retry on reconnect"
                    )
                await self._remember_workspace_role(
                    workspace_id,
                    user_id,
                    (role_context.get("role") or "ADJUDICATOR"),
                    chat_id=chat_id,
                )
                # Cache assistant text for follow-ups like "yes" when conversation_history is incomplete
                _rt = (response_text_to_emit or "").strip()
                if (
                    _rt
                    and "Select one role to proceed" not in _rt
                    and not _rt.startswith(ANNEXURE_UPLOAD_GATE_MESSAGE[:40])
                    and not _rt.lower().startswith("i apologize")
                ):
                    self._intelligence_last_ai_by_chat[chat_id] = _rt
            else:
                logger.error("❌ Socket client not available to emit response")

            # STEP 7: Log audit event
            audit_logger.log_event(
                "user_message_processed",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "message_length": len(payload.content),
                    "response_length": len(response_text_to_emit),
                    "processing_time_ms": processing_time,
                    "intent": response.detected_intent,
                },
            )

        except Exception as e:
            logger.error("Error processing user_message_request: {}", str(e))
            logger.exception(e)

            # Try to send error response back (only if payload was successfully parsed)
            try:
                # Get basic info from data dict for error response
                chat_id = data.get("chatId")
                workspace_id = data.get("workspaceId", "unknown")
                user_id = data.get("userId", "unknown")

                if self._socket_client and chat_id:
                    error_meta = AIResponseMetadata(
                        messageId=chat_id,
                        aiModel=settings.gemini_model,
                        error=str(e),
                        **self._token_usage_metadata(workspace_id, current_usage()),
                    )
                    error_response = AIResponsePayload(
                        chatId=chat_id,
                        workspaceId=workspace_id,
                        userId=user_id,
                        content=f"I apologize, but I encountered an error processing your message: {str(e)}",
                        metadata=error_meta,
                    )
                    await self._socket_client.emit_ai_response(
                        error_response.dict(by_alias=True)
                    )
                    logger.info("Error response sent to user")
            except Exception as emit_error:
                logger.error(f"Failed to emit error response: {emit_error}")
        finally:
            # Always close the per-turn usage scope opened above.
            try:
                reset_usage(usage_token)
            except Exception:
                pass

    async def on_probing_prompts_request(self, data: Dict[str, Any]) -> None:
        """
        Handle probing_prompts_request from backend.
        - If conversation context is provided (lastUserMessage, lastAiResponse, or conversation_history),
          generate contextual, role-based suggestion chips so they change with the chat.
        - Otherwise return a random 4 from the stored 15-16 strategy chips (role/case-related).
        Chips are sent as prompts[]; when user clicks one, that text goes into the chat as the user message.

        The whole body runs inside a usage scope: the contextual-chip branch calls
        Gemini (``_generate_contextual_suggestion_chips``), and without a scope
        those tokens were billed to nobody. Most polls serve cached chips and spend
        nothing, so the usage metadata is attached only when tokens were actually
        consumed — see the emit below.
        """
        import random

        from app.ai.usage import capture_usage

        with capture_usage() as chips_usage:
            await self._handle_probing_prompts_request(data, chips_usage)

    async def _handle_probing_prompts_request(
        self, data: Dict[str, Any], chips_usage: Any = None
    ) -> None:
        """Body of :meth:`on_probing_prompts_request` (see its docstring).

        Split out so the usage scope can wrap the whole thing with one ``with``
        rather than re-indenting the entire handler.
        """
        import random

        try:
            workspace_id = (
                data.get("workspaceId") or data.get("workspace_id") or ""
            ).strip()
            chat_type = (data.get("chatType") or data.get("chat_type") or "").strip()
            max_prompts = data.get("maxPrompts") or data.get("max_prompts")
            if max_prompts is None:
                max_prompts = 4
            try:
                max_prompts = int(max_prompts)
            except (TypeError, ValueError):
                max_prompts = 4

            prompts = []
            if chat_type == "ANALYSIS_REPORT":
                # Analysis Report: return random 4 from stored 15-16 analysis-report chips (same pattern as strategy)
                chips = (
                    self._workspace_analysis_suggestion_chips.get(workspace_id) or []
                )
                if not chips:
                    chips = self._fallback_chips_for_analysis_report(16)
                count = min(max_prompts, len(chips))
                prompts = random.sample(chips, count) if count > 0 else []
                logger.info(
                    "probing_prompts: ANALYSIS_REPORT, returning random {} from {} chips",
                    len(prompts),
                    len(chips),
                )
            elif chat_type == "DRAFTING_ASSISTANT":
                # Drafting panel: serve from the drafting-specific chip cache
                # (populated by _handle_drafting_assistant_message after every
                # drafting turn). Prefer per-session chips when BE supplies
                # actionableId; fall back to workspace-keyed chips; then
                # static drafting fallback. This isolates the drafting strip
                # from the intelligence-chat chip cache so the lawyer never
                # sees stale Strategy / Analysis chips here.
                actionable_id_for_chips = (
                    (data.get("actionableId") or data.get("actionable_id") or "").strip()
                )
                chips = (
                    self._workspace_drafting_suggestion_chips.get(actionable_id_for_chips)
                    or self._workspace_drafting_suggestion_chips.get(workspace_id)
                    or []
                )
                source = (
                    "actionable" if (actionable_id_for_chips and chips)
                    else "workspace" if chips
                    else "fallback"
                )
                if not chips:
                    chips = self._fallback_drafting_chips(8)
                count = min(max_prompts, len(chips))
                prompts = random.sample(chips, count) if count > 0 else []
                logger.info(
                    "probing_prompts: DRAFTING_ASSISTANT, returning random {} from {} chips (source={}, actionableId={})",
                    len(prompts),
                    len(chips),
                    source,
                    actionable_id_for_chips[:24] if actionable_id_for_chips else "(none)",
                )
            else:
                # STRATEGY_OBJECTIVE or other: contextual chips, then strategy chips
                # 1) If we have 10-12 stored contextual chips for this workspace, return random 4 each time (so each poll shows different chips)
                stored_chat_chips = (
                    self._workspace_chat_suggestion_chips.get(workspace_id) or []
                )
                if stored_chat_chips:
                    count = min(max_prompts, len(stored_chat_chips))
                    prompts = (
                        random.sample(stored_chat_chips, count) if count > 0 else []
                    )
                    logger.info(
                        "probing_prompts: returning random {} from {} stored chips (rotation)",
                        len(prompts),
                        len(stored_chat_chips),
                    )
                else:
                    # 2) No stored chat chips: if we have conversation context, generate 10-12, store, return random 4
                    last_user = (
                        data.get("lastUserMessage")
                        or data.get("last_user_message")
                        or ""
                    ).strip()
                    last_ai = (
                        data.get("lastAiResponse") or data.get("last_ai_response") or ""
                    ).strip()
                    conv_history = (
                        data.get("conversation_history")
                        or data.get("conversationHistory")
                        or []
                    )
                    if conv_history and (not last_user and not last_ai):
                        for m in reversed(conv_history):
                            r = (m.get("role") or "").upper()
                            c = (m.get("content") or "").strip()
                            if not c:
                                continue
                            if r == "USER" and not last_user:
                                last_user = c[:500]
                            elif r == "AI" and not last_ai:
                                last_ai = c[:500]
                            if last_user and last_ai:
                                break
                    prompts = []
                    if (
                        last_user
                        or last_ai
                        or (conv_history and len(conv_history) >= 2)
                    ):
                        role = "ADJUDICATOR"
                        try:
                            ctx = workspace_manager.get_workspace(workspace_id)
                            if ctx and ctx.workspace_role:
                                role = (ctx.workspace_role or "ADJUDICATOR").upper()
                        except Exception:
                            pass
                        if last_user or last_ai:
                            conv_summary = None
                            if conv_history and len(conv_history) > 2:
                                parts = []
                                for m in conv_history[-4:]:
                                    r = m.get("role") or m.get("role", "")
                                    c = (m.get("content") or "")[:80]
                                    if c:
                                        parts.append(f"{r}: {c}")
                                conv_summary = " | ".join(parts) if parts else None
                            generated = (
                                await self._generate_contextual_suggestion_chips(
                                    workspace_id=workspace_id,
                                    role=role,
                                    user_message=last_user,
                                    ai_response_text=last_ai,
                                    max_chips=12,
                                    conversation_summary=conv_summary,
                                )
                            )
                            if generated:
                                self._workspace_chat_suggestion_chips[workspace_id] = (
                                    generated
                                )
                                count = min(max_prompts, len(generated))
                                prompts = (
                                    random.sample(generated, count) if count > 0 else []
                                )
                                logger.info(
                                    "probing_prompts: generated {} contextual chips, returning random {}",
                                    len(generated),
                                    len(prompts),
                                )
                    # 3) Fallback: strategy chips (random 4 from 15-16)
                    if not prompts:
                        chips = self._workspace_suggestion_chips.get(workspace_id) or []
                        if not chips:
                            strategy_payload = (
                                self._workspace_strategy_payload.get(workspace_id) or {}
                            )
                            arr = strategy_payload.get("suggestionChips") or []
                            if isinstance(arr, list):
                                chips = [str(x).strip() for x in arr if str(x).strip()]
                                if chips:
                                    self._workspace_suggestion_chips[workspace_id] = list(
                                        chips
                                    )
                                    self._last_workspace_id_with_chips = workspace_id
                        if not chips and self._last_workspace_id_with_chips:
                            chips = (
                                self._workspace_suggestion_chips.get(
                                    self._last_workspace_id_with_chips
                                )
                                or []
                            )
                        if not chips:
                            role = "ADJUDICATOR"
                            try:
                                ctx = workspace_manager.get_workspace(workspace_id)
                                if ctx and ctx.workspace_role:
                                    role = (ctx.workspace_role or "ADJUDICATOR").upper()
                            except Exception:
                                pass
                            chips = self._fallback_chips_for_role(role, 16)
                            self._workspace_suggestion_chips[workspace_id] = list(chips)
                            self._last_workspace_id_with_chips = workspace_id
                        if not chips:
                            logger.warning(
                                "probing_prompts_request: no suggestion chips for workspace {}",
                                workspace_id,
                            )
                        count = min(max_prompts, len(chips)) if chips else 0
                        prompts = random.sample(chips, count) if count > 0 else []
                    # 4) Always return at least generic prompts so UI never gets count=0
                    if not prompts:
                        generic = [
                            "Summarize key points from the documents",
                            "Explain the main strengths",
                            "What are the risks or gaps?",
                            "Suggest next steps",
                        ]
                        prompts = generic[:max_prompts]
                        logger.info(
                            "probing_prompts_request: using generic fallback prompts (count={})",
                            len(prompts),
                        )

            response = {
                "workspaceId": data.get("workspaceId"),
                "userId": data.get("userId"),
                "actionableId": data.get("actionableId"),
                "chatId": data.get("chatId"),
                "chatType": data.get("chatType"),
                "viewType": data.get("viewType"),
                "prompts": prompts,
            }
            if data.get("timestamp") is not None:
                response["timestamp"] = data.get("timestamp")

            # Bill the chip-generation spend (only the contextual branch calls
            # Gemini; a cached-chip poll consumes nothing and gets no metadata
            # block, so the backend does not record a stream of $0 entries).
            # Billed before the throttle check below so a throttled response
            # still adds its real spend to the workspace cumulative.
            chips_metadata = None
            spent_tokens = (
                int(getattr(chips_usage, "input_tokens", 0) or 0)
                + int(getattr(chips_usage, "output_tokens", 0) or 0)
            )
            if workspace_id and spent_tokens > 0:
                chips_metadata = self._ingest_usage_metadata(workspace_id, chips_usage)
                if chips_metadata:
                    response["metadata"] = chips_metadata

            if self._socket_client:
                import time

                now = time.time()
                last = self._last_probing_emit_time.get(workspace_id) or 0
                if now - last < self.PROBING_THROTTLE_SECONDS:
                    logger.debug(
                        "probing_prompts_response throttled (last emit %.1fs ago), workspaceId=%s",
                        now - last,
                        workspace_id[:12] if workspace_id else "",
                    )
                else:
                    self._last_probing_emit_time[workspace_id] = now
                    await self._socket_client.emit("probing_prompts_response", response)
                    logger.success(
                        "probing_prompts_response emitted: workspaceId={}, count={}",
                        workspace_id[:12] if workspace_id else "",
                        len(prompts),
                    )
            else:
                logger.warning("No socket client to emit probing_prompts_response")
        except Exception as e:
            logger.exception(e)
            logger.error("probing_prompts_request failed: {}", str(e))
