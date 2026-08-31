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

    @staticmethod
    def _assessment_entry_name(entry: Dict[str, Any]) -> str:
        """File name of one ``documentAssessments`` entry.

        The success path writes ``filename`` (workspace_events.py) while the
        collapsed not-ready-audio path writes ``fileName``, so read both.
        """
        return str(entry.get("filename") or entry.get("fileName") or "").strip()

    @staticmethod
    def _recompute_assessment_summary(
        assessments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Recompute the ``summary`` counters over a merged assessments list.

        Mirrors ``DocumentProcessor.process_batch``: ``successfulFiles`` counts
        entries whose ``success`` is truthy, ``failedFiles`` is the remainder,
        and ``averageReadabilityScore`` averages over the successful entries
        only. Note that a collapsed not-ready-audio entry carries no
        ``success`` key (see workspace_events.py), so it necessarily counts as
        not-successful here — the payload no longer holds the information the
        batch used.
        """
        success_count = 0
        total_score = 0.0
        for entry in assessments:
            if not isinstance(entry, dict) or not entry.get("success"):
                continue
            success_count += 1
            score = entry.get("readabilityScore", 0)
            if isinstance(score, (int, float)):
                total_score += float(score)
        avg_score = total_score / success_count if success_count else 0
        return {
            "totalFiles": len(assessments),
            "successfulFiles": success_count,
            "failedFiles": len(assessments) - success_count,
            "averageReadabilityScore": round(avg_score, 2),
        }

    def _merge_document_assessments(
        self,
        workspace_id: str,
        new_envelope: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge a freshly-built ``document_assessments`` envelope into the
        cached one, keyed by ``documentId``, and return the merged envelope.

        An incremental ``workspace_sync`` only processes the newly-uploaded
        files, so its envelope describes those files alone. Writing it to
        ``_workspace_document_assessments_payload`` verbatim used to *replace*
        the whole cache, which silently shrank the inventory that
        ``_build_document_inventory`` feeds to every feature prompt and the
        annexure count that ``_get_annexure_count`` reads — previously-uploaded
        documents then started showing up as ``missingInfo``. Merging keeps the
        cache a manifest of every live document.

        The caller still emits its own (new-files-only) envelope; only the
        cached copy is merged, so the incremental wire contract is unchanged
        while a reconnect replay now re-sends the complete set.

        Returns ``new_envelope`` unchanged when there is nothing cached yet.
        """
        new_inner = new_envelope.get("payload") or {}
        new_items = new_inner.get("documentAssessments")
        if not isinstance(new_items, list):
            return new_envelope

        cached = self._workspace_document_assessments_payload.get(workspace_id)
        prev_items: List[Dict[str, Any]] = []
        if isinstance(cached, dict):
            prev_inner = cached.get("payload") or {}
            maybe_prev = prev_inner.get("documentAssessments")
            if isinstance(maybe_prev, list):
                prev_items = [p for p in maybe_prev if isinstance(p, dict)]
        if not prev_items:
            return new_envelope

        merged: List[Dict[str, Any]] = list(prev_items)
        index_by_id = {
            str(p.get("documentId")): i
            for i, p in enumerate(merged)
            if p.get("documentId")
        }
        replaced = 0
        appended = 0
        for item in new_items:
            if not isinstance(item, dict):
                continue
            doc_id = str(item.get("documentId") or "").strip()
            existing = index_by_id.get(doc_id) if doc_id else None
            if existing is not None:
                merged[existing] = item
                replaced += 1
            else:
                if doc_id:
                    index_by_id[doc_id] = len(merged)
                merged.append(item)
                appended += 1

        logger.info(
            "Merged document assessments for workspace {}: {} carried over, "
            "{} replaced, {} appended ({} total)",
            str(workspace_id)[:12],
            len(prev_items),
            replaced,
            appended,
            len(merged),
        )
        merged_envelope = dict(new_envelope)
        merged_inner = dict(new_inner)
        merged_inner["documentAssessments"] = merged
        merged_inner["summary"] = self._recompute_assessment_summary(merged)
        merged_envelope["payload"] = merged_inner
        return merged_envelope

    def _prune_document_assessments(
        self,
        workspace_id: str,
        deleted_ids: Any,
    ) -> List[Dict[str, Any]]:
        """Drop deleted documents from the cached ``document_assessments``
        payload and return the entries that were removed.

        ``_build_document_inventory`` declares this cache "GROUND TRUTH for
        which documents are present" and instructs the agent not to flag
        anything listed there as missing. Deleting a document's vectors without
        pruning this cache therefore left the agent asserting the document was
        still in evidence — the reason a deleted invoice never appeared in
        ``missingInfo``. Must run *before* any regeneration that builds a
        prompt, so the inventory those prompts embed is already correct.

        The returned entries are the caller's snapshot for the removed-evidence
        ledger: once the cache is pruned this is the only remaining record that
        the document ever existed.
        """
        wanted = {str(d).strip() for d in (deleted_ids or []) if str(d).strip()}
        if not wanted:
            return []

        cached = self._workspace_document_assessments_payload.get(workspace_id)
        if not isinstance(cached, dict):
            return []
        inner = cached.get("payload") or {}
        assessments = inner.get("documentAssessments")
        if not isinstance(assessments, list) or not assessments:
            return []

        kept: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        for entry in assessments:
            if not isinstance(entry, dict):
                continue
            doc_id = str(entry.get("documentId") or "").strip()
            if doc_id and doc_id in wanted:
                removed.append(entry)
            else:
                kept.append(entry)

        if not removed:
            return []

        new_envelope = dict(cached)
        new_inner = dict(inner)
        new_inner["documentAssessments"] = kept
        new_inner["summary"] = self._recompute_assessment_summary(kept)
        new_envelope["payload"] = new_inner
        self._workspace_document_assessments_payload[workspace_id] = new_envelope
        logger.info(
            "Pruned {} assessment(s) for deleted document(s) in workspace {}; "
            "{} remain",
            len(removed),
            str(workspace_id)[:12],
            len(kept),
        )
        return removed

    @staticmethod
    def _build_document_assessment_envelope(
        workspace_id: str,
        user_id: str,
        batch_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build the ``document_assessments`` emit envelope from a
        ``process_batch`` result.

        Single implementation shared by the workspace_created, workspace_sync
        and workspace_version_restore ingestion paths, which each used to build
        this identical structure inline.

        Per-entry contract: ``documentType`` is the snake_case classifier enum,
        ``extractedFields`` holds masked/safe values for display, and
        ``sensitiveFields`` holds full raw identifiers for sealed encrypted
        storage on the backend — those MUST NOT be displayed. Audio entries add
        ``transcript`` + ``transcriptStatus``; when transcription is not ready
        ("pending") or errored ("failed") the entry collapses to the minimal
        ``{documentId, fileName, transcriptStatus}`` shape the backend expects
        (no quality / summary fields). Note that shape uses ``fileName`` while
        the normal path uses ``filename`` — read both via
        ``_assessment_entry_name``.
        """
        document_results: List[Dict[str, Any]] = []
        for result in batch_result.get("results", []):
            doc_result: Dict[str, Any] = {
                "documentId": result.get("document_id"),
                "filename": result.get("filename"),
                "success": result.get("success", False),
                "readabilityScore": result.get("readability_score", 0),
                "readabilityRating": result.get("readability_rating", "Unknown"),
                "readable": result.get("readable", False),
                "extractedTextLength": result.get("extracted_text_length", 0),
                "storedInVectorDb": result.get("stored_in_vector_db", False),
                "summary": result.get("summary", ""),
                "evidentiaryStrength": result.get("evidentiary_strength", "N/A"),
                "documentType": result.get("document_type", "other"),
                "extractedFields": result.get("extracted_fields", {}),
                "sensitiveFields": result.get("sensitive_fields", {}),
            }
            if result.get("is_audio"):
                status = result.get("transcript_status", "failed")
                transcript_text = result.get("transcript", "") or ""
                if status == "ready" and transcript_text:
                    doc_result["transcript"] = transcript_text
                    doc_result["transcriptStatus"] = "ready"
                else:
                    doc_result = {
                        "documentId": result.get("document_id"),
                        "fileName": result.get("filename"),
                        "transcriptStatus": (
                            status if status in ("pending", "failed") else "failed"
                        ),
                    }
            if not result.get("success") and "transcriptStatus" not in doc_result:
                doc_result["error"] = result.get("error", "Unknown error")
            document_results.append(doc_result)

        return {
            "featureType": "document_assessments",
            "workspaceId": workspace_id,
            "userId": user_id,
            "payload": {
                "documentAssessments": document_results,
                "summary": {
                    "totalFiles": batch_result.get("total_files", 0),
                    "successfulFiles": batch_result.get("successful", 0),
                    "failedFiles": batch_result.get("failed", 0),
                    "averageReadabilityScore": batch_result.get(
                        "average_readability_score", 0
                    ),
                },
            },
        }

    def _record_removed_documents(
        self,
        workspace_id: str,
        removed_entries: List[Dict[str, Any]],
    ) -> None:
        """Append pruned ``document_assessments`` entries to the
        removed-evidence ledger (``CacheKinds.REMOVED_DOCUMENTS``).

        Takes the snapshot returned by ``_prune_document_assessments`` — after
        that call the cache no longer holds any record of the document, so this
        ledger becomes the only memory that it ever existed. Dedupes by
        ``documentId`` so repeated deletes do not stack, and reassigns the list
        rather than mutating in place because ``PersistentWorkspaceDict`` only
        writes through on ``__setitem__``.
        """
        if not removed_entries:
            return

        existing = self._workspace_removed_documents.get(workspace_id) or []
        ledger = [e for e in existing if isinstance(e, dict)]
        seen = {
            str(e.get("documentId"))
            for e in ledger
            if e.get("documentId")
        }
        removed_at = datetime.now().isoformat()
        added = 0
        for entry in removed_entries:
            if not isinstance(entry, dict):
                continue
            doc_id = str(entry.get("documentId") or "").strip()
            if not doc_id or doc_id in seen:
                continue
            fields = entry.get("extractedFields")
            # Keep the ledger small — it rides the Qdrant state cache.
            key_fields: Dict[str, Any] = {}
            if isinstance(fields, dict):
                for key, value in list(fields.items())[:6]:
                    if value in (None, "", [], {}):
                        continue
                    key_fields[key] = value
            ledger.append(
                {
                    "documentId": doc_id,
                    "fileName": self._assessment_entry_name(entry),
                    "documentType": (entry.get("documentType") or "other"),
                    "keyFields": key_fields,
                    "removedAt": removed_at,
                }
            )
            seen.add(doc_id)
            added += 1

        if not added:
            return
        self._workspace_removed_documents[workspace_id] = ledger
        logger.info(
            "Recorded {} removed document(s) in the evidence ledger for "
            "workspace {} ({} total)",
            added,
            str(workspace_id)[:12],
            len(ledger),
        )

    def _forget_removed_documents(
        self,
        workspace_id: str,
        document_ids: Any,
    ) -> None:
        """Drop ledger entries for documents that are back in evidence.

        Called after a successful ingestion (normal upload or version restore)
        so a restored document stops being reported as missing. This is the
        mechanism that makes a restore self-healing even when it arrives as an
        ordinary ``workspace_sync``.
        """
        wanted = {str(d).strip() for d in (document_ids or []) if str(d).strip()}
        if not wanted:
            return
        existing = self._workspace_removed_documents.get(workspace_id)
        if not isinstance(existing, list) or not existing:
            return
        kept = [
            e
            for e in existing
            if isinstance(e, dict)
            and str(e.get("documentId") or "").strip() not in wanted
        ]
        if len(kept) == len(existing):
            return
        self._workspace_removed_documents[workspace_id] = kept
        logger.info(
            "Cleared {} removed-document ledger entry/entries for workspace {} "
            "(document(s) back in evidence); {} remain",
            len(existing) - len(kept),
            str(workspace_id)[:12],
            len(kept),
        )

    def _build_removed_documents_block(self, workspace_id: str) -> str:
        """Build the REMOVED EVIDENCE prompt block from the ledger.

        Injected into the analysis-report prompt *before* STEP 2 so it visibly
        outranks the DOCUMENT-TYPE COVERAGE TABLE and STRICT EXCLUSIONS that
        follow. Those two lists exist to stop the agent inventing generic asks
        for documents the user already provided; they must not suppress a
        document the user actually withdrew. Returns "" when the ledger is
        empty so callers can no-op cleanly.
        """
        ledger = self._workspace_removed_documents.get(workspace_id)
        if not isinstance(ledger, list) or not ledger:
            return ""

        lines: List[str] = []
        for entry in ledger:
            if not isinstance(entry, dict):
                continue
            doc_type = str(entry.get("documentType") or "other").strip()
            name = str(entry.get("fileName") or "").strip()
            removed_at = str(entry.get("removedAt") or "").strip()[:10]
            fields = entry.get("keyFields") or {}
            field_pairs: List[str] = []
            if isinstance(fields, dict):
                for key, value in fields.items():
                    if isinstance(value, list):
                        if not value:
                            continue
                        s_val = ", ".join(str(v) for v in value[:3])
                    elif isinstance(value, (str, int, float, bool)):
                        s_val = str(value).strip()
                    else:
                        continue
                    if not s_val:
                        continue
                    if len(s_val) > 80:
                        s_val = s_val[:77] + "…"
                    field_pairs.append(f"{key}={s_val}")
            descriptor = doc_type
            if name:
                descriptor += f' — was "{name}"'
            if removed_at:
                descriptor += f", removed {removed_at}"
            if field_pairs:
                descriptor += " | " + "; ".join(field_pairs)
            lines.append(f"- {descriptor}")

        if not lines:
            return ""

        header = (
            "REMOVED EVIDENCE (previously uploaded by the user, since deleted "
            "— NOT available as evidence):"
        )
        rules = (
            "REMOVED-EVIDENCE RULES:\n"
            "- These documents are NOT in evidence. Never cite them, never "
            "reference them as an Annexure, and never treat their contents as "
            "proved.\n"
            "- For each removed documentType that is material to the strategic "
            "objective, you MUST emit one missingInfo item naming the document, "
            "stating that it was previously on record and has been removed, and "
            "explaining why it matters.\n"
            "- This rule OVERRIDES the DOCUMENT-TYPE COVERAGE TABLE and the "
            "STRICT EXCLUSIONS below: a removed invoice / receipt / proof of "
            "purchase MUST be flagged as missing."
        )
        return header + "\n" + "\n".join(lines) + "\n\n" + rules

    def _removed_evidence_missing_info_items(
        self,
        workspace_id: str,
    ) -> List[Dict[str, str]]:
        """Deterministic ``missingInfo`` text for each removed document.

        The REMOVED-EVIDENCE prompt rules ask the agent to raise these itself,
        but prompt compliance is not a guarantee — this is the layer that makes
        a withdrawn document *always* visible as a gap. Both this and
        ``_apply_removed_evidence_missing_info`` derive from the same ledger, so
        the generated text and the rejection-memory exemption agree exactly.
        """
        ledger = self._workspace_removed_documents.get(workspace_id)
        if not isinstance(ledger, list) or not ledger:
            return []

        items: List[Dict[str, str]] = []
        for entry in ledger:
            if not isinstance(entry, dict):
                continue
            doc_type = str(entry.get("documentType") or "other").strip()
            type_label = doc_type.replace("_", " ").strip() or "document"
            name = str(entry.get("fileName") or "").strip()

            fields = entry.get("keyFields") or {}
            field_bits: List[str] = []
            if isinstance(fields, dict):
                for key, value in list(fields.items())[:3]:
                    if isinstance(value, list):
                        if not value:
                            continue
                        s_val = ", ".join(str(v) for v in value[:2])
                    elif isinstance(value, (str, int, float, bool)):
                        s_val = str(value).strip()
                    else:
                        continue
                    if not s_val:
                        continue
                    if len(s_val) > 60:
                        s_val = s_val[:57] + "…"
                    field_bits.append(f"{key.replace('_', ' ')} {s_val}")
            fields_clause = f" ({'; '.join(field_bits)})" if field_bits else ""
            name_clause = f' ("{name}")' if name else ""

            text = (
                f"The {type_label}{name_clause} that was previously on record in "
                f"this matter has been removed from the evidence set"
                f"{fields_clause}. It is no longer available to rely on, so every "
                f"fact it established must now be proved by other means. Produce "
                f"the document again, or obtain a certified copy from the issuing "
                f"party, before the matter is advanced on this point."
            )
            items.append(
                {
                    "documentId": str(entry.get("documentId") or ""),
                    "documentType": doc_type,
                    "text": text,
                }
            )
        return items

    def _apply_removed_evidence_missing_info(
        self,
        workspace_id: str,
        report: Dict[str, Any],
        *,
        max_items: int = 8,
    ) -> Dict[str, Any]:
        """Guarantee one ``missingInfo`` entry per removed document.

        Appends the deterministic text from
        ``_removed_evidence_missing_info_items`` for any removed documentType
        the agent did not already raise, then re-clamps
        ``caseResolutionScore`` — the score was computed before this append and
        100 must never be shown alongside a non-empty ``missingInfo``.

        Skips a documentType already named in an existing entry so the agent's
        own (better, fact-grounded) wording wins when it complied.
        """
        if not isinstance(report, dict):
            return report
        candidates = self._removed_evidence_missing_info_items(workspace_id)
        if not candidates:
            return report

        existing = report.get("missingInfo")
        if not isinstance(existing, list):
            existing = []
        # Entries are plain strings at this point (the {id,text,isMatched} shape
        # is stamped later by _tag_analysis_report_against_current_state).
        existing_blob = " ".join(
            e if isinstance(e, str) else str(e.get("text", ""))
            for e in existing
            if isinstance(e, (str, dict))
        ).lower()

        appended = 0
        skipped_present = 0
        for cand in candidates:
            type_words = cand["documentType"].replace("_", " ").strip().lower()
            if type_words and type_words in existing_blob:
                skipped_present += 1
                continue
            if len(existing) >= max_items:
                logger.warning(
                    "Removed-evidence missingInfo for {!r} dropped: the {}-item "
                    "cap is already full for workspace {}",
                    cand["documentType"],
                    max_items,
                    str(workspace_id)[:12],
                )
                continue
            existing.append(cand["text"])
            appended += 1

        if not appended:
            if skipped_present:
                logger.info(
                    "Removed-evidence gap(s) already raised by the agent for "
                    "workspace {} ({} documentType(s))",
                    str(workspace_id)[:12],
                    skipped_present,
                )
            return report

        report["missingInfo"] = existing
        logger.info(
            "Added {} deterministic removed-evidence missingInfo item(s) for "
            "workspace {} ({} already raised by the agent)",
            appended,
            str(workspace_id)[:12],
            skipped_present,
        )
        # missingInfo just became non-empty (or grew): re-apply the 95% cap.
        try:
            max_allowed = self._analysis_report_max_allowed_score(report=report)
            score = report.get("caseResolutionScore")
            if isinstance(score, (int, float)) and score > max_allowed:
                report["caseResolutionScore"] = max_allowed
                logger.info(
                    "Re-clamped caseResolutionScore {} → {} after adding "
                    "removed-evidence gaps",
                    score,
                    max_allowed,
                )
        except Exception as clamp_err:
            logger.debug("Score re-clamp after removed-evidence append: {}", clamp_err)
        return report

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
