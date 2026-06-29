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
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    f"- The user has uploaded exactly {num_annexures} annexures.\n"
                    f"- The ONLY valid annexure references are: Annexure 1 through Annexure {num_annexures}.\n"
                    f"- You MUST NOT reference Annexure {num_annexures + 1} or any higher number.\n"
                    "- Do NOT invent additional annexures."
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

            analysis_prompt = f"""You are a legal strategy analyst for Indian law. Analyze the following documents and provide a detailed strategy and objective.

**Role**: {workspace_role}
**Document Content** (documents are in upload order; the last one is the most recently uploaded):
{combined_document_content}

**Task**: Based on the document content above, provide:

1. **Case Summary** (10-20 sentences): Start EXACTLY with the phrase: **"This case is about..."**. Write a fact-rich narrative that NAMES the parties exactly as written in the documents (full names + designations / roles such as plaintiff, defendant, complainant, vendor, vendee). When the documents contain them, you MUST include: property/land/document identifiers (e.g. *Arazi No. 262, area 0.263 Hectares*, *Khasra No. 285*), location to the most specific level supported by the documents (Mauza/Village → Block/Tehsil → District/State), exact monetary amounts in the same notation as the source (*Rs. 11,64,000*, *Rs. 10,00,000*), specific dates of agreements/notices/filings (DD/MM/YYYY), and any suit/case numbers (e.g. *O.S. No. 827/2019*). Reference uploaded files only as **Annexure N**. Do not generalise — extract.

2. **Report Case Summary** (10-20 sentences): MUST start EXACTLY with **"This matter is about..."**. Mirror the same facts, structure, and coverage as the case summary but as a neutral, expanded, polished narrative for formal reports and filings. Apply the SAME fact-extraction checklist: named parties (full names + designations), property/document identifiers, full location, exact amounts as written, specific dates, suit/case numbers, named statutes. **Strictly do not** use: Petitioner, Respondent, Client, Case, Adjudicator, Mediator, or role-style labels; describe parties only by names, designations, or factual identifiers from the documents.

3. **Strategic Objective** (10-20 sentences): Write a role-tailored strategic objective for the **{workspace_role}** perspective. It MUST anchor on the actual strongest facts (named parties, specific dates, named documents/Annexures, exact amounts, suit numbers) — never generic categories. Cover: (a) the strongest facts from the documents (named), (b) the legal issues (with named statutes such as BNS / BNSS / BSA / IT Act / Specific Relief Act etc. as relevant), (c) practical priority order (what to do first), and (d) how evidence/timeline supports or limits the objective.

4. **Possible Next Steps** (3-5 bullet points): Immediate, executable actions for right away. Keep them concrete (who/what to do). Prefer steps that preserve evidence, clarify missing facts, and move the case forward procedurally for the selected role. When citing an uploaded file by its order in the bundle, write **Annexure N** (same order as above); **never** write "Document N" or "document" for an upload—use **annexure / annexures** only.

**Completed-action recognition (CRITICAL).** Inspect the metadata header on every annexure. When a document is a `notice` / `complaint` / `affidavit` / `application` / `plaint` AND either (a) its `ISSUED BY` field names the client side (matches the `{workspace_role}` perspective, the named client, or the client's advocate), OR (b) the header carries a `[DRAFTED BY ASSISTANT …]` marker, treat the underlying procedural action as **ALREADY COMPLETED** in this matter. Apply this consistently across every output field:
- `caseSummary` / `reportCaseSummary`: narrate the completed action in past tense (e.g. *"On 22/05/2026, a legal notice for recovery of €17,185 was issued by Advocate Rohan Mehta to Dana Europe OÜ and the related noticees…"*), citing the served-on date, the addressees, the demand amount, and the compliance window. Then continue with the next stage of the matter.
- `strategicObjective`: refer to the action in past tense (*"the demand notice has been served on …"*) and shift focus to the next pending action that the served notice enables — monitoring the compliance window, preparing the follow-up suit/FIR/application on default, evaluating arbitration invocation, etc.
- `possibleNextSteps`: **DO NOT re-list the action that has already been performed**. If a legal/demand notice has been served, the next step is NOT *"issue a legal notice"*; it is monitoring the compliance deadline (cite the exact date if available in `[KEY DATES]`) and the natural follow-up (e.g. summary suit under Order XXXVII CPC, BNS §318 / §316 criminal complaint, arbitration invocation per the agreement). If an FIR complaint has been filed, the next step is NOT *"file an FIR"*; it is follow-up on registration / investigation status. Same for affidavits, applications, plaints, written statements.
- Apply this rule independently to each completed action — if multiple drafted documents exist in the annexure set, each one is a separate completed milestone.

**Format your response as JSON**:
{{
  "caseSummary": "...",
  "reportCaseSummary": "...",
  "strategicObjective": "...",
  "possibleNextSteps": ["...", "...", "...", "..."]
}}

**Important**: 
- Be specific to the case facts in the documents. Do NOT invent facts.
- Use Indian legal terminology appropriately.
- Reference BNS (Bharatiya Nyaya Sanhita) sections, NOT IPC (Indian Penal Code is replaced).
- Reference BNSS (Bharatiya Nagarik Suraksha Sanhita) for procedural matters, NOT CrPC.
- Reference BSA (Bharatiya Sakshya Adhiniyam) for evidence matters, NOT Indian Evidence Act.
- For IT Act, continue using Information Technology Act, 2000 sections as it's still in force.
- Make next steps actionable and role-specific.
- If documents do not contain enough information, explicitly state the limitation in caseSummary/reportCaseSummary/strategicObjective (do not guess).
- If documents seem to include multiple matters, explicitly call out the split and focus your output on the dominant dispute supported by strongest evidence.

**Legal-domain coverage (civil law AND criminal law — BOTH branches):**
- Treat caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps as a dual-lens exercise. Before writing each field, consciously ask:
  (1) "What CRIMINAL-law angle (e.g. BNS 2023 / BNSS 2023 / BSA 2023 / IT Act 2000 / NI Act §138 / PMLA / NDPS / POCSO / PCA, etc.) do the facts attract?", and
  (2) "What CIVIL-law angle (e.g. Indian Contract Act 1872 / Specific Relief Act 1963 / Transfer of Property Act 1882 / CPC 1908 / Limitation Act 1963 / Sale of Goods Act 1930 / Consumer Protection Act 2019 / Companies Act 2013 / Arbitration Act 1996 / family laws / RERA / MV Act / CGST Act / Insolvency & Bankruptcy Code, etc.) do the same facts attract?"
- When BOTH branches are plausibly supported by the documents, the output MUST cover both:
  * `caseSummary` and `reportCaseSummary`: name the criminal-law issues AND the civil-law / private-law issues raised by the facts.
  * `strategicObjective`: address the role's plan on BOTH the criminal-law track (e.g. FIR / complaint / prosecution / defence / quashing under BNSS) AND the civil-law track (e.g. suit for damages / specific performance / declaration / injunction / consumer complaint / arbitration / NCLT petition), in priority order.
  * `possibleNextSteps`: include at least one immediate criminal-track action AND at least one immediate civil-track action when both branches are live (e.g. "Preserve digital evidence and prepare BNS §318 cheating complaint" alongside "Issue legal notice under Indian Contract Act §73 demanding compensation").
- When the facts only support one branch (purely civil OR purely criminal), stay in that branch — do NOT manufacture the other side just to look balanced. Coherence beats balance.
- **No bracket tags in user-facing output.** Do NOT prefix or label items with "(criminal angle)", "(civil angle)", "(criminal-law angle)", "(civil-law angle)", or any similar bracketed tag. Weave the criminal-law and civil-law content naturally into the prose of `caseSummary` / `reportCaseSummary` / `strategicObjective` and into the plain text of each `possibleNextSteps` bullet, the same way a lawyer would draft them in a memo."""

            try:
                strategy_inventory = self._build_document_inventory(workspace_id)
                import json as _json_strategy
                try:
                    _entities_block = _json_strategy.dumps(
                        case_entities, indent=2, ensure_ascii=False
                    )
                except Exception:
                    _entities_block = "{}"
                # Improved prompt for better JSON generation
                structured_prompt = f"""Analyze these legal documents and provide a JSON response.

EXTRACTED FACTS (these entities were already pulled from the documents by a
focused extraction step — treat them as authoritative ground truth. Anchor
every named reference in your output to these. Do NOT rename, abbreviate,
or modify any of these names. Do NOT introduce 'Party No. 1' / 'Party No. 2'
/ 'the plaintiff' / 'the respondent' / 'the petitioner' style placeholders —
EVERY reference to a person, entity, property, suit, or amount in your
output MUST use the exact form listed below. If a relevant fact is missing
from this block but is clearly present in the DOCUMENT CONTENT below, you
may add it, but never invent.):

{_entities_block}

{strategy_inventory or "DOCUMENT INVENTORY: (no classified inventory available)"}

{annexure_limit_block}

DOCUMENT CONTENT:
{combined_document_content}

TRUNCATED-TEXT AND PERIPHERAL-ENTITY RULES (CRITICAL — applies to caseSummary, reportCaseSummary, strategicObjective, possibleNextSteps, and suggestionChips):

1. Some uploaded annexures are SCREENSHOTS of mobile-app or web-app UIs (UPI apps, billers directories, recent-payees lists, vendor catalogs, search-suggestion dropdowns, etc.). In those UIs, the application itself truncates long labels with "..." or "…" so they fit the row width. OCR captures the truncation verbatim.

2. TREAT ANY TOKEN, NAME, OR PHRASE IN THE DOCUMENT CONTENT THAT ENDS IN "..." OR "…" AS A UI-TRUNCATED LABEL, NOT A REAL ENTITY NAME. Examples of the pattern you must filter: "Smart Prep...", "Uttar Pradesh Power Corporatio...", "MSEDCL Mahavitaran - Maharashtra ...", "ABC Industri…".

3. NEVER reproduce a UI-truncated label verbatim in your output. NEVER write any phrase that ends in "..." or "…" inside caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps / suggestionChips. NEVER attempt to guess the full name from a truncated prefix — that would be a hallucination.

4. PERIPHERAL ENTITIES — Only include in your output the entities that are MATERIALLY CONNECTED to the alleged events or transactions described in the documents (named parties, amounts paid/received, identified accused/complainants, banks where the disputed transactions occurred, statutes invoked, courts named). DO NOT list incidental items pulled from app-UI screenshots — billers directories, recent-payees lists, vendor catalogs, search results — unless a specific entry is itself part of the alleged events.

5. If after filtering you have NO concrete case facts left in a section, state that limitation explicitly per the INSUFFICIENT DATA rule below — do NOT fill the gap with peripheral biller / vendor / payee listings.

{SPEAKER_LABEL_GUARDRAIL}

YOUR ROLE: {workspace_role}

TASK:
1. Case summary (10-20 sentences — write the FULL chronological narrative, do NOT compress ; MUST start with "This case is about..." ; MUST name every party exactly as written, include every property/land/document identifier, exact amounts in source notation, every significant date in DD/MM/YYYY, every suit/case/FIR number, and named statutes when present in the documents — never generic categories)
2. Report case summary (10-20 sentences — write the FULL narrative, do NOT compress ; MUST start EXACTLY with "This matter is about..." ; MUST mirror the same facts, structure, and coverage as the case summary BUT rewritten as a neutral, expanded, and polished narrative suitable for formal reports and filings ; same fact-extraction checklist as the case summary — named parties, property/document IDs, exact amounts, every significant date, suit numbers, named statutes ; STRICTLY DO NOT use the words: Petitioner, Respondent, Client, Case, Adjudicator, Mediator, or any role-based labels ; avoid even generic role references ; describe parties only by their names, designations, or factual identifiers from the documents ; maintain an advanced, professional tone while keeping it clear and easy to read)
3. Strategic objective (10-20 sentences — write the FULL plan, do NOT compress ; role-specific for {workspace_role} ; MUST anchor on named parties, specific dates, exact amounts, suit numbers, and named statutes from the documents — never generic categories)
4. Possible next steps (3-5 immediate, concrete actions that should be taken right away). When referring to an uploaded file by index, use **Annexure N** where N is the upload-order index within the hard limit specified in AVAILABLE ANNEXURES above. Never use the word **Document** or **document** for an upload—use **annexure** / **annexures** only.

LEGAL FRAMEWORK (CRITICAL - USE CORRECT INDIAN LAWS):
- Always reference applicable and **most relevant Indian statutes/sections based on the case facts**, including but not limited to:
  CRIMINAL-LAW SIDE:
  • Bharatiya Nyaya Sanhita (BNS), 2023 — substantive offences
  • Bharatiya Nagarik Suraksha Sanhita (BNSS), 2023 — criminal procedure
  • Bharatiya Sakshya Adhiniyam (BSA), 2023 — evidence
  • Information Technology Act, 2000 — cyber-offences
  • Negotiable Instruments Act, 1881 — Section 138 (cheque dishonour, quasi-criminal)
  • Prevention of Money Laundering Act, 2002; NDPS Act, 1985; Arms Act, 1959; POCSO, 2012; PCA, 1988; UAPA, 1967, etc., where facts fit
  CIVIL-LAW SIDE:
  • Indian Contract Act, 1872
  • Specific Relief Act, 1963
  • Transfer of Property Act, 1882
  • Code of Civil Procedure, 1908
  • Limitation Act, 1963
  • Sale of Goods Act, 1930
  • Consumer Protection Act, 2019
  • Companies Act, 2013
  • Arbitration and Conciliation Act, 1996
  • Hindu Marriage Act 1955 / Special Marriage Act 1954 / Hindu Succession Act 1956 / Guardians and Wards Act 1890 (family / succession matters)
  • Real Estate (Regulation and Development) Act, 2016; Motor Vehicles Act, 1988; Insolvency and Bankruptcy Code, 2016, etc., where facts fit
  REGULATORY / TAX (where relevant):
  • Goods and Services Tax (GST) Acts
  • Right to Information Act, 2005
  • SC/ST (Prevention of Atrocities) Act, 1989
  • Any other applicable central or state law

- Do NOT restrict analysis to only one statute—identify and apply **all relevant laws**.
- Prefer **latest applicable laws** (e.g., BNS/BNSS/BSA instead of IPC/CrPC/Evidence Act where applicable). Do NOT cite IPC 1860, CrPC 1973, or the Indian Evidence Act 1872.

DUAL-LENS RULE (CIVIL LAW + CRIMINAL LAW — BOTH BRANCHES):
- Before writing caseSummary / reportCaseSummary / strategicObjective / possibleNextSteps, consciously ask:
  (1) What is the **criminal-law angle** of these facts? (BNS / BNSS / BSA / IT Act / NI Act §138 / etc.)
  (2) What is the **civil-law angle** of the same facts? (Contract / Specific Relief / TPA / CPC / Consumer Protection / Companies Act / Arbitration / family law / etc.)
- When BOTH branches are plausibly supported by the documents, the output MUST cover both:
  * caseSummary + reportCaseSummary: name the criminal-law issues AND the civil-law / private-law issues raised by the facts.
  * strategicObjective: address the role's plan on BOTH the criminal-law track (e.g. FIR / complaint / prosecution / defence / quashing under BNSS) AND the civil-law track (e.g. suit for damages / specific performance / declaration / injunction / consumer complaint / arbitration / NCLT petition), in priority order.
  * possibleNextSteps: include at least one immediate criminal-track action AND at least one immediate civil-track action when both branches are live.
- When the facts only support one branch (purely civil OR purely criminal), stay in that branch — do NOT manufacture the other side just for balance. Coherence beats balance.
- **No bracket tags in user-facing output.** Do NOT prefix or label items with "(criminal angle)", "(civil angle)", "(criminal-law angle)", "(civil-law angle)", or any similar bracketed tag. Weave both threads into natural prose / bullet text the way a lawyer would draft them in a memo.

MULTIPLE MATTERS:
If documents appear to include multiple matters, explicitly mention ambiguity and focus your output on the dominant dispute supported by strongest evidence.

INSUFFICIENT DATA:
If documents do not contain enough information, explicitly state the limitation in caseSummary, reportCaseSummary, and strategicObjective. Do NOT guess facts.

OUTPUT (JSON only, complete all fields):
```json
{{
  "caseSummary": "This case is about... (facts, parties, core legal issues)",
  "reportCaseSummary": "This matter is about... (same facts/coverage as caseSummary; neutral expanded narrative; no Petitioner/Respondent/Client/Case/Adjudicator/Mediator labels)",
  "strategicObjective": "Role-tailored strategic objective for {workspace_role} (facts, priorities, evidence support/limits)",
  "possibleNextSteps": [
    "Immediate action 1",
    "Immediate action 2",
    "Immediate action 3",
    "Immediate action 4",
    "Immediate action 5"
  ],
  "suggestionChips": [
    "Chip 1", "Chip 2", "Chip 3", "Chip 4", "Chip 5", "Chip 6", "Chip 7", "Chip 8",
    "Chip 9", "Chip 10", "Chip 11", "Chip 12", "Chip 13", "Chip 14", "Chip 15", "Chip 16"
  ]
}}
```

suggestionChips: exactly 15-16 short, actionable next-step labels (2-5 words each) specific to this case and role. Examples: "Review Title Deeds", "Map Legal Heirs", "Draft Demand Notice", "File FIR", "Preserve Digital Evidence". No full sentences. Provide 15-16 varied chips.

CRITICAL: Output complete, valid JSON only. Do not truncate; close every string with \", close every array with ], and make sure the root JSON object is fully closed. Keep caseSummary, reportCaseSummary, and strategicObjective under 4500 characters each AND target 10-20 sentences each — use the full budget to include every named party, every suit/document/property identifier, every exact amount (in the same notation as the documents), every significant date, and every named statute reference. Do NOT compress to a short summary; do NOT pad with generic boilerplate. Ensure caseSummary starts exactly with "This case is about..." and reportCaseSummary starts exactly with "This matter is about...". In possibleNextSteps strings, never use "Document" or "document" to mean an uploaded file—use Annexure N or annexure/annexures."""
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
                                    + "\n\nRewrite all THREE fields so EVERY missing "
                                    "item above appears in the prose. Do NOT introduce "
                                    "new facts beyond what is needed to incorporate "
                                    "these. Do NOT delete any existing facts. Keep the "
                                    "length budget (under 4500 characters each). Keep "
                                    "the openers EXACTLY: caseSummary must start with "
                                    "'This case is about...'; reportCaseSummary must "
                                    "start with 'This matter is about...'.\n\n"
                                    "Return ONE JSON object with exactly these three "
                                    "keys: caseSummary, reportCaseSummary, "
                                    "strategicObjective. No other keys, no "
                                    "commentary, no markdown.\n\n"
                                    "ORIGINAL caseSummary:\n"
                                    f"{case_summary}\n\n"
                                    "ORIGINAL reportCaseSummary:\n"
                                    f"{_report_case_summary}\n\n"
                                    "ORIGINAL strategicObjective:\n"
                                    f"{strategic_objective}\n"
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
                                        "- strategicObjective: 10-20 sentences. "
                                        f"Role-specific for {workspace_role}. "
                                        "Anchor on named parties from the "
                                        "EXTRACTED FACTS, exact dates in "
                                        "DD/MM/YYYY, suit/FIR numbers, exact "
                                        "amounts, and named statutes "
                                        "(BNS/BNSS/BSA/IT Act/NDPS/etc.). Do "
                                        "NOT use Petitioner / Respondent / "
                                        "Client / Case / Adjudicator / "
                                        "Mediator labels. Cover both "
                                        "criminal-law and civil-law tracks "
                                        "when both are supported by the "
                                        "facts. No bracket tags like "
                                        "'(criminal angle)'."
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
                                recovery_prompt = f"""Return ONLY one valid JSON object with these keys:
{{
  {_fields_json}
}}

Context:
- Role: {workspace_role}
- Case summary: {case_summary}
- Strategic objective (existing, may be empty/short): {strategic_objective}

EXTRACTED FACTS (authoritative ground truth — use exact names/spellings, do NOT rename or abbreviate):
{_entities_block_recovery}

Documents:
{combined_document_content}

Rules:
{_rules_block}
- Use "Annexure N" wording for uploaded files (never "Document N").
- No invented facts; only use what is in the EXTRACTED FACTS and Documents above.
- JSON only; no markdown fences.
"""
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

    async def _regenerate_strategy_objective_preserving_case_summary(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_objective_instruction: str,
        current_strategy: Dict[str, Any],
    ) -> bool:
        """
        Regenerate strategicObjective, possibleNextSteps, and suggestionChips from the user's new
        objective instruction. Keeps caseSummary and reportCaseSummary unchanged; emits strategy_objective.
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

            prompt = f"""You are a legal strategy analyst for Indian law.

The following case summaries are FIXED. Do NOT change, paraphrase, or contradict them.

LOCKED caseSummary (verbatim context):
{case_summary}

LOCKED reportCaseSummary (verbatim context):
{report_case_summary or "(same facts as case summary; neutral report narrative)"}

**User's new strategic priority / instruction** (follow this as the primary constraint):
{user_objective_instruction.strip()}

Current strategic objective (to replace):
{(current_strategy.get("strategicObjective") or "").strip()}

Current possible next steps (to replace):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

**Role**: {role}

**Document content** (for fact consistency only; do not rewrite the case summaries):
{combined_document_content}

Return JSON ONLY with these keys:
{{
  "strategicObjective": "5-6 sentences, role-tailored for {role}, aligned with the user's instruction and the case facts",
  "possibleNextSteps": ["3-5 concrete immediate actions; use Annexure N for uploads, never Document N"],
  "suggestionChips": ["15-16 short labels, 2-5 words each, actionable"]
}}

Rules:
- Do not restate or replace caseSummary/reportCaseSummary in the output.
- The new strategicObjective and possibleNextSteps MUST reflect the new user instruction and MUST NOT just repeat the current objective/steps.
- BNS/BNSS/BSA naming (not IPC/CrPC/IEA where replaced).
- No invented facts; if the instruction conflicts with documents, note the tension in strategicObjective.
- {SPEAKER_LABEL_GUARDRAIL}
- JSON only; complete strings and arrays."""

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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": merged,
                    },
                )
                logger.success(
                    "✅ Strategy objective regen emitted (case summary preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Strategy objective regen failed: {}", e)
            return False

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

            prompt = f"""You are a legal-strategy editor for Indian law.

You will edit the existing Case Summary and Report Case Summary based on the user's instruction below. The Strategic Objective and Possible Next Steps are LOCKED and must NOT change.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{user_feedback.strip()}

CURRENT caseSummary (you must produce an edited version of this text after applying the user's instruction):
{current_case_summary}

CURRENT reportCaseSummary (apply the SAME edit; keep it a neutral expanded narrative — no role labels like Petitioner/Respondent/Client/Case/Adjudicator/Mediator):
{current_report_case_summary or current_case_summary}

LOCKED strategicObjective (verbatim context, do not output):
{(current_strategy.get("strategicObjective") or "").strip()}

LOCKED possibleNextSteps (verbatim context, do not output):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

**Role**: {role}

**Document content** (for fact consistency only; do NOT invent named parties, dates, amounts, suit numbers, or statutes that are not in this content):
{combined_document_content}

EDIT-INSTRUCTION RULES (CRITICAL — override all preservation rules below when they conflict):
1. The user's instruction OUTRANKS the "preserve every named party / amount / date / statute" rule. If the user says "remove" / "delete" / "drop" / "change" a specific sentence, paragraph, fact, or phrase, you MUST apply that change to the text — even if it means dropping a named party, an amount, or a date.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced — including role labels like "the Petitioner" — even if the name does not already appear in the text. A literal matching token is NOT required.
2. If the user QUOTES exact text inside the instruction (anything that looks like a verbatim sentence or phrase from the current case summary), find that exact text in the CURRENT caseSummary above and REMOVE it (and apply the same removal to reportCaseSummary if the same content appears there). Do not "interpret" — match the surface text.
3. Apply ONLY the edits the user asked for. Do NOT remove other content the user did not mention.
4. If the user's instruction asks for something impossible (e.g. add a new dated fact not in the documents), ignore that invention and apply only the parts of the instruction that are removals / rephrases / valid edits.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise from screenshots, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Return JSON ONLY with these two keys:
{{
  "caseSummary": "edited case summary; MUST start EXACTLY with 'This case is about...'; reflect the user's instruction; preserve everything else from CURRENT caseSummary that the user did NOT ask to change",
  "reportCaseSummary": "edited report case summary; MUST start EXACTLY with 'This matter is about...'; mirror the caseSummary facts after the edit; neutral expanded narrative; do NOT use the words Petitioner/Respondent/Client/Case/Adjudicator/Mediator"
}}

Output rules:
- Do not output strategicObjective, possibleNextSteps, or suggestionChips.
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do not switch to IPC/CrPC/Evidence Act.
- JSON only; complete strings."""

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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": merged,
                    },
                )
                logger.success(
                    "✅ Case summary regen emitted (other fields preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Case summary regen failed: {}", e)
            return False

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

            prompt = f"""You are a legal-strategy editor for Indian law.

You will edit the existing Possible Next Steps based on the user's instruction below. The Case Summary, Report Case Summary, and Strategic Objective are LOCKED and must NOT change.

**User's edit instruction** (PRIMARY CONSTRAINT — apply EXACTLY as worded):
{user_feedback.strip()}

CURRENT possibleNextSteps (apply the user's instruction to this list; the indices are 1-based):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

CURRENT suggestionChips (refresh only as needed to stay aligned with the updated next steps):
{json.dumps(current_strategy.get("suggestionChips") or [], ensure_ascii=True)}

LOCKED caseSummary (verbatim context, do not output):
{current_case_summary}

LOCKED strategicObjective (verbatim context, do not output):
{current_strategic_objective}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL):
1. The user's instruction OUTRANKS any general guidance. If the user says "remove the 4th point", the returned list MUST omit the 4th item from CURRENT possibleNextSteps and keep the other items in their original order.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced in the steps — including role labels like "the Petitioner" — even if the name does not already appear. A literal matching token is NOT required.
2. If the user says "add a step about X", APPEND a new step about X (or insert at the position they specified) and keep all existing steps unless the user also asked to drop something.
3. If the user QUOTES an existing step verbatim and asks to remove / rephrase it, match the surface text and apply the change.
4. Do NOT silently rewrite untouched steps. Only the steps the user asked to change should differ; the rest must be reproduced word-for-word.

TRUNCATED-TEXT RULE: Never produce a token ending in "..." or "…" — those are UI-truncation noise from screenshots, not real entity names.

Return JSON ONLY with these two keys:
{{
  "possibleNextSteps": ["the resulting 3-5 next steps after applying the instruction; reference uploads as Annexure N (NEVER 'Document N')"],
  "suggestionChips": ["15-16 short labels, 2-5 words each, actionable; aligned with the updated next steps"]
}}

Output rules:
- Do not output caseSummary, reportCaseSummary, or strategicObjective.
- No invented facts (no new named parties / dates / amounts / suit numbers / statutes that are not already in caseSummary or strategicObjective).
- Use Indian-law naming (BNS/BNSS/BSA/IT Act). Do not cite IPC/CrPC/IEA.
- JSON only; complete strings and arrays."""

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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": merged,
                    },
                )
                logger.success(
                    "✅ Possible next steps regen emitted (other fields preserved) workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Possible next steps regen failed: {}", e)
            return False

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

            prompt = f"""You are a legal-strategy editor for Indian law.

You will edit the existing Strategic Objective based on the user's instruction below. The Case Summary, Report Case Summary, Possible Next Steps, and Suggestion Chips are LOCKED and must NOT change.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{user_feedback.strip()}

CURRENT strategicObjective (you must produce an edited version of this text after applying the user's instruction):
{current_strategic_objective}

LOCKED caseSummary (verbatim context, do not output):
{current_case_summary}

LOCKED reportCaseSummary (verbatim context, do not output):
{(current_strategy.get("reportCaseSummary") or "").strip()}

LOCKED possibleNextSteps (verbatim context, do not output):
{json.dumps(current_strategy.get("possibleNextSteps") or [], ensure_ascii=True)}

LOCKED suggestionChips (verbatim context, do not output):
{json.dumps(current_strategy.get("suggestionChips") or [], ensure_ascii=True)}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL — override every other instinct):
1. The user's instruction OUTRANKS any tendency to "improve" or "summarise" the strategic objective. Apply ONLY the change the user named.
1a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she"), identify that party from the role / gender / description context and use the supplied name wherever that party is referenced — including role labels like "the Petitioner" — even if the name does not already appear in the text. A literal matching token is NOT required.
2. If the user QUOTES exact text (a sentence, a phrase, a list of names) inside the instruction, find that exact text in CURRENT strategicObjective and REMOVE (or rephrase, per the verb the user used) ONLY that text.
3. Every other sentence, clause, and reference in CURRENT strategicObjective MUST be reproduced WORD-FOR-WORD in the output. Do NOT compress lists of entities. Do NOT drop arbitration/summary-suit/GST/cloud-suspension/leverage sentences. Do NOT change the length of the output unless the user explicitly asked for that.
4. Do NOT enforce any sentence-count or word-count target. The output length should equal the input length minus what the user removed (or plus what the user asked to add).
5. If the user asks for something impossible (e.g. inventing a fact not in the documents), ignore that invention and apply only the valid removal / rephrase parts.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Return JSON ONLY with ONE key:
{{
  "strategicObjective": "edited strategic objective; reflect the user's instruction; reproduce every other sentence from CURRENT strategicObjective verbatim"
}}

Output rules:
- Do NOT output caseSummary, reportCaseSummary, possibleNextSteps, or suggestionChips.
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do NOT switch to IPC/CrPC/Evidence Act.
- JSON only; complete string."""

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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": merged,
                    },
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
            prompt = f"""The lawyer chatted in informal language about how they want to steer the case. They then confirmed they want the strategy panels updated.

Summarize the NEW strategic objective / priority they want (venue, relief, procedural focus, or emphasis), in 2-8 sentences. Use ONLY what appears in the thread; do not invent facts. If the thread does not support a concrete objective, use an empty string.

Workspace role: {role}

Thread:
{thread}

Reply JSON only: {{"objectiveText": "..."}}"""
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

            prompt = f"""Classify whether the USER message suggests a possible NEW strategic priority or direction for their legal case (e.g. different court/venue, focus on one remedy, shift emphasis, change timing), in informal or indirect wording — even if they do NOT ask to "update the objective" or "change strategy".

Return JSON only: {{"offer": true or false}}

Rules:
- offer=true: user is discussing or leaning toward a different strategic emphasis that could change the formal strategic objective; includes informal / "lemon language".
- offer=false: pure legal information question, greeting, document request, or no strategic repositioning.

User message:
{um[:4000]}

Assistant reply (for context, first 1200 chars):
{ar[:1200]}"""
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

            prompt = f"""You are a STRICT intent classifier for a legal strategic-objective workflow.

Your job is to determine whether the user is:
1. DEFINING or CHANGING a strategy/objective (actionable intent), OR
2. EXPLORING / ASKING / ANALYZING (non-actionable intent)

--------------------------------
OUTPUT FORMAT (JSON ONLY)
--------------------------------
{{
  "mode": "direct_update" | "ask_confirmation" | "none",
  "objectiveText": "normalized objective text or empty",
  "isQuestionLike": true or false,
  "updateRequestedNow": true or false
}}

--------------------------------
CORE PRINCIPLE
--------------------------------
"direct_update" is ONLY allowed when the user clearly intends an IMMEDIATE strategy/objective update.

If immediate intent is missing -> DO NOT use direct_update.

--------------------------------
STEP-BY-STEP DECISION LOGIC
--------------------------------

STEP 1: INFORMATIONAL QUESTION DETECTION (HIGHEST PRIORITY)
If the user is REQUESTING INFORMATION — asking you to explain, describe, analyse,
list, compare, summarise, or answer a what/how/why/which question ABOUT the case,
the law, the evidence, the current objective, the risks, etc.:

-> isQuestionLike = true
-> updateRequestedNow = false
-> mode = "none"

This includes questions that merely MENTION the words "objective" or "strategy"
without asking to change them, e.g.:
- "explain the applicable case laws"            -> mode = none (information)
- "what is the current strategic objective?"    -> mode = none (information)
- "how does the objective help the petitioner?" -> mode = none (information)
- "which laws apply to this dispute?"            -> mode = none (information)

CRITICAL EXCEPTION — a question FORM that is actually an INSTRUCTION to change the
objective is NOT an informational question. If the user, even politely or as a
question, asks you to UPDATE / CHANGE / REVISE / SET / REWRITE the objective or
strategy, treat it as an update:
- "can you update the objective to focus on bail?"   -> mode = direct_update
- "please change the strategy to file in Patna only" -> mode = direct_update
- "could you set the objective to recover Rs 12,000?" -> mode = direct_update
The test is INTENT, not punctuation: information requested -> none; a change to the
objective requested -> direct_update (or ask_confirmation if tentative).

--------------------------------

STEP 2: EXPLORATION / TENTATIVE INTENT
If the user is suggesting, brainstorming, or unsure:

Examples:
- "should we focus on..."
- "maybe we can..."
- "what if we..."
- "we could consider..."

-> mode = "ask_confirmation"
-> updateRequestedNow = false
-> isQuestionLike = true or false (based on phrasing)

--------------------------------

STEP 3: DIRECT STRATEGY / OBJECTIVE UPDATE (STRICT)
Classify as "direct_update" ONLY if ALL conditions are satisfied:

A. The statement is DECLARATIVE (not a question)
B. The intent is FINAL (not exploratory)
C. The user is clearly choosing a direction
D. The user expects this change to be APPLIED NOW

--------------------------------
STRATEGY CHANGE RULE (CRITICAL)
--------------------------------

A strategy shift MUST be treated as "direct_update" if it is decisive and immediate,
even if the user does NOT explicitly say "update objective".

This includes statements like:
- "Focus on X instead of Y"
- "Drop X and pursue Y"
- "We will proceed with X"
- "Shift strategy to X"
- "Prioritize X over Y"

If the above statements are:
- Declarative
- Final (not tentative)
- Actionable immediately

-> mode = "direct_update"
-> updateRequestedNow = true
-> isQuestionLike = false

--------------------------------
DECLARATIVE OBJECTIVE STATEMENTS (CRITICAL — VERY COMMON)
--------------------------------

When the lawyer DECLARES the objective using phrasing like:
- "objective is <X>"
- "the objective is <X>"
- "our objective is <X>"
- "client objective is <X>" / "client's objective is <X>"
- "the client's goal/aim is <X>"
- "objective should be <X>" / "the objective should be <X>"
- "the objective of the case is <X>"
- "set the objective to <X>" / "set objective: <X>"
- "objective: <X>"
- "we want <X>" / "we are seeking <X>" (declarative, not exploratory)

…this is a DECLARATIVE STATEMENT OF THE STRATEGIC OBJECTIVE.

It is NOT an explanation, NOT informational, and NOT exploratory.
You MUST classify it as:
-> mode = "direct_update"
-> updateRequestedNow = true
-> isQuestionLike = false
-> objectiveText = the concrete content of <X> (the part AFTER "is" / ":" / "to")

Counter-examples that are NOT declarative objective statements
(these remain mode = "none"):
- Meta-talk about the concept: "the objective of a writ petition is generally to..."
  (talking ABOUT what an objective is, not setting one)
- Generic teaching: "in cases like this, the objective is usually to ..."
  (describing typical practice, no first-person commitment)
- Question forms: "what should the objective be?", "is the objective to ...?"
- Hypotheticals: "if the objective were X, then ..."

Heuristic to disambiguate:
- First-person / case-specific subject ("our", "client", "the case", "we")
  + "objective"/"goal"/"aim" + "is"/"should be"/":"/"to"
  -> DECLARATIVE -> direct_update
- Third-person / generic subject ("the objective of <concept>", "an objective is")
  + descriptive verb -> EXPLANATORY -> none

--------------------------------
HARD CONSTRAINTS (MANDATORY)
--------------------------------

1. NEVER set mode = "direct_update" if updateRequestedNow = false

2. If isQuestionLike = true:
   -> mode MUST be "none"

3. Analytical or informational requests ALWAYS -> mode = "none"

Examples:
- "identify risks"
- "explain strategy"
- "list options"
- "summarize"
- "compare approaches"
- "what should we do?"

4. If the intent is unclear -> default to "none"

--------------------------------
OBJECTIVE TEXT RULE
--------------------------------

- Extract ONLY when mode != "none"
- Keep it SHORT, CLEAR, and ACTION-ORIENTED
- Do NOT add new information
- Do NOT hallucinate or expand meaning
- For declarative statements ("objective is X", "client objective is X",
  "objective: X", "set the objective to X"), the objectiveText is exactly
  the content X — strip the leading "objective is" / "the objective is" /
  "client objective is" / ":" / "to " framing.

Examples:
Input: "Focus on breach of contract instead of negligence"
Output: "Focus on breach of contract over negligence"

Input: "Client objective is to obtain regular bail under Section 439 CrPC"
Output: "Obtain regular bail under Section 439 CrPC"

Input: "the objective is to challenge the territorial jurisdiction"
Output: "Challenge the territorial jurisdiction"

Input: "objective: secure interim injunction before next hearing"
Output: "Secure interim injunction before next hearing"

--------------------------------
FINAL INSTRUCTION
--------------------------------

Return ONLY valid JSON.
No explanation.
No extra text.

--------------------------------
USER MESSAGE
--------------------------------
{msg[:5000]}
"""
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
                    "\n11) SCOPED PANEL DEFAULT (CRITICAL — OVERRIDES RULE 8): "
                    f"This chat is scoped to the {_PANEL_LABELS[default_panel]} panel. "
                    "If the message contains an edit verb that operates on the EXISTING panel "
                    'TEXT but names NO panel (e.g. "change the wordings", "add a few words", '
                    '"make it shorter", "rephrase it", "remove the 2nd line"), attribute the '
                    f"edit to {default_panel}.shouldEdit=true with the user's full edit text as "
                    "the instruction. Rule 8 still applies when the user explicitly names a "
                    "DIFFERENT panel. Genuine questions still classify as isQuestionLike=true. "
                    "EXCEPTION: a statement that DECLARES a NEW strategic objective / direction "
                    "(see Rule 4's NEW-OBJECTIVE DECLARATION clause) is NOT a surgical edit — do "
                    "NOT apply this scoped default to it; leave strategicObjective.shouldEdit=false "
                    "so the objective-change flow handles it.\n"
                )

            prompt = f"""You are a STRICT intent classifier for a legal-strategy workspace chat.

The user is viewing a workspace with FIVE editable panels:
- CASE SUMMARY (caseSummary): a 10-20 sentence factual narrative of the case.
- REPORT CASE SUMMARY (reportCaseSummary): the same facts rewritten as a neutral, expanded narrative for formal reports.
- STRATEGIC OBJECTIVE (strategicObjective): the role-tailored strategic plan, 10-20 sentences.
- POSSIBLE NEXT STEPS (possibleNextSteps): 3-5 immediate actionable bullets.
- ANALYSIS REPORT (analysisReport): the case analysis — strengths, weaknesses, risks, missing information, and score explanations.

Your job: decide whether the user is DIRECTLY asking to edit one or more of these panels, and split the user's instruction into per-panel chunks.

USER MESSAGE:
\"\"\"{msg}\"\"\"

OUTPUT FORMAT (JSON ONLY):
{{
  "caseSummary":        {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for case summary; empty when shouldEdit=false"}},
  "reportCaseSummary":  {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for report case summary; empty when shouldEdit=false"}},
  "strategicObjective": {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for strategic objective; empty when shouldEdit=false"}},
  "possibleNextSteps":  {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for possible next steps; empty when shouldEdit=false"}},
  "analysisReport":     {{"shouldEdit": true | false, "instruction": "ONLY the per-panel edit text for the analysis report; empty when shouldEdit=false"}},
  "listOfDates":        {{"shouldEdit": true | false, "instruction": "set true ONLY for a NAME change / rename (Rule 12) so the person's name is replaced in the existing dated events; empty otherwise"}},
  "isRename":           true | false,
  "isQuestionLike":     true | false
}}
("isRename" = true ONLY for a workspace-wide NAME change / rename / name-assignment per Rule 12; false for every other kind of edit.)

DECISION RULES:

1) QUESTIONS / EXPLANATIONS / GREETINGS → all four shouldEdit = false, isQuestionLike = true.
   Examples: "what laws apply?", "explain the case summary", "hi", "can you describe this?",
   "is X applicable?", "tell me about ...".

2) EDIT VERBS that trigger shouldEdit when paired with a panel name:
   remove, delete, drop, change, replace, edit, modify, rewrite, add, append,
   insert, expand, shorten, rephrase, fix, correct, update.

3) PANEL NAME RECOGNITION (tolerate typos and case):
   - "case summary", "summary", "the summary"                → caseSummary
   - "report case summary", "report summary"                 → reportCaseSummary
   - "strategic objective", "stategic objective" (typo),
     "objective", "strategy"                                 → strategicObjective
   - "possible next steps", "next steps", "next step",
     "action items", "steps"                                 → possibleNextSteps
   - "analysis report", "analysis", "the report", "report",
     "strength", "strengths", "weakness", "weaknesses",
     "risk", "risks", "missing info", "missing information"  → analysisReport

4) "client objective is to ..." / "the objective is to ..." / "we want to ..." PREFIX RULE (CRITICAL):
   These phrases are NOT a strategic objective declaration when they are followed by an edit verb plus a panel name.
   Example: "client objective is to remove sentence X from case summary" means
   caseSummary.shouldEdit=true, instruction="Remove sentence X from case summary".
   Do NOT treat the wrapping phrase as a strategic objective change.

   NEW-OBJECTIVE DECLARATION → NOT A SURGICAL EDIT (CRITICAL):
   When the user DECLARES or CHANGES the strategic objective to a NEW DIRECTION — e.g.
   "objective is to <direction>", "the objective is to <direction>", "client objective is <direction>",
   "the objective should be <direction>", "change/set/update the objective to <new direction>",
   "we want to pursue <direction>" (e.g. "objective is to focus on recovering ₹12,000 first",
   "change the objective to quashing the false FIR") — this is NOT a surgical panel edit.
   Set strategicObjective.shouldEdit=FALSE and isQuestionLike=false. A separate objective-change
   flow regenerates the whole strategy + analysis from the new objective; this classifier must
   NOT claim it.
   Set strategicObjective.shouldEdit=TRUE ONLY for a SURGICAL edit to the EXISTING objective TEXT —
   remove / delete / drop / rephrase / replace a specific sentence, phrase, or quoted text in the
   objective — or for a workspace-wide rename (see Rule 12).
   Worked contrasts:
     - "objective is to prioritise custody of the son"          -> strategicObjective.shouldEdit=FALSE (new direction → objective-change flow)
     - "change the objective to focus on quashing the FIR"       -> strategicObjective.shouldEdit=FALSE (new direction → objective-change flow)
     - "remove the jurisdiction line from the objective"         -> strategicObjective.shouldEdit=TRUE  (surgical text edit)
     - "rephrase the second sentence of the objective"           -> strategicObjective.shouldEdit=TRUE  (surgical text edit)
     - rename "Replace 'Wife' with 'Sandhya'" (Rule 12)          -> strategicObjective.shouldEdit=TRUE  (rename)

5) SPLIT MULTI-PANEL MESSAGES INTO PER-PANEL INSTRUCTIONS. CRITICAL.
   Each shouldEdit=true field's `instruction` MUST contain ONLY the user's edit text for THAT panel — not the wrapping phrase, not the other panels' edits.
   Use the surrounding "from case summary" / "in the objective" / "from possible next steps" clauses to attribute each sub-instruction.
   Worked example for a three-panel message:
     INPUT: "client objective is to remove this sentence 'AAA AAA AAA' from case summary and 'BBB BBB BBB' remove this from strategic objective and remove 4th point from possible next steps"
     OUTPUT:
     {{
       "caseSummary":        {{"shouldEdit": true,  "instruction": "Remove this sentence from the case summary: AAA AAA AAA"}},
       "reportCaseSummary":  {{"shouldEdit": false, "instruction": ""}},
       "strategicObjective": {{"shouldEdit": true,  "instruction": "Remove this from the strategic objective: BBB BBB BBB"}},
       "possibleNextSteps":  {{"shouldEdit": true,  "instruction": "Remove the 4th point from possible next steps"}},
       "isQuestionLike":     false
     }}

6) WHEN THE USER QUOTES EXACT TEXT TO REMOVE/CHANGE — preserve that quoted text verbatim inside the per-panel `instruction`.

7) `instruction` MUST be non-empty whenever shouldEdit=true. If you cannot extract a clean per-panel instruction with the targeted text, set shouldEdit=false for that panel instead of guessing.

8) AMBIGUITY: if the user says "remove this" / "the response" / "that paragraph" without naming a panel, set ALL four shouldEdit=false. The chat agent will ask for clarification.

9) NEVER trigger an edit when the message is a question, even if a panel name appears.
   "Explain the objective" → strategicObjective.shouldEdit = false, isQuestionLike = true.

10) FACT DISCUSSION WITHOUT EDIT VERB: if the message is only discussing facts
    ("I think the date was actually 16/05/2026") with NO imperative edit verb, set all five shouldEdit=false.

12) WORKSPACE-WIDE RENAME / FIND-AND-REPLACE (HIGHEST PRIORITY — OVERRIDES RULES 8 AND 11):
    When the user asks to correct / replace / change / rename / fix a specific WORD, NAME, TERM,
    DATE, or PHRASE "X" with/to "Y" — e.g. "correct the name Rambur with rambir", "change Rambur to
    Rambir in the workspace", "replace 19/04/2024 with 18/04/2024 everywhere", "fix the spelling of
    Sandhya" — this is a workspace-wide rename. It is NOT scoped to a single panel even when the chat
    is scoped (rule 11) and even if no panel is named.
    Set shouldEdit=true for ALL SIX panels — caseSummary, reportCaseSummary, strategicObjective,
    possibleNextSteps, analysisReport, AND listOfDates — and give EACH the SAME instruction in
    the form:
      Replace every occurrence of 'X' with 'Y' wherever it appears; change nothing else.
    (Substitute the actual X and Y. Preserve the user's exact spelling/casing for X and Y.)
    listOfDates is included ONLY for a rename / name change (so the person's name is replaced in
    the existing dated events) — never for any other kind of edit.
    isQuestionLike=false. Only a genuine question (no edit verb) stays isQuestionLike=true.

    NAME-ASSIGNMENT sub-case (a party identified by ROLE / DESCRIPTION / GENDER, not a literal
    existing token): when the user assigns or corrects the NAME of a party who is described
    rather than named — e.g. "the female employee's name is heena", "the petitioner is named X",
    "the complainant is heena because she is female" — DO NOT emit a bare literal "Replace 'X'
    with 'Y'" (the party may currently appear only as a role label like "the Petitioner" / "the
    employee" / "she", which a literal find-and-replace would miss). Instead set shouldEdit=true
    for ALL SIX panels (including listOfDates) with a SEMANTIC instruction that preserves the
    descriptor and any gender clue, e.g.:
      Name the female employee (the female party, currently referred to as "the Petitioner" / "the
      employee" / "she") "heena" wherever she is referenced — including role labels; do not require
      a literal matching token; change nothing else.

    For BOTH the literal-rename and the name-assignment sub-case, also set "isRename": true. For
    every other kind of edit (line/word add/update/delete, rephrase, remove a sentence), set
    "isRename": false.
{scoped_panel_rule}
Return JSON only — no markdown, no commentary."""

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
                "isQuestionLike": bool(data.get("isQuestionLike") is True),
            }
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
                "Panel-edit classifier: msg_len={} isQuestion={} caseSummary={} reportCaseSummary={} strategicObjective={} possibleNextSteps={} analysisReport={} listOfDates={}",
                len(msg),
                result["isQuestionLike"],
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
