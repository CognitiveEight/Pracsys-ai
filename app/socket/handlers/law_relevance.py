"""Lawyer-selected applicable-law relevance assessment.

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


class LawRelevanceMixin:
    """Lawyer-selected applicable-law relevance assessment."""

    def _normalize_manual_law_relevance(self, raw: Optional[str]) -> str:
        """Map model output to High, Medium, Low, or not relevant."""
        if not raw:
            return "not relevant"
        s = str(raw).strip().lower()
        if s in ("high", "h"):
            return "High"
        if s in ("medium", "med", "m"):
            return "Medium"
        if s in ("low", "l"):
            return "Low"
        if any(
            x in s
            for x in (
                "not relevant",
                "not applicable",
                "irrelevant",
                "none",
                "no relevance",
                "n/a",
            )
        ):
            return "not relevant"
        # Title-case unknown strings for optional freeform per spec
        return str(raw).strip() or "not relevant"

    async def _document_snippet_for_law_relevance(
        self, workspace_id: str, max_chars: int = 12000
    ) -> str:
        """Concatenate recent document text for relevance assessment."""
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("applicable_law_relevance: vector store read failed: {}", e)
            all_docs = []
        parts: List[str] = []
        for doc in all_docs[:8]:
            content = str(doc.get("content") or "").strip()
            if len(content) < 20:
                continue
            parts.append(content[:2500])
        joined = "\n\n---\n\n".join(parts)
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n[... truncated ...]"
        return joined or "No document text available for this workspace."

    async def _search_kanoon_for_law_relevance(
        self,
        act: str,
        section: str,
        signals: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """
        Same discovery path as applicable_laws: Indian Kanoon first, then Google CSE to site:indiankanoon.org.
        Queries combine the selected act/section with case keywords from the workspace.
        """
        kw = " ".join((signals.get("keywords") or [])[:6])
        title_hint = (signals.get("workspaceTitle") or "")[:80]
        sec_plain = re.sub(r"^section\s*", "", (section or "").strip(), flags=re.I)
        queries = [
            " ".join(p for p in (act, section, kw) if p).strip(),
            " ".join(p for p in (act, f"Section {sec_plain}", kw) if p).strip(),
            " ".join(p for p in (act, section, title_hint) if p).strip(),
        ]
        seen_urls: set = set()
        merged: List[Dict[str, str]] = []
        for q in queries:
            if not q or len(q.strip()) < 5:
                continue
            logger.info("law_relevance: Indian Kanoon query={}", q[:200])
            batch = await self._search_indiankanoon(q)
            for row in batch:
                u = row.get("url") or ""
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    merged.append(row)
                    if len(merged) >= 10:
                        return merged
            if merged:
                return merged
        fallback_q = queries[0] if queries and queries[0] else f"{act} {section}"
        vertex_rows = await vertex_search_indiankanoon(fallback_q)
        for row in vertex_rows:
            u = row.get("url") or ""
            if u and u not in seen_urls:
                seen_urls.add(u)
                merged.append(row)
                if len(merged) >= 10:
                    break
        logger.info(
            "law_relevance: total search rows after Kanoon/Vertex={}",
            len(merged),
        )
        return merged

    def _merge_law_relevance_description(
        self,
        lawyer_desc: Optional[str],
        rationale: str,
        max_len: int = 1600,
    ) -> Optional[str]:
        """Attach evidence-style rationale from documents + Kanoon to optional lawyer description."""
        r = (rationale or "").strip()
        l = (lawyer_desc or "").strip()
        if r and l:
            out = (
                f"{l}\n\nAssessment (case documents and Indian Kanoon search): {r}"
            )
        elif r:
            out = f"Assessment (case documents and Indian Kanoon search): {r}"
        else:
            out = l
        if len(out) > max_len:
            out = out[: max_len - 3] + "..."
        return out or None

    def _apply_law_relevance_consistency_guards(
        self,
        relevance: str,
        rationale: str,
        search_results: List[Dict[str, str]],
        doc_context: str,
    ) -> Tuple[str, str]:
        """
        High/Medium should rest on substance; Low must still reflect something in the case file.
        """
        r = (rationale or "").strip()
        dc = (doc_context or "").strip()
        has_docs = bool(dc) and "No document text available" not in dc
        has_search = len(search_results) > 0
        weak_rationale = len(r) < 55

        if relevance in ("High", "Medium") and weak_rationale:
            relevance = "Low" if has_docs else "not relevant"
            if not r:
                r = (
                    "Insufficient explicit reasoning; downgraded pending clearer case and search tie-in."
                )
        if relevance == "High":
            if not has_search and not (has_docs and len(dc) > 400):
                relevance = "Medium"
                extra = (
                    " Adjusted to Medium: High confidence needs stronger document detail "
                    "and/or Indian Kanoon support."
                )
                r = (r + extra).strip() if r else extra.strip()
        if relevance == "Low":
            if not has_docs:
                relevance = "not relevant"
                r = "No usable case documents to tie this provision to the matter."
            elif len(r) < 35:
                relevance = "not relevant"
                r = (
                    "Low relevance requires at least one concrete fact from the file; "
                    "none was identified."
                )
        return relevance, r.strip()

    async def _assess_law_relevance_with_kanoon_and_llm(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        act: str,
        section: str,
        title: str,
        desc: str,
        user_note: str,
        doc_context: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Run Indian Kanoon/CSE search like applicable_laws, then score relevance with grounded rationale.
        Returns (relevance label, description text including rationale).
        """
        from app.ai.gemini_client import gemini_client

        signals = await self._extract_case_signals_from_documents(
            workspace_id, user_id
        )
        search_results = await self._search_kanoon_for_law_relevance(
            act, section, signals
        )

        sources_text = []
        for idx, row in enumerate(search_results[:8], start=1):
            sources_text.append(
                f"{idx}. title={row.get('title','')} | url={row.get('url','')} | snippet={row.get('snippet','')}"
            )
        source_blob = (
            "\n".join(sources_text)
            if sources_text
            else "No Indian Kanoon / search snippets retrieved (rely on documents only)."
        )

        prompt = f"""You are a senior Indian lawyer. The user selected ONE statute provision; decide if it fits THIS case.

You MUST use BOTH:
(A) the case document excerpt, and
(B) the Indian Kanoon / case-law search lines (titles/snippets/URLs) when present.

Return exactly one JSON object (no markdown), shape:
{{
  "relevance": "High" | "Medium" | "Low" | "not relevant",
  "rationale": "string, 2–5 short sentences"
}}

SCORING (strict):
- **High**: Document facts clearly engage this act+section, AND search results show this provision or very similar charges/relief in comparable fact patterns (name the pattern briefly). Rationale must name at least one **specific document fact** and one **search-line theme** (or state search was empty but document fit is overwhelming—only if documents are very strong).
- **Medium**: Document facts plausibly engage this provision; search supports or is neutral; some uncertainty. Rationale must cite at least one **concrete document fact** and explain the fit.
- **Low**: Only if the **documents** still contain at least one concrete fact that could trigger this provision, but the fit is weak or peripheral. Do **not** pick Low from search alone. Rationale must quote the weak fact you rely on.
- **not relevant**: Provision does not meaningfully apply, or documents lack any hook, or search+documents show mismatch.

Rules:
- Do NOT fabricate facts not present in the document excerpt.
- Do NOT cite IPC unless the selected act is IPC; prefer BNS framing if overlapping.
- If search block is empty, you may still assign Medium or Low only with strong/slight document hooks respectively; **High** requires either search support or unusually clear statutory fit spelled out in the documents.

Selected law:
- Act: {act}
- Section: {section}
{f"- Title: {title}" if title else ""}
{f"- Prior description: {desc}" if desc else ""}
{f"- Lawyer note: {user_note}" if user_note else ""}

Workspace title / keywords (hint): {signals.get("workspaceTitle","")} | {", ".join(signals.get("keywords") or [])}

Case documents (excerpt):
{doc_context}

Indian Kanoon / search results (titles and snippets):
{source_blob}
"""
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.15,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        text = (raw or "").strip()
        if text.startswith("Error generating"):
            return "not relevant", "Assessment failed (model error)."
        if "```" in text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
            if m:
                text = m.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        rel = "not relevant"
        rationale = ""
        if start >= 0 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
                rel = self._normalize_manual_law_relevance(obj.get("relevance"))
                rationale = str(obj.get("rationale") or "").strip()
            except Exception:
                try:
                    open_braces = text.count("{") - text.count("}")
                    open_brackets = text.count("[") - text.count("]")
                    salvaged = text[start : end + 1] + (
                        "]" * max(0, open_brackets)
                    ) + ("}" * max(0, open_braces))
                    obj = json.loads(salvaged)
                    rel = self._normalize_manual_law_relevance(obj.get("relevance"))
                    rationale = str(obj.get("rationale") or "").strip()
                except Exception:
                    m = re.search(r'"relevance"\s*:\s*"([^"]+)"', text)
                    if m:
                        rel = self._normalize_manual_law_relevance(m.group(1))
        rel, rationale = self._apply_law_relevance_consistency_guards(
            rel, rationale, search_results, doc_context
        )
        merged = self._merge_law_relevance_description(desc, rationale)
        return rel, merged

    def _applicable_law_relevance_law_dict(
        self,
        *,
        law_id: Optional[str],
        act: str,
        section: str,
        title: Optional[str],
        description: Optional[str],
        relevance: str,
    ) -> Dict[str, Any]:
        """Build law object for applicable-law relevance responses (omit empty optionals)."""
        # User-facing label: the UI renders relevance as "<value> Relevance", so
        # the internal "not relevant" value reads as "not relevant Relevance".
        # Emit "No" instead so it displays as "No Relevance". Internal logic and
        # comparisons continue to use the "not relevant" canonical value.
        if str(relevance).strip().lower() == "not relevant":
            relevance = "No"
        out: Dict[str, Any] = {
            "act": act,
            "section": section,
            "relevance": relevance,
        }
        if law_id:
            out["id"] = law_id
        if title:
            out["title"] = title
        if description:
            out["description"] = description
        return out

    async def _emit_applicable_law_relevance_to_backend(
        self,
        workspace_id: str,
        user_id: str,
        request_id: Optional[str],
        law_dict: Dict[str, Any],
    ) -> None:
        """
        Backend integration: many gateways only persist AI output on document_feature_response.
        Emit that first (featureType applicable_law_relevance), then the standalone event for spec parity.
        """
        if not self._socket_client:
            logger.warning("applicable_law_relevance: no socket client; cannot emit")
            return

        payload_inner: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "userId": user_id,
            "law": law_dict,
        }
        if request_id:
            payload_inner["requestId"] = request_id

        doc_feature_body: Dict[str, Any] = {
            "featureType": "applicable_law_relevance",
            "workspaceId": workspace_id,
            "userId": user_id,
            "payload": payload_inner,
        }
        try:
            doc_log = json.dumps(doc_feature_body, default=str)
        except Exception:
            doc_log = str(doc_feature_body)
        logger.info(
            "applicable_law_relevance → backend document_feature_response (preview): {}",
            doc_log[:2000] + ("..." if len(doc_log) > 2000 else ""),
        )
        await self._socket_client.emit("document_feature_response", doc_feature_body)
        await asyncio.sleep(0.1)

        standalone: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "userId": user_id,
            "law": law_dict,
        }
        if request_id:
            standalone["requestId"] = request_id
        try:
            solo_log = json.dumps(standalone, default=str)
        except Exception:
            solo_log = str(standalone)
        logger.info(
            "applicable_law_relevance → backend applicable_law_relevance_response (preview): {}",
            solo_log[:2000] + ("..." if len(solo_log) > 2000 else ""),
        )
        await self._socket_client.emit("applicable_law_relevance_response", standalone)
        logger.success(
            "✅ applicable law relevance delivered to backend (document_feature_response + applicable_law_relevance_response) requestId={} relevance={}",
            request_id,
            law_dict.get("relevance"),
        )

    async def on_applicable_law_relevance_request(self, data: Dict[str, Any]) -> None:
        """
        Server asks whether a lawyer-selected act/section is relevant to the case documents.
        Emits document_feature_response (featureType applicable_law_relevance) and
        applicable_law_relevance_response with relevance: High | Medium | Low | not relevant.
        """
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "applicable_law_relevance_request ← backend (received): keys={}",
            list(payload_in.keys()) if isinstance(payload_in, dict) else type(payload_in),
        )
        try:
            req = ApplicableLawRelevanceRequestPayload(**payload_in)
        except Exception as e:
            logger.warning("applicable_law_relevance_request: invalid payload {}: {}", e, payload_in)
            workspace_id = str(payload_in.get("workspaceId") or payload_in.get("workspace_id") or "")
            user_id = str(payload_in.get("userId") or payload_in.get("user_id") or "")
            request_id_raw = payload_in.get("requestId") or payload_in.get("request_id")
            request_id = str(request_id_raw) if request_id_raw else None
            law_raw = payload_in.get("law") if isinstance(payload_in.get("law"), dict) else {}
            if self._socket_client and workspace_id and user_id:
                law_dict = self._applicable_law_relevance_law_dict(
                    law_id=str(law_raw.get("id")) if law_raw.get("id") else None,
                    act=str(law_raw.get("act") or ""),
                    section=str(law_raw.get("section") or ""),
                    title=str(law_raw.get("title") or "").strip() or None,
                    description=str(law_raw.get("description") or "").strip() or None,
                    relevance="not relevant",
                )
                await self._emit_applicable_law_relevance_to_backend(
                    workspace_id, user_id, request_id, law_dict
                )
            else:
                logger.warning(
                    "applicable_law_relevance_request: cannot respond — missing socket or workspaceId/userId"
                )
            return

        workspace_id = req.workspaceId
        user_id = req.userId
        request_id = req.requestId
        law = req.law
        act = (law.act or "").strip()
        section = (law.section or "").strip()

        logger.info(
            "applicable_law_relevance_request ← backend (parsed): workspaceId={} userId={} requestId={} act={} section={}",
            workspace_id[:24] if workspace_id else "",
            user_id[:24] if user_id else "",
            request_id,
            act[:80] if act else "",
            section[:80] if section else "",
        )

        async def _emit_response(
            relevance: str, description: Optional[str] = None
        ) -> None:
            base_law_desc = (law.description or "").strip() or None
            final_desc = description if description else base_law_desc
            law_dict = self._applicable_law_relevance_law_dict(
                law_id=law.id,
                act=act,
                section=section,
                title=(law.title or "").strip() or None,
                description=final_desc,
                relevance=relevance,
            )
            await self._emit_applicable_law_relevance_to_backend(
                workspace_id, user_id, request_id, law_dict
            )

        if not act or not section:
            await _emit_response("not relevant")
            return

        doc_context = await self._document_snippet_for_law_relevance(workspace_id)
        role = "ADJUDICATOR"
        try:
            ctx = workspace_manager.get_workspace(workspace_id)
            if ctx and getattr(ctx, "workspace_role", None):
                role = str(ctx.workspace_role).strip().upper() or role
        except Exception:
            pass

        ud = (law.userDescription or "").strip()
        title = (law.title or "").strip()
        desc = (law.description or "").strip()
        rel, merged_desc = await self._assess_law_relevance_with_kanoon_and_llm(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=role,
            act=act,
            section=section,
            title=title,
            desc=desc,
            user_note=ud,
            doc_context=doc_context,
        )
        await _emit_response(rel, merged_desc)
