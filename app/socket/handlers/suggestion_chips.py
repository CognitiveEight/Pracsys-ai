"""Suggestion-chip generation for panels and chat.

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


class SuggestionChipsMixin:
    """Suggestion-chip generation for panels and chat."""

    def _fallback_chips_for_role(self, role: str, needed: int) -> List[str]:
        """Return generic role-appropriate chips to pad when the model returns fewer than 10. Need 12+ for rotation."""
        role_upper = (role or "ADJUDICATOR").upper()
        pools = {
            "ADJUDICATOR": [
                "Summarise key issues",
                "Identify applicable laws",
                "Assess evidence strength",
                "Outline procedural steps",
                "List open questions",
                "Suggest further inquiry",
                "Compare parties' positions",
                "Note gaps in evidence",
                "Recommend next steps",
                "Initiate cyber investigation",
                "Examine edited content",
                "Collect victim statements",
                "Consult cyber forensics",
                "Review jurisdictional aspects",
                "Order preservation of evidence",
            ],
            "PETITIONER": [
                "Draft relief sought",
                "List supporting evidence",
                "Cite applicable provisions",
                "Strengthen arguments",
                "Address counter-arguments",
                "Request interim relief",
                "Outline chronology",
                "Identify witnesses",
                "Suggest documents to file",
                "Summarise harm suffered",
                "Map legal remedies",
                "Draft cause of action",
            ],
            "RESPONDENT": [
                "Identify defence grounds",
                "Challenge petitioner evidence",
                "Cite exceptions",
                "Propose dismissal grounds",
                "List procedural objections",
                "Suggest counter-evidence",
                "Outline limitations",
                "Address each claim",
                "Draft reply points",
                "Highlight inconsistencies",
                "Invoke limitation",
                "Request security for costs",
            ],
            "MEDIATOR": [
                "List common ground",
                "Identify sticking points",
                "Suggest compromise options",
                "Outline risks for each party",
                "Draft settlement framework",
                "Clarify interests",
                "Propose confidence-building steps",
                "Summarise positions",
                "Suggest next round",
                "Propose joint fact-finding",
                "Draft term sheet",
                "Suggest cooling-off period",
            ],
        }
        generic = pools.get(role_upper, pools["ADJUDICATOR"])
        return list(generic)[:needed]

    async def _generate_contextual_suggestion_chips(
        self,
        workspace_id: str,
        role: str,
        user_message: str,
        ai_response_text: str,
        max_chips: int = 12,
        conversation_summary: Optional[str] = None,
    ) -> List[str]:
        """
        Generate 10-12 short, role-based, case-related suggestion chips.
        If the model returns fewer than 10, we pad with generic role-based chips so we have 10-12 to sample from.
        """
        import json

        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            role_upper = (role or "ADJUDICATOR").upper()
            user_preview = (user_message or "")[:300]
            ai_preview = (ai_response_text or "")[:400]
            prompt = f"""You are suggesting follow-up prompts for a legal workspace chat. The workspace role is: {role_upper}.

Last user message: "{user_preview}"
Last assistant reply (summary): "{ai_preview}"
{f'Recent context: {conversation_summary[:200]}' if conversation_summary else ''}

You MUST output a JSON array of exactly 10, 11, or 12 short follow-up prompts. Minimum 10 items. Not 4. Not 5. At least 10 strings in the array.
Each prompt: 2-8 words, role-appropriate, case-related, actionable. User will click one and it gets sent as their next message.
Vary types: draft, explain, list, risks, next steps, clarify, evidence, options.

Return ONLY a JSON array, no other text. Example with 10+ items: ["Draft a settlement proposal", "List key issues for mediation", "What are the risks for each party?", "Suggest compromise options", "Summarise evidence so far", "Outline procedural next steps", "Identify gaps in the complaint", "Recommend documents to collect", "Clarify jurisdiction", "Compare both sides' positions", "Suggest interim measures"]
"""
            raw = await gemini_client.generate(prompt, temperature=0.7, max_tokens=12000)
            if not raw or "Error" in (raw[:80] or ""):
                return []
            raw = raw.strip()
            start = raw.find("[")
            if start < 0:
                return []
            depth = 0
            end = -1
            for i in range(start, len(raw)):
                if raw[i] == "[":
                    depth += 1
                elif raw[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end <= start:
                return []
            arr = json.loads(raw[start:end])
            if not isinstance(arr, list):
                arr = []
            chips = [
                str(x).strip() for x in arr if x and 2 <= len(str(x).strip()) <= 120
            ][:max_chips]
            from_model = len(chips)
            # Always ensure we have 10–12 chips so every 4s we can emit a different random 4
            target_count = 12
            if len(chips) < target_count:
                seen = {c.lower() for c in chips}
                fallbacks = self._fallback_chips_for_role(role, target_count + 5)
                for pad in fallbacks:
                    if len(chips) >= target_count:
                        break
                    if pad and pad.lower() not in seen:
                        chips.append(pad)
                        seen.add(pad.lower())
            result = chips[:target_count]
            logger.info(
                "Contextual chips: model gave {}, after padding have {} (return random 4 each poll)",
                from_model,
                len(result),
            )
            return result
        except Exception as e:
            logger.warning("Contextual suggestion chips generation failed: {}", str(e))
            # Still return 10–12 fallback chips so rotation works
            return self._fallback_chips_for_role(role, 12)

    async def _generate_drafting_chips(
        self,
        *,
        workspace_id: str,
        actionable_id: str,
        draft_status: str,
        draft_title: str,
        open_questions: List[Dict[str, Any]],
        last_assistant_message: str,
        last_draft_markdown: str,
    ) -> List[str]:
        """
        Drafting-specific suggestion chips for the chip strip at the bottom
        of the Drafting Assistant panel. Branches on `draft_status`:

          - "gathering_info"  → chips that help fill the open slots.
          - "ready"           → chips that suggest specific edits / additions
                                to the just-delivered draft.
          - "revising"        → similar to ready, plus version-comparison.

        Each chip is 2–6 words and is meant to be sent verbatim as the
        lawyer's next message — clicking "Strengthen Grounds" sends the
        text "Strengthen Grounds" back as a follow-up turn. Indian-law
        framing only; no IPC.
        """
        import json

        status = (draft_status or "").strip().lower()
        title = (draft_title or "New Draft").strip()
        msg_preview = (last_assistant_message or "")[:600]
        draft_preview = (last_draft_markdown or "")[:1200]
        open_q_text = "\n".join(
            f"- {q.get('label')}" for q in (open_questions or []) if q.get("label")
        )[:600]

        # Mode-specific guidance for the model.
        if status == "gathering_info":
            mode_block = (
                "MODE: gathering_info — open slots remain. Each chip should "
                "either (a) volunteer one of the missing pieces verbatim "
                "(short imperative phrasing), or (b) push the draft forward "
                "(\"Generate Final Draft\", \"Use Defaults and Proceed\")."
            )
        elif status == "revising":
            mode_block = (
                "MODE: revising — a draft was just regenerated. Suggest "
                "specific further edits the lawyer might apply to this draft, "
                "plus one comparison option (\"Show Previous Version\")."
            )
        else:
            mode_block = (
                "MODE: ready — a full draft was just delivered. Suggest "
                "specific, actionable edits to that draft: strengthen / add "
                "specific clauses or grounds, tighten / shorten sections, "
                "add citations applicable to this case, adjust the prayer."
            )

        prompt = f"""You are suggesting follow-up chips for the Drafting Assistant chat panel of an Indian legal workspace. The lawyer is mid-draft.

DRAFT TITLE: {title}
DRAFT STATUS: {status or "ready"}

OPEN QUESTIONS STILL ON FILE (if any):
{open_q_text or "(none — all answered)"}

LAST ASSISTANT MESSAGE (the most recent chat turn shown to the lawyer):
\"\"\"
{msg_preview}
\"\"\"

DRAFT BODY PREVIEW (if a draft has been delivered):
\"\"\"
{draft_preview or "(no draft yet)"}
\"\"\"

{mode_block}

CONSTRAINTS:
- Each chip MUST be 2–6 words.
- Use Indian-advocate phrasing (\"Adjust the Prayer\", \"Add Section 50 Ground\", \"Tighten the Facts\", \"Cite Arnesh Kumar\", \"Mention Tihar Detention\", \"Add Verification Clause\").
- Reference real case anchors when visible in the draft body (e.g. quantity numbers, dates, party names, statute citations).
- No IPC / CrPC / Indian Evidence Act references. Use BNS / BNSS / BSA where citations are needed.
- No full sentences. No trailing punctuation.
- Each chip stands alone — if the lawyer clicks it, the text is sent as their next message and the drafting agent should be able to act on it as a revision instruction (or as a slot answer when in gathering_info).

OUTPUT FORMAT — strict JSON array of 8 to 12 chip strings, no commentary, no markdown fences:
["Strengthen the Grounds", "Adjust the Prayer", "Add Section 50 Ground", ...]
"""
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            raw = await gemini_client.generate(
                prompt,
                temperature=0.4,
                max_tokens=1200,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Drafting chips generation failed: {}", e)
            return self._fallback_drafting_chips(8)

        if not raw or "Error" in (raw[:80] or ""):
            return self._fallback_drafting_chips(8)
        try:
            text = raw.strip()
            start = text.find("[")
            if start < 0:
                return self._fallback_drafting_chips(8)
            depth = 0
            end = -1
            for i in range(start, len(text)):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end <= start:
                return self._fallback_drafting_chips(8)
            arr = json.loads(text[start:end])
        except Exception:
            return self._fallback_drafting_chips(8)
        if not isinstance(arr, list):
            return self._fallback_drafting_chips(8)

        chips: List[str] = []
        seen: set = set()
        for item in arr:
            s = str(item or "").strip().strip('"').strip("'").rstrip(".!?,;:")
            if not s or len(s) > 60:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            chips.append(s)
            if len(chips) >= 12:
                break

        # Pad with fallback chips if the model gave us fewer than 8.
        if len(chips) < 8:
            for pad in self._fallback_drafting_chips(8):
                if pad.lower() in seen:
                    continue
                chips.append(pad)
                seen.add(pad.lower())
                if len(chips) >= 8:
                    break

        logger.info(
            "Drafting chips: generated {} for workspace={} actionable={} status={}",
            len(chips),
            (workspace_id or "")[:12],
            (actionable_id or "")[:24],
            status,
        )
        return chips

    def _fallback_drafting_chips(self, needed: int = 8) -> List[str]:
        """Return generic drafting-relevant chips when no draft turn has run yet.

        Used when BE polls probing_prompts_request with chatType=DRAFTING_ASSISTANT
        before any drafting LLM turn has produced contextual chips. Indian-law
        framing only; no IPC.
        """
        generic = [
            "Adjust the Prayer",
            "Strengthen the Grounds",
            "Add a Section 50 Ground",
            "Tighten the Facts",
            "Add Verification Clause",
            "Cite Arnesh Kumar",
            "Provide Missing Details",
            "Generate Final Draft",
        ]
        return list(generic)[:needed]

    def _fallback_chips_for_analysis_report(self, needed: int = 16) -> List[str]:
        """Return generic analysis-report prompt chips when AI generation fails or report is empty."""
        generic = [
            "What is in this document",
            "Explain key strengths",
            "List missing information",
            "What are the risks",
            "Summarise weaknesses",
            "Suggest next steps",
            "Explain quality score",
            "Relevant jurisdiction",
            "Pending actions",
            "Recommended drafts",
            "Compare with strategy",
            "Identify gaps",
            "Outline procedural steps",
            "Key evidence summary",
            "Legal basis for strengths",
            "Mitigation for risks",
        ]
        return list(generic)[:needed]

    async def _generate_analysis_report_suggestion_chips(
        self,
        workspace_id: str,
        report: Dict[str, Any],
        role: str,
    ) -> List[str]:
        """
        Generate 15-16 short, analysis-report-specific suggestion chips (2-5 words each).
        Used for ANALYSIS_REPORT chatType probing_prompts; stored per workspace.
        """
        import json

        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            role_upper = (role or "ADJUDICATOR").upper()
            # Build short context from report for the prompt
            missing = report.get("missingInfo") or report.get("missing_info") or []
            strengths = report.get("strengths") or []
            weak = report.get("weaknesses") or []
            risks = report.get("risks") or []
            next_steps = report.get("nextSteps") or report.get("next_steps") or []
            drafts = (
                report.get("suggestedDrafts") or report.get("suggested_drafts") or []
            )
            score = report.get("caseResolutionScore") or report.get(
                "case_resolution_score"
            )
            if (
                isinstance(strengths, list)
                and strengths
                and isinstance(strengths[0], dict)
            ):
                strength_pts = [
                    s.get("point", s.get("text", ""))[:80] for s in strengths[:3]
                ]
            else:
                strength_pts = [str(s)[:80] for s in (strengths or [])[:3]]
            context_parts = [
                f"Score: {score}%",
                (
                    f"Missing info ({len(missing)}): "
                    + "; ".join(str(m)[:60] for m in missing[:2])
                    if missing
                    else "None"
                ),
                f"Strengths: " + "; ".join(strength_pts) if strength_pts else "None",
                (
                    f"Weaknesses: " + "; ".join(str(w)[:60] for w in weak[:2])
                    if weak
                    else "None"
                ),
                (
                    f"Risks: " + "; ".join(str(r)[:60] for r in risks[:2])
                    if risks
                    else "None"
                ),
                (
                    f"Next steps: " + "; ".join(str(n)[:60] for n in next_steps[:2])
                    if next_steps
                    else "None"
                ),
                (
                    f"Suggested drafts: " + "; ".join(str(d)[:60] for d in drafts[:2])
                    if drafts
                    else "None"
                ),
            ]
            context_str = "\n".join(context_parts)
            prompt = f"""You are generating suggestion chips for an Analysis Report view in a legal case tool. Role: {role_upper}.

Analysis report summary:
{context_str}

Output a JSON array of exactly 15 or 16 short, actionable prompt labels (2-5 words each). Each label is a question or action the user might tap to ask about the analysis. Examples: "What is in this document", "Explain key strengths", "List missing information", "What are the risks", "Summarise weaknesses", "Suggest next steps", "Explain quality score", "Relevant jurisdiction", "Pending actions", "Recommended drafts". No full sentences. Vary: missing info, strengths, weaknesses, risks, next steps, drafts, jurisdiction, score explanation.

Return ONLY a JSON array of 15-16 strings, no other text."""
            raw = await gemini_client.generate(prompt, temperature=0.5, max_tokens=12000)
            if not raw:
                logger.warning(
                    "Analysis report chips: empty response from Gemini; using fallback"
                )
                return self._fallback_chips_for_analysis_report(16)
            raw = (raw or "").strip()
            # Treat only leading \"Error...\" as fatal; allow 'error' words inside JSON
            if raw.startswith("Error") or "Error generating" in (raw[:80] or ""):
                logger.warning(
                    "Analysis report chips: Gemini error response; using fallback. raw preview={}",
                    raw[:120],
                )
                return self._fallback_chips_for_analysis_report(16)
            start = raw.find("[")
            if start < 0:
                logger.warning(
                    "Analysis report chips: no '[' found in raw response; falling back. raw preview={}",
                    raw[:200],
                )
                # Salvage any double-quoted strings as a last resort before full fallback
                import re as _re

                quoted = _re.findall(r'"([^"]{2,100})"', raw)
                chips = [q.strip() for q in quoted if q.strip()]
                if chips:
                    logger.info(
                        "Analysis report chips: salvaged {} labels from quoted strings",
                        len(chips),
                    )
                    chips = chips[:16]
                else:
                    return self._fallback_chips_for_analysis_report(16)
            else:
                depth = 0
                end = -1
                for i in range(start, len(raw)):
                    if raw[i] == "[":
                        depth += 1
                    elif raw[i] == "]":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end <= start:
                    logger.warning(
                        "Analysis report chips: could not find matching ']' (start={}, end={}); raw preview={}",
                        start,
                        end,
                        raw[:200],
                    )
                    # Try salvage from quoted strings
                    import re as _re

                    quoted = _re.findall(r'"([^"]{2,100})"', raw)
                    chips = [q.strip() for q in quoted if q.strip()][:16]
                    if chips:
                        logger.info(
                            "Analysis report chips: salvaged {} labels from quoted strings after bracket mismatch",
                            len(chips),
                        )
                    else:
                        return self._fallback_chips_for_analysis_report(16)
                else:
                    try:
                        arr = json.loads(raw[start:end])
                    except json.JSONDecodeError as je:
                        logger.warning(
                            "Analysis report chips: JSON decode failed; attempting salvage. error={}, raw segment preview={}",
                            je,
                            raw[start : start + 200],
                        )
                        import re as _re

                        quoted = _re.findall(r'"([^"]{2,100})"', raw[start:end])
                        chips = [q.strip() for q in quoted if q.strip()][:16]
                        if chips:
                            logger.info(
                                "Analysis report chips: salvaged {} labels from quoted strings after JSON error",
                                len(chips),
                            )
                        else:
                            return self._fallback_chips_for_analysis_report(16)
                    else:
                        if not isinstance(arr, list):
                            logger.warning(
                                "Analysis report chips: parsed JSON is not a list (type={}); using fallback",
                                type(arr),
                            )
                            return self._fallback_chips_for_analysis_report(16)
                        chips = [
                            str(x).strip()
                            for x in arr
                            if x and 2 <= len(str(x).strip()) <= 100
                        ][:16]
            if len(chips) < 15:
                seen = {c.lower() for c in chips}
                for pad in self._fallback_chips_for_analysis_report(20):
                    if len(chips) >= 16:
                        break
                    if pad and pad.lower() not in seen:
                        chips.append(pad)
                        seen.add(pad.lower())
            result = chips[:16]
            logger.info(
                "Analysis report chips: {} for workspace {}",
                len(result),
                workspace_id[:12] if workspace_id else "",
            )
            return result
        except Exception as e:
            logger.warning(
                "Analysis report suggestion chips generation failed: {}", str(e)
            )
            return self._fallback_chips_for_analysis_report(16)
