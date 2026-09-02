"""Workspace title/summary/category/description generation.

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


from app.prompts.workspace_metadata_prompts import (
    generate_case_summary_from_documents_prompt,
    generate_case_summary_from_documents_prompt2,
    generate_case_summary_from_documents_simple_prompt,
    generate_fallback_metadata_prompt,
    get_workspace_category_and_subcategory_prompt,
    get_workspace_category_and_subcategory_prompt2,
)


class WorkspaceMetadataMixin:
    """Workspace title/summary/category/description generation."""

    # Role labels that unambiguously mark a side in Indian filings. Used only
    # to detect a title whose two sides were emitted in the wrong order.
    _CASE_TITLE_SIDE_LABELS: Dict[str, str] = {
        "petitioner": "petitioner",
        "petitioners": "petitioner",
        "complainant": "petitioner",
        "complainants": "petitioner",
        "appellant": "petitioner",
        "appellants": "petitioner",
        "plaintiff": "petitioner",
        "plaintiffs": "petitioner",
        "applicant": "petitioner",
        "applicants": "petitioner",
        "informant": "petitioner",
        "claimant": "petitioner",
        "claimants": "petitioner",
        "respondent": "respondent",
        "respondents": "respondent",
        "accused": "respondent",
        "defendant": "respondent",
        "defendants": "respondent",
        "opposite party": "respondent",
        "opposite parties": "respondent",
        "opponent": "respondent",
        "opponents": "respondent",
        "non-applicant": "respondent",
        "non applicant": "respondent",
    }

    # Placeholder sides produced by the fallback paths ("Petitioner vs
    # Respondent", "<Party> vs Matter"): never treated as real party names.
    _CASE_TITLE_PLACEHOLDER_SIDES = {
        "petitioner",
        "respondent",
        "matter",
        "issue",
        "party",
        "party1",
        "party2",
        "unknown",
    }

    # How far (characters) from a party name a role label may sit, on the same
    # line, and still be taken as labelling that party.
    _CASE_TITLE_LABEL_WINDOW = 200

    def _case_title_bare_party(self, side: str) -> str:
        """Strip "& Anr"/"& Ors" shorthand and punctuation off one title side."""
        s = (side or "").strip().strip('"').strip("'")
        s = re.sub(
            r"\s*&\s*(anr|ors|another|others)\.?$", "", s, flags=re.IGNORECASE
        )
        return s.strip(" .,:;-").strip()

    def _case_title_party_side(self, doc_text: str, party: str) -> Optional[str]:
        """
        Decide whether `party` reads as the petitioner-side or respondent-side
        party in `doc_text`, by looking at the role label nearest to each
        mention of the name (e.g. a cause title "ABC ...Petitioner / Versus /
        XYZ ...Respondent", or "Complainant: ABC", "Accused: XYZ").

        Returns "petitioner", "respondent", or None when the documents give no
        clear signal - callers must treat None as "leave the title alone".
        """
        name = self._case_title_bare_party(party)
        if not doc_text or len(name) < 3:
            return None
        if name.lower() in self._CASE_TITLE_PLACEHOLDER_SIDES:
            return None

        text = doc_text[:20000]
        label_re = re.compile(
            r"\b("
            + "|".join(
                re.escape(lbl)
                for lbl in sorted(self._CASE_TITLE_SIDE_LABELS, key=len, reverse=True)
            )
            + r")\b",
            flags=re.IGNORECASE,
        )
        name_re = re.compile(re.escape(name), flags=re.IGNORECASE)

        # Scoped to a single line on purpose. Cause titles put the label on the
        # same line as the party ("ABC ... ...Petitioner"), as do labelled
        # complaints ("Complainant: ABC"), while the label of the *other* side
        # is often physically closer across a line break ("Versus" lines).
        scores = {"petitioner": 0, "respondent": 0}
        for line in text.splitlines():
            if len(line) > 2000:
                line = line[:2000]
            occurrences = list(name_re.finditer(line))
            if not occurrences:
                continue
            labels = [
                (m.start(), m.end(), self._CASE_TITLE_SIDE_LABELS[m.group(1).lower()])
                for m in label_re.finditer(line)
            ]
            if not labels:
                continue
            for occurrence in occurrences[:5]:
                nearest_side = None
                nearest_dist = None
                for label_start, label_end, side in labels:
                    if label_start >= occurrence.end():
                        dist = label_start - occurrence.end()
                    elif label_end <= occurrence.start():
                        dist = occurrence.start() - label_end
                    else:
                        continue  # the label is inside the name itself
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_dist, nearest_side = dist, side
                if (
                    nearest_side
                    and nearest_dist is not None
                    and nearest_dist <= self._CASE_TITLE_LABEL_WINDOW
                ):
                    scores[nearest_side] += 1

        if scores["petitioner"] > scores["respondent"]:
            return "petitioner"
        if scores["respondent"] > scores["petitioner"]:
            return "respondent"
        return None

    def _orient_case_title_sides(
        self, left: str, right: str, doc_text: str
    ) -> Tuple[str, str]:
        """
        Put the petitioner-side party first when the documents clearly say the
        model emitted the two sides the wrong way round.

        Deliberately conservative: the sides are swapped only when the left
        party is clearly respondent-side AND the right party is clearly
        petitioner-side. Any ambiguity leaves the model's order untouched.
        """
        if not doc_text:
            return left, right
        try:
            if self._case_title_party_side(doc_text, left) != "respondent":
                return left, right
            if self._case_title_party_side(doc_text, right) != "petitioner":
                return left, right
        except Exception as e:  # never let title cleanup break metadata
            logger.warning("Case title orientation check failed: {}", str(e))
            return left, right
        logger.info(
            "Case title orientation corrected (sides were swapped): '{} vs {}' -> '{} vs {}'",
            left,
            right,
            right,
            left,
        )
        return right, left

    def _normalize_case_title(self, title: str, doc_text: str = "") -> str:
        """
        Normalize a case title to "Petitioner vs Respondent" style with
        Indian-court multi-party shorthand:

        - When a side has 2 parties joined by " & ", reduce to "<first> & Anr".
        - When a side has 3+ parties joined by " & ", reduce to "<first> & Ors".
        - Single-party sides and sides already ending in "& Anr" / "& Ors" are
          left untouched.
        - Also rewrites " v ", " v. ", " versus " (case-insensitive) to " vs ".
        - When `doc_text` (the document content the title was derived from) is
          supplied, a reversed "<respondent> vs <petitioner>" title is flipped
          back into petitioner-first order.

        Only " & " is treated as a multi-party separator. We deliberately do
        NOT split on commas or the word "and" because those frequently appear
        inside legitimate party / company names (e.g. "Smith, Patel & Co",
        "Pradeep Mehra and Sons").
        """
        if not (title or "").strip():
            return title or ""

        t = title.strip().strip('"').strip("'")

        vs_match = re.search(
            r"\s+(vs|versus|v\.?)\s+",
            t,
            flags=re.IGNORECASE,
        )
        if not vs_match:
            return t

        left = t[: vs_match.start()].strip()
        right = t[vs_match.end() :].strip()

        anr_ors_re = re.compile(
            r"\s*&\s*(anr|ors|another|others)\.?$",
            flags=re.IGNORECASE,
        )

        def _reduce_side(side: str) -> str:
            s = (side or "").strip().rstrip(",")
            if not s:
                return s
            if anr_ors_re.search(s):
                return s
            parts = [p.strip() for p in re.split(r"\s+&\s+", s) if p.strip()]
            if len(parts) <= 1:
                return s
            first = parts[0]
            if len(parts) == 2:
                return f"{first} & Anr"
            return f"{first} & Ors"

        left, right = self._orient_case_title_sides(
            _reduce_side(left), _reduce_side(right), doc_text
        )
        normalized = f"{left} vs {right}"
        normalized = re.sub(r"\s{2,}", " ", normalized).strip()
        return normalized

    def _salvage_workspace_metadata_from_truncated_json(
        self, raw: str, combined: str = ""
    ) -> Dict[str, Any]:
        """Extract caseTitle, caseCategory, subcategories and description from truncated Gemini JSON."""
        import re

        result = {
            "caseTitle": "",
            "caseCategory": "General",
            "subcategories": [],
            "description": "",
        }
        if not raw:
            return result
        # Salvage caseTitle: "caseTitle": "VALUE" (for document-based display title)
        title_match = re.search(r'"caseTitle"\s*:\s*"([^"]*)"', raw)
        if not title_match:
            title_match = re.search(r'"caseTitle"\s*:\s*"([^"]*)$', raw)
        if title_match:
            val = title_match.group(1).strip()
            if val:
                result["caseTitle"] = self._normalize_case_title(val, combined)
        # Salvage caseCategory
        if "caseCategory" in raw:
            cat_match = re.search(r'"caseCategory"\s*:\s*"([^"]*)"', raw)
            if not cat_match:
                cat_match = re.search(r'"caseCategory"\s*:\s*"([^"]*)$', raw)
            if cat_match:
                val = cat_match.group(1).strip().upper()
                if val and len(val) >= 2:
                    result["caseCategory"] = (
                        val if ("LAW" in val or "ACT" in val) else (val + " LAW")
                    )
        # Salvage subcategories
        sub_match = re.search(r'"subcategories"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
        if not sub_match:
            sub_match = re.search(r'"subcategories"\s*:\s*\[(.*)$', raw, re.DOTALL)
        if sub_match:
            inner = sub_match.group(1)
            subs = re.findall(r'"([^"]+)"', inner)
            result["subcategories"] = [s.strip() for s in subs if s.strip()][:5]
        # Salvage description (short case summary, 3-4 lines only)
        desc_match = re.search(r'"description"\s*:\s*"([^"]*)"', raw)
        if not desc_match:
            desc_match = re.search(r'"description"\s*:\s*"([^"]*)$', raw)
        if desc_match:
            val = desc_match.group(1).strip()
            if val and self._is_valid_short_summary(val):
                lines = [
                    ln.strip()
                    for ln in val.replace("\\n", "\n").split("\n")
                    if ln.strip()
                ]
                if len(lines) < 3:
                    while len(lines) < 3:
                        lines.append(
                            "Additional details are provided in the supporting documents."
                        )
                elif len(lines) > 4:
                    lines = lines[:4]
                lines = [ln[:120] if len(ln) > 120 else ln for ln in lines]
                result["description"] = "\n".join(lines)[:500]
        return result

    def _is_valid_short_summary(self, description: str) -> bool:
        """Return False if description looks like raw document paste or is not a short summary."""
        if not description or not description.strip():
            return False
        # Reject if it contains document labels or raw paste
        if (
            "--- Document" in description
            or "Document 1 ---" in description
            or "Document 2 ---" in description
        ):
            return False
        # Reject if too long (short summary should be at most ~500 chars for 3-4 lines)
        if len(description) > 550:
            return False
        # Reject if more than 4 lines
        lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
        if len(lines) > 4:
            return False
        # Reject if any single line is very long (likely raw text)
        if any(len(ln) > 200 for ln in lines):
            return False
        return True

    def _is_generic_description(self, text: str) -> bool:
        """Return True if description is the known generic placeholder."""
        if not text or len(text.strip()) < 20:
            return True
        generic_phrases = [
            "Legal matter based on uploaded documents",
            "The case involves parties and issues requiring legal review",
            "The matter requires appropriate legal action",
            "Supporting documents provide further context",
            "parties and issues requiring legal review",  # Partial match
            "appropriate legal action",  # Partial match
        ]
        # Check if it contains the exact generic block or 2+ generic phrases
        exact_generic = (
            "Legal matter based on uploaded documents" in text
            and "The case involves parties and issues requiring legal review" in text
        )
        phrase_count = sum(1 for p in generic_phrases if p in text)
        return exact_generic or phrase_count >= 2

    async def _generate_case_summary_from_documents(self, combined: str) -> str:
        """Use AI to generate a case-specific 3-4 line summary from document content. Works for any language."""
        import re

        if not combined or len(combined.strip()) < 30:
            return ""
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            doc_content = combined[:4000].replace("--- Document", "\nDocument")

            for attempt in range(2):  # First attempt + one retry if generic
                if attempt == 0:
                    prompt = generate_case_summary_from_documents_prompt(doc_content=doc_content)
                else:
                    prompt = generate_case_summary_from_documents_prompt2(doc_content=doc_content)

                raw = await gemini_client.generate(
                    prompt, temperature=0.2 if attempt == 0 else 0.4, max_tokens=12000
                )
                if not raw:
                    logger.warning(
                        "AI returned empty response for case summary (attempt {})",
                        attempt + 1,
                    )
                    continue
                # Split by newline first
                lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()][:4]
                # If model returned 1–2 lines (e.g. one paragraph), split by sentence to get 3–4 lines
                if len(lines) < 3 and lines:
                    combined_text = " ".join(lines)
                    logger.debug(
                        "Attempting sentence split on: {}", combined_text[:100]
                    )
                    # Split on sentence endings: period, exclamation, or question mark followed by space or end
                    # Use lookahead to keep the punctuation
                    sentences = re.split(r"([.!?])\s+", combined_text)
                    # Reconstruct sentences with punctuation
                    reconstructed = []
                    for i in range(0, len(sentences) - 1, 2):
                        if i + 1 < len(sentences):
                            sentence = (sentences[i] + sentences[i + 1]).strip()
                        else:
                            sentence = sentences[i].strip()
                        if (
                            sentence and len(sentence) > 8
                        ):  # Lower threshold for short valid sentences
                            # Ensure sentence ends with punctuation
                            if not re.search(r"[.!?]$", sentence):
                                sentence += "."
                            reconstructed.append(sentence)
                    # If we didn't get good splits, try simpler period-based split
                    if len(reconstructed) < 2:
                        sentences = re.split(r"\.\s+", combined_text)
                        reconstructed = []
                        for s in sentences:
                            s = s.strip()
                            if s and len(s) > 8:
                                if not s.endswith("."):
                                    s += "."
                                reconstructed.append(s)
                    if (
                        len(reconstructed) >= 2
                    ):  # Accept 2+ sentences (better than failing)
                        lines = reconstructed[:4]
                        logger.info(
                            "Sentence split successful: {} sentences from {} lines",
                            len(lines),
                            len(
                                [
                                    ln.strip()
                                    for ln in raw.strip().split("\n")
                                    if ln.strip()
                                ]
                            ),
                        )
                if len(lines) < 2:  # Require at least 2 lines (be more lenient)
                    logger.warning(
                        "AI returned only {} lines (need at least 2) for case summary (attempt {})",
                        len(lines),
                        attempt + 1,
                    )
                    continue
                summary = "\n".join(lines[:4])
                lines = [
                    ln[:120] if len(ln) > 120 else ln for ln in summary.split("\n")
                ]
                summary = "\n".join(lines)[:500]
                if not self._is_valid_short_summary(summary):
                    logger.warning(
                        "AI summary failed validation (attempt {}): {}",
                        attempt + 1,
                        summary[:100],
                    )
                    continue
                if self._is_generic_description(summary):
                    logger.warning(
                        "AI returned generic description (attempt {}): {}",
                        attempt + 1,
                        summary[:100],
                    )
                    if attempt == 0:
                        logger.info("Retrying with stricter prompt")
                    continue
                logger.info(
                    "Successfully generated case-specific description (attempt {})",
                    attempt + 1,
                )
                return scrub_app_brand(summary)

            logger.error(
                "Failed to generate case-specific description after {} attempts", 2
            )
            # Try simple fallback before giving up
            logger.info("Attempting simple fallback for case summary generation")
            simple_result = await self._generate_case_summary_from_documents_simple(
                combined
            )
            if simple_result:
                logger.info("Simple fallback succeeded: {}", simple_result[:80])
                return simple_result
            return ""
        except Exception as e:
            logger.warning("AI case summary generation failed: {}", str(e))
            # Try simple fallback on exception too
            try:
                simple_result = await self._generate_case_summary_from_documents_simple(
                    combined
                )
                if simple_result:
                    logger.info(
                        "Simple fallback succeeded after exception: {}",
                        simple_result[:80],
                    )
                    return simple_result
            except Exception:
                pass
            return ""

    async def _generate_case_summary_from_documents_simple(self, combined: str) -> str:
        """Simpler fallback: generate case summary with minimal prompt when main method fails."""
        import re

        if not combined or len(combined.strip()) < 30:
            return ""
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            doc_content = combined[:3000].replace("--- Document", "\nDocument")
            prompt = generate_case_summary_from_documents_simple_prompt(doc_content=doc_content)
            raw = await gemini_client.generate(prompt, temperature=0.5, max_tokens=12000)
            if not raw:
                return ""
            lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()][:4]
            if len(lines) < 2 and lines:  # More lenient: accept 2+ lines
                combined_text = " ".join(lines)
                # Split on sentence endings
                sentences = re.split(r"([.!?])\s+", combined_text)
                reconstructed = []
                for i in range(0, len(sentences) - 1, 2):
                    if i + 1 < len(sentences):
                        sentence = (sentences[i] + sentences[i + 1]).strip()
                    else:
                        sentence = sentences[i].strip()
                    if sentence and len(sentence) > 8:
                        if not re.search(r"[.!?]$", sentence):
                            sentence += "."
                        reconstructed.append(sentence)
                # Fallback to simple period split
                if len(reconstructed) < 2:
                    sentences = re.split(r"\.\s+", combined_text)
                    reconstructed = []
                    for s in sentences:
                        s = s.strip()
                        if s and len(s) > 8:
                            if not s.endswith("."):
                                s += "."
                            reconstructed.append(s)
                if len(reconstructed) >= 2:
                    lines = reconstructed[:4]
            if len(lines) < 2:  # Require at least 2 lines
                return ""
            summary = "\n".join(lines[:4])
            lines = [ln[:120] if len(ln) > 120 else ln for ln in summary.split("\n")]
            summary = "\n".join(lines)[:500]
            if self._is_valid_short_summary(
                summary
            ) and not self._is_generic_description(summary):
                return scrub_app_brand(summary)
            return ""
        except Exception:
            return ""

    def _generate_case_description_from_content(self, doc_text: str) -> str:
        """Synchronous fallback: generate short summary from content. Prefer _generate_case_summary_from_documents (async) for case-specific text."""
        if not doc_text or len(doc_text.strip()) < 50:
            return "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."

        import re

        # Match "--- Document N ---" (avoid [^---] set-difference warning: use [^-]* = any chars except dash)
        cleaned = re.sub(
            r"---\s*Document\s*\d+[^-]*---", " ", doc_text, flags=re.IGNORECASE
        )
        cleaned = cleaned.strip()
        if len(cleaned) < 30:
            cleaned = doc_text

        sentences = []
        for s in cleaned.split("."):
            s = s.strip()
            if len(s) < 15 or len(s) > 180:
                continue
            if s.startswith("---") or "Document" in s or s.isdigit():
                continue
            sentences.append(s + ".")

        short_sentences = [s for s in sentences if len(s) <= 120][:4]
        if len(short_sentences) >= 3:
            return scrub_app_brand("\n".join(short_sentences[:4]))
        return "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."

    def _ensure_metadata_complete(
        self, metadata: Dict[str, Any], combined: str = ""
    ) -> Dict[str, Any]:
        """Ensure all metadata fields have non-empty values."""
        result = metadata.copy()
        # Ensure category
        if not result.get("caseCategory") or result.get("caseCategory") == "General":
            result["caseCategory"] = "CIVIL LAW"
        elif (
            "LAW" not in result["caseCategory"] and "ACT" not in result["caseCategory"]
        ):
            result["caseCategory"] = result["caseCategory"] + " LAW"
        # Ensure subcategories
        if not result.get("subcategories"):
            result["subcategories"] = (
                ["Bharatiya Nyaya Sanhita"]
                if "CRIMINAL" in result.get("caseCategory", "")
                else ["Indian Contract Act, 1872"]
            )
        # Ensure description (3-4 lines minimum) - must be a short summary, not raw document text
        desc = result.get("description") or ""
        if not desc.strip() or not self._is_valid_short_summary(desc):
            result["description"] = ""
        if not result.get("description"):
            # Only use generic fallback if we have NO document content
            # If we have document content, async path should have generated description
            # If async failed, leave empty rather than use generic (better than wrong description)
            if not combined or len(combined.strip()) < 50:
                result["description"] = (
                    "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
                )
            else:
                # We have document content but description is empty - async generation likely failed
                # Try sync extraction as last resort, but only if it returns non-generic content
                doc_text = combined[:800].replace("\n", " ")
                extracted = self._generate_case_description_from_content(doc_text)
                # Only use if it's not the generic fallback
                if extracted and not self._is_generic_description(extracted):
                    result["description"] = extracted
                else:
                    # Leave empty - better than generic placeholder
                    logger.warning(
                        "Could not generate case-specific description; leaving empty"
                    )
                    result["description"] = ""
        else:
            # Ensure existing description has 3-4 lines and is case-specific
            desc_lines = [
                ln.strip() for ln in result["description"].split("\n") if ln.strip()
            ]
            # Check if description is generic - if so, clear it (don't regenerate with sync method which also returns generic)
            generic_phrases = [
                "Legal matter based",
                "The case involves parties",
                "The matter requires",
                "Supporting documents",
            ]
            is_generic = any(
                phrase in result["description"] for phrase in generic_phrases
            )

            if is_generic:
                # Clear generic description - async path should have already tried to generate case-specific
                # Don't use sync _generate_case_description_from_content as it also returns generic
                logger.warning(
                    "Description is generic, clearing it (should have been replaced by async AI generation)"
                )
                result["description"] = ""
                desc_lines = []

            if len(desc_lines) < 3:
                # Pad with short generic lines only (never use raw document text)
                while len(desc_lines) < 3:
                    desc_lines.append(
                        "Additional details are provided in the supporting documents."
                    )
                result["description"] = "\n".join(desc_lines)
            elif len(desc_lines) > 4:
                result["description"] = "\n".join(desc_lines[:4])
            # Ensure no line is too long (short summary only)
            desc_lines = [
                ln[:120] if len(ln) > 120 else ln
                for ln in (result["description"].split("\n"))
            ]
            result["description"] = "\n".join(desc_lines)[:500]
        # Ensure title
        if not result.get("caseTitle"):
            result["caseTitle"] = "Petitioner vs Respondent"
        # Ensure title is in "vs" format
        title = result.get("caseTitle", "")
        if title and " vs " not in title and " v " not in title:
            if " vs" in title or "vs " in title:
                title = title.replace(" vs", " vs ").replace("vs ", " vs ")
            else:
                title = f"{title} vs Matter"
            result["caseTitle"] = title
        # Final pass: collapse multi-party sides to "<first> & Anr"/"& Ors"
        # so titles never appear as "A vs B & C" or "A & B vs C".
        result["caseTitle"] = self._normalize_case_title(
            result["caseTitle"], combined
        )
        return result

    async def _generate_fallback_metadata(
        self, combined: str, workspace_id: str
    ) -> Dict[str, Any]:
        """Generate fallback metadata when AI parsing fails - ensures all fields have values."""
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            doc_preview = combined[:400].replace("\n", " ")
            fallback_prompt = generate_fallback_metadata_prompt(doc_preview=doc_preview)
            raw = await gemini_client.generate(
                fallback_prompt, temperature=0.3, max_tokens=12000
            )
            import json
            import re

            start = raw.find("{")
            if start >= 0:
                end = raw.find("}", start) + 1
                if end > start:
                    try:
                        data = json.loads(raw[start:end])
                        title = (data.get("caseTitle") or "").strip()
                        category = (
                            (data.get("caseCategory") or "CIVIL LAW").strip().upper()
                        )
                        subs = data.get("subcategories") or []
                        desc = (data.get("description") or "").strip()
                        if not title:
                            title = "Petitioner vs Respondent"
                        if " vs " not in title and " v " not in title:
                            title = f"{title} vs Matter"
                        if not category or category == "General":
                            category = "CIVIL LAW"
                        if "LAW" not in category and "ACT" not in category:
                            category = category + " LAW"
                        if not subs:
                            subs = (
                                ["Bharatiya Nyaya Sanhita"]
                                if "CRIMINAL" in category
                                else ["Indian Contract Act, 1872"]
                            )
                        if not desc and combined and len(combined.strip()) > 50:
                            desc = await self._generate_case_summary_from_documents(
                                combined
                            )
                        if not desc:
                            desc = self._generate_case_description_from_content(
                                doc_preview
                            )
                        else:
                            # Ensure 3-4 lines and case-specific
                            desc_lines = [
                                ln.strip() for ln in desc.split("\n") if ln.strip()
                            ]
                            # Check if generic - regenerate from content if so
                            generic_phrases = [
                                "Legal matter based",
                                "The case involves parties",
                                "The matter requires",
                                "Supporting documents",
                            ]
                            is_generic = any(
                                phrase in desc for phrase in generic_phrases
                            )

                            if (
                                is_generic
                                and doc_preview
                                and len(doc_preview.strip()) > 50
                            ):
                                desc = self._generate_case_description_from_content(
                                    doc_preview
                                )
                                desc_lines = [
                                    ln.strip() for ln in desc.split("\n") if ln.strip()
                                ]

                            if len(desc_lines) < 3:
                                # Pad with case-specific content
                                if doc_preview and len(doc_preview.strip()) > 50:
                                    sentences = [
                                        s.strip()
                                        for s in doc_preview.split(".")
                                        if len(s.strip()) > 20
                                    ]
                                    while len(desc_lines) < 3 and sentences:
                                        new_line = sentences.pop(0) + "."
                                        if new_line not in desc_lines:
                                            desc_lines.append(new_line)
                                    while len(desc_lines) < 3:
                                        desc_lines.append(
                                            "Additional details are provided in the supporting documents."
                                        )
                                else:
                                    while len(desc_lines) < 3:
                                        desc_lines.append(
                                            "Additional details are provided in the supporting documents."
                                        )
                                desc = "\n".join(desc_lines)
                            elif len(desc_lines) > 4:
                                desc = "\n".join(desc_lines[:4])
                        return {
                            "caseTitle": self._normalize_case_title(title, combined),
                            "caseCategory": category,
                            "subcategories": subs[:3],
                            "description": desc[:500],
                        }
                    except Exception:
                        pass
        except Exception:
            pass
        # Ultimate fallback - use AI to generate case-specific summary from documents when available
        desc = "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
        if combined and len(combined.strip()) > 50:
            ai_desc = await self._generate_case_summary_from_documents(combined)
            if ai_desc:
                desc = ai_desc
        return {
            "caseTitle": "Petitioner vs Respondent",
            "caseCategory": "CIVIL LAW",
            "subcategories": ["Indian Contract Act, 1872"],
            "description": desc,
        }

    async def _get_workspace_category_and_subcategory(
        self,
        workspace_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Infer case title (from documents), case category, subcategories and description (3-4 lines) from workspace documents using AI.
        Returns {"caseTitle": str, "caseCategory": str, "subcategories": List[str], "description": str}.
        Description must be 3-4 lines (minimum 3, maximum 4). caseTitle is fixed once; do not overwrite on sync.
        """
        import json
        import re

        try:
            from app.ai.gemini_client import gemini_client
            from app.vectorstore.qdrant import vector_store

            all_docs = []
            try:
                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
            except Exception as e:
                logger.warning("Could not retrieve documents for category: {}", str(e))
                return {
                    "caseTitle": "Petitioner vs Respondent",
                    "caseCategory": "CIVIL LAW",
                    "subcategories": ["Indian Contract Act, 1872"],
                    "description": "Legal matter based on uploaded documents.",
                }
            if not all_docs:
                return {
                    "caseTitle": "Petitioner vs Respondent",
                    "caseCategory": "CIVIL LAW",
                    "subcategories": ["Indian Contract Act, 1872"],
                    "description": "Legal matter based on uploaded documents.",
                }
            parts = []
            for i, doc in enumerate(all_docs, 1):
                content = (doc.get("content") or "").strip()
                if content and len(content) >= 20:
                    parts.append(f"--- Document {i} ---\n{content}")
            combined = "\n\n".join(parts) if parts else ""
            if not combined:
                return {
                    "caseTitle": "Petitioner vs Respondent",
                    "caseCategory": "CIVIL LAW",
                    "subcategories": ["Indian Contract Act, 1872"],
                    "description": "Legal matter based on uploaded documents.",
                }
            combined = (combined[:5000] + "...") if len(combined) > 5000 else combined
            gemini_client.initialize()
            prompt = get_workspace_category_and_subcategory_prompt(combined=combined)
            raw = await gemini_client.generate(prompt, temperature=0.2, max_tokens=12000)
            if not raw or "Error" in (raw[:80] or ""):
                return await self._generate_fallback_metadata(combined, workspace_id)
            raw_clean = raw.strip()
            if "```" in raw_clean:
                code = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_clean)
                if code:
                    raw_clean = (code.group(1) or "").strip()
            start = raw_clean.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw_clean)):
                    if raw_clean[i] == "{":
                        depth += 1
                    elif raw_clean[i] == "}":
                        depth -= 1
                        if depth == 0:
                            raw_clean = raw_clean[start : i + 1]
                            break
            if not raw_clean or not raw_clean.strip().startswith("{"):
                salvaged = self._salvage_workspace_metadata_from_truncated_json(
                    raw, combined
                )
                salvaged = self._ensure_metadata_complete(salvaged, combined)
                if combined and len(combined.strip()) > 50:
                    ai_desc = await self._generate_case_summary_from_documents(combined)
                    if ai_desc:
                        salvaged["description"] = ai_desc
                if (
                    salvaged.get("caseCategory") != "General"
                    or salvaged.get("subcategories")
                    or salvaged.get("caseTitle")
                ):
                    return salvaged
                return await self._generate_fallback_metadata(combined, workspace_id)
            try:
                data = json.loads(raw_clean)
            except json.JSONDecodeError:
                salvaged = self._salvage_workspace_metadata_from_truncated_json(
                    raw, combined
                )
                salvaged = self._ensure_metadata_complete(salvaged, combined)
                if combined and len(combined.strip()) > 50:
                    ai_desc = await self._generate_case_summary_from_documents(combined)
                    if ai_desc:
                        salvaged["description"] = ai_desc
                if (
                    salvaged.get("caseCategory") != "General"
                    or salvaged.get("subcategories")
                    or salvaged.get("caseTitle")
                ):
                    logger.info(
                        "Used salvaged workspace metadata from truncated response"
                    )
                    return salvaged
                return await self._generate_fallback_metadata(combined, workspace_id)
            case_title = self._normalize_case_title(
                (data.get("caseTitle") or "").strip(), combined
            )
            case_category = (data.get("caseCategory") or "").strip().upper()
            subcategories = data.get("subcategories") or data.get("subcategory") or []
            if isinstance(subcategories, str):
                subcategories = [subcategories] if subcategories else []
            subcategories = [str(s).strip() for s in subcategories if s][:5]
            description = (data.get("description") or "").strip()
            if description:
                # Handle both actual newlines and escaped \n in JSON
                description = description.replace("\\n", "\n")
                # Reject if it looks like raw document paste
                if not self._is_valid_short_summary(description):
                    description = ""
                else:
                    lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
                    # Ensure 3-4 lines: pad if less than 3, truncate if more than 4
                    if len(lines) < 3:
                        while len(lines) < 3:
                            lines.append(
                                "Additional details regarding the legal matter."
                            )
                    elif len(lines) > 4:
                        lines = lines[:4]
                    # Truncate any line that is too long (keep summary short)
                    lines = [ln[:120] if len(ln) > 120 else ln for ln in lines]
                    description = "\n".join(lines)[:500]

            # Build initial result
            result = {
                "caseTitle": case_title,
                "caseCategory": case_category,
                "subcategories": subcategories,
                "description": description,
            }

            # If description is missing, invalid, or generic, generate case-specific summary from documents using AI
            generic_phrases = [
                "Legal matter based on uploaded documents",
                "The case involves parties and issues requiring legal review",
            ]
            need_description = (
                not result.get("description")
                or not self._is_valid_short_summary(result["description"])
                or any(p in (result.get("description") or "") for p in generic_phrases)
            )
            if need_description:
                if not combined or len(combined.strip()) < 50:
                    logger.warning(
                        "Cannot generate case-specific description: no document content available (length: {})",
                        len(combined) if combined else 0,
                    )
                else:
                    # Clear generic description before trying AI generation
                    if result.get("description") and any(
                        p in result["description"] for p in generic_phrases
                    ):
                        result["description"] = ""
                        logger.info(
                            "Cleared generic description, generating case-specific summary from documents ({} chars)",
                            len(combined),
                        )
                    ai_summary = await self._generate_case_summary_from_documents(
                        combined
                    )
                    if ai_summary:
                        result["description"] = ai_summary
                        logger.info(
                            "✅ Generated case-specific description from documents (AI): {}",
                            ai_summary[:80],
                        )
                    elif not result.get("description"):
                        # AI generation failed - try one more time with simpler prompt
                        logger.warning(
                            "First AI description attempt failed, retrying with simpler prompt"
                        )
                        ai_summary = (
                            await self._generate_case_summary_from_documents_simple(
                                combined
                            )
                        )
                        if ai_summary:
                            result["description"] = ai_summary
                            logger.info(
                                "✅ Generated case-specific description (retry): {}",
                                ai_summary[:80],
                            )
                        else:
                            logger.error(
                                "❌ Failed to generate case-specific description after retries - description will be empty"
                            )
                            result["description"] = (
                                ""  # Leave empty rather than use generic
                            )

            # If title is missing, try to generate it from document content before fallback
            if not result["caseTitle"]:
                title_prompt = get_workspace_category_and_subcategory_prompt2(combined=combined)
                try:
                    title_raw = await gemini_client.generate(
                        title_prompt, temperature=0.3, max_tokens=12000
                    )
                    if title_raw:
                        title_raw = title_raw.strip().strip('"').strip("'")
                        if " vs " in title_raw or " v " in title_raw:
                            result["caseTitle"] = (
                                title_raw.split("\n")[0].split(".")[0].strip()
                            )
                        else:
                            result["caseTitle"] = f"{title_raw} vs Matter"
                except Exception:
                    pass  # _ensure_metadata_complete will set fallback

            # Ensure all fields are complete (fills any missing values)
            result = self._ensure_metadata_complete(result, combined)

            logger.info(
                "Workspace metadata: caseTitle={}, caseCategory={}, subcategories={}, description={}",
                result["caseTitle"][:50] if result["caseTitle"] else "",
                result["caseCategory"],
                result["subcategories"],
                result["description"][:50] if result["description"] else "",
            )
            return result
        except Exception as e:
            logger.warning(
                "Workspace category/subcategory generation failed: {}", str(e)
            )
            import traceback

            logger.debug("Traceback: {}", traceback.format_exc())
            desc = "Legal matter based on uploaded documents.\nThe case involves parties and issues requiring legal review.\nThe matter requires appropriate legal action.\nSupporting documents provide further context."
            if "combined" in locals() and combined and len(combined.strip()) > 50:
                ai_desc = await self._generate_case_summary_from_documents(combined)
                if ai_desc:
                    desc = ai_desc
            fallback = {
                "caseTitle": "Petitioner vs Respondent",
                "caseCategory": "CIVIL LAW",
                "subcategories": ["Indian Contract Act, 1872"],
                "description": desc,
            }
            # Try to ensure metadata is complete (in case method exists)
            try:
                fallback = self._ensure_metadata_complete(
                    fallback, combined if "combined" in locals() else ""
                )
            except AttributeError:
                # Method doesn't exist, use fallback as-is
                pass
            return fallback

    async def _heal_devanagari_in_document_assessments(
        self, workspace_id: str
    ) -> bool:
        """Detect Devanagari Hindi text in cached audio transcripts and
        transliterate to Hinglish in place. Idempotent — once a workspace's
        cache is healed, subsequent calls become no-ops because no Devanagari
        is detected. Used to retroactively fix workspaces processed before
        the Hinglish transliteration step was added — without forcing the
        user to re-upload the audio.

        Returns True when at least one transcript was healed (cache updated),
        False otherwise.
        """
        cached = self._workspace_document_assessments_payload.get(workspace_id)
        if not isinstance(cached, dict):
            return False
        inner_payload = cached.get("payload")
        if not isinstance(inner_payload, dict):
            return False
        assessments = inner_payload.get("documentAssessments")
        if not isinstance(assessments, list) or not assessments:
            return False

        # Cheap pre-check: does any audio entry actually carry Devanagari?
        # Skip the LLM round-trip entirely when nothing needs healing.
        candidates_idx: List[int] = []
        for i, entry in enumerate(assessments):
            if not isinstance(entry, dict):
                continue
            transcript_text = entry.get("transcript")
            if (
                isinstance(transcript_text, str)
                and transcript_text.strip()
                and _audio_has_devanagari(transcript_text)
            ):
                candidates_idx.append(i)
        if not candidates_idx:
            return False

        logger.info(
            "Devanagari heal: workspace {} has {} cached audio transcript(s) "
            "with Devanagari; running one-shot Hinglish transliteration",
            workspace_id[:12] if workspace_id else "",
            len(candidates_idx),
        )

        healed_count = 0
        for i in candidates_idx:
            entry = assessments[i]
            original = entry.get("transcript", "")
            try:
                new_text = await asyncio.to_thread(
                    transliterate_formatted_transcript_to_hinglish, original
                )
            except Exception as e:
                logger.warning(
                    "Devanagari heal: failed for entry {!r}: {}",
                    entry.get("fileName") or entry.get("documentId") or i, e,
                )
                continue
            if not isinstance(new_text, str) or not new_text.strip():
                continue
            if new_text == original:
                continue  # Heal returned same string — likely failure.
            entry["transcript"] = new_text
            healed_count += 1

        if healed_count == 0:
            return False

        # Re-assign so the PersistentWorkspaceDict notices the write and
        # mirrors it to Qdrant — subsequent process restarts won't re-heal.
        self._workspace_document_assessments_payload[workspace_id] = cached
        logger.success(
            "✅ Devanagari heal: workspace {} — healed {} cached transcript(s) "
            "to Hinglish; cache updated.",
            workspace_id[:12] if workspace_id else "",
            healed_count,
        )
        return True
