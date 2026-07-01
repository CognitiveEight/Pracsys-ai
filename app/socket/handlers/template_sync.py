"""Drafting template library sync and blueprint proposals.

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


class TemplateSyncMixin:
    """Drafting template library sync and blueprint proposals."""

    async def on_template_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle template_sync event (Server → AI_Maverick).

        Payload:
        {
            action: "uploaded" | "renamed" | "deleted",
            userId: str,
            templates: [ {id, name, previousName?, category, extension,
                          mimeType?, size?, fileHash?, fileUrl?, path?} ],
            timestamp: str (ISO)
        }

        For "uploaded" the AI downloads each template, extracts its text via
        `file_handlers.process_file_by_extension`, and stores the result in a
        lawyer-scoped Qdrant cache keyed by (userId, template_id). "renamed"
        only patches cached metadata (content unchanged) — keyed on
        template_id, so no stale name-keyed entry is left behind. "deleted"
        removes the cache entry (supports bulk: multiple templates per event).
        """
        payload_in = data if isinstance(data, dict) else {}
        # Log the RAW inbound event exactly as the backend pushed it
        # (top-level keys + action + template count) before validation,
        # so a malformed/unexpected payload is still visible in the logs.
        logger.info(
            "📥 template_sync RECEIVED: action={} userId={} templates={} keys={}",
            (payload_in or {}).get("action"),
            str((payload_in or {}).get("userId") or "")[:12],
            len((payload_in or {}).get("templates") or [])
            if isinstance((payload_in or {}).get("templates"), list)
            else "(not a list)",
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncPayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync: invalid payload (action={}, keys={}): {}",
                (payload_in or {}).get("action"),
                list((payload_in or {}).keys()),
                e,
            )
            return

        action = (payload.action or "").strip().lower()
        user_id = (payload.userId or "").strip()
        if not user_id:
            logger.warning("template_sync: missing userId — dropping event")
            return

        templates = list(payload.templates or [])
        logger.info(
            "📚 template_sync action={} user={} count={}",
            action,
            user_id[:12],
            len(templates),
        )
        # Per-item log so it's clear exactly what the backend pushed.
        # Signed-URL query strings (credentials) are stripped — only the
        # GCS object path is shown so each file is identifiable.
        for idx, tpl in enumerate(templates):
            raw_url = tpl.fileUrl or ""
            url_path = raw_url.split("?", 1)[0] if raw_url else ""
            logger.info(
                "   📄 [{}] id={} name={!r} category={} ext={} "
                "hasFileUrl={} fileHash={} path={}",
                idx,
                tpl.id,
                tpl.name,
                tpl.category,
                tpl.extension,
                bool(raw_url),
                tpl.fileHash,
                tpl.path or url_path or "(none)",
            )

        # Full field-by-field dump of the raw inbound payload for end-to-end
        # verification. Signed-URL credentials are stripped from every
        # template's fileUrl (only the GCS object path is kept) so no
        # credentials ever land in the logs.
        try:
            sanitized = dict(payload_in)
            raw_templates = payload_in.get("templates")
            if isinstance(raw_templates, list):
                redacted = []
                for tpl_raw in raw_templates:
                    if isinstance(tpl_raw, dict):
                        tpl_copy = dict(tpl_raw)
                        fu = tpl_copy.get("fileUrl")
                        if isinstance(fu, str) and fu:
                            tpl_copy["fileUrl"] = fu.split("?", 1)[0]
                        redacted.append(tpl_copy)
                    else:
                        redacted.append(tpl_raw)
                sanitized["templates"] = redacted
            logger.info(
                "📋 template_sync RAW payload (credentials stripped):\n{}",
                json.dumps(sanitized, indent=2, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("template_sync: could not dump raw payload: {}", e)

        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("template_sync: drafting agent unavailable: {}", e)
            return

        if action == "uploaded":
            await self._ingest_template_items(user_id, templates)
            # A newly-ingested template may match suggestions that were built
            # before it was ready — regenerate the panel if the library grew.
            await self._maybe_reemit_drafting_on_templates(user_id)
            # Emit the AI's blueprint proposal for the dashboard approval screen,
            # for each freshly-uploaded file-template that did NOT already carry
            # an approved blueprint (scenario B re-uploads skip this).
            await self._emit_blueprint_proposals(user_id, templates)
        elif action == "formatting_updated":
            # The lawyer approved / edited a template's AI blueprint in the
            # dashboard. File content is unchanged — store the approved
            # blueprint as authoritative (no re-download / re-extract).
            for tpl in templates:
                if not tpl.id:
                    continue
                blueprint = None
                if getattr(tpl, "formatting", None) is not None and tpl.formatting.blueprint is not None:
                    blueprint = tpl.formatting.blueprint.model_dump()
                if blueprint is None:
                    logger.info(
                        "formatting_updated: no blueprint on item {} — skipping",
                        tpl.id,
                    )
                    continue
                await drafting_agent.apply_approved_blueprint(
                    user_id=user_id, template_id=tpl.id, blueprint=blueprint
                )
        elif action == "preset_created":
            # A court / letterhead preset (no file body) — cache its layout for
            # use at DOCX export.
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.store_preset(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Custom",
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
        elif action == "renamed":
            for tpl in templates:
                if not tpl.id or not tpl.name:
                    continue
                await drafting_agent.rename_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    new_name=tpl.name,
                    previous_name=tpl.previousName,
                )
        elif action == "deleted":
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.delete_template(
                    user_id=user_id, template_id=tpl.id
                )
            # Self-heal the drafting panel: demote any suggestion card that was
            # matched to a now-deleted template and re-match it against the
            # current library, so a deleted template can never drive a kickoff.
            await self._maybe_reemit_drafting_on_templates(user_id)
        else:
            logger.warning("template_sync: unknown action {!r}", action)

    async def _emit_blueprint_proposals(
        self, user_id: str, templates: List[Any]
    ) -> None:
        """Emit a ``template_blueprint_proposal`` for each freshly-uploaded
        file-template that did NOT already carry an approved blueprint.

        The AI profiles the template at ingest; here we package that into the
        proposal the dashboard renders on its approval screen. Per-item failures
        are isolated and non-fatal."""
        if not self._socket_client:
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_emit_blueprint_proposals: drafting agent unavailable: {}", e)
            return
        for tpl in templates:
            # Skip presets (no draft body) and re-uploads that already shipped an
            # approved blueprint (those are stored authoritative at ingest).
            if getattr(tpl, "kind", None) == "preset":
                continue
            if (
                getattr(tpl, "formatting", None) is not None
                and tpl.formatting.blueprint is not None
            ):
                continue
            if not tpl.id:
                continue
            try:
                proposal = await drafting_agent.build_blueprint_proposal(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal build failed (template={}): {}", tpl.id, e
                )
                continue
            if not proposal:
                continue
            try:
                await self._socket_client.emit(
                    "template_blueprint_proposal", proposal
                )
                logger.success(
                    "📤 template_blueprint_proposal emitted (template={}, status={}, vars={})",
                    tpl.id,
                    proposal.get("status"),
                    len((proposal.get("blueprint") or {}).get("variables") or []),
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal emit failed (template={}): {}", tpl.id, e
                )

    async def _ingest_template_items(
        self, user_id: str, templates: List[Any]
    ) -> int:
        """Download + extract + cache each template into the in-memory store.

        Shared by the ``template_sync`` ``uploaded`` action and the
        ``template_sync_response`` snapshot. Returns the count ingested.
        Per-item failures are isolated and non-fatal. Re-uses any existing
        cached payload so an unchanged file hash skips re-extraction.
        """
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_ingest_template_items: drafting agent unavailable: {}", e)
            return 0

        ingested = 0
        for tpl in templates:
            if not tpl.id or not tpl.fileUrl:
                logger.warning(
                    "template ingest: skipping item with missing id/fileUrl ({!r})",
                    tpl.id,
                )
                continue
            try:
                previous = await drafting_agent.get_template(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception:
                previous = None
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Document Draft",
                    extension=tpl.extension or "",
                    mime_type=tpl.mimeType,
                    file_url=tpl.fileUrl,
                    path=tpl.path,
                    file_hash=tpl.fileHash,
                    previous_payload=previous if isinstance(previous, dict) else None,
                    kind=getattr(tpl, "kind", None),
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
                ingested += 1
            except Exception as e:
                logger.warning(
                    "template ingest failed (template={}): {}", tpl.id, e
                )
        return ingested

    async def on_template_sync_response(self, data: Dict[str, Any]) -> None:
        """Handle template_sync_response (Server → AI), the reply to our
        ``request_template_sync``. Ingests the full snapshot into the
        in-memory store and resolves the awaiting Future (by requestId)."""
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "📥 template_sync_response RECEIVED: userId={} requestId={} count={} keys={}",
            str((payload_in or {}).get("userId") or "")[:12],
            (payload_in or {}).get("requestId"),
            (payload_in or {}).get("count"),
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncResponsePayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync_response: invalid payload (keys={}): {}",
                list((payload_in or {}).keys()),
                e,
            )
            return

        user_id = (payload.userId or "").strip()
        request_id = payload.requestId
        templates = list(payload.templates or [])

        # Full sanitized dump (signed-URL credentials stripped) for verification.
        try:
            sanitized = dict(payload_in)
            raw_templates = payload_in.get("templates")
            if isinstance(raw_templates, list):
                redacted = []
                for tpl_raw in raw_templates:
                    if isinstance(tpl_raw, dict):
                        tpl_copy = dict(tpl_raw)
                        fu = tpl_copy.get("fileUrl")
                        if isinstance(fu, str) and fu:
                            tpl_copy["fileUrl"] = fu.split("?", 1)[0]
                        redacted.append(tpl_copy)
                    else:
                        redacted.append(tpl_raw)
                sanitized["templates"] = redacted
            logger.info(
                "📋 template_sync_response RAW payload (credentials stripped):\n{}",
                json.dumps(sanitized, indent=2, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("template_sync_response: could not dump raw payload: {}", e)

        try:
            if not user_id:
                logger.warning(
                    "template_sync_response: missing userId — dropping event"
                )
            else:
                ingested = await self._ingest_template_items(user_id, templates)
                logger.success(
                    "📚 template_sync_response ingested {}/{} template(s) for user={}",
                    ingested,
                    len(templates),
                    user_id[:12],
                )
        finally:
            # Always resolve the awaiting future — even if ingestion raised — so
            # ensure_templates_synced (which now waits with no hard time cap)
            # can never hang indefinitely on a failed ingest.
            fut = (
                self._pending_template_sync.get(request_id) if request_id else None
            )
            if fut is not None and not fut.done():
                fut.set_result(True)

        # If this snapshot arrived late (after the ensure_templates_synced
        # timeout) and grew the library, regenerate any drafting panels that
        # were built against the smaller/empty library.
        if user_id:
            await self._maybe_reemit_drafting_on_templates(user_id)

    async def on_request_blueprint_proposal(self, data: Dict[str, Any]) -> None:
        """Handle request_blueprint_proposal (Server → AI), the "Regenerate
        Extra Info" action on the dashboard's blueprint-approval screen.

        Re-profiles the template (forcing a fresh profile even if one exists)
        and emits a fresh ``template_blueprint_proposal`` back. Ingests the
        template from ``fileUrl`` first when the in-memory store is cold."""
        payload_in = data if isinstance(data, dict) else {}
        user_id = str(payload_in.get("userId") or "").strip()
        template_id = str(payload_in.get("templateId") or "").strip()
        file_url = payload_in.get("fileUrl") or ""
        file_hash = payload_in.get("fileHash") or ""
        logger.info(
            "📥 request_blueprint_proposal RECEIVED: userId={} templateId={} hasFileUrl={}",
            user_id[:12],
            template_id,
            bool(file_url),
        )
        if not user_id or not template_id:
            logger.warning(
                "request_blueprint_proposal: missing userId/templateId — dropping event"
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error(
                "request_blueprint_proposal: drafting agent unavailable: {}", e
            )
            return

        # Ensure the template is in the store with extracted text. On a cold
        # cache (AI restart) the template may be absent — ingest it from the
        # supplied fileUrl, deriving name/extension from the object path.
        try:
            tpl = await drafting_agent.get_template(
                user_id=user_id, template_id=template_id
            )
        except Exception:
            tpl = None
        if (not tpl or not (tpl.get("extracted_text") or "").strip()) and file_url:
            url_path = file_url.split("?", 1)[0]
            fname = url_path.rsplit("/", 1)[-1] if url_path else ""
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=template_id,
                    name=fname or template_id,
                    category=(tpl.get("category") if isinstance(tpl, dict) else None)
                    or "Document Draft",
                    extension=ext,
                    mime_type=None,
                    file_url=file_url,
                    path=None,
                    file_hash=file_hash,
                    previous_payload=tpl if isinstance(tpl, dict) else None,
                )
            except Exception as e:
                logger.warning(
                    "request_blueprint_proposal: cold ingest failed (template={}): {}",
                    template_id,
                    e,
                )

        try:
            proposal = await drafting_agent.build_blueprint_proposal(
                user_id=user_id, template_id=template_id, force_reprofile=True
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: proposal build failed (template={}): {}",
                template_id,
                e,
            )
            proposal = None
        if not proposal:
            logger.warning(
                "request_blueprint_proposal: no proposal produced (template={})",
                template_id,
            )
            return
        if not self._socket_client:
            logger.warning(
                "request_blueprint_proposal: no socket client — cannot emit proposal"
            )
            return
        try:
            await self._socket_client.emit(
                "template_blueprint_proposal", proposal
            )
            logger.success(
                "📤 template_blueprint_proposal (regenerated) emitted "
                "(template={}, status={}, vars={})",
                template_id,
                proposal.get("status"),
                len((proposal.get("blueprint") or {}).get("variables") or []),
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: emit failed (template={}): {}",
                template_id,
                e,
            )

    async def ensure_templates_synced(
        self, user_id: str, *, timeout: Optional[float] = None
    ) -> None:
        """Make sure the in-memory template store is warm for ``user_id``.

        If the store already has templates for the user (delta events keep it
        fresh within a session), this is a no-op. Otherwise it emits
        ``request_template_sync`` and awaits the ``template_sync_response``
        snapshot (resolved in :meth:`on_template_sync_response` only AFTER the
        templates finish ingesting), so a cold cache (e.g. right after an AI
        restart) is fully filled before template matching runs.

        ``timeout`` is ``None`` by default: there is NO hard time cap, because a
        template PDF can take longer than any fixed limit to OCR/ingest, and
        cutting off early made matching run against an empty library. The future
        is always resolved by ``on_template_sync_response`` (even if ingestion
        fails), so this await cannot hang indefinitely. Pass an explicit
        ``timeout`` only if a caller deliberately wants to bound the wait.
        """
        if not user_id or not self._socket_client:
            return
        try:
            from app.langgraph.template_store import template_store

            if await template_store.count_for_user(user_id=user_id) > 0:
                return  # already warm this session
        except Exception:
            pass

        request_id = f"tmpl-{(user_id or '')[:8]}-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_event_loop()
        fut: "asyncio.Future" = loop.create_future()
        self._pending_template_sync[request_id] = fut
        try:
            await self._socket_client.emit(
                "request_template_sync",
                {"userId": user_id, "requestId": request_id},
            )
            logger.info(
                "📨 request_template_sync emitted (user={}, requestId={})",
                user_id[:12],
                request_id,
            )
            if timeout is None:
                # No hard time cap — wait until templates are fully ingested.
                await fut
            else:
                await asyncio.wait_for(fut, timeout=timeout)
            logger.info(
                "✅ template snapshot received for user={} (requestId={})",
                user_id[:12],
                request_id,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "request_template_sync timed out after {}s (user={}) — "
                "proceeding with current store",
                timeout,
                user_id[:12],
            )
        except Exception as e:
            logger.warning(
                "request_template_sync failed (user={}): {}", user_id[:12], e
            )
        finally:
            self._pending_template_sync.pop(request_id, None)
