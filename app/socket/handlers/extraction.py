"""Case-entity extraction sub-agents (parties/timeline/assets).

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


class ExtractionMixin:
    """Case-entity extraction sub-agents (parties/timeline/assets)."""

    async def _run_extraction_subagent(
        self,
        workspace_id: str,
        prompt: str,
        sub_name: str,
        expected_keys: List[str],
    ) -> Dict[str, Any]:
        """
        Shared driver for the three parallel extraction sub-agents.

        Runs ONE Gemini call (temperature 0.0, JSON mode, max_tokens 12000),
        parses the response, and returns a dict with every ``expected_keys``
        entry populated to a list. Failures are non-fatal: returns
        ``{key: [] for key in expected_keys}`` so other sub-agents and
        downstream synthesis still proceed.
        """
        scaffold: Dict[str, Any] = {k: [] for k in expected_keys}
        from app.ai.gemini_client import gemini_client
        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning(
                "Strategy sub-extractor '{}' failed for workspace {}: {}",
                sub_name,
                workspace_id[:12] if workspace_id else "",
                e,
            )
            return scaffold
        if not raw:
            return scaffold
        try:
            json_str = self._prepare_json_candidate(raw.strip(), prefer="object")
            if not json_str:
                return scaffold
            import json as _json
            parsed = _json.loads(json_str)
            if not isinstance(parsed, dict):
                return scaffold
            for k in expected_keys:
                if not isinstance(parsed.get(k), list):
                    parsed[k] = []
            return parsed
        except Exception as parse_err:
            logger.warning(
                "Strategy sub-extractor '{}' JSON parse failed for workspace {}: {}",
                sub_name,
                workspace_id[:12] if workspace_id else "",
                parse_err,
            )
            return scaffold

    async def _extract_parties_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor A: parties + designations (people and organisations)."""
        prompt = (
            "You are a focused entity extractor for legal documents. Your ONLY "
            "job is to list every PARTY (person, firm, company, government "
            "office) that appears in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "parties": [\n'
            '    { "name": "exact name as written in the documents", '
            '"designation": "role / community / status as written (e.g. plaintiff, defendant, vendor, vendee, complainant, M/s ..., advocate, SDM, Sub-Registrar, member of Scheduled Caste community)", '
            '"role_in_matter": "short description of why this party matters (e.g. original owner, registered purchaser, civil-suit plaintiff)", '
            '"first_mention_doc": "Annexure N or document filename" }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Use names EXACTLY as written. Do NOT collapse 'Smt. Premlata' "
            "to 'Premlata' or 'Munna Lal' to 'Munnalal'. Write what the "
            "documents write, including honorifics (Smt., Shri, Dr., M/s).\n"
            "- Include every distinct party — original owners, current owners, "
            "purchasers, advocates, court officials, witnesses, official "
            "signatories — if they are named in the documents.\n"
            "- Never write 'Party No. 1' / 'Party No. 2' / 'the plaintiff' / "
            "'the petitioner' / 'the respondent' placeholders. If you cannot "
            "find a real name for an entry, DROP the entry.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON. No commentary, no markdown fences.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
        )
        return await self._run_extraction_subagent(
            workspace_id, prompt, "parties", ["parties"]
        )

    async def _extract_timeline_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor B: dated events + suit/case/FIR numbers."""
        prompt = (
            "You are a focused timeline extractor for legal documents. Your "
            "ONLY job is to extract every DATED EVENT and every SUIT/CASE/FIR "
            "number that appears in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "dates": [\n'
            '    { "date_iso": "YYYY-MM-DD", '
            '"event": "what happened on this date — name the actor, the counterparty, the action (e.g. \\"Sitaram executed a registered agreement to sell Arazi No. 262 to M/s Srinet & Sandhilya Construction Pvt. Ltd.\\")", '
            '"actors": ["names exactly as written"], '
            '"source_doc": "Annexure N or document filename" }\n'
            "  ],\n"
            '  "suit_numbers": [\n'
            '    { "number": "exact case/suit/FIR identifier as written (e.g. O.S. No. 827/2019, Case No. 13779, FIR No. 145/2024)", '
            '"court": "court / forum where filed", '
            '"filed_by": "name exactly as written", '
            '"against": ["names"], '
            '"filed_on": "YYYY-MM-DD or null", '
            '"subject_matter": "what the suit is about" }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Walk the documents chronologically. Include EVERY dated event "
            "with a clear actor and action — agreements, notices, registrations, "
            "sale deeds, FIRs, filings, hearings, orders, judgments, payments.\n"
            "- Use Indian DD/MM/YYYY convention when parsing dates: '02/12/2008' "
            "is 2 December 2008 (date_iso = '2008-12-02').\n"
            "- For suit/case/FIR numbers: include EVERY one you can find with "
            "its exact identifier verbatim. Do not normalise 'O.S. No. 827/2019' "
            "to 'OS 827 2019' — keep the source punctuation.\n"
            "- Use names EXACTLY as written. Never invent or generalise.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
        )
        return await self._run_extraction_subagent(
            workspace_id, prompt, "timeline", ["dates", "suit_numbers"]
        )

    async def _extract_assets_and_law_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor C: properties + amounts + statutes + annexures."""
        prompt = (
            "You are a focused asset and legal-framework extractor for legal "
            "documents. Your ONLY job is to extract every PROPERTY / ASSET, "
            "every MONETARY AMOUNT, every STATUTE referenced, and every "
            "ANNEXURE described in the documents below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "properties": [\n'
            '    { "identifier": "Arazi No. X / Khasra No. Y / Plot N / Survey No. N etc., exactly as written", '
            '"area": "area exactly as written (e.g. 0.263 Hectares, 2400 sq.ft.)", '
            '"location": "most specific location written in the docs (Mauza / Village -> Block / Tehsil -> District -> State)", '
            '"current_owner_per_docs": "name as written, if stated" }\n'
            "  ],\n"
            '  "amounts": [\n'
            '    { "amount": "in source notation (e.g. Rs. 11,64,000)", '
            '"context": "advance / total consideration / balance / payment / loan / fine / etc.", '
            '"from": "name as written", '
            '"to": "name as written", '
            '"date_iso": "YYYY-MM-DD if a date attaches to the amount, else null" }\n'
            "  ],\n"
            '  "statutes": [\n'
            '    { "name": "exact statute name as written (e.g. Specific Relief Act, 1963; Bharatiya Nyaya Sanhita, 2023; UP Zamindari Abolition & Land Reforms Act)", '
            '"sections": ["section numbers as written, e.g. 10, 12, 143"], '
            '"applies_to": "short reason why this statute is relevant per the documents" }\n'
            "  ],\n"
            '  "annexures": [\n'
            '    { "label": "Annexure 1", '
            '"type": "Sale Deed / Affidavit / Notice / Khatauni / Written Statement / etc.", '
            '"date_iso": "YYYY-MM-DD or null", '
            '"parties": ["names exactly as written"] }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Use values EXACTLY as written. Preserve Indian numerical "
            "notation for amounts (Rs. 11,64,000 stays that way; do NOT "
            "convert to '11.64 lakh' or '1,164,000').\n"
            "- Include every distinct property mention, even when the same "
            "plot is referenced by different identifiers in different docs.\n"
            "- For statutes: use names as written. Include both BNS / BNSS / "
            "BSA / IT Act / etc. and any state-level land-revenue / property "
            "statutes referenced.\n"
            "- Reference uploaded files only as Annexure N.\n"
            "- Return ONLY valid JSON.\n\n"
            "DOCUMENTS:\n"
            f"{combined_document_content}"
        )
        return await self._run_extraction_subagent(
            workspace_id,
            prompt,
            "assets_and_law",
            ["properties", "amounts", "statutes", "annexures"],
        )

    def _merge_case_entities(
        self,
        parties_result: Dict[str, Any],
        timeline_result: Dict[str, Any],
        assets_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine three sub-extractor results into the unified case_entities dict."""
        merged: Dict[str, Any] = {k: [] for k in self._EMPTY_CASE_ENTITIES.keys()}
        if isinstance(parties_result, dict):
            merged["parties"] = list(parties_result.get("parties") or [])
        if isinstance(timeline_result, dict):
            merged["dates"] = list(timeline_result.get("dates") or [])
            merged["suit_numbers"] = list(timeline_result.get("suit_numbers") or [])
        if isinstance(assets_result, dict):
            merged["properties"] = list(assets_result.get("properties") or [])
            merged["amounts"] = list(assets_result.get("amounts") or [])
            merged["statutes"] = list(assets_result.get("statutes") or [])
            merged["annexures"] = list(assets_result.get("annexures") or [])
        return merged

    async def _extract_case_entities(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """
        Pass 1 of the strategy pipeline — "deep agents in spirit".

        Runs three focused sub-extractors in parallel via ``asyncio.gather`` so
        each entity family gets the full 12000-token budget for its slice:

        - parties (people + organisations + designations)
        - timeline (dated events + suit/case/FIR numbers)
        - assets_and_law (properties + amounts + statutes + annexures)

        Combines into the unified ``case_entities`` dict that the synthesis
        pass anchors to. Any sub-extractor that fails contributes empty arrays
        so a single sub-failure never blocks the whole strategy generation.
        """
        if not combined_document_content or not combined_document_content.strip():
            return dict(self._EMPTY_CASE_ENTITIES)
        if combined_document_content.strip() == "No document content available.":
            return dict(self._EMPTY_CASE_ENTITIES)

        # Content-hash short-circuit. Strategy and analysis_report both call this
        # helper inside the same generation pipeline; without the cache we would
        # pay for the 3-parallel-sub-extractor pass twice for the same content.
        import hashlib as _hashlib
        try:
            _content_hash = _hashlib.sha1(
                combined_document_content[:80000].encode("utf-8", errors="ignore")
            ).hexdigest()
        except Exception:
            _content_hash = ""
        if _content_hash and workspace_id:
            _cached = self._workspace_case_entities_cache.get(workspace_id)
            if _cached and _cached[0] == _content_hash:
                logger.info(
                    "🧠 case_entities cache HIT for workspace {} (skipping 3 sub-extractors)",
                    workspace_id[:12],
                )
                return _cached[1]

        try:
            parties_result, timeline_result, assets_result = await asyncio.gather(
                self._extract_parties_subagent(
                    workspace_id, combined_document_content
                ),
                self._extract_timeline_subagent(
                    workspace_id, combined_document_content
                ),
                self._extract_assets_and_law_subagent(
                    workspace_id, combined_document_content
                ),
            )
        except Exception as gather_err:
            logger.warning(
                "Strategy extraction (parallel sub-agents) failed for workspace {}: {}",
                workspace_id[:12] if workspace_id else "",
                gather_err,
            )
            return dict(self._EMPTY_CASE_ENTITIES)

        merged = self._merge_case_entities(
            parties_result, timeline_result, assets_result
        )
        # Per-sub-agent diagnostic logs so future thin-summary failures can be
        # traced to whichever sub-agent came back empty.
        logger.info(
            "🔎 Strategy sub-extractor PARTIES: {} party/entity record(s) for workspace {}",
            len(merged.get("parties") or []),
            workspace_id[:12] if workspace_id else "",
        )
        logger.info(
            "🔎 Strategy sub-extractor TIMELINE: {} dated event(s), {} suit/case number(s) for workspace {}",
            len(merged.get("dates") or []),
            len(merged.get("suit_numbers") or []),
            workspace_id[:12] if workspace_id else "",
        )
        logger.info(
            "🔎 Strategy sub-extractor ASSETS+LAW: {} property/ies, {} amount(s), {} statute(s), {} annexure(s) for workspace {}",
            len(merged.get("properties") or []),
            len(merged.get("amounts") or []),
            len(merged.get("statutes") or []),
            len(merged.get("annexures") or []),
            workspace_id[:12] if workspace_id else "",
        )
        # Populate cache so a sibling caller (e.g. analysis_report after
        # strategy) sees the same entities without re-running extraction.
        if _content_hash and workspace_id:
            self._workspace_case_entities_cache[workspace_id] = (_content_hash, merged)
        return merged

    @staticmethod
    def _verify_entities_in_summary(
        summary_text: str,
        entities: Dict[str, Any],
    ) -> Dict[str, List[str]]:
        """
        Programmatic coverage check after synthesis. Returns a dict with three
        buckets of missing items so the caller can issue one consolidated
        retry prompt:

        - ``missing_parties``: party names whose full form OR primary surname
          token does not appear in ``summary_text``.
        - ``missing_suit_numbers``: every ``suit_numbers[i]["number"]`` that
          does not appear verbatim in ``summary_text``.
        - ``missing_key_dates``: up to 8 of the most legally material dates
          (suit filings, registered deeds, notices, court orders) that do not
          appear in either ISO or DD/MM/YYYY form in ``summary_text``.
        """
        result: Dict[str, List[str]] = {
            "missing_parties": [],
            "missing_suit_numbers": [],
            "missing_key_dates": [],
        }
        if not isinstance(summary_text, str) or not summary_text:
            return result
        if not isinstance(entities, dict):
            return result

        # Parties: full name OR primary surname token.
        for party in entities.get("parties", []) or []:
            if not isinstance(party, dict):
                continue
            name = (party.get("name") or "").strip()
            if not name:
                continue
            primary_token = name.split()[-1] if " " in name else name
            if name not in summary_text and primary_token not in summary_text:
                result["missing_parties"].append(name)

        # Suit numbers: verbatim.
        for suit in entities.get("suit_numbers", []) or []:
            if not isinstance(suit, dict):
                continue
            number = (suit.get("number") or "").strip()
            if number and number not in summary_text:
                result["missing_suit_numbers"].append(number)

        # Key dates: pick the top-N most legally material entries.
        _KEY_DATE_TOKENS = (
            "suit", "civil suit", "fir", "complaint", "registered", "sale deed",
            "notice", "legal notice", "order", "judgment", "decree", "writ",
            "filed", "execution", "agreement", "registration",
        )
        key_date_candidates: List[Dict[str, Any]] = []
        for entry in entities.get("dates", []) or []:
            if not isinstance(entry, dict):
                continue
            date_iso = (entry.get("date_iso") or "").strip()
            event = (entry.get("event") or "").strip().lower()
            if not date_iso or not event:
                continue
            if any(tok in event for tok in _KEY_DATE_TOKENS):
                key_date_candidates.append(entry)
        for entry in key_date_candidates[:8]:
            date_iso = (entry.get("date_iso") or "").strip()
            if not date_iso:
                continue
            # Build DD/MM/YYYY alternative for matching.
            dmy = ""
            try:
                y, m, d = date_iso.split("-")
                if len(y) == 4 and len(m) == 2 and len(d) == 2:
                    dmy = f"{d}/{m}/{y}"
            except Exception:
                dmy = ""
            if date_iso not in summary_text and (
                not dmy or dmy not in summary_text
            ):
                result["missing_key_dates"].append(dmy or date_iso)

        return result
