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

        async def _runner() -> None:
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
                )
            except Exception as laws_err:
                logger.warning(
                    "Applicable laws (background{}) failed: {}",
                    ctx,
                    laws_err,
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
                )
            except Exception as case_err:
                logger.warning(
                    "Case laws (background{}) failed: {}",
                    ctx,
                    case_err,
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

        try:
            asyncio.create_task(_runner())
            logger.debug(
                "Scheduled list_of_dates + applicable_laws + procedural_timeline + drafting_suggestions{} for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule timeline/laws task{}: {}",
                ctx,
                rt_err,
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
            asyncio.create_task(_runner())
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
        payload: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "featureTypes": features,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if user_id:
            payload["userId"] = user_id
        try:
            await self._socket_client.emit("workspace:refresh", payload)
            logger.info(
                "workspace:refresh emitted (reason={}, features={}) for workspace {}",
                reason,
                len(features),
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
            prompt = f"""You are a strict semantic deduplicator.
Given the numbered {item_type} list below, remove duplicates by MEANING (not wording).
If two or more items mean the same thing, keep only the best, most specific one.
Preserve distinct items even if related.

Return ONLY JSON:
{{"keepIndexes":[1,2,5]}}

Rules:
- keepIndexes must be 1-based indexes from the list.
- No duplicates in keepIndexes.
- Keep original order.
- Keep at most {max_keep} items.

{item_type.upper()}:
{numbered}
"""
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
            prompt = f"""You are merging two versions of a {item_type} list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved verbatim, in original order):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new items):
{numbered_new}

YOUR TASK: produce the cumulative list the user should see.

RULES (apply strictly):
1. Keep EVERY item from PREVIOUS verbatim, in its original order.
2. From NEW: append an item ONLY if it expresses a genuinely DIFFERENT meaning from EVERY item already in PREVIOUS.
3. Treat as DUPLICATES (drop the NEW item, do not append):
     - Wording variations of the same concept ("Name of the Petitioner's bank" vs "Name of the Petitioner's bank and specific branch").
     - Paraphrased restatements of the same fact / requirement / risk.
     - Same underlying entity / document / event with slightly different framing.
4. KEEP as new (do append) only when the NEW item refers to a clearly distinct object, requirement, or concern not already covered by any PREVIOUS item.
5. Do NOT merge items together, do NOT rewrite anything — just preserve PREVIOUS verbatim and append the genuine novelties from NEW.
6. Cap the final list at {max_keep} items. If more would qualify, drop the lowest-priority ones from NEW.

Return ONLY this JSON, no commentary:
{{"merged":["...","..."]}}
"""
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
                "LLM merge ({}): prev={} new={} → merged={}",
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
            prompt = f"""You are merging two versions of a CASE STRENGTHS list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new strengths):
{numbered_new}

YOUR TASK: produce the cumulative strengths list using indexes:
- Output an array of items, each with a "source" ("prev" or "new") and "index" (1-based).
- Include EVERY item from PREVIOUS (in order), as ``{{"source":"prev","index":N}}``.
- After all PREVIOUS items, append NEW items as ``{{"source":"new","index":M}}`` ONLY if they express a genuinely DIFFERENT strength from EVERY item already in PREVIOUS.
- Drop NEW items that are paraphrased duplicates of any PREVIOUS item.
- Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
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
                "LLM merge (strengths): prev={} new={} → merged={}",
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
            prompt = f"""You are merging two versions of a {item_type} timeline from regenerations of the same legal case.

Each entry is formatted as ``<date> — <title> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new events):
{numbered_new}

YOUR TASK: produce the cumulative timeline by reference, NOT by rewriting.

RULES (apply strictly):
1. Include EVERY item from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append items from NEW as ``{{"source":"new","index":M}}`` ONLY when they describe a genuinely DIFFERENT event from every PREVIOUS item.

3. **DESCRIPTION is a primary duplicate signal** — weight it equally with title.
   Two entries on the SAME date (or within ~3 days) are DUPLICATES when their
   descriptions describe the same action/observation, EVEN IF the titles are
   worded differently. The model writing each version may pick different
   titles ("Marked as Absconded" vs "Alleged Absconding Date") for the same
   underlying fact — do not treat the title difference as evidence of distinct
   events when the descriptions are about the same thing.

   Two descriptions are "the same action/observation" when they reference:
     • the same actor (the company / the bank / the court / the petitioner),
     • the same action (marked, signed, filed, deposited, dismissed), and
     • the same object/target (the employee, the contract, the petition).
   Allowed paraphrasing: a specific name vs a generic referent
   ("Yogesh" ↔ "the employee"), tense ("marked" ↔ "has marked"), and
   trailing date phrasing ("on February 21st" ↔ "" when the entry's date is
   already 2026-02-21).

   **Worked example (drop the NEW one):**
     PREVIOUS: ``2026-02-21 — Marked as Absconded — The company marked Yogesh as absconded after his visit on February 21st.``
     NEW:      ``2026-02-21 — Alleged Absconding Date — The company marked the employee as absconded after his visit.``
     → Same date, same actor (company), same action (marked absconded), same
       target (Yogesh = "the employee"). DUPLICATE. Drop the NEW entry.

4. **KEEP genuinely distinct events** even when they share a date — multiple
   legal events can occur on the same day. Two entries on the same date are
   distinct when their descriptions reference different actors, actions, OR
   objects (e.g. "Bank notice issued" + "Reply filed" on the same day are two
   different events).

5. Other duplicate patterns to drop:
     - "01-04-2020 Initial Plot Booking" vs "01-04-2020 First booking made".
     - Same legal filing / hearing / deadline on the same or adjacent date
       phrased with different wording.
     - The same notice / order / payment described with title variations.

6. Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
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
                "LLM merge ({}): prev={} new={} → merged={}",
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

            prompt = f"""You are filtering a chronological list of dates extracted from a legal case file. Each entry has a date, a short title, and a one-sentence description.

Classify each entry as ONE of:
  - "factual"     : a real-world event that actually happened to or between
                    the parties (incident, accident, agreement signed, payment
                    made, marriage, separation, employment joining or quitting,
                    document handed over, transaction, communication exchanged,
                    medical examination, FIR registered as an event, site
                    inspection, possession delay, fraud transaction, etc.).
                    Anything a lawyer would mention when narrating "the story
                    of what happened" to a colleague.
  - "procedural"  : a lawyer-side / court-side / system-side milestone
                    (court filing, hearing, adjournment, judgment, summons,
                    legal / demand / statutory notice issuance, response
                    deadline, filing deadline, evidence deadline, compliance
                    deadline, limitation period, lawyer or client consultation,
                    mediation session, scheduled meeting, "case initiation",
                    report due, period of judicial inactivity, judge absent,
                    document expiry as a deadline). Anything that belongs on
                    a separate Procedural Timeline panel rather than the
                    factual narrative.

Apply general semantic judgment — do NOT rely on specific keywords. The same
entry may be procedural in one case and factual in another:
  - "Visa Expiry" is FACTUAL only when the dispute is itself about visa
    overstay / immigration; otherwise it's procedural / metadata.
  - "Notice Sent" is procedural; "Letter from employer demanding meeting"
    is factual communication.
  - "Marriage" is factual; "Marriage Certificate Filed in Court" is
    procedural.
  - "Application Submitted" is procedural (a filing).

If the entry's title is a vague placeholder ("Date Mentioned", "Event",
"Case Date") with no concrete event content, classify as "procedural" so it
gets dropped.

Return ONLY this JSON, no commentary:
{{"classifications":["factual","procedural", ...]}}

The array MUST have EXACTLY {len(entries)} entries, in the same order as
the numbered list below.

Entries:
{numbered}
"""
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
                    logger.info(
                        "List of dates: LLM classifier dropped procedural entry "
                        "(title={!r}, eventType={!r}, date={})",
                        str(entry.get(title_field) or ""),
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
            "Dedupe ({}): {} entries → {} after per-date semantic dedup",
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
            prompt = f"""You are merging two versions of an APPLICABLE LAWS list from regenerations of the same Indian legal case.

Each entry is formatted as ``<Act> <Section> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new applicable provisions):
{numbered_new}

YOUR TASK: produce the cumulative applicable-laws list using indexes.

RULES (apply strictly):
1. Include EVERY entry from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append entries from NEW as ``{{"source":"new","index":M}}`` ONLY when they refer to a DIFFERENT statutory provision than every PREVIOUS entry.
3. Treat as DUPLICATES (drop the NEW item, do NOT append):
     - Same Act + same Section number, even if description is rephrased
       ("BNS Section 85 — cruelty by husband" vs "Bharatiya Nyaya Sanhita Section 85 — wife abuse").
     - Different formal name for the same provision (e.g. "BNS Sec. 85" and "BNS s.85").
     - Description rephrasing of an already-included section.
4. KEEP genuinely distinct sections, even from the same Act ("BNS Section 85" and "BNS Section 318" are different provisions).
5. Cap at {max_laws} entries total. Drop the lowest-priority NEW entries first if needed.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
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
                "LLM merge (applicable laws): prev={} new={} → merged={}",
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
    ) -> None:
        ctx = log_context or " (objective change)"

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
        new_analysis = await self._generate_analysis_report_from_documents(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=workspace_role,
            batch_result=None,
            is_update=False,
        )
        if not (isinstance(new_analysis, dict) and new_analysis):
            new_analysis = self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )

        self._workspace_analysis_payload[workspace_id] = new_analysis
        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "analysis_report",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": new_analysis,
                },
            )
            logger.success(
                "✅ Objective-change analysis update emitted "
                "(fully regenerated for new objective, prev replaced){}",
                ctx,
            )
            # Fresh objective-aligned report is emitted; lift suppression so future emits are allowed.
            self._suppress_analysis_emit_until_ts.pop(workspace_id, None)

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
        if not relevance_done:
            preserved_dates = (
                self._workspace_list_of_dates_payload.get(workspace_id) or []
            )
            if not isinstance(preserved_dates, list):
                preserved_dates = []
            self._workspace_list_of_dates_payload[workspace_id] = preserved_dates
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": preserved_dates,
                        },
                    },
                )
                logger.success(
                    "✅ Objective-change list_of_dates preserved (relevance re-eval skipped){}",
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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "applicable_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": preserved_laws,
                    },
                )
                logger.success(
                    "✅ Objective-change applicable_laws preserved (regeneration skipped){}",
                    ctx,
                )

        # 3b) Case laws: REGENERATE for the new objective/perspective — the
        # objective steers the precedent search + selection, and
        # merge_with_previous=False replaces stale cases. Preserve-and-re-emit
        # on failure so the panel never goes blank.
        cases_done = False
        try:
            await self._generate_and_emit_case_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                objective_context=new_objective_text or None,
                merge_with_previous=False,
            )
            cases_done = True
        except Exception as case_err:
            logger.warning("Case laws regeneration failed{}: {}", ctx, case_err)
        if not cases_done:
            preserved_cases = self._workspace_case_laws_payload.get(workspace_id) or {}
            if not isinstance(preserved_cases, dict):
                preserved_cases = {}
            self._workspace_case_laws_payload[workspace_id] = preserved_cases
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "case_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": preserved_cases,
                    },
                )
                logger.success(
                    "✅ Objective-change case_laws preserved (regeneration skipped){}",
                    ctx,
                )

        # 4) Upcoming dates (procedural timeline): preserve existing payload on
        # objective change — same preserve-and-re-emit pattern as list_of_dates /
        # applicable_laws above. Only regenerate the timeline when documents are
        # uploaded/deleted, never on an objective change.
        preserved_timeline = (
            self._workspace_procedural_timeline_payload.get(workspace_id) or []
        )
        if not isinstance(preserved_timeline, list):
            preserved_timeline = []
        self._workspace_procedural_timeline_payload[workspace_id] = preserved_timeline
        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "procedural_timeline",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": {"proceduralTimeline": preserved_timeline},
                },
            )
            logger.success(
                "✅ Objective-change preserved procedural_timeline re-emitted{}",
                ctx,
            )

        # 5) Drafting Assistant suggested drafts: REGENERATE for the new objective
        # (Mode 2). It rebuilds its brief from the now-updated caches (case
        # summary + regenerated strategy/objective + analysis + applicable laws +
        # dates), so the suggested drafts realign with the new perspective.
        try:
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
            logger.success(
                "✅ Objective-change drafting suggestions regenerated{}", ctx
            )
        except Exception as draft_err:
            logger.warning(
                "Objective-change drafting regeneration failed{}: {}",
                ctx,
                draft_err,
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

        async def _runner() -> None:
            try:
                await self._run_objective_change_incremental_refresh(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                    log_context=ctx,
                )
            except Exception as rean_err:
                logger.warning(
                    "Objective-change incremental refresh{} failed: {}",
                    ctx,
                    rean_err,
                )

        try:
            asyncio.create_task(_runner())
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
            strategies_result = await self._generate_strategies_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=role,
                batch_result=batch_result,
                is_update=is_update,
            )
            if strategies_result and self._socket_client:
                logger.success("✅ Strategies generated (background), emitting...")
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
                chips = strategies_result.get("suggestionChips") or []
                if chips:
                    self._workspace_suggestion_chips[workspace_id] = list(chips)
                    self._last_workspace_id_with_chips = workspace_id
                logger.success("🎉 Strategy & Objectives sent to backend (background)")
        except Exception as strategy_error:
            logger.error("Background strategy generation failed: {}", strategy_error)
            logger.exception(strategy_error)
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
            asyncio.create_task(_runner())
            logger.info(
                "Scheduled background strategy + analysis + timeline/laws for workspace {}",
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as e:
            logger.error("Could not schedule strategy/analysis pipeline: {}", e)

    async def _emit_analysis_report(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False,
        log_context: str = "",
    ) -> bool:
        """
        Generate analysis report (or use fallback on error), then emit to backend and cache.
        Emit is done outside try/except so it always runs and errors propagate.
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
        # Merge with the previously cached version when this is a regeneration
        # (audio re-uploaded, transcript edited, sync after add/edit). Without
        # this, the FE sees v1 from the first emit AND v2 from the second emit
        # with overlapping items — e.g. "Name of the Petitioner's bank and
        # specific branch" (v1) plus "Name of the Petitioner's bank" (v2).
        # The merge unions prev + new and runs LLM semantic dedup so each
        # emitted report is the cumulative state with no semantic duplicates,
        # rejection memory preserved, and identity/score from prev kept.
        if is_update and isinstance(analysis_report, dict):
            try:
                analysis_report = await self._merge_analysis_report_with_previous(
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    new_analysis=analysis_report,
                    log_context=log_context or " (emit)",
                )
            except Exception as merge_err:
                logger.warning(
                    "Analysis report merge with previous failed{}: {}; "
                    "emitting unmerged",
                    log_context, merge_err,
                )
        # Guard: if objective-change refresh is active, skip stale background emits
        # so workspace sees only objective-aligned analysis report.
        suppress_until = self._suppress_analysis_emit_until_ts.get(workspace_id, 0.0)
        if suppress_until and time.time() < suppress_until:
            logger.info(
                "Skipping stale analysis_report emit for workspace {} (suppressed for {:.1f}s more){}",
                workspace_id[:12] if workspace_id else "",
                max(0.0, suppress_until - time.time()),
                log_context,
            )
            return False
        # Cache and set pending re-emit before emit so re-emit on reconnect has data if emit fails
        self._workspace_analysis_payload[workspace_id] = analysis_report
        self._pending_reemit_workspace_id = workspace_id
        self._pending_reemit_user_id = user_id
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "analysis_report",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": analysis_report,
            },
        )
        logger.success("✅ Analysis report sent to backend{}", log_context)
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
        # List of Dates + Applicable laws: background so intelligence chat can proceed
        self._schedule_list_of_dates_and_applicable_laws(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=workspace_role,
            batch_result=batch_result,
            log_context=log_context or " (after analysis)",
        )
        return True

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
            # Merge with the previous cached version: drops items the user
            # already saw, preserves rejection memory, keeps identity + score.
            # Without this, a transcript-edit re-upload re-emits the prev
            # missingInfo / weaknesses / risks alongside any new ones, doubling
            # up the list visible in the UI.
            if isinstance(analysis_report, dict):
                analysis_report = await self._merge_analysis_report_with_previous(
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    new_analysis=analysis_report,
                    log_context=" (sync)",
                )
            if self._socket_client and analysis_report:
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
                logger.success("✅ Analysis report sent to backend (sync)")
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
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": fallback,
                        },
                    )
                    self._workspace_analysis_payload[workspace_id] = fallback
                    logger.success(
                        "✅ Analysis report (fallback) sent to backend (sync)"
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
