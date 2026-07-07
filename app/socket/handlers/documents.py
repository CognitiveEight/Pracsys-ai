"""Shared document-content / inventory / signal helpers.

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


class DocumentsMixin:
    """Shared document-content / inventory / signal helpers."""

    @staticmethod
    def _drop_entries_for_deleted_docs(
        entries: List[Dict[str, Any]],
        live_doc_ids: set,
        *,
        drop_unattributed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Filter cached timeline/date entries against the current set of
        document IDs in the vector store, dropping any entry whose
        ``sourceDocId`` is set but no longer present.

        Used by ``_generate_and_emit_list_of_dates`` and
        ``_generate_and_emit_procedural_timeline`` before they merge new
        regenerations against their prior cached payload. Without this
        filter, deleting a document leaves its dated entries in the cache
        forever — the merge helper preserves prev verbatim, so stale rows
        survive every subsequent regeneration.

        ``drop_unattributed`` widens the filter for *deletion* contexts:
        when True, also drop entries whose ``sourceDocId`` is missing or
        empty. Such rows can arise when an older code path emitted entries
        with no source attribution (e.g. the LLM returned a
        ``sourceDocumentIndex`` outside the doc range and the empty value
        was stripped at events.py:4446). On uploads we keep these rows for
        backward compat; on deletions we conservatively drop them because
        we cannot rule out that they originated from the doc being deleted.
        Anything genuinely attributable to a still-live doc will be
        re-emitted by the regeneration that runs immediately after.
        """
        if not isinstance(entries, list) or not entries:
            return list(entries or [])
        kept: List[Dict[str, Any]] = []
        dropped_orphans = 0
        dropped_unattributed = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            src = str(e.get("sourceDocId") or "").strip()
            if src:
                if src not in live_doc_ids:
                    dropped_orphans += 1
                    continue
            else:
                if drop_unattributed:
                    dropped_unattributed += 1
                    continue
            kept.append(e)
        if dropped_orphans or dropped_unattributed:
            logger.info(
                "Dropped {} orphan + {} unattributed cached entry/entries "
                "(drop_unattributed={})",
                dropped_orphans,
                dropped_unattributed,
                drop_unattributed,
            )
        return kept

    def _build_document_inventory(self, workspace_id: str) -> str:
        """Build a deterministic, prompt-ready inventory of documents the
        user has uploaded, drawn from the cached document_assessments
        payload (which includes ``documentType`` and ``extractedFields``
        produced by ``app.processing.document_classifier``).

        Feature agents (analysis_report, strategy, list_of_dates,
        applicable_laws, procedural_timeline) inject this string into
        their prompts so the LLM has a non-OCR signal of which documents
        are present. This is the structural fix for the bug where the
        analysis_report flagged identity / financial documents as
        ``missingInfo`` even though the user had already uploaded them —
        OCR noise made the agent unable to recognise an Aadhaar / PAN /
        bank statement from text alone.

        Returns an empty string if the cache is missing so callers can
        no-op cleanly. Output shape (one line per document):

            DOCUMENT INVENTORY (user-provided uploads, classified):
            - Annexure 1: aadhaar_card | name=Ramesh Kumar; dob=1985-05-12; address=present; aadhaar_number=XXXX-XXXX-1234
            - Annexure 2: pan_card | name=Ramesh Kumar; dob=1985-05-12; pan_number=ABCXX1234X
            - Annexure 3: bank_statement | period_from=2024-01-01; period_to=2024-03-31; closing_balance=present
        """
        cached = self._workspace_document_assessments_payload.get(workspace_id)
        if not isinstance(cached, dict):
            return ""
        inner = cached.get("payload") or {}
        assessments = inner.get("documentAssessments")
        if not isinstance(assessments, list) or not assessments:
            return ""

        lines: List[str] = []
        for idx, item in enumerate(assessments, 1):
            if not isinstance(item, dict):
                continue
            # Audio entries with transcriptStatus only (no documentType) are
            # represented as their transcript-status so the agent sees them.
            doc_type = (item.get("documentType") or "other").strip()
            transcript_status = item.get("transcriptStatus")
            extracted = item.get("extractedFields") or {}

            field_pairs: List[str] = []
            if isinstance(extracted, dict):
                for key, value in extracted.items():
                    if value is None or value == "":
                        continue
                    if isinstance(value, list):
                        # collapse short lists, summarise long ones
                        if not value:
                            continue
                        if len(value) <= 3:
                            field_pairs.append(
                                f"{key}=" + ", ".join(str(v) for v in value)
                            )
                        else:
                            field_pairs.append(
                                f"{key}=present ({len(value)} entries)"
                            )
                    elif isinstance(value, (str, int, float, bool)):
                        s_val = str(value).strip()
                        if not s_val:
                            continue
                        # Cap very long values so the inventory stays compact.
                        if len(s_val) > 80:
                            s_val = s_val[:77] + "…"
                        field_pairs.append(f"{key}={s_val}")
            fields_repr = "; ".join(field_pairs) if field_pairs else "(no structured fields extracted)"

            label = f"Annexure {idx}: {doc_type}"
            if transcript_status and transcript_status != "ready":
                label += f" [transcript {transcript_status}]"
            lines.append(f"- {label} | {fields_repr}")

        if not lines:
            return ""

        header = (
            "DOCUMENT INVENTORY (user-provided uploads, classified by the "
            "ingestion pipeline — TREAT AS GROUND TRUTH for which documents "
            "are present):"
        )
        guard = (
            "INVENTORY USE RULES:\n"
            "- Any documentType listed above MUST be treated as PRESENT in evidence.\n"
            "- Identifier numbers in fields (aadhaar_number, pan_number, gstin, etc.) appear MASKED (e.g. XXXX-XXXX-1234); the unmasked value is sealed and not shown — do NOT report a number as 'missing' merely because it is masked.\n"
            "- Field values shown as 'present' indicate the field exists in the document but its value is omitted; treat the field as available.\n"
            "- Before flagging any item as missing or absent in your output, cross-check this inventory; if the item or an evidentiary equivalent is listed here, do NOT flag it."
        )
        return header + "\n" + "\n".join(lines) + "\n\n" + guard

    async def _extract_case_signals_from_documents(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Extract search-friendly title, keywords and evidence snippets from workspace docs."""
        del user_id  # Not needed for extraction, kept for future traceability.
        workspace_title = ""
        try:
            ctx = workspace_manager.get_workspace(workspace_id)
            if ctx:
                workspace_title = str(getattr(ctx, "title", "") or "").strip()
        except Exception:
            workspace_title = ""

        snippets: List[str] = []
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning(
                "Applicable laws: failed to fetch documents for signal extraction: {}",
                e,
            )
            all_docs = []

        for doc in all_docs[:5]:
            content = str(doc.get("content") or "").strip()
            if not content:
                continue
            snippets.append(content[:1200])

        joined = " ".join(snippets)
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", joined)
        stop = {
            "this",
            "that",
            "with",
            "from",
            "were",
            "have",
            "been",
            "their",
            "there",
            "which",
            "shall",
            "under",
            "about",
            "against",
            "where",
            "claim",
            "case",
            "court",
            "petitioner",
            "respondent",
            "document",
            "documents",
            "matter",
            "hereby",
            "thereof",
            "plaintiff",
            "defendant",
        }
        freq: Dict[str, int] = {}
        for token in tokens:
            t = token.lower()
            if t in stop:
                continue
            freq[t] = freq.get(t, 0) + 1
        ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        keywords = [k for k, _v in ranked[:8]]

        query_parts = [p for p in [workspace_title, " ".join(keywords)] if p]
        query = " ".join(query_parts).strip()
        if not query:
            query = "Indian legal case applicable sections"
        # Bias case-law search toward both civil-law and criminal-law angles so the
        # downstream LLM sees grounded snippets from each branch instead of skewing
        # the entire applicable_laws output toward one side of Indian law.
        civil_criminal_hint = "civil law criminal law"
        if not re.search(
            r"\b(civil law|criminal law|civil|criminal)\b", query, flags=re.IGNORECASE
        ):
            query = f"{query} {civil_criminal_hint}".strip()
        return {
            "workspaceTitle": workspace_title,
            "query": query,
            "keywords": keywords,
            "evidenceSnippet": joined[:5000],
        }

    async def _assemble_combined_document_content(self, workspace_id: str) -> str:
        """Assemble combined document content the same way strategy/analysis do.

        Built identically (same per-doc cap + ``_format_document_for_prompt``) so
        the ``_extract_case_entities`` content-hash cache is shared — party
        extraction usually hits the cache populated by strategy/analysis.
        """
        from app.vectorstore.qdrant import vector_store

        try:
            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("Relevant parties: could not fetch documents: {}", e)
            return "No document content available."
        if not all_docs:
            return "No document content available."
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
            content = (doc.get("content") or "").strip()
            if not content or len(content) < 20:
                continue
            if len(content) > per_doc_cap:
                content = content[:per_doc_cap] + "\n[... truncated for length ...]"
            doc_with_clip = dict(doc)
            doc_with_clip["content"] = content
            parts.append(_format_document_for_prompt(doc_with_clip, i, len(all_docs)))
        combined = "\n\n".join(parts) if parts else "No document content available."
        if len(combined) > max_total_content:
            combined = combined[:max_total_content] + "\n[... content truncated ...]"
        return combined
