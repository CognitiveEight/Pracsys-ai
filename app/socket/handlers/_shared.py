"""Shared JSON-parsing helpers used across features.

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
    _mint_item_id,
    _normalize_match_text,
    _strip_strength_point_statute_boilerplate,
    _annexure_wording_for_next_steps,
    _clamp_annexure_references,
    _format_document_for_prompt,
    _normalize_report_mobile_from_overview,
    _overview_conversation_date_to_iso,
    extract_overview_conversation_report_contact_meta,
)


class JsonHelpersMixin:
    """Shared JSON-parsing helpers used across features."""

    def _strip_markdown_json_fences(self, text: str) -> str:
        cleaned = (text or "").strip()
        if "```" not in cleaned:
            return cleaned
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, flags=re.I)
        if m:
            return (m.group(1) or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        return cleaned.strip()

    def _extract_balanced_json_fragment(
        self, text: str, open_char: str
    ) -> Optional[str]:
        if not text:
            return None
        close_char = "}" if open_char == "{" else "]"
        start = text.find(open_char)
        if start < 0:
            return None
        depth = 0
        i = start
        in_string = False
        escape = False
        quote = '"'
        while i < len(text):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == quote:
                    in_string = False
                i += 1
                continue
            if c == '"' or c == "'":
                in_string = True
                quote = c
                i += 1
                continue
            if c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return text[start:].strip() if start >= 0 else None

    def _prepare_json_candidate(
        self, raw_text: str, prefer: str = "object"
    ) -> Optional[str]:
        text = self._strip_markdown_json_fences(raw_text)
        if not text:
            return None

        open_char = "{" if prefer == "object" else "["
        candidate = self._extract_balanced_json_fragment(text, open_char)
        if not candidate:
            fallback_char = "[" if open_char == "{" else "{"
            candidate = self._extract_balanced_json_fragment(text, fallback_char)
        if not candidate:
            return None

        # Repair common truncation by balancing quotes/brackets/braces.
        unescaped_quotes = len(re.findall(r'(?<!\\)"', candidate))
        if unescaped_quotes % 2 != 0:
            candidate += '"'
        open_brackets = candidate.count("[") - candidate.count("]")
        open_braces = candidate.count("{") - candidate.count("}")
        if open_brackets > 0:
            candidate += "]" * open_brackets
        if open_braces > 0:
            candidate += "}" * open_braces
        candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
        return candidate.strip()

    # ------------------------------------------------------------------
    # currentState reconciliation
    #
    # The frontend echoes the previously-emitted, user-curated panel state
    # back on workspace_sync (see schemas.WorkspaceCurrentState). When a panel
    # regenerates, each fresh item is tagged so the frontend knows what carried
    # over vs what is new:
    #   - a fresh item whose CONTENT matches a previous item -> isMatched=True,
    #     and it CARRIES the previous item's id;
    #   - a brand-new fresh item -> isMatched=False, server mints a new id;
    #   - previous-only items the fresh run did not regenerate are DROPPED.
    # Matching is by text/content only; the curation ``status`` is ignored.
    # ------------------------------------------------------------------

    async def _match_items_to_previous(
        self,
        fresh_texts: List[str],
        prev_pool: List[Dict[str, Any]],
        *,
        item_type: str,
        enable_llm: bool = True,
    ) -> Dict[int, str]:
        """Map fresh item indexes to a previous item's id, by meaning.

        ``fresh_texts`` — the match text for each fresh item (index == position).
        ``prev_pool``   — previous items, each ``{"id": str, "text": str}``.

        Returns ``{fresh_index: prev_id}`` for the fresh items that matched a
        previous one (each previous id used at most once). A deterministic
        normalized-text pass runs first; any still-unmatched fresh items go
        through one fail-open Gemini semantic pass when ``enable_llm``.
        """
        matches: Dict[int, str] = {}
        if not fresh_texts or not prev_pool:
            return matches

        consumed: Set[str] = set()  # prev ids already claimed
        # 1) Deterministic normalized-text pass.
        prev_norm = [
            (str(p.get("id") or ""), _normalize_match_text(p.get("text")))
            for p in prev_pool
        ]
        for fi, ftext in enumerate(fresh_texts):
            fkey = _normalize_match_text(ftext)
            if not fkey:
                continue
            for pid, pkey in prev_norm:
                if not pid or pid in consumed or not pkey:
                    continue
                if pkey == fkey:
                    matches[fi] = pid
                    consumed.add(pid)
                    break

        # 2) Fail-open LLM semantic pass over the leftovers.
        remaining_fresh = [
            (fi, ftext)
            for fi, ftext in enumerate(fresh_texts)
            if fi not in matches and str(ftext or "").strip()
        ]
        remaining_prev = [
            p for p in prev_pool if str(p.get("id") or "") not in consumed
        ]
        if not enable_llm or not remaining_fresh or not remaining_prev:
            return matches

        try:
            from app.ai.gemini_client import gemini_client
            from app.prompts.objective_change_prompts import (
                llm_match_fresh_against_previous_prompt,
            )

            numbered_fresh = "\n".join(
                f"{local + 1}. {ftext}" for local, (_fi, ftext) in enumerate(remaining_fresh)
            )
            numbered_prev = "\n".join(
                f"{i + 1}. [{p.get('id')}] {p.get('text')}"
                for i, p in enumerate(remaining_prev)
            )
            prompt = llm_match_fresh_against_previous_prompt(
                item_type=item_type,
                numbered_fresh=numbered_fresh,
                numbered_prev=numbered_prev,
            )
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=4000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            pairs = parsed.get("matches") if isinstance(parsed, dict) else None
            valid_prev_ids = {str(p.get("id") or "") for p in remaining_prev}
            for pair in pairs or []:
                try:
                    local = int(pair.get("freshIndex")) - 1
                    pid = str(pair.get("prevId") or "")
                except (TypeError, ValueError):
                    continue
                if local < 0 or local >= len(remaining_fresh):
                    continue
                if not pid or pid in consumed or pid not in valid_prev_ids:
                    continue
                fi = remaining_fresh[local][0]
                if fi in matches:
                    continue
                matches[fi] = pid
                consumed.add(pid)
        except Exception as e:
            logger.debug(
                "currentState semantic match ({}) failed; using deterministic only: {}",
                item_type, e,
            )
        return matches

    async def _reconcile_against_current_state(
        self,
        *,
        fresh: List[str],
        prev_items: Optional[List[Dict[str, Any]]],
        section: str,
        text_key: str = "text",
        enable_llm: bool = True,
    ) -> List[Dict[str, Any]]:
        """Tag a fresh text list against previously-curated items.

        Returns ``[{id, text, isMatched}]`` in fresh order. Matched items carry
        the previous id (``isMatched=True``); new items get a freshly minted id
        (``isMatched=False``). Previous-only items are dropped. When
        ``prev_items`` is empty/None (e.g. first workspace_created, or no
        currentState) every item is emitted new with a minted id — so the shape
        is uniform from the very first emit.
        """
        fresh_clean = [
            s.strip() for s in (fresh or []) if isinstance(s, str) and s.strip()
        ]
        prev_pool: List[Dict[str, Any]] = []
        for p in prev_items or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            ptext = p.get(text_key) or p.get("text") or ""
            if pid and str(ptext).strip():
                prev_pool.append({"id": pid, "text": str(ptext).strip()})

        matches = await self._match_items_to_previous(
            fresh_clean, prev_pool, item_type=section, enable_llm=enable_llm
        )
        out: List[Dict[str, Any]] = []
        for fi, text in enumerate(fresh_clean):
            prev_id = matches.get(fi)
            if prev_id:
                out.append({"id": prev_id, "text": text, "isMatched": True})
            else:
                out.append(
                    {"id": _mint_item_id(section), "text": text, "isMatched": False}
                )
        return out
