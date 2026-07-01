"""Case Laws (precedent) retrieval, ranking, enrichment.

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
    CaseLawDocumentRequest,
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


from app.prompts.case_laws_prompts import (
    derive_case_law_query_prompt,
    select_case_laws_prompt,
    extract_judgment_digest_prompt,
    classify_and_extract_uploaded_case_law_prompt,
)


class CaseLawsMixin:
    """Case Laws (precedent) retrieval, ranking, enrichment."""

    @staticmethod
    def _court_binding(court: str) -> tuple:
        """Map a court name to (bindingStrength, stars) deterministically.

        Indian precedent hierarchy: Supreme Court binds all courts; a High Court
        binds courts within its state; consumer commissions and tribunals are
        persuasive (the National Commission more so than State/District). This is
        a heuristic label to help a lawyer weigh the precedent at a glance — not
        a substitute for checking jurisdiction.
        """
        c = (court or "").lower()
        if "supreme court" in c:
            return "Binding", 5
        if "high court" in c:
            return "Binding (within state)", 4
        if "national" in c and "commission" in c:  # NCDRC
            return "Highly Persuasive", 4
        if "state" in c and "commission" in c:
            return "Persuasive", 3
        if ("district" in c and ("commission" in c or "forum" in c)) or "forum" in c:
            return "Persuasive", 2
        if "tribunal" in c or "appellate" in c:
            return "Persuasive", 3
        if not c:
            return None, None
        return "Persuasive", 3

    @staticmethod
    def _as_str_list(value: Any, *, limit: int = 8) -> List[str]:
        """Coerce a model field into a clean list of non-empty strings."""
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out = []
        for v in value:
            s = str(v).strip()
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _combine_case_citation(title: str, citation: Optional[str]) -> str:
        """Backend-contract `citation` = "Title, Citation". Idempotent.

        Returns the title alone when no citation is present; the citation alone
        when the title is already embedded in it (so re-applying never double-
        prepends the title); otherwise "Title, Citation".
        """
        title = (title or "").strip()
        raw_cit = (citation or "").strip()
        if not raw_cit:
            return title
        if title and title.lower() not in raw_cit.lower():
            return f"{title}, {raw_cit}"
        return raw_cit

    def _normalize_case_laws_payload(
        self, raw: Any, allowed_urls: Optional[set] = None
    ) -> Dict[str, Any]:
        """Normalize, source-verify, dedupe, and cap a case-laws digest payload.

        When ``allowed_urls`` is provided, any case whose ``sourceUrl`` is not in
        that set is dropped (strict source-verified gate). Survivors are stamped
        ``verified=True``, given a deterministic binding weight from their court,
        deduped by URL, capped, and brand-scrubbed. Rich digest fields (ratio,
        material facts, relief, etc.) are passed through with safe defaults.
        """
        items_raw: Any
        if isinstance(raw, dict):
            items_raw = raw.get("caseLaws") or raw.get("case_laws") or raw.get("cases")
        else:
            items_raw = raw
        if not isinstance(items_raw, list):
            items_raw = []

        def _norm_url(u: str) -> str:
            return (u or "").strip().rstrip("/").lower()

        def _opt(value: Any) -> Optional[str]:
            return str(value or "").strip() or None

        def _reference_name(source_name: Optional[str]) -> str:
            # Human-facing label for the source link, e.g.
            # "Full judgment (Indian Kanoon)"; plain "Full judgment" when the
            # provenance tag is unknown.
            sn = (source_name or "").strip()
            return f"Full judgment ({sn})" if sn else "Full judgment"

        restrict_to_sources = bool(allowed_urls)
        allowed_norm = {_norm_url(u) for u in (allowed_urls or [])}

        out: List[Dict[str, Any]] = []
        seen: set = set()
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            source_url = str(item.get("sourceUrl") or item.get("url") or "").strip()
            summary = str(item.get("summary") or item.get("description") or "").strip()
            if not title or not source_url:
                continue
            key = _norm_url(source_url)
            # Strict source-verified gate: drop anything not grounded in a real
            # retrieved source URL — this is what prevents fabricated citations.
            if restrict_to_sources and key not in allowed_norm:
                continue
            if key in seen:
                continue
            seen.add(key)
            relevance = str(item.get("relevance") or "").strip().lower()
            relevance = {"high": "High", "medium": "Medium", "low": "Low"}.get(
                relevance, "Medium"
            )
            court = _opt(item.get("court"))
            strength, stars = self._court_binding(court or "")
            try:
                confidence = int(item.get("confidence"))
                confidence = max(0, min(100, confidence))
            except (TypeError, ValueError):
                confidence = None
            status = str(item.get("legalStatus") or "").strip()
            # Conform to the backend contract: `citation` carries the full
            # "Title, Citation" string (the separate `title` field is retained).
            out.append(
                {
                    "id": f"caselaw_{len(out) + 1}",
                    "title": title,
                    "citation": self._combine_case_citation(
                        title, _opt(item.get("citation"))
                    ),
                    "equivalentCitation": _opt(item.get("equivalentCitation")),
                    "court": court,
                    "bench": self._as_str_list(item.get("bench"), limit=5),
                    "date": _opt(item.get("date")),
                    "bindingStrength": strength,
                    "bindingStars": stars,
                    "areaOfLaw": self._as_str_list(item.get("areaOfLaw"), limit=6),
                    "legalIssues": self._as_str_list(item.get("legalIssues")),
                    "materialFacts": self._as_str_list(item.get("materialFacts")),
                    "ratio": _opt(item.get("ratio")),
                    "holding": _opt(item.get("holding")),
                    "relief": self._as_str_list(item.get("relief"), limit=6),
                    "summary": summary,
                    "relevance": relevance,
                    "matchedFacts": self._as_str_list(item.get("matchedFacts")),
                    "applicabilityReason": _opt(item.get("applicabilityReason")),
                    "distinguishing": self._as_str_list(item.get("distinguishing")),
                    "adverseImpact": _opt(item.get("adverseImpact")),
                    "confidence": confidence,
                    "sourceUrl": source_url,
                    "sourceName": _opt(item.get("sourceName")),
                    # Backend/FE contract fields for the clickable source link.
                    # Mirror sourceUrl/sourceName; kept alongside them for
                    # back-compat.
                    "referenceUrl": source_url,
                    "referenceName": _reference_name(item.get("sourceName")),
                    "legalStatus": status or "Not independently verified",
                    "verified": True,
                }
            )
            if len(out) >= 8:
                break
        return scrub_app_brand({"caseLaws": out})

    async def _derive_case_law_query(
        self,
        signals: Dict[str, Any],
        objective_context: Optional[str] = None,
    ) -> str:
        """Build a legal-issue search query for precedent retrieval.

        Indian Kanoon matches best on doctrine/cause-of-action/statute/remedy
        terms; a query full of party names, brands, and places returns little
        from Kanoon's judgment index and pushes the pipeline onto noisy web
        sources. This asks the model for a concise issue-focused query with the
        proper nouns stripped, falling back to the raw signals query on failure.
        """
        from app.ai.gemini_client import gemini_client

        base = signals.get("query", "") or signals.get("workspaceTitle", "")
        snippet = signals.get("evidenceSnippet", "") or ""
        if not snippet and not base:
            return base
        prompt = derive_case_law_query_prompt(base=base, objective_context=objective_context, snippet=snippet)
        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=2000,
                response_mime_type="application/json",
            )
            text = self._prepare_json_candidate(raw or "", prefer="object") or ""
            parsed = json.loads(text)
            q = str(parsed.get("query") or "").strip()
            return q or base
        except Exception as e:
            logger.warning("Case laws: query derivation failed ({}); using base", e)
            return base

    async def _generate_case_laws_from_sources(
        self,
        workspace_id: str,
        workspace_role: str,
        signals: Dict[str, Any],
        search_results: List[Dict[str, str]],
        objective_context: Optional[str] = None,
        research_notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Two-pass legal-digest builder.

        Pass 1 (select & compare): the LLM picks the most relevant precedents
        ONLY from the retrieved sources, attaching each to a real source URL and
        producing the case-vs-case comparison fields (relevance, matched facts,
        applicability, distinguishing features, adverse impact, confidence).

        Pass 2 (enrich): for each selected case the actual judgment text is
        fetched and a second LLM pass extracts the judgment-intrinsic digest
        fields (citation, court, bench, issues, material facts, ratio decidendi,
        holding, relief) STRICTLY from that text — so the headnote is grounded,
        not recalled. Court binding weight is derived deterministically and
        good-law status defaults to "not verified".
        """
        if not search_results:
            return {"caseLaws": []}

        allowed_urls = {
            (r.get("url") or "").strip()
            for r in search_results
            if (r.get("url") or "").strip()
        }

        selected = await self._select_case_laws(
            workspace_id=workspace_id,
            workspace_role=workspace_role,
            signals=signals,
            search_results=search_results,
            objective_context=objective_context,
            research_notes=research_notes,
        )
        # Keep only source-backed picks, then bound how many we deep-fetch.
        def _norm(u: str) -> str:
            return (u or "").strip().rstrip("/").lower()

        allowed_norm = {_norm(u) for u in allowed_urls}
        selected = [
            c
            for c in selected
            if isinstance(c, dict) and _norm(c.get("sourceUrl")) in allowed_norm
        ][:6]

        enriched = await self._enrich_case_laws(workspace_role, signals, selected)

        normalized = self._normalize_case_laws_payload(
            {"caseLaws": enriched}, allowed_urls=allowed_urls
        )
        # Quality gate — drop cards that would render half-empty or off-topic:
        #  (a) no judgment-intrinsic substance (enrichment failed, e.g. a
        #      news/blog page that could not be fetched) — show real headnotes
        #      only, never a bare card; and
        #  (b) clearly off-topic picks (Low relevance with low confidence), which
        #      is how an unrelated result like a customs/baggage matter slips in.
        kept: List[Dict[str, Any]] = []
        for c in normalized.get("caseLaws", []):
            has_substance = bool(
                c.get("ratio")
                or c.get("holding")
                or c.get("materialFacts")
                or c.get("legalIssues")
            )
            conf = c.get("confidence")
            off_topic = c.get("relevance") == "Low" and isinstance(conf, int) and conf < 65
            if has_substance and not off_topic:
                kept.append(c)
            else:
                logger.info(
                    "Case laws: dropped {} (substance={}, off_topic={})",
                    (c.get("sourceUrl") or "")[:70],
                    has_substance,
                    off_topic,
                )
        for i, c in enumerate(kept):
            c["id"] = f"caselaw_{i + 1}"
        normalized = {"caseLaws": kept}
        logger.info(
            "Case laws: {} verified digest(s) from {} source(s)",
            len(kept),
            len(search_results),
        )
        return normalized

    async def _select_case_laws(
        self,
        workspace_id: str,
        workspace_role: str,
        signals: Dict[str, Any],
        search_results: List[Dict[str, str]],
        objective_context: Optional[str] = None,
        research_notes: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Pass 1 — pick relevant precedents and compare them to THIS matter."""
        from app.ai.gemini_client import gemini_client

        sources_text = []
        for idx, row in enumerate(search_results, start=1):
            sources_text.append(
                f"{idx}. title={row.get('title','')} | url={row.get('url','')} "
                f"| source={row.get('sourceName','')} | snippet={row.get('snippet','')}"
            )
        source_blob = "\n".join(sources_text)

        objective_block = (
            (
                "CURRENT STRATEGIC OBJECTIVE / CASE PERSPECTIVE — prioritise "
                "precedents that advance this objective:\n" + objective_context.strip()
            )
            if objective_context and objective_context.strip()
            else ""
        )
        research_block = (
            (
                "GROUNDED RESEARCH NOTES (web findings — use to identify the "
                "precedents and their citations, then attach the matching SOURCE "
                "RESULT url below):\n" + research_notes.strip()
            )
            if research_notes and research_notes.strip()
            else ""
        )

        prompt = select_case_laws_prompt(objective_block=objective_block, research_block=research_block, self=self, signals=signals, source_blob=source_blob, workspace_id=workspace_id, workspace_role=workspace_role)
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.2,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        text = self._prepare_json_candidate(raw or "", prefer="object") or ""
        try:
            parsed = json.loads(text)
            items = parsed.get("caseLaws") if isinstance(parsed, dict) else parsed
        except Exception:
            logger.warning("Case laws (select): failed to parse JSON")
            items = []
        return [c for c in (items or []) if isinstance(c, dict)]

    async def _enrich_case_laws(
        self,
        workspace_role: str,
        signals: Dict[str, Any],
        selected: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Pass 2 — fetch each judgment, extract the digest, and reconcile.

        Runs concurrently across the shortlist. The extraction pass also re-reads
        the present matter so the comparison fields (relevance, applicability,
        adverse impact, distinguishing) reflect the REAL holding rather than the
        optimistic pass-1 guess. If a judgment cannot be fetched, the intrinsic
        fields stay empty (the quality gate later drops such bare cards).
        """
        if not selected:
            return []

        async def _enrich_one(case: Dict[str, Any]) -> Dict[str, Any]:
            url = (case.get("sourceUrl") or "").strip()
            text = await fetch_judgment_text(url)
            if not text:
                return case  # keep pass-1 fields; gate drops it if still bare
            extracted = await self._extract_judgment_digest(
                workspace_role, signals, case, text
            )
            merged = dict(case)
            # Judgment-grounded fields win; never overwrite with an empty value.
            for k, v in (extracted or {}).items():
                if v in (None, "", []):
                    continue
                merged[k] = v
            return merged

        return await asyncio.gather(*[_enrich_one(c) for c in selected])

    async def _extract_judgment_digest(
        self,
        workspace_role: str,
        signals: Dict[str, Any],
        case: Dict[str, Any],
        judgment_text: str,
    ) -> Dict[str, Any]:
        """Extract the digest from fetched text AND reconcile the comparison.

        Intrinsic fields are taken STRICTLY from the judgment text; the
        comparison fields are re-assessed against the present matter using the
        REAL holding, so "why relevant" can never contradict the disposition.
        """
        from app.ai.gemini_client import gemini_client

        prompt = extract_judgment_digest_prompt(case=case, judgment_text=judgment_text, signals=signals)
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
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning(
                "Case laws (enrich): failed to parse digest for {}",
                (case.get("sourceUrl") or "")[:80],
            )
            return {}

    def _merge_case_laws_with_previous(
        self,
        workspace_id: str,
        new_payload: Dict[str, Any],
        *,
        max_items: int = 12,
    ) -> Dict[str, Any]:
        """Union previously-cached case laws with the new set, deduped by URL.

        Deterministic (no LLM): cases have a stable identity (their source URL),
        so a re-run after a transcript edit keeps everything the user has seen
        and folds in any genuinely new precedents. Previous entries are kept
        first, in order; new non-duplicate entries are appended; capped + re-id'd.
        """
        def _list(payload: Any) -> List[Dict[str, Any]]:
            items = payload.get("caseLaws") if isinstance(payload, dict) else None
            return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []

        def _key(c: Dict[str, Any]) -> str:
            return (c.get("sourceUrl") or "").strip().rstrip("/").lower()

        prev = _list(self._workspace_case_laws_payload.get(workspace_id))
        new = _list(new_payload)

        merged: List[Dict[str, Any]] = []
        seen: set = set()
        for c in prev + new:
            k = _key(c)
            if not k or k in seen:
                continue
            seen.add(k)
            merged.append(dict(c))
            if len(merged) >= max_items:
                break
        for i, c in enumerate(merged):
            c["id"] = f"caselaw_{i + 1}"
            # Recombine so legacy-cached entries (separate citation) also conform.
            c["citation"] = self._combine_case_citation(
                c.get("title") or "", c.get("citation")
            )
            # Backfill the source-link contract fields for legacy/cached entries
            # produced before referenceUrl/referenceName existed (idempotent).
            if not (c.get("referenceUrl") or "").strip():
                c["referenceUrl"] = c.get("sourceUrl") or ""
            if not (c.get("referenceName") or "").strip():
                sn = (c.get("sourceName") or "").strip()
                c["referenceName"] = f"Full judgment ({sn})" if sn else "Full judgment"
        return {"caseLaws": merged}

    async def _generate_and_emit_case_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        *,
        objective_context: Optional[str] = None,
        merge_with_previous: bool = True,
    ) -> None:
        """Generate and emit document_feature_response with featureType case_laws."""
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        logger.info(
            "Case laws: generation started for workspace {}",
            workspace_id[:12] if workspace_id else "",
        )
        signals = await self._extract_case_signals_from_documents(workspace_id, user_id)
        # Derive a legal-issue-focused query (no party/brand names) so Indian
        # Kanoon returns real judgment pages rather than noisy web articles.
        search_query = await self._derive_case_law_query(signals, objective_context)
        if not search_query:
            search_query = signals.get("query", "")
        search_results, research_notes = await find_case_law_sources(search_query)
        logger.info(
            "Case laws: gathered {} source(s) for workspace {}",
            len(search_results),
            workspace_id[:12] if workspace_id else "",
        )

        new_payload = await self._generate_case_laws_from_sources(
            workspace_id=workspace_id,
            workspace_role=role,
            signals=signals,
            search_results=search_results,
            objective_context=objective_context,
            research_notes=research_notes,
        )
        if merge_with_previous:
            payload = self._merge_case_laws_with_previous(workspace_id, new_payload)
        else:
            payload = new_payload
        self._workspace_case_laws_payload[workspace_id] = payload
        if not self._socket_client:
            return
        n = len(payload.get("caseLaws", []))
        if n == 0:
            # Distinguish "found nothing" from a hard failure so the empty panel
            # is greppable: pair this with the source-count logs above and the
            # per-card drop logs in _generate_case_laws_from_sources.
            logger.warning(
                "Case laws: emitting EMPTY payload for workspace {} (sources={}, "
                "check Kanoon scrape / grounding / judgment-fetch quality gate)",
                workspace_id[:12] if workspace_id else "",
                len(search_results),
            )
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "case_laws",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ Case laws sent to backend ({} cases)",
            n,
        )
        await asyncio.sleep(0.1)

    async def _emit_case_law_document_response(
        self,
        *,
        request_id: Optional[str],
        workspace_id: str,
        user_id: str,
        accepted: bool,
        case_law: Optional[Dict[str, Any]] = None,
        rejection_reason: Optional[str] = None,
    ) -> None:
        """Emit case_law_document_response (AI → Server), echoing requestId."""
        if not self._socket_client:
            return
        body: Dict[str, Any] = {
            "requestId": request_id,
            "workspaceId": workspace_id,
            "userId": user_id,
            "accepted": accepted,
        }
        if accepted and case_law is not None:
            body["caseLaw"] = case_law
        if not accepted:
            body["rejectionReason"] = (
                rejection_reason or "The uploaded document is not a case law."
            )
        await self._socket_client.emit("case_law_document_response", body)
        logger.success(
            "✅ case_law_document_response emitted (accepted={}) requestId={}",
            accepted,
            request_id,
        )

    async def on_case_law_document_request(self, data: Dict[str, Any]) -> None:
        """
        Server → AI: a lawyer uploaded a document (stored in GCS). Fetch it,
        decide whether it is a case law, and reply with
        case_law_document_response echoing requestId. On acceptance the case is
        also merged into the Case Laws panel (featureType=case_laws), mirroring
        on_add_case_law. Any failure still emits a terminal accepted:false reply
        so the request is never left unanswered.
        """
        payload_in = data if isinstance(data, dict) else {}
        try:
            req = CaseLawDocumentRequest(**payload_in)
        except Exception as e:
            logger.warning("case_law_document_request: invalid payload: {}", e)
            await self._emit_case_law_document_response(
                request_id=(
                    payload_in.get("requestId") or payload_in.get("request_id")
                ),
                workspace_id=(
                    payload_in.get("workspaceId")
                    or payload_in.get("workspace_id")
                    or ""
                ),
                user_id=(
                    payload_in.get("userId") or payload_in.get("user_id") or ""
                ),
                accepted=False,
                rejection_reason="Invalid case_law_document_request payload.",
            )
            return

        request_id = req.requestId
        workspace_id = (req.workspaceId or "").strip()
        user_id = (req.userId or "").strip()
        document_url = (req.documentUrl or "").strip()
        document_name = (req.documentName or "").strip() or "case-law-document"
        role = (req.workspaceRole or "ADJUDICATOR").strip().upper()

        logger.info(
            "📄 case_law_document_request: workspace={} doc={} requestId={}",
            workspace_id[:12] if workspace_id else "",
            document_name,
            request_id,
        )

        if not workspace_id or not document_url:
            await self._emit_case_law_document_response(
                request_id=request_id,
                workspace_id=workspace_id,
                user_id=user_id,
                accepted=False,
                rejection_reason="Missing workspaceId or documentUrl.",
            )
            return

        try:
            try:
                await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
            except Exception as e:  # pragma: no cover - best-effort
                logger.debug(
                    "case_law_document_request: hydration failed (workspace={}): {}",
                    workspace_id,
                    e,
                )

            # 1) Fetch + extract text from the signed URL (PII-redacted; no vector
            # store / no scoring side effects — same path as chat attachments).
            text = await document_processor.download_and_extract_text(
                document_url, document_name
            )
            if not text or len(text.strip()) < 100:
                await self._emit_case_law_document_response(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    accepted=False,
                    rejection_reason=(
                        "The uploaded document could not be read or contains too "
                        "little text to assess as a case law."
                    ),
                )
                return

            # 2) One LLM pass: classify (is it a case law?) + extract the digest.
            from app.ai.gemini_client import gemini_client

            signals = await self._extract_case_signals_from_documents(
                workspace_id, user_id
            )
            prompt = classify_and_extract_uploaded_case_law_prompt(
                judgment_text=text, signals=signals
            )
            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.1,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            parsed_text = (
                self._prepare_json_candidate(raw or "", prefer="object") or ""
            )
            try:
                parsed = json.loads(parsed_text)
            except Exception:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}

            is_case_law = bool(parsed.get("isCaseLaw"))
            case = (
                parsed.get("caseLaw")
                if isinstance(parsed.get("caseLaw"), dict)
                else {}
            )

            if not is_case_law or not case:
                reason = (parsed.get("rejectionReason") or "").strip() or (
                    "The uploaded document does not appear to be a judicial "
                    "judgment or legal precedent."
                )
                await self._emit_case_law_document_response(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    accepted=False,
                    rejection_reason=reason,
                )
                return

            # 3) Resolve a stable public source URL from the extracted citation;
            # fall back to the (expiring) upload URL when none resolves.
            citation = (case.get("citation") or case.get("title") or "").strip()
            reference_url = document_url
            source_name = "Uploaded document"
            if citation:
                try:
                    sources, _ = await find_case_law_sources(citation, limit=3)
                except Exception as e:
                    logger.debug(
                        "case_law_document_request: source resolve failed: {}", e
                    )
                    sources = []
                for row in sources or []:
                    resolved = (row.get("url") or "").strip()
                    if resolved:
                        reference_url = resolved
                        source_name = (
                            row.get("sourceName") or ""
                        ).strip() or "Indian Kanoon"
                        break

            # 4) Normalize (trusted upload — no strict allowed_urls gate). This
            # stamps referenceUrl/referenceName and the rich digest fields.
            case_item = dict(case)
            case_item["sourceUrl"] = reference_url
            case_item["sourceName"] = source_name
            normalized = self._normalize_case_laws_payload({"caseLaws": [case_item]})
            items = normalized.get("caseLaws", [])
            if not items:
                await self._emit_case_law_document_response(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    accepted=False,
                    rejection_reason=(
                        "The uploaded document was recognised as a case law but "
                        "could not be digested into a usable precedent."
                    ),
                )
                return
            item = items[0]

            # 5) Merge into the panel + re-emit (same as on_add_case_law).
            merged = self._merge_case_laws_with_previous(
                workspace_id, {"caseLaws": items}
            )
            self._workspace_case_laws_payload[workspace_id] = merged
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "case_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": merged,
                    },
                )
                logger.success(
                    "✅ case_law_document_request: appended 1 case; panel now has "
                    "{} (workspace={})",
                    len(merged.get("caseLaws", [])),
                    workspace_id[:12] if workspace_id else "",
                )

            # 6) Reply with the minimal contract item.
            await self._emit_case_law_document_response(
                request_id=request_id,
                workspace_id=workspace_id,
                user_id=user_id,
                accepted=True,
                case_law={
                    "citation": item.get("citation"),
                    "court": item.get("court"),
                    "summary": item.get("summary"),
                    "relevance": item.get("relevance") or "Medium",
                    "referenceUrl": item.get("referenceUrl"),
                    "referenceName": item.get("referenceName"),
                },
            )
        except Exception as e:
            logger.warning("case_law_document_request failed: {}", e)
            logger.exception(e)
            await self._emit_case_law_document_response(
                request_id=request_id,
                workspace_id=workspace_id,
                user_id=user_id,
                accepted=False,
                rejection_reason="Failed to process the uploaded document.",
            )
