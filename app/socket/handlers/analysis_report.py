"""Analysis report generation, scoring, feedback, and rejection memory.

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
    _strip_strength_point_statute_boilerplate,
    _annexure_wording_for_next_steps,
    _clamp_annexure_references,
    _format_document_for_prompt,
    _normalize_report_mobile_from_overview,
    _overview_conversation_date_to_iso,
    extract_overview_conversation_report_contact_meta,
)


from app.prompts.analysis_report_prompts import (
    regenerate_analysis_report_with_feedback_prompt,
    generate_analysis_report_from_documents_prompt,
    generate_analysis_report_from_documents_prompt2,
    ensure_structured_relevant_jurisdiction_prompt,
    ensure_quality_score_explanation_prompt,
    compute_rejection_score_delta_prompt,
    compute_added_missing_info_score_delta_prompt,
)


class AnalysisReportMixin:
    """Analysis report generation, scoring, feedback, and rejection memory."""

    @meter_usage
    async def _regenerate_analysis_report_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        current_report: Dict[str, Any],
    ) -> bool:
        """
        Apply a free-form user edit instruction (rename, rephrase, add / change /
        remove a strength / weakness / risk / missingInfo item, etc.) to the
        CACHED analysis report and re-emit it.

        Operates on the cached payload only — it must NOT call
        ``_emit_analysis_report`` / ``_generate_analysis_report_from_documents``,
        which regenerate from documents and would re-introduce whatever the user
        just asked to change (e.g. a misspelled name). Every field and list item
        the user did not name is reproduced verbatim; the JSON schema is
        preserved (strengths stay ``[{point, laws}]``, the other lists stay
        lists of strings). Updates ``_workspace_analysis_payload`` and emits
        ``document_feature_response`` with featureType=analysis_report.
        """
        try:
            from app.ai.gemini_client import gemini_client

            if not isinstance(current_report, dict) or not current_report:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            # The cached report's text sections are now {id,text,isMatched}
            # dicts. The edit prompt expects plain strings ("the other lists
            # stay lists of strings"), so normalize a copy to strings for the
            # prompt and the no-op comparison; the emitted result is re-tagged
            # below so identity survives the edit.
            current_report_norm = dict(current_report)
            for _k in ("missingInfo", "weaknesses", "risks", "relevantJurisdiction"):
                if _k in current_report_norm:
                    current_report_norm[_k] = _coerce_analysis_text_list(
                        current_report_norm.get(_k)
                    )
            current_report_json = json.dumps(current_report_norm, ensure_ascii=True)

            prompt = regenerate_analysis_report_with_feedback_prompt(SPEAKER_LABEL_GUARDRAIL=SPEAKER_LABEL_GUARDRAIL, current_report_json=current_report_json, instruction=instruction, role=role)

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
            edited = json.loads(json_str)
            if not isinstance(edited, dict):
                return False

            edited = self._normalize_analysis_report_lists(
                edited, max_annexure=self._get_annexure_count(workspace_id)
            )
            if not isinstance(edited, dict):
                return False

            # If the LLM produced an identical report (e.g. a rename whose term
            # was not present), nothing actually changed — return False so the
            # caller does NOT report "analysis report" as Updated and does not
            # emit a no-op payload.
            try:
                if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                    current_report_norm, sort_keys=True, ensure_ascii=True
                ):
                    logger.info(
                        "Analysis report edit produced no change — instruction={!r}",
                        instruction[:160],
                    )
                    return False
            except Exception:
                pass

            # Re-tag the edited report so the emitted payload keeps the
            # {id,text,isMatched} shape: unchanged items match currentState and
            # keep their id; text the user just edited becomes a new item.
            try:
                edited = await self._tag_analysis_report_against_current_state(
                    workspace_id, edited, log_context=" (surgical edit)",
                )
            except Exception as tag_err:
                logger.debug(
                    "Surgical-edit currentState tagging failed: {}; emitting untagged",
                    tag_err,
                )

            self._workspace_analysis_payload[workspace_id] = edited

            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="analysis_report",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=edited,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ Analysis report surgical edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Analysis report surgical edit failed: {}", e)
            return False

    async def _generate_analysis_report_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate analysis report (quality score, missing info, strengths, risks, jurisdiction)
        from workspace documents from the perspective of the selected role.

        Returns payload suitable for document_feature_response with featureType "analysis_report":
        missingInfo, strengths [{point, laws}], weaknesses, risks, relevantJurisdiction,
        caseResolutionScore (0-100), qualityScoreExplanation, reportQualityScoreExplanation,
        pendingActions, nextSteps, suggestedDrafts.
        """
        try:
            from app.agents.role_models import RoleBehaviorContract
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            logger.info(
                "📊 Generating analysis report for workspace {} with role {}",
                workspace_id,
                workspace_role,
            )

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
                logger.info(
                    "Retrieved {} document(s) from vector store for analysis report",
                    len(all_docs),
                )
            except Exception as vector_error:
                logger.warning(
                    "Could not retrieve documents from vector store: {}",
                    vector_error,
                )

            # Build parts with per-document cap so ALL documents are represented (no limit on number of docs)
            # Large budget so 100+ docs each get a meaningful slice (100 docs → 1000 chars each, 200 → 500 each)
            max_total_content = (
                100000  # Total character budget for all documents combined
            )
            num_docs = len(
                [
                    d
                    for d in all_docs
                    if (d.get("content") or "").strip()
                    and len((d.get("content") or "").strip()) >= 20
                ]
            )
            # Per-doc cap: every document included, fair share of budget (no doc count limit)
            per_doc_cap = (
                (max_total_content // max(1, num_docs))
                if num_docs
                else max_total_content
            )

            parts = []
            for i, doc in enumerate(all_docs, 1):
                content = (doc.get("content") or "").strip()
                if not content or len(content) < 20:
                    continue
                if len(content) > per_doc_cap:
                    content = content[:per_doc_cap] + "\n[... truncated for length ...]"
                # Build labelled block with metadata header (document_type,
                # issued_by, addressees, subject, key dates, legal provisions,
                # drafted_by_assistant marker) so the analysis-report prompt
                # treats drafted documents as completed actions.
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

            if num_docs > 0 and len(combined_document_content) > max_total_content:
                combined_document_content = (
                    combined_document_content[:max_total_content]
                    + "\n[... content truncated ...]"
                )
                logger.info(
                    "Truncated combined document content for analysis report to ~{} chars ({} docs, ~{} chars each)",
                    max_total_content,
                    num_docs,
                    per_doc_cap,
                )
            elif num_docs > 1:
                logger.info(
                    "Analysis report using all {} documents (~{} chars per doc cap)",
                    num_docs,
                    per_doc_cap,
                )

            doc_count = len(all_docs)
            if doc_count == 0:
                return {
                    "missingInfo": [],
                    "strengths": [],
                    "weaknesses": [],
                    "risks": [],
                    "relevantJurisdiction": [],
                    "caseResolutionScore": 0,
                    "qualityScoreExplanation": "",
                    "reportQualityScoreExplanation": "",
                    "pendingActions": [],
                    "nextSteps": [],
                    "suggestedDrafts": [],
                }

            role_ctx = RoleBehaviorContract.get_context(role)
            role_instruction = (
                "Analyze from the perspective of {}: {}. "
                "Framing: {}. Do not: {}.".format(
                    role,
                    role_ctx.reasoning_style,
                    "; ".join(role_ctx.framing_rules[:3]),
                    "; ".join(role_ctx.forbidden_actions[:2]),
                )
            )
            # Anchor analysis to the latest strategy/objective so regenerated reports
            # reflect lawyer edits from intelligence chat.
            strategy_payload = self._workspace_strategy_payload.get(workspace_id) or {}
            if not isinstance(strategy_payload, dict):
                strategy_payload = {}
            current_case_summary = str(strategy_payload.get("caseSummary") or "").strip()
            current_strategic_objective = str(
                strategy_payload.get("strategicObjective") or ""
            ).strip()
            if not current_case_summary:
                current_case_summary = "(not provided)"
            if not current_strategic_objective:
                current_strategic_objective = "(not provided)"

            document_inventory = self._build_document_inventory(workspace_id)

            # Tell the model exactly how many annexures exist so it cannot
            # invent "Annexure 2", "Annexure 3", ... when only one upload
            # is present. The post-processing clamp in
            # _normalize_analysis_report_lists is the safety net; this is
            # the prevention layer.
            num_user_uploads = num_docs
            if num_user_uploads <= 0:
                annexure_limit_block = (
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    "- No annexures have been uploaded.\n"
                    "- Do NOT reference any 'Annexure N' anywhere in the output."
                )
            elif num_user_uploads == 1:
                annexure_limit_block = (
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    "- The user has uploaded exactly 1 annexure.\n"
                    "- The ONLY valid annexure reference is: Annexure 1.\n"
                    "- You MUST NOT reference Annexure 2 or any higher number.\n"
                    "- Do NOT invent additional annexures. Do NOT use the plural form \"annexures\"."
                )
            else:
                annexure_limit_block = (
                    generate_analysis_report_from_documents_prompt(num_user_uploads=num_user_uploads)
                )

            # Deep-extraction pre-pass: pull structured entities (parties,
            # dates, suits, properties, amounts, statutes, annexures) so the
            # analysis_report synthesis below can anchor strengths /
            # weaknesses / risks / missingInfo to named facts instead of
            # generic phrasing. Shares the content-hash cache with the strategy
            # pipeline — when strategy already ran for this content this is a
            # cache hit, no extra Gemini calls.
            case_entities = await self._extract_case_entities(
                workspace_id=workspace_id,
                combined_document_content=combined_document_content,
            )
            _ar_party_names = [
                (p.get("name") or "").strip()
                for p in (case_entities.get("parties") or [])
                if isinstance(p, dict) and (p.get("name") or "").strip()
            ]
            logger.info(
                "🧠 Analysis-report extraction complete for workspace {}: parties=[{}]",
                workspace_id[:12] if workspace_id else "",
                ", ".join(_ar_party_names) if _ar_party_names else "(none)",
            )
            import json as _json_ar
            try:
                _entities_block_ar = _json_ar.dumps(
                    case_entities, indent=2, ensure_ascii=False
                )
            except Exception:
                _entities_block_ar = "{}"

            # Current-date context for year resolution: a source date stated
            # without a year defaults to the current year (mirrors list_of_dates).
            _today_ar = datetime.utcnow()
            today_str_ar = _today_ar.strftime("%Y-%m-%d")
            current_year_ar = _today_ar.strftime("%Y")

            analysis_prompt = (
                """You are a senior legal analyst specializing in Indian law. Your task is to produce a structured legal analysis based ONLY on the uploaded documents.

EXTRACTED FACTS (these entities were already pulled from the documents by a
focused extraction step — treat them as authoritative ground truth. Anchor
every reference in strengths / weaknesses / risks / missingInfo to these.
Do NOT introduce 'Party No. 1' / 'the plaintiff' / 'the petitioner' /
'the respondent' style placeholders — EVERY reference to a person, entity,
property, suit, amount, or statute MUST use the exact form listed below.
relevantJurisdiction is governed by the strict FORMAT block below and is
NOT subject to this entity-grounding directive.):

{entities_block_ar}

CRITICAL OUTPUT RULES:
- Your entire response MUST be exactly one valid JSON object.
- Do NOT output markdown.
- Do NOT output code blocks.
- Do NOT output explanations outside JSON.
- The first character MUST be {{ and the last character MUST be }}.
- Escape quotes inside strings using \\".

ROLE: {}
ROLE INSTRUCTION: {}
CURRENT CASE SUMMARY (from strategy panel):
{}
CURRENT STRATEGIC OBJECTIVE (from strategy panel):
{}

{}

{annexure_limit_block}

DOCUMENTS (in upload order; last document is most recent):
{}

OBJECTIVE-ALIGNMENT RULES (MANDATORY):
- The CURRENT STRATEGIC OBJECTIVE is the PRIMARY lens. EVERY strength, weakness, risk and missingInfo item MUST be about ACHIEVING THIS objective, using the annexures as evidence. Do NOT carry over or retain analysis framed for a different or earlier objective — if the objective is "divorce", analyse the divorce posture (grounds, cruelty/desertion, custody, maintenance, evidence of breakdown), NOT an unrelated factual angle (e.g. recovering stolen property) unless that fact directly advances the divorce objective.
- If CURRENT STRATEGIC OBJECTIVE is provided, align analysis to it while staying faithful to annexure facts.
- Evaluate objective feasibility using annexure evidence: strengths/weaknesses/risks must explicitly reflect what helps or blocks that objective.
- missingInfo should prioritize gaps that are critical to execute that objective.
- pendingActions and nextSteps must be practical actions that move the matter toward that objective in priority order.
- relevantJurisdiction must reflect objective-linked forum cues in annexures or objective text (for example, if the objective says filing in Patna, assess whether Patna forum support appears in annexure facts and list appropriate jurisdiction/forum entries).
- FORUM-NEXUS CONFLICT (MANDATORY): if the objective names a forum/body/location that is unsuitable in EITHER of these ways, you MUST NOT present it as supported — you MUST add (a) a top-priority `risks` item naming the problem and (b) a `missingInfo` item asking for what is needed to sustain that route:
   (a) TERRITORIAL — the forum's location has NO factual nexus in EXTRACTED FACTS or the annexures (no party, event, cause of action, or property located there); e.g. "file in Patna" for a matter arising elsewhere. The risk names the territorial-jurisdiction / maintainability problem (Article 226(2) / Section 197 BNSS); the missingInfo asks for the specific nexus/cause-of-action link.
   (b) AUTHORITY / SUBJECT-MATTER — the named forum has NO power to grant the requested relief for this matter type (e.g. an international or global body such as the ICC, a regulator, or a wrong-tier forum that does not adjudicate this domestic dispute). The risk states that this forum lacks jurisdiction/authority over the matter and names the forum that actually does; the missingInfo asks for the legal basis or realistic route (e.g. a guideline-breach representation / referral) to reach the requested forum.
  You may still list the objective's forum in relevantJurisdiction, but the paired risk + missingInfo are required, and `nextSteps` must reflect the realistic route (not silently revert to the prior forum's steps).
- NEVER invent a party, organisation, body, or place to manufacture a nexus to the objective's forum. Keep the real entities from EXTRACTED FACTS — do not rename or relocate them to match the objective.
- Never fabricate facts to force alignment; when objective and evidence conflict, state the conflict as BOTH a `risks` item AND (where information is needed) a `missingInfo` item — not merely as a weakness.

RELEVANT JURISDICTION FORMAT (STRICT):
- Output must be COURT/FORUM-centric by location (High Court / Supreme Court / District Court / Family Court / Magistrate Court / Sessions Court / Tribunal / Commission etc.). Cover BOTH the criminal-side forum and the civil-side forum where both branches are live.
- Return ONLY jurisdiction/forum names (no reasons, no legal basis text, no explanatory suffix).
- Do NOT output location-only permissibility lines like "Patna, Bihar: Permissible under ...".
- Good examples (criminal side):
  - "Metropolitan Magistrate Court, Delhi"
  - "District & Sessions Court, Patna"
  - "High Court of Delhi (Criminal Side)"
  - "Supreme Court of India"
- Good examples (civil / tribunal side):
  - "District Civil Judge, Patna"
  - "Family Court, Bandra, Mumbai"
  - "District Consumer Disputes Redressal Commission, Pune"
  - "State Consumer Disputes Redressal Commission, Maharashtra"
  - "National Consumer Disputes Redressal Commission, New Delhi"
  - "National Company Law Tribunal, Mumbai Bench"
  - "Debts Recovery Tribunal, Delhi"
  - "Real Estate Regulatory Authority, Karnataka"
  - "Arbitral Tribunal (seat: Mumbai)"
  - "High Court of Bombay (Civil Side / Commercial Division)"

LEGAL FRAMEWORK:
Use applicable Indian laws across BOTH civil-law and criminal-law branches. The following are illustrative (NOT exhaustive — any other Indian central or state law may be cited if facts and sources support it):

CRIMINAL-LAW SIDE (preferred citations):
- Bharatiya Nyaya Sanhita (BNS), 2023 — substantive offences
- Bharatiya Nagarik Suraksha Sanhita (BNSS), 2023 — criminal procedure
- Bharatiya Sakshya Adhiniyam (BSA), 2023 — evidence
- Information Technology Act, 2000 (cyber-offences)
- Negotiable Instruments Act, 1881 — Section 138 (cheque dishonour, quasi-criminal)
- PMLA 2002, NDPS 1985, Arms Act 1959, POCSO 2012, PCA 1988, UAPA 1967, Dowry Prohibition Act 1961, etc., where facts fit

CIVIL-LAW SIDE:
- Indian Contract Act, 1872 (breach, misrepresentation, indemnity, agency)
- Specific Relief Act, 1963 (specific performance, injunctions, declarations)
- Transfer of Property Act, 1882 (sale, mortgage, lease, gift, easement)
- Code of Civil Procedure, 1908 (suit / plaint / appeal / execution)
- Limitation Act, 1963 (time-bar)
- Sale of Goods Act, 1930 (goods quality / passing of property)
- Consumer Protection Act, 2019 (defect / deficiency / unfair trade practice)
- Companies Act, 2013 (oppression, mismanagement, director duties, NCLT)
- Arbitration and Conciliation Act, 1996 (arbitration clauses)
- Hindu Marriage Act 1955 / Special Marriage Act 1954 / Hindu Succession Act 1956 / Guardians and Wards Act 1890 (family / succession)
- Real Estate (Regulation and Development) Act, 2016; Motor Vehicles Act, 1988; Insolvency and Bankruptcy Code, 2016, etc., where facts fit

DO NOT cite:
- IPC (Indian Penal Code, 1860) — use BNS 2023 instead
- CrPC (Code of Criminal Procedure, 1973) — use BNSS 2023 instead
- Indian Evidence Act, 1872 — use BSA 2023 instead

DUAL LENS — CIVIL LAW + CRIMINAL LAW (MANDATORY):
Before producing strengths / weaknesses / risks / pendingActions / nextSteps / relevantJurisdiction, consciously scan BOTH branches:
- (Criminal angle) "What offence / criminal-procedural / evidence question do these facts attract?" → BNS / BNSS / BSA / IT Act / NI Act §138 / PMLA / NDPS, etc.
- (Civil angle)    "What private remedy / suit / regulatory complaint do the same facts support?" → Contract Act / Specific Relief Act / TPA / CPC / Limitation Act / Consumer Protection Act / Companies Act / Arbitration Act / family laws / RERA / IBC, etc.

When BOTH branches are plausibly supported by the documents, the analysis MUST cover both:
- `strengths`: include factual strengths from each branch where present (each row's `laws` array can mix branches: e.g. `["BNS Section 318", "Indian Contract Act, 1872 Section 17"]`).
- `weaknesses` and `risks`: surface gaps / objections / procedural risks from BOTH the criminal track (e.g. ingredients of offence not made out, BNSS bail risk, evidence admissibility under BSA) and the civil track (e.g. limitation under Limitation Act 1963, want of cause of action, jurisdiction, forum-selection / arbitration clause, locus, deficiency-vs-defect framing).
- `pendingActions` and `nextSteps`: when both branches are live, include at least one immediate criminal-track action AND at least one immediate civil-track action (e.g. "Preserve digital evidence and file BNS §318 complaint" alongside "Issue legal notice and prepare suit for damages under Contract Act §73 / Specific Relief Act §10").
- `relevantJurisdiction`: list forums from BOTH branches where applicable (criminal: Magistrate / Sessions / High Court criminal side; civil: District Civil Judge / Family Court / High Court civil side / Consumer Commissions (DCDRC / SCDRC / NCDRC) / NCLT / NCLAT / Debts Recovery Tribunal / RERA Authority / Arbitral Tribunal / Labour Court / Industrial Tribunal, as facts warrant).

When the facts only support one branch (purely civil OR purely criminal), stay in that branch — do NOT manufacture the other side just to look balanced. Coherence beats balance.

**No bracket tags in user-facing output.** Do NOT prefix or label any item in `strengths` / `weaknesses` / `risks` / `pendingActions` / `nextSteps` / `suggestedDrafts` / `relevantJurisdiction` with "(criminal angle)", "(civil angle)", "(criminal-law angle)", "(civil-law angle)", or any similar bracketed tag. Cite the relevant statutes inside the `laws` array (where present) or weave them into normal sentences — never use a parenthesised "angle" prefix.

COMPLETED-ACTION RECOGNITION (CRITICAL):
Inspect the metadata header on every annexure. When a document is a `notice` / `complaint` / `affidavit` / `application` / `plaint` AND either (a) its `ISSUED BY` field names the client side (matches the active role, the named client, or the client's advocate), OR (b) the header carries a `[DRAFTED BY ASSISTANT …]` marker, treat the underlying procedural action as **ALREADY COMPLETED** in this matter. Apply this consistently:
- `strengths`: surface the completed action as a procedural strength when relevant (e.g. *"Demand notice under §73 Indian Contract Act, 1872 has already been served on the noticees, perfecting the cause of action for a money-recovery suit"*). Cite the served-on date and the addressees.
- `weaknesses` / `risks`: do NOT flag "notice not yet served" or "complaint not yet filed" as a weakness if the metadata shows the action has been completed; instead flag dependent risks (e.g. *"Compliance deadline 06/06/2026 — if no response, the matter must move to suit promptly to avoid limitation drift"*).
- `pendingActions` and `nextSteps`: **DO NOT re-list the completed action**. If a legal/demand notice has been served, the next step is monitoring the compliance window and preparing the follow-up suit, not *"issue a legal notice"*. If an FIR complaint has been filed, the next step is follow-up on registration / investigation status, not *"file an FIR"*. Same for affidavits, applications, plaints, written statements.
- Apply independently to each completed action if multiple drafted documents exist.

DOCUMENT RELATION RULE:
If documents appear to include multiple matters, mention that ambiguity and prioritize the dispute best supported by evidence.

--------------------------------------------------
STEP 1 — FACT EXTRACTION (internal reasoning only)
--------------------------------------------------

Carefully read the documents and identify key facts such as:

- parties involved
- dates
- transactions
- contractual obligations
- warranty or service terms
- communications
- disputes raised
- evidence provided

Only treat something as "present" if it is explicitly stated in the documents.

--------------------------------------------------
STEP 2 — MISSING INFORMATION VERIFICATION
--------------------------------------------------

Before listing an item in missingInfo, verify ALL THREE conditions:

1. The information is NOT present anywhere in the documents.
2. The information is NOT covered by any documentType listed in DOCUMENT INVENTORY (e.g. if inventory shows aadhaar_card, do NOT add "Aadhaar / identity proof missing"; if inventory shows bank_statement, do NOT add "bank statement / account proof missing"; if inventory shows pan_card, do NOT add "PAN / tax identity missing").
3. The information is CRITICAL to prove the case or defend the case.

If ANY condition fails, DO NOT include it.

DOCUMENT-TYPE COVERAGE TABLE (use to map inventory entries to common missingInfo phrasings — if the type is in inventory, the listed missingInfo phrasings are FORBIDDEN):
- aadhaar_card → identity proof, Aadhaar, UID
- pan_card → PAN, tax ID, Income Tax identifier
- passport → passport, foreign travel proof
- voter_id → voter ID, EPIC, electoral identity
- driving_licence → driving licence, DL, address proof
- msme_certificate → MSME registration, Udyam certificate, enterprise registration
- gst_certificate → GST registration, GSTIN
- bank_statement → bank statement, account statement, financial transactions proof
- itr_return → income tax return, ITR, income proof
- salary_slip / form_16 → salary proof, employment proof, income evidence
- electricity_bill / water_bill / gas_bill / telecom_bill → address proof, utility bill
- sale_deed / rental_agreement / loan_agreement → property documents, agreement, written contract
- fir / complaint → FIR, police complaint, criminal complaint
- court_order / judgment → court order, judgment, prior decision
- notice / affidavit → notice, sworn statement
- marriage_certificate → marriage proof
- birth_certificate → birth proof, age proof
- death_certificate → death proof

STRICT EXCLUSIONS:
Do NOT list these as missing if they appear anywhere in the documents:

- serial number
- model number
- purchase date
- seller name
- dealer name
- invoice
- receipt
- proof of purchase
- reference numbers
- warranty card details
- document dates

Only include missing information that a lawyer would NEED to establish liability or relief.

Examples of valid missingInfo:
- warranty scope not defined
- reason for termination unclear
- identity of signatory not specified
- contract terms not included
- formal complaint not documented

Maximum items: 8

--------------------------------------------------
STEP 3 — LEGAL ANALYSIS
--------------------------------------------------

All strengths and weaknesses MUST be based on facts from the documents.

Strength rules:
- **MANDATORY: minimum 2 strengths, maximum 6.** Returning 0 or 1 is a schema violation. Every matter has at least two facts that help the client's position — find them.
- Each `point` must state the factual and legal reasoning in plain prose as 2-4 sentences (clear, specific to the annexures), naming the party / date / document / amount it relies on (from EXTRACTED FACTS).
- Put **statutes and sections only** in the `laws` array (e.g. "BSA", "BNS Section 318", "IT Act 2000").
- **Forbidden in `point` text:** do not end (or pad) a strength with boilerplate such as "which is a relevant fact under [Act]" or "admissible under [Act(s)]" — no trailing "under the Bharatiya Sakshya Adhiniyam" / "under the IT Act" restatements; the `laws` field carries citations.
- Must be supported by the annexure facts.

What COUNTS as a strength (use these categories when evidence feels thin — at least 2 of these almost always apply):
1. **Documentary evidence** preserved in an annexure — e.g. a registered sale deed, MLC / medico-legal certificate, hospital record, FIR, charge-sheet, written notice, audio/video transcript, bank statement, signed contract, photograph, official certificate.
2. **Party admissions** — anything the opposing side stated, signed, or did that supports the client's position (e.g. partial payment, written reply admitting a fact, official detention record).
3. **Procedural advantage** — favourable forum / jurisdiction, limitation period still open, statutory presumption in client's favour (e.g. presumption of cruelty under §498A successor, presumption under BSA §85B for electronic records).
4. **Statutory framework support** — a named provision under BNS / BNSS / BSA / IT Act / Specific Relief Act / Hindu Marriage Act / Domestic Violence Act / etc. that, on the recorded facts, points to relief for the client.
5. **Corroboration across annexures** — two or more annexures that independently support the same fact, increasing evidentiary weight. ONLY apply this category when AVAILABLE ANNEXURES shows multiple annexures are present; when only a single annexure exists, do NOT invent corroboration.

If the documents are genuinely sparse, still produce 2 strengths by anchoring to the annexure(s) actually uploaded (within the hard limit in AVAILABLE ANNEXURES) and the most credible statutory provision. Do NOT skip the field. Do NOT replace it with empty `[]`.

Weakness rules:
- Only list weaknesses supported by document facts
- Do NOT invent weaknesses

Risk rules:
- Procedural or legal risks (limitation, burden of proof, jurisdiction issues)

--------------------------------------------------
CASE RESOLUTION SCORE RULE
--------------------------------------------------

caseResolutionScore must follow STRICT logic:

100 = only when:
- missingInfo is empty
- weaknesses is empty
- risks is empty

If ANY of those contain items → score must be < 100.

Scoring logic (ALWAYS START FROM 100, then subtract):
- Start at 100.
- Each missingInfo item: -5
- Each weakness: -5
- Each risk: -3

The score is the integer result of that subtraction. NEVER output 0 for a case
that has any strengths or analysed content — clamp the final value between 30 and 100.
caseResolutionScore is REQUIRED and MUST be a plain integer (e.g. 72), never null,
never a string, never with a "%" sign.

--------------------------------------------------
OUTPUT JSON STRUCTURE
--------------------------------------------------

Output keys in this exact order (score and explanations FIRST so they are not truncated):
- caseResolutionScore, qualityScoreExplanation, and reportQualityScoreExplanation MUST appear immediately after the opening "{{".

{{
  "caseResolutionScore": 72,
  "qualityScoreExplanation": "Required: 3-5 sentences explaining how this score follows from THIS case's specific missing info, weaknesses, risks, and strengths. MUST start with 'The current score is <N>% because'. MUST name at least one specific missing item, one specific weakness/risk, and one specific strength using named parties (from EXTRACTED FACTS), specific dates (DD/MM/YYYY), exact amounts (Rs. notation), and Annexure/suit references — never generic phrasing like 'Score reflects gaps in annexures and procedural risks'. Non-empty.",
  "reportQualityScoreExplanation": "Required: 3-5 sentences with the same scoring logic as qualityScoreExplanation, written for external or report-facing use. MUST start with 'The current score is <N>% because'. Same entity-grounding rule — name at least one specific missing item, one specific weakness/risk, and one specific strength using named parties, dates, amounts, and Annexure/suit references. Do not use the words 'legal' or 'respondent' (any casing). Use neutral wording (e.g. procedural risk, parties, case file). Non-empty.",
  "missingInfo": ["Each missing-info entry is a 2-4 sentence string naming WHAT is missing, FROM WHOM (the named party/document/Annexure it should come from), WHY it matters for the strategic objective, and which existing Annexure (if any) currently approximates it. Use exact names and identifiers from EXTRACTED FACTS — never one-word labels like 'address' or 'medical report'."],
  "strengths": [
    {{
      "point": "MANDATORY 2-4 SENTENCES of factual prose (NOT a label, NOT a one-liner). Name the specific parties (use exact names from EXTRACTED FACTS), the dated events (DD/MM/YYYY), exact amounts in source notation (Rs. 11,64,000), suit/document references (O.S. No. 827/2019), and the Annexure(s) that establish this strength. Do NOT pad with statute boilerplate phrases such as 'which is a relevant fact under <Act>, YYYY' or 'admissible under <Act>'; statutes belong only in the laws array. Anchor every claim in named facts. The whole point string must be at least 120 characters.\n\nGOOD example (do this): 'Rohan Sharma was 20 years old at the time of the arrest on 18/04/2026, as recorded in Annexure 1. Youth at this scale is a recognised mitigating factor for bail and is supported by the contact log showing his sister approached counsel within five days of the remand to Tihar Jail.'\n\nBAD example (do NOT do this): 'Youth of the accused.' — that is a label, not a strength.\n\nBAD example (do NOT do this): 'Youth of the accused, which is a relevant fact under the Narcotic Drugs and Psychotropic Substances Act, 1985.' — single-sentence label padded with a citation; the citation belongs in the laws array, and the prose must be 2-4 sentences with named entities.",
      "laws": ["BNS Section 318", "BSA"]
    }}
  ],
  "weaknesses": ["Each weakness is a 2-4 sentence string naming the SPECIFIC party (exact name), the dated event, the exact amount or document/annexure, and the statute or principle that makes this a weakness for the client's case. Never generic phrasing like 'limitation may apply' — say which party's claim is exposed under which Act and why."],
  "risks": ["Each risk is a 2-4 sentence string explaining the SPECIFIC procedural / evidentiary / limitation / contradiction risk, with the named party + date + document/annexure that triggers it, and the named rule (e.g. Limitation Act 1963 §54, BNSS §187, BSA §65B) that is exposed. Use exact names and amounts from EXTRACTED FACTS."],
  "relevantJurisdiction": ["District Court, City"],
  "pendingActions": ["action1"],
  "nextSteps": ["step1","step2"],
  "suggestedDrafts": ["Draft Legal Notice"]
}}

RULES:

missingInfo:
0–8 items
ONLY information absent from documents and legally required.

strengths:
**MANDATORY 2-6 items (never 0, never 1).** Each has factual `point` prose (2-4 FULL sentences with named parties, DD/MM/YYYY dates, exact amounts, and Annexure references — minimum 120 characters total) plus `laws` for citations. **NEVER emit a label-only `point` like "Youth of the accused" or "Duration of detention"** — that is a heading, not a strength. **NEVER pad a label with citation boilerplate like ", which is a relevant fact under <Act>, YYYY"** — statutes belong only in the `laws` array; the prose must stand on its own as 2-4 sentences of named facts.

weaknesses:
0–6 factual weaknesses.

risks:
0–6 procedural/legal risks.

relevantJurisdiction:
1–5 courts or legal forums.

pendingActions / nextSteps / suggestedDrafts:
0–5 items each.

qualityScoreExplanation RULE:
- MANDATORY non-empty string of 3-5 sentences.
- MUST start with "The current score is <N>% because" (where N is the integer caseResolutionScore).
- MUST cite the SPECIFIC facts driving the score: name at least one missingInfo item by what is missing and from whom (using EXTRACTED FACTS names), one weakness or risk by the named party/date/amount/annexure that triggers it, and one strength by the named fact (party + date + Annexure) that supports it.
- If the score is 100, say specifically that no missingInfo, weaknesses, or risks were flagged, and name the top 1-2 strengths (parties + dates + Annexures) that justify it.
- Never use generic phrasing like "gaps in annexures" or "procedural risks identified" without naming WHICH gap, WHICH risk, and FOR WHOM.

reportQualityScoreExplanation RULE:
- MANDATORY non-empty string of 3-5 sentences.
- MUST start with "The current score is <N>% because" (same N as above).
- Same entity-grounding obligation as qualityScoreExplanation — name specific missingInfo / weakness / risk / strength items with parties, dates, amounts, and Annexure/suit references from EXTRACTED FACTS.
- MUST NOT contain the words "legal" or "respondent" in any form—including compounds where they appear as standalone words (rewrite to neutral alternatives: e.g. procedural risk, parties, opposing side, case file, annexures).

FINAL RULE:
Output ONLY the JSON object.
No extra text.

ENTITY-GROUNDING RULE (CRITICAL):
- Every entry in `strengths` (the `point` field), `weaknesses`, `risks`, and `missingInfo` MUST cite at least one named party, specific date (DD/MM/YYYY), exact amount, suit/case/FIR number, or Annexure reference taken from EXTRACTED FACTS — never generic categories.
- DATE INTERPRETATION: Today is {current_year_ar}. When a date appears in EXTRACTED FACTS without an explicit year, render it in DD/MM/YYYY using the CURRENT YEAR ({current_year_ar}); step back one year only if the current year would place the event in the future. Never default to 2024 or any other legacy year for a date whose year is not stated in the source. Statute years (e.g. "Information Technology Act, 2000", "BNS, 2023") are fixed by law and MUST NOT be changed by this rule.
- Do NOT use "Party No. 1" / "Party No. 2" / "the plaintiff" / "the respondent" / "the petitioner" anywhere in `strengths`, `weaknesses`, `risks`, `missingInfo`, `pendingActions`, `nextSteps`, `suggestedDrafts`, or the explanation strings. Always use the exact names listed in EXTRACTED FACTS.
- The `relevantJurisdiction` array is EXEMPT from this rule: it follows the strict COURT/FORUM-by-location format defined above and lists only jurisdiction/forum names — no party names, no dates, no amounts.
"""
            ).format(
                role,
                role_instruction,
                current_case_summary,
                current_strategic_objective,
                document_inventory or "DOCUMENT INVENTORY: (no classified inventory available — reason from DOCUMENTS text below)",
                combined_document_content,
                entities_block_ar=_entities_block_ar,
                annexure_limit_block=annexure_limit_block,
                current_year_ar=current_year_ar,
            )

            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                analysis_prompt = analysis_prompt.rstrip() + "\n\n" + custom_suffix

            ai_response = await gemini_client.generate(
                prompt=analysis_prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    workspace_role
                ),
                temperature=0.2,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            logger.info(
                "Analysis report AI response length: {} chars",
                len(ai_response or ""),
            )

            import json
            import re

            raw = (ai_response or "").strip()
            # Strip markdown code blocks so we can find JSON inside
            if "```" in raw:
                code_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
                if code_match:
                    raw = code_match.group(1).strip()
                else:
                    raw = re.sub(r"^```\w*\s*", "", raw)
                    raw = re.sub(r"\s*```\s*$", "", raw).strip()
            # Find first "{" and extract balanced JSON object (handles preamble text and nested JSON)
            # Use string-aware scan so braces inside "..." don't break balance
            json_str = None
            start = raw.find("{")
            if start >= 0:
                depth = 0
                i = start
                in_string = False
                escape = False
                quote_char = None
                while i < len(raw):
                    c = raw[i]
                    if in_string:
                        if escape:
                            escape = False
                        elif c == "\\":
                            escape = True
                        elif c == quote_char:
                            in_string = False
                        i += 1
                        continue
                    if c == '"' or c == "'":
                        in_string = True
                        quote_char = c
                        i += 1
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            json_str = raw[start : i + 1]
                            break
                    i += 1
            if not json_str and raw.startswith("{"):
                json_str = raw
            if json_str and json_str.count("{") != json_str.count("}"):
                open_braces = json_str.count("{") - json_str.count("}")
                json_str = json_str.rstrip() + "}" * open_braces

            if not json_str:
                logger.warning(
                    "No JSON found in analysis report response; using fallback. raw preview: {}",
                    (raw or "")[:400],
                )
                return self._analysis_report_fallback(role)

            try:
                data = json.loads(json_str)
                parsed_from_partial = False
            except json.JSONDecodeError as e:
                # Try to repair truncated JSON (e.g. "Unterminated string" when Gemini hits token limit)
                data = None
                parsed_from_partial = False
                if "Unterminated string" in (e.msg or "") or "Unterminated" in (
                    e.msg or ""
                ):
                    pos = getattr(e, "pos", None)
                    if pos is not None and pos > 0 and pos <= len(json_str):
                        truncated = json_str[:pos].rstrip()
                        # Key has no value or value was cut: add empty string and close structure
                        if truncated.endswith(":"):
                            truncated += ' ""'
                        elif truncated.endswith(':"') or (
                            truncated.endswith('"') and not truncated.endswith('""')
                        ):
                            truncated += '"'  # close the opened string as empty
                        else:
                            truncated += ' ""'
                        open_brackets = truncated.count("[") - truncated.count("]")
                        open_braces = truncated.count("{") - truncated.count("}")
                        truncated += "]" * open_brackets + "}" * open_braces
                        try:
                            data = json.loads(truncated)
                            parsed_from_partial = True
                            logger.info(
                                "Repaired analysis report JSON after truncation (pos={})",
                                pos,
                            )
                        except json.JSONDecodeError:
                            pass
                if data is None and getattr(e, "pos", None):
                    # Fallback salvage: trim to last complete token boundary and close containers.
                    partial = json_str[: e.pos].rstrip()
                    while partial and partial[-1] not in [",", "}", "]"]:
                        partial = partial[:-1].rstrip()
                    partial = partial.rstrip(",")
                    if partial:
                        open_brackets = partial.count("[") - partial.count("]")
                        open_braces = partial.count("{") - partial.count("}")
                        if open_brackets > 0:
                            partial += "]" * open_brackets
                        if open_braces > 0:
                            partial += "}" * open_braces
                        try:
                            data = json.loads(partial)
                            parsed_from_partial = True
                            logger.info(
                                "Salvaged analysis report JSON from partial response (pos={})",
                                e.pos,
                            )
                        except json.JSONDecodeError:
                            pass
                if data is None:
                    logger.warning(
                        "Analysis report JSON parse error: {}; using fallback", e
                    )
                    return self._analysis_report_fallback(role)

            # Tail recovery: Gemini may return valid JSON with missing/empty tail arrays.
            # Recover those sections via a compact follow-up call and merge.
            tail_keys = [
                "weaknesses",
                "risks",
                "relevantJurisdiction",
                "pendingActions",
                "nextSteps",
                "suggestedDrafts",
            ]
            missing_tail_keys = []
            for k in tail_keys:
                v = data.get(k)
                if k not in data or v is None:
                    missing_tail_keys.append(k)
                    continue
                if isinstance(v, list) and len(v) == 0:
                    missing_tail_keys.append(k)
            if missing_tail_keys:
                try:
                    recovery_prompt = (
                        """Return ONLY one valid JSON object with these keys:
weaknesses, risks, relevantJurisdiction, pendingActions, nextSteps, suggestedDrafts

Context:
- Role: {}
- Case summary: {}
- Strategic objective: {}

Documents:
{}

Rules:
- Base strictly on annexure facts; do not invent.
- Each key must be a JSON array.
- For weaknesses, risks, and relevantJurisdiction: provide at least 1 item each whenever documents contain case facts. Use [] only when truly impossible.
- Keep concise and practical.
- Output JSON only.
"""
                    ).format(
                        role,
                        current_case_summary,
                        current_strategic_objective,
                        combined_document_content,
                    )
                    recovery_raw = await gemini_client.generate(
                        prompt=recovery_prompt,
                        system_instruction=get_structured_document_feature_system_instruction(
                            workspace_role
                        ),
                        temperature=0.1,
                        max_tokens=12000,
                        response_mime_type="application/json",
                    )
                    recovered = json.loads((recovery_raw or "").strip())
                    if isinstance(recovered, dict):
                        for k in missing_tail_keys:
                            v = recovered.get(k)
                            if isinstance(v, list):
                                data[k] = v
                    logger.info(
                        "Recovered analysis report tail keys (missing/empty): {}",
                        ", ".join(missing_tail_keys),
                    )
                except Exception as recovery_err:
                    logger.warning(
                        "Could not recover missing/empty tail keys: {}",
                        recovery_err,
                    )

            missing_info = data.get("missingInfo") or data.get("missing_info") or []
            # Coerce off-schema shapes (e.g. {"point": ..., "laws": [...]})
            # back to plain strings so the FE never sees Python dict reprs.
            missing_info = _coerce_analysis_text_list(missing_info)[:10]
            # Product language requirement: refer to uploaded files as annexures in analysis text.
            missing_info = [
                re.sub(
                    r"\bdocuments\b",
                    "annexures",
                    re.sub(r"\bdocument\b", "annexure", x, flags=re.IGNORECASE),
                    flags=re.IGNORECASE,
                )
                for x in missing_info
            ]

            strengths_raw = data.get("strengths") or []
            if not isinstance(strengths_raw, list):
                strengths_raw = []
            strengths = []
            for s in strengths_raw[:8]:
                if isinstance(s, dict):
                    point = (s.get("point") or s.get("text") or "").strip()
                    laws = s.get("laws") or s.get("law") or []
                    if not isinstance(laws, list):
                        laws = [laws] if laws else []
                    laws = [str(l).strip() for l in laws if l][:5]
                    if point:
                        point = re.sub(
                            r"\bdocuments\b",
                            "annexures",
                            re.sub(
                                r"\bdocument\b", "annexure", point, flags=re.IGNORECASE
                            ),
                            flags=re.IGNORECASE,
                        )
                        point = _strip_strength_point_statute_boilerplate(point)
                        if not point:
                            continue
                        strengths.append({"point": point, "laws": laws})
                elif isinstance(s, str) and s.strip():
                    point = s.strip()
                    point = re.sub(
                        r"\bdocuments\b",
                        "annexures",
                        re.sub(r"\bdocument\b", "annexure", point, flags=re.IGNORECASE),
                        flags=re.IGNORECASE,
                    )
                    point = _strip_strength_point_statute_boilerplate(point)
                    if not point:
                        continue
                    strengths.append({"point": point, "laws": []})

            # Guard: the schema mandates 2-6 strengths, each with 2-4 sentence
            # prose. The model occasionally returns an empty array OR emits
            # label-only points like "Youth of the accused" — usually because
            # the entity-grounding rules made it over-cautious on cases with
            # thin documentary evidence. Fire ONE focused retry when either:
            #   (a) fewer than 2 strengths were produced, OR
            #   (b) any surviving strength has a point shorter than 40 chars
            #       (a label, not a 2-4 sentence strength).
            _has_short_strength_point = any(
                isinstance(s, dict) and len((s.get("point") or "").strip()) < 40
                for s in strengths
            )
            if (len(strengths) < 2 or _has_short_strength_point) and (
                isinstance(combined_document_content, str)
                and combined_document_content.strip()
                and combined_document_content.strip() != "No document content available."
            ):
                logger.warning(
                    "Analysis report returned {} strength(s) (short_label_present={}) "
                    "for workspace {}; firing focused retry to recover full prose.",
                    len(strengths),
                    _has_short_strength_point,
                    workspace_id[:12] if workspace_id else "",
                )
                try:
                    _strengths_retry_prompt = (
                        generate_analysis_report_from_documents_prompt2(_entities_block_ar=_entities_block_ar, combined_document_content=combined_document_content, data=data, missing_info=missing_info)
                    )
                    _strengths_retry_raw = await gemini_client.generate(
                        prompt=_strengths_retry_prompt,
                        system_instruction=get_structured_document_feature_system_instruction(
                            workspace_role
                        ),
                        temperature=0.2,
                        max_tokens=12000,
                        response_mime_type="application/json",
                    )
                    _strengths_retry_json = self._prepare_json_candidate(
                        (_strengths_retry_raw or "").strip(), prefer="object"
                    )
                    if _strengths_retry_json:
                        import json as _json_sr
                        _retry_parsed = _json_sr.loads(_strengths_retry_json)
                        if isinstance(_retry_parsed, dict):
                            _retry_strengths = _retry_parsed.get("strengths") or []
                            if isinstance(_retry_strengths, list) and _retry_strengths:
                                _rebuilt: List[Dict[str, Any]] = []
                                for s in _retry_strengths[:6]:
                                    if isinstance(s, dict):
                                        point = (s.get("point") or s.get("text") or "").strip()
                                        laws = s.get("laws") or s.get("law") or []
                                        if not isinstance(laws, list):
                                            laws = [laws] if laws else []
                                        laws = [str(l).strip() for l in laws if l][:5]
                                        if point:
                                            point = re.sub(
                                                r"\bdocuments\b",
                                                "annexures",
                                                re.sub(
                                                    r"\bdocument\b",
                                                    "annexure",
                                                    point,
                                                    flags=re.IGNORECASE,
                                                ),
                                                flags=re.IGNORECASE,
                                            )
                                            point = _strip_strength_point_statute_boilerplate(
                                                point
                                            )
                                            if point:
                                                _rebuilt.append(
                                                    {"point": point, "laws": laws}
                                                )
                                if len(_rebuilt) >= 2:
                                    logger.success(
                                        "Strengths retry recovered {} item(s) for workspace {}",
                                        len(_rebuilt),
                                        workspace_id[:12] if workspace_id else "",
                                    )
                                    strengths = _rebuilt
                                else:
                                    logger.warning(
                                        "Strengths retry only produced {} item(s) for "
                                        "workspace {}; keeping original list",
                                        len(_rebuilt),
                                        workspace_id[:12] if workspace_id else "",
                                    )
                except Exception as _sr_err:
                    logger.warning(
                        "Strengths retry failed (non-fatal): {}", _sr_err
                    )

            weaknesses = data.get("weaknesses") or []
            # Coerce off-schema shapes (the model sometimes returns
            # `{"point": ..., "laws": [...]}` here, which would otherwise
            # render as a raw Python dict literal in the FE panel).
            weaknesses = _coerce_analysis_text_list(weaknesses)[:8]
            weaknesses = [
                re.sub(
                    r"\bdocuments\b",
                    "annexures",
                    re.sub(r"\bdocument\b", "annexure", x, flags=re.IGNORECASE),
                    flags=re.IGNORECASE,
                )
                for x in weaknesses
            ]

            risks = data.get("risks") or []
            risks = _coerce_analysis_text_list(risks)[:8]
            risks = [
                re.sub(
                    r"\bdocuments\b",
                    "annexures",
                    re.sub(r"\bdocument\b", "annexure", x, flags=re.IGNORECASE),
                    flags=re.IGNORECASE,
                )
                for x in risks
            ]

            relevant_jurisdiction = (
                data.get("relevantJurisdiction")
                or data.get("relevant_jurisdiction")
                or data.get("jurisdiction")
                or []
            )
            relevant_jurisdiction = _coerce_analysis_text_list(relevant_jurisdiction)[
                :6
            ]
            relevant_jurisdiction = await self._ensure_structured_relevant_jurisdiction(
                relevant_jurisdiction,
                workspace_role=workspace_role,
                case_summary=current_case_summary,
                strategic_objective=current_strategic_objective,
                document_excerpt=combined_document_content,
            )

            # Final guardrail for repeatedly-empty critical sections:
            # force a focused regeneration and, if still empty, apply deterministic defaults.
            must_fill = []
            if len(weaknesses) == 0:
                must_fill.append("weaknesses")
            if len(risks) == 0:
                must_fill.append("risks")
            if len(relevant_jurisdiction) == 0:
                must_fill.append("relevantJurisdiction")
            if must_fill:
                try:
                    force_tail_prompt = (
                        """Return ONLY one valid JSON object with keys:
weaknesses, risks, relevantJurisdiction

Context:
- Role: {}
- Case summary: {}
- Strategic objective: {}

Documents:
{}

Rules:
- Use only annexure facts; do not invent.
- weaknesses: provide 1-3 specific vulnerability points.
- risks: provide 1-3 concrete procedural/legal risks.
- relevantJurisdiction: provide 1-3 likely forums/courts/authorities.
- If evidence is weak, still provide best-effort concise entries from available facts (do not return empty arrays).
- Output JSON only.
"""
                    ).format(
                        role,
                        current_case_summary,
                        current_strategic_objective,
                        combined_document_content,
                    )
                    force_raw = await gemini_client.generate(
                        prompt=force_tail_prompt,
                        system_instruction=get_structured_document_feature_system_instruction(
                            workspace_role
                        ),
                        temperature=0.0,
                        max_tokens=12000,
                        response_mime_type="application/json",
                    )
                    force_obj = json.loads((force_raw or "").strip())
                    if isinstance(force_obj, dict):
                        if not weaknesses and isinstance(force_obj.get("weaknesses"), list):
                            weaknesses = _coerce_analysis_text_list(
                                force_obj.get("weaknesses")
                            )[:8]
                        if not risks and isinstance(force_obj.get("risks"), list):
                            risks = _coerce_analysis_text_list(
                                force_obj.get("risks")
                            )[:8]
                        if (not relevant_jurisdiction) and isinstance(
                            force_obj.get("relevantJurisdiction"), list
                        ):
                            relevant_jurisdiction = _coerce_analysis_text_list(
                                force_obj.get("relevantJurisdiction")
                            )[:6]
                    logger.info(
                        "Forced analysis tail regeneration attempted for: {}",
                        ", ".join(must_fill),
                    )
                except Exception as force_tail_err:
                    logger.warning(
                        "Forced tail regeneration failed: {}",
                        force_tail_err,
                    )

            if not weaknesses:
                weaknesses = [
                    "Current annexures do not yet establish complete evidence support for every pleaded point."
                ]
            if not risks:
                risks = [
                    "On current annexures, adverse inference risk may arise if supporting records remain unfiled."
                ]
            if not relevant_jurisdiction:
                relevant_jurisdiction = [
                    "Appropriate forum to be confirmed from territorial and subject-matter nexus in annexures."
                ]

            raw_score = data.get("caseResolutionScore")
            if raw_score is None:
                raw_score = data.get("case_resolution_score")
            # Robust parse: accept int/float, and pull the first integer out of
            # strings like "72", "72%", "Approximately 70%". None when missing
            # or unparseable.
            if isinstance(raw_score, bool):
                parsed_score: Optional[int] = None
            elif isinstance(raw_score, (int, float)):
                parsed_score = int(raw_score)
            elif isinstance(raw_score, str):
                _score_match = re.search(r"\d+", raw_score)
                parsed_score = int(_score_match.group()) if _score_match else None
            else:
                parsed_score = None
            # Cap at 95% when Missing Info / Risks / Weaknesses still have items (100% only when all empty)
            max_allowed = self._analysis_report_max_allowed_score(
                missing_info=missing_info, weaknesses=weaknesses, risks=risks
            )
            if parsed_score is None or parsed_score <= 0:
                # The model omitted / zeroed / mangled the score, or its JSON was
                # truncated. This report always carries real content here
                # (weaknesses/risks are force-filled just above), so it must
                # never display 0% — derive a score from the documented logic.
                case_resolution_score = self._derive_case_resolution_score(
                    missing_info=missing_info,
                    weaknesses=weaknesses,
                    risks=risks,
                    max_allowed=max_allowed,
                )
                logger.warning(
                    "Analysis report: model returned invalid/zero score ({!r}); "
                    "derived {}% from content",
                    raw_score,
                    case_resolution_score,
                )
            else:
                case_resolution_score = max(0, min(parsed_score, max_allowed))

            quality_score_explanation = (
                data.get("qualityScoreExplanation")
                or data.get("quality_score_explanation")
                or ""
            )
            if not isinstance(quality_score_explanation, str):
                quality_score_explanation = str(quality_score_explanation or "").strip()
            quality_score_explanation = await self._ensure_quality_score_explanation(
                workspace_role=workspace_role,
                case_resolution_score=case_resolution_score,
                missing_info=missing_info,
                weaknesses=weaknesses,
                risks=risks,
                strengths=strengths,
                existing=quality_score_explanation,
            )
            quality_score_explanation = re.sub(
                r"\bdocuments\b",
                "annexures",
                re.sub(
                    r"\bdocument\b",
                    "annexure",
                    quality_score_explanation,
                    flags=re.IGNORECASE,
                ),
                flags=re.IGNORECASE,
            )

            report_quality_raw = (
                data.get("reportQualityScoreExplanation")
                or data.get("report_quality_score_explanation")
                or ""
            )
            if not isinstance(report_quality_raw, str):
                report_quality_raw = str(report_quality_raw or "").strip()
            else:
                report_quality_raw = report_quality_raw.strip()

            report_quality_score_explanation = (
                self._sanitize_report_quality_score_explanation(report_quality_raw)
            )
            report_quality_score_explanation = re.sub(
                r"\bdocuments\b",
                "annexures",
                re.sub(
                    r"\bdocument\b",
                    "annexure",
                    report_quality_score_explanation,
                    flags=re.IGNORECASE,
                ),
                flags=re.IGNORECASE,
            )
            if not report_quality_score_explanation:
                report_quality_score_explanation = (
                    self._sanitize_report_quality_score_explanation(
                        quality_score_explanation
                    )
                )
            if not report_quality_score_explanation:
                report_quality_score_explanation = (
                    self._sanitize_report_quality_score_explanation(
                        self._derive_quality_score_explanation(
                            case_resolution_score,
                            missing_info,
                            weaknesses,
                            risks,
                            strengths,
                        )
                    )
                )

            pending_actions = (
                data.get("pendingActions") or data.get("pending_actions") or []
            )
            if not isinstance(pending_actions, list):
                pending_actions = [pending_actions] if pending_actions else []
            pending_actions = [str(x).strip() for x in pending_actions if x][:6]

            next_steps = data.get("nextSteps") or data.get("next_steps") or []
            if not isinstance(next_steps, list):
                next_steps = [next_steps] if next_steps else []
            next_steps = [str(x).strip() for x in next_steps if x][:6]

            suggested_drafts = (
                data.get("suggestedDrafts") or data.get("suggested_drafts") or []
            )
            if not isinstance(suggested_drafts, list):
                suggested_drafts = [suggested_drafts] if suggested_drafts else []
            suggested_drafts = [str(x).strip() for x in suggested_drafts if x][:6]

            # Deterministic year backstop: snap any FULL date whose year is a
            # hallucinated legacy year (e.g. 2024 on a year-less transcript) to
            # the current year, across every free-text field. Only full dates
            # are touched — statute years ("IT Act, 2000", "BNS, 2023") and
            # amounts are preserved; an explicit source year is kept. Mirrors the
            # list_of_dates guard so all panels agree on year resolution.
            def _snap(_t):
                return snap_years_in_text(
                    _t, today_iso=today_str_ar, document_text=combined_document_content
                )

            missing_info = [_snap(x) for x in missing_info]
            for _s in strengths:
                if isinstance(_s, dict) and isinstance(_s.get("point"), str):
                    _s["point"] = _snap(_s["point"])
            weaknesses = [_snap(x) for x in weaknesses]
            risks = [_snap(x) for x in risks]
            quality_score_explanation = _snap(quality_score_explanation)
            report_quality_score_explanation = _snap(report_quality_score_explanation)

            payload = {
                "missingInfo": missing_info,
                "strengths": strengths,
                "weaknesses": weaknesses,
                "risks": risks,
                "relevantJurisdiction": relevant_jurisdiction,
                "caseResolutionScore": case_resolution_score,
                "qualityScoreExplanation": quality_score_explanation,
                "reportQualityScoreExplanation": report_quality_score_explanation,
                "pendingActions": pending_actions,
                "nextSteps": next_steps,
                "suggestedDrafts": suggested_drafts,
            }

            # Detailed analysis report logs for debugging
            logger.info("=" * 80)
            logger.info("📊 ANALYSIS REPORT PAYLOAD (AI-GENERATED)")
            logger.info("=" * 80)
            logger.info("📈 caseResolutionScore: {}%", case_resolution_score)
            if quality_score_explanation:
                logger.info(
                    "📝 qualityScoreExplanation: {}",
                    quality_score_explanation[:200]
                    + ("..." if len(quality_score_explanation) > 200 else ""),
                )
            if report_quality_score_explanation:
                logger.info(
                    "📝 reportQualityScoreExplanation: {}",
                    report_quality_score_explanation[:200]
                    + (
                        "..."
                        if len(report_quality_score_explanation) > 200
                        else ""
                    ),
                )
            logger.info("")
            logger.info("❓ MISSING INFO ({} items):", len(missing_info))
            for idx, item in enumerate(missing_info, 1):
                logger.info("  {}. {}", idx, item)
            logger.info("")
            logger.info("✅ STRENGTHS ({} items):", len(strengths))
            for idx, s in enumerate(strengths, 1):
                logger.info(
                    "  {}. {} | laws: {}", idx, s.get("point", ""), s.get("laws", [])
                )
            logger.info("")
            logger.info("⚠️ WEAKNESSES ({} items):", len(weaknesses))
            for idx, w in enumerate(weaknesses, 1):
                logger.info("  {}. {}", idx, w)
            logger.info("")
            logger.info("🔴 RISKS ({} items):", len(risks))
            for idx, r in enumerate(risks, 1):
                logger.info("  {}. {}", idx, r)
            logger.info("")
            logger.info(
                "📍 RELEVANT JURISDICTION ({} items):", len(relevant_jurisdiction)
            )
            for idx, j in enumerate(relevant_jurisdiction, 1):
                logger.info("  {}. {}", idx, j)
            logger.info("")
            logger.info("📋 PENDING ACTIONS ({} items):", len(pending_actions))
            for idx, a in enumerate(pending_actions, 1):
                logger.info("  {}. {}", idx, a)
            logger.info("")
            logger.info("🚀 NEXT STEPS ({} items):", len(next_steps))
            for idx, n in enumerate(next_steps, 1):
                logger.info("  {}. {}", idx, n)
            logger.info("")
            logger.info("📄 SUGGESTED DRAFTS ({} items):", len(suggested_drafts))
            for idx, d in enumerate(suggested_drafts, 1):
                logger.info("  {}. {}", idx, d)
            logger.info("=" * 80)

            logger.success(
                "✅ Analysis report generated: score={}, strengths={}, missingInfo={}",
                case_resolution_score,
                len(strengths),
                len(missing_info),
            )
            # Clamp "Annexure N" references in the freshly generated payload
            # against the real upload count so a single-doc workspace never
            # sees hallucinated Annexure 2 / Annexure 3 / ... in the first
            # emit. Downstream merge and feedback paths re-apply the clamp
            # via _normalize_analysis_report_lists, so this is defence in
            # depth.
            self._normalize_analysis_report_lists(payload, max_annexure=num_docs)
            return payload

        except Exception as e:
            logger.error("Error generating analysis report: {}", e)
            logger.exception(e)
            return self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )

    async def _ensure_structured_relevant_jurisdiction(
        self,
        entries: List[str],
        *,
        workspace_role: str,
        case_summary: str,
        strategic_objective: str,
        document_excerpt: str,
    ) -> List[str]:
        """
        Ensure jurisdiction items are court/forum names only (no reasons).
        Target format examples:
        - "High Court of Delhi"
        - "District & Sessions Court, Patna"
        - "Supreme Court of India"
        """
        cleaned = [str(x).strip() for x in (entries or []) if str(x).strip()]
        if not cleaned:
            return []

        def _is_structured(v: str) -> bool:
            s = (v or "").strip()
            if not s:
                return False
            forum_re = re.compile(
                r"\b(supreme\s+court|high\s+court|district(?:\s*&\s*sessions)?\s+court|sessions\s+court|family\s+court|magistrate|tribunal|commission|consumer\s+commission|forum)\b",
                re.I,
            )
            return bool(forum_re.search(s))

        def _strip_reason(v: str) -> str:
            s = (v or "").strip()
            if not s:
                return s
            # Remove trailing parenthetical / dash / colon explanations if present.
            s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
            s = re.sub(r"\s*[-:]\s*.*$", "", s).strip()
            return s

        cleaned = [_strip_reason(v) for v in cleaned]
        cleaned = [v for v in cleaned if _is_structured(v)]
        if cleaned:
            return cleaned[:6]

        try:
            from app.ai.gemini_client import gemini_client

            prompt = ensure_structured_relevant_jurisdiction_prompt(case_summary=case_summary, cleaned=cleaned, document_excerpt=document_excerpt, strategic_objective=strategic_objective)
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.1,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            arr = parsed.get("relevantJurisdiction") if isinstance(parsed, dict) else []
            if not isinstance(arr, list):
                return cleaned[:6]
            out = [_strip_reason(str(x).strip()) for x in arr if str(x).strip()]
            out = [x for x in out if _is_structured(x)]
            return out[:6] if out else cleaned[:6]
        except Exception as e:
            logger.warning("Jurisdiction structure enforcement failed: {}", e)
            return cleaned[:6]

    def _analysis_report_fallback(self, role: str) -> Dict[str, Any]:
        """Return a minimal valid analysis report when AI fails or no docs."""
        return {
            "missingInfo": [],
            "strengths": [],
            "weaknesses": [],
            "risks": [],
            "relevantJurisdiction": [],
            "caseResolutionScore": 0,
            "qualityScoreExplanation": "",
            "reportQualityScoreExplanation": "",
            "pendingActions": [],
            "nextSteps": [],
            "suggestedDrafts": [],
        }

    def _analysis_report_max_allowed_score(
        self,
        missing_info: Optional[List[Any]] = None,
        weaknesses: Optional[List[Any]] = None,
        risks: Optional[List[Any]] = None,
        report: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Return max allowed caseResolutionScore (100 only when no missing info, risks, or weaknesses).
        If any of those sections have items, cap at 95% so 100% is not shown until user has addressed all.
        """
        if report is not None:
            missing_info = report.get("missingInfo") or report.get("missing_info") or []
            weaknesses = report.get("weaknesses") or []
            risks = report.get("risks") or []
        has_pending = bool(
            (missing_info and len(missing_info) > 0)
            or (weaknesses and len(weaknesses) > 0)
            or (risks and len(risks) > 0)
        )
        return (
            100
            if not has_pending
            else self._ANALYSIS_REPORT_MAX_SCORE_WITH_PENDING_ITEMS
        )

    def _derive_case_resolution_score(
        self,
        *,
        missing_info: Optional[List[Any]] = None,
        weaknesses: Optional[List[Any]] = None,
        risks: Optional[List[Any]] = None,
        max_allowed: Optional[int] = None,
    ) -> int:
        """
        Deterministic fallback score, used when the model omits / zeroes /
        returns an unparseable caseResolutionScore for a populated report.

        Mirrors the prompt's documented scoring logic (start from 100, then
        −5 per missingInfo, −5 per weakness, −3 per risk) and clamps the result
        between a floor (so an analysed case never reads 0%) and the
        pending-items cap.
        """
        if max_allowed is None:
            max_allowed = self._analysis_report_max_allowed_score(
                missing_info=missing_info, weaknesses=weaknesses, risks=risks
            )
        base = 100
        base -= 5 * len(missing_info or [])
        base -= 5 * len(weaknesses or [])
        base -= 3 * len(risks or [])
        return max(
            self._ANALYSIS_REPORT_DERIVED_SCORE_FLOOR, min(base, max_allowed)
        )

    def _derive_quality_score_explanation(
        self,
        case_resolution_score: int,
        missing_info: List[Any],
        weaknesses: List[Any],
        risks: List[Any],
        strengths: List[Any],
    ) -> str:
        """Deterministic explanation when the model omits qualityScoreExplanation or output was truncated."""
        nm = len(missing_info or [])
        nw = len(weaknesses or [])
        nr = len(risks or [])
        ns = len(strengths or [])
        lead = f"The current case resolution score is {case_resolution_score}%."
        if nm == 0 and nw == 0 and nr == 0:
            return (
                f"{lead} No missing critical information, factual weaknesses, or procedural risks were flagged "
                "in the annexures for this pass."
            )
        clauses = []
        if nm:
            clauses.append(f"{nm} critical information gap(s) noted in the annexures")
        if nw:
            clauses.append(f"{nw} factual weakness(es)")
        if nr:
            clauses.append(f"{nr} procedural or legal risk(s)")
        if len(clauses) == 1:
            why = clauses[0]
        else:
            why = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"
        second = f"The score reflects {why}, which reduces readiness relative to a fully complete case file."
        third = (
            f"The analysis still identifies {ns} strength(s) that support the file."
            if ns
            else ""
        )
        return " ".join(s for s in (lead, second, third) if s).strip()

    def _sanitize_report_quality_score_explanation(self, text: str) -> str:
        """Report-facing score explanation: remove disallowed words (legal, respondent) per product rules."""
        if not (text or "").strip():
            return ""
        t = (text or "").strip()
        t = re.sub(r"\brespondents?\b", "parties", t, flags=re.I)
        t = re.sub(r"\s+or\s+legal\s+", " or ", t, flags=re.I)
        t = re.sub(r"\s+and\s+legal\s+", " and ", t, flags=re.I)
        t = re.sub(r"\blegal\b", "", t, flags=re.I)
        t = re.sub(r"\s{2,}", " ", t)
        t = re.sub(r"\s+([.,;:])", r"\1", t)
        return t.strip()

    async def _ensure_quality_score_explanation(
        self,
        workspace_role: str,
        case_resolution_score: int,
        missing_info: List[Any],
        weaknesses: List[Any],
        risks: List[Any],
        strengths: List[Any],
        existing: str,
    ) -> str:
        """Fill qualityScoreExplanation via model output, short LLM pass, or deterministic text."""
        if (existing or "").strip():
            return (existing or "").strip()
        try:
            import json as _json

            from app.ai.gemini_client import gemini_client

            strength_pts = []
            for s in (strengths or [])[:6]:
                if isinstance(s, dict):
                    strength_pts.append((s.get("point") or s.get("text") or "")[:200])
                elif s:
                    strength_pts.append(str(s)[:200])
            summary = _json.dumps(
                {
                    "caseResolutionScore": case_resolution_score,
                    "missingInfo": [str(x)[:200] for x in (missing_info or [])[:8]],
                    "weaknesses": [str(x)[:200] for x in (weaknesses or [])[:8]],
                    "risks": [str(x)[:200] for x in (risks or [])[:8]],
                    "strengthPoints": [x for x in strength_pts if x],
                },
                ensure_ascii=False,
            )
            prompt = (
                ensure_quality_score_explanation_prompt(case_resolution_score=case_resolution_score, summary=summary)
            )
            txt = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    workspace_role
                ),
                temperature=0.35,
                max_tokens=12000,
            )
            txt = (txt or "").strip()
            # Unwrap if the model returned JSON instead of plain text
            # e.g. {"analysis": "..."} or {"explanation": "..."} or {"qualityScoreExplanation": "..."}
            if txt.startswith("{"):
                try:
                    parsed = _json.loads(txt)
                    if isinstance(parsed, dict):
                        txt = (
                            parsed.get("analysis")
                            or parsed.get("explanation")
                            or parsed.get("qualityScoreExplanation")
                            or parsed.get("quality_score_explanation")
                            or parsed.get("text")
                            or next(
                                (v for v in parsed.values() if isinstance(v, str) and len(v) >= 40),
                                "",
                            )
                        ).strip()
                except (_json.JSONDecodeError, Exception):
                    pass
            # Strip markdown code fences if present
            if "```" in txt:
                _fence_match = re.search(r"```(?:\w+)?\s*([\s\S]*?)```", txt)
                if _fence_match:
                    txt = _fence_match.group(1).strip()
            if len(txt) >= 40:
                return txt
        except Exception as e:
            logger.warning("Quality score explanation (LLM fallback) failed: {}", e)
        return self._derive_quality_score_explanation(
            case_resolution_score,
            missing_info,
            weaknesses,
            risks,
            strengths,
        )

    def _get_annexure_count(self, workspace_id: str) -> int:
        """
        Return the number of annexures the user has actually uploaded for a
        workspace. Backs the analysis-report / strategy clamp so the model
        cannot emit "Annexure N" where N exceeds the real upload count.
        Falls back to 0 when the workspace has no cached assessments yet
        (e.g. cold reconnect with no documents).
        """
        if not workspace_id:
            return 0
        cached = self._workspace_document_assessments_payload.get(workspace_id)
        if not isinstance(cached, dict):
            return 0
        inner = cached.get("payload") or {}
        assessments = inner.get("documentAssessments")
        if isinstance(assessments, list):
            return len(assessments)
        return 0

    def _normalize_analysis_report_lists(
        self,
        report: Optional[Dict[str, Any]],
        max_annexure: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Defense-in-depth sanitizer: ensures the user-facing list fields of an
        analysis-report payload contain only plain strings, never dict reprs.

        The model occasionally returns ``risks`` / ``weaknesses`` /
        ``missingInfo`` / etc. as dicts (the same ``{"point": ..., "laws": [...]}``
        shape used for ``strengths``). Without this sanitizer those entries
        would render in the FE as Python dict literals like
        ``"{'point': '...', 'laws': [...]}"``. Strengths are intentionally
        left untouched because they are a typed `List[Dict{point, laws}]`.

        When ``max_annexure`` is provided, also clamps any "Annexure N"
        reference in the user-facing fields down to the highest valid index,
        so the model cannot emit "Annexure 2 / 3 / N" when fewer documents
        are actually uploaded. ``relevantJurisdiction`` is exempt — the
        prompt enforces a forum-only format there.

        Mutates the dict in place and returns it for convenience. Returns
        the input unchanged when it is not a dict.
        """
        if not isinstance(report, dict):
            return report
        for key in (
            "missingInfo",
            "weaknesses",
            "risks",
            "relevantJurisdiction",
            "pendingActions",
            "nextSteps",
            "suggestedDrafts",
        ):
            if key in report:
                report[key] = _coerce_analysis_text_list(report.get(key))
        # `missing_info` snake_case alias is occasionally used as a fallback
        # source — clean it too if present.
        if "missing_info" in report:
            report["missing_info"] = _coerce_analysis_text_list(
                report.get("missing_info")
            )
        # Strengths: List[Dict{point, laws}]. We don't flatten it (the laws
        # list is meaningful) but we do strip stray "1. " / "2. " enumeration
        # prefixes from the `point` so a regenerated "1. Hairline fracture
        # documented" doesn't escape the prev-vs-new merge as different from
        # a previously-emitted "Hairline fracture documented".
        strengths = report.get("strengths")
        if isinstance(strengths, list):
            for s in strengths:
                if not isinstance(s, dict):
                    continue
                point = s.get("point")
                if isinstance(point, str) and point.strip():
                    s["point"] = _strip_enum_prefix(point.strip())

        if max_annexure is not None:
            for key in (
                "missingInfo",
                "missing_info",
                "weaknesses",
                "risks",
                "pendingActions",
                "nextSteps",
                "suggestedDrafts",
            ):
                values = report.get(key)
                if isinstance(values, list):
                    report[key] = [
                        _clamp_annexure_references(v, max_annexure) for v in values
                    ]
            for scalar_key in (
                "qualityScoreExplanation",
                "reportQualityScoreExplanation",
            ):
                if scalar_key in report:
                    report[scalar_key] = _clamp_annexure_references(
                        report.get(scalar_key), max_annexure
                    )
            if isinstance(strengths, list):
                for s in strengths:
                    if not isinstance(s, dict):
                        continue
                    point = s.get("point")
                    if isinstance(point, str) and point:
                        s["point"] = _clamp_annexure_references(point, max_annexure)
        # Final safety net: strip the application's own brand/platform name from
        # every string in the report (central chokepoint — every analysis-report
        # generation, sync, marker-removal, and surgical-edit path runs through
        # here before caching/emitting).
        report = scrub_app_brand(report)
        return report

    def _record_rejected_analysis_item(
        self,
        workspace_id: str,
        section: str,
        rejected_value: str,
    ) -> None:
        """
        Persist a rejected missingInfo / riskOrWeakness value into the
        per-workspace rejection memory so that subsequent flows (notably the
        objective-change incremental refresh) do not silently re-introduce
        items the user has already dismissed.
        """
        rv = (rejected_value or "").strip()
        if not workspace_id or not rv:
            return
        if section not in ("missingInfo", "riskOrWeakness"):
            return
        try:
            current = self._workspace_rejected_analysis_items.get(workspace_id) or {}
            if not isinstance(current, dict):
                current = {}
            bucket = current.get(section)
            if not isinstance(bucket, list):
                bucket = []
            for existing in bucket:
                if not isinstance(existing, str):
                    continue
                if self._match_rejected_value(existing, rv) or self._match_rejected_value(
                    rv, existing
                ):
                    return
            bucket.append(rv)
            current[section] = bucket[-50:]
            self._workspace_rejected_analysis_items[workspace_id] = current
        except Exception as e:
            logger.debug(
                "Failed to record rejected analysis item (workspace={}, section={}): {}",
                workspace_id,
                section,
                e,
            )

    def _is_previously_rejected_analysis_item(
        self,
        workspace_id: str,
        section: str,
        candidate: Any,
    ) -> bool:
        """
        Return True when `candidate` semantically matches any value the user
        has previously rejected for this workspace+section.
        """
        if not workspace_id or section not in ("missingInfo", "riskOrWeakness"):
            return False
        try:
            current = self._workspace_rejected_analysis_items.get(workspace_id) or {}
            if not isinstance(current, dict):
                return False
            bucket = current.get(section)
            if not isinstance(bucket, list) or not bucket:
                return False
            for prev_rv in bucket:
                if not isinstance(prev_rv, str) or not prev_rv.strip():
                    continue
                if self._match_rejected_value(candidate, prev_rv):
                    return True
        except Exception:
            return False
        return False

    def _match_rejected_value(self, item: Any, rejected_value: str) -> bool:
        """Return True if item matches/addresses rejectedValue using robust text overlap."""
        rv = (rejected_value or "").strip()
        if not rv:
            return False

        item_text = ""
        if isinstance(item, str):
            item_text = item.strip()
        elif isinstance(item, dict) and "point" in item:
            item_text = (item.get("point") or "").strip()
        if not item_text:
            return False

        # Fast-path exact/containment checks.
        if item_text == rv or rv in item_text or item_text in rv:
            return True

        def _norm(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r"[^a-z0-9\s]", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s

        stop_words = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "for",
            "of",
            "to",
            "in",
            "on",
            "with",
            "by",
            "from",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "that",
            "this",
            "these",
            "those",
            "it",
            "its",
            "their",
            "his",
            "her",
            "our",
            "your",
            "at",
            "as",
            "can",
            "could",
            "should",
            "would",
            "about",
            "into",
            "also",
            "than",
            "then",
            "over",
            "under",
        }

        def _tokens(s: str) -> Set[str]:
            out: Set[str] = set()
            for tok in _norm(s).split():
                if len(tok) <= 2 or tok in stop_words:
                    continue
                out.add(tok)
            return out

        item_tokens = _tokens(item_text)
        rv_tokens = _tokens(rv)
        if not item_tokens or not rv_tokens:
            return False

        overlap = item_tokens.intersection(rv_tokens)
        if not overlap:
            return False

        # Strong match: at least 2 shared informative tokens, or most of item intent covered.
        if len(overlap) >= 2:
            return True
        return len(overlap) / max(1, len(item_tokens)) >= 0.6
        return False

    def _parse_bullets_from_block(self, block: str) -> List[str]:
        """Parse a text block into a list of bullet items."""
        import re

        if not block:
            return []

        b = block.strip()
        # Remove leading label separators sometimes included in copied text
        b = re.sub(r"^\s*[:\-–]\s*", "", b)

        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if not lines:
            return []

        items: List[str] = []
        for ln in lines:
            # Remove common bullet/number prefixes
            ln = re.sub(r"^(?:[-*•]\s+)", "", ln)
            ln = re.sub(r"^\d+\s*[.)]\s+", "", ln)
            ln = ln.strip()
            if not ln:
                continue
            # Stop if the next section header accidentally got included
            if re.match(
                r"^(?:RESOLVED_MISSING_INFO|REMOVED_WEAKNESSES|REMOVED_RISKS|STRATEGIC_OBJECTIVE_UPDATE)\b",
                ln,
                flags=re.I,
            ):
                continue
            items.append(ln)

        # If the block seems to be comma/semicolon separated (no clear bullet lines), split again.
        if len(items) <= 1 and ("," in b or ";" in b) and "\n" not in b:
            parts = [p.strip() for p in re.split(r"[;,]", b) if p.strip()]
            items = parts

        # De-duplicate while preserving order
        seen = set()
        out: List[str] = []
        for it in items:
            key = it.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    def _extract_intelligence_chat_panel_updates(
        self,
        combined_text: str,
    ) -> Dict[str, Any]:
        """
        Extract user-requested panel mutations from chat text.

        Supported explicit markers:
        - STRATEGIC_OBJECTIVE_UPDATE:
        - RESOLVED_MISSING_INFO:
        - REMOVED_WEAKNESSES:
        - REMOVED_RISKS:
        """
        import re

        text = combined_text or ""
        if not text.strip():
            return {}

        # Use case-insensitive, dotall captures. Stop at the next known marker.
        def _capture(pattern: str) -> Optional[str]:
            m = re.search(pattern, text, flags=re.I | re.S)
            return m.group(1).strip() if (m and m.group(1)) else None

        # Strict marker-only parsing to avoid false positives from normal draft/chat text.
        objective_block = _capture(
            r"(?:STRATEGIC_OBJECTIVE_UPDATE\s*[:=\-])\s*(.*?)\s*(?=(?:RESOLVED_MISSING_INFO|REMOVED_WEAKNESSES|REMOVED_RISKS)\b|$)"
        )
        missing_block = _capture(
            r"(?:RESOLVED_MISSING_INFO\s*[:=\-])\s*(.*?)\s*(?=(?:REMOVED_WEAKNESSES|REMOVED_RISKS|STRATEGIC_OBJECTIVE_UPDATE|$))"
        )
        weaknesses_block = _capture(
            r"(?:REMOVED_WEAKNESSES\s*[:=\-])\s*(.*?)\s*(?=(?:REMOVED_RISKS|RESOLVED_MISSING_INFO|STRATEGIC_OBJECTIVE_UPDATE|$))"
        )
        risks_block = _capture(
            r"(?:REMOVED_RISKS\s*[:=\-])\s*(.*?)\s*(?=(?:REMOVED_WEAKNESSES|RESOLVED_MISSING_INFO|STRATEGIC_OBJECTIVE_UPDATE|$))"
        )

        updates: Dict[str, Any] = {}
        if objective_block:
            updates["strategicObjective"] = objective_block.strip()
        if missing_block:
            updates["missingInfo"] = self._parse_bullets_from_block(missing_block)
        if weaknesses_block:
            updates["weaknesses"] = self._parse_bullets_from_block(weaknesses_block)
        if risks_block:
            updates["risks"] = self._parse_bullets_from_block(risks_block)

        # Require at least one meaningful field to treat as an update request.
        if not updates:
            return {}

        # Remove empty lists
        for k in ("missingInfo", "weaknesses", "risks"):
            if k in updates and isinstance(updates[k], list) and len(updates[k]) == 0:
                updates.pop(k, None)

        return updates

    def _strip_panel_update_markers_from_text(self, text: str) -> str:
        """Remove control-marker blocks from assistant text before user-facing emit."""
        s = (text or "").strip()
        if not s:
            return s
        s = re.sub(
            r"(?is)\b(?:RESOLVED_MISSING_INFO|REMOVED_WEAKNESSES|REMOVED_RISKS|STRATEGIC_OBJECTIVE_UPDATE)\s*[:=\-].*?(?=\n\s*\n|$)",
            "",
            s,
        )
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
        return s

    def _schedule_analysis_report_refresh_after_marker_miss(
        self,
        workspace_id: str,
        user_id: str,
        *,
        log_context: str = "",
    ) -> None:
        """Regenerate analysis report when marker-based item matching found no removals."""
        if not workspace_id or not user_id:
            return
        if workspace_id in self._analysis_refresh_inflight:
            logger.info(
                "Analysis refresh already inflight after marker miss (workspace={})",
                workspace_id[:12],
            )
            return
        self._analysis_refresh_inflight.add(workspace_id)

        async def _runner() -> None:
            try:
                role = await self._restore_workspace_role(
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
                try:
                    emitted = await asyncio.wait_for(
                        self._emit_analysis_report(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=(role or "ADJUDICATOR"),
                            batch_result=None,
                            is_update=True,
                            log_context=log_context or " (marker miss refresh)",
                        ),
                        timeout=35.0,
                    )
                    if not emitted:
                        logger.warning(
                            "Analysis refresh completed without emit (workspace={}); sending cached analysis payload",
                            workspace_id[:12],
                        )
                        cached_report = self._workspace_analysis_payload.get(workspace_id)
                        if self._socket_client and isinstance(cached_report, dict):
                            fallback_payload = dict(cached_report)
                            fallback_payload["timestamp"] = (
                                datetime.utcnow().isoformat() + "Z"
                            )
                            await self._emit_document_feature(
                                feature_type="analysis_report",
                                workspace_id=workspace_id,
                                user_id=user_id,
                                payload=fallback_payload,
                                usage=None,
                            )
                            logger.info(
                                "Cached analysis report emitted after no-emit refresh (workspace={})",
                                workspace_id[:12],
                            )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Analysis refresh timed out after marker miss (workspace={}); emitting cached analysis payload to clear loader",
                        workspace_id[:12],
                    )
                    cached_report = self._workspace_analysis_payload.get(workspace_id)
                    if self._socket_client and isinstance(cached_report, dict):
                        fallback_payload = dict(cached_report)
                        fallback_payload["timestamp"] = (
                            datetime.utcnow().isoformat() + "Z"
                        )
                        await self._emit_document_feature(
                            feature_type="analysis_report",
                            workspace_id=workspace_id,
                            user_id=user_id,
                            payload=fallback_payload,
                            usage=None,
                        )
                        logger.info(
                            "Cached analysis report emitted after timeout (workspace={})",
                            workspace_id[:12],
                        )
            except Exception as err:
                logger.warning(
                    "Analysis refresh after marker miss failed (workspace={}): {}",
                    workspace_id[:12],
                    err,
                )
            finally:
                self._analysis_refresh_inflight.discard(workspace_id)

        try:
            asyncio.create_task(_runner())
            logger.info(
                "Scheduled analysis report refresh after marker miss (workspace={})",
                workspace_id[:12],
            )
        except RuntimeError as err:
            self._analysis_refresh_inflight.discard(workspace_id)
            logger.warning(
                "Could not schedule marker-miss analysis refresh (workspace={}): {}",
                workspace_id[:12],
                err,
            )

    async def _try_apply_intelligence_chat_panel_updates(
        self,
        workspace_id: str,
        user_id: str,
        combined_text: str,
        *,
        ignore_strategic_objective: bool = False,
    ) -> Dict[str, Any]:
        """
        If the chat message includes user panel update markers, apply them to:
        - strategy_objective (only if not already handled by objective regeneration in STRATEGY_OBJECTIVE chat)
        - analysis_report.missingInfo / weaknesses / risks
        and re-emit updated panels to backend.
        """
        if not self._socket_client:
            return {"applied": False}

        updates = self._extract_intelligence_chat_panel_updates(combined_text)
        if ignore_strategic_objective:
            updates.pop("strategicObjective", None)
        if not updates:
            return {"applied": False}

        applied_any = False
        response_parts: List[str] = []

        # 1) Strategy objective: raw text update + emit when regen did not run (e.g. no case summary yet)
        objective_text = updates.get("strategicObjective")
        if objective_text and not ignore_strategic_objective:
            current_strategy = self._workspace_strategy_payload.get(workspace_id)
            if isinstance(current_strategy, dict):
                current_strategy = dict(current_strategy)
                current_strategy["strategicObjective"] = objective_text
                current_strategy["timestamp"] = datetime.utcnow().isoformat()
                self._workspace_strategy_payload[workspace_id] = current_strategy
                applied_any = True
                await self._emit_document_feature(
                    feature_type="strategy_objective",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=current_strategy,
                    usage=None,
                )
                response_parts.append("Strategy objective text updated")

        # 2) Analysis report updates
        missing_items = updates.get("missingInfo") or []
        weakness_items = updates.get("weaknesses") or []
        risk_items = updates.get("risks") or []

        if missing_items or weakness_items or risk_items:
            # Analysis-only refresh hint so FE can show loader specifically for Analysis Report.
            await self._emit_workspace_refresh(
                workspace_id=workspace_id,
                user_id=user_id,
                reason="analysis_report_update_started",
                feature_types=["analysis_report"],
            )
            current_report = self._workspace_analysis_payload.get(workspace_id)
            if isinstance(current_report, dict):
                removed_missing = 0
                removed_weak = 0
                removed_risk = 0

                # missingInfo
                missing_list_key = (
                    "missingInfo" if "missingInfo" in current_report else "missing_info"
                )
                current_missing = current_report.get(missing_list_key) or []
                if isinstance(current_missing, list):
                    new_missing = [
                        x
                        for x in current_missing
                        if not any(
                            self._match_rejected_value(x, v) for v in missing_items
                        )
                    ]
                    removed_missing = len(current_missing) - len(new_missing)
                    current_report[missing_list_key] = new_missing

                # weaknesses
                current_weak = current_report.get("weaknesses") or []
                if isinstance(current_weak, list):
                    new_weak = [
                        x
                        for x in current_weak
                        if not any(
                            self._match_rejected_value(x, v) for v in weakness_items
                        )
                    ]
                    removed_weak = len(current_weak) - len(new_weak)
                    current_report["weaknesses"] = new_weak

                # risks
                current_risk = current_report.get("risks") or []
                if isinstance(current_risk, list):
                    new_risk = [
                        x
                        for x in current_risk
                        if not any(self._match_rejected_value(x, v) for v in risk_items)
                    ]
                    removed_risk = len(current_risk) - len(new_risk)
                    current_report["risks"] = new_risk

                removed_any = (removed_missing + removed_weak + removed_risk) > 0

                if removed_any:
                    old_score = int(
                        current_report.get("caseResolutionScore")
                        or current_report.get("case_resolution_score")
                        or 0
                    )
                    delta = min(
                        5, max(0, removed_missing * 2 + removed_weak + removed_risk)
                    )
                    if delta < 1:
                        delta = 1

                    # Cap based on pending items
                    new_max_allowed = self._analysis_report_max_allowed_score(
                        report=current_report
                    )
                    new_score = min(new_max_allowed, old_score + delta)
                    current_report["caseResolutionScore"] = new_score

                    missing_now = (
                        current_report.get("missingInfo")
                        or current_report.get("missing_info")
                        or []
                    )
                    weak_now = current_report.get("weaknesses") or []
                    risk_now = current_report.get("risks") or []
                    strengths_now = current_report.get("strengths") or []
                    base_explanation = self._derive_quality_score_explanation(
                        case_resolution_score=new_score,
                        missing_info=missing_now if isinstance(missing_now, list) else [],
                        weaknesses=weak_now if isinstance(weak_now, list) else [],
                        risks=risk_now if isinstance(risk_now, list) else [],
                        strengths=strengths_now if isinstance(strengths_now, list) else [],
                    )
                    note = (
                        f" Lawyer-provided updates resolved {removed_missing} missing information item(s), "
                        f"{removed_weak} weakness(es), and {removed_risk} risk(s)."
                    )
                    current_report["qualityScoreExplanation"] = (
                        f"{base_explanation} {note}".strip()
                    )
                    current_report["reportQualityScoreExplanation"] = (
                        self._sanitize_report_quality_score_explanation(
                            f"{base_explanation} {note}".strip()
                        )
                    ).strip()
                # Always update timestamp so frontend receives a fresh analysis payload
                # even when marker text does not match and list contents remain unchanged.
                current_report["timestamp"] = datetime.utcnow().isoformat() + "Z"

                self._workspace_analysis_payload[workspace_id] = current_report
                await self._emit_document_feature(
                    feature_type="analysis_report",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=current_report,
                    usage=None,
                )
                applied_any = True
                response_parts.append(
                    "Analysis report updated with revised quality score"
                )
                if removed_missing == 0 and removed_weak == 0 and removed_risk == 0:
                    logger.info(
                        "Analysis update markers found, but no matching items removed (workspace={})",
                        workspace_id[:12] if workspace_id else "",
                    )
                    self._schedule_analysis_report_refresh_after_marker_miss(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        log_context=" (after missing-info marker miss)",
                    )
                    response_parts.append(
                        "Refreshing analysis report using your newly provided details"
                    )

        return {
            "applied": applied_any,
            "response_text": (
                "; ".join(response_parts) + "."
                if response_parts
                else "Workspace updated."
            ),
        }

    async def _compute_rejection_score_delta(
        self,
        current_score: int,
        section: str,
        rejected_value: str,
        report: Dict[str, Any],
    ) -> Tuple[bool, int, str]:
        """
        Ask AI to (a) decide if the user's rejection is VALID — i.e. the rejected
        item is genuinely not relevant to this specific case — and (b) compute a
        score INCREASE (1-5) when valid. The system will ADD the returned delta
        to the current case resolution score.

        - Valid rejection (item truly not relevant) → isValidRejection=true,
          scoreIncrease 1-5.
        - Invalid rejection (item IS relevant; user is dismissing a real gap) →
          isValidRejection=false, scoreIncrease=0.

        Returns (is_valid_rejection, delta, reason). On AI failure returns
        (True, default_bump, generic reason) so the legacy "always bump" behaviour
        is preserved when Gemini is unavailable.
        """
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            if not gemini_client.model:
                return (
                    True,
                    self._ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT,
                    "Score adjusted to reflect user rejection.",
                )
            remaining_missing = len(
                report.get("missingInfo") or report.get("missing_info") or []
            )
            remaining_weak = len(report.get("weaknesses") or [])
            remaining_risks = len(report.get("risks") or [])

            case_summary = (
                report.get("caseSummary") or report.get("case_summary") or ""
            ).strip()
            report_summary = (
                report.get("reportCaseSummary")
                or report.get("report_case_summary")
                or ""
            ).strip()
            strategic_objective = (
                report.get("strategicObjective")
                or report.get("strategic_objective")
                or ""
            ).strip()

            # Build short context: other items in the same section so the model can judge relative impact
            section_items: List[str] = []
            if section == "missingInfo":
                for x in report.get("missingInfo") or report.get("missing_info") or []:
                    t = x.get("point", x) if isinstance(x, dict) else x
                    if isinstance(t, str) and t.strip():
                        section_items.append(t.strip()[:120])
            else:
                for x in (report.get("weaknesses") or []) + (report.get("risks") or []):
                    t = x.get("point", x) if isinstance(x, dict) else x
                    if isinstance(t, str) and t.strip():
                        section_items.append(t.strip()[:120])
            section_context = (
                "Other items in this section (for context): "
                + "; ".join(section_items[:8])
                if section_items
                else ""
            )
            # When this rejection leaves no items in any category, score can reach 100%; otherwise capped at 95%
            can_reach_100 = (
                remaining_missing == 0 and remaining_weak == 0 and remaining_risks == 0
            )
            prompt = compute_rejection_score_delta_prompt(can_reach_100=can_reach_100, case_summary=case_summary, current_score=current_score, rejected_value=rejected_value, remaining_missing=remaining_missing, remaining_risks=remaining_risks, remaining_weak=remaining_weak, report_summary=report_summary, section=section, section_context=section_context, strategic_objective=strategic_objective)
            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    None
                ),
                temperature=0.3,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            raw = (raw or "").strip()
            if not raw or raw.startswith("Error") or "Error generating" in raw[:50]:
                logger.warning(
                    "Rejection score delta: Gemini error or empty; using default. raw={}",
                    (raw or "")[:100],
                )
                return (
                    True,
                    self._ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT,
                    "Score adjusted to reflect user rejection.",
                )
            # Strip markdown code blocks if present
            if "```" in raw:
                match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
                if match:
                    raw = match.group(1).strip()
                else:
                    raw = re.sub(r"^```\w*\s*", "", raw)
                    raw = re.sub(r"\s*```\s*$", "", raw)
            obj: Optional[Dict[str, Any]] = None
            start = raw.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(raw[start : i + 1])
                            except json.JSONDecodeError:
                                obj = None
                            break

            def _coerce_bool(value: Any, default: bool) -> bool:
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return bool(value)
                if isinstance(value, str):
                    return value.strip().lower() in ("true", "yes", "1")
                return default

            if obj and isinstance(obj, dict) and "scoreIncrease" in obj:
                is_valid = _coerce_bool(
                    obj.get("isValidRejection", obj.get("is_valid_rejection")),
                    default=True,
                )
                delta = int(
                    obj.get("scoreIncrease", self._ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT)
                )
                if not is_valid:
                    delta = 0
                else:
                    delta = max(1, min(5, delta))  # Rejection bump 1-5 as per prompt
                    if current_score + delta > 100:
                        delta = max(0, 100 - current_score)
                reason = (
                    obj.get("reason") or "Score adjusted based on rejection priority."
                ).strip()
                logger.info(
                    "Rejection score delta: AI isValid={} delta={}, reason={}",
                    is_valid,
                    delta,
                    (reason or "")[:80],
                )
                return (is_valid, delta, reason[:200])
            # Regex fallback: extract scoreIncrease from raw text (e.g. malformed or truncated JSON)
            num_match = re.search(
                r'"scoreIncrease"\s*:\s*(\d+)|scoreIncrease"\s*:\s*(\d+)|\bscoreIncrease\s*:\s*(\d+)',
                raw,
            )
            if num_match:
                valid_match = re.search(
                    r'"isValidRejection"\s*:\s*(true|false)',
                    raw,
                    flags=re.IGNORECASE,
                )
                is_valid = (
                    valid_match.group(1).strip().lower() == "true"
                    if valid_match
                    else True
                )
                delta = int(
                    num_match.group(1) or num_match.group(2) or num_match.group(3) or 0
                )
                if not is_valid:
                    delta = 0
                else:
                    delta = max(1, min(5, delta))  # Rejection bump 1-5 as per prompt
                    if current_score + delta > 100:
                        delta = max(0, 100 - current_score)
                reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', raw)
                reason = (
                    reason_match.group(1).strip()
                    if reason_match
                    else "Score adjusted based on rejection priority."
                )[:200]
                logger.info(
                    "Rejection score delta: parsed via regex isValid={} delta={}",
                    is_valid,
                    delta,
                )
                return (is_valid, delta, reason)
            logger.warning(
                "Rejection score delta: could not parse response; using default. raw={}",
                raw[:200],
            )
        except Exception as e:
            logger.warning("Rejection score delta failed: {}; using default bump", e)
        return (
            True,
            self._ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT,
            "Score adjusted to reflect user rejection.",
        )

    async def _retag_analysis_report_safe(
        self, workspace_id: str, report: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Re-apply currentState tagging to a report whose text sections were
        flattened to plain strings by ``_normalize_analysis_report_lists`` (e.g.
        after a reject/add mutation), so the emitted payload keeps the
        {id,text,isMatched} shape. Fail-open: returns the input on any error.
        """
        try:
            return await self._tag_analysis_report_against_current_state(
                workspace_id, report, log_context=" (feedback)",
            )
        except Exception as tag_err:
            logger.debug(
                "Analysis report re-tagging after feedback failed: {}; "
                "emitting untagged",
                tag_err,
            )
            return report

    async def on_analysis_report_feedback(self, data: Dict[str, Any]) -> None:
        """
        Handle analysis_report_feedback: user rejected an analysis item (missingInfo or riskOrWeakness).

        - The matching item is always removed from the list (the user explicitly
          rejected it).
        - The AI then judges whether the rejection itself is VALID — i.e. the
          rejected item is genuinely not relevant to this case.
            - VALID rejection → caseResolutionScore is bumped 1–5.
            - INVALID rejection (item IS relevant; user dismissed a real gap) →
              score is unchanged.
        - qualityScoreExplanation and reportQualityScoreExplanation are
          regenerated either way, and the analysis report is always re-emitted
          so the FE loader clears.

        Concurrency: a per-workspace ``asyncio.Lock`` serialises rejections so
        bulk-reject (e.g. user clicks "Reject" on 5 items at once) cannot race —
        each rejection reads the cache AFTER the previous one's write completes.
        """
        import copy

        payload = data if isinstance(data, dict) else {}
        workspace_id = payload.get("workspaceId") or payload.get("workspace_id") or ""
        user_id = payload.get("userId") or payload.get("user_id") or ""
        workspace_role = (
            payload.get("workspaceRole") or payload.get("workspace_role") or ""
        )
        section = payload.get("section") or ""
        rejected_value = (
            payload.get("rejectedValue") or payload.get("rejected_value") or ""
        ).strip()
        is_rejected = payload.get("isRejected", payload.get("is_rejected", True))

        if not workspace_id or not section or not rejected_value:
            logger.warning(
                "analysis_report_feedback: missing workspaceId, section, or rejectedValue – ignoring"
            )
            return
        if section not in ("missingInfo", "riskOrWeakness"):
            logger.warning(
                "analysis_report_feedback: invalid section '{}' – ignoring", section
            )
            return
        if not is_rejected:
            logger.debug("analysis_report_feedback: isRejected=false – no score change")
            return

        # Bulk reject sends N events within ~2ms. Instead of processing each
        # under a lock (which is correct but takes ~19s per event because of the
        # AI calls — the user disconnects before items 2..N finish), we
        # accumulate them in a per-workspace queue and flush the whole batch
        # ~500ms after the last event arrives. One regeneration, one emit, all
        # rejections applied. Single rejections still work — they just incur the
        # short debounce delay before flushing.
        self._pending_feedback_queue.setdefault(workspace_id, []).append(
            {
                "section": section,
                "rejected_value": rejected_value,
                "user_id": user_id,
                "workspace_role": workspace_role,
            }
        )
        existing_task = self._pending_feedback_task.get(workspace_id)
        if existing_task is None or existing_task.done():
            self._pending_feedback_task[workspace_id] = asyncio.create_task(
                self._debounce_and_flush_feedback(workspace_id)
            )
        logger.info(
            "analysis_report_feedback queued ({} pending) for workspace {} (section={}, value={!r})",
            len(self._pending_feedback_queue[workspace_id]),
            workspace_id,
            section,
            rejected_value[:80],
        )

    async def _debounce_and_flush_feedback(self, workspace_id: str) -> None:
        """Wait briefly so a burst of rejections coalesces, then flush them all."""
        try:
            await asyncio.sleep(self._feedback_debounce_seconds)
        except asyncio.CancelledError:
            return

        lock = self._analysis_feedback_locks.setdefault(
            workspace_id, asyncio.Lock()
        )
        # Loop because new events that arrive during processing are queued;
        # the handler only schedules a fresh task after this one finishes, so
        # we must drain everything we see before exiting.
        while True:
            async with lock:
                queued = self._pending_feedback_queue.get(workspace_id) or []
                if not queued:
                    return
                # Snapshot and clear under the lock so concurrent appends from
                # the handler are picked up by the next loop iteration.
                events = list(queued)
                self._pending_feedback_queue[workspace_id] = []
            try:
                await self._apply_batched_feedback(workspace_id, events)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "analysis_report_feedback batch flush failed for {}: {}",
                    workspace_id,
                    exc,
                )

    @meter_usage
    async def _apply_batched_feedback(
        self, workspace_id: str, events: List[Dict[str, Any]]
    ) -> None:
        """Apply a coalesced batch of rejections in one regeneration cycle."""
        import copy

        if not events:
            return

        # All events for one workspace share user_id / workspace_role — take from
        # the first; fall back to whichever is present if any are missing.
        first = events[0]
        user_id = first.get("user_id") or ""
        workspace_role = first.get("workspace_role") or ""
        for ev in events:
            if not user_id and ev.get("user_id"):
                user_id = ev["user_id"]
            if not workspace_role and ev.get("workspace_role"):
                workspace_role = ev["workspace_role"]

        logger.info(
            "analysis_report_feedback flushing batch of {} rejections for workspace {}",
            len(events),
            workspace_id,
        )

        # Hydrate cache once per batch.
        try:
            await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
        except Exception as e:  # pragma: no cover
            logger.debug(
                "analysis_report_feedback batch: hydration failed for workspace {}: {}",
                workspace_id,
                e,
            )

        current = self._workspace_analysis_payload.get(workspace_id)
        if not current:
            logger.warning(
                "analysis_report_feedback batch: no cached analysis report for workspace {} – dropping {} rejections",
                workspace_id,
                len(events),
            )
            return

        report = copy.deepcopy(current)
        self._normalize_analysis_report_lists(
            report, max_annexure=self._get_annexure_count(workspace_id)
        )
        old_score = int(
            report.get("caseResolutionScore")
            or report.get("case_resolution_score")
            or 0
        )

        # Pass 1: remove every matching item up front. We collect the events
        # that actually removed something so the score-delta calls only run for
        # real removals, not stale duplicates.
        removed_events: list[dict] = []
        for ev in events:
            section = ev["section"]
            rejected_value = ev["rejected_value"]
            removed_here = False
            if section == "missingInfo":
                missing = (
                    report.get("missingInfo") or report.get("missing_info") or []
                )
                if isinstance(missing, list):
                    new_missing = [
                        x
                        for x in missing
                        if not self._match_rejected_value(x, rejected_value)
                    ]
                    if len(new_missing) < len(missing):
                        report["missingInfo"] = new_missing
                        removed_here = True
            else:
                weaknesses = report.get("weaknesses") or []
                if isinstance(weaknesses, list):
                    new_weak = [
                        x
                        for x in weaknesses
                        if not self._match_rejected_value(x, rejected_value)
                    ]
                    if len(new_weak) < len(weaknesses):
                        report["weaknesses"] = new_weak
                        removed_here = True
                risks = report.get("risks") or []
                if isinstance(risks, list):
                    new_risks = [
                        x
                        for x in risks
                        if not self._match_rejected_value(x, rejected_value)
                    ]
                    if len(new_risks) < len(risks):
                        report["risks"] = new_risks
                        removed_here = True

            if removed_here:
                self._record_rejected_analysis_item(
                    workspace_id=workspace_id,
                    section=section,
                    rejected_value=rejected_value,
                )
                removed_events.append(ev)

        if not removed_events:
            # Every rejection was stale (already removed). Just refresh the
            # timestamp and re-emit so the FE loader clears.
            logger.info(
                "analysis_report_feedback batch: all {} rejections were stale for workspace {} – re-emitting unchanged",
                len(events),
                workspace_id,
            )
            report["timestamp"] = datetime.utcnow().isoformat() + "Z"
            report = await self._retag_analysis_report_safe(workspace_id, report)
            self._workspace_analysis_payload[workspace_id] = report
            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="analysis_report",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=report,
                    usage=current_usage(),
                )
            return

        # Pass 2: validity check + score delta for each real removal, in parallel.
        delta_tasks = [
            self._compute_rejection_score_delta(
                current_score=old_score,
                section=ev["section"],
                rejected_value=ev["rejected_value"],
                report=report,
            )
            for ev in removed_events
        ]
        delta_results = await asyncio.gather(*delta_tasks, return_exceptions=True)

        total_delta = 0
        for ev, result in zip(removed_events, delta_results):
            if isinstance(result, BaseException):
                logger.warning(
                    "analysis_report_feedback batch: score-delta call failed for {!r}: {}",
                    ev["rejected_value"][:80],
                    result,
                )
                continue
            is_valid, delta, _reason = result
            if not is_valid:
                logger.info(
                    "analysis_report_feedback batch: rejection judged INVALID — no score bump for {!r}",
                    ev["rejected_value"][:80],
                )
                continue
            total_delta += int(delta or 0)

        # Cap the batch's combined score bump.
        if total_delta > self._feedback_batch_max_score_delta:
            logger.info(
                "analysis_report_feedback batch: capping combined score delta {} → {}",
                total_delta,
                self._feedback_batch_max_score_delta,
            )
            total_delta = self._feedback_batch_max_score_delta

        new_score = min(100, old_score + total_delta)
        max_allowed = self._analysis_report_max_allowed_score(report=report)
        new_score = min(new_score, max_allowed)
        # Guard: clearing every pending item must not collapse the score to 0.
        # This happens when the cached report carried a missing / None /
        # non-numeric caseResolutionScore, so old_score read as 0 and the bump
        # stayed ~0. A report with no pending items is fully resolved, so
        # reflect the full allowed score instead of showing 0.
        # (max_allowed == 100 ⟺ no missingInfo / weaknesses / risks remain.)
        if new_score <= 0 and max_allowed >= 100:
            logger.warning(
                "analysis_report_feedback batch: score read as 0 with no pending "
                "items for workspace {} (likely missing cached caseResolutionScore); "
                "setting to {}",
                workspace_id,
                max_allowed,
            )
            new_score = max_allowed
        report["caseResolutionScore"] = new_score

        # Regenerate the explanation once for the final state.
        updated_missing = (
            report.get("missingInfo") or report.get("missing_info") or []
        )
        updated_weaknesses = report.get("weaknesses") or []
        updated_risks = report.get("risks") or []
        updated_strengths = report.get("strengths") or []

        fresh_explanation = await self._ensure_quality_score_explanation(
            workspace_role=workspace_role,
            case_resolution_score=new_score,
            missing_info=updated_missing,
            weaknesses=updated_weaknesses,
            risks=updated_risks,
            strengths=updated_strengths,
            existing="",
        )
        report["qualityScoreExplanation"] = fresh_explanation
        report["reportQualityScoreExplanation"] = (
            self._sanitize_report_quality_score_explanation(fresh_explanation)
        ).strip()
        report["timestamp"] = datetime.utcnow().isoformat() + "Z"

        report = await self._retag_analysis_report_safe(workspace_id, report)
        self._workspace_analysis_payload[workspace_id] = report

        if self._socket_client:
            await self._emit_document_feature(
                feature_type="analysis_report",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=report,
                usage=current_usage(),
            )
            logger.success(
                "✅ analysis_report_feedback batch applied: {} rejections, score {}% → {}% (delta +{})",
                len(removed_events),
                old_score,
                new_score,
                total_delta,
            )
        else:
            logger.warning(
                "analysis_report_feedback batch: no socket client – report updated in cache only"
            )

    async def _compute_added_missing_info_score_delta(
        self,
        current_score: int,
        title: str,
        description: str,
        report: Dict[str, Any],
    ) -> Tuple[bool, int, str]:
        """
        Ask AI to (a) decide if user-supplied title+description is RELATED to the case
        and (b) compute a score DECREASE (1-5) when related. The system will SUBTRACT
        the returned delta from the current case resolution score.

        Returns (is_related, decrease, reason). On failure returns
        (False, 0, "") so the caller can no-op safely.
        """
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            if not gemini_client.model:
                logger.warning(
                    "added_missing_information: Gemini unavailable; assuming not related"
                )
                return (False, 0, "")

            case_summary = (
                report.get("caseSummary") or report.get("case_summary") or ""
            ).strip()
            report_summary = (
                report.get("reportCaseSummary")
                or report.get("report_case_summary")
                or ""
            ).strip()
            strategic_objective = (
                report.get("strategicObjective")
                or report.get("strategic_objective")
                or ""
            ).strip()

            def _flatten(items: Any, limit: int = 6) -> List[str]:
                out: List[str] = []
                if not isinstance(items, list):
                    return out
                for x in items[:limit]:
                    if isinstance(x, dict):
                        t = x.get("point") or x.get("text") or ""
                    else:
                        t = x
                    if isinstance(t, str) and t.strip():
                        out.append(t.strip()[:200])
                return out

            missing_items = _flatten(
                report.get("missingInfo") or report.get("missing_info") or [],
                limit=8,
            )
            weakness_items = _flatten(report.get("weaknesses") or [], limit=6)
            risk_items = _flatten(report.get("risks") or [], limit=6)

            missing_block = (
                "\n".join(f"- {x}" for x in missing_items) if missing_items else "- (none)"
            )
            weakness_block = (
                "\n".join(f"- {x}" for x in weakness_items)
                if weakness_items
                else "- (none)"
            )
            risk_block = (
                "\n".join(f"- {x}" for x in risk_items) if risk_items else "- (none)"
            )

            safe_title = (title or "").strip()[:300]
            safe_description = (description or "").strip()[:1500]

            prompt = compute_added_missing_info_score_delta_prompt(case_summary=case_summary, current_score=current_score, missing_block=missing_block, report_summary=report_summary, risk_block=risk_block, safe_description=safe_description, safe_title=safe_title, strategic_objective=strategic_objective, weakness_block=weakness_block)

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    None
                ),
                temperature=0.3,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            raw = (raw or "").strip()
            if not raw or raw.startswith("Error") or "Error generating" in raw[:50]:
                logger.warning(
                    "added_missing_information score delta: empty/error response; treating as not related. raw={}",
                    (raw or "")[:120],
                )
                return (False, 0, "")

            if "```" in raw:
                fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
                if fence_match:
                    raw = fence_match.group(1).strip()
                else:
                    raw = re.sub(r"^```\w*\s*", "", raw)
                    raw = re.sub(r"\s*```\s*$", "", raw)

            obj: Optional[Dict[str, Any]] = None
            start = raw.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(raw[start : i + 1])
                            except json.JSONDecodeError:
                                obj = None
                            break

            if not (obj and isinstance(obj, dict)):
                logger.warning(
                    "added_missing_information score delta: could not parse JSON; treating as not related. raw={}",
                    raw[:200],
                )
                return (False, 0, "")

            is_related_raw = obj.get("isRelated")
            if isinstance(is_related_raw, str):
                is_related = is_related_raw.strip().lower() in ("true", "yes", "1")
            else:
                is_related = bool(is_related_raw)

            try:
                delta = int(
                    obj.get("scoreDecrease") or obj.get("score_decrease") or 0
                )
            except (TypeError, ValueError):
                delta = 0
            delta = max(0, min(5, delta))
            reason = (obj.get("reason") or "").strip()[:200]

            if not is_related:
                logger.info(
                    "added_missing_information: AI judged not related (reason={})",
                    reason or "(no reason)",
                )
                return (False, 0, reason)

            if delta < 1:
                # Related but no magnitude provided → fall back to default drop so the score actually moves.
                delta = self._ADDED_MISSING_INFO_SCORE_DROP_DEFAULT

            logger.info(
                "added_missing_information score delta: AI delta=-{}, reason={}",
                delta,
                reason or "(no reason)",
            )
            return (True, delta, reason or "Score adjusted to reflect newly disclosed information.")
        except Exception as e:
            logger.warning(
                "added_missing_information score delta failed: {}; treating as not related",
                e,
            )
            return (False, 0, "")

    @meter_usage
    async def on_added_missing_information(self, data: Dict[str, Any]) -> None:
        """
        Handle added_missing_information: user manually supplies title+description
        to address previously-flagged missing information.

        - If the user-supplied text is RELATED to the case (per AI judgement),
          the entry is APPENDED to the missingInfo list (deduped against existing
          entries) so it is visible to the user and can later be rejected via the
          existing analysis_report_feedback flow. caseResolutionScore is then
          DECREASED, qualityScoreExplanation is regenerated freshly, and the
          analysis report is re-emitted.
        - If the text is NOT related, the cached analysis report is re-emitted
          unchanged (only the timestamp is refreshed) so the frontend loader
          clears. Score, explanations, and list items stay exactly as-is.
        """
        import copy

        payload = data if isinstance(data, dict) else {}
        workspace_id = (
            payload.get("workspaceId") or payload.get("workspace_id") or ""
        ).strip()
        user_id = (payload.get("userId") or payload.get("user_id") or "").strip()
        workspace_role = (
            payload.get("workspaceRole") or payload.get("workspace_role") or ""
        )
        title = (payload.get("title") or "").strip()
        description = (payload.get("description") or "").strip()

        if not workspace_id:
            logger.warning(
                "added_missing_information: missing workspaceId – ignoring"
            )
            return
        if not title and not description:
            logger.warning(
                "added_missing_information: both title and description are empty – ignoring (workspace={})",
                workspace_id,
            )
            return

        # Repopulate in-memory caches from Qdrant when AI service was restarted
        # between the original analysis_report emit and this event.
        try:
            await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
        except Exception as e:  # pragma: no cover - best-effort hydration
            logger.debug(
                "added_missing_information: hydration failed for workspace {}: {}",
                workspace_id,
                e,
            )

        current = self._workspace_analysis_payload.get(workspace_id)
        if not current:
            logger.warning(
                "added_missing_information: no cached analysis report for workspace {} – cannot apply update",
                workspace_id,
            )
            return

        report = copy.deepcopy(current)
        # Self-heal any dict-shaped list entries that may already be sitting
        # in the cached report from before the dict→string coercion fix.
        self._normalize_analysis_report_lists(
            report, max_annexure=self._get_annexure_count(workspace_id)
        )
        old_score = int(
            report.get("caseResolutionScore")
            or report.get("case_resolution_score")
            or 0
        )

        is_related, score_delta, _delta_reason = (
            await self._compute_added_missing_info_score_delta(
                current_score=old_score,
                title=title,
                description=description,
                report=report,
            )
        )

        if not is_related:
            # Frontend shows a loader on submit and clears it only when a fresh
            # analysis_report payload is received. Re-emit the cached report
            # unchanged (only refresh the timestamp) so the loader resolves
            # without altering the score, explanation, or list items.
            logger.info(
                "added_missing_information: input is not related to the case – "
                "re-emitting unchanged analysis_report to clear FE loader (workspace={})",
                workspace_id,
            )
            report["timestamp"] = datetime.utcnow().isoformat() + "Z"
            report = await self._retag_analysis_report_safe(workspace_id, report)
            self._workspace_analysis_payload[workspace_id] = report
            if self._socket_client:
                await self._emit_document_feature(
                    feature_type="analysis_report",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=report,
                    usage=current_usage(),
                )
                logger.success(
                    "✅ added_missing_information no-op: re-emitted unchanged analysis_report (score stays at {}%)",
                    old_score,
                )
            else:
                logger.warning(
                    "added_missing_information: no socket client – cannot clear FE loader for workspace {}",
                    workspace_id,
                )
            return

        # Append the user-supplied entry to missingInfo so it is visible in the
        # report and can later be rejected via analysis_report_feedback. Use a
        # plain string (matches the existing missingInfo schema) and dedupe
        # against the current list using the same matcher the rejection flow
        # relies on.
        if title and description:
            new_entry = f"{title}: {description}"
        else:
            new_entry = title or description
        new_entry = new_entry.strip()

        missing_list_key = (
            "missingInfo" if "missingInfo" in report else "missing_info"
        )
        current_missing = report.get(missing_list_key) or []
        if not isinstance(current_missing, list):
            current_missing = [current_missing] if current_missing else []

        already_present = any(
            self._match_rejected_value(x, new_entry) for x in current_missing
        )
        if not already_present and new_entry:
            current_missing = list(current_missing) + [new_entry]
            report[missing_list_key] = current_missing
            # Remember user-added entries so the answered-by-upload resolution
            # pass never auto-removes them (only analysis_report_feedback can).
            bucket = list(self._workspace_user_added_missing_info.get(workspace_id) or [])
            if new_entry not in bucket:
                bucket.append(new_entry)
                self._workspace_user_added_missing_info[workspace_id] = bucket[-50:]
            logger.info(
                "added_missing_information: appended new entry to missingInfo "
                "(workspace={}, total={})",
                workspace_id,
                len(current_missing),
            )
        else:
            logger.info(
                "added_missing_information: entry already present in missingInfo – not duplicating (workspace={})",
                workspace_id,
            )

        # Apply score decrease (clamped at 0). The 95% cap still applies because
        # we just appended a missingInfo item, so the case has at least one
        # pending item.
        new_score = max(0, old_score - score_delta)
        max_allowed = self._analysis_report_max_allowed_score(report=report)
        new_score = min(new_score, max_allowed)
        report["caseResolutionScore"] = new_score

        # Regenerate a clean, generic explanation that reflects the updated state.
        # Lawyers share this with clients, so do NOT mention deltas or that the
        # user manually added information.
        updated_missing = (
            report.get("missingInfo") or report.get("missing_info") or []
        )
        updated_weaknesses = report.get("weaknesses") or []
        updated_risks = report.get("risks") or []
        updated_strengths = report.get("strengths") or []

        fresh_explanation = await self._ensure_quality_score_explanation(
            workspace_role=workspace_role,
            case_resolution_score=new_score,
            missing_info=updated_missing,
            weaknesses=updated_weaknesses,
            risks=updated_risks,
            strengths=updated_strengths,
            existing="",  # force regeneration so the explanation reflects current state
        )
        report["qualityScoreExplanation"] = fresh_explanation
        report["reportQualityScoreExplanation"] = (
            self._sanitize_report_quality_score_explanation(fresh_explanation)
        ).strip()
        report["timestamp"] = datetime.utcnow().isoformat() + "Z"

        report = await self._retag_analysis_report_safe(workspace_id, report)
        self._workspace_analysis_payload[workspace_id] = report

        if self._socket_client:
            await self._emit_document_feature(
                feature_type="analysis_report",
                workspace_id=workspace_id,
                user_id=user_id,
                payload=report,
                usage=current_usage(),
            )
            logger.success(
                "✅ added_missing_information applied: score {}% → {}% (-{}), re-emitted analysis_report",
                old_score,
                new_score,
                score_delta,
            )
        else:
            logger.warning(
                "added_missing_information: no socket client – report updated in cache only"
            )
