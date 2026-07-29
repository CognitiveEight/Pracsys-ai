"""Strategy & objective generation, edits, and intent classification.

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
from app.ai.usage import current_usage, meter_usage
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
    snap_years_in_text,
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
    find_ungrounded_proper_nouns,
    _strip_strength_point_statute_boilerplate,
    _annexure_wording_for_next_steps,
    _clamp_annexure_references,
    _format_document_for_prompt,
    _normalize_report_mobile_from_overview,
    _overview_conversation_date_to_iso,
    extract_overview_conversation_report_contact_meta,
)


from app.prompts.strategy_prompts import (
    generate_strategies_from_documents_prompt,
    generate_strategies_from_documents_prompt2,
    generate_strategies_from_documents_prompt3,
    generate_strategies_from_documents_prompt4,
    generate_strategies_from_documents_prompt5,
    generate_strategies_from_documents_prompt6,
    regenerate_strategy_objective_preserving_case_summary_prompt,
    reground_strategy_objective_prompt,
    regenerate_case_summary_with_feedback_prompt,
    regenerate_next_steps_with_feedback_prompt,
    regenerate_strategic_objective_with_feedback_prompt,
    summarize_objective_instruction_from_thread_prompt,
    should_offer_objective_update_after_discussion_prompt,
    llm_classify_objective_change_intent_prompt,
    llm_classify_panel_edit_intent_prompt,
    llm_classify_panel_edit_intent_prompt2,
    panel_edit_ack_prompt,
)


class StrategyMixin:
    """Strategy & objective generation, edits, and intent classification."""

    async def _generate_strategies_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate detailed, document-specific strategies and objectives using AI analysis.
        Uses ALL documents currently in the workspace (from vector store), in upload order.
        If documents include multiple matters, the model is instructed to call out ambiguity
        and prioritize the dominant evidence-backed dispute.

        Args:
            workspace_id: Workspace identifier
            user_id: User identifier
            workspace_role: User's workspace role
            batch_result: Optional document processing batch result (for readability stats); if None, uses only vector store
            is_update: Whether this is an update (new docs added / doc deleted) or initial generation

        Returns:
            Strategy generation result payload or None if failed
        """
        try:
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            logger.info(
                f"🧠 Generating detailed strategies for workspace {workspace_id} with role {workspace_role}"
            )

            # Build documents_info from batch_result when available (for analysis metadata)
            documents_info = []
            if batch_result:
                for result in batch_result.get("results", []):
                    if result.get("success") and result.get("readable"):
                        documents_info.append(
                            {
                                "document_id": result.get("document_id"),
                                "filename": result.get("filename"),
                                "readability_score": result.get("readability_score", 0),
                                "text_length": result.get("extracted_text_length", 0),
                                "stored_in_vector": result.get(
                                    "stored_in_vector_db", False
                                ),
                            }
                        )

            # Retrieve ALL document contents from vector store (in upload order: first → latest)
            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
                logger.info(
                    f"Retrieved {len(all_docs)} document(s) from vector store for strategy generation"
                )
            except Exception as vector_error:
                logger.warning(
                    f"Could not retrieve documents from vector store: {vector_error}"
                )

            # Build combined content with a fair per-document cap so all documents are represented.
            parts = []
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
            if not all_docs:
                combined_document_content = "No document content available."
            else:
                for i, doc in enumerate(all_docs, 1):
                    content = (doc.get("content") or "").strip()
                    if not content or len(content) < 20:
                        continue
                    if len(content) > per_doc_cap:
                        content = (
                            content[:per_doc_cap] + "\n[... truncated for length ...]"
                        )
                    # Build labelled block with metadata header so the
                    # prompt distinguishes drafted-by-assistant documents
                    # (completed client-side actions) from raw evidence.
                    doc_with_clip = dict(doc)
                    doc_with_clip["content"] = content
                    parts.append(
                        _format_document_for_prompt(
                            doc_with_clip, i, len(all_docs)
                        )
                    )
                combined_document_content = (
                    "\n\n".join(parts) if parts else "No document content available."
                )
            if len(combined_document_content) > max_total_content:
                combined_document_content = (
                    combined_document_content[:max_total_content]
                    + "\n[... content truncated ...]"
                )
                logger.info(
                    "Strategy generation content truncated to ~{} chars ({} docs, ~{} chars each)",
                    max_total_content,
                    len(valid_docs),
                    per_doc_cap,
                )
            num_annexures = len(valid_docs)
            if num_annexures <= 0:
                annexure_limit_block = (
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    "- No annexures have been uploaded.\n"
                    "- Do NOT reference any 'Annexure N' anywhere in the output."
                )
            elif num_annexures == 1:
                annexure_limit_block = (
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    "- The user has uploaded exactly 1 annexure.\n"
                    "- The ONLY valid annexure reference is: Annexure 1.\n"
                    "- You MUST NOT reference Annexure 2 or any higher number.\n"
                    "- Do NOT invent additional annexures. Do NOT use the plural form \"annexures\"."
                )
            else:
                annexure_limit_block = (
                    generate_strategies_from_documents_prompt3(num_annexures=num_annexures)
                )

            # Pass 1: "deep agents" extraction — three focused sub-agents run
            # in parallel (parties, timeline, assets+law). Each sub-agent logs
            # its own counts; here we add one final summary line listing the
            # actual party names so the synthesis output can be quickly audited.
            case_entities: Dict[str, Any] = await self._extract_case_entities(
                workspace_id=workspace_id,
                combined_document_content=combined_document_content,
            )
            _extracted_party_names = [
                (p.get("name") or "").strip()
                for p in (case_entities.get("parties") or [])
                if isinstance(p, dict) and (p.get("name") or "").strip()
            ]
            logger.info(
                "🧠 Strategy deep-extraction complete for workspace {}: parties=[{}]",
                workspace_id[:12] if workspace_id else "",
                ", ".join(_extracted_party_names) if _extracted_party_names else "(none)",
            )
            # Diagnostic: dump first 1500 chars of combined_document_content so
            # we can tell at a glance whether names ARE present in what we send
            # to Gemini when the extracted parties list comes back empty.
            logger.debug(
                "Strategy pass 1 combined_document_content preview (first 1500 chars): {}",
                (combined_document_content or "")[:1500],
            )

            # When no documents (e.g. all deleted), return fallback so backend can clear strategy / show empty state
            doc_count = len(all_docs)
            report_mobile, report_contact_date = (
                extract_overview_conversation_report_contact_meta(all_docs)
            )
            if doc_count == 0:
                fallback_payload = {
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "role": workspace_role,
                    "isUpdate": is_update,
                    "isEmpty": True,
                    "caseSummary": "",
                    "reportCaseSummary": "",
                    "strategicObjective": "",
                    "possibleNextSteps": [],
                    "suggestionChips": [],
                    "reportMobileNumber": None,
                    "reportContactedDate": None,
                    "analysis": {
                        "documentCount": 0,
                        "averageReadability": 0,
                        "totalDocuments": 0,
                        "successfulDocuments": 0,
                        "basedOnAIAnalysis": False,
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                }
                logger.info(
                    "No documents in workspace; returning empty strategy payload (isEmpty=true)"
                )
                return fallback_payload

            # Generate AI-powered case summary and strategic objective
            logger.info(
                "🤖 Generating AI-powered case analysis (all documents, upload order)..."
            )

            # Current-date context for year resolution: a source date stated
            # without a year defaults to the current year (mirrors list_of_dates).
            _today = datetime.utcnow()
            today_str = _today.strftime("%Y-%m-%d")
            current_year = _today.strftime("%Y")

            analysis_prompt = generate_strategies_from_documents_prompt(combined_document_content=combined_document_content, workspace_role=workspace_role)

            try:
                strategy_inventory = self._build_document_inventory(workspace_id)
                import json as _json_strategy
                try:
                    _entities_block = _json_strategy.dumps(
                        case_entities, indent=2, ensure_ascii=False
                    )
                except Exception:
                    _entities_block = "{}"
                # Improved prompt for better JSON generation. Inject any
                # user-confirmed role->party anchor so a later regeneration
                # (e.g. after a document-upload sync) keeps advocating for the
                # party the user pinned via chat, instead of re-guessing.
                _confirmed_party = self._resolve_confirmed_role_party(
                    workspace_id, (workspace_role or "").strip().upper()
                )
                structured_prompt = generate_strategies_from_documents_prompt2(SPEAKER_LABEL_GUARDRAIL=SPEAKER_LABEL_GUARDRAIL, _entities_block=_entities_block, annexure_limit_block=annexure_limit_block, combined_document_content=combined_document_content, strategy_inventory=strategy_inventory, workspace_role=workspace_role, current_year=current_year, today_str=today_str, confirmed_party=_confirmed_party)
                custom_suffix = build_custom_instruction_suffix(workspace_id)
                if custom_suffix:
                    structured_prompt = (
                        structured_prompt.rstrip() + "\n\n" + custom_suffix
                    )
                ai_response = await gemini_client.generate(
                    prompt=structured_prompt,
                    system_instruction=get_structured_document_feature_system_instruction(
                        workspace_role
                    ),
                    temperature=0.2,
                    max_tokens=12000,
                    response_mime_type="application/json",
                )

                logger.info(f"AI generated {len(ai_response)} characters of analysis")
                logger.info("=" * 80)
                logger.info("🤖 RAW AI RESPONSE:")
                logger.info("-" * 80)
                logger.info(ai_response)
                logger.info("=" * 80)

                # Try to parse JSON response with shared repair logic
                import json
                import re

                ai_response = ai_response.strip()
                json_str = self._prepare_json_candidate(ai_response, prefer="object")
                if json_str:
                    logger.info("✅ Extracted JSON candidate for strategy analysis")
                else:
                    logger.warning("❌ No JSON found in AI response")

                if json_str:
                    # Try to parse JSON
                    try:
                        analysis_data = json.loads(json_str)

                        case_summary = analysis_data.get("caseSummary", "")
                        strategic_objective = analysis_data.get(
                            "strategicObjective", ""
                        )

                        # Pass 3 (verifier): if the synthesis dropped any
                        # extracted party name from caseSummary or
                        # strategicObjective, ask Gemini to rewrite all three
                        # fields preserving every missing item. One retry only.
                        try:
                            _missing_report = self._verify_entities_in_summary(
                                f"{case_summary}\n{strategic_objective}",
                                case_entities,
                            )
                        except Exception as ver_err:
                            logger.debug(
                                "Entity verifier failed (non-fatal): {}", ver_err
                            )
                            _missing_report = {
                                "missing_parties": [],
                                "missing_suit_numbers": [],
                                "missing_key_dates": [],
                            }
                        _missing_parties = _missing_report.get("missing_parties") or []
                        _missing_suits = _missing_report.get("missing_suit_numbers") or []
                        _missing_dates = _missing_report.get("missing_key_dates") or []
                        _any_missing = bool(_missing_parties or _missing_suits or _missing_dates)
                        if _any_missing and (
                            case_entities.get("parties")
                            or case_entities.get("suit_numbers")
                            or case_entities.get("dates")
                        ):
                            logger.warning(
                                "Strategy synthesis dropped items for workspace {}: "
                                "parties={} suits={} dates={}; retrying once with explicit directive",
                                workspace_id[:12] if workspace_id else "",
                                _missing_parties,
                                _missing_suits,
                                _missing_dates,
                            )
                            try:
                                _report_case_summary = analysis_data.get(
                                    "reportCaseSummary", ""
                                )
                                _missing_blocks: List[str] = []
                                if _missing_parties:
                                    _missing_blocks.append(
                                        "PARTY NAMES you omitted (use verbatim, exact spellings):\n- "
                                        + "\n- ".join(_missing_parties)
                                    )
                                if _missing_suits:
                                    _missing_blocks.append(
                                        "SUIT / CASE / FIR NUMBERS you omitted (must appear verbatim):\n- "
                                        + "\n- ".join(_missing_suits)
                                    )
                                if _missing_dates:
                                    _missing_blocks.append(
                                        "KEY DATES you omitted (use DD/MM/YYYY):\n- "
                                        + "\n- ".join(_missing_dates)
                                    )
                                _verify_prompt = (
                                    "You produced the following caseSummary, "
                                    "reportCaseSummary, and strategicObjective for a "
                                    "legal matter, but you OMITTED these load-bearing "
                                    "facts from the extracted ground-truth:\n\n"
                                    + "\n\n".join(_missing_blocks)
                                    + generate_strategies_from_documents_prompt5(_report_case_summary=_report_case_summary, case_summary=case_summary, strategic_objective=strategic_objective)
                                )
                                _verify_raw = await gemini_client.generate(
                                    prompt=_verify_prompt,
                                    system_instruction=get_structured_document_feature_system_instruction(
                                        workspace_role
                                    ),
                                    temperature=0.1,
                                    max_tokens=12000,
                                    response_mime_type="application/json",
                                )
                                _verify_json = self._prepare_json_candidate(
                                    (_verify_raw or "").strip(), prefer="object"
                                )
                                if _verify_json:
                                    _verify_parsed = json.loads(_verify_json)
                                    if isinstance(_verify_parsed, dict):
                                        _new_cs = (
                                            _verify_parsed.get("caseSummary") or ""
                                        ).strip()
                                        _new_rcs = (
                                            _verify_parsed.get("reportCaseSummary")
                                            or ""
                                        ).strip()
                                        _new_so = (
                                            _verify_parsed.get("strategicObjective")
                                            or ""
                                        ).strip()
                                        if _new_cs:
                                            analysis_data["caseSummary"] = _new_cs
                                            case_summary = _new_cs
                                        if _new_rcs:
                                            analysis_data["reportCaseSummary"] = _new_rcs
                                        if _new_so:
                                            analysis_data[
                                                "strategicObjective"
                                            ] = _new_so
                                            strategic_objective = _new_so
                                        # Re-check after retry so we can tell at a
                                        # glance whether the retry actually closed
                                        # the gap.
                                        _still = self._verify_entities_in_summary(
                                            f"{case_summary}\n{strategic_objective}",
                                            case_entities,
                                        )
                                        _still_any = bool(
                                            (_still.get("missing_parties") or [])
                                            or (_still.get("missing_suit_numbers") or [])
                                            or (_still.get("missing_key_dates") or [])
                                        )
                                        if _still_any:
                                            logger.warning(
                                                "Entity verifier retry STILL missing for "
                                                "workspace {}: parties={} suits={} dates={}",
                                                workspace_id[:12] if workspace_id else "",
                                                _still.get("missing_parties") or [],
                                                _still.get("missing_suit_numbers") or [],
                                                _still.get("missing_key_dates") or [],
                                            )
                                        else:
                                            logger.success(
                                                "Entity verifier retry recovered all missing items "
                                                "(parties={} suits={} dates={}) for workspace {}",
                                                len(_missing_parties),
                                                len(_missing_suits),
                                                len(_missing_dates),
                                                workspace_id[:12] if workspace_id else "",
                                            )
                            except Exception as retry_err:
                                logger.warning(
                                    "Entity verifier retry failed (non-fatal): {}",
                                    retry_err,
                                )

                        # Strip UI-truncated labels ("Smart Prep…", "MSEDCL
                        # Mahavitaran - Maharashtra ...") that leaked from
                        # OCR of payment-app screenshots. Belt-and-braces
                        # alongside the TRUNCATED-TEXT prompt rule above.
                        # Keeps local vars in sync so the recovery prompt
                        # below sees the sanitized narrative as context.
                        _cs_clean = _strip_ui_truncated_tokens(
                            analysis_data.get("caseSummary") or ""
                        )
                        _rcs_clean = _strip_ui_truncated_tokens(
                            analysis_data.get("reportCaseSummary") or ""
                        )
                        _so_clean = _strip_ui_truncated_tokens(
                            analysis_data.get("strategicObjective") or ""
                        )
                        if _cs_clean != (analysis_data.get("caseSummary") or "") or (
                            _rcs_clean != (analysis_data.get("reportCaseSummary") or "")
                        ) or _so_clean != (analysis_data.get("strategicObjective") or ""):
                            logger.info(
                                "Stripped UI-truncated tokens from strategy narrative "
                                "(workspace {})",
                                workspace_id[:12] if workspace_id else "",
                            )
                        analysis_data["caseSummary"] = _cs_clean
                        analysis_data["reportCaseSummary"] = _rcs_clean
                        analysis_data["strategicObjective"] = _so_clean
                        case_summary = _cs_clean
                        strategic_objective = _so_clean

                        possible_next_steps = (
                            analysis_data.get("possibleNextSteps")
                            or analysis_data.get("possible_next_steps")
                            or []
                        )
                        if isinstance(possible_next_steps, str):
                            possible_next_steps = (
                                [possible_next_steps] if possible_next_steps else []
                            )
                        possible_next_steps = [
                            str(s).strip() for s in possible_next_steps if s
                        ][:5]
                        suggestion_chips = (
                            analysis_data.get("suggestionChips")
                            or analysis_data.get("suggestion_chips")
                            or []
                        )
                        if isinstance(suggestion_chips, str):
                            suggestion_chips = (
                                [suggestion_chips] if suggestion_chips else []
                            )
                        suggestion_chips = [
                            str(s).strip() for s in suggestion_chips if s
                        ][:16]

                        # If the objective or the tail arrays are missing/short
                        # due to partial AI output, recover them with a focused
                        # follow-up call. Objective threshold = 200 chars
                        # because the main prompt demands 10-20 sentences and
                        # anything shorter is implausibly thin (and triggers
                        # the generic hardcoded fallback below).
                        strategic_objective_missing = (
                            not strategic_objective
                            or len(strategic_objective.strip()) < 200
                        )
                        next_steps_missing = len(possible_next_steps) < 3
                        chips_missing = len(suggestion_chips) < 10
                        if (
                            strategic_objective_missing
                            or next_steps_missing
                            or chips_missing
                        ):
                            try:
                                # Build the recovery prompt conditionally —
                                # only ask Gemini for the fields actually
                                # missing, preserving whatever the first call
                                # produced correctly.
                                _requested_fields = []
                                _field_specs = []
                                if strategic_objective_missing:
                                    _requested_fields.append(
                                        '"strategicObjective": "..."'
                                    )
                                    _field_specs.append(
                                        generate_strategies_from_documents_prompt6(workspace_role=workspace_role)
                                    )
                                if next_steps_missing:
                                    _requested_fields.append(
                                        '"possibleNextSteps": ["..."]'
                                    )
                                    _field_specs.append(
                                        "- possibleNextSteps: exactly 3-5 "
                                        "immediate, practical actions."
                                    )
                                if chips_missing:
                                    _requested_fields.append(
                                        '"suggestionChips": ["..."]'
                                    )
                                    _field_specs.append(
                                        "- suggestionChips: exactly 15-16 "
                                        "short labels (2-5 words each), "
                                        "actionable and case-specific."
                                    )

                                try:
                                    _entities_block_recovery = json.dumps(
                                        case_entities,
                                        indent=2,
                                        ensure_ascii=False,
                                    )
                                except Exception:
                                    _entities_block_recovery = "{}"

                                _fields_json = ",\n  ".join(_requested_fields)
                                _rules_block = "\n".join(_field_specs)
                                recovery_prompt = generate_strategies_from_documents_prompt4(_entities_block_recovery=_entities_block_recovery, _fields_json=_fields_json, _rules_block=_rules_block, case_summary=case_summary, combined_document_content=combined_document_content, strategic_objective=strategic_objective, workspace_role=workspace_role)
                                recovery_raw = await gemini_client.generate(
                                    prompt=recovery_prompt,
                                    system_instruction=get_structured_document_feature_system_instruction(
                                        workspace_role
                                    ),
                                    temperature=0.1,
                                    max_tokens=12000,
                                    response_mime_type="application/json",
                                )
                                recovery_json = self._prepare_json_candidate(
                                    (recovery_raw or "").strip(), prefer="object"
                                )
                                recovered_data = (
                                    json.loads(recovery_json)
                                    if recovery_json
                                    else json.loads((recovery_raw or "").strip())
                                )
                                if isinstance(recovered_data, dict):
                                    recovered_objective_raw = (
                                        recovered_data.get("strategicObjective")
                                        or recovered_data.get("strategic_objective")
                                        or ""
                                    )
                                    if isinstance(recovered_objective_raw, str):
                                        recovered_objective = _strip_ui_truncated_tokens(
                                            recovered_objective_raw.strip()
                                        )
                                    else:
                                        recovered_objective = ""

                                    recovered_next_steps = (
                                        recovered_data.get("possibleNextSteps")
                                        or recovered_data.get("possible_next_steps")
                                        or []
                                    )
                                    if isinstance(recovered_next_steps, str):
                                        recovered_next_steps = (
                                            [recovered_next_steps]
                                            if recovered_next_steps
                                            else []
                                        )
                                    recovered_next_steps = [
                                        str(s).strip()
                                        for s in recovered_next_steps
                                        if str(s).strip()
                                    ][:5]

                                    recovered_chips = (
                                        recovered_data.get("suggestionChips")
                                        or recovered_data.get("suggestion_chips")
                                        or []
                                    )
                                    if isinstance(recovered_chips, str):
                                        recovered_chips = (
                                            [recovered_chips] if recovered_chips else []
                                        )
                                    recovered_chips = [
                                        str(s).strip()
                                        for s in recovered_chips
                                        if str(s).strip()
                                    ][:16]

                                    if strategic_objective_missing and recovered_objective:
                                        strategic_objective = recovered_objective
                                        analysis_data["strategicObjective"] = recovered_objective
                                    if len(possible_next_steps) < 3 and recovered_next_steps:
                                        possible_next_steps = recovered_next_steps
                                    if len(suggestion_chips) < 10 and recovered_chips:
                                        suggestion_chips = recovered_chips

                                    logger.info(
                                        "Recovered strategy tail fields: objective={} chars, nextSteps={}, chips={}",
                                        len(strategic_objective),
                                        len(possible_next_steps),
                                        len(suggestion_chips),
                                    )
                            except Exception as recovery_err:
                                logger.warning(
                                    "Could not recover strategy tail fields: {}",
                                    recovery_err,
                                )

                        report_raw = analysis_data.get(
                            "reportCaseSummary"
                        ) or analysis_data.get("report_case_summary")
                        if isinstance(report_raw, str):
                            report_case_summary = report_raw.strip()
                        elif report_raw is None:
                            report_case_summary = ""
                        else:
                            report_case_summary = str(report_raw).strip()

                        # Validate we got meaningful content
                        if case_summary and strategic_objective:
                            logger.success("✅ AI analysis completed successfully")
                            logger.info(f"   - Case Summary: {len(case_summary)} chars")
                            logger.info(
                                f"   - Report case summary: {len(report_case_summary)} chars"
                            )
                            logger.info(
                                f"   - Strategic Objective: {len(strategic_objective)} chars"
                            )
                            logger.info(
                                f"   - Possible Next Steps: {len(possible_next_steps)} items"
                            )
                            logger.info(
                                f"   - Suggestion chips: {len(suggestion_chips)} items"
                            )
                        else:
                            logger.warning(
                                "⚠️ AI response incomplete, using partial data + fallback"
                            )
                            # Use what we got and fill in missing pieces
                            if not case_summary:
                                case_summary = f"Legal matter for {workspace_role} role based on {doc_count} document(s). The documents have been analyzed and contain important case information."
                            if not strategic_objective:
                                strategic_objective = "Conduct a comprehensive legal review to determine the optimal course of action based on the available evidence and applicable BNS provisions."
                            if not possible_next_steps or len(possible_next_steps) < 3:
                                possible_next_steps = (
                                    possible_next_steps
                                    + [
                                        "Review all uploaded documents immediately",
                                        "Preserve and secure all evidence",
                                        "Consult with legal counsel",
                                        "Identify and contact relevant parties",
                                        "Prepare initial documentation",
                                    ][:5]
                                )  # Ensure we have at least 3-5
                            if not suggestion_chips:
                                suggestion_chips = [
                                    "Review documents",
                                    "Analyze provisions",
                                    "Draft action plan",
                                    "Review evidence",
                                    "File legal action",
                                    "Preserve documents",
                                    "Map legal heirs",
                                    "Draft demand notice",
                                    "Check BNS provisions",
                                    "Assess evidence",
                                    "Follow BNSS steps",
                                    "Organize documents",
                                    "Consult applicable laws",
                                    "Prepare chronology",
                                    "Identify parties",
                                    "Plan next steps",
                                ][:16]
                            logger.info("✅ Using hybrid AI + fallback content")

                        if not report_case_summary:
                            report_case_summary = _derive_report_case_summary(
                                case_summary
                            )

                    except json.JSONDecodeError as json_error:
                        logger.error(f"❌ JSON parsing failed: {json_error}")
                        logger.info(
                            f"Failed JSON string (first 500 chars): {json_str[:500]}"
                        )

                        # Try to extract partial information using regex
                        # Handle cases where string values are incomplete (no closing quote)
                        case_summary_match = re.search(
                            r'"caseSummary"\s*:\s*"([^"]*)', json_str
                        )
                        objective_match = re.search(
                            r'"strategicObjective"\s*:\s*"([^"]*)', json_str
                        )
                        report_summary_match = re.search(
                            r'"reportCaseSummary"\s*:\s*"([^"]*)', json_str
                        )
                        if case_summary_match or objective_match:
                            logger.info("✅ Extracted partial data from malformed JSON")
                            case_summary = (
                                case_summary_match.group(1)
                                if case_summary_match
                                else ""
                            )
                            strategic_objective = (
                                objective_match.group(1) if objective_match else ""
                            )
                            report_case_summary = (
                                report_summary_match.group(1).strip()
                                if report_summary_match
                                else ""
                            )

                            # Extract possible next steps from the partial JSON
                            possible_next_steps = []
                            suggestion_chips = []
                            possible_next_steps_match = re.search(
                                r'"possibleNextSteps"\s*:\s*\[(.*?)\]',
                                json_str,
                                re.DOTALL,
                            )
                            if not possible_next_steps_match:
                                possible_next_steps_match = re.search(
                                    r'"possible_next_steps"\s*:\s*\[(.*?)\]',
                                    json_str,
                                    re.DOTALL,
                                )
                            if possible_next_steps_match:
                                next_steps_inner = possible_next_steps_match.group(1)
                                next_steps = re.findall(r'"([^"]+)"', next_steps_inner)
                                possible_next_steps = [
                                    s.strip() for s in next_steps if s.strip()
                                ][:5]

                            logger.info(
                                f"   Extracted: Summary={bool(case_summary)}, Objective={bool(strategic_objective)}, NextSteps={len(possible_next_steps)}"
                            )

                            # Fill in missing pieces
                            if not case_summary:
                                raise  # Can't salvage without case summary, use full fallback
                            if not strategic_objective:
                                strategic_objective = "Conduct a comprehensive legal review based on the case facts to determine the optimal course of action under BNS."
                            if not suggestion_chips:
                                suggestion_chips = [
                                    "Review evidence",
                                    "File legal action",
                                    "Preserve documents",
                                    "Review documents",
                                    "Analyze provisions",
                                    "Draft action plan",
                                    "Map legal heirs",
                                    "Draft demand notice",
                                    "Check BNS provisions",
                                    "Assess evidence",
                                    "Follow BNSS steps",
                                    "Organize documents",
                                    "Consult applicable laws",
                                    "Prepare chronology",
                                    "Identify parties",
                                ][:16]

                            if not report_case_summary:
                                report_case_summary = _derive_report_case_summary(
                                    case_summary
                                )

                            logger.success(
                                "✅ Using hybrid: AI-extracted content + fallback where needed"
                            )
                        else:
                            raise  # No usable content, use full fallback
                else:
                    logger.error("❌ Could not extract JSON from AI response")
                    raise ValueError("No JSON in response")

            except Exception as ai_error:
                logger.error(f"AI analysis failed: {ai_error}")
                logger.warning("Using fallback generic analysis")
                # Fallback to generic analysis
                case_summary = f"Analysis of {doc_count} document(s) for {workspace_role} role. The documents have been processed and are ready for detailed review under Indian legal framework (BNS, BNSS, BSA)."
                report_case_summary = (
                    f"Analysis of {doc_count} document(s). The documents have been processed "
                    "and are ready for detailed review under Indian legal framework (BNS, BNSS, BSA)."
                )
                strategic_objective = "Conduct a comprehensive legal review to determine the optimal course of action based on the available evidence and applicable provisions under Bharatiya Nyaya Sanhita (BNS) and other relevant laws."
                possible_next_steps = [
                    "Review all uploaded documents immediately",
                    "Preserve and secure all evidence",
                    "Consult with legal counsel",
                    "Identify and contact relevant parties",
                    "Prepare initial documentation",
                ]
                suggestion_chips = [
                    "Review documents",
                    "Analyze BNS provisions",
                    "Assess evidence",
                    "Draft action plan",
                    "Review evidence",
                    "File legal action",
                    "Preserve documents",
                    "Map legal heirs",
                    "Draft demand notice",
                    "Check BNS provisions",
                    "Follow BNSS steps",
                    "Organize documents",
                    "Consult applicable laws",
                    "Prepare chronology",
                    "Identify parties",
                    "Plan next steps",
                ][:16]

            _raw_next_steps = list(possible_next_steps or [])
            possible_next_steps = []
            for s in _raw_next_steps:
                t = _annexure_wording_for_next_steps(str(s).strip())
                if t:
                    possible_next_steps.append(t)

            # Clamp any "Annexure N" reference where N exceeds the number of
            # uploaded annexures. The strategy prompt asks the model to use
            # Annexure-N wording, but with a single upload the model has
            # historically hallucinated Annexure 2 / Annexure 3 — this is the
            # safety net that guarantees no such reference reaches the client.
            case_summary = _clamp_annexure_references(case_summary, num_annexures)
            report_case_summary = _clamp_annexure_references(
                report_case_summary, num_annexures
            )
            strategic_objective = _clamp_annexure_references(
                strategic_objective, num_annexures
            )
            possible_next_steps = [
                _clamp_annexure_references(s, num_annexures) for s in possible_next_steps
            ]
            suggestion_chips = [
                _clamp_annexure_references(s, num_annexures)
                for s in (suggestion_chips or [])
            ]

            # Deterministic year backstop: after all generation/recovery/verifier
            # merges, snap any FULL date whose year is a hallucinated legacy year
            # (e.g. 2024 on a year-less transcript) to the current year. Only full
            # dates are touched — statute years ("IT Act, 2000", "BNS, 2023") and
            # amounts are preserved; an explicit source year is kept. Mirrors the
            # list_of_dates guard so all panels agree on year resolution.
            case_summary = snap_years_in_text(
                case_summary, today_iso=today_str, document_text=combined_document_content
            )
            report_case_summary = snap_years_in_text(
                report_case_summary, today_iso=today_str, document_text=combined_document_content
            )
            strategic_objective = snap_years_in_text(
                strategic_objective, today_iso=today_str, document_text=combined_document_content
            )
            possible_next_steps = [
                snap_years_in_text(s, today_iso=today_str, document_text=combined_document_content)
                for s in possible_next_steps
            ]

            # Format the response (use doc_count from vector store when batch_result not provided, e.g. after delete)
            strategies_payload = {
                "workspaceId": workspace_id,
                "userId": user_id,
                "role": workspace_role,
                "isUpdate": is_update,
                "caseSummary": case_summary,
                "reportCaseSummary": report_case_summary,
                "strategicObjective": strategic_objective,
                "possibleNextSteps": possible_next_steps if possible_next_steps else [],
                "suggestionChips": suggestion_chips if suggestion_chips else [],
                "reportMobileNumber": report_mobile,
                "reportContactedDate": report_contact_date,
                "analysis": {
                    "documentCount": doc_count,
                    "averageReadability": (batch_result or {}).get(
                        "average_readability_score", 0
                    ),
                    "totalDocuments": (batch_result or {}).get(
                        "total_files", doc_count
                    ),
                    "successfulDocuments": (batch_result or {}).get(
                        "successful", doc_count
                    ),
                    "basedOnAIAnalysis": True,
                },
                "timestamp": datetime.utcnow().isoformat(),
            }

            logger.success(
                f"✅ Generated detailed strategy analysis for workspace {workspace_id}"
            )
            if report_mobile or report_contact_date:
                logger.info(
                    "Overview conversation report fields: mobile={}, contactedDate={}",
                    report_mobile,
                    report_contact_date,
                )

            # Strip the application's own brand/platform name from the strategy
            # panel (caseSummary / reportCaseSummary / strategicObjective /
            # possibleNextSteps) before it is cached or emitted.
            strategies_payload = scrub_app_brand(strategies_payload)
            return strategies_payload

        except Exception as e:
            logger.error(f"Error generating strategies: {e}")
            logger.exception(e)
            return None

    def _looks_like_question(self, text: str) -> bool:
        """
        Deterministic guard: True when the message is an informational query
        (a question / request to explain / analyse / list / suggest), so it must
        NEVER trigger a strategic-objective update or regeneration.

        Used as a hard pre-check in front of the LLM objective-change classifier
        and the STRATEGY_CHANGE intent path — LLM classifiers occasionally label
        a plain question as an objective change, and that must not auto-rewrite
        the objective.
        """
        tl = (text or "").strip().lower()
        if not tl:
            return False
        if tl.rstrip().endswith("?"):
            return True
        q_starts = (
            "what ", "what's", "whats ", "how ", "how's", "why ", "when ",
            "where ", "which ", "who ", "whom ", "whose ", "explain",
            "describe", "define ", "list ", "summarize", "summarise",
            "outline ", "tell me", "can you", "could you", "would you",
            "will you", "do you", "does ", "did ", "is ", "are ", "should i",
            "should we", "is there", "are there", "give me", "show me",
            "walk me", "clarify", "elaborate", "analyze", "analyse",
            "compare ", "suggest", "recommend", "help me understand",
            "i want to understand", "i want to know",
        )
        return any(tl.startswith(p) for p in q_starts)

    def _mentions_objective_update_intent(self, text: str) -> bool:
        """
        True when the message explicitly couples an edit/change verb with the
        objective/strategy target (e.g. "update the objective to ...", "can you
        change the strategy to ..."). This holds even when the request is phrased
        as a question, which is exactly why the informational-question fast-path
        must NOT suppress it — the LLM classifier then confirms the details.
        """
        tl = (text or "").strip().lower()
        if not tl:
            return False
        verbs = (
            "update", "change", "revise", "modify", "replace", "regenerate",
            "refresh", "set ", "rewrite", "adjust", "redefine", "reframe",
            "make the", "make it",
        )
        targets = ("objective", "strategy", "strategic")
        return any(v in tl for v in verbs) and any(t in tl for t in targets)

    def _is_pure_informational_query(self, text: str) -> bool:
        """
        Deterministic fast-path used to short-circuit obvious informational
        queries BEFORE the LLM objective-change classifier and to gate the
        objective-regeneration paths.

        Returns True ONLY when the message looks like a question AND carries no
        explicit objective/strategy update intent — i.e. a genuine "explain /
        what / how" query. Anything that pairs an edit verb with the objective
        (even if phrased as a question) returns False so the LLM can decide.
        """
        return self._looks_like_question(text) and not self._mentions_objective_update_intent(
            text
        )

    def _extract_direct_objective_update_instruction(self, text: str) -> Optional[str]:
        """
        Return instruction only when user explicitly asks to update/change objective.
        This powers direct auto-update (no confirmation question).
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 2000:
            return None
        tl = raw.lower()
        q_prefixes = (
            "what ",
            "how ",
            "why ",
            "when ",
            "where ",
            "explain ",
            "can you ",
            "could you ",
            "should i ",
            "is there ",
            "are there ",
            "define ",
            "list ",
            "summarize ",
            "summarise ",
            "tell me ",
            "describe ",
            "outline ",
        )
        if any(tl.startswith(p) for p in q_prefixes):
            return None
        if tl.rstrip().endswith("?") and len(raw) < 800:
            return None
        explicit_pairs = (
            ("update", "objective"),
            ("change", "objective"),
            ("revise", "objective"),
            ("modify", "objective"),
            ("replace", "objective"),
            ("regenerate", "objective"),
            ("refresh", "objective"),
            ("update", "strategy"),
            ("change", "strategy"),
            ("regenerate", "strategy"),
        )
        explicit_hit = any(a in tl and b in tl for (a, b) in explicit_pairs)
        if not explicit_hit:
            # Also treat declarative objective definitions as direct objective updates,
            # e.g. "client objective is to file in Patna only".
            declarative_objective = (
                ("objective" in tl)
                and (
                    "objective is" in tl
                    or "objective is to" in tl
                    or "client objective" in tl
                    or "main objective" in tl
                )
                and (
                    "want to file" in tl
                    or "file the case" in tl
                    or "only in" in tl
                    or "jurisdiction" in tl
                    or "priority is" in tl
                )
                and not tl.endswith("?")
            )
            # Also treat clear client-goal declarations as direct updates even if the
            # user does not literally type the word "objective".
            declarative_goal = (
                not tl.endswith("?")
                and (
                    "client wants to" in tl
                    or "client want to" in tl
                    or "our client wants to" in tl
                    or "we want to file" in tl
                    or "i want to file" in tl
                    or "wants to file a case in" in tl
                    or "wants to file the case in" in tl
                    or "file a case in" in tl
                    or "file the case in" in tl
                    or "only in delhi" in tl
                    or "only in patna" in tl
                )
            )
            if not declarative_objective and not declarative_goal:
                return None
        return raw

    def _extract_implicit_objective_shift_instruction(self, text: str) -> Optional[str]:
        """
        Detect likely strategic-shift intent in informal language.
        Used to append confirmation question (not direct auto-update).
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 2000:
            return None
        tl = raw.lower()
        if is_user_affirmation_to_continue(raw):
            return None
        if tl.rstrip().endswith("?") and len(raw) < 900:
            return None
        signals = (
            "want to file",
            "file the case",
            "jurisdiction",
            "strategic priority",
            "client want",
            "we want",
            "we need to",
            "priority is",
            "should file",
            "must file",
            "only in",
            "only at",
            "approach",
            "focus on",
            "main objective",
            "mitigate",
            "strategy",
            "prioritize",
            "ensure",
            "target",
            "to mitigate",
        )
        heuristic_declarative_strategy = (
            len(raw) >= 35
            and not tl.endswith("?")
            and (
                tl.startswith("to ")
                or " strategy " in f" {tl} "
                or " objective " in f" {tl} "
                or " mitigate " in f" {tl} "
            )
        )
        if not any(s in tl for s in signals) and not heuristic_declarative_strategy:
            return None
        if self._extract_direct_objective_update_instruction(raw):
            return None
        return raw

    def _resolve_confirmed_role_party(
        self,
        workspace_id: str,
        role: str,
        *,
        explicit: Optional[str] = None,
    ) -> Optional[str]:
        """Return the user-confirmed named party for ``role`` in this workspace.

        Prefers an explicit value; otherwise reads the ``confirmed_role_party``
        mapping persisted in the workspace context metadata (set on a
        role-reassignment). Only returns a party when its stored role matches the
        role we are generating for, so a stale mapping for a different role never
        leaks in.
        """
        if explicit and str(explicit).strip():
            return str(explicit).strip()
        try:
            from app.workspace.context import context_manager

            ctx = context_manager.get_context(workspace_id)
            crp = (getattr(ctx, "metadata", None) or {}).get("confirmed_role_party")
            if isinstance(crp, dict):
                stored_role = str(crp.get("role") or "").strip().upper()
                stored_party = str(crp.get("party") or "").strip()
                if stored_role == (role or "").strip().upper() and stored_party:
                    return stored_party
        except Exception:
            pass
        return None

    @meter_usage
    async def _regenerate_strategy_objective_preserving_case_summary(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_objective_instruction: str,
        current_strategy: Dict[str, Any],
        confirmed_party: Optional[str] = None,
    ) -> bool:
        """
        Regenerate strategicObjective, possibleNextSteps, and suggestionChips from the user's new
        objective instruction. Keeps caseSummary and reportCaseSummary unchanged; emits strategy_objective.

        ``confirmed_party`` is the user-confirmed named party who holds the workspace role
        (from a role-reassignment). When set, it is injected as an authoritative anchor so the
        regenerated objective advocates for that specific party. Falls back to the party persisted
        in the workspace context metadata when not explicitly passed.
        """
        try:
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            case_summary = (current_strategy.get("caseSummary") or "").strip()
            report_case_summary = (current_strategy.get("reportCaseSummary") or "").strip()
            if not case_summary:
                return False

            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
            except Exception as e:
                logger.warning(
                    "Objective regen: could not load documents: {}", e
                )
            parts = []
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
            if not all_docs:
                combined_document_content = "No document content available."
            else:
                for i, doc in enumerate(all_docs, 1):
                    content = (doc.get("content") or "").strip()
                    if not content or len(content) < 20:
                        continue
                    if len(content) > per_doc_cap:
                        content = (
                            content[:per_doc_cap] + "\n[... truncated for length ...]"
                        )
                    # Build labelled block with metadata header so the
                    # prompt distinguishes drafted-by-assistant documents
                    # from raw evidence.
                    doc_with_clip = dict(doc)
                    doc_with_clip["content"] = content
                    parts.append(
                        _format_document_for_prompt(
                            doc_with_clip, i, len(all_docs),
                            label_kind="Document",
                        )
                    )
                combined_document_content = (
                    "\n\n".join(parts) if parts else "No document content available."
                )
            if len(combined_document_content) > max_total_content:
                combined_document_content = (
                    combined_document_content[:max_total_content]
                    + "\n[... content truncated ...]"
                )

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"
            confirmed_party = self._resolve_confirmed_role_party(
                workspace_id, role, explicit=confirmed_party
            )

            # Extracted-entities allow-list (cached; cheap) — the only bodies/
            # parties/places the regenerated objective may name. Grounds the
            # prompt AND seeds the deterministic fabrication backstop below.
            try:
                case_entities = await self._extract_case_entities(
                    workspace_id=workspace_id,
                    combined_document_content=combined_document_content,
                )
            except Exception as ent_err:
                logger.debug("Objective regen: entity extraction failed: {}", ent_err)
                case_entities = {}
            try:
                entities_block = json.dumps(
                    case_entities or {}, indent=2, ensure_ascii=False
                )
            except Exception:
                entities_block = "{}"

            prompt = regenerate_strategy_objective_preserving_case_summary_prompt(SPEAKER_LABEL_GUARDRAIL=SPEAKER_LABEL_GUARDRAIL, case_summary=case_summary, combined_document_content=combined_document_content, current_strategy=current_strategy, report_case_summary=report_case_summary, role=role, user_objective_instruction=user_objective_instruction, entities_block=entities_block, confirmed_party=confirmed_party)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.25,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False

            strategic_objective = (data.get("strategicObjective") or "").strip()
            possible_next_steps = (
                data.get("possibleNextSteps") or data.get("possible_next_steps") or []
            )
            if isinstance(possible_next_steps, str):
                possible_next_steps = [possible_next_steps] if possible_next_steps else []
            possible_next_steps = [
                _annexure_wording_for_next_steps(str(s).strip())
                for s in possible_next_steps
                if s
            ][:5]

            # Deterministic anti-fabrication backstop: the objective must not
            # name any organisation/body/place absent from the documents,
            # extracted entities, or the user's own instruction (a forum/venue
            # the user chose is allowed). If a fabricated entity slipped through
            # (e.g. "Bihar Cricket Association" for a Karnataka/national case),
            # run ONE corrective re-prompt to re-ground the text.
            try:
                allowed_texts = [
                    combined_document_content,
                    user_objective_instruction,
                    entities_block,
                    case_summary,
                    report_case_summary,
                ]
                ungrounded = find_ungrounded_proper_nouns(
                    strategic_objective + "\n" + "\n".join(possible_next_steps),
                    allowed_texts,
                )
                if ungrounded:
                    logger.warning(
                        "Objective regen: ungrounded entities detected {} for "
                        "workspace {} — running one corrective re-prompt",
                        ungrounded,
                        workspace_id[:12] if workspace_id else "",
                    )
                    reground_prompt = reground_strategy_objective_prompt(
                        strategic_objective=strategic_objective,
                        possible_next_steps=possible_next_steps,
                        ungrounded_terms=ungrounded,
                        combined_document_content=combined_document_content,
                        user_objective_instruction=user_objective_instruction,
                        entities_block=entities_block,
                        role=role,
                    )
                    raw2 = await gemini_client.generate(
                        prompt=reground_prompt,
                        system_instruction=get_structured_document_feature_system_instruction(
                            role
                        ),
                        temperature=0.0,
                        max_tokens=8000,
                        response_mime_type="application/json",
                    )
                    json2 = self._prepare_json_candidate(
                        (raw2 or "").strip(), prefer="object"
                    )
                    data2 = json.loads(json2) if json2 else {}
                    if isinstance(data2, dict):
                        so2 = (data2.get("strategicObjective") or "").strip()
                        ns2 = (
                            data2.get("possibleNextSteps")
                            or data2.get("possible_next_steps")
                            or []
                        )
                        if isinstance(ns2, str):
                            ns2 = [ns2] if ns2 else []
                        if so2:
                            strategic_objective = so2
                        if ns2:
                            possible_next_steps = [
                                _annexure_wording_for_next_steps(str(s).strip())
                                for s in ns2
                                if s
                            ][:5]
                        still_ungrounded = find_ungrounded_proper_nouns(
                            strategic_objective + "\n" + "\n".join(possible_next_steps),
                            allowed_texts,
                        )
                        if still_ungrounded:
                            logger.warning(
                                "Objective regen: entities still ungrounded after "
                                "re-prompt {} (workspace {})",
                                still_ungrounded,
                                workspace_id[:12] if workspace_id else "",
                            )
            except Exception as reground_err:
                logger.debug(
                    "Objective regen fabrication backstop failed: {}; keeping "
                    "original objective",
                    reground_err,
                )

            suggestion_chips = (
                data.get("suggestionChips") or data.get("suggestion_chips") or []
            )
            if isinstance(suggestion_chips, str):
                suggestion_chips = [suggestion_chips] if suggestion_chips else []
            suggestion_chips = [str(s).strip() for s in suggestion_chips if s][:16]
            if len(suggestion_chips) < 10:
                prior = current_strategy.get("suggestionChips") or []
                if isinstance(prior, list):
                    for p in prior:
                        t = str(p).strip()
                        if t and t.lower() not in {x.lower() for x in suggestion_chips}:
                            suggestion_chips.append(t)
                        if len(suggestion_chips) >= 16:
                            break
            if len(suggestion_chips) < 10:
                for pad in self._fallback_chips_for_role(role, 16):
                    if pad and pad.lower() not in {x.lower() for x in suggestion_chips}:
                        suggestion_chips.append(pad)
                    if len(suggestion_chips) >= 16:
                        break

            if not strategic_objective:
                return False
            if len(possible_next_steps) < 2:
                possible_next_steps = (
                    possible_next_steps
                    + [
                        "Map jurisdiction and venue requirements against the instruction",
                        "Preserve and index key annexures referenced in strategy",
                    ]
                )[:5]

            merged = dict(current_strategy)
            merged["caseSummary"] = current_strategy.get("caseSummary", case_summary)
            merged["reportCaseSummary"] = current_strategy.get(
                "reportCaseSummary", report_case_summary
            )
            merged["strategicObjective"] = strategic_objective
            merged["possibleNextSteps"] = possible_next_steps
            merged["suggestionChips"] = suggestion_chips
            merged["isUpdate"] = True
            merged["role"] = role
            merged["workspaceId"] = workspace_id
            merged["userId"] = user_id
            merged["timestamp"] = datetime.utcnow().isoformat()

            merged = scrub_app_brand(merged)
            self._workspace_strategy_payload[workspace_id] = merged
            self._workspace_suggestion_chips[workspace_id] = list(suggestion_chips)
            self._last_workspace_id_with_chips = workspace_id

            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=merged,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ Strategy objective regen emitted (case summary preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Strategy objective regen failed: {}", e)
            return False

    @meter_usage
    async def _regenerate_case_summary_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        current_strategy: Dict[str, Any],
        emit_result: bool = True,
    ) -> bool:
        """
        Regenerate caseSummary + reportCaseSummary applying the user's edit
        instruction. Preserves strategicObjective + possibleNextSteps +
        suggestionChips byte-for-byte. Updates cache and emits
        document_feature_response (featureType=strategy_objective).
        """
        try:
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            current_case_summary = (current_strategy.get("caseSummary") or "").strip()
            current_report_case_summary = (
                current_strategy.get("reportCaseSummary") or ""
            ).strip()
            if not current_case_summary:
                return False

            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
            except Exception as e:
                logger.warning("Case-summary regen: could not load documents: {}", e)
            parts: List[str] = []
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
            if not all_docs:
                combined_document_content = "No document content available."
            else:
                for i, doc in enumerate(all_docs, 1):
                    content = (doc.get("content") or "").strip()
                    if not content or len(content) < 20:
                        continue
                    if len(content) > per_doc_cap:
                        content = content[:per_doc_cap] + "\n[... truncated for length ...]"
                    doc_with_clip = dict(doc)
                    doc_with_clip["content"] = content
                    parts.append(
                        _format_document_for_prompt(
                            doc_with_clip, i, len(all_docs)
                        )
                    )
                combined_document_content = (
                    "\n\n".join(parts) if parts else "No document content available."
                )
            if len(combined_document_content) > max_total_content:
                combined_document_content = (
                    combined_document_content[:max_total_content]
                    + "\n[... content truncated ...]"
                )

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            prompt = regenerate_case_summary_with_feedback_prompt(SPEAKER_LABEL_GUARDRAIL=SPEAKER_LABEL_GUARDRAIL, combined_document_content=combined_document_content, current_case_summary=current_case_summary, current_report_case_summary=current_report_case_summary, current_strategy=current_strategy, role=role, user_feedback=user_feedback)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.2,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False

            new_case_summary = _strip_ui_truncated_tokens(
                (data.get("caseSummary") or "").strip()
            )
            new_report_case_summary = _strip_ui_truncated_tokens(
                (data.get("reportCaseSummary") or "").strip()
            )
            if not new_case_summary:
                return False
            # If the model omitted reportCaseSummary, mirror caseSummary as a fallback.
            if not new_report_case_summary:
                new_report_case_summary = new_case_summary

            merged = dict(current_strategy)
            merged["caseSummary"] = new_case_summary
            merged["reportCaseSummary"] = new_report_case_summary
            # Explicitly lock the other three fields from the prior payload.
            merged["strategicObjective"] = current_strategy.get("strategicObjective", "")
            merged["possibleNextSteps"] = list(
                current_strategy.get("possibleNextSteps") or []
            )
            merged["suggestionChips"] = list(
                current_strategy.get("suggestionChips") or []
            )
            merged["isUpdate"] = True
            merged["role"] = role
            merged["workspaceId"] = workspace_id
            merged["userId"] = user_id
            merged["timestamp"] = datetime.utcnow().isoformat()

            merged = scrub_app_brand(merged)
            self._workspace_strategy_payload[workspace_id] = merged

            if emit_result and self._socket_client:
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=merged,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ Case summary regen emitted (other fields preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Case summary regen failed: {}", e)
            return False

    @meter_usage
    async def _regenerate_next_steps_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        current_strategy: Dict[str, Any],
        emit_result: bool = True,
    ) -> bool:
        """
        Regenerate possibleNextSteps + suggestionChips applying the user's edit
        instruction. Preserves caseSummary + reportCaseSummary + strategicObjective
        byte-for-byte. Updates cache and emits document_feature_response
        (featureType=strategy_objective).
        """
        try:
            from app.ai.gemini_client import gemini_client

            current_case_summary = (current_strategy.get("caseSummary") or "").strip()
            current_strategic_objective = (
                current_strategy.get("strategicObjective") or ""
            ).strip()
            if not current_case_summary:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            prompt = regenerate_next_steps_with_feedback_prompt(current_case_summary=current_case_summary, current_strategic_objective=current_strategic_objective, current_strategy=current_strategy, role=role, user_feedback=user_feedback)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.25,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False

            possible_next_steps = (
                data.get("possibleNextSteps") or data.get("possible_next_steps") or []
            )
            if isinstance(possible_next_steps, str):
                possible_next_steps = [possible_next_steps] if possible_next_steps else []
            possible_next_steps = [
                _annexure_wording_for_next_steps(_strip_ui_truncated_tokens(str(s).strip()))
                for s in possible_next_steps
                if s
            ][:5]
            if len(possible_next_steps) < 2:
                # Refuse the change rather than emit a degraded list — keep the user's
                # prior content rather than half-empty next steps.
                return False

            suggestion_chips = (
                data.get("suggestionChips") or data.get("suggestion_chips") or []
            )
            if isinstance(suggestion_chips, str):
                suggestion_chips = [suggestion_chips] if suggestion_chips else []
            suggestion_chips = [
                _strip_ui_truncated_tokens(str(s).strip())
                for s in suggestion_chips
                if s
            ][:16]
            if len(suggestion_chips) < 10:
                prior = current_strategy.get("suggestionChips") or []
                if isinstance(prior, list):
                    for p in prior:
                        t = str(p).strip()
                        if t and t.lower() not in {x.lower() for x in suggestion_chips}:
                            suggestion_chips.append(t)
                        if len(suggestion_chips) >= 16:
                            break
            if len(suggestion_chips) < 10:
                for pad in self._fallback_chips_for_role(role, 16):
                    if pad and pad.lower() not in {x.lower() for x in suggestion_chips}:
                        suggestion_chips.append(pad)
                    if len(suggestion_chips) >= 16:
                        break

            merged = dict(current_strategy)
            # Lock these three explicitly.
            merged["caseSummary"] = current_strategy.get("caseSummary", "")
            merged["reportCaseSummary"] = current_strategy.get("reportCaseSummary", "")
            merged["strategicObjective"] = current_strategy.get("strategicObjective", "")
            merged["possibleNextSteps"] = possible_next_steps
            merged["suggestionChips"] = suggestion_chips
            merged["isUpdate"] = True
            merged["role"] = role
            merged["workspaceId"] = workspace_id
            merged["userId"] = user_id
            merged["timestamp"] = datetime.utcnow().isoformat()

            merged = scrub_app_brand(merged)
            self._workspace_strategy_payload[workspace_id] = merged
            self._workspace_suggestion_chips[workspace_id] = list(suggestion_chips)
            self._last_workspace_id_with_chips = workspace_id

            if emit_result and self._socket_client:
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=merged,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ Possible next steps regen emitted (other fields preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Possible next steps regen failed: {}", e)
            return False

    @meter_usage
    async def _regenerate_strategic_objective_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        current_strategy: Dict[str, Any],
        emit_result: bool = True,
    ) -> bool:
        """
        Regenerate strategicObjective applying the user's surgical edit
        instruction (e.g. "remove this exact line", "rephrase the X sentence").
        Preserves caseSummary + reportCaseSummary + possibleNextSteps +
        suggestionChips byte-for-byte. Updates cache and emits
        document_feature_response with featureType=strategy_objective.

        Distinct from _regenerate_strategy_objective_preserving_case_summary,
        which is used by the legacy STRATEGY_OBJECTIVE chat_type flow when
        the user DECLARES a brand-new objective (and therefore needs the
        next steps + chips to refresh). This surgical variant assumes the
        user wants the existing content reproduced verbatim except for the
        specific change they named.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current_strategic_objective = (
                current_strategy.get("strategicObjective") or ""
            ).strip()
            current_case_summary = (current_strategy.get("caseSummary") or "").strip()
            if not current_strategic_objective:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            prompt = regenerate_strategic_objective_with_feedback_prompt(SPEAKER_LABEL_GUARDRAIL=SPEAKER_LABEL_GUARDRAIL, current_case_summary=current_case_summary, current_strategic_objective=current_strategic_objective, current_strategy=current_strategy, role=role, user_feedback=user_feedback)

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.2,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False

            new_strategic_objective = _strip_ui_truncated_tokens(
                (data.get("strategicObjective") or "").strip()
            )
            if not new_strategic_objective:
                return False

            merged = dict(current_strategy)
            # Lock the other four fields explicitly so any future refactor
            # of dict(current_strategy) cannot accidentally let them drift.
            merged["caseSummary"] = current_strategy.get("caseSummary", "")
            merged["reportCaseSummary"] = current_strategy.get("reportCaseSummary", "")
            merged["strategicObjective"] = new_strategic_objective
            merged["possibleNextSteps"] = list(
                current_strategy.get("possibleNextSteps") or []
            )
            merged["suggestionChips"] = list(
                current_strategy.get("suggestionChips") or []
            )
            merged["isUpdate"] = True
            merged["role"] = role
            merged["workspaceId"] = workspace_id
            merged["userId"] = user_id
            merged["timestamp"] = datetime.utcnow().isoformat()

            merged = scrub_app_brand(merged)
            self._workspace_strategy_payload[workspace_id] = merged

            if emit_result and self._socket_client:
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=merged,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ Strategic objective surgical edit emitted (other fields preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Strategic objective surgical edit failed: {}", e)
            return False

    def _last_ai_offered_objective_update(self, ai_text: str) -> bool:
        """True if the assistant previously appended the objective-update offer question."""
        t = (ai_text or "").lower()
        return (
            "update the strategic objective" in t
            or "update the whole workspace based on this new objective" in t
        )

    def _format_thread_for_objective_extraction(
        self,
        chat_history: List[Dict[str, Any]],
        current_user_message: str,
        *,
        max_messages: int = 14,
    ) -> str:
        """Build a compact thread for summarizing the desired new objective after 'yes'."""
        lines: List[str] = []
        hist = chat_history or []
        tail = hist[-max_messages:] if len(hist) > max_messages else hist
        for m in tail:
            role = (m.get("role") or "").upper()
            c = (m.get("content") or "").strip()
            if not c:
                continue
            prefix = "User" if role == "USER" else "Assistant"
            lines.append(f"{prefix}: {c}")
        lines.append(f"User (confirmation): {(current_user_message or '').strip()}")
        return "\n".join(lines)

    async def _summarize_objective_instruction_from_thread(
        self,
        thread: str,
        workspace_role: str,
    ) -> Optional[str]:
        """Extract the new strategic priority from an informal discussion thread."""
        try:
            from app.ai.gemini_client import gemini_client

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            prompt = summarize_objective_instruction_from_thread_prompt(role=role, thread=thread)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.1,
                max_tokens=2000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return None
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return None
            txt = (data.get("objectiveText") or data.get("objective_text") or "").strip()
            return txt or None
        except Exception as e:
            logger.warning("Objective instruction summarization failed: {}", e)
            return None

    @staticmethod
    def _extract_declarative_objective_text(user_message: str) -> str:
        """Detect declarative "objective is …" / "client objective is …" / "objective: …"
        patterns and return the extracted objective text.

        Returns "" when:
        - the message is a question (ends with "?" or starts with question word),
        - the pattern is an explanatory framing like "the objective of <concept> is …",
        - no recognized declarative pattern matches,
        - the extracted text is too short / too generic to be a real objective.

        This is a deterministic safety net for the LLM classifier — when it fires,
        it forces mode="direct_update" with this text as objectiveText.
        """
        msg = (user_message or "").strip()
        if not msg or len(msg) < 12:
            return ""
        # Strip trailing/leading quotes/punctuation noise that confuses regexes
        stripped = msg.strip().strip('"\u201c\u201d\u2018\u2019')
        if not stripped:
            return ""
        # Skip obvious questions.
        if stripped.endswith("?"):
            return ""
        first_word = re.match(r"\s*(\w+)", stripped, flags=re.UNICODE)
        if first_word:
            fw = first_word.group(1).lower()
            if fw in {
                "what",
                "why",
                "how",
                "which",
                "when",
                "where",
                "who",
                "should",
                "can",
                "could",
                "would",
                "is",
                "are",
                "do",
                "does",
                "did",
            }:
                return ""

        # Reject explanatory framing where "objective" is being defined, not set:
        #   "the objective of a writ petition is to ..."
        #   "an objective in such cases is usually ..."
        if re.search(
            r"\b(?:the|an|a)\s+(?:strategic\s+|primary\s+|main\s+)?objective\s+(?:of|in)\s+\w+",
            stripped,
            flags=re.IGNORECASE,
        ):
            return ""

        # Patterns we treat as declarative objective-setters.
        # Each pattern captures the objective text in group 1.
        patterns = [
            # "objective: <X>"  /  "the objective: <X>"
            r"(?:^|[\.\!\n;])\s*(?:the\s+|our\s+|my\s+|case\s+|client(?:'s|s)?\s+)?objective\s*[:=]\s*(.+?)(?:[\.\!\n;]|$)",
            # "set (the) objective to <X>"  /  "set objective: <X>" already covered above
            r"\bset\s+(?:the\s+)?objective\s+to\s+(.+?)(?:[\.\!\n;]|$)",
            # "(client(?:'s)?|our|my|the|case) objective (is|should be) <X>"
            r"(?:^|[\.\!\n;]|\s)(?:the|our|my|case|client(?:'s|s)?)\s+objective\s+(?:is|should\s+be|will\s+be|must\s+be)\s+(.+?)(?:[\.\!\n;]|$)",
            # bare "objective is <X>" / "objective should be <X>" at sentence start
            r"(?:^|[\.\!\n;])\s*objective\s+(?:is|should\s+be|will\s+be|must\s+be)\s+(.+?)(?:[\.\!\n;]|$)",
        ]

        for pat in patterns:
            m = re.search(pat, stripped, flags=re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            captured = (m.group(1) or "").strip()
            if not captured:
                continue
            # Strip leading filler tokens that are not part of the objective itself.
            captured = re.sub(
                r"^(?:to\s+|that\s+|going\s+to\s+|gonna\s+)",
                "",
                captured,
                flags=re.IGNORECASE,
            ).strip()
            # Reject vague / placeholder content that wouldn't be a real objective.
            if len(captured) < 6:
                continue
            low = captured.lower()
            vague = {
                "tbd",
                "to be decided",
                "unclear",
                "unknown",
                "not sure",
                "discussed",
            }
            if low in vague:
                continue
            # Cap length so we don't accidentally swallow a paragraph.
            if len(captured) > 400:
                captured = captured[:400].rstrip() + "…"
            # Capitalize first letter for nicer display, leave rest intact.
            return captured[:1].upper() + captured[1:]

        return ""

    async def _should_offer_objective_update_after_discussion(
        self,
        user_message: str,
        assistant_reply: str,
    ) -> bool:
        """
        True when the user is exploring a new strategic direction in informal language
        without explicitly asking to update the strategy panel — offer to update at end of reply.
        """
        um = (user_message or "").strip()
        ar = (assistant_reply or "").strip()
        if len(um) < 12:
            return False
        if is_user_affirmation_to_continue(um):
            return False
        if OBJECTIVE_UPDATE_OFFER_QUESTION.lower() in ar.lower():
            return False
        try:
            from app.ai.gemini_client import gemini_client

            prompt = should_offer_objective_update_after_discussion_prompt(ar=ar, um=um)
            # max_tokens is generous because gemini-3-flash-preview counts
            # internal "thinking" tokens against the same budget; 120 was too
            # small and the response was returning with finish_reason=MAX_TOKENS
            # (no Part), which produced a non-JSON error string for callers.
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=512,
                response_mime_type="application/json",
            )
            raw_text = (raw or "").strip()
            # gemini_client now returns "" when the candidate had no Part and
            # the legacy code returned an "Error generating response: …" string.
            # Treat both as "no offer" without spamming WARN logs.
            if not raw_text or raw_text.lower().startswith("error generating"):
                logger.debug(
                    "Objective-offer classifier got empty/error response; treating as no-offer"
                )
                return False
            json_str = self._prepare_json_candidate(raw_text, prefer="object")
            if not json_str:
                return False
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False
            return bool(data.get("offer") is True)
        except Exception as e:
            logger.warning("Objective-offer classifier failed: {}", e)
            return False

    async def _llm_classify_objective_change_intent(
        self,
        user_message: str,
        *,
        chat_type_upper: str,
    ) -> Dict[str, Any]:
        """
        LLM-only classifier for NL objective-change detection.
        Returns:
          {"mode": "direct_update" | "ask_confirmation" | "none", "objectiveText": "...", "isQuestionLike": bool, "updateRequestedNow": bool}
        """
        msg = (user_message or "").strip()
        if not msg or chat_type_upper != "STRATEGY_OBJECTIVE":
            return {"mode": "none", "objectiveText": "", "isQuestionLike": False}
        # Fast-path ONLY for pure informational queries — a question that carries
        # no objective/strategy update intent ("explain the applicable case laws",
        # "what is the strongest argument?"). These must never become an objective
        # update, so we short-circuit before the model call. A question that DOES
        # pair an edit verb with the objective ("can you update the objective to
        # focus on bail?") is intentionally NOT short-circuited — it falls through
        # to the LLM so the nuanced question-vs-update call is made properly.
        if self._is_pure_informational_query(msg):
            logger.info(
                "Objective intent: pure informational query — treating as question "
                "(no objective update). msg_len={}",
                len(msg),
            )
            return {
                "mode": "none",
                "objectiveText": "",
                "isQuestionLike": True,
                "updateRequestedNow": False,
            }
        try:
            from app.ai.gemini_client import gemini_client

            prompt = llm_classify_objective_change_intent_prompt(msg=msg)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=500,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not candidate:
                return {"mode": "none", "objectiveText": "", "isQuestionLike": False}
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                return {"mode": "none", "objectiveText": "", "isQuestionLike": False}
            mode = str(parsed.get("mode") or "").strip().lower()
            if mode not in {"direct_update", "ask_confirmation", "none"}:
                mode = "none"
            obj_text = str(parsed.get("objectiveText") or "").strip()
            is_question_like = bool(parsed.get("isQuestionLike") is True)
            update_requested_now = bool(parsed.get("updateRequestedNow") is True)
            if is_question_like and not update_requested_now:
                mode = "none"
            # LLM-output-only promotion: if classifier extracted a clear objective text,
            # says it's not a question, and did not mark immediate update explicitly,
            # treat as direct update for strategy/objective parity.
            if (
                mode in {"none", "ask_confirmation"}
                and obj_text
                and len(obj_text) >= 20
                and not is_question_like
            ):
                mode = "direct_update"
                update_requested_now = True
            # Guardrail: if model marks ask_confirmation but the user message is declarative
            # and objective text is clear, treat as direct update (strategy/objective parity).
            if (
                mode == "ask_confirmation"
                and obj_text
                and len(obj_text) >= 20
                and not is_question_like
                and update_requested_now
            ):
                mode = "direct_update"
            # Deterministic safety net: lawyers very often state the objective
            # using literal patterns like "objective is X", "client objective is X",
            # "the objective should be X", "objective: X". Some model variants still
            # treat these as informational/explanatory and emit mode="none" with
            # empty objectiveText. Detect those patterns from the raw message and
            # force a direct_update so the strategy panel actually moves.
            if mode == "none" and not is_question_like:
                fallback_obj = self._extract_declarative_objective_text(msg)
                if fallback_obj:
                    logger.info(
                        "Objective intent classifier overridden by declarative pattern: '{}…'",
                        fallback_obj[:60],
                    )
                    mode = "direct_update"
                    update_requested_now = True
                    obj_text = fallback_obj
            if mode == "direct_update" and not update_requested_now:
                mode = "none"
            if mode == "none":
                obj_text = ""
            return {
                "mode": mode,
                "objectiveText": obj_text,
                "isQuestionLike": is_question_like,
                "updateRequestedNow": update_requested_now,
            }
        except Exception as e:
            logger.warning("Objective intent classifier failed: {}", e)
            return {
                "mode": "none",
                "objectiveText": "",
                "isQuestionLike": False,
                "updateRequestedNow": False,
            }

    async def _llm_classify_panel_edit_intent(
        self,
        user_message: str,
        *,
        current_strategy: Optional[Dict[str, Any]] = None,
        default_panel: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        LLM-based classifier that decides whether the user's chat message is a
        direct edit request on one of the four strategy panel fields:
        caseSummary, reportCaseSummary, strategicObjective, possibleNextSteps.

        Returns:
            {
              "caseSummary":         {"shouldEdit": bool, "instruction": str},
              "reportCaseSummary":   {"shouldEdit": bool, "instruction": str},
              "strategicObjective":  {"shouldEdit": bool, "instruction": str},
              "possibleNextSteps":   {"shouldEdit": bool, "instruction": str},
              "isQuestionLike":      bool
            }

        ``shouldEdit`` is True only when the user is directly asking for a
        change (remove / drop / delete / change / replace / edit / modify /
        rewrite / add / append / insert / expand / shorten / rephrase /
        fix / correct). Questions, explanations, and greetings classify as
        all-False so the message falls through to the intelligence chat
        agent unchanged. ``instruction`` is the normalised edit text (the
        whole user message when no narrower extraction is possible).
        """
        empty: Dict[str, Any] = {
            "caseSummary": {"shouldEdit": False, "instruction": ""},
            "reportCaseSummary": {"shouldEdit": False, "instruction": ""},
            "strategicObjective": {"shouldEdit": False, "instruction": ""},
            "possibleNextSteps": {"shouldEdit": False, "instruction": ""},
            "analysisReport": {"shouldEdit": False, "instruction": ""},
            "listOfDates": {"shouldEdit": False, "instruction": ""},
            "isRename": False,
            "isRoleReassignment": False,
            "reassignRole": "",
            "reassignParty": "",
            "isQuestionLike": False,
        }
        msg = (user_message or "").strip()
        if not msg:
            return empty
        try:
            from app.ai.gemini_client import gemini_client

            has_strategy = bool(
                isinstance(current_strategy, dict)
                and (current_strategy.get("caseSummary") or "").strip()
            )
            if not has_strategy:
                # No cached strategy means there's nothing to edit yet.
                return empty

            # When the chat is already scoped to a specific panel (e.g. the
            # Strategy & Objective panel), an edit instruction that names no
            # panel ("change the wordings", "add a few words") should default
            # to that panel rather than being dropped as ambiguous (rule 8).
            _PANEL_LABELS = {
                "strategicObjective": "STRATEGIC OBJECTIVE (strategicObjective)",
                "caseSummary": "CASE SUMMARY (caseSummary)",
                "reportCaseSummary": "REPORT CASE SUMMARY (reportCaseSummary)",
                "possibleNextSteps": "POSSIBLE NEXT STEPS (possibleNextSteps)",
                "analysisReport": "ANALYSIS REPORT (analysisReport)",
            }
            scoped_panel_rule = ""
            if default_panel in _PANEL_LABELS:
                scoped_panel_rule = (
                    llm_classify_panel_edit_intent_prompt2(_PANEL_LABELS=_PANEL_LABELS, default_panel=default_panel)
                )

            prompt = llm_classify_panel_edit_intent_prompt(msg=msg, scoped_panel_rule=scoped_panel_rule)

            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=4096,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return empty
            try:
                data = json.loads(json_str)
            except Exception:
                return empty
            if not isinstance(data, dict):
                return empty

            def _slot(key: str) -> Dict[str, Any]:
                val = data.get(key)
                if not isinstance(val, dict):
                    return {"shouldEdit": False, "instruction": ""}
                should = bool(val.get("shouldEdit") is True)
                instr = str(val.get("instruction") or "").strip()
                # Require a non-empty per-panel instruction. If the model
                # flagged shouldEdit but didn't extract clean per-panel text,
                # treat as classifier failure for THIS panel — better to
                # under-trigger than to dispatch the wrong instruction.
                if should and not instr:
                    return {"shouldEdit": False, "instruction": ""}
                if not should:
                    instr = ""
                return {"shouldEdit": should, "instruction": instr}

            result = {
                "caseSummary": _slot("caseSummary"),
                "reportCaseSummary": _slot("reportCaseSummary"),
                "strategicObjective": _slot("strategicObjective"),
                "possibleNextSteps": _slot("possibleNextSteps"),
                "analysisReport": _slot("analysisReport"),
                "listOfDates": _slot("listOfDates"),
                "isRename": bool(data.get("isRename") is True),
                "isRoleReassignment": bool(data.get("isRoleReassignment") is True),
                "reassignRole": str(data.get("reassignRole") or "").strip().upper(),
                "reassignParty": str(data.get("reassignParty") or "").strip(),
                "isQuestionLike": bool(data.get("isQuestionLike") is True),
            }
            # A role reassignment re-orients advocacy via a dedicated flow — it is
            # never a cosmetic rename and never a surgical per-panel edit. Enforce
            # that here so a stray classifier flag can't double-dispatch.
            if result["isRoleReassignment"] and result["reassignParty"]:
                result["isRename"] = False
                for k in (
                    "caseSummary",
                    "reportCaseSummary",
                    "strategicObjective",
                    "possibleNextSteps",
                    "analysisReport",
                    "listOfDates",
                ):
                    result[k] = {"shouldEdit": False, "instruction": ""}
            else:
                # Guard against a partial classification (flag set, party missing).
                result["isRoleReassignment"] = False
                result["reassignRole"] = ""
                result["reassignParty"] = ""
            # If the model marked it as a question AND also flagged an edit,
            # trust the question signal and drop the edit flags — safer
            # default for "explain X" / "what about X" style messages.
            if result["isQuestionLike"]:
                for k in (
                    "caseSummary",
                    "reportCaseSummary",
                    "strategicObjective",
                    "possibleNextSteps",
                    "analysisReport",
                    "listOfDates",
                ):
                    result[k] = {"shouldEdit": False, "instruction": ""}
            logger.info(
                "Panel-edit classifier: msg_len={} isQuestion={} isRoleReassignment={} reassign={}:{} caseSummary={} reportCaseSummary={} strategicObjective={} possibleNextSteps={} analysisReport={} listOfDates={}",
                len(msg),
                result["isQuestionLike"],
                result["isRoleReassignment"],
                result["reassignRole"] or "-",
                result["reassignParty"] or "-",
                "yes" if result["caseSummary"]["shouldEdit"] else "no",
                "yes" if result["reportCaseSummary"]["shouldEdit"] else "no",
                "yes" if result["strategicObjective"]["shouldEdit"] else "no",
                "yes" if result["possibleNextSteps"]["shouldEdit"] else "no",
                "yes" if result["analysisReport"]["shouldEdit"] else "no",
                "yes" if result["listOfDates"]["shouldEdit"] else "no",
            )
            return result
        except Exception as e:
            logger.warning("Panel-edit intent classifier failed: {}", e)
            return empty

    async def _build_panel_edit_ack(
        self,
        *,
        workspace_id: str,
        user_message: str,
        panels: List[str],
        instructions: Dict[str, str],
        is_rename: bool = False,
    ) -> str:
        """
        Compose the chat confirmation after surgical panel edits were applied.

        Returns a short natural-language message describing what changed (and,
        for possible-next-steps edits, the resulting list state), or "" so the
        caller falls back to a templated acknowledgement.
        """
        try:
            from app.ai.gemini_client import gemini_client

            edits = [
                (panel, (instructions.get(panel) or "").strip())
                for panel in panels
            ]
            edits = [(p, i) for p, i in edits if i]
            if not edits:
                return ""

            updated_next_steps: List[str] = []
            if "possible next steps" in panels:
                cur_strategy = self._workspace_strategy_payload.get(workspace_id)
                if isinstance(cur_strategy, dict):
                    steps = cur_strategy.get("possibleNextSteps") or []
                    if isinstance(steps, list):
                        updated_next_steps = [str(s).strip() for s in steps if s]

            prompt = panel_edit_ack_prompt(
                user_message=(user_message or "").strip(),
                edits=edits,
                updated_next_steps=updated_next_steps,
                is_rename=is_rename,
            )
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.2,
                max_tokens=500,
            )
            return (raw or "").strip()
        except Exception as e:
            logger.warning("Panel-edit ack generation failed (non-fatal): {}", e)
            return ""

    def _strategy_objective_fallback(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        is_update: bool = True,
    ) -> Dict[str, Any]:
        """Return a minimal strategy/objective payload when there are no docs or generation fails."""
        return {
            "workspaceId": workspace_id,
            "userId": user_id,
            "role": (workspace_role or "ADJUDICATOR").strip().upper(),
            "isUpdate": is_update,
            "isEmpty": True,
            "caseSummary": "",
            "reportCaseSummary": "",
            "strategicObjective": "",
            "possibleNextSteps": [],
            "suggestionChips": [],
            "reportMobileNumber": None,
            "reportContactedDate": None,
            "analysis": {
                "documentCount": 0,
                "averageReadability": 0,
                "totalDocuments": 0,
                "successfulDocuments": 0,
                "basedOnAIAnalysis": False,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
