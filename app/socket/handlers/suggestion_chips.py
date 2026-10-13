"""Suggestion-chip generation for panels and chat.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import json
from typing import Any, Dict, List, Optional

from loguru import logger

from app.llm.errors import is_generation_error

from app.utils.redact import chars


from app.socket.event_utils import (
    _coerce_analysis_text_list,
)


from app.prompts.suggestion_chips_prompts import (
    generate_contextual_suggestion_chips_prompt,
    generate_drafting_chips_prompt,
    generate_analysis_report_suggestion_chips_prompt,
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

        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            role_upper = (role or "ADJUDICATOR").upper()
            user_preview = (user_message or "")[:300]
            ai_preview = (ai_response_text or "")[:400]
            prompt = generate_contextual_suggestion_chips_prompt(ai_preview=ai_preview, conversation_summary=conversation_summary, role_upper=role_upper, user_preview=user_preview)
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

        prompt = generate_drafting_chips_prompt(draft_preview=draft_preview, mode_block=mode_block, msg_preview=msg_preview, open_q_text=open_q_text, status=status, title=title)
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
            (actionable_id or "")[:12],
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

        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            role_upper = (role or "ADJUDICATOR").upper()
            # Build short context from report for the prompt
            # missingInfo / weaknesses / risks are now {id,text,isMatched} dicts
            # (were plain strings) — coerce to text so chips never embed dict reprs.
            missing = _coerce_analysis_text_list(
                report.get("missingInfo") or report.get("missing_info") or []
            )
            strengths = report.get("strengths") or []
            weak = _coerce_analysis_text_list(report.get("weaknesses") or [])
            risks = _coerce_analysis_text_list(report.get("risks") or [])
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
                "Strengths: " + "; ".join(strength_pts) if strength_pts else "None",
                (
                    "Weaknesses: " + "; ".join(str(w)[:60] for w in weak[:2])
                    if weak
                    else "None"
                ),
                (
                    "Risks: " + "; ".join(str(r)[:60] for r in risks[:2])
                    if risks
                    else "None"
                ),
                (
                    "Next steps: " + "; ".join(str(n)[:60] for n in next_steps[:2])
                    if next_steps
                    else "None"
                ),
                (
                    "Suggested drafts: " + "; ".join(str(d)[:60] for d in drafts[:2])
                    if drafts
                    else "None"
                ),
            ]
            context_str = "\n".join(context_parts)
            prompt = generate_analysis_report_suggestion_chips_prompt(context_str=context_str, role_upper=role_upper)
            raw = await gemini_client.generate(prompt, temperature=0.5, max_tokens=12000)
            if not raw:
                logger.warning(
                    "Analysis report chips: empty response from Gemini; using fallback"
                )
                return self._fallback_chips_for_analysis_report(16)
            raw = (raw or "").strip()
            # Treat only leading \"Error...\" as fatal; allow 'error' words inside JSON
            if is_generation_error(raw):
                logger.warning(
                    "Analysis report chips: Gemini error response; using fallback. raw preview={}",
                    chars(raw),
                )
                return self._fallback_chips_for_analysis_report(16)
            start = raw.find("[")
            if start < 0:
                logger.warning(
                    "Analysis report chips: no '[' found in raw response; falling back. raw preview={}",
                    chars(raw),
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
                        chars(raw),
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
                            "Analysis report chips: JSON decode failed; attempting salvage. error={}, raw segment={}",
                            je,
                            chars(raw[start:end]),
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
