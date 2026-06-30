"""Relevant Parties extraction and role classification.

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


from app.prompts.relevant_parties_prompts import (
    classify_relevant_parties_prompt,
)


class RelevantPartiesMixin:
    """Relevant Parties extraction and role classification."""

    @staticmethod
    def _party_name_key(name: str) -> str:
        """Normalized key for deduping/matching party names."""
        return re.sub(r"\s+", " ", (name or "").strip().lower()).strip(" .,")

    def _normalize_relevant_parties_payload(self, raw: Any) -> Dict[str, Any]:
        """Coerce roles to the enum, dedupe by name, cap, stamp ids, scrub brand."""
        items_raw: Any
        if isinstance(raw, dict):
            items_raw = raw.get("parties") or raw.get("relevantParties")
        else:
            items_raw = raw
        if not isinstance(items_raw, list):
            items_raw = []

        out: List[Dict[str, Any]] = []
        seen: set = set()
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            # `details` is the contract field; fall back to legacy `description`.
            details = str(item.get("details") or item.get("description") or "").strip()
            if not name:
                continue
            key = self._party_name_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "id": f"party_{len(out) + 1}",
                    "name": name,
                    "role": self._normalize_party_role(item.get("role")),
                    "details": details,
                    # PII — empty on auto-gen; only the user fills these in via
                    # the add/edit_relevant_party panel-edit events.
                    "contactNumber": str(item.get("contactNumber") or "").strip(),
                    "email": str(item.get("email") or "").strip(),
                    "officialIdUrl": str(item.get("officialIdUrl") or "").strip(),
                }
            )
            if len(out) >= 30:
                break
        return scrub_app_brand({"parties": out})

    async def _classify_relevant_parties(
        self,
        workspace_role: str,
        raw_parties: List[Dict[str, Any]],
        evidence_snippet: str,
    ) -> Dict[str, Any]:
        """Classify already-extracted parties into the normalized role enum."""
        from app.ai.gemini_client import gemini_client

        if not raw_parties:
            return {"parties": []}

        listing = []
        for idx, p in enumerate(raw_parties, start=1):
            if not isinstance(p, dict):
                continue
            listing.append(
                f"{idx}. name={p.get('name','')} | designation={p.get('designation','')} "
                f"| role_in_matter={p.get('role_in_matter','')} "
                f"| first_mention={p.get('first_mention_doc','')}"
            )
        parties_blob = "\n".join(listing)

        prompt = classify_relevant_parties_prompt(evidence_snippet=evidence_snippet, parties_blob=parties_blob, workspace_role=workspace_role)
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.1,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        text = self._prepare_json_candidate(raw or "", prefer="object") or ""
        parsed: Dict[str, Any] = {"parties": []}
        try:
            parsed = json.loads(text)
        except Exception:
            logger.warning("Relevant parties: failed to parse classification JSON")
            parsed = {"parties": []}
        return self._normalize_relevant_parties_payload(parsed)

    def _merge_relevant_parties_with_previous(
        self, workspace_id: str, new_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Union previous + new parties by name; drop user-deleted; preserve edits.

        Previous entries are kept first so any user edits survive a regeneration;
        new (non-duplicate) parties are appended; anything the user explicitly
        deleted is filtered out so it does not silently reappear.
        """

        def _list(payload: Any) -> List[Dict[str, Any]]:
            items = payload.get("parties") if isinstance(payload, dict) else None
            return (
                [p for p in items if isinstance(p, dict)]
                if isinstance(items, list)
                else []
            )

        prev = _list(self._workspace_relevant_parties_payload.get(workspace_id))
        new = _list(new_payload)
        deleted = set(self._workspace_deleted_parties.get(workspace_id) or [])

        merged: List[Dict[str, Any]] = []
        seen: set = set()
        for p in prev + new:
            key = self._party_name_key(p.get("name"))
            if not key or key in seen or key in deleted:
                continue
            seen.add(key)
            # Coerce to the contract shape so legacy-cached entries (old
            # `description`, no PII keys) survive a regeneration consistently.
            merged.append(
                {
                    "id": p.get("id") or "",
                    "name": str(p.get("name") or "").strip(),
                    "role": self._normalize_party_role(p.get("role")),
                    "details": str(
                        p.get("details") or p.get("description") or ""
                    ).strip(),
                    "contactNumber": str(p.get("contactNumber") or "").strip(),
                    "email": str(p.get("email") or "").strip(),
                    "officialIdUrl": str(p.get("officialIdUrl") or "").strip(),
                }
            )
            if len(merged) >= 30:
                break
        for i, p in enumerate(merged):
            p["id"] = f"party_{i + 1}"
        return {"parties": merged}

    async def _generate_and_emit_relevant_parties(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """Generate and emit document_feature_response with featureType relevant_parties."""
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        logger.info(
            "Relevant parties: generation started for workspace {}",
            workspace_id[:12] if workspace_id else "",
        )
        combined = await self._assemble_combined_document_content(workspace_id)
        entities = await self._extract_case_entities(workspace_id, combined)
        raw_parties = entities.get("parties") if isinstance(entities, dict) else []
        signals = await self._extract_case_signals_from_documents(workspace_id, user_id)
        new_payload = await self._classify_relevant_parties(
            role, raw_parties or [], signals.get("evidenceSnippet", "")
        )
        payload = self._merge_relevant_parties_with_previous(workspace_id, new_payload)
        self._workspace_relevant_parties_payload[workspace_id] = payload
        if not self._socket_client:
            return
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "relevant_parties",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ Relevant parties sent to backend ({} parties)",
            len(payload.get("parties", [])),
        )
        await asyncio.sleep(0.1)
