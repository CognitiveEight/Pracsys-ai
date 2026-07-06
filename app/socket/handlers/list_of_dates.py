"""List of Dates (chronology) generation and feedback.

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


from app.prompts.list_of_dates_prompts import (
    regenerate_list_of_dates_with_feedback_prompt,
    reeval_list_of_dates_relevance_prompt,
    generate_list_of_dates_from_documents_prompt,
)


class ListOfDatesMixin:
    """List of Dates (chronology) generation and feedback."""

    async def _regenerate_list_of_dates_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        emit_result: bool = True,
    ) -> bool:
        """
        Apply a surgical find-and-replace / correction to the EXISTING List of
        Dates entries (Mode 1) — a NAME, TERM, DATE, or YEAR change.

        Surgical only: it applies the instruction to each entry's
        title / description / evidenceSnippet (and, for a date/year change, the
        ISO ``date`` field too) and changes NOTHING else — no new or removed
        entries, and relevance / sourceDocId / sourceDocName / eventType /
        pageNumber / isIncident are all preserved. It never regenerates the list
        from documents. Operates on the cached
        ``_workspace_list_of_dates_payload`` and re-emits featureType=list_of_dates.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_list_of_dates_payload.get(workspace_id)
            if not isinstance(current, list) or not current:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = regenerate_list_of_dates_with_feedback_prompt(current_json=current_json, instruction=instruction)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.1,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="array")
            if not json_str:
                return False
            edited = json.loads(json_str)
            # Guardrail: only accept when the entry count is unchanged — a name
            # edit must never add/drop dates. Otherwise keep the cache untouched.
            if not isinstance(edited, list) or len(edited) != len(current):
                logger.warning(
                    "List of dates name edit rejected (count {} != {}); keeping cache",
                    len(edited) if isinstance(edited, list) else "n/a",
                    len(current),
                )
                return False

            edited = scrub_app_brand(edited)
            if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                current, sort_keys=True, ensure_ascii=True
            ):
                return False  # nothing changed

            self._workspace_list_of_dates_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": edited,
                        },
                    },
                )
                logger.success(
                    "✅ List of dates name edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("List of dates name edit failed: {}", e)
            return False

    async def _reeval_list_of_dates_relevance(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        objective_text: str,
        emit_result: bool = True,
    ) -> bool:
        """
        Re-score ONLY the ``relevance`` field of the EXISTING List of Dates
        entries against a changed case objective/perspective (Mode 2). It keeps
        every entry and every other field (date / title / description /
        sourceDocId / eventType / ...) exactly as-is — no new or removed dates,
        no text rewrite. Operates on the cached payload and re-emits.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_list_of_dates_payload.get(workspace_id)
            if not isinstance(current, list) or not current:
                return False
            obj = (objective_text or "").strip()
            if not obj:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = reeval_list_of_dates_relevance_prompt(current_json=current_json, obj=obj)

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="array")
            if not json_str:
                return False
            edited = json.loads(json_str)
            if not isinstance(edited, list) or len(edited) != len(current):
                logger.warning(
                    "List of dates relevance re-eval rejected (count {} != {}); keeping cache",
                    len(edited) if isinstance(edited, list) else "n/a",
                    len(current),
                )
                return False

            edited = scrub_app_brand(edited)
            self._workspace_list_of_dates_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": edited,
                        },
                    },
                )
                logger.success(
                    "✅ List of dates relevance re-evaluated for new objective workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("List of dates relevance re-eval failed: {}", e)
            return False

    async def _generate_list_of_dates_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        all_docs: List[Dict[str, Any]],
        workspace_role: Optional[str] = None,
        batch_result: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extract a chronological list of important dates/events from case documents using AI.
        Returns list of items for payload.listOfDates (date, title, description, sourceDocId, sourceDocName, etc.).
        """
        import json
        import re
        from datetime import datetime

        if not all_docs:
            return []

        # Build document index and names: 1-based index -> document_id, display name
        doc_id_by_index: Dict[int, str] = {}
        doc_name_by_index: Dict[int, str] = {}
        doc_content_by_index: Dict[int, str] = {}
        doc_explicit_dates_by_index: Dict[int, set[str]] = {}
        doc_explicit_years_by_index: Dict[int, set[int]] = {}
        doc_anchor_year_by_index: Dict[int, Optional[int]] = {}
        id_to_name: Dict[str, str] = {}
        if batch_result:
            for r in batch_result.get("results") or []:
                did = r.get("document_id")
                name = r.get("filename") or r.get("originalName") or ""
                if did:
                    id_to_name[str(did)] = name or f"Annexure"

        valid_docs = [
            d
            for d in all_docs
            if (d.get("content") or "").strip()
            and len((d.get("content") or "").strip()) >= 20
        ]
        max_total_content = 100000
        per_doc_cap = (
            (max_total_content // max(1, len(valid_docs)))
            if valid_docs
            else max_total_content
        )
        parts = []
        for i, doc in enumerate(all_docs, 1):
            doc_id = str(doc.get("document_id") or "")
            content = (doc.get("content") or "").strip()
            if not content or len(content) < 20:
                continue
            name = id_to_name.get(doc_id) or f"Annexure {i}"
            doc_id_by_index[i] = doc_id
            doc_name_by_index[i] = name
            doc_content_by_index[i] = content
            if len(content) > per_doc_cap:
                content = content[:per_doc_cap] + "\n[... truncated for length ...]"
            parts.append(
                f"--- Annexure {i} (id: {doc_id}, name: {name}) ---\n{content}"
            )
        combined = "\n\n".join(parts) if parts else ""
        if not combined:
            return []

        if len(combined) > max_total_content:
            combined = combined[:max_total_content] + "\n[... content truncated ...]"

        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            if not gemini_client.model:
                return []

            from datetime import timedelta as _timedelta
            today_dt = datetime.utcnow()
            today_str = today_dt.strftime("%Y-%m-%d")
            tomorrow_str = (today_dt + _timedelta(days=1)).strftime("%Y-%m-%d")
            next_week_str = (today_dt + _timedelta(days=7)).strftime("%Y-%m-%d")
            current_year = today_str[:4]
            list_of_dates_inventory = self._build_document_inventory(workspace_id)
            prompt = generate_list_of_dates_from_documents_prompt(combined=combined, current_year=current_year, list_of_dates_inventory=list_of_dates_inventory, next_week_str=next_week_str, today_str=today_str, tomorrow_str=tomorrow_str)
            # Keep deterministic JSON extraction for timeline completeness.
            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    workspace_role
                ),
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            raw = (raw or "").strip()
            if not raw or raw.startswith("Error"):
                logger.warning("List of dates: Gemini error or empty response")
                return []

            arr = None
            json_str = self._prepare_json_candidate(raw, prefer="array")
            if json_str:
                try:
                    parsed = json.loads(json_str)
                    if isinstance(parsed, list):
                        arr = parsed
                    elif isinstance(parsed, dict):
                        arr = (
                            parsed.get("listOfDates")
                            or parsed.get("dates")
                            or parsed.get("items")
                            or []
                        )
                except json.JSONDecodeError as e:
                    logger.warning(
                        "List of dates: JSON decode error after repair: {}; attempting salvage",
                        e,
                    )

            if arr is None:
                search_region = self._strip_markdown_json_fences(raw)
                start = search_region.find("[")
                if start < 0 and search_region.find("{") < 0:
                    logger.warning(
                        "List of dates: no JSON markers found in response; raw preview={}",
                        (search_region or "")[:150],
                    )
                    return []
                salvage = []
                offset = 0
                for mobj in re.finditer(
                    r'\{\s*"date"\s*:\s*"([^"]+)"\s*,\s*"title"\s*:\s*"([^"]*)"',
                    search_region,
                ):
                    try:
                        # Find the full object: from { to matching }
                        beg = offset + mobj.start()
                        if beg < 0:
                            continue
                        depth_br = 0
                        j = beg
                        in_str = False
                        esc = False
                        q = '"'
                        while j < len(search_region):
                            ch = search_region[j]
                            if esc:
                                esc = False
                                j += 1
                                continue
                            if ch == "\\" and in_str:
                                esc = True
                                j += 1
                                continue
                            if not in_str:
                                if ch == q:
                                    in_str = True
                                elif ch == "{":
                                    depth_br += 1
                                elif ch == "}":
                                    depth_br -= 1
                                    if depth_br == 0:
                                        candidate = search_region[beg : j + 1]
                                        try:
                                            obj = json.loads(candidate)
                                            if isinstance(obj, dict) and obj.get(
                                                "date"
                                            ):
                                                salvage.append(obj)
                                        except json.JSONDecodeError:
                                            pass
                                        break
                            else:
                                if ch == q:
                                    in_str = False
                            j += 1
                    except Exception:
                        pass
                if salvage:
                    arr = salvage
                    logger.info(
                        "List of dates: salvaged {} entries from malformed JSON",
                        len(arr),
                    )
                else:
                    return []
            if not isinstance(arr, list):
                arr = [arr] if isinstance(arr, dict) else []

            if not isinstance(arr, list):
                return []

            result = []
            background_timeline_pattern = re.compile(
                r"\b(founder|born|birth|founded|established|industrialization|hydroelectric|milestone|"
                r"brand history|company history|legacy history|origin story|corporate history)\b",
                flags=re.IGNORECASE,
            )
            # Patterns that indicate scraped UI/navigation/listing-table content
            _garbage_text_pattern = re.compile(
                r"View PDF|"
                r"Date of (Listing|Upload)|"
                r"Listing History|"
                r"Case Status Pending|"
                r"Last Listed|"
                r"Next Listing Date|"
                r"All Parties|"
                r"IA/MA|"
                r"Copyright Policy|"
                r"Feedback|"
                r"Skip to main content|"
                r"Sitemap|"
                r"Hyperlinking Polic|"
                r"Contact Us|"
                r"Screen Reader Access|"
                r"Page last updated|"
                r"Connected Matters|"
                r"Order/Judgement",
                re.I,
            )

            _generic_titles = frozenset(
                (
                    "date mentioned in document",
                    "date mentioned in text",
                    "date in document",
                    "mentioned date",
                    "event",
                    "date",
                )
            )

            # list_of_dates is a FACTUAL panel only — procedural event types
            # (Court Hearing / Filing / Deadline / Meeting / legal Notice) live
            # exclusively on the procedural_timeline panel. The factual types
            # below are the only ones allowed here. Procedural variants are
            # mapped to a sentinel "__PROCEDURAL__" so the entry-loop can drop
            # the entry entirely instead of mis-classifying it as "Other".
            _ALLOWED_EVENT_TYPES = frozenset(
                ("Incident", "Agreement", "Payment", "Communication", "Other")
            )
            _PROCEDURAL_SENTINEL = "__PROCEDURAL__"
            # Map every variant the model might emit (lowercased) to either a
            # canonical factual type or the procedural sentinel.
            _EVENT_TYPE_ALIASES: Dict[str, str] = {
                # ----- Procedural (drop these entries) -----
                "court hearing": _PROCEDURAL_SENTINEL,
                "hearing": _PROCEDURAL_SENTINEL,
                "tribunal hearing": _PROCEDURAL_SENTINEL,
                "hearing date": _PROCEDURAL_SENTINEL,
                "next date": _PROCEDURAL_SENTINEL,
                "adjournment": _PROCEDURAL_SENTINEL,
                "judgment": _PROCEDURAL_SENTINEL,
                "judgement": _PROCEDURAL_SENTINEL,
                "judgment or order": _PROCEDURAL_SENTINEL,
                "judgement or order": _PROCEDURAL_SENTINEL,
                "order": _PROCEDURAL_SENTINEL,
                "court order": _PROCEDURAL_SENTINEL,
                "tribunal order": _PROCEDURAL_SENTINEL,
                "final order": _PROCEDURAL_SENTINEL,
                "interim order": _PROCEDURAL_SENTINEL,
                "filing": _PROCEDURAL_SENTINEL,
                "court filing": _PROCEDURAL_SENTINEL,
                "complaint": _PROCEDURAL_SENTINEL,
                "application": _PROCEDURAL_SENTINEL,
                "appeal": _PROCEDURAL_SENTINEL,
                "deadline": _PROCEDURAL_SENTINEL,
                "due date": _PROCEDURAL_SENTINEL,
                "response deadline": _PROCEDURAL_SENTINEL,
                "filing deadline": _PROCEDURAL_SENTINEL,
                "compliance deadline": _PROCEDURAL_SENTINEL,
                "compliance": _PROCEDURAL_SENTINEL,
                "evidence deadline": _PROCEDURAL_SENTINEL,
                "limitation": _PROCEDURAL_SENTINEL,
                "meeting": _PROCEDURAL_SENTINEL,
                "consultation": _PROCEDURAL_SENTINEL,
                "mediation": _PROCEDURAL_SENTINEL,
                "notice": _PROCEDURAL_SENTINEL,
                "legal notice": _PROCEDURAL_SENTINEL,
                "demand notice": _PROCEDURAL_SENTINEL,
                "statutory notice": _PROCEDURAL_SENTINEL,
                "notice issuance": _PROCEDURAL_SENTINEL,
                "reply": _PROCEDURAL_SENTINEL,
                # ----- Factual (keep) -----
                "incident": "Incident",
                "accident": "Incident",
                "assault": "Incident",
                "fraud": "Incident",
                "dispute": "Incident",
                "agreement": "Agreement",
                "contract": "Agreement",
                "signing": "Agreement",
                "marriage": "Agreement",
                "payment": "Payment",
                "transaction": "Payment",
                "communication": "Communication",
                "email": "Communication",
                "whatsapp": "Communication",
                "letter": "Communication",
                "phone call": "Communication",
                "other": "Other",
                "event": "Other",
                "date": "Other",
            }

            def _normalize_event_type(raw: str) -> str:
                """Map any LLM-returned eventType to one of the allowed canonical
                values OR to ``_PROCEDURAL_SENTINEL`` when the type belongs on
                the procedural_timeline panel and the entry should be dropped."""
                if not raw:
                    return "Other"
                if raw in _ALLOWED_EVENT_TYPES:
                    return raw
                # Procedural Title-Cased values that aren't in the allowed set.
                if raw in {"Court Hearing", "Filing", "Deadline", "Meeting"}:
                    return _PROCEDURAL_SENTINEL
                return _EVENT_TYPE_ALIASES.get(raw.lower(), "Other")

            def _is_garbage_text_local(text: str) -> bool:
                if not text:
                    return False
                if _garbage_text_pattern.search(text):
                    return True
                # Raw date-list dump: 3+ DD-MM-YYYY or DD/MM/YYYY tokens
                if len(re.findall(r"\d{2}[-/]\d{2}[-/]\d{4}", text)) >= 3:
                    return True
                return False

            def _is_background_timeline_entry(
                title: str, description: str, source_name: str
            ) -> bool:
                text = f"{title} {description} {source_name}".strip()
                return bool(background_timeline_pattern.search(text))

            def _extract_page_markers(source_content: str) -> List[Tuple[int, int]]:
                """
                Extract page markers and their positions from OCR/plain text dumps.
                Supports markers like:
                - --- Page 3 ---
                - -- 3 of 15 --
                """
                markers: List[Tuple[int, int]] = []
                if not source_content:
                    return markers
                for m in re.finditer(r"---\s*Page\s+(\d+)\s*---", source_content, re.I):
                    try:
                        markers.append((m.start(), int(m.group(1))))
                    except Exception:
                        continue
                for m in re.finditer(r"--\s*(\d+)\s+of\s+\d+\s*--", source_content, re.I):
                    try:
                        markers.append((m.start(), int(m.group(1))))
                    except Exception:
                        continue
                markers.sort(key=lambda x: x[0])
                return markers

            def _resolve_page_number(
                item_payload: Dict[str, Any], source_content: str
            ) -> int:
                """
                Ensure pageNumber is always an integer.
                Uses model value when valid, then tries PDF page markers, then defaults to 1.
                """
                raw_page = item_payload.get("pageNumber")
                if isinstance(raw_page, int) and raw_page > 0:
                    return raw_page
                if isinstance(raw_page, str):
                    raw_page = raw_page.strip()
                    if raw_page.isdigit() and int(raw_page) > 0:
                        return int(raw_page)

                if source_content:
                    page_markers = _extract_page_markers(source_content)
                    if page_markers:
                        # Try to anchor by date/title/description and map to nearest prior page marker.
                        anchors = [
                            str(item_payload.get("date") or "").strip(),
                            str(item_payload.get("title") or "").strip(),
                            str(item_payload.get("description") or "").strip(),
                        ]
                        haystack = source_content.lower()
                        pos = -1
                        for anchor in anchors:
                            if len(anchor) < 4:
                                continue
                            pos = haystack.find(anchor.lower())
                            if pos >= 0:
                                break
                        if pos >= 0:
                            nearest = 1
                            for marker_pos, marker_page in page_markers:
                                if marker_pos <= pos:
                                    try:
                                        nearest = int(marker_page)
                                    except Exception:
                                        nearest = 1
                                else:
                                    break
                            return nearest if nearest > 0 else 1
                        # Page markers exist but anchor was not found; default to first marker page.
                        try:
                            first_page = int(page_markers[0][1])
                            return first_page if first_page > 0 else 1
                        except Exception:
                            return 1
                return 1

            def _extract_explicit_dates(source_content: str) -> set[str]:
                """Extract explicit full dates from source text as YYYY-MM-DD."""
                out: set[str] = set()
                if not source_content:
                    return out
                patterns = [
                    (r"\b\d{4}-\d{2}-\d{2}\b", "%Y-%m-%d"),
                    (r"\b\d{2}/\d{2}/\d{4}\b", "%d/%m/%Y"),
                    (r"\b\d{2}-\d{2}-\d{4}\b", "%d-%m-%Y"),
                    (r"\b\d{4}/\d{2}/\d{2}\b", "%Y/%m/%d"),
                    (
                        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
                        "%B %d, %Y",
                    ),
                    (
                        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b",
                        "%d %B %Y",
                    ),
                ]
                for patt, fmt in patterns:
                    for m in re.finditer(patt, source_content, flags=re.I):
                        try:
                            dt = datetime.strptime(m.group(0), fmt)
                            out.add(dt.strftime("%Y-%m-%d"))
                        except Exception:
                            continue
                return out

            def _extract_explicit_date_mentions(
                source_content: str,
            ) -> List[Tuple[str, int]]:
                """
                Extract explicit date mentions with character positions.
                Returns list of (YYYY-MM-DD, start_pos).
                """
                out: List[Tuple[str, int]] = []
                if not source_content:
                    return out
                patterns = [
                    (r"\b\d{4}-\d{2}-\d{2}\b", "%Y-%m-%d"),
                    (r"\b\d{2}/\d{2}/\d{4}\b", "%d/%m/%Y"),
                    (r"\b\d{2}-\d{2}-\d{4}\b", "%d-%m-%Y"),
                    (r"\b\d{4}/\d{2}/\d{2}\b", "%Y/%m/%d"),
                    (
                        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
                        "%B %d, %Y",
                    ),
                    (
                        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b",
                        "%d %B %Y",
                    ),
                ]
                for patt, fmt in patterns:
                    for m in re.finditer(patt, source_content, flags=re.I):
                        try:
                            dt = datetime.strptime(m.group(0), fmt)
                            out.append((dt.strftime("%Y-%m-%d"), m.start()))
                        except Exception:
                            continue
                # Deterministic ordering by occurrence position
                out.sort(key=lambda x: x[1])
                return out

            def _extract_explicit_years(source_content: str) -> set[int]:
                """Extract explicit 4-digit years written in source text.

                Masks out currency / money amounts before extracting so that
                "₹2000" / "Rs. 2000" / "$2000" don't pollute the year set —
                otherwise an audio transcript that mentions a salary can
                anchor every relative date to year 2000.
                """
                years: set[int] = set()
                if not source_content:
                    return years
                masked = re.sub(
                    r"(?:₹|Rs\.?|INR|\$|€|£)\s*[\d,]+(?:\.\d+)?",
                    " ",
                    source_content,
                    flags=re.IGNORECASE,
                )
                masked = re.sub(
                    r"\b\d[\d,]*\s*(?:rupees?|rs\.?|/-?)\b",
                    " ",
                    masked,
                    flags=re.IGNORECASE,
                )
                for m in re.finditer(r"\b(19|20)\d{2}\b", masked):
                    try:
                        years.add(int(m.group(0)))
                    except Exception:
                        continue
                return years

            def _resolve_anchor_year(source_content: str, explicit_dates: set[str]) -> Optional[int]:
                """
                Resolve a reference year for relative expressions like "past 10 years".
                Prefer latest explicit full date; else latest explicit year.
                """
                years = []
                for d in explicit_dates:
                    try:
                        years.append(int(d[:4]))
                    except Exception:
                        continue
                if years:
                    return max(years)
                years_only = _extract_explicit_years(source_content)
                return max(years_only) if years_only else None

            def _is_duration_derived_date_allowed(
                normalized_date: str,
                item_payload: Dict[str, Any],
                source_content: str,
                anchor_year: Optional[int],
            ) -> bool:
                """
                Allow inferred year-only dates only when grounded by relative duration
                phrases in the same source and an explicit anchor year.
                """
                if not normalized_date or len(normalized_date) < 10:
                    return False
                if normalized_date[5:] != "01-01":
                    return False
                if anchor_year is None:
                    return False
                try:
                    target_year = int(normalized_date[:4])
                except Exception:
                    return False

                corpus = " ".join(
                    [
                        source_content or "",
                        str(item_payload.get("title") or ""),
                        str(item_payload.get("description") or ""),
                    ]
                ).lower()
                if not corpus:
                    return False

                word_to_num = {
                    "one": 1,
                    "two": 2,
                    "three": 3,
                    "four": 4,
                    "five": 5,
                    "six": 6,
                    "seven": 7,
                    "eight": 8,
                    "nine": 9,
                    "ten": 10,
                    "eleven": 11,
                    "twelve": 12,
                    "thirteen": 13,
                    "fourteen": 14,
                    "fifteen": 15,
                    "sixteen": 16,
                    "seventeen": 17,
                    "eighteen": 18,
                    "nineteen": 19,
                    "twenty": 20,
                }

                def _to_year_count(raw: str) -> Optional[int]:
                    token = (raw or "").strip().lower()
                    if not token:
                        return None
                    if token.isdigit():
                        val = int(token)
                        return val if 0 < val <= 50 else None
                    return word_to_num.get(token)

                # Examples matched:
                # - "10 years", "past 5 years", "4-5 years", "4 to 5 years"
                # - "ten years", "past four years", "four to five years"
                pattern = (
                    r"\b(?:past\s+)?"
                    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
                    r"eighteen|nineteen|twenty)"
                    r"(?:\s*(?:-|to)\s*"
                    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
                    r"eighteen|nineteen|twenty))?"
                    r"\s+years?\b"
                )
                for m in re.finditer(pattern, corpus):
                    n1 = _to_year_count(m.group(1))
                    n2 = _to_year_count(m.group(2)) if m.group(2) else n1
                    if n1 is None or n2 is None:
                        continue
                    low, high = sorted((n1, n2))
                    allowed_years = {anchor_year - n for n in range(low, high + 1)}
                    if target_year in allowed_years:
                        return True
                return False

            for item in arr:
                if not isinstance(item, dict):
                    continue
                date_val = (item.get("date") or item.get("dateValue") or "").strip()
                if not date_val:
                    continue
                # Normalize to YYYY-MM-DD
                try:
                    normalized = None
                    for fmt in (
                        "%Y-%m-%d",
                        "%d/%m/%Y",
                        "%d-%m-%Y",
                        "%Y/%m/%d",
                        "%B %d, %Y",
                        "%d %B %Y",
                    ):
                        try:
                            dt = datetime.strptime(date_val.strip(), fmt)
                            normalized = dt.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue
                    if normalized:
                        date_val = normalized
                    elif (
                        len(date_val) >= 10
                        and date_val[4] == "-"
                        and date_val[7] == "-"
                    ):
                        date_val = date_val[:10]
                    else:
                        continue
                except Exception:
                    continue

                idx = item.get("sourceDocumentIndex")
                idx = int(idx) if idx is not None else 1
                source_doc_id = doc_id_by_index.get(idx, "")
                source_doc_name = doc_name_by_index.get(idx, "")
                source_content = doc_content_by_index.get(idx, "")
                if idx not in doc_explicit_dates_by_index:
                    explicit_dates = _extract_explicit_dates(source_content)
                    doc_explicit_dates_by_index[idx] = explicit_dates
                    explicit_years = _extract_explicit_years(source_content)
                    doc_explicit_years_by_index[idx] = explicit_years
                    doc_anchor_year_by_index[idx] = _resolve_anchor_year(
                        source_content, explicit_dates
                    )
                explicit_dates = doc_explicit_dates_by_index.get(idx, set())
                explicit_years = doc_explicit_years_by_index.get(idx, set())
                anchor_year = doc_anchor_year_by_index.get(idx)

                # Post-LLM hallucination guard: when Gemini stamps a partial
                # date with a wildly wrong year (e.g. "2 February" → 2000-02-02
                # for a 2026 conversation), snap to the current year unless
                # the LLM had an explicit anchor or the year is genuinely in
                # the source text. Same helper used by the langgraph agent path
                # so both paths agree on year correction.
                date_val = snap_hallucinated_year(
                    date_iso=date_val,
                    document_text=source_content,
                    today_iso=today_str,
                    anchor_year=anchor_year if isinstance(anchor_year, int) else None,
                    log_prefix="List of dates",
                )

                # Keep only grounded dates:
                # 1) exact explicit date appears in source, OR
                # 2) explicit year appears in source with YYYY-01-01 normalization, OR
                # 3) derived from relative "X years" phrase anchored to explicit year in same source.
                year_val = int(date_val[:4]) if len(date_val) >= 4 and date_val[:4].isdigit() else None
                is_explicit_full_date = date_val in explicit_dates
                is_explicit_year_only = (
                    date_val.endswith("-01-01")
                    and year_val is not None
                    and year_val in explicit_years
                )
                is_allowed_relative = _is_duration_derived_date_allowed(
                    date_val, item, source_content, anchor_year
                )
                if not (is_explicit_full_date or is_explicit_year_only or is_allowed_relative):
                    logger.debug(
                        "List of dates: dropping ungrounded date={} title='{}' doc='{}'",
                        date_val,
                        (item.get("title") or "").strip(),
                        source_doc_name or source_doc_id or str(idx),
                    )
                    continue
                page_number = _resolve_page_number(item, source_content)

                # Build core entry used by backend List of Dates UI
                raw_title = (item.get("title") or "").strip()
                raw_desc = (item.get("description") or "").strip()

                # Drop generic meaningless titles that indicate the LLM had no context
                if raw_title.lower() in _generic_titles:
                    continue

                # Drop entries whose title or description contain scraped UI garbage
                if _is_garbage_text_local(raw_title) or _is_garbage_text_local(raw_desc):
                    continue

                # Cap the description length WITHOUT splitting on sentence
                # boundaries — the naive ". " splitter treated abbreviation
                # periods ("Model No.", "Serial No.", "SRF No.", "Rs.") as
                # sentence ends and chopped descriptions mid-clause (e.g.
                # "...received the IWC (Model No. /Serial No."). Keep the full
                # description and only trim at a word boundary if it is unusually
                # long (the prompt already targets 1-2 short sentences).
                if raw_desc and len(raw_desc) > 350:
                    raw_desc = (
                        raw_desc[:350].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
                    )

                # Drop entries with a description too short to be meaningful
                if raw_desc and len(raw_desc.strip()) < 8:
                    continue

                raw_event_type = (item.get("eventType") or "").strip()
                normalized_event_type = _normalize_event_type(raw_event_type)
                # Drop entries that map to a procedural type — those belong
                # exclusively on the procedural_timeline panel and must not
                # appear on list_of_dates (this is the user-visible "factual
                # narrative" panel).
                if normalized_event_type == _PROCEDURAL_SENTINEL:
                    logger.info(
                        "List of dates: dropped procedural-type entry "
                        "(eventType={!r}, title={!r}) — belongs in procedural_timeline",
                        raw_event_type, raw_title,
                    )
                    continue
                # NOTE: title-based procedural detection moved AFTER the entry
                # is built — a single batched LLM classification call runs
                # over the surviving entries (`_llm_classify_factual_vs_procedural`)
                # so any procedural phrasing the LLM emits — even with a
                # benign eventType and unfamiliar wording — gets dropped by
                # general semantic judgment, not a hardcoded keyword list.
                # Drop strictly-future entries — list_of_dates is a record of
                # what HAS HAPPENED. Future dates (deadlines, scheduled
                # hearings, upcoming events) belong on procedural_timeline.
                if date_val > today_str:
                    logger.info(
                        "List of dates: dropped future-dated entry "
                        "(date={}, today={}, title={!r}) — list_of_dates is past-only",
                        date_val, today_str, raw_title,
                    )
                    continue
                # Drop fake YYYY-01-01 placeholders that the LLM synthesized
                # from a relative phrase ("three years ago" → 2023-01-01)
                # when the source does not actually contain that 4-digit year
                # as a year reference. Source-grounded "YYYY-01-01" entries
                # (e.g. a contract dated 1 January 2024) are kept because the
                # year IS in the source.
                if (
                    date_val.endswith("-01-01")
                    and re.search(rf"\b{date_val[:4]}\b", source_content or "") is None
                ):
                    logger.info(
                        "List of dates: dropped fake YYYY-01-01 placeholder "
                        "(date={}, title={!r}) — relative phrase only, no concrete "
                        "day/month and year not in source",
                        date_val, raw_title,
                    )
                    continue
                entry = {
                    "date": date_val,
                    "title": raw_title or "Event",
                    "description": raw_desc,
                    "sourceDocId": source_doc_id or None,
                    "sourceDocName": source_doc_name or None,
                    "pageNumber": page_number,
                    "isIncident": item.get("isIncident", True),
                    "eventType": normalized_event_type,
                    "relevance": (item.get("relevance") or "").strip() or None,
                }
                # For frontend compatibility: many UIs expect a generic 'source'/'sourceAnswer' field.
                # Populate these from the resolved document name so UI does not show "N/A".
                if source_doc_name:
                    entry["source"] = source_doc_name
                    entry["sourceAnswer"] = source_doc_name
                # Guardrail: suppress purely historical/corporate background chronology
                # that can leak from supporting/reference annexures.
                if _is_background_timeline_entry(
                    str(entry.get("title") or ""),
                    str(entry.get("description") or ""),
                    str(entry.get("sourceDocName") or ""),
                ):
                    continue
                # Drop None/empty so payload is clean; keep description if present
                entry = {k: v for k, v in entry.items() if v is not None and v != ""}
                result.append(entry)

            # NOTE: The raw-snippet "Date Mentioned in Document" backfill has been
            # removed. It could only produce literal text extracts (including raw Hindi
            # court orders, email headers, and listing-table rows) as descriptions,
            # which are never clean single-sentence summaries. The LLM prompt already
            # instructs the model to capture all case-relevant dates, so the backfill
            # was adding noise rather than value.

            # LLM temporal-inference backfill via LangGraph:
            # captures relative/chained timeline statements grounded in document text.
            existing_keys = {
                (
                    str(e.get("sourceDocId") or "").strip(),
                    str(e.get("date") or "").strip(),
                )
                for e in result
            }
            for idx, source_content in doc_content_by_index.items():
                source_doc_id = doc_id_by_index.get(idx, "")
                source_doc_name = doc_name_by_index.get(idx, "")
                anchor_year = doc_anchor_year_by_index.get(idx)
                rel_events: List[Dict[str, Any]] = []
                try:
                    rel_events = await list_of_dates_temporal_agent.infer_temporal_events(
                        document_text=source_content,
                        source_doc_id=source_doc_id,
                        source_doc_name=source_doc_name,
                        anchor_year=anchor_year,
                        today_str=today_str,
                    )
                except Exception as temporal_err:
                    logger.debug(
                        "List of dates temporal agent failed for doc {}: {}",
                        source_doc_id or idx,
                        temporal_err,
                    )
                    rel_events = []
                for e in rel_events:
                    # Resolve best-effort page for inferred temporal events too.
                    e["pageNumber"] = _resolve_page_number(e, source_content)
                    key = (
                        str(e.get("sourceDocId") or "").strip(),
                        str(e.get("date") or "").strip(),
                    )
                    if key in existing_keys:
                        continue
                    result.append(e)
                    existing_keys.add(key)

            # Stable ordering for UI: chronological first, then title/doc.
            result.sort(
                key=lambda x: (
                    str(x.get("date") or ""),
                    str(x.get("title") or ""),
                    str(x.get("sourceDocName") or ""),
                )
            )

            # Final deterministic dedupe: same sourceDocId + date + title should appear once,
            # even if descriptions differ across model/backfill passes.
            deduped: List[Dict[str, Any]] = []
            seen_keys: set[Tuple[str, str, str]] = set()
            for entry in result:
                key = (
                    str(entry.get("sourceDocId") or "").strip(),
                    str(entry.get("date") or "").strip(),
                    str(entry.get("title") or "").strip().lower(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append(entry)
            result = deduped

            # Semantic factual-vs-procedural classification — single batched
            # LLM call that judges each entry as a factual case event or a
            # procedural milestone, dropping the procedural ones. Replaces
            # the earlier hardcoded title-keyword regex so the filter
            # generalises to ANY case type and ANY procedural phrasing the
            # LLM might emit.
            before_class = len(result)
            try:
                result = await self._llm_classify_factual_vs_procedural(
                    result, item_type="list of dates"
                )
                if len(result) < before_class:
                    logger.info(
                        "List of dates: LLM factual-vs-procedural classifier "
                        "dropped {} entries (kept {})",
                        before_class - len(result), len(result),
                    )
            except Exception as classify_err:
                logger.warning(
                    "List of dates: factual-vs-procedural classifier failed "
                    "(non-fatal, keeping all entries): {}",
                    classify_err,
                )

            # Semantic same-date dedupe — runs UNCONDITIONALLY on the freshly
            # generated list, even on first emit (no prev cache yet). Catches
            # the LLM emitting two paraphrased entries on the same date that
            # describe the same real-world event ("Arrival in India" vs
            # "New Delhi Arrival" on 17-07-2023; "Visa Issued" vs "Visa
            # Issuance" on 25-06-2025; etc.). Per-date scope means genuinely
            # distinct events that share a date are preserved.
            before_semantic = len(result)
            try:
                result = await self._dedupe_dated_entries_semantically(
                    result, item_type="list of dates"
                )
                if len(result) < before_semantic:
                    logger.info(
                        "List of dates: per-date semantic dedupe collapsed "
                        "{} entries → {}",
                        before_semantic, len(result),
                    )
            except Exception as dedupe_err:
                logger.warning(
                    "List of dates: per-date semantic dedupe failed (non-fatal): {}",
                    dedupe_err,
                )

            logger.info(
                "List of dates: generated {} entries for workspace {}",
                len(result),
                workspace_id[:12] if workspace_id else "",
            )
            # Strip the application's own brand/platform name from every entry
            # (title / description / evidenceSnippet) before caching/emitting.
            return scrub_app_brand(result)

        except Exception as e:
            logger.warning("List of dates generation failed: {}", e)
            return []

    async def _generate_and_emit_list_of_dates(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        batch_result: Optional[Dict[str, Any]] = None,
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Fetch workspace documents, generate list of dates from them, emit to backend and cache.
        Emits document_feature_response with featureType "list_of_dates" and payload.listOfDates.

        ``deleted_doc_ids`` is set when this regeneration was triggered by a
        document deletion (see workspace_sync handler). When deletion is the
        only change (``batch_result is None`` and ``deleted_doc_ids`` is
        non-empty), this method takes a FAST PATH: it skips the Gemini
        regeneration entirely and just filters the cached prior list to
        drop entries that came from the deleted document(s), then emits.
        Rationale: the remaining documents have not changed, so their dates
        are already in the cache; running another LLM call would only
        introduce paraphrased duplicates that have to be merged back.
        """
        if not self._socket_client:
            return
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("List of dates: could not fetch documents: {}", e)
            all_docs = []

        # ------------------------------------------------------------------
        # FAST PATH: pure-deletion regeneration.
        # When the caller passed deleted_doc_ids and no batch_result (i.e.
        # nothing was newly uploaded — the only change is a deletion), there
        # is nothing for the LLM to add. Filter the cached prior list to
        # remove entries from the deleted docs and emit. This saves a Gemini
        # call per deletion and avoids paraphrased-duplicate noise in the
        # subsequent merge.
        # ------------------------------------------------------------------
        deleted_set_fast = {str(x).strip() for x in (deleted_doc_ids or []) if x}
        if deleted_set_fast and not isinstance(batch_result, dict):
            live_doc_ids_fast = {
                str(d.get("document_id") or "").strip()
                for d in (all_docs or [])
                if d.get("document_id")
            }
            prior_fast = self._workspace_list_of_dates_payload.get(workspace_id) or []
            if not isinstance(prior_fast, list):
                prior_fast = []
            before_count = len(prior_fast)
            prior_fast = [
                e
                for e in prior_fast
                if isinstance(e, dict)
                and str(e.get("sourceDocId") or "").strip() not in deleted_set_fast
            ]
            prior_fast = self._drop_entries_for_deleted_docs(
                prior_fast,
                live_doc_ids_fast,
                drop_unattributed=True,
            )
            prior_fast.sort(
                key=lambda x: (
                    str(x.get("date") or ""),
                    str(x.get("title") or ""),
                    str(x.get("sourceDocName") or ""),
                )
            )
            self._workspace_list_of_dates_payload[workspace_id] = prior_fast
            payload_fast = {
                "workspaceId": workspace_id,
                "userId": user_id,
                "listOfDates": prior_fast,
            }
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "list_of_dates",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": payload_fast,
                },
            )
            logger.success(
                "✅ List of dates filtered+emitted after deletion (no LLM regen): "
                "{} → {} entries (dropped {})",
                before_count,
                len(prior_fast),
                before_count - len(prior_fast),
            )
            self._log_list_of_dates_outbound(
                workspace_id=workspace_id,
                user_id=user_id,
                list_of_dates=prior_fast,
                context="post-deletion-filtered",
            )
            await asyncio.sleep(0.2)
            return

        processed_doc_ids: set[str] = set()
        if isinstance(batch_result, dict):
            for r in batch_result.get("results") or []:
                did = str(r.get("document_id") or "").strip()
                if did:
                    processed_doc_ids.add(did)

        target_docs = all_docs
        if processed_doc_ids:
            # Incremental upload sync: build timeline only from newly processed docs.
            target_docs = [
                d
                for d in (all_docs or [])
                if str(d.get("document_id") or "").strip() in processed_doc_ids
            ]
            logger.info(
                "List of dates incremental mode: {} processed doc(s), {} matched in vector store",
                len(processed_doc_ids),
                len(target_docs),
            )

        list_of_dates = await self._generate_list_of_dates_from_documents(
            workspace_id=workspace_id,
            user_id=user_id,
            all_docs=target_docs,
            workspace_role=workspace_role,
            batch_result=batch_result,
        )
        # Always merge against the FULL prior cache, regardless of whether this
        # is a sync (with `processed_doc_ids`) or a full refresh. The previous
        # retained/replaced split skipped cross-document paraphrase comparisons
        # — a regenerated entry from the audio doc could be a paraphrase of
        # an existing entry from a different doc, and that pair never reached
        # the LLM merge. The merge helper preserves prev verbatim, so this is
        # safe for the sync case too: entries from non-changed docs stay as-is
        # and only the LLM decides whether new entries are duplicates of ANY
        # prior entry.
        #
        # IMPORTANT: filter the cached `prior` against the LIVE document ids
        # before merging. The merge preserves prev verbatim, so without this
        # filter, entries from a deleted document would persist forever in
        # the cache (and keep being re-emitted) — that is the bug the user
        # reported when documents are deleted via workspace_sync.
        live_doc_ids = {
            str(d.get("document_id") or "").strip()
            for d in (all_docs or [])
            if d.get("document_id")
        }
        prior = self._workspace_list_of_dates_payload.get(workspace_id) or []
        if not isinstance(prior, list):
            prior = []
        # Deletion context: do an explicit, deterministic pre-pass first so
        # rows from freshly-deleted IDs are killed even if the vector_store
        # delete hasn't fully propagated yet (live_doc_ids would otherwise
        # still include them and the live-set filter would keep them).
        deleted_set = {str(x).strip() for x in (deleted_doc_ids or []) if x}
        if deleted_set:
            before = len(prior)
            prior = [
                e
                for e in prior
                if isinstance(e, dict)
                and str(e.get("sourceDocId") or "").strip() not in deleted_set
            ]
            if before != len(prior):
                logger.info(
                    "list_of_dates: explicit-delete pre-pass dropped {} cached entry/entries",
                    before - len(prior),
                )
        prior = self._drop_entries_for_deleted_docs(
            prior,
            live_doc_ids,
            drop_unattributed=bool(deleted_set),
        )
        if prior:
            merged = await self._llm_merge_dated_entries_against_previous(
                prior, list_of_dates or [],
                item_type="list of dates",
            )
        else:
            merged = list(list_of_dates or [])
        merged.sort(
            key=lambda x: (
                str(x.get("date") or ""),
                str(x.get("title") or ""),
                str(x.get("sourceDocName") or ""),
            )
        )
        self._workspace_list_of_dates_payload[workspace_id] = merged
        list_of_dates = merged
        # Payload matches backend spec: workspaceId, userId at root and inside payload for storage
        payload = {
            "workspaceId": workspace_id,
            "userId": user_id,
            "listOfDates": list_of_dates,
        }
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "list_of_dates",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ List of dates sent to backend ({} entries)", len(list_of_dates)
        )
        self._log_list_of_dates_outbound(
            workspace_id=workspace_id,
            user_id=user_id,
            list_of_dates=list_of_dates,
            context="generated",
        )
        await asyncio.sleep(0.2)  # Allow backend to process before next event

    def _log_list_of_dates_outbound(
        self,
        workspace_id: str,
        user_id: str,
        list_of_dates: List[Dict[str, Any]],
        context: str,
    ) -> None:
        """
        Record outbound list_of_dates responses with generation timestamp for tracing.
        """
        generated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        outbound = {
            "featureType": "list_of_dates",
            "workspaceId": workspace_id,
            "userId": user_id,
            "payload": {
                "workspaceId": workspace_id,
                "userId": user_id,
                "listOfDates": list_of_dates,
            },
            "generatedAt": generated_at,
            "context": context,
        }
        logger.info(
            "📤 list_of_dates outbound at {} [{}]: {}",
            generated_at,
            context,
            json.dumps(outbound, ensure_ascii=False),
        )
