"""
Socket.IO event handlers with Intelligence Chat Agent integration.
Updated to match backend Socket.IO specification.
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

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]

# Labels to strip from reportCaseSummary / derived report text (neutral / report-facing).
_STRATEGY_ROLE_LABELS_RE = re.compile(
    r"\b(?:adjudicator|petitioner|respondent|mediator|client|clients)\b",
    re.I,
)

# Appended to Strategy & Objective replies when the user discusses a new priority informally.
OBJECTIVE_UPDATE_OFFER_QUESTION = (
    "Do you want me to update the whole workspace based on this new objective "
    "that you have defined?"
)

# ---------------------------------------------------------------------------
# Intelligence-chat document retrieval (hybrid: semantic search + stable
# annexure numbering). Tuned for gemini-2.5-flash's 1M-token window — a missed
# document (hallucination) costs far more than extra context tokens.
# ---------------------------------------------------------------------------
CHAT_SMALL_WORKSPACE_DOC_THRESHOLD = 15  # at/below this, include every doc
CHAT_SEARCH_CHUNK_LIMIT = 40  # chunks fetched before dedup-to-document
CHAT_MAX_DOCS_INCLUDED = 12  # distinct docs included in large workspaces
CHAT_MAX_CHARS_PER_DOC = 40000  # ~10K tokens/doc
CHAT_MAX_TOTAL_CHARS = 400000  # ~100K tokens of annexure context
CHAT_MIN_SCORE_THRESHOLD = 0.30  # soft signal only (fallback trigger), never a hard drop

# Explicit annexure references in a user message, e.g. "Annexure 3", "annexure-12",
# "Annexure IV". Group 1 is an arabic integer or a roman numeral.
_ANNEXURE_REF_RE = re.compile(
    r"annexure[\s\-_]*([0-9]{1,3}|[ivxlcdm]+)\b",
    re.I,
)

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

# Injected into every prompt that generates / regenerates case-summary,
# report-case-summary, or strategic-objective prose. Audio-transcript annexures
# label speakers with diarization placeholders ("Caller", "Receiver",
# "Speaker A", "Speaker B"); those are NOT party names and must never surface
# in the generated narrative — the "name parties exactly as written" rule does
# NOT extend to them. See app/processing/audio_transcription.py speaker labels.
SPEAKER_LABEL_GUARDRAIL = (
    "SPEAKER / DIARIZATION LABELS (CRITICAL — applies to caseSummary, "
    "reportCaseSummary, strategicObjective, possibleNextSteps, and "
    "suggestionChips): Audio-recording annexures are transcribed with "
    "speaker placeholders such as \"Caller\", \"Receiver\", \"Speaker A\", "
    "\"Speaker B\" (and timestamps like [MM:SS]). These are diarization "
    "placeholders, NOT the names of the parties — the rule to name parties "
    "exactly as written does NOT apply to them. NEVER write the words "
    "\"Caller\", \"Receiver\", \"Speaker A\", or \"Speaker B\" in your output. "
    "Instead, identify who each speaker actually is from the surrounding facts "
    "and refer to them by their real name or factual designation (e.g. the "
    "complainant, the accused, the recipient of the call, the advocate). If a "
    "speaker's real identity cannot be determined from the documents, use a "
    "neutral factual descriptor — never the placeholder label itself."
)


def _roman_or_int(token: str) -> Optional[int]:
    """Parse an arabic integer or roman numeral to int; None if unparseable."""
    token = (token or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    if not all(ch in _ROMAN_VALUES for ch in token):
        return None
    total = 0
    prev = 0
    for ch in reversed(token):
        val = _ROMAN_VALUES[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total if total > 0 else None


def _derive_report_case_summary(case_summary: str) -> str:
    """Best-effort neutral summary when the model omits reportCaseSummary."""
    if not (case_summary or "").strip():
        return ""
    t = case_summary.strip()
    t = re.sub(
        r"^This\s+case\s+is\s+about\b",
        "This matter is about",
        t,
        count=1,
        flags=re.I,
    )
    t = re.sub(r"\bCase\b", "matter", t)
    t = re.sub(
        r"\bfor\s+(?:the\s+)?(?:adjudicator|petitioner|respondent|mediator|client|clients)\s+role\b",
        "based on the available materials",
        t,
        flags=re.I,
    )
    t = _STRATEGY_ROLE_LABELS_RE.sub("", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,;:])", r"\1", t)
    return t.strip()


def _coerce_analysis_text_item(value: Any) -> str:
    """
    Normalize a single analysis-report list item into a clean display string.

    Risks / weaknesses / missingInfo / relevantJurisdiction in the analysis
    report schema are `List[str]`, but the LLM occasionally returns them in
    the same `{"point": "...", "laws": [...]}` shape used for strengths.
    Without normalization, those entries leak into the emitted payload as
    Python dict reprs (e.g. ``"{'point': '...', 'laws': [...]}"``), which
    is exactly what the user sees in the Risks & Weaknesses panel.

    Coercion rules:
      - ``None``                                  → ``""``
      - ``str``                                   → stripped string
      - ``dict`` with ``point``/``text``/``description``/``value`` keys
                                                  → that text, stripped
      - any other type                            → ``str(value).strip()``
        (only as a last resort; we never want to surface a dict literal)
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _strip_enum_prefix(value.strip())
    if isinstance(value, dict):
        for key in ("point", "text", "description", "value", "title"):
            candidate = value.get(key) if hasattr(value, "get") else None
            if isinstance(candidate, str) and candidate.strip():
                return _strip_enum_prefix(candidate.strip())
        # Fall back to the first non-empty string value (e.g. nested
        # custom keys) before giving up.
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return _strip_enum_prefix(v.strip())
        return ""
    if isinstance(value, (list, tuple, set)):
        # Defensive: surface the first text-like element.
        for inner in value:
            coerced = _coerce_analysis_text_item(inner)
            if coerced:
                return coerced
        return ""
    return _strip_enum_prefix(str(value).strip())


# Enumeration / bullet prefixes that the LLM sometimes prepends to list items
# in regenerations even though we asked for plain strings. Stripping them at
# the coercion boundary guarantees that "1. Bank statement" (new) and
# "Bank statement" (prev) are treated as the same item by every downstream
# merge / dedup pass — without it, the prev-vs-new LLM merge sees two visibly
# different strings and lets the duplicate through.
_ENUM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\(\d+\)"                       # (1) (12)
    r"|\d+[.):\-]"                   # 1.  1)  1:  1-
    r"|[a-zA-Z][.):]"                # a.  a)  a:
    r"|[ivxlcdmIVXLCDM]+[.)]"        # i.  ii)  iii.
    r"|[•·\*—–]"                     # • · * — –
    r"|-(?=\s)"                      # leading hyphen as a bullet, only when
                                      # followed by whitespace (avoids
                                      # eating the hyphen in "9-7000")
    r")\s+"
)


def _strip_enum_prefix(text: str) -> str:
    """Strip a single leading enumeration / bullet prefix from a list item."""
    if not text:
        return text
    stripped = _ENUM_PREFIX_RE.sub("", text, count=1)
    return stripped.strip() or text


# UI-truncation guard for case-summary / strategic-objective narratives.
# OCR of mobile/web app screenshots (UPI billers directories, recent-payees
# lists, vendor catalogs) captures the UI's own visual truncation ("…" or
# "..."). The strategy LLM is told to reproduce names verbatim and ends up
# leaking those clipped labels into caseSummary / reportCaseSummary /
# strategicObjective as if they were real entities ("Smart Prep…",
# "Uttar Pradesh Power Corporatio…", "MSEDCL Mahavitaran - Maharashtra ...").
#
# Match constraints (deliberately conservative so prose ellipses
# "the witness said ..." aren't mauled):
#   - First word: Title Case word or ACRONYM (≥1 leading uppercase).
#   - Continuation words: Title Case / acronym / parenthetical acronym /
#     short lowercase connectors (≤3 chars: of, and, the, &, -).
#   - Immediately followed by a 3-dot or U+2026 truncation marker.
# A typical prose sentence ("he did not see the accused...") fails on the
# very first lowercase content word and the regex backtracks to no-match.
_UI_TRUNCATION_RE = re.compile(
    r"\b("
    r"(?:[A-Z][A-Za-z0-9&]*)"
    r"(?:[\s\-&]+"
    r"(?:[A-Z][A-Za-z0-9&]*"
    r"|\([A-Z][A-Za-z0-9&]*\)"
    r"|[a-z]{1,3}"
    r"))*"
    r")\s*(?:\.{3}|…)"
)


def _strip_ui_truncated_tokens(text: str) -> str:
    """Remove UI-truncated entity labels (token ending in ... or …) that
    survived the prompt-level filter. Idempotent and safe on None / empty
    input. Applied only to narrative case-summary fields where we know the
    consumer is a user-facing legal description (not raw OCR storage).

    The grammar-repair pass below only runs when the regex actually
    matched something — otherwise prose like "And then the court ruled..."
    or "etc..." would have its legitimate leading "And" or trailing
    "..." snipped off.
    """
    if not text or not isinstance(text, str):
        return text or ""
    if not _UI_TRUNCATION_RE.search(text):
        return text
    cleaned = _UI_TRUNCATION_RE.sub("", text)
    # Repair commas / connectors orphaned by the removed labels so the
    # surrounding sentence stays grammatical.
    cleaned = re.sub(r"\s*,\s*,", ",", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*(?=[.;])", "", cleaned)
    cleaned = re.sub(r"\band\s*,", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\band\s+(?=[.;])", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\band\s*$", "", cleaned, flags=re.IGNORECASE)
    # Second pass on ", ." that only emerges after the "and" stripping.
    cleaned = re.sub(r",\s*(?=[.;])", "", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    # Orphan "and " / leading comma at sentence start when the removed
    # label opened the sentence. Tolerates leading whitespace left by
    # the strip.
    cleaned = re.sub(r"^\s*(?:and\s+|,\s*)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(\n\s*)(?:and\s+|,\s*)", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


# Matches the application's own brand/platform name in any casing/spacing
# ("SimplerToday", "Simpler Today", "simplertoday.ai"). The platform name is
# never a case party/fact, but it appears in the platform-generated intake PDF
# ("Contacted SimplerToday.ai on: …"), so the model sometimes echoes it into
# generated panels. scrub_app_brand removes it from output payloads.
_APP_BRAND_RE = re.compile(r"\bsimpler\s*today(?:\s*\.?\s*ai)?\b", re.IGNORECASE)


def scrub_app_brand(obj: Any) -> Any:
    """Recursively remove the app/platform brand name from a generated payload
    (str / list / dict) and tidy the punctuation/whitespace it leaves behind.
    Idempotent; non-str/list/dict values are returned unchanged.
    """
    if isinstance(obj, str):
        if not _APP_BRAND_RE.search(obj):
            return obj
        s = _APP_BRAND_RE.sub("", obj)
        # Tidy connectors/punctuation orphaned by the removed token so the
        # surrounding sentence stays grammatical, e.g.
        # "by SimplerToday.ai, a hair transplant office" -> "by a hair transplant office".
        # Order matters: drop the dangling "<connector> ," BEFORE collapsing
        # "<space>," (which would otherwise erase the space the connector
        # pattern needs to match).
        s = re.sub(
            r"\b(by|from|with|at|of|to|against)\s*,\s*", r"\1 ", s, flags=re.IGNORECASE
        )
        s = re.sub(r"\s+,", ",", s)
        s = re.sub(r",\s*,", ",", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s
    if isinstance(obj, list):
        return [scrub_app_brand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_app_brand(v) for k, v in obj.items()}
    return obj


def _coerce_analysis_text_list(items: Any) -> List[str]:
    """Apply `_coerce_analysis_text_item` to every entry, dropping empties."""
    if not isinstance(items, list):
        if items is None:
            return []
        items = [items]
    out: List[str] = []
    for x in items:
        s = _coerce_analysis_text_item(x)
        if s:
            out.append(s)
    return out


_STRENGTH_POINT_MIN_LEN = 60


def _strip_strength_point_statute_boilerplate(text: str) -> str:
    """
    Remove trailing citation boilerplate from analysis strength points (statutes belong in `laws`, not prose).
    Strips phrases like 'which is a relevant fact under …' and 'admissible under …'.

    Guard: if stripping would collapse the point below ``_STRENGTH_POINT_MIN_LEN``
    characters (i.e. the model produced a label + boilerplate single-sentence form
    like "Youth of the accused, which is a relevant fact under <Act>, YYYY."), we
    return the ORIGINAL prose instead. The universal law-strip in ``agent.py`` will
    then remove the citation in a context-aware way without collapsing the whole
    point to a two-word fragment.
    """
    if not (text or "").strip():
        return ""
    original = text.strip()
    t = original
    for _ in range(5):
        prev = t
        t = re.sub(
            r"\s*,?\s*which\s+is\s+a\s+relevant\s+fact\s+under[^.]*\.",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            r"\s*,?\s*admissible\s+under[^.]*\.",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            r"\s*,?\s*which\s+is\s+a\s+relevant\s+fact\s+under[^.]*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            r"\s*,?\s*admissible\s+under[^.]*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
        if t == prev:
            break
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*,\s*$", "", t)
    cleaned = t.strip()
    # Preserve the original when stripping collapsed it to a sub-threshold label.
    # The universal law-strip will neutralise the citation downstream without
    # destroying the surrounding prose.
    if len(cleaned) < _STRENGTH_POINT_MIN_LEN and len(cleaned) < len(original):
        return original
    return cleaned


def _annexure_wording_for_next_steps(text: str) -> str:
    """
    Product wording for strategy possibleNextSteps: refer to uploads as Annexure N, not Document N;
    replace standalone document/documents with annexure/annexures.
    """
    if not (text or "").strip():
        return ""
    t = text.strip()
    t = re.sub(r"\b[Dd]ocuments\s+(\d+)\b", r"Annexures \1", t)
    t = re.sub(r"\b[Dd]ocument\s+(\d+)\b", r"Annexure \1", t)
    t = re.sub(
        r"\bdocuments\b",
        "annexures",
        re.sub(r"\bdocument\b", "annexure", t, flags=re.IGNORECASE),
        flags=re.IGNORECASE,
    )
    return t


_ANNEXURE_SINGLE_RE = re.compile(r"\bAnnexure\s+(\d+)\b", re.IGNORECASE)
_ANNEXURE_PLURAL_RE = re.compile(
    r"\bAnnexures\s+(\d+(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s+)\d+)*)\b",
    re.IGNORECASE,
)


def _clamp_annexure_references(text: Any, max_annexure: int) -> Any:
    """
    Rewrite any "Annexure N" / "Annexures N, M" reference where N > max_annexure
    down to the highest valid index (1..max_annexure). When max_annexure <= 0,
    strip the phrase entirely so the output never falsely attributes facts to
    annexures that were never uploaded.

    Non-string inputs are returned unchanged (callers may pass through
    arbitrary JSON values from the model).
    """
    if not isinstance(text, str) or not text:
        return text
    if max_annexure <= 0:
        text = _ANNEXURE_PLURAL_RE.sub("", text)
        text = _ANNEXURE_SINGLE_RE.sub("", text)
        return re.sub(r"\s{2,}", " ", text).strip()

    def _clamp_plural(match: re.Match) -> str:
        nums = [int(x) for x in re.findall(r"\d+", match.group(1))]
        clamped: List[int] = []
        for n in nums:
            v = min(n, max_annexure)
            if v not in clamped:
                clamped.append(v)
        if not clamped:
            return ""
        if len(clamped) == 1:
            return f"Annexure {clamped[0]}"
        if len(clamped) == 2:
            return f"Annexures {clamped[0]} and {clamped[1]}"
        return (
            "Annexures "
            + ", ".join(str(v) for v in clamped[:-1])
            + f" and {clamped[-1]}"
        )

    def _clamp_single(match: re.Match) -> str:
        n = int(match.group(1))
        return f"Annexure {min(n, max_annexure)}"

    text = _ANNEXURE_PLURAL_RE.sub(_clamp_plural, text)
    text = _ANNEXURE_SINGLE_RE.sub(_clamp_single, text)
    return text


def _format_document_for_prompt(
    doc: Dict[str, Any],
    index: int,
    total: int,
    *,
    label_kind: str = "Annexure",
) -> str:
    """Render a single Qdrant document as a labelled prompt block.

    Prepends a metadata header (DOCUMENT TYPE, ISSUED BY, ADDRESSEES,
    SUBJECT, KEY DATES, LEGAL PROVISIONS CITED, LANGUAGE, DRAFTED BY
    ASSISTANT) above the raw body, so the regeneration prompts can
    reason about each document as structured evidence instead of
    treating every annexure as undifferentiated text. The metadata
    keys come from `extracted_fields` that the document processor
    already populates.

    The `DRAFTED BY ASSISTANT` line is emitted only when
    `extracted_fields.drafted_by_assistant` is True (set in
    `app/processing/document_processor.py` based on the assistant-
    draft filename pattern). It signals that the underlying
    procedural action has already been performed by the client side
    in this workspace, so panels should treat it as a completed
    milestone — not a pending action.
    """
    metadata = doc.get("metadata") or {}
    extracted = metadata.get("extracted_fields") or {}
    document_type = (metadata.get("document_type") or "").strip()
    body = (doc.get("content") or "").strip()

    label = (
        f"{label_kind} {index} (most recently uploaded)"
        if index == total
        else f"{label_kind} {index}"
    )

    header_lines: List[str] = []
    if document_type and document_type.lower() != "other":
        header_lines.append(f"[DOCUMENT TYPE: {document_type}]")

    def _coerce_str_list(val: Any) -> List[str]:
        if val is None:
            return []
        if isinstance(val, str):
            s = val.strip()
            return [s] if s else []
        if isinstance(val, (list, tuple)):
            return [str(v).strip() for v in val if str(v).strip()]
        return [str(val).strip()]

    issued_by = (
        extracted.get("issuing_party_or_deponent")
        or extracted.get("issued_by")
        or ""
    )
    client_name = extracted.get("client_name") or ""
    if issued_by:
        issued_line = f"[ISSUED BY: {issued_by}"
        if client_name and client_name.lower() not in issued_by.lower():
            issued_line += f" — representing {client_name}"
        issued_line += "]"
        header_lines.append(issued_line)
    elif client_name:
        header_lines.append(f"[CLIENT: {client_name}]")

    addressees = _coerce_str_list(extracted.get("addressee"))
    if addressees:
        header_lines.append(f"[ADDRESSEES: {'; '.join(addressees)}]")

    subject = (extracted.get("subject") or "").strip()
    if subject:
        # Keep subject short to avoid duplicating the entire body
        if len(subject) > 200:
            subject = subject[:200].rstrip() + "…"
        header_lines.append(f"[SUBJECT: {subject}]")

    # Surface key dates (date of issue, due date, notice period).
    date_bits: List[str] = []
    issue_date = (extracted.get("date") or extracted.get("issue_date") or "").strip()
    if issue_date:
        date_bits.append(f"issued {issue_date}")
    due_date = (extracted.get("due_date") or extracted.get("deadline") or "").strip()
    if due_date:
        date_bits.append(f"due {due_date}")
    notice_period = (extracted.get("notice_period") or "").strip()
    if notice_period:
        date_bits.append(f"notice period {notice_period}")
    if date_bits:
        header_lines.append(f"[KEY DATES: {'; '.join(date_bits)}]")

    laws = _coerce_str_list(extracted.get("legal_provisions"))
    if laws:
        # Cap the law list so one verbose doc does not eat the prompt budget.
        if len(laws) > 6:
            laws = laws[:6] + [f"…and {len(laws) - 6} more"]
        header_lines.append(f"[LEGAL PROVISIONS CITED: {'; '.join(laws)}]")

    language = (extracted.get("language") or "").strip()
    if language and language.lower() not in {"english", "en"}:
        header_lines.append(f"[LANGUAGE: {language}]")

    if extracted.get("drafted_by_assistant"):
        drafted_at = (extracted.get("drafted_at") or "").strip()
        if drafted_at:
            header_lines.append(
                f"[DRAFTED BY ASSISTANT on {drafted_at} — represents a "
                "COMPLETED ACTION by the client side in this workspace]"
            )
        else:
            header_lines.append(
                "[DRAFTED BY ASSISTANT — represents a COMPLETED ACTION "
                "by the client side in this workspace]"
            )

    header_block = "\n".join(header_lines)
    if header_block:
        return f"--- {label} ---\n{header_block}\n\n{body}"
    return f"--- {label} ---\n{body}"


_OVERVIEW_CONTACTED_DATE_RE = re.compile(
    r"Contacted\s+SimplerToday(?:\.ai)?\s+on:\s*"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
    re.I,
)
_OVERVIEW_TO_PHONE_RE = re.compile(
    r"To:\s*(\+?91[\s\-]*\d[\d\s\-]{8,18})",
    re.I,
)


def _normalize_report_mobile_from_overview(raw: str) -> Optional[str]:
    """Format mobile from Overview 'To:' line as '+91 XXXXX XXXXX'. Returns None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 12 and digits.startswith("91"):
        national = digits[2:12]
    elif len(digits) == 11 and digits.startswith("0"):
        national = digits[1:11]
    elif len(digits) == 10:
        national = digits
    else:
        return None
    if len(national) != 10 or national[0] not in "6789":
        return None
    return f"+91 {national[:5]} {national[5:]}"


def _overview_conversation_date_to_iso(day: str, month: str, year: str) -> Optional[str]:
    """DD/MM/YYYY (or DD-MM-YYYY) from Overview header → YYYY-MM-DD."""
    try:
        d, m, y = int(day), int(month), int(year)
        date_type(y, m, d)
        return f"{y:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return None


def extract_overview_conversation_report_contact_meta(
    all_docs: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Lawyers may upload 'Overview of the Conversation' PDFs with a fixed top block:
      To: +91 ...
      Contacted SimplerToday.ai on: DD/MM/YYYY
    Values are taken only from that template in indexed text — never invented.
    Missing pieces → None (JSON null). First matching document wins (upload order).
    """
    for doc in all_docs or []:
        content = (doc.get("content") or "").strip()
        if len(content) < 20:
            continue
        m_date = _OVERVIEW_CONTACTED_DATE_RE.search(content)
        if not m_date:
            continue
        iso_date = _overview_conversation_date_to_iso(
            m_date.group(1), m_date.group(2), m_date.group(3)
        )
        m_phone = _OVERVIEW_TO_PHONE_RE.search(content)
        mobile = (
            _normalize_report_mobile_from_overview(m_phone.group(1))
            if m_phone
            else None
        )
        return mobile, iso_date
    return None, None


class SocketEventHandler:
    """
    Handles Socket.IO events and routes them to appropriate handlers.

    Per backend specification, AI service handles:
    - workspace_created: New workspace created
    - workspace_sync: Workspace updated
    - user_message_request: User sent chat message

    And emits:
    - ai_response: AI response back to server
    """

    # Throttle probing_prompts_response to avoid burst of emits that can trigger server disconnect
    PROBING_THROTTLE_SECONDS = 1.0

    def __init__(self):
        self._handlers: Dict[str, EventHandler] = {}
        self._socket_client = None  # Will be set by main service
        # NOTE: every per-workspace cache below is a PersistentWorkspaceDict so
        # that writes (`self._workspace_X[workspace_id] = ...`) are mirrored to
        # Qdrant in the background. After an AI service restart we hydrate the
        # cache by reading these same kinds back via `restore_state`.
        self._workspace_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("strategy_suggestion_chips")
        )  # workspace_id -> 15-16 strategy chips
        self._workspace_analysis_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("analysis_suggestion_chips")
        )  # workspace_id -> 15-16 analysis-report chips
        self._workspace_chat_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("chat_suggestion_chips")
        )  # workspace_id -> 10-12 contextual chips (refreshed per AI reply; we return random 4 on each request)
        # Drafting Assistant — separate chip cache so the Drafting panel
        # never serves the intelligence-chat chips. Keyed by either
        # actionable_id (per draft session) or workspace_id (panel-level
        # fallback when the polling request doesn't carry actionableId).
        # In-memory only; chips are cheap to regenerate.
        self._workspace_drafting_suggestion_chips: Dict[str, List[str]] = {}
        self._last_workspace_id_with_chips: Optional[str] = (
            None  # fallback when request workspace has no chips
        )
        self._last_probing_emit_time: Dict[str, float] = (
            {}
        )  # workspace_id -> time of last probing_prompts_response emit
        # Cache last document_feature_response payloads per workspace so we can re-emit reliably after reconnect
        # (backend sometimes disconnects the socket during long AI work)
        # - document_assessments: initial assessment for all documents
        # - workspace_metadata: derived case title/category/description
        # - strategy_objective: detailed strategy/opinion
        # - analysis_report: score + strengths/weaknesses
        # - list_of_dates: timeline extracted from documents
        self._workspace_document_assessments_payload: Dict[str, Any] = (
            PersistentWorkspaceDict(CacheKinds.DOCUMENT_ASSESSMENTS)
        )
        self._workspace_metadata_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.WORKSPACE_METADATA_PAYLOAD
        )
        self._workspace_strategy_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.STRATEGY_PAYLOAD
        )
        self._workspace_analysis_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.ANALYSIS_REPORT
        )
        # Per-workspace cache of the extracted case_entities dict so a single
        # generation cycle that runs strategy → analysis_report extracts the
        # entities ONCE. Keyed by workspace_id, value is (content_sha1, entities).
        # The content hash automatically busts the cache when documents change.
        self._workspace_case_entities_cache: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        # Per-workspace memory of rejected analysis-report items so that an
        # objective-change regeneration does not silently re-introduce a
        # missingInfo / weakness / risk the user already dismissed via
        # analysis_report_feedback. Shape per workspace_id:
        #   {"missingInfo": [str, ...], "riskOrWeakness": [str, ...]}
        self._workspace_rejected_analysis_items: Dict[str, Dict[str, List[str]]] = (
            PersistentWorkspaceDict(CacheKinds.REJECTED_ANALYSIS_ITEMS)
        )
        # List of Dates timeline per workspace (chronological events from case documents)
        self._workspace_list_of_dates_payload: Dict[str, List[Dict[str, Any]]] = (
            PersistentWorkspaceDict(CacheKinds.LIST_OF_DATES)
        )
        # Applicable laws payload per workspace (acts/sections inferred from docs + case law search)
        self._workspace_applicable_laws_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.APPLICABLE_LAWS)
        )
        # Procedural timeline payload per workspace (upcoming filing/hearing/compliance/evidence/adjournment dates)
        self._workspace_procedural_timeline_payload: Dict[
            str, List[Dict[str, Any]]
        ] = PersistentWorkspaceDict(CacheKinds.PROCEDURAL_TIMELINE)
        # Drafting Assistant — proactive suggested drafts panel per workspace.
        # Shape: {"suggestedDrafts": [{"title","relevance","description"}, ...]}
        self._workspace_drafting_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.DRAFTING_SUGGESTIONS)
        )
        # In-flight request_template_sync correlations: requestId -> Future,
        # resolved by on_template_sync_response once the snapshot is ingested
        # into the in-memory template store. Lets the suggestion/kickoff path
        # await a cold-cache fill before matching.
        self._pending_template_sync: Dict[str, "asyncio.Future"] = {}
        # Per-workspace drafting-suggestion provenance, used to self-heal the
        # panel when a template finishes ingesting *after* suggestions were
        # built (e.g. a slow scanned-PDF OCR that misses the template-sync
        # window). Shape: workspace_id -> {"user_id", "role", "lib_count"},
        # where lib_count is the ready-template count the suggestions were
        # pre-matched against. When the library later grows, the panel is
        # regenerated. See _maybe_reemit_drafting_on_templates.
        self._drafting_meta_by_workspace: Dict[str, Dict[str, Any]] = {}
        # Re-emit after reconnect when socket dropped right after analysis report (so backend gets the update)
        self._had_disconnect: bool = False
        self._pending_reemit_workspace_id: Optional[str] = None
        self._pending_reemit_user_id: Optional[str] = None
        # Per-workspace coalescing for analysis_report_feedback. Bulk-reject sends
        # N events in ~2ms; processing them sequentially under a lock is correct
        # but takes ~19s × N (Gemini scoring + explanation regeneration). We
        # instead debounce: events accumulate in a queue, and a single batch
        # flush kicks off ~500ms after the last event, removing every rejected
        # item at once and emitting ONE updated report. The lock still guards
        # the flush itself so a re-burst can't race a flush in flight.
        self._analysis_feedback_locks: Dict[str, asyncio.Lock] = {}
        self._pending_feedback_queue: Dict[str, List[Dict[str, Any]]] = {}
        self._pending_feedback_task: Dict[str, asyncio.Task] = {}
        # Window for collecting bulk-reject bursts before flushing.
        self._feedback_debounce_seconds: float = 0.5
        # Max total score bump from a single batch (each valid rejection
        # contributes 1–5; capping the sum prevents huge scores when the user
        # rejects many items at once).
        self._feedback_batch_max_score_delta: int = 15
        # Chat attachment cache: chat_id -> list of {fileName, content} so follow-up questions in same chat see the document
        self._chat_attachment_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        # Intelligence chat: last assistant text per chat_id (client may omit conversation_history)
        self._intelligence_last_ai_by_chat: Dict[str, str] = {}
        # Persistent role cache keys:
        # - per chat: f"{workspace_id}:{user_id}:{chat_id}" -> role
        # - per user/workspace: f"{workspace_id}:{user_id}" -> role
        self._role_cache_by_chat: Dict[str, str] = {}
        self._role_cache_by_user_workspace: Dict[str, str] = {}
        # Last ai_response payload; re-emit after reconnect when socket dropped right after emit so backend receives it
        self._pending_ai_response: Optional[Dict[str, Any]] = None
        # De-dup guard for replayed workspace_sync events (e.g., reconnect replay).
        # workspace_id -> {"fingerprint": str, "ts": float}
        self._last_workspace_sync_fingerprint: Dict[str, Dict[str, Any]] = {}
        # Guard against overlapping analysis-report refresh tasks from chat marker misses.
        self._analysis_refresh_inflight: Set[str] = set()
        # During objective-change regeneration, suppress stale background analysis_report emits
        # from older pipelines so FE receives only the fresh objective-aligned report.
        self._suppress_analysis_emit_until_ts: Dict[str, float] = {}
        self._register_default_handlers()

    async def _hydrate_workspace_caches(
        self,
        workspace_id: str,
        *,
        user_id: Optional[str] = None,
    ) -> None:
        """Repopulate in-memory per-workspace caches from Qdrant.

        Runs at the start of chat and document-feature handlers so that an AI
        service restart does not silently degrade chat with empty annexures,
        missing analysis report and missing applicable laws. No-ops when the
        in-memory cache already has data for this workspace (the live process
        is the source of truth).
        """
        if not workspace_id:
            return
        try:
            # Workspace context (title, status, role, evidence files, metadata)
            from app.workspace.context import context_manager

            existing_ctx = context_manager.get_context(workspace_id)
            need_meta_hydration = (
                existing_ctx is None
                or not (existing_ctx.evidence_files or [])
                or not existing_ctx.metadata
            )
        except Exception:
            existing_ctx = None
            need_meta_hydration = True

        # Skip work entirely when every cache we care about is already warm.
        all_warm = (
            not need_meta_hydration
            and bool(self._workspace_analysis_payload.get(workspace_id))
            and bool(self._workspace_applicable_laws_payload.get(workspace_id))
            and bool(self._workspace_procedural_timeline_payload.get(workspace_id))
            and bool(self._workspace_list_of_dates_payload.get(workspace_id))
        )
        if all_warm:
            return

        try:
            cached_kinds = await hydrate_workspace_state(workspace_id)
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug(
                "Workspace cache hydration failed (workspace={}): {}",
                workspace_id,
                e,
            )
            cached_kinds = {}

        if not cached_kinds:
            return

        meta_payload = cached_kinds.get(CacheKinds.WORKSPACE_META)
        if isinstance(meta_payload, dict):
            try:
                from app.workspace.context import context_manager

                if existing_ctx is not None:
                    existing_ctx.apply_cache_dict(meta_payload)
                else:
                    await context_manager.hydrate_context_from_cache(
                        workspace_id=workspace_id, user_id=user_id
                    )
            except Exception as e:
                logger.debug(
                    "Workspace context hydration failed (workspace={}): {}",
                    workspace_id,
                    e,
                )

        # Hydrate per-kind in-memory dicts only when nothing is in memory yet.
        kind_to_dict = {
            CacheKinds.ANALYSIS_REPORT: self._workspace_analysis_payload,
            CacheKinds.APPLICABLE_LAWS: self._workspace_applicable_laws_payload,
            CacheKinds.PROCEDURAL_TIMELINE: self._workspace_procedural_timeline_payload,
            CacheKinds.LIST_OF_DATES: self._workspace_list_of_dates_payload,
            CacheKinds.WORKSPACE_METADATA_PAYLOAD: self._workspace_metadata_payload,
            CacheKinds.DOCUMENT_ASSESSMENTS: self._workspace_document_assessments_payload,
            CacheKinds.STRATEGY_PAYLOAD: self._workspace_strategy_payload,
            CacheKinds.REJECTED_ANALYSIS_ITEMS: self._workspace_rejected_analysis_items,
            CacheKinds.DRAFTING_SUGGESTIONS: self._workspace_drafting_payload,
        }
        for kind, target in kind_to_dict.items():
            payload = cached_kinds.get(kind)
            if payload is None:
                continue
            if target.get(workspace_id):
                continue
            # Defensive normalization: historical analysis-report payloads
            # may still contain dict-shaped risks / weaknesses / missingInfo
            # entries that would otherwise render as Python dict literals
            # in the FE. Clean them up at hydration time so the in-memory
            # cache (and every downstream emit) stays well-formed.
            if kind == CacheKinds.ANALYSIS_REPORT and isinstance(payload, dict):
                try:
                    self._normalize_analysis_report_lists(
                        payload,
                        max_annexure=self._get_annexure_count(workspace_id),
                    )
                except Exception as norm_err:
                    logger.debug(
                        "Analysis report hydration normalization skipped "
                        "(workspace={}): {}",
                        workspace_id,
                        norm_err,
                    )
            try:
                if isinstance(target, PersistentWorkspaceDict):
                    target.hydrate_from_cache(workspace_id, payload)
                else:
                    target[workspace_id] = payload
            except Exception as e:
                logger.debug(
                    "Cache rehydration skipped (kind={}, workspace={}): {}",
                    kind,
                    workspace_id,
                    e,
                )

        ci_payload = cached_kinds.get(CacheKinds.CUSTOM_INSTRUCTIONS)
        if isinstance(ci_payload, dict):
            try:
                hydrate_custom_instructions_from_cache(workspace_id, ci_payload)
            except Exception:
                pass

        logger.info(
            "Hydrated workspace caches from Qdrant: {} (kinds={})",
            workspace_id,
            sorted(cached_kinds.keys()),
        )

    async def _remember_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        role: str,
        *,
        chat_id: Optional[str] = None,
    ) -> None:
        role_upper = (role or "").strip().upper()
        if role_upper not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
            return
        ws_user_key = f"{workspace_id}:{user_id}"
        self._role_cache_by_user_workspace[ws_user_key] = role_upper
        if chat_id:
            self._role_cache_by_chat[f"{workspace_id}:{user_id}:{chat_id}"] = role_upper
        try:
            from app.vectorstore.qdrant import vector_store

            await vector_store.upsert_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                role=role_upper,
                chat_id=chat_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to persist role cache in vector DB (workspace={}, user={}): {}",
                workspace_id,
                user_id,
                e,
            )

    async def _restore_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        *,
        chat_id: Optional[str] = None,
    ) -> Optional[str]:
        if chat_id:
            in_mem_chat = self._role_cache_by_chat.get(f"{workspace_id}:{user_id}:{chat_id}")
            if in_mem_chat:
                return in_mem_chat
        in_mem_user = self._role_cache_by_user_workspace.get(f"{workspace_id}:{user_id}")
        if in_mem_user:
            return in_mem_user
        try:
            from app.vectorstore.qdrant import vector_store

            from_vector_db = await vector_store.get_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                chat_id=chat_id,
            )
            role_upper = str(from_vector_db or "").strip().upper()
            if role_upper in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                self._role_cache_by_user_workspace[f"{workspace_id}:{user_id}"] = role_upper
                if chat_id:
                    self._role_cache_by_chat[f"{workspace_id}:{user_id}:{chat_id}"] = role_upper
                return role_upper
        except Exception as e:
            logger.warning(
                "Failed to restore role cache from vector DB (workspace={}, user={}): {}",
                workspace_id,
                user_id,
                e,
            )
        return None

    async def _resolve_workspace_role(
        self,
        workspace_id: str,
        user_id: str,
        *,
        preferred_role: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> str:
        """
        Resolve the effective workspace role before generation.

        Priority:
        1) Explicit role from current payload
        2) In-memory workspace context
        3) Persisted role cache (Qdrant-backed)
        4) ADJUDICATOR fallback
        """
        valid_roles = {"ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"}
        normalized_preferred = (preferred_role or "").strip().upper()
        if normalized_preferred in valid_roles:
            return normalized_preferred

        try:
            from app.workspace.context import context_manager

            ctx = context_manager.get_context(workspace_id)
            if ctx and ctx.workspace_role:
                ctx_role = (ctx.workspace_role or "").strip().upper()
                if ctx_role in valid_roles:
                    return ctx_role
        except Exception:
            pass

        restored_role = await self._restore_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            chat_id=chat_id,
        )
        if restored_role in valid_roles:
            # Keep in-memory context aligned after process restarts/reconnects.
            try:
                from app.workspace.context import context_manager

                context_manager.upsert_workspace(
                    workspace_id,
                    user_id,
                    workspace_role=restored_role,
                )
            except Exception:
                pass
            return restored_role

        return "ADJUDICATOR"

    def set_socket_client(self, client):
        """Set the socket client for emitting responses."""
        self._socket_client = client
        logger.debug("Socket client registered with event handler")

    def _get_generation_system_instruction(self, workspace_role: Optional[str]) -> str:
        """Use one consistent legal system instruction across intelligence generations."""
        return get_legal_intelligence_system_instruction(
            current_role=(workspace_role or "").strip().upper() or None,
            annexures_uploaded=True,
            role_confirmed=True,
        )

    def _register_default_handlers(self) -> None:
        """Register default event handlers."""
        # Connection events
        self.register("connect", self.on_connect)
        self.register("connected", self.on_connected)
        self.register("disconnect", self.on_disconnect)
        self.register("error", self.on_error)

        # Backend-specified events (AI_Maverick listens to these)
        self.register("workspace_created", self.on_workspace_created)
        self.register("workspace_sync", self.on_workspace_sync)
        self.register("user_message_request", self.on_user_message_request)
        self.register("probing_prompts_request", self.on_probing_prompts_request)
        self.register("analysis_report_feedback", self.on_analysis_report_feedback)
        self.register(
            "added_missing_information",
            self.on_added_missing_information,
        )
        self.register(
            "applicable_law_relevance_request",
            self.on_applicable_law_relevance_request,
        )
        self.register(
            "analysis_docx_generate_request",
            self.on_analysis_docx_generate_request,
        )
        # Drafting Assistant — template library sync from BE.
        self.register("template_sync", self.on_template_sync)
        # Reply to our AI-initiated request_template_sync (full snapshot).
        self.register("template_sync_response", self.on_template_sync_response)
        # "Regenerate Extra Info" — backend asks the AI to re-profile a template
        # and emit a fresh blueprint proposal for the approval screen.
        self.register(
            "request_blueprint_proposal", self.on_request_blueprint_proposal
        )

        # Legacy event handlers
        self.register("workspace_updated", self.on_workspace_updated)
        self.register("workspace_deleted", self.on_workspace_deleted)

        logger.info("✅ Event handlers registered")

    def register(self, event_name: str, handler: EventHandler) -> None:
        """
        Register an event handler.

        Args:
            event_name: Name of the event
            handler: Async function to handle the event
        """
        self._handlers[event_name] = handler
        logger.debug("Registered handler for event: {}", event_name)

    async def handle(self, event_name: str, data: Any) -> None:
        """
        Handle an incoming event.

        Args:
            event_name: Name of the event
            data: Event data
        """
        handler = self._handlers.get(event_name)

        if handler:
            try:
                logger.debug("Handling event: {} with data: {}", event_name, data)
                await handler(data)
            except Exception as e:
                logger.error("Error handling event {}: {}", event_name, str(e))
                logger.exception(e)
                raise SocketEventError(
                    f"Failed to handle event: {event_name}",
                    details={"event": event_name, "error": str(e)},
                )
        else:
            logger.warning("No handler registered for event: {}", event_name)

    # ========================================================================
    # Helper Methods
    # ========================================================================

    # Empty case-entities scaffold used by the strategy "deep agents" pipeline.
    # Sub-extractors that fail or time out fall back to this so synthesis never
    # crashes on a missing key.
    _EMPTY_CASE_ENTITIES: Dict[str, List[Any]] = {
        "parties": [],
        "dates": [],
        "properties": [],
        "amounts": [],
        "suit_numbers": [],
        "statutes": [],
        "annexures": [],
    }

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

            current_report_json = json.dumps(current_report, ensure_ascii=True)

            prompt = f"""You are a legal-analysis-report editor for Indian law.

You will edit the existing Analysis Report (JSON below) by applying the user's instruction. Return the FULL report with the change applied and everything else reproduced verbatim.

**User's edit instruction** (THIS IS THE PRIMARY CONSTRAINT — apply it EXACTLY as worded):
{instruction}

CURRENT analysis report JSON (edit THIS, preserving its exact structure and keys):
{current_report_json}

**Role**: {role}

EDIT-INSTRUCTION RULES (CRITICAL — override every other instinct):
1. The user's instruction OUTRANKS any tendency to "improve", "summarise", or "regenerate" the report. Apply ONLY the change the user named.
2. If the instruction is a RENAME / FIND-AND-REPLACE (e.g. replace every occurrence of "X" with "Y"), replace the term EVERYWHERE it appears across ALL string content: every `strengths[].point` and `strengths[].laws` entry, every `weaknesses`, `risks`, `missingInfo`, `nextSteps`, `pendingActions`, `suggestedDrafts`, and `relevantJurisdiction` item, and the `qualityScoreExplanation` / `reportQualityScoreExplanation` text. Miss NO occurrence.
2a. NAME ASSIGNMENT: If the instruction supplies or corrects a NAME for a party that is referenced by ROLE or DESCRIPTION (e.g. "the female employee", "the petitioner", "the employee", "she") rather than by a literal existing token, identify that party from the role / gender / description context and use the supplied name wherever that party is referenced across ALL the string fields listed in rule 2 — INCLUDING role labels like "the Petitioner" / "the employee" — even if the name does not already appear in the report. A literal matching token is NOT required.
3. If the user QUOTES exact text (a sentence, a phrase, a list item) inside the instruction, find that exact text in the report and REMOVE (or rephrase, per the verb the user used) ONLY that text/item.
4. Every other field, list item, sentence, and reference MUST be reproduced WORD-FOR-WORD. Do NOT drop, reorder, compress, or re-score anything the user did not mention.
5. Keep `caseResolutionScore` and the score explanations unchanged UNLESS the user explicitly asked to change the score. (A rename inside the explanation text is allowed — that is just text replacement, not a score change.)
6. Preserve the exact JSON shape and keys present in CURRENT analysis report: `strengths` stays a list of objects with `point` (string) and `laws` (list of strings); `weaknesses` / `risks` / `missingInfo` / `nextSteps` / `pendingActions` / `suggestedDrafts` / `relevantJurisdiction` stay lists of strings.
7. If the user asks for something impossible (e.g. inventing a fact not present), ignore that part and apply only the valid edit.

TRUNCATED-TEXT RULE: Never reproduce a token, name, or phrase that ends in "..." or "…" — those are UI-truncation noise, not real entity names. Drop them silently.

{SPEAKER_LABEL_GUARDRAIL}

Output rules:
- Preserve Indian-law citations exactly as in the source (BNS/BNSS/BSA/IT Act). Do NOT switch to IPC/CrPC/Evidence Act.
- Return ONLY the complete, valid analysis-report JSON object (no markdown code fences, no commentary)."""

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
                    current_report, sort_keys=True, ensure_ascii=True
                ):
                    logger.info(
                        "Analysis report edit produced no change — instruction={!r}",
                        instruction[:160],
                    )
                    return False
            except Exception:
                pass

            self._workspace_analysis_payload[workspace_id] = edited

            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": edited,
                    },
                )
                logger.success(
                    "✅ Analysis report surgical edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Analysis report surgical edit failed: {}", e)
            return False

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
        Apply a NAME change to the EXISTING List of Dates entries (Mode 1).

        Surgical only: it replaces the renamed party's name in each entry's
        title / description / evidenceSnippet and changes NOTHING else — no new
        or removed entries, and date / relevance / sourceDocId / sourceDocName /
        eventType / pageNumber / isIncident are all preserved. It never
        regenerates the list from documents. Operates on the cached
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
            prompt = f"""You are editing a legal case's LIST OF DATES (a JSON array of dated events). Apply ONLY the user's name change to the EXISTING entries and return the FULL array.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT list of dates (edit THIS array in place):
{current_json}

STRICT RULES:
1. This is a NAME change ONLY. Replace the renamed party's name wherever it appears in each entry's "title", "description", and "evidenceSnippet". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
2. Keep the SAME number of entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
3. Do NOT change any other field — "date", "relevance", "sourceDocId", "sourceDocName", "eventType", "pageNumber", "isIncident" must be reproduced EXACTLY as-is for every entry.
4. Change nothing except the renamed name. Every other word stays verbatim.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""

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
            prompt = f"""The case OBJECTIVE / perspective has changed. Re-score how RELEVANT each existing dated event is to the NEW objective. Return the SAME array with ONLY the "relevance" field updated.

NEW objective / case perspective:
{obj}

CURRENT list of dates (re-score relevance against the new objective):
{current_json}

STRICT RULES:
1. Keep the SAME entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
2. Change ONLY each entry's "relevance" value — set it to "High", "Medium", or "Low" based on how important that event is to the NEW objective.
3. Reproduce EVERY other field ("date", "title", "description", "evidenceSnippet", "sourceDocId", "sourceDocName", "eventType", "pageNumber", "isIncident") EXACTLY as-is — do NOT rewrite any text.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""

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

    async def _regenerate_applicable_laws_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        emit_result: bool = True,
    ) -> bool:
        """
        Apply a NAME change to the EXISTING Applicable Laws entries (Mode 1
        rename). Surgical only: replaces the renamed party's name in each law's
        ``description`` and changes NOTHING else — same laws, same count/order,
        and `id` / `act` / `section` / `relevance` preserved. Never regenerates
        laws from documents. Operates on the cached
        ``_workspace_applicable_laws_payload`` and re-emits featureType=applicable_laws.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_applicable_laws_payload.get(workspace_id)
            if not isinstance(current, dict):
                return False
            laws = current.get("laws")
            if not isinstance(laws, list) or not laws:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = f"""You are editing a case's APPLICABLE LAWS (JSON object {{"laws": [...]}}). Apply ONLY the user's name change to the EXISTING law entries and return the FULL object.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT applicable laws (edit THIS object in place):
{current_json}

STRICT RULES:
1. This is a NAME change ONLY. Replace the renamed party's name wherever it appears in each law's "description". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
2. Keep the SAME laws in the SAME order. Do NOT add, remove, merge, or reorder laws.
3. Do NOT change "id", "act", "section", or "relevance" for any law — reproduce them EXACTLY.
4. Change nothing except the renamed name inside "description". Statute names / section numbers stay verbatim.

Return ONLY the complete, valid JSON object (no markdown code fences, no commentary)."""

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
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            edited = json.loads(json_str)
            edited_laws = edited.get("laws") if isinstance(edited, dict) else None
            if not isinstance(edited_laws, list) or len(edited_laws) != len(laws):
                logger.warning(
                    "Applicable laws name edit rejected (count mismatch); keeping cache"
                )
                return False

            edited = self._normalize_applicable_laws_payload(edited)
            if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                current, sort_keys=True, ensure_ascii=True
            ):
                return False  # nothing changed

            self._workspace_applicable_laws_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "applicable_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": edited,
                    },
                )
                logger.success(
                    "✅ Applicable laws name edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Applicable laws name edit failed: {}", e)
            return False

    async def _regenerate_procedural_timeline_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        emit_result: bool = True,
    ) -> bool:
        """
        Apply a NAME change to the EXISTING Procedural Timeline (Upcoming Dates)
        entries (Mode 1 rename). Surgical only: replaces the renamed party's name
        in each entry's ``title`` / ``description`` and preserves ``date`` /
        ``type`` and the entry set. Never regenerates the timeline. Operates on
        the cached ``_workspace_procedural_timeline_payload`` and re-emits
        featureType=procedural_timeline.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_procedural_timeline_payload.get(workspace_id)
            if not isinstance(current, list) or not current:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = f"""You are editing a case's PROCEDURAL TIMELINE / UPCOMING DATES (a JSON array of milestones). Apply ONLY the user's name change to the EXISTING entries and return the FULL array.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT procedural timeline (edit THIS array in place):
{current_json}

STRICT RULES:
1. This is a NAME change ONLY. Replace the renamed party's name wherever it appears in each entry's "title" and "description". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
2. Keep the SAME number of entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
3. Do NOT change "date" or "type" for any entry — reproduce them EXACTLY.
4. Change nothing except the renamed name.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""

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
            if not isinstance(edited, list) or len(edited) != len(current):
                logger.warning(
                    "Procedural timeline name edit rejected (count {} != {}); keeping cache",
                    len(edited) if isinstance(edited, list) else "n/a",
                    len(current),
                )
                return False

            edited = scrub_app_brand(edited)
            if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                current, sort_keys=True, ensure_ascii=True
            ):
                return False  # nothing changed

            self._workspace_procedural_timeline_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "procedural_timeline",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {"proceduralTimeline": edited},
                    },
                )
                logger.success(
                    "✅ Procedural timeline name edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            return True
        except Exception as e:
            logger.warning("Procedural timeline name edit failed: {}", e)
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
                    "AVAILABLE ANNEXURES (HARD LIMIT — do not violate):\n"
                    f"- The user has uploaded exactly {num_user_uploads} annexures.\n"
                    f"- The ONLY valid annexure references are: Annexure 1 through Annexure {num_user_uploads}.\n"
                    f"- You MUST NOT reference Annexure {num_user_uploads + 1} or any higher number.\n"
                    "- Do NOT invent additional annexures."
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
- Never fabricate facts to force alignment; when objective and evidence conflict, state the conflict as weakness/risk.

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
                        "You produced an analysis report for an Indian-law matter but "
                        "left the strengths array nearly empty. The schema MANDATES "
                        "2-6 strengths. Every matter has at least two facts that help "
                        "the client's position.\n\n"
                        "Return ONE JSON object with exactly this shape (no commentary, "
                        "no markdown):\n\n"
                        "{\n"
                        '  "strengths": [\n'
                        '    { "point": "2-4 sentence factual strength naming the specific '
                        'party / date / Annexure / amount from EXTRACTED FACTS that '
                        'establishes it. Do NOT pad with statute boilerplate. Do NOT use '
                        '\'Party No. X\' placeholders.", "laws": ["BNS Section 318", '
                        '"BSA"] }\n'
                        "  ]\n"
                        "}\n\n"
                        "Anchor every strength to EXTRACTED FACTS. Valid strength "
                        "categories (use at least 2):\n"
                        "1. Documentary evidence preserved in an annexure (MLC, FIR, "
                        "registered deed, hospital record, signed contract, "
                        "audio/video transcript, bank statement, photograph, official "
                        "certificate).\n"
                        "2. Party admissions (opposing side stated, signed, or did "
                        "something supporting the client — partial payment, written "
                        "reply admitting a fact, official detention record).\n"
                        "3. Procedural advantage (favourable forum / jurisdiction, "
                        "limitation period still open, statutory presumption in "
                        "client's favour).\n"
                        "4. Statutory framework support (a named provision under BNS / "
                        "BNSS / BSA / IT Act / Specific Relief Act / Hindu Marriage "
                        "Act / Domestic Violence Act / etc. that on the recorded facts "
                        "points to relief for the client).\n"
                        "5. Corroboration across annexures (two or more annexures that "
                        "independently support the same fact).\n\n"
                        f"EXTRACTED FACTS:\n{_entities_block_ar}\n\n"
                        "CONTEXT — items you already produced for this report:\n"
                        f"- missingInfo: {(missing_info or [])[:8]}\n"
                        f"- weaknesses: {(data.get('weaknesses') or [])[:6] if isinstance(data.get('weaknesses'), list) else []}\n"
                        f"- risks: {(data.get('risks') or [])[:6] if isinstance(data.get('risks'), list) else []}\n\n"
                        "DOCUMENTS (in upload order):\n"
                        f"{combined_document_content[:60000]}\n"
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

            prompt = f"""Rewrite/produce relevantJurisdiction entries in STRICT structured form.

Current entries:
{json.dumps(cleaned, ensure_ascii=False)}

Case summary:
{case_summary}

Strategic objective:
{strategic_objective}

Document excerpt:
{(document_excerpt or "")[:12000]}

Rules:
- Output ONLY JSON object: {{"relevantJurisdiction": ["..."]}}
- 1 to 5 items only
- Entries MUST be COURT/FORUM names only (no reasons/legal basis text).
- Do NOT output explanations in parentheses, after hyphen, or after colon.
- Do NOT output location-only lines.
- Good: "Metropolitan Magistrate Court, Delhi"
- Good: "High Court of Delhi"
- Do NOT output lines like "Patna, Bihar: Permissible under ...".
- Prefer case-specific nearby forums from facts.
- Include High Court/Supreme Court only when plausibly relevant to the case posture.
"""
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
            prompt = f"""You are a legal case analyst. From the following annexures, extract a chronological list of FACTUAL, CASE-RELEVANT dates and events.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

**Today's date**: {today_str} (current year is {current_year}). Anchor every relative or partial date against this.

{list_of_dates_inventory or "DOCUMENT INVENTORY: (no classified inventory available)"}

**Annexure content** (each file is labeled Annexure 1, Annexure 2, etc. with id and name):
{combined}

**Task**:
1) Identify dates directly tied to this dispute: agreements, payments, notices, hearings where an order/action occurred, deadlines, possession, filings, and key communications.
2) For every such date, write a short, single-sentence description (max 12 words) of what actually happened.
3) Output a JSON array of date entries.

Each entry MUST have:
- "date": string in YYYY-MM-DD format only
- "title": string, 3-5 word title for the event (e.g. "FIR Registered", "Salary Deducted", "Marriage Ceremony", "Possession Delay")
- "description": string, one concise sentence describing what happened (max 12 words, no raw text dumps)
- "sourceDocumentIndex": integer (1-based), which document (1, 2, 3...) the event comes from
- "pageNumber": integer (mandatory, default 1 if unclear)
- "isIncident": boolean (optional, default true)
- "eventType": one of "Incident" | "Agreement" | "Payment" | "Communication" | "Other"
  (procedural event types — Court Hearing, Filing, Deadline, Meeting — belong
  to the Procedural Timeline panel and MUST NOT be used here)
- "relevance": one of "High" | "Medium" | "Low"

This panel records the FACTUAL NARRATIVE of the dispute — the chain of
real-world events that, taken together, explain how the dispute arose, what
each party did, and what concrete consequences followed. The dispute can be
of ANY type (civil, criminal, matrimonial, employment, property, immigration,
consumer, cyber-fraud, IPR, tax, regulatory, etc.) — the same principles apply
across all of them.

CORE TEST FOR EACH CANDIDATE DATE — keep the entry only if ALL three are true:
  (a) The date marks something that ACTUALLY HAPPENED in the past — not a
      future scheduled event, not a deadline, not a planned step. The date
      must be on or before today ({today_str}).
  (b) The event is CASE-RELEVANT — i.e. it directly contributes to the chain
      of cause-and-effect that gave rise to this dispute. Identity-document
      metadata (passport / visa / licence issue or expiry, ID generation
      timestamps, system-generated certificate numbers) is case-relevant
      ONLY when the dispute is ABOUT that document itself (e.g. an immigration
      overstay matter, a passport-fraud case, a visa-revocation challenge).
      In every other dispute, those dates are biographical / administrative
      noise and MUST be dropped.
  (c) The event is FACTUAL, not procedural. Court filings, hearings,
      adjournments, next-date orders, judgments, legal notices, demand
      notices, statutory notices, response deadlines, filing deadlines,
      evidence deadlines, compliance deadlines, limitation-period endings,
      lawyer consultations, and mediation sessions are PROCEDURAL — they
      belong to a separate Procedural Timeline panel, not this one.

ASK YOURSELF before keeping each date:
  - "If a lawyer were narrating the story of this dispute to a colleague,
     would they mention this event?" If no → drop.
  - "Is this date the kind of thing a court would care about because it
     describes the underlying conduct, harm, or transaction?" If yes → keep.
  - "Is this date about WHEN the parties did something to each other (or to
     the case property / instrument)?" If yes → keep.
  - "Is this date about WHEN a court / tribunal / regulator / lawyer scheduled
     or required something?" If yes → drop, it belongs on procedural_timeline.
  - "Would removing this date from the narrative leave a gap, or is it
     orphan metadata?" Drop if it is orphan metadata.

DEDUPLICATION — output ONE entry per real-world event:
  - If multiple parts of the source describe the same underlying event with
    slightly different wording (e.g. "Arrival in India" and "New Delhi
    Arrival" on the same date), emit ONE entry, not two.
  - If the same event is mentioned across multiple annexures, still ONE
    entry. Pick the most specific, factually clearest title and description.
  - Never emit two entries that share the same date AND describe the same
    underlying real-world action.

ALSO EXCLUDE:
- Future / upcoming dates of any kind (planned hearings, expiry deadlines,
  scheduled meetings, requested departure dates).
- Every row of a court listing / hearing table — those are procedural.
- Dates appearing only in "Date of Listing / Date of Upload / Order/Judgement"
  table columns with no substantive context.
- Dates alongside "View PDF", "IA/MA", "Connected Matters", "Contact Us",
  "Feedback", "Sitemap" or other website navigation text.
- Placeholder dates like 11-11-1111 or 9999-99-99.
- Background / historical company information unrelated to this dispute.
- Document-system metadata: "ETA Generation Date", "Document Generation Date",
  certificate creation timestamps, system audit timestamps, etc., unless that
  exact date is a load-bearing fact of the dispute.

Rules:
- Output ONLY a single valid JSON array. No markdown, no code fences, no other text.
- The first character of your response MUST be [ and the last MUST be ].
- Date normalization: output each "date" as YYYY-MM-DD.
- **Indian DD/MM/YYYY convention** — when the source contains a slash- or
  dash-separated numeric date like ``01/05/2026``, ``01-05-2026``, ``01.05.2026``,
  the FIRST number is the DAY and the SECOND number is the MONTH (Indian
  convention). Do NOT interpret it as US MM/DD/YYYY.
    • ``01/05/2026`` → DAY 1, MONTH 5 → ``2026-05-01`` (1 May 2026), NOT
      ``2026-01-05`` (5 January 2026).
    • ``13/04/2026`` → DAY 13, MONTH 4 → ``2026-04-13``.
    • Whenever the first number is > 12, that already proves DD/MM/YYYY
      (e.g. ``25/03/2026`` can only be 25 March). Apply the same rule even
      when both numbers are ≤ 12 — DAY first, MONTH second.
    • Only switch to MM/DD/YYYY if the source EXPLICITLY identifies the
      format (e.g. labels like "Date (MM/DD/YYYY): 01/05/2026") or the date
      sits inside a clearly US/foreign-jurisdiction document.
- **Year resolution when the source mentions a date without a year** (e.g. "2 February", "21 तारीख", "last Tuesday", "the 15th of March"):
  - Default to the CURRENT YEAR ({current_year}) — these are recent events from a live conversation/document, not historical records.
  - Pick the year that places the event ON OR BEFORE today ({today_str}); if "{current_year}-MM-DD" would be in the future relative to today, use {int(current_year) - 1} instead (event must have already happened to be reported).
  - Only omit the entry if the source itself explicitly states a year that is unreadable/contradictory.
- **Relative-time anchors** (e.g. "tomorrow", "today", "कल", "आज", "in 24 hours", "within 24 hours", "by 5 PM tomorrow", "next week", "next Monday"): anchor STRICTLY to today's date ({today_str}). Do NOT derive these from any other date mentioned in the source. Specifically:
  - "tomorrow" / "कल" / "by 5 PM tomorrow" / "by EOD tomorrow" → {tomorrow_str}.
  - "today" / "आज" / "in 24 hours" / "within 24 hours" → {today_str}.
  - "next week" / "in a week" / "in 7 days" → {next_week_str}.
  - "next Monday" / "next Tuesday" etc. → the next occurrence of that weekday strictly AFTER today ({today_str}).
- **Currency / money is NOT a year**. Numbers like "₹2000", "Rs. 7000", "$5000" are AMOUNTS in the case (salary, fines, transaction values), NOT year references. Never use a currency amount as a year for any date entry.
- Sorting: sort by date ascending. Tie-break: sourceDocumentIndex ascending, then title alphabetically.
- Escape quotes inside strings with \\". Keep title ≤ 5 words, description ≤ 12 words.

Example shape:
[{{"date":"{current_year}-01-15","title":"FIR Registered","description":"FIR was lodged at the local police station regarding the incident.","sourceDocumentIndex":1,"pageNumber":3,"isIncident":true,"eventType":"Incident","relevance":"High"}}]
"""
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

    def _strip_markdown_json_fences(self, text: str) -> str:
        cleaned = (text or "").strip()
        if "```" not in cleaned:
            return cleaned
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, flags=re.I)
        if m:
            return (m.group(1) or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        return cleaned.strip()

    def _extract_balanced_json_fragment(
        self, text: str, open_char: str
    ) -> Optional[str]:
        if not text:
            return None
        close_char = "}" if open_char == "{" else "]"
        start = text.find(open_char)
        if start < 0:
            return None
        depth = 0
        i = start
        in_string = False
        escape = False
        quote = '"'
        while i < len(text):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == quote:
                    in_string = False
                i += 1
                continue
            if c == '"' or c == "'":
                in_string = True
                quote = c
                i += 1
                continue
            if c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return text[start:].strip() if start >= 0 else None

    def _prepare_json_candidate(
        self, raw_text: str, prefer: str = "object"
    ) -> Optional[str]:
        text = self._strip_markdown_json_fences(raw_text)
        if not text:
            return None

        open_char = "{" if prefer == "object" else "["
        candidate = self._extract_balanced_json_fragment(text, open_char)
        if not candidate:
            fallback_char = "[" if open_char == "{" else "{"
            candidate = self._extract_balanced_json_fragment(text, fallback_char)
        if not candidate:
            return None

        # Repair common truncation by balancing quotes/brackets/braces.
        unescaped_quotes = len(re.findall(r'(?<!\\)"', candidate))
        if unescaped_quotes % 2 != 0:
            candidate += '"'
        open_brackets = candidate.count("[") - candidate.count("]")
        open_braces = candidate.count("{") - candidate.count("}")
        if open_brackets > 0:
            candidate += "]" * open_brackets
        if open_braces > 0:
            candidate += "}" * open_braces
        candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
        return candidate.strip()

    def _normalize_applicable_laws_payload(self, raw: Any) -> Dict[str, Any]:
        """Normalize model/search output into strict applicable_laws payload schema."""
        laws_raw = []
        if isinstance(raw, dict):
            laws_raw = raw.get("laws") or []
        elif isinstance(raw, list):
            laws_raw = raw
        if not isinstance(laws_raw, list):
            laws_raw = []

        def normalize_section_label(value: str) -> str:
            """Normalize noisy section labels and return raw section token."""
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                return ""

            # Remove any repeated/single "Section"/"Sec" prefix variants.
            core = re.sub(
                r"^(?:(?:section|sec)\s*[:.-]?\s*)+",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip(" :.-")
            return core or text

        normalized: List[Dict[str, str]] = []
        relevance_allowed = {"high": "High", "medium": "Medium", "low": "Low"}
        for idx, item in enumerate(laws_raw, start=1):
            if not isinstance(item, dict):
                continue
            act = str(item.get("act") or "").strip()
            section = normalize_section_label(item.get("section"))
            description = str(item.get("description") or "").strip()
            # Guardrail: user explicitly asked to not show IPC / CrPC / Indian
            # Evidence Act 1872 items. Map those to BNS 2023 / BNSS 2023 / BSA
            # 2023 in upstream prompt; here we just drop any leakage.
            act_l = act.lower()
            desc_l = description.lower()
            if (
                re.search(r"\bindian\s*penal\s*code\b", act_l)
                or re.search(r"\bipc\b", act_l)
                or re.search(r"\bipc\b", desc_l)
            ):
                continue
            if (
                re.search(r"\bcode\s*of\s*criminal\s*procedure\b", act_l)
                or re.search(r"\bcr\.?\s*p\.?\s*c\.?\b", act_l)
                or re.search(r"\bcr\.?\s*p\.?\s*c\.?\b", desc_l)
            ):
                continue
            if re.search(r"\bindian\s*evidence\s*act\b", act_l) and re.search(
                r"\b1872\b", act_l
            ):
                continue
            relevance_in = str(item.get("relevance") or "").strip().lower()
            relevance = relevance_allowed.get(relevance_in, "Medium")
            if not act or not section or not description:
                continue
            normalized.append(
                {
                    "id": f"law_{idx}",
                    "act": act,
                    "section": section,
                    "description": description,
                    "relevance": relevance,
                }
            )
            if len(normalized) >= 12:
                break
        # Final safety net: never surface the application's own brand/platform
        # name in any law row (central chokepoint for the applicable_laws path).
        return scrub_app_brand({"laws": normalized})

    def _heuristic_applicable_laws_fallback(
        self, evidence_snippet: str
    ) -> Dict[str, Any]:
        """Best-effort law mapping from keywords when model JSON fails or returns empty.

        Designed to produce a BALANCED list across Indian civil law and criminal law:
        criminal-side keywords route to BNS / BNSS / BSA / IT Act / NI Act 138 etc.,
        while civil-side keywords route to Contract Act, Specific Relief Act, TPA,
        Consumer Protection Act, Companies Act, Limitation Act, etc.
        """
        txt = (evidence_snippet or "").lower()
        candidates: List[Dict[str, str]] = []

        def add(
            act: str, section: str, description: str, relevance: str = "Medium"
        ) -> None:
            candidates.append(
                {
                    "id": "",
                    "act": act,
                    "section": section,
                    "description": description,
                    "relevance": relevance,
                }
            )

        # ----------------------------- CRIMINAL LAW -----------------------------
        if re.search(
            r"\b(hack|hacked|phish|otp|password|account|cyber|digital|upi|wallet)\b",
            txt,
        ):
            add(
                "Information Technology Act, 2000",
                "Section 66C",
                "Potential identity / credential misuse through digital systems.",
                "High",
            )
            add(
                "Information Technology Act, 2000",
                "Section 66D",
                "Potential cheating by personation using computer resources.",
                "High",
            )
        if re.search(r"\b(theft|stolen|stole|robbery|burglar|dacoity|loot)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 303",
                "Potential theft of movable property based on allegations.",
                "High",
            )
        if re.search(
            r"\b(cheat|fraud|defraud|forgery|forged|fake document|fabricat)\b", txt
        ):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 318",
                "Potential cheating / dishonest inducement of property.",
                "High",
            )
        if re.search(r"\b(threat|intimidat|blackmail|extort)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 351",
                "Potential criminal intimidation-related conduct.",
                "Medium",
            )
        if re.search(r"\b(assault|hurt|injur|beat|attack|grievous)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 115",
                "Potential voluntarily causing hurt allegations.",
                "Medium",
            )
        if re.search(r"\b(criminal breach of trust|misappropriat|embezzl)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 316",
                "Potential criminal breach of trust over entrusted property.",
                "High",
            )
        if re.search(
            r"\b(fir|f\.i\.r\.|police complaint|investigation|charge ?sheet|"
            r"chargesheet|magistrate|bail)\b",
            txt,
        ):
            add(
                "Bharatiya Nagarik Suraksha Sanhita, 2023",
                "Section 173",
                "Registration / handling of information of cognizable offence.",
                "Medium",
            )
        if re.search(r"\b(cheque|cheque bounce|dishonour|dishonor|bounced)\b", txt):
            add(
                "Negotiable Instruments Act, 1881",
                "Section 138",
                "Dishonour of cheque for insufficiency of funds.",
                "High",
            )

        # ------------------------------- CIVIL LAW ------------------------------
        if re.search(
            r"\b(contract|agreement|mou|breach|consideration|offer|acceptance)\b",
            txt,
        ):
            add(
                "Indian Contract Act, 1872",
                "Section 73",
                "Compensation for loss caused by breach of contract.",
                "High",
            )
        if re.search(
            r"\b(specific performance|injunction|declaration|relief|recover possession)\b",
            txt,
        ):
            add(
                "Specific Relief Act, 1963",
                "Section 10",
                "Possible specific performance of a contract is enforceable.",
                "Medium",
            )
            add(
                "Specific Relief Act, 1963",
                "Section 38",
                "Perpetual injunction may be sought to prevent breach of obligation.",
                "Medium",
            )
        if re.search(
            r"\b(sale deed|mortgage|lease|tenant|landlord|gift deed|"
            r"transfer of property|easement)\b",
            txt,
        ):
            add(
                "Transfer of Property Act, 1882",
                "Section 54",
                "Sale-of-immovable-property obligations may govern the transaction.",
                "Medium",
            )
        if re.search(r"\b(suit|plaint|written statement|decree|civil court)\b", txt):
            add(
                "Code of Civil Procedure, 1908",
                "Order VII Rule 1",
                "Framing / maintainability of the plaint.",
                "Medium",
            )
        if re.search(r"\b(time ?bar|barred by limitation|delay|condonation)\b", txt):
            add(
                "Limitation Act, 1963",
                "Section 3",
                "Limitation bar must be tested before the cause of action proceeds.",
                "Medium",
            )
        if re.search(
            r"\b(consumer|deficiency in service|unfair trade|product defect|service provider)\b",
            txt,
        ):
            add(
                "Consumer Protection Act, 2019",
                "Section 35",
                "Consumer complaint for deficiency in service / unfair trade practice.",
                "Medium",
            )
        if re.search(r"\b(company|director|shareholder|board|mismanagement|nclt)\b", txt):
            add(
                "Companies Act, 2013",
                "Section 241",
                "Oppression and mismanagement concerns requiring review.",
                "Medium",
            )
        if re.search(
            r"\b(arbitration|arbitral|arbitrator|seat|venue|award)\b", txt
        ):
            add(
                "Arbitration and Conciliation Act, 1996",
                "Section 11",
                "Appointment of arbitrator if the parties cannot agree.",
                "Medium",
            )
        if re.search(r"\b(gst|invoice|input tax|itc|tax credit)\b", txt):
            add(
                "Central Goods and Services Tax Act, 2017",
                "Section 16",
                "Input tax credit eligibility may be relevant.",
                "Medium",
            )
        if re.search(
            r"\b(divorce|matrimonial|maintenance|guardianship|custody|alimony|"
            r"hindu marriage)\b",
            txt,
        ):
            add(
                "Hindu Marriage Act, 1955",
                "Section 13",
                "Grounds for divorce may be invoked.",
                "Medium",
            )

        return self._normalize_applicable_laws_payload({"laws": candidates})

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

    async def _search_indiankanoon(self, query: str) -> List[Dict[str, str]]:
        """Search Indian Kanoon directly and return top lightweight results."""
        if not query.strip():
            return []
        search_url = f"https://indiankanoon.org/search/?formInput={quote_plus(query)}"
        logger.info("Applicable laws: Indian Kanoon query={}", query[:180])
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.get(
                    search_url, headers={"User-Agent": "Mozilla/5.0"}
                )
                if res.status_code >= 400:
                    logger.warning(
                        "Applicable laws: Indian Kanoon search status {}",
                        res.status_code,
                    )
                    return []
                html = res.text or ""
        except Exception as e:
            logger.warning("Applicable laws: Indian Kanoon search failed: {}", e)
            return []

        pattern = re.compile(
            r'<a[^>]+href="(?P<href>/doc/\d+/?)"[^>]*>(?P<title>.*?)</a>',
            flags=re.I | re.S,
        )
        results: List[Dict[str, str]] = []
        seen = set()
        for m in pattern.finditer(html):
            href = m.group("href") or ""
            title = re.sub(r"<.*?>", " ", m.group("title") or "")
            title = unescape(re.sub(r"\s+", " ", title)).strip()
            if not title:
                continue
            url = f"https://indiankanoon.org{href}"
            if url in seen:
                continue
            seen.add(url)
            results.append({"title": title, "url": url, "snippet": ""})
            if len(results) >= 8:
                break
        if results:
            preview = [
                {"title": r.get("title", "")[:120], "url": r.get("url", "")}
                for r in results[:5]
            ]
            logger.info("Applicable laws: Indian Kanoon results preview={}", preview)
        else:
            logger.info("Applicable laws: Indian Kanoon returned no results")
        return results

    async def _generate_applicable_laws_from_sources(
        self,
        workspace_id: str,
        workspace_role: str,
        signals: Dict[str, Any],
        search_results: List[Dict[str, str]],
        objective_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate final laws list JSON from case signals + search context.

        When ``objective_context`` (the current strategic objective) is provided,
        the law selection is steered toward that objective/perspective so a
        perspective change re-derives the relevant statutes.
        """
        from app.ai.gemini_client import gemini_client

        sources_text = []
        for idx, row in enumerate(search_results[:8], start=1):
            sources_text.append(
                f"{idx}. title={row.get('title','')} | url={row.get('url','')} | snippet={row.get('snippet','')}"
            )
        source_blob = (
            "\n".join(sources_text)
            if sources_text
            else "No external case snippets available."
        )
        targeted_acts_block = format_targeted_acts_for_prompt()
        targeted_acts_count = len(TARGETED_INDIAN_ACTS)
        objective_block = (
            (
                "CURRENT STRATEGIC OBJECTIVE / CASE PERSPECTIVE — this is the PRIMARY lens for "
                "selecting laws. Select ONLY statutes that ADVANCE this objective. Treat the case "
                "facts/evidence as support for the objective — do NOT list statutes that merely "
                "address the underlying factual incident (e.g. a theft, an accident) UNLESS they "
                "directly serve the objective. When the objective is matrimonial (divorce, "
                "judicial separation, custody, maintenance, restitution), prioritise "
                "matrimonial/family law (e.g. Hindu Marriage Act, 1955; Guardians and Wards Act; "
                "maintenance provisions) and DROP unrelated offence statutes. If the objective "
                "shifts perspective, the law list must shift with it — drop laws that only fit the "
                "earlier perspective:\n" + objective_context.strip()
            )
            if objective_context and objective_context.strip()
            else ""
        )
        prompt = f"""
You are an Indian legal research assistant.
Return ONLY one valid JSON object with this exact shape:
{{
  "laws": [
    {{
      "id": "law_1",
      "act": "Act name",
      "section": "Section X",
      "description": "Why this section is potentially applicable to this case.",
      "relevance": "High"
    }}
  ]
}}

Scope of analysis — Indian civil law AND criminal law (BOTH branches):
- Treat this as a dual-lens exercise. Before finalising the list, consciously ask:
  (1) "What CRIMINAL-law provisions could the documented facts attract?" and
  (2) "What CIVIL-law provisions / private remedies could the same facts attract?"
- Where the facts plausibly support BOTH a criminal angle and a civil angle, include
  representative sections from EACH branch (balanced coverage). Do not silently drop
  one branch just because the other is more obvious.
- Where the facts only support one branch, return only that branch — do NOT invent
  the other side just to fill a quota. Coherence beats balance.

Typical Indian statutes to consider (illustrative, NOT exhaustive — any other Indian
law may be cited if the facts and sources support it):
- Criminal-law side (preferred citations):
  * Bharatiya Nyaya Sanhita, 2023 (BNS) — substantive offences
  * Bharatiya Nagarik Suraksha Sanhita, 2023 (BNSS) — criminal procedure
  * Bharatiya Sakshya Adhiniyam, 2023 (BSA) — evidence
  * Information Technology Act, 2000 (cyber-offence sections)
  * Negotiable Instruments Act, 1881 (Section 138 — dishonour of cheque, quasi-criminal)
  * PMLA 2002, NDPS 1985, Arms Act 1959, POCSO 2012, Dowry Prohibition Act 1961, PCA 1988, etc., where facts fit.
- Civil-law side:
  * Indian Contract Act, 1872 (breach, misrepresentation, indemnity, agency)
  * Specific Relief Act, 1963 (specific performance, injunctions, declarations)
  * Transfer of Property Act, 1882 (sale, mortgage, lease, gift disputes)
  * Code of Civil Procedure, 1908 (suit / appeal / execution-related provisions)
  * Limitation Act, 1963 (time-bar issues)
  * Sale of Goods Act, 1930 (goods-quality / passing-of-property disputes)
  * Consumer Protection Act, 2019 (deficiency in service, unfair trade practice)
  * Companies Act, 2013 (oppression, mismanagement, director duties, NCLT)
  * Arbitration and Conciliation Act, 1996 (where there is an arbitration clause)
  * Hindu / Muslim / Special Marriage / Succession laws, family law statutes (matrimonial / succession / guardianship matters)
  * RERA 2016, Rent Control statutes, MV Act 1988 (compensation), CGST Act 2017, etc., where facts fit.

Hard rules:
- Do NOT cite the **Indian Penal Code, 1860 (IPC)**. If a provision is IPC-linked, map it to the corresponding **BNS, 2023** section instead.
- Do NOT cite the **Code of Criminal Procedure, 1973 (CrPC)**. If procedural, map it to the corresponding **BNSS, 2023** section instead.
- Do NOT cite the **Indian Evidence Act, 1872**. If an evidence rule is needed, use the corresponding **BSA, 2023** section instead.
- Do NOT restrict to only the lists above; ANY Indian statute (central or state) is allowed when it actually governs the fact pattern.
- **Grounding (any type of matter):** Every row must be justified only from (a) the document excerpt below and/or (b) the numbered Indian Kanoon search lines. Do **not** invent or guess Act/Section numbers; the statutory label you output must match the **legal wrong or remedy** actually discussed in those sources.
- **Coherence:** The offence, liability, or remedy described in your `description` must match what that Act and Section actually govern. If you cannot tie the fact pattern to the section with the sources in hand, **omit that row** rather than stretching unrelated provisions.
- In each `description`, briefly show the link (e.g. a concrete fact from the documents, or "Search line N …" referring to one of the numbered results).
- **No bracket tags or "angle" labels in `description`.** Do NOT prefix the description with "Criminal angle:", "Civil angle:", "Quasi-criminal angle:", "(criminal angle)", "(civil angle)", or any similar tag. The `act` field already makes the branch obvious to the lawyer; keep `description` as plain case-specific prose.
- Relevance must be one of: High, Medium, Low.
- Keep description concise and case-specific.
- If evidence is insufficient, return {{"laws":[]}}.

TARGETED INDIAN ACTS — strongly preferred universe ({targeted_acts_count} acts):
The product targets the following curated list of Indian statutes / codes / rules. **Every `act` value you emit MUST be drawn from this list whenever the fact pattern is governed by any act on the list.** Match the act name as closely as possible to the wording shown here.

If — and only if — a fact in the documents is clearly governed by an Indian law that is NOT on this list (rare), you may include it, provided (a) the source documents or numbered Indian Kanoon results explicitly support it, and (b) you are NOT citing IPC 1860, CrPC 1973, or the Indian Evidence Act 1872 (use BNS 2023 / BNSS 2023 / BSA 2023 instead).

{targeted_acts_block}

{self._build_document_inventory(workspace_id) or "DOCUMENT INVENTORY: (no classified inventory available)"}

Workspace role: {workspace_role}
Workspace title: {signals.get("workspaceTitle","")}
{objective_block}
Search query: {signals.get("query","")}
Case keywords: {", ".join(signals.get("keywords", []))}
Document evidence snippet:
{signals.get("evidenceSnippet","")}

Indian Kanoon / case-law search results:
{source_blob}
"""
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.2,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        logger.info("Applicable laws: raw LLM response preview={}", (raw or "")[:500])
        text = self._prepare_json_candidate(raw or "", prefer="object") or ""
        parsed: Dict[str, Any] = {"laws": []}
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                arr_candidate = self._prepare_json_candidate(raw or "", prefer="array")
                parsed_arr = json.loads(arr_candidate) if arr_candidate else []
                parsed = {"laws": parsed_arr} if isinstance(parsed_arr, list) else {"laws": []}
                if parsed.get("laws"):
                    logger.info("Applicable laws: salvaged laws array from malformed JSON")
            except Exception:
                logger.warning(
                    "Applicable laws: failed to parse Gemini JSON; using heuristic fallback"
                )
                parsed = {"laws": []}

        normalized = self._normalize_applicable_laws_payload(parsed)
        logger.info(
            "Applicable laws: final normalized payload from LLM count={} payload={}",
            len(normalized.get("laws", [])),
            normalized,
        )
        if not normalized.get("laws"):
            heuristic = self._heuristic_applicable_laws_fallback(
                signals.get("evidenceSnippet", "")
            )
            if heuristic.get("laws"):
                logger.info(
                    "Applicable laws: heuristic fallback generated {} law(s)",
                    len(heuristic.get("laws", [])),
                )
                logger.info(
                    "Applicable laws: final payload from heuristic={}", heuristic
                )
                return heuristic
        return normalized

    async def _generate_and_emit_applicable_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        *,
        objective_context: Optional[str] = None,
        merge_with_previous: bool = True,
    ) -> None:
        """Generate and emit document_feature_response with featureType applicable_laws.

        ``objective_context`` (the current strategic objective) steers law
        selection and the Indian Kanoon search query so a case-perspective change
        (e.g. divorce -> judicial separation) re-derives the relevant statutes.
        ``merge_with_previous=False`` REPLACES the cached laws instead of unioning
        them — used on an objective change so stale laws from the old perspective
        are dropped.
        """
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        logger.info(
            "Applicable laws: generation started for workspace {}",
            workspace_id[:12] if workspace_id else "",
        )
        signals = await self._extract_case_signals_from_documents(workspace_id, user_id)
        # Bias the search query toward the new objective when provided, so a
        # perspective change actually changes which case-law is fetched.
        search_query = signals.get("query", "")
        if objective_context:
            search_query = (f"{objective_context} {search_query}").strip()[:400]
        search_results = await self._search_indiankanoon(search_query)
        if not search_results:
            search_results = await vertex_search_indiankanoon(search_query)
        logger.info(
            "Applicable laws: gathered {} search result(s) for workspace {}",
            len(search_results),
            workspace_id[:12] if workspace_id else "",
        )

        new_payload = await self._generate_applicable_laws_from_sources(
            workspace_id=workspace_id,
            workspace_role=role,
            signals=signals,
            search_results=search_results,
            objective_context=objective_context,
        )
        if merge_with_previous:
            # Union with the previously cached payload and dedupe so a regeneration
            # after a transcript edit doesn't drop laws the user has already seen
            # while still folding in any genuinely new entries.
            payload = await self._merge_applicable_laws_with_previous(
                workspace_id, new_payload
            )
        else:
            # Objective change: replace stale laws with the freshly-derived set.
            payload = new_payload
        self._workspace_applicable_laws_payload[workspace_id] = payload
        if not self._socket_client:
            return
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "applicable_laws",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ Applicable laws sent to backend ({} laws)",
            len(payload.get("laws", [])),
        )
        await asyncio.sleep(0.1)

    async def _generate_procedural_timeline_from_documents(
        self,
        workspace_id: str,
        user_id: str,
        all_docs: List[Dict[str, Any]],
        workspace_role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Delegate to the ProceduralTimelineAgent (LangGraph 3-node pipeline).

        The agent:
          1. Extracts timeline entries per document in parallel
             (self-classifying prompt — one call per document).
          2. Runs a consolidation LLM pass that semantically merges
             cross-document duplicates and drops commercial noise.
          3. Validates formats, normalises types, fuzzy-deduplicates,
             and sorts chronologically.

        Returns list of {title, date, description, type} procedural entries.
        """
        from datetime import datetime as _dt

        if not all_docs:
            return []

        from app.langgraph.procedural_timeline_agent import procedural_timeline_agent

        # Build the document list the agent expects: {document_id, name, content}
        documents = []
        for i, doc in enumerate(all_docs, 1):
            content = (doc.get("content") or "").strip()
            if not content or len(content) < 20:
                continue
            documents.append(
                {
                    "document_id": str(doc.get("document_id") or f"doc_{i}"),
                    "name": str(doc.get("name") or doc.get("filename") or f"Annexure {i}"),
                    "content": content,
                }
            )

        if not documents:
            return []

        try:
            result = await procedural_timeline_agent.extract(
                workspace_id=workspace_id,
                documents=documents,
                today_str=_dt.utcnow().strftime("%Y-%m-%d"),
                document_inventory=self._build_document_inventory(workspace_id),
            )
            logger.info(
                "Procedural timeline: generated {} entries for workspace {}",
                len(result),
                workspace_id[:12] if workspace_id else "",
            )
            # Strip the application's own brand/platform name from every
            # milestone (title / description) before caching/emitting.
            return scrub_app_brand(result)
        except Exception as e:
            logger.warning("Procedural timeline agent failed: {}", e)
            return []

    async def _generate_and_emit_procedural_timeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        batch_result: Optional[Dict[str, Any]] = None,
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Fetch workspace documents, generate procedural/upcoming dates, emit to backend and cache.
        Emits document_feature_response with featureType "procedural_timeline".

        ``deleted_doc_ids`` mirrors the parameter on
        ``_generate_and_emit_list_of_dates``. When deletion is the only
        change (``batch_result is None`` and ``deleted_doc_ids`` is
        non-empty), a fast path is taken: the cached prior list is filtered
        to remove entries from deleted documents and emitted, with no LLM
        regeneration. The remaining documents have not changed, so their
        timeline entries are already in the cache.
        """
        if not self._socket_client:
            return
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("Procedural timeline: could not fetch documents: {}", e)
            all_docs = []

        # ------------------------------------------------------------------
        # FAST PATH: pure-deletion regeneration. See the same block in
        # _generate_and_emit_list_of_dates for rationale.
        # ------------------------------------------------------------------
        deleted_set_fast = {str(x).strip() for x in (deleted_doc_ids or []) if x}
        if deleted_set_fast and not isinstance(batch_result, dict):
            live_doc_ids_fast = {
                str(d.get("document_id") or "").strip()
                for d in (all_docs or [])
                if d.get("document_id")
            }
            prior_fast = (
                self._workspace_procedural_timeline_payload.get(workspace_id) or []
            )
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
            prior_fast.sort(key=lambda x: str(x.get("date") or ""))
            self._workspace_procedural_timeline_payload[workspace_id] = prior_fast
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "procedural_timeline",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": {"proceduralTimeline": prior_fast},
                },
            )
            logger.success(
                "✅ Procedural timeline filtered+emitted after deletion (no LLM regen): "
                "{} → {} entries (dropped {})",
                before_count,
                len(prior_fast),
                before_count - len(prior_fast),
            )
            await asyncio.sleep(0.1)
            return

        processed_doc_ids: set = set()
        if isinstance(batch_result, dict):
            for r in batch_result.get("results") or []:
                did = str(r.get("document_id") or "").strip()
                if did:
                    processed_doc_ids.add(did)

        target_docs = all_docs
        if processed_doc_ids:
            target_docs = [
                d for d in (all_docs or [])
                if str(d.get("document_id") or "").strip() in processed_doc_ids
            ]

        timeline = await self._generate_procedural_timeline_from_documents(
            workspace_id=workspace_id,
            user_id=user_id,
            all_docs=target_docs,
            workspace_role=workspace_role,
        )

        # Same full-prior merge pattern as list_of_dates: feed the entire prior
        # cache to the LLM merge so cross-document paraphrases get caught,
        # regardless of which docs were re-processed in this regeneration.
        #
        # IMPORTANT: filter the cached `prior` against the LIVE document ids
        # before merging — the merge preserves prev verbatim, so otherwise
        # entries from deleted documents survive every regeneration. Same
        # bug shape as list_of_dates; same self-healing fix.
        live_doc_ids = {
            str(d.get("document_id") or "").strip()
            for d in (all_docs or [])
            if d.get("document_id")
        }
        prior = self._workspace_procedural_timeline_payload.get(workspace_id) or []
        if not isinstance(prior, list):
            prior = []
        # Deletion context: deterministic pre-pass mirroring the
        # list_of_dates path; see comment there for rationale.
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
                    "procedural_timeline: explicit-delete pre-pass dropped {} cached entry/entries",
                    before - len(prior),
                )
        prior = self._drop_entries_for_deleted_docs(
            prior,
            live_doc_ids,
            drop_unattributed=bool(deleted_set),
        )
        if prior:
            merged = await self._llm_merge_dated_entries_against_previous(
                prior, timeline or [],
                item_type="procedural timeline",
            )
        else:
            merged = list(timeline or [])
        merged.sort(key=lambda x: str(x.get("date") or ""))
        self._workspace_procedural_timeline_payload[workspace_id] = merged
        timeline = merged

        payload = {
            "proceduralTimeline": timeline,
        }
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "procedural_timeline",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            },
        )
        logger.success(
            "✅ Procedural timeline sent to backend ({} entries)", len(timeline)
        )
        await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Drafting Assistant — suggested drafts panel
    # ------------------------------------------------------------------

    async def _evidence_brief_for_drafting(
        self, workspace_id: str, cap: int = 40000
    ) -> str:
        """Concatenate the workspace's uploaded documents (capped) so the
        drafting agent can auto-fill variables from the raw evidence — not just
        the panel summaries. Reuses the per-doc-cap + `_format_document_for_prompt`
        pattern used by strategy/analysis. Returns '' on failure / no docs."""
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning(
                "drafting evidence fetch failed (workspace={}): {}", workspace_id, e
            )
            return ""
        valid = [
            d for d in (all_docs or [])
            if (d.get("content") or "").strip()
            and len((d.get("content") or "").strip()) >= 20
        ]
        if not valid:
            return ""
        per_doc = max(800, cap // max(1, len(valid)))
        parts: List[str] = []
        for i, doc in enumerate(valid, 1):
            content = (doc.get("content") or "").strip()
            if len(content) > per_doc:
                content = content[:per_doc] + "\n[... truncated for length ...]"
            doc_clip = dict(doc)
            doc_clip["content"] = content
            parts.append(_format_document_for_prompt(doc_clip, i, len(valid)))
        out = "\n\n".join(parts)
        if len(out) > cap:
            out = out[:cap] + "\n[... content truncated ...]"
        logger.info(
            "drafting evidence brief built (workspace={}, docs={}, chars={})",
            workspace_id,
            len(valid),
            len(out),
        )
        return out

    def _build_case_brief_for_drafting(
        self, workspace_id: str
    ) -> Dict[str, str]:
        """Compose the case-context strings the drafting agent needs.

        Pulled from already-cached panels (analysis_report, strategy, etc.)
        so a single LLM call generates focused, grounded suggestions.
        """
        case_summary = ""
        applicable_laws_brief = ""
        strategy_brief = ""
        analysis_brief = ""
        list_of_dates_brief = ""

        try:
            strat = self._workspace_strategy_payload.get(workspace_id) or {}
            if isinstance(strat, dict):
                case_summary = (strat.get("caseSummary") or "").strip()
                strategic = (strat.get("strategicObjective") or "").strip()
                next_steps = strat.get("possibleNextSteps") or []
                if isinstance(next_steps, list):
                    next_steps_text = "; ".join(
                        str(s).strip() for s in next_steps[:5] if s
                    )
                else:
                    next_steps_text = ""
                strategy_brief = "\n".join(
                    line for line in [
                        f"Strategic objective: {strategic}" if strategic else "",
                        f"Possible next steps: {next_steps_text}" if next_steps_text else "",
                    ] if line
                )
        except Exception:
            pass

        try:
            analysis = self._workspace_analysis_payload.get(workspace_id) or {}
            if isinstance(analysis, dict):
                weaknesses = analysis.get("weaknesses") or []
                missing = analysis.get("missingInfo") or []
                pending = analysis.get("pendingActions") or []
                strengths = analysis.get("strengths") or []
                lines: List[str] = []
                if isinstance(strengths, list) and strengths:
                    strength_pts = [
                        (s.get("point") or s.get("text") or "") if isinstance(s, dict) else str(s)
                        for s in strengths[:4]
                    ]
                    strength_pts = [p.strip() for p in strength_pts if p and p.strip()]
                    if strength_pts:
                        lines.append("Strengths: " + " | ".join(strength_pts))
                if isinstance(weaknesses, list) and weaknesses:
                    weak_pts = [str(w).strip() for w in weaknesses[:4] if w]
                    if weak_pts:
                        lines.append("Weaknesses: " + " | ".join(weak_pts))
                if isinstance(missing, list) and missing:
                    miss_pts = [str(m).strip() for m in missing[:4] if m]
                    if miss_pts:
                        lines.append("Missing info: " + " | ".join(miss_pts))
                if isinstance(pending, list) and pending:
                    pend_pts = [str(p).strip() for p in pending[:4] if p]
                    if pend_pts:
                        lines.append("Pending actions: " + " | ".join(pend_pts))
                analysis_brief = "\n".join(lines)
        except Exception:
            pass

        try:
            laws_payload = (
                self._workspace_applicable_laws_payload.get(workspace_id) or {}
            )
            if isinstance(laws_payload, dict):
                laws = laws_payload.get("applicableLaws") or laws_payload.get("laws") or []
                if isinstance(laws, list) and laws:
                    parts: List[str] = []
                    for law in laws[:8]:
                        if not isinstance(law, dict):
                            continue
                        act = (law.get("act") or "").strip()
                        section = (law.get("section") or "").strip()
                        if act and section:
                            parts.append(f"{act} §{section}")
                        elif act:
                            parts.append(act)
                    if parts:
                        applicable_laws_brief = "; ".join(parts)
        except Exception:
            pass

        try:
            dates = self._workspace_list_of_dates_payload.get(workspace_id) or []
            if isinstance(dates, list) and dates:
                lines = []
                for d in dates[:6]:
                    if not isinstance(d, dict):
                        continue
                    dt = (d.get("date") or "").strip()
                    title = (d.get("title") or d.get("eventType") or "").strip()
                    if dt and title:
                        lines.append(f"{dt} — {title}")
                if lines:
                    list_of_dates_brief = "\n".join(lines)
        except Exception:
            pass

        return {
            "case_summary": case_summary,
            "applicable_laws_brief": applicable_laws_brief,
            "strategy_brief": strategy_brief,
            "analysis_brief": analysis_brief,
            "list_of_dates_brief": list_of_dates_brief,
        }

    async def _generate_and_emit_drafting_suggestions(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """
        Generate the proactive Drafting Assistant panel suggestions and emit
        them as a document_feature_response (featureType="drafting_assistant").

        Cached in `_workspace_drafting_payload` for reconnect re-emit.
        """
        if not self._socket_client:
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent

            role_upper = (workspace_role or "ADJUDICATOR").strip().upper() or "ADJUDICATOR"
            # Make sure the lawyer's template library is loaded into the
            # in-memory store BEFORE pre-matching. The backend only pushes
            # template_sync on upload/rename/delete, so after an AI restart
            # the store is cold — request a full snapshot and wait for it.
            await self.ensure_templates_synced(user_id)
            brief = self._build_case_brief_for_drafting(workspace_id)
            suggestions = await drafting_agent.suggest(
                role=role_upper,
                case_summary=brief["case_summary"],
                applicable_laws_brief=brief["applicable_laws_brief"],
                strategy_brief=brief["strategy_brief"],
                analysis_brief=brief["analysis_brief"],
                list_of_dates_brief=brief["list_of_dates_brief"],
                # Pre-match each suggestion against the lawyer's template
                # library so the response carries templateId / templateName
                # per card. Frontend can display "Uses <template>" badge
                # and pass templateId straight through on click, skipping
                # a second matcher round-trip server-side.
                user_id=user_id,
            )

            payload = {"suggestedDrafts": suggestions or []}
            self._workspace_drafting_payload[workspace_id] = payload

            # Record provenance so the panel can self-heal if a template
            # finishes ingesting after this point (slow scanned-PDF OCR that
            # missed the template-sync window). `lib_count` is the template
            # count these suggestions were pre-matched against; a later growth
            # triggers a regenerate. See _maybe_reemit_drafting_on_templates.
            try:
                from app.langgraph.template_store import template_store

                lib_count = await template_store.count_for_user(user_id=user_id)
            except Exception:
                lib_count = 0
            self._drafting_meta_by_workspace[workspace_id] = {
                "user_id": user_id,
                "role": role_upper,
                "lib_count": lib_count,
            }

            # Log exactly what we're about to send back per suggestion —
            # title + the pre-matched template fields the frontend uses to
            # render the "Uses <template>" badge and pass templateId on click.
            logger.info(
                "📤 drafting_assistant suggestions payload (workspace={}, user={}): "
                "{} suggestion(s)",
                workspace_id,
                (user_id or "")[:12],
                len(payload["suggestedDrafts"]),
            )
            for idx, s in enumerate(payload["suggestedDrafts"]):
                if not isinstance(s, dict):
                    logger.info("   ✏️ [{}] (non-dict: {})", idx, type(s).__name__)
                    continue
                logger.info(
                    "   ✏️ [{}] title={!r} relevance={} matchMode={} "
                    "templateId={} templateName={!r}",
                    idx,
                    s.get("title"),
                    s.get("relevance"),
                    s.get("matchMode"),
                    s.get("templateId"),
                    s.get("templateName"),
                )

            response_body = {
                "featureType": "drafting_assistant",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            }
            # Full dump of the exact response body sent to the backend for
            # end-to-end verification (titles/descriptions/templateId/
            # templateName only — no credentials, so no redaction needed).
            try:
                logger.info(
                    "📦 drafting_assistant RESPONSE body to backend:\n{}",
                    json.dumps(response_body, indent=2, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.warning("Drafting suggestions: could not dump response body: {}", e)

            await self._socket_client.emit(
                "document_feature_response",
                response_body,
            )
            logger.success(
                "✅ Drafting suggestions sent to backend ({} item(s))",
                len(payload["suggestedDrafts"]),
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Drafting suggestions emit failed: {}", e)

    async def _maybe_reemit_drafting_on_templates(self, user_id: str) -> None:
        """Self-heal the Drafting Assistant panel after the template library
        CHANGES — a template lands late (upload) OR a template is removed
        (delete).

        On UPLOAD a card that had no match can pick up the new template; on
        DELETE a card matched to the now-gone template is demoted (its stale
        lock released) and re-matched against the current library — so a
        deleted template can never keep driving a suggestion. Matched cards
        whose template still exists stay frozen. The ``lib_count`` comparison
        fires on any COUNT CHANGE (grow or shrink) and debounces no-op
        snapshots (unchanged count) so reconnect re-sends don't churn.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return
        try:
            from app.langgraph.template_store import template_store

            current_count = await template_store.count_for_user(user_id=user_id)
        except Exception as e:
            logger.warning(
                "drafting re-emit: count_for_user failed (user={}): {}",
                user_id[:12],
                e,
            )
            return

        for workspace_id, meta in list(self._drafting_meta_by_workspace.items()):
            if (meta or {}).get("user_id") != user_id:
                continue
            prev_count = int((meta or {}).get("lib_count") or 0)
            if current_count == prev_count:
                continue  # library unchanged — no-op snapshot, nothing to do
            role = (meta or {}).get("role") or "ADJUDICATOR"
            logger.info(
                "🔁 Template library changed ({}→{}) — refreshing drafting "
                "suggestions for workspace={} (user={}) — matched cards whose "
                "template still exists stay frozen; deleted-template cards are "
                "demoted and re-matched",
                prev_count,
                current_count,
                workspace_id,
                user_id[:12],
            )
            # Bump the recorded count up front so a burst of template events
            # can't queue duplicate refreshes for the same change.
            self._drafting_meta_by_workspace[workspace_id] = {
                **(meta or {}),
                "lib_count": current_count,
            }
            asyncio.create_task(
                self._refresh_drafting_matches_on_growth(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                )
            )

    async def _refresh_drafting_matches_on_growth(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """Re-emit a workspace's drafting panel after the library grew,
        FREEZING already-matched cards.

        Unlike ``_generate_and_emit_drafting_suggestions`` (which re-rolls
        the LLM suggestions and would lose each card's advertised template),
        this preserves the previously-emitted cards: matched cards keep their
        template verbatim and only unmatched cards are re-matched against the
        larger library — so a freshly-uploaded template can fill in a card
        that had no match, but can never hijack a card that already advertised
        a different template.

        Falls back to a full regeneration when there is no prior payload to
        refresh (cold workspace).
        """
        if not self._socket_client:
            return
        prior_payload = self._workspace_drafting_payload.get(workspace_id) or {}
        prior_cards = prior_payload.get("suggestedDrafts") or []
        if not prior_cards:
            # Nothing to freeze — fall back to a full generation.
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent

            refreshed = await drafting_agent.rematch_unmatched_suggestions(
                user_id=user_id,
                suggestions=prior_cards,
            )
            payload = {"suggestedDrafts": refreshed or []}
            self._workspace_drafting_payload[workspace_id] = payload

            for idx, s in enumerate(payload["suggestedDrafts"]):
                if not isinstance(s, dict):
                    continue
                logger.info(
                    "   ✏️ [{}] title={!r} matchMode={} templateId={} "
                    "templateName={!r}",
                    idx,
                    s.get("title"),
                    s.get("matchMode"),
                    s.get("templateId"),
                    s.get("templateName"),
                )

            response_body = {
                "featureType": "drafting_assistant",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": payload,
            }
            await self._socket_client.emit(
                "document_feature_response",
                response_body,
            )
            logger.success(
                "✅ Drafting panel refreshed (frozen matched cards) — "
                "{} item(s) re-emitted (workspace={})",
                len(payload["suggestedDrafts"]),
                workspace_id,
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Drafting suggestions refresh failed: {}", e)

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

    def _schedule_list_of_dates_and_applicable_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
        *,
        log_context: str = "",
        deleted_doc_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Run list_of_dates → applicable_laws → procedural_timeline without blocking the
        active socket handler, so user_message_request (intelligence chat) can be
        processed while these emit.

        ``deleted_doc_ids`` is forwarded to the dated-feature regeneration
        helpers so they can purge cached entries that came from documents
        the user just removed (see workspace_sync deletion path). When
        omitted (e.g. upload-driven regeneration) the helpers run in their
        existing lenient mode.
        """
        ctx = log_context or ""

        async def _runner() -> None:
            try:
                await self._generate_and_emit_list_of_dates(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=batch_result,
                    deleted_doc_ids=deleted_doc_ids,
                )
            except Exception as lod_err:
                logger.warning(
                    "List of dates (background{}) failed: {}",
                    ctx,
                    lod_err,
                )
            try:
                logger.info(
                    "Applicable laws: invoking (background{}) for workspace {}",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                )
                await self._generate_and_emit_applicable_laws(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                )
            except Exception as laws_err:
                logger.warning(
                    "Applicable laws (background{}) failed: {}",
                    ctx,
                    laws_err,
                )
            try:
                logger.info(
                    "Procedural timeline: invoking (background{}) for workspace {}",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                )
                await self._generate_and_emit_procedural_timeline(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=batch_result,
                    deleted_doc_ids=deleted_doc_ids,
                )
            except Exception as pt_err:
                logger.warning(
                    "Procedural timeline (background{}) failed: {}",
                    ctx,
                    pt_err,
                )
            try:
                logger.info(
                    "Drafting suggestions: invoking (background{}) for workspace {}",
                    ctx,
                    workspace_id[:12] if workspace_id else "",
                )
                await self._generate_and_emit_drafting_suggestions(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                )
            except Exception as draft_err:
                logger.warning(
                    "Drafting suggestions (background{}) failed: {}",
                    ctx,
                    draft_err,
                )

        try:
            asyncio.create_task(_runner())
            logger.debug(
                "Scheduled list_of_dates + applicable_laws + procedural_timeline + drafting_suggestions{} for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule timeline/laws task{}: {}",
                ctx,
                rt_err,
            )

    def _schedule_applicable_laws_only(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        *,
        log_context: str = "",
    ) -> None:
        ctx = log_context or ""

        async def _runner() -> None:
            try:
                await self._generate_and_emit_applicable_laws(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                )
            except Exception as laws_err:
                logger.warning(
                    "Applicable laws only (background{}) failed: {}",
                    ctx,
                    laws_err,
                )

        try:
            asyncio.create_task(_runner())
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule applicable_laws task{}: {}",
                ctx,
                rt_err,
            )

    async def _emit_workspace_refresh(
        self,
        workspace_id: str,
        user_id: Optional[str] = None,
        *,
        reason: str = "ai_update_started",
        feature_types: Optional[List[str]] = None,
    ) -> None:
        """
        Emit FE loader hint before heavy AI refresh/regeneration starts.
        """
        if not self._socket_client or not workspace_id:
            return
        features = feature_types or [
            "strategy_objective",
            "analysis_report",
            "list_of_dates",
            "applicable_laws",
            "procedural_timeline",
        ]
        payload: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "featureTypes": features,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if user_id:
            payload["userId"] = user_id
        try:
            await self._socket_client.emit("workspace:refresh", payload)
            logger.info(
                "workspace:refresh emitted (reason={}, features={}) for workspace {}",
                reason,
                len(features),
                workspace_id[:12] if workspace_id else "",
            )
        except Exception as e:
            logger.warning("workspace:refresh emit failed: {}", e)

    @staticmethod
    def _canonical_text_key(text: Any) -> str:
        s = str(text or "").strip().lower()
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _merge_unique_string_lists(old_list: Any, new_list: Any, limit: int = 12) -> List[str]:
        out: List[str] = []
        seen = set()
        for src in (old_list or [], new_list or []):
            if not isinstance(src, list):
                continue
            for item in src:
                text = str(item or "").strip()
                if not text:
                    continue
                key = SocketEventHandler._canonical_text_key(text)
                if key in seen:
                    continue
                seen.add(key)
                out.append(text)
                if len(out) >= limit:
                    return out
        return out

    def _merge_analysis_report_for_objective_change(
        self, old_report: Dict[str, Any], new_report: Dict[str, Any]
    ) -> Dict[str, Any]:
        old_report = old_report or {}
        new_report = new_report or {}
        merged = dict(old_report)

        for key, limit in (
            ("missingInfo", 10),
            ("weaknesses", 8),
            ("risks", 8),
            ("relevantJurisdiction", 6),
            ("pendingActions", 6),
            ("nextSteps", 6),
            ("suggestedDrafts", 6),
        ):
            merged[key] = self._merge_unique_string_lists(
                old_report.get(key), new_report.get(key), limit=limit
            )

        old_strengths = old_report.get("strengths") or []
        new_strengths = new_report.get("strengths") or []
        strengths_map: Dict[str, Dict[str, Any]] = {}
        for s in (old_strengths + new_strengths):
            if not isinstance(s, dict):
                continue
            point = str(s.get("point") or "").strip()
            if not point:
                continue
            key = self._canonical_text_key(point)
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            prev = strengths_map.get(key)
            if prev:
                prev["laws"] = self._merge_unique_string_lists(
                    prev.get("laws"), laws, limit=6
                )
            else:
                strengths_map[key] = {
                    "point": point,
                    "laws": self._merge_unique_string_lists([], laws, limit=6),
                }
        merged["strengths"] = list(strengths_map.values())[:8]

        merged["caseResolutionScore"] = int(
            new_report.get("caseResolutionScore")
            if new_report.get("caseResolutionScore") is not None
            else old_report.get("caseResolutionScore") or 0
        )
        merged["qualityScoreExplanation"] = (
            str(new_report.get("qualityScoreExplanation") or "").strip()
            or str(old_report.get("qualityScoreExplanation") or "").strip()
        )
        merged["reportQualityScoreExplanation"] = (
            str(new_report.get("reportQualityScoreExplanation") or "").strip()
            or str(old_report.get("reportQualityScoreExplanation") or "").strip()
        )
        return merged

    def _merge_applicable_laws_for_objective_change(
        self, old_payload: Dict[str, Any], new_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        def _law_key(law: Dict[str, Any]) -> str:
            act = self._canonical_text_key(law.get("act") or "")
            sec_raw = str(law.get("section") or "").strip().lower()
            sec_raw = re.sub(r"\b(section|sec\.?)\b", " ", sec_raw)
            sec_raw = re.sub(r"[^a-z0-9]", " ", sec_raw)
            sec = re.sub(r"\s+", " ", sec_raw).strip()
            return f"{act}::{sec}"

        old_laws = (old_payload or {}).get("laws") or []
        new_laws = (new_payload or {}).get("laws") or []
        merged_by_key: Dict[str, Dict[str, Any]] = {}

        for law in old_laws:
            if not isinstance(law, dict):
                continue
            act = str(law.get("act") or "").strip()
            sec = str(law.get("section") or "").strip()
            if not act or not sec:
                continue
            merged_by_key[_law_key(law)] = dict(law)

        for law in new_laws:
            if not isinstance(law, dict):
                continue
            act = str(law.get("act") or "").strip()
            sec = str(law.get("section") or "").strip()
            if not act or not sec:
                continue
            k = _law_key(law)
            if k in merged_by_key:
                old = merged_by_key[k]
                merged_by_key[k] = {
                    **old,
                    **law,
                    "description": str(law.get("description") or old.get("description") or "").strip(),
                    "relevance": str(law.get("relevance") or old.get("relevance") or "Medium").strip(),
                }
            else:
                merged_by_key[k] = dict(law)

        merged_laws = list(merged_by_key.values())[:12]
        return {"laws": merged_laws}

    def _merge_list_of_dates_for_objective_change(
        self, old_dates: List[Dict[str, Any]], new_dates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for item in (old_dates or []) + (new_dates or []):
            if not isinstance(item, dict):
                continue
            date_v = str(item.get("date") or "").strip()
            title_v = self._canonical_text_key(item.get("title") or "")
            if not (date_v or title_v):
                continue
            # Dedupe by date + normalized title (ignore source doc IDs to avoid repeats across refreshes)
            key = f"{date_v}::{title_v}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged[:200]

    async def _llm_semantic_keep_indexes(
        self,
        items: List[str],
        *,
        item_type: str,
        max_keep: int,
    ) -> Optional[List[int]]:
        if not items:
            return []
        try:
            from app.ai.gemini_client import gemini_client

            numbered = "\n".join(f"{i+1}. {v}" for i, v in enumerate(items))
            prompt = f"""You are a strict semantic deduplicator.
Given the numbered {item_type} list below, remove duplicates by MEANING (not wording).
If two or more items mean the same thing, keep only the best, most specific one.
Preserve distinct items even if related.

Return ONLY JSON:
{{"keepIndexes":[1,2,5]}}

Rules:
- keepIndexes must be 1-based indexes from the list.
- No duplicates in keepIndexes.
- Keep original order.
- Keep at most {max_keep} items.

{item_type.upper()}:
{numbered}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            keep = parsed.get("keepIndexes") if isinstance(parsed, dict) else None
            if not isinstance(keep, list):
                return None
            cleaned: List[int] = []
            seen = set()
            for idx in keep:
                try:
                    n = int(idx)
                except Exception:
                    continue
                if n < 1 or n > len(items) or n in seen:
                    continue
                seen.add(n)
                cleaned.append(n)
                if len(cleaned) >= max_keep:
                    break
            return cleaned or None
        except Exception as e:
            logger.warning("LLM semantic dedupe failed for {}: {}", item_type, e)
            return None

    async def _llm_semantic_dedupe_strings(
        self, items: List[str], *, item_type: str, max_keep: int
    ) -> List[str]:
        if not items:
            return []
        keep = await self._llm_semantic_keep_indexes(
            items, item_type=item_type, max_keep=max_keep
        )
        if not keep:
            return items[:max_keep]
        out = [items[i - 1] for i in keep if 1 <= i <= len(items)]
        return out[:max_keep]

    async def _llm_merge_against_previous(
        self,
        prev_items: List[str],
        new_items: List[str],
        *,
        item_type: str,
        max_keep: int,
    ) -> List[str]:
        """Single Gemini call that merges a fresh-regeneration list with the
        previously-emitted list, preserving prev verbatim and adding only
        genuinely new items.

        Replaces the older "union + flat dedupe" pattern. The flat dedup didn't
        know which items were old vs new, so when the LLM saw a paraphrased
        pair it sometimes kept the new wording and dropped the old one — which
        the user has been seeing in the UI for hours and now suddenly
        disappears. This call gives the LLM the prev/new split explicitly and
        instructs it to keep prev verbatim, drop new items that are paraphrases
        of any prev item, and append the rest.

        Returns the merged list. Falls back to ``prev_items + (non-empty new
        items not exact-duplicates of prev) [:max_keep]`` on any LLM failure.
        """
        prev_clean = [s.strip() for s in (prev_items or []) if isinstance(s, str) and s.strip()]
        new_clean = [s.strip() for s in (new_items or []) if isinstance(s, str) and s.strip()]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            # Nothing to merge against — just dedupe within new.
            return await self._llm_semantic_dedupe_strings(
                new_clean, item_type=item_type, max_keep=max_keep,
            )
        if not new_clean:
            return prev_clean[:max_keep]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_clean))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_clean))
            prompt = f"""You are merging two versions of a {item_type} list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved verbatim, in original order):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new items):
{numbered_new}

YOUR TASK: produce the cumulative list the user should see.

RULES (apply strictly):
1. Keep EVERY item from PREVIOUS verbatim, in its original order.
2. From NEW: append an item ONLY if it expresses a genuinely DIFFERENT meaning from EVERY item already in PREVIOUS.
3. Treat as DUPLICATES (drop the NEW item, do not append):
     - Wording variations of the same concept ("Name of the Petitioner's bank" vs "Name of the Petitioner's bank and specific branch").
     - Paraphrased restatements of the same fact / requirement / risk.
     - Same underlying entity / document / event with slightly different framing.
4. KEEP as new (do append) only when the NEW item refers to a clearly distinct object, requirement, or concern not already covered by any PREVIOUS item.
5. Do NOT merge items together, do NOT rewrite anything — just preserve PREVIOUS verbatim and append the genuine novelties from NEW.
6. Cap the final list at {max_keep} items. If more would qualify, drop the lowest-priority ones from NEW.

Return ONLY this JSON, no commentary:
{{"merged":["...","..."]}}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            merged_raw = parsed.get("merged") if isinstance(parsed, dict) else None
            if not isinstance(merged_raw, list) or not merged_raw:
                raise ValueError(f"missing or empty 'merged' field: {parsed!r}")
            merged: List[str] = []
            seen_lower: set = set()
            for x in merged_raw:
                s = str(x).strip()
                if not s:
                    continue
                key = s.lower()
                if key in seen_lower:
                    continue
                seen_lower.add(key)
                merged.append(s)
                if len(merged) >= max_keep:
                    break
            # Sanity: every prev item should be present (the LLM was instructed
            # to preserve them verbatim). If the LLM dropped one, splice it
            # back to honour the contract; the user's already seen it.
            for prev_item in prev_clean:
                if prev_item.lower() not in seen_lower:
                    if len(merged) < max_keep:
                        merged.append(prev_item)
                        seen_lower.add(prev_item.lower())
            logger.info(
                "LLM merge ({}): prev={} new={} → merged={}",
                item_type, len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous failed for {}: {}; falling back to "
                "prev + non-duplicate-by-string new",
                item_type, e,
            )
            # Conservative fallback: keep prev verbatim, append new items that
            # aren't exact case-insensitive matches of any prev item.
            seen_lower = {p.lower() for p in prev_clean}
            fallback = list(prev_clean)
            for n in new_clean:
                if n.lower() in seen_lower:
                    continue
                seen_lower.add(n.lower())
                fallback.append(n)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_merge_strengths_against_previous(
        self,
        prev_strengths: List[Dict[str, Any]],
        new_strengths: List[Dict[str, Any]],
        *,
        max_keep: int = 8,
    ) -> List[Dict[str, Any]]:
        """Strengths variant of `_llm_merge_against_previous`. Operates on
        ``[{"point": str, "laws": [str, ...]}]`` and asks Gemini to keep prev
        verbatim then append only genuinely-new strengths from the regenerated
        list. Returns the merged list with prev objects preserved as-is.
        """
        prev_clean = [
            s for s in (prev_strengths or [])
            if isinstance(s, dict) and str(s.get("point") or "").strip()
        ]
        new_clean = [
            s for s in (new_strengths or [])
            if isinstance(s, dict) and str(s.get("point") or "").strip()
        ]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            return await self._llm_semantic_dedupe_strengths(new_clean, max_keep=max_keep)
        if not new_clean:
            return prev_clean[:max_keep]

        def _label(s: Dict[str, Any]) -> str:
            point = str(s.get("point") or "").strip()
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            laws_s = ", ".join(str(x).strip() for x in laws if str(x).strip())
            return point if not laws_s else f"{point} | laws: {laws_s}"

        prev_labels = [_label(s) for s in prev_clean]
        new_labels = [_label(s) for s in new_clean]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = f"""You are merging two versions of a CASE STRENGTHS list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new strengths):
{numbered_new}

YOUR TASK: produce the cumulative strengths list using indexes:
- Output an array of items, each with a "source" ("prev" or "new") and "index" (1-based).
- Include EVERY item from PREVIOUS (in order), as ``{{"source":"prev","index":N}}``.
- After all PREVIOUS items, append NEW items as ``{{"source":"new","index":M}}`` ONLY if they express a genuinely DIFFERENT strength from EVERY item already in PREVIOUS.
- Drop NEW items that are paraphrased duplicates of any PREVIOUS item.
- Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev_indexes: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_clean):
                    merged.append(prev_clean[idx - 1])
                    included_prev_indexes.add(idx)
                elif src == "new" and 1 <= idx <= len(new_clean):
                    merged.append(new_clean[idx - 1])
                if len(merged) >= max_keep:
                    break

            # Splice back any prev items the LLM dropped — they were already
            # shown to the user; honour the contract.
            for i, prev_item in enumerate(prev_clean, start=1):
                if i not in included_prev_indexes and len(merged) < max_keep:
                    merged.append(prev_item)

            logger.info(
                "LLM merge (strengths): prev={} new={} → merged={}",
                len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous (strengths) failed: {}; "
                "falling back to prev + non-duplicate-by-point new",
                e,
            )
            seen_points = {str(s.get("point") or "").strip().lower() for s in prev_clean}
            fallback = list(prev_clean)
            for s in new_clean:
                pkey = str(s.get("point") or "").strip().lower()
                if pkey in seen_points:
                    continue
                seen_points.add(pkey)
                fallback.append(s)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_semantic_dedupe_strengths(
        self, strengths: List[Dict[str, Any]], *, max_keep: int = 8
    ) -> List[Dict[str, Any]]:
        if not strengths:
            return []
        lines = []
        valid_strengths: List[Dict[str, Any]] = []
        for s in strengths:
            if not isinstance(s, dict):
                continue
            point = str(s.get("point") or "").strip()
            if not point:
                continue
            laws = s.get("laws") if isinstance(s.get("laws"), list) else []
            laws_s = ", ".join(str(x).strip() for x in laws if str(x).strip())
            lines.append(point if not laws_s else f"{point} | laws: {laws_s}")
            valid_strengths.append(s)
        if not valid_strengths:
            return []
        keep = await self._llm_semantic_keep_indexes(
            lines, item_type="analysis strengths", max_keep=max_keep
        )
        if not keep:
            return valid_strengths[:max_keep]
        return [valid_strengths[i - 1] for i in keep if 1 <= i <= len(valid_strengths)][
            :max_keep
        ]

    async def _llm_merge_dated_entries_against_previous(
        self,
        prev_entries: List[Dict[str, Any]],
        new_entries: List[Dict[str, Any]],
        *,
        item_type: str,
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
        max_keep: int = 200,
    ) -> List[Dict[str, Any]]:
        """LLM-only prev-vs-new merge for dated entries (list_of_dates,
        procedural_timeline). Keeps every prev entry, appends new entries only
        when the LLM judges them a genuinely different event from every prev
        entry on the same date.

        Uses index-based references so the original entry dicts (with
        sourceDocId, pageNumber, eventType, etc.) are preserved verbatim.
        """
        prev_clean = [
            e for e in (prev_entries or [])
            if isinstance(e, dict) and str(e.get(date_field) or "").strip()
        ]
        new_clean = [
            e for e in (new_entries or [])
            if isinstance(e, dict) and str(e.get(date_field) or "").strip()
        ]

        if not prev_clean and not new_clean:
            return []
        if not prev_clean:
            return new_clean[:max_keep]
        if not new_clean:
            return prev_clean[:max_keep]

        def _label(e: Dict[str, Any]) -> str:
            d = str(e.get(date_field) or "").strip()
            t = str(e.get(title_field) or "").strip()
            desc = str(e.get(description_field) or "").strip()
            parts = [d, t]
            if desc:
                parts.append(desc)
            return " — ".join(p for p in parts if p)

        prev_labels = [_label(e) for e in prev_clean]
        new_labels = [_label(e) for e in new_clean]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = f"""You are merging two versions of a {item_type} timeline from regenerations of the same legal case.

Each entry is formatted as ``<date> — <title> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new events):
{numbered_new}

YOUR TASK: produce the cumulative timeline by reference, NOT by rewriting.

RULES (apply strictly):
1. Include EVERY item from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append items from NEW as ``{{"source":"new","index":M}}`` ONLY when they describe a genuinely DIFFERENT event from every PREVIOUS item.

3. **DESCRIPTION is a primary duplicate signal** — weight it equally with title.
   Two entries on the SAME date (or within ~3 days) are DUPLICATES when their
   descriptions describe the same action/observation, EVEN IF the titles are
   worded differently. The model writing each version may pick different
   titles ("Marked as Absconded" vs "Alleged Absconding Date") for the same
   underlying fact — do not treat the title difference as evidence of distinct
   events when the descriptions are about the same thing.

   Two descriptions are "the same action/observation" when they reference:
     • the same actor (the company / the bank / the court / the petitioner),
     • the same action (marked, signed, filed, deposited, dismissed), and
     • the same object/target (the employee, the contract, the petition).
   Allowed paraphrasing: a specific name vs a generic referent
   ("Yogesh" ↔ "the employee"), tense ("marked" ↔ "has marked"), and
   trailing date phrasing ("on February 21st" ↔ "" when the entry's date is
   already 2026-02-21).

   **Worked example (drop the NEW one):**
     PREVIOUS: ``2026-02-21 — Marked as Absconded — The company marked Yogesh as absconded after his visit on February 21st.``
     NEW:      ``2026-02-21 — Alleged Absconding Date — The company marked the employee as absconded after his visit.``
     → Same date, same actor (company), same action (marked absconded), same
       target (Yogesh = "the employee"). DUPLICATE. Drop the NEW entry.

4. **KEEP genuinely distinct events** even when they share a date — multiple
   legal events can occur on the same day. Two entries on the same date are
   distinct when their descriptions reference different actors, actions, OR
   objects (e.g. "Bank notice issued" + "Reply filed" on the same day are two
   different events).

5. Other duplicate patterns to drop:
     - "01-04-2020 Initial Plot Booking" vs "01-04-2020 First booking made".
     - Same legal filing / hearing / deadline on the same or adjacent date
       phrased with different wording.
     - The same notice / order / payment described with title variations.

6. Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_clean):
                    merged.append(prev_clean[idx - 1])
                    included_prev.add(idx)
                elif src == "new" and 1 <= idx <= len(new_clean):
                    merged.append(new_clean[idx - 1])
                if len(merged) >= max_keep:
                    break

            # Splice back any prev entries the LLM dropped — they were already
            # shown to the user; honour the contract.
            for i, prev_item in enumerate(prev_clean, start=1):
                if i not in included_prev and len(merged) < max_keep:
                    merged.append(prev_item)

            logger.info(
                "LLM merge ({}): prev={} new={} → merged={}",
                item_type, len(prev_clean), len(new_clean), len(merged),
            )
            return merged[:max_keep]
        except Exception as e:
            logger.warning(
                "LLM merge against previous ({}) failed: {}; "
                "falling back to prev + new minus exact (date,title) matches",
                item_type, e,
            )
            seen_keys = {
                (
                    str(p.get(date_field) or "").strip(),
                    str(p.get(title_field) or "").strip().lower(),
                )
                for p in prev_clean
            }
            fallback = list(prev_clean)
            for n in new_clean:
                key = (
                    str(n.get(date_field) or "").strip(),
                    str(n.get(title_field) or "").strip().lower(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                fallback.append(n)
                if len(fallback) >= max_keep:
                    break
            return fallback[:max_keep]

    async def _llm_classify_factual_vs_procedural(
        self,
        entries: List[Dict[str, Any]],
        *,
        item_type: str = "list of dates",
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
    ) -> List[Dict[str, Any]]:
        """LLM-based filter that drops procedural / lawyer-side entries from a
        factual-events list, keeping only entries that describe what happened
        in the real world to/between the parties.

        Replaces the earlier hardcoded title-keyword regex. The LLM is given
        a numbered list of entries (date + title + description) and asked to
        classify each as 'factual' or 'procedural'. We keep only the
        'factual' indexes. Generalises across any case type (matrimonial,
        cyber-fraud, immigration, property, employment, etc.) and any
        procedural phrasing the upstream LLM might emit.

        On any LLM failure (no API key, parse error, count mismatch), returns
        the input unchanged — the cheap deterministic filters upstream
        (eventType, future-date, YYYY-01-01) still applied, so falling back
        to the unfiltered list is safe.
        """
        if not entries:
            return []
        # Cheap exit: nothing to classify if all entries are already trivially
        # factual by their normalized eventType (Incident / Agreement /
        # Payment / Communication). The LLM only needs to weigh in on
        # ambiguous types like "Other".
        ambiguous_indexes = [
            i for i, e in enumerate(entries)
            if isinstance(e, dict)
            and str(e.get("eventType") or "").strip() in ("Other", "")
        ]
        if not ambiguous_indexes:
            return entries

        try:
            from app.ai.gemini_client import gemini_client

            lines: List[str] = []
            for i, e in enumerate(entries):
                d = str(e.get(date_field) or "").strip()
                t = str(e.get(title_field) or "").strip()
                desc = str(e.get(description_field) or "").strip()
                lines.append(f"{i+1}. [{d}] {t} — {desc}")
            numbered = "\n".join(lines)

            prompt = f"""You are filtering a chronological list of dates extracted from a legal case file. Each entry has a date, a short title, and a one-sentence description.

Classify each entry as ONE of:
  - "factual"     : a real-world event that actually happened to or between
                    the parties (incident, accident, agreement signed, payment
                    made, marriage, separation, employment joining or quitting,
                    document handed over, transaction, communication exchanged,
                    medical examination, FIR registered as an event, site
                    inspection, possession delay, fraud transaction, etc.).
                    Anything a lawyer would mention when narrating "the story
                    of what happened" to a colleague.
  - "procedural"  : a lawyer-side / court-side / system-side milestone
                    (court filing, hearing, adjournment, judgment, summons,
                    legal / demand / statutory notice issuance, response
                    deadline, filing deadline, evidence deadline, compliance
                    deadline, limitation period, lawyer or client consultation,
                    mediation session, scheduled meeting, "case initiation",
                    report due, period of judicial inactivity, judge absent,
                    document expiry as a deadline). Anything that belongs on
                    a separate Procedural Timeline panel rather than the
                    factual narrative.

Apply general semantic judgment — do NOT rely on specific keywords. The same
entry may be procedural in one case and factual in another:
  - "Visa Expiry" is FACTUAL only when the dispute is itself about visa
    overstay / immigration; otherwise it's procedural / metadata.
  - "Notice Sent" is procedural; "Letter from employer demanding meeting"
    is factual communication.
  - "Marriage" is factual; "Marriage Certificate Filed in Court" is
    procedural.
  - "Application Submitted" is procedural (a filing).

If the entry's title is a vague placeholder ("Date Mentioned", "Event",
"Case Date") with no concrete event content, classify as "procedural" so it
gets dropped.

Return ONLY this JSON, no commentary:
{{"classifications":["factual","procedural", ...]}}

The array MUST have EXACTLY {len(entries)} entries, in the same order as
the numbered list below.

Entries:
{numbered}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            classifications = parsed.get("classifications") if isinstance(parsed, dict) else None
            if not isinstance(classifications, list):
                logger.warning(
                    "{}: classifier response missing 'classifications' list (got {}); "
                    "keeping all entries.",
                    item_type, type(classifications).__name__,
                )
                return entries

            kept: List[Dict[str, Any]] = []
            for i, entry in enumerate(entries):
                if i >= len(classifications):
                    # LLM returned fewer items than expected — keep the rest
                    # rather than discard.
                    kept.append(entry)
                    continue
                verdict = str(classifications[i] or "").strip().lower()
                if verdict == "procedural":
                    logger.info(
                        "List of dates: LLM classifier dropped procedural entry "
                        "(title={!r}, eventType={!r}, date={})",
                        str(entry.get(title_field) or ""),
                        str(entry.get("eventType") or ""),
                        str(entry.get(date_field) or ""),
                    )
                    continue
                # "factual" or any unknown verdict → keep (conservative).
                kept.append(entry)
            return kept
        except Exception as e:
            logger.warning(
                "{}: factual-vs-procedural classifier failed: {}; keeping all entries.",
                item_type, e,
            )
            return entries

    async def _dedupe_dated_entries_semantically(
        self,
        entries: List[Dict[str, Any]],
        *,
        item_type: str,
        title_field: str = "title",
        description_field: str = "description",
        date_field: str = "date",
    ) -> List[Dict[str, Any]]:
        """Drop near-duplicate dated entries that share the same date.

        Groups by ``date_field`` and, for each date with ≥2 entries, asks the
        same LLM-semantic dedupe used for analysis_report to keep only the
        meaning-distinct ones. Entries on different dates are never compared,
        so two genuinely separate events on the same day with different titles
        are preserved.

        Used for list_of_dates and procedural_timeline regenerations after a
        workspace_sync — without it, regenerating from edited transcripts
        produces near-duplicate entries with slightly different titles for the
        same date.
        """
        if not entries:
            return []

        grouped: Dict[str, List[int]] = {}
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            d = str(entry.get(date_field) or "").strip()
            if not d:
                continue
            grouped.setdefault(d, []).append(idx)

        # Indexes that survive dedup. Start with everything, then remove
        # within each duplicate group.
        kept_indexes: set[int] = set(range(len(entries)))

        for date_value, idxs in grouped.items():
            if len(idxs) < 2:
                continue

            def _label_for(entry: Dict[str, Any]) -> str:
                title = str(entry.get(title_field) or "").strip()
                desc = str(entry.get(description_field) or "").strip()
                return f"{title} — {desc}" if title and desc else (title or desc)

            labels = [_label_for(entries[i]) for i in idxs]
            if not all(labels):
                continue

            kept_label_indexes = await self._llm_semantic_keep_indexes(
                labels, item_type=f"{item_type} on {date_value}", max_keep=len(idxs)
            )
            if not kept_label_indexes:
                continue  # Dedupe failed; keep all on this date.

            keep_set_for_date = {idxs[i - 1] for i in kept_label_indexes if 1 <= i <= len(idxs)}
            for original_idx in idxs:
                if original_idx not in keep_set_for_date:
                    kept_indexes.discard(original_idx)

        if len(kept_indexes) == len(entries):
            return entries

        before = len(entries)
        deduped = [entries[i] for i in range(len(entries)) if i in kept_indexes]
        logger.info(
            "Dedupe ({}): {} entries → {} after per-date semantic dedup",
            item_type, before, len(deduped),
        )
        return deduped

    def _law_signature(self, law: Dict[str, Any]) -> str:
        """Stable identity for an applicable_laws entry: lowercase act+section."""
        act = str(law.get("act") or "").strip().lower()
        section = str(law.get("section") or "").strip().lower()
        # Normalise away whitespace and punctuation noise.
        act = re.sub(r"\s+", " ", act)
        section = re.sub(r"\s+", " ", section)
        return f"{act}|{section}"

    async def _merge_applicable_laws_with_previous(
        self,
        workspace_id: str,
        new_payload: Dict[str, Any],
        *,
        max_laws: int = 12,
    ) -> Dict[str, Any]:
        """LLM-only prev-vs-new merge for applicable_laws.

        Same explicit prev/new split as analysis_report and list_of_dates: the
        LLM sees the previously-cached laws and the freshly-regenerated laws
        with labels, returns merged indexes, and we map back to the original
        dict entries (preserving act/section/description/relevance verbatim).
        Caps at ``max_laws`` (existing 12 limit) and re-stamps stable law_N ids.
        """
        new_laws_raw = new_payload.get("laws") if isinstance(new_payload, dict) else None
        new_laws: List[Dict[str, Any]] = (
            [law for law in new_laws_raw if isinstance(law, dict)]
            if isinstance(new_laws_raw, list)
            else []
        )

        prev_payload = self._workspace_applicable_laws_payload.get(workspace_id)
        prev_laws: List[Dict[str, Any]] = []
        if isinstance(prev_payload, dict):
            prev_laws_raw = prev_payload.get("laws")
            if isinstance(prev_laws_raw, list):
                prev_laws = [law for law in prev_laws_raw if isinstance(law, dict)]

        # No prior cache → just take new (cap + stamp ids).
        if not prev_laws:
            ordered = new_laws[:max_laws]
            for i, law in enumerate(ordered):
                law["id"] = f"law_{i+1}"
            return {"laws": ordered}
        # No new laws → keep prev verbatim.
        if not new_laws:
            ordered = prev_laws[:max_laws]
            for i, law in enumerate(ordered):
                law["id"] = f"law_{i+1}"
            return {"laws": ordered}

        def _label(law: Dict[str, Any]) -> str:
            act = str(law.get("act") or "").strip()
            section = str(law.get("section") or "").strip()
            desc = str(law.get("description") or "").strip()
            return f"{act} {section} — {desc}".strip(" —")

        prev_labels = [_label(law) for law in prev_laws]
        new_labels = [_label(law) for law in new_laws]

        try:
            from app.ai.gemini_client import gemini_client

            numbered_prev = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prev_labels))
            numbered_new = "\n".join(f"{i+1}. {s}" for i, s in enumerate(new_labels))
            prompt = f"""You are merging two versions of an APPLICABLE LAWS list from regenerations of the same Indian legal case.

Each entry is formatted as ``<Act> <Section> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new applicable provisions):
{numbered_new}

YOUR TASK: produce the cumulative applicable-laws list using indexes.

RULES (apply strictly):
1. Include EVERY entry from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append entries from NEW as ``{{"source":"new","index":M}}`` ONLY when they refer to a DIFFERENT statutory provision than every PREVIOUS entry.
3. Treat as DUPLICATES (drop the NEW item, do NOT append):
     - Same Act + same Section number, even if description is rephrased
       ("BNS Section 85 — cruelty by husband" vs "Bharatiya Nyaya Sanhita Section 85 — wife abuse").
     - Different formal name for the same provision (e.g. "BNS Sec. 85" and "BNS s.85").
     - Description rephrasing of an already-included section.
4. KEEP genuinely distinct sections, even from the same Act ("BNS Section 85" and "BNS Section 318" are different provisions).
5. Cap at {max_laws} entries total. Drop the lowest-priority NEW entries first if needed.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
            candidate = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            parsed = json.loads(candidate) if candidate else json.loads((raw or "").strip())
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError(f"missing/malformed 'items': {parsed!r}")

            merged: List[Dict[str, Any]] = []
            included_prev: set = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                src = str(entry.get("source") or "").strip().lower()
                try:
                    idx = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if src == "prev" and 1 <= idx <= len(prev_laws):
                    merged.append(dict(prev_laws[idx - 1]))
                    included_prev.add(idx)
                elif src == "new" and 1 <= idx <= len(new_laws):
                    merged.append(dict(new_laws[idx - 1]))
                if len(merged) >= max_laws:
                    break

            # Splice back any prev entries the LLM dropped.
            for i, prev_law in enumerate(prev_laws, start=1):
                if i not in included_prev and len(merged) < max_laws:
                    merged.append(dict(prev_law))

            logger.info(
                "LLM merge (applicable laws): prev={} new={} → merged={}",
                len(prev_laws), len(new_laws), len(merged),
            )
        except Exception as e:
            logger.warning(
                "LLM merge against previous (applicable laws) failed: {}; "
                "falling back to prev + non-duplicate-by-(act,section) new",
                e,
            )
            merged = [dict(p) for p in prev_laws]
            seen_sigs = {self._law_signature(p) for p in prev_laws if self._law_signature(p)}
            for n in new_laws:
                sig = self._law_signature(n)
                if not sig or sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                merged.append(dict(n))
                if len(merged) >= max_laws:
                    break

        for i, law in enumerate(merged[:max_laws]):
            law["id"] = f"law_{i+1}"
        return {"laws": merged[:max_laws]}

    async def _merge_analysis_report_with_previous(
        self,
        workspace_id: str,
        workspace_role: str,
        new_analysis: Dict[str, Any],
        *,
        log_context: str = "",
    ) -> Dict[str, Any]:
        """Merge a freshly-regenerated analysis report against the cached
        previous version: filter rejected items, union prev + new, run LLM
        semantic dedup per list field, preserve identity + score, regenerate
        explanations.

        Used after workspace_sync regenerations (e.g. transcript edit + reupload)
        so duplicates from the previous report don't surface again. Mirrors the
        merge pattern already proven on the strategic-objective-change path
        (`_run_objective_change_incremental_refresh`).

        Returns the merged report. Caller is responsible for caching + emitting.
        """
        ctx = log_context or " (sync merge)"
        prev_raw = self._workspace_analysis_payload.get(workspace_id)
        prev_analysis: Dict[str, Any] = (
            dict(prev_raw) if isinstance(prev_raw, dict) else {}
        )

        # No prior cached version → nothing to dedupe against; first emit.
        if not prev_analysis:
            return new_analysis

        merged: Dict[str, Any] = dict(new_analysis)

        def _str_list(items: Any) -> List[str]:
            return _coerce_analysis_text_list(items)

        def _filter_rejected(section: str, items: List[str]) -> List[str]:
            if not items:
                return items
            return [
                s
                for s in items
                if not self._is_previously_rejected_analysis_item(
                    workspace_id=workspace_id, section=section, candidate=s,
                )
            ]

        new_missing_filtered = _filter_rejected(
            "missingInfo", _str_list(new_analysis.get("missingInfo"))
        )
        new_weak_filtered = _filter_rejected(
            "riskOrWeakness", _str_list(new_analysis.get("weaknesses"))
        )
        new_risks_filtered = _filter_rejected(
            "riskOrWeakness", _str_list(new_analysis.get("risks"))
        )

        # All list fields go through the SAME prev-vs-new LLM merge so the
        # model sees the explicit "this is what the user already saw" / "this
        # is the freshly regenerated candidate" split. Replaces the older
        # "union + flat dedup" which lost that signal and sometimes kept the
        # newly-rephrased copy while dropping the original the user has been
        # seeing in the UI.
        merged["missingInfo"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("missingInfo")),
            new_missing_filtered,
            item_type="missing information items",
            max_keep=10,
        )
        merged["weaknesses"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("weaknesses")),
            new_weak_filtered,
            item_type="weaknesses",
            max_keep=8,
        )
        merged["risks"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("risks")),
            new_risks_filtered,
            item_type="risks",
            max_keep=8,
        )
        merged["pendingActions"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("pendingActions")),
            _str_list(new_analysis.get("pendingActions")),
            item_type="pending actions",
            max_keep=6,
        )
        merged["nextSteps"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("nextSteps")),
            _str_list(new_analysis.get("nextSteps")),
            item_type="next steps",
            max_keep=6,
        )
        merged["suggestedDrafts"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("suggestedDrafts")),
            _str_list(new_analysis.get("suggestedDrafts")),
            item_type="suggested drafts",
            max_keep=6,
        )
        merged["relevantJurisdiction"] = await self._llm_merge_against_previous(
            _str_list(prev_analysis.get("relevantJurisdiction")),
            _str_list(new_analysis.get("relevantJurisdiction")),
            item_type="relevant jurisdiction entries",
            max_keep=6,
        )

        prev_strengths = (
            prev_analysis.get("strengths")
            if isinstance(prev_analysis.get("strengths"), list)
            else []
        )
        new_strengths = (
            new_analysis.get("strengths")
            if isinstance(new_analysis.get("strengths"), list)
            else []
        )
        merged["strengths"] = await self._llm_merge_strengths_against_previous(
            prev_strengths, new_strengths, max_keep=8,
        )

        # Identity fields don't shift on a doc-edit re-upload — same case.
        for identity_field in (
            "caseTitle",
            "caseCategory",
            "subcategories",
            "description",
            "caseType",
        ):
            prev_val = prev_analysis.get(identity_field)
            if prev_val not in (None, "", []):
                merged[identity_field] = prev_val

        # Preserve the score (and any user-feedback deltas baked in), then
        # re-cap against the merged list state.
        prev_score_val = prev_analysis.get("caseResolutionScore")
        if prev_score_val is None:
            prev_score_val = prev_analysis.get("case_resolution_score")
        try:
            prev_score_int = (
                int(prev_score_val) if prev_score_val is not None else None
            )
        except (TypeError, ValueError):
            prev_score_int = None
        max_allowed = self._analysis_report_max_allowed_score(report=merged)
        if prev_score_int is not None:
            merged["caseResolutionScore"] = min(max(0, prev_score_int), max_allowed)
        # Guard: when the prior score was missing/None (preservation above
        # skipped) and the fresh analysis came back 0, a fully-resolved report
        # (no pending items, has strengths) must not display 0 — reflect the
        # full allowed score instead.
        try:
            _cur_score = int(merged.get("caseResolutionScore") or 0)
        except (TypeError, ValueError):
            _cur_score = 0
        if _cur_score <= 0 and (
            merged.get("strengths")
            or merged.get("missingInfo")
            or merged.get("weaknesses")
            or merged.get("risks")
        ):
            # A populated analysis report must never display 0%. When neither the
            # previous nor the fresh score is usable, derive one from content
            # (full allowed score if nothing is pending, otherwise the documented
            # deduction logic) rather than leaving the stuck 0.
            if max_allowed >= 100:
                derived_score = max_allowed
            else:
                derived_score = self._derive_case_resolution_score(
                    missing_info=merged.get("missingInfo")
                    or merged.get("missing_info"),
                    weaknesses=merged.get("weaknesses"),
                    risks=merged.get("risks"),
                    max_allowed=max_allowed,
                )
            logger.warning(
                "Analysis sync merge: score 0 for populated report, workspace "
                "{}{}; deriving {}",
                workspace_id,
                ctx,
                derived_score,
            )
            merged["caseResolutionScore"] = derived_score
        prev_report_score = prev_analysis.get("reportQualityScore")
        if prev_report_score is not None:
            try:
                prev_report_score_int = int(prev_report_score)
                max_allowed = self._analysis_report_max_allowed_score(report=merged)
                merged["reportQualityScore"] = min(
                    max(0, prev_report_score_int), max_allowed
                )
            except (TypeError, ValueError):
                pass

        try:
            score_for_expl = int(merged.get("caseResolutionScore") or 0)
        except (TypeError, ValueError):
            score_for_expl = 0
        try:
            fresh_explanation = await self._ensure_quality_score_explanation(
                workspace_role=workspace_role,
                case_resolution_score=score_for_expl,
                missing_info=merged.get("missingInfo") or [],
                weaknesses=merged.get("weaknesses") or [],
                risks=merged.get("risks") or [],
                strengths=merged.get("strengths") or [],
                existing="",
            )
        except Exception as expl_err:
            logger.warning(
                "Analysis sync merge: explanation regen failed{}: {}", ctx, expl_err
            )
            fresh_explanation = (
                merged.get("qualityScoreExplanation")
                or prev_analysis.get("qualityScoreExplanation")
                or ""
            )
        if fresh_explanation:
            merged["qualityScoreExplanation"] = fresh_explanation
            merged["reportQualityScoreExplanation"] = (
                self._sanitize_report_quality_score_explanation(fresh_explanation)
            ).strip()

        merged["timestamp"] = datetime.utcnow().isoformat() + "Z"
        self._normalize_analysis_report_lists(
            merged, max_annexure=self._get_annexure_count(workspace_id)
        )

        logger.info(
            "Analysis sync merge{}: prev_missing={} new_missing={} merged_missing={}; "
            "prev_weak={} new_weak={} merged_weak={}; prev_risks={} new_risks={} merged_risks={}",
            ctx,
            len(_str_list(prev_analysis.get("missingInfo"))),
            len(new_missing_filtered),
            len(merged.get("missingInfo") or []),
            len(_str_list(prev_analysis.get("weaknesses"))),
            len(new_weak_filtered),
            len(merged.get("weaknesses") or []),
            len(_str_list(prev_analysis.get("risks"))),
            len(new_risks_filtered),
            len(merged.get("risks") or []),
        )
        return merged

    async def _run_objective_change_incremental_refresh(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        *,
        log_context: str = "",
    ) -> None:
        ctx = log_context or " (objective change)"

        # 1) Analysis report: REGENERATE for the new objective and REPLACE the old
        # one — do NOT merge with the previous report. A change of objective is a
        # change of legal PERSPECTIVE (e.g. jewellery-theft → divorce); merging
        # with the prev report would keep the stale theft strengths/weaknesses/
        # risks and PRESERVE the old quality score, producing a mixed,
        # wrong-perspective analysis. The generator already (a) anchors to the new
        # cached objective, (b) applies OBJECTIVE-ALIGNMENT rules, (c) runs
        # `_normalize_analysis_report_lists` (which also scrubs the brand), and
        # (d) computes a fresh caseResolutionScore — so used directly it yields a
        # fully objective-aligned report with a fresh score and no carryover.
        # (`_merge_analysis_report_with_previous` is unchanged and still used by
        # the document re-upload / sync paths, where incremental merge is correct.)
        new_analysis = await self._generate_analysis_report_from_documents(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=workspace_role,
            batch_result=None,
            is_update=False,
        )
        if not (isinstance(new_analysis, dict) and new_analysis):
            new_analysis = self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )

        self._workspace_analysis_payload[workspace_id] = new_analysis
        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "analysis_report",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": new_analysis,
                },
            )
            logger.success(
                "✅ Objective-change analysis update emitted "
                "(fully regenerated for new objective, prev replaced){}",
                ctx,
            )
            # Fresh objective-aligned report is emitted; lift suppression so future emits are allowed.
            self._suppress_analysis_emit_until_ts.pop(workspace_id, None)

        # The new objective text (set by the strategy regen that ran before this
        # cascade) drives the list-of-dates relevance re-score and the applicable
        # laws regeneration below.
        _strat_now = self._workspace_strategy_payload.get(workspace_id) or {}
        new_objective_text = (
            (_strat_now.get("strategicObjective") or "").strip()
            if isinstance(_strat_now, dict)
            else ""
        )

        # 2) List of dates: keep the SAME entries but RE-SCORE their relevance for
        # the new objective/perspective (Mode 2). No new/removed dates, no text
        # rewrite. Falls back to a plain preserve-and-re-emit if the re-eval fails
        # or there is no new objective text to score against.
        relevance_done = False
        if new_objective_text:
            try:
                relevance_done = await self._reeval_list_of_dates_relevance(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    objective_text=new_objective_text,
                )
            except Exception as rel_err:
                logger.warning(
                    "List of dates relevance re-eval failed{}: {}", ctx, rel_err
                )
        if not relevance_done:
            preserved_dates = (
                self._workspace_list_of_dates_payload.get(workspace_id) or []
            )
            if not isinstance(preserved_dates, list):
                preserved_dates = []
            self._workspace_list_of_dates_payload[workspace_id] = preserved_dates
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": preserved_dates,
                        },
                    },
                )
                logger.success(
                    "✅ Objective-change list_of_dates preserved (relevance re-eval skipped){}",
                    ctx,
                )

        # 3) Applicable laws: REGENERATE for the new objective/perspective (Mode 2)
        # — e.g. divorce → judicial separation changes which statutes apply.
        # objective_context steers the law selection + search query, and
        # merge_with_previous=False replaces stale laws instead of unioning them.
        # Falls back to preserve-and-re-emit on failure.
        laws_done = False
        try:
            await self._generate_and_emit_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                objective_context=new_objective_text or None,
                merge_with_previous=False,
            )
            laws_done = True
        except Exception as laws_err:
            logger.warning(
                "Applicable laws regeneration failed{}: {}", ctx, laws_err
            )
        if not laws_done:
            preserved_laws = (
                self._workspace_applicable_laws_payload.get(workspace_id) or {}
            )
            if not isinstance(preserved_laws, dict):
                preserved_laws = {}
            self._workspace_applicable_laws_payload[workspace_id] = preserved_laws
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "applicable_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": preserved_laws,
                    },
                )
                logger.success(
                    "✅ Objective-change applicable_laws preserved (regeneration skipped){}",
                    ctx,
                )

        # 4) Upcoming dates (procedural timeline): preserve existing payload on
        # objective change — same preserve-and-re-emit pattern as list_of_dates /
        # applicable_laws above. Only regenerate the timeline when documents are
        # uploaded/deleted, never on an objective change.
        preserved_timeline = (
            self._workspace_procedural_timeline_payload.get(workspace_id) or []
        )
        if not isinstance(preserved_timeline, list):
            preserved_timeline = []
        self._workspace_procedural_timeline_payload[workspace_id] = preserved_timeline
        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "procedural_timeline",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": {"proceduralTimeline": preserved_timeline},
                },
            )
            logger.success(
                "✅ Objective-change preserved procedural_timeline re-emitted{}",
                ctx,
            )

        # 5) Drafting Assistant suggested drafts: REGENERATE for the new objective
        # (Mode 2). It rebuilds its brief from the now-updated caches (case
        # summary + regenerated strategy/objective + analysis + applicable laws +
        # dates), so the suggested drafts realign with the new perspective.
        try:
            await self._generate_and_emit_drafting_suggestions(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
            )
            logger.success(
                "✅ Objective-change drafting suggestions regenerated{}", ctx
            )
        except Exception as draft_err:
            logger.warning(
                "Objective-change drafting regeneration failed{}: {}",
                ctx,
                draft_err,
            )

    def _schedule_objective_change_reanalysis(
        self,
        workspace_id: str,
        user_id: str,
        *,
        log_context: str = "",
    ) -> None:
        """
        On strategic objective changes from intelligence chat:
        - regenerate analysis_report
        - preserve/re-emit current list_of_dates and applicable_laws
        (dates/laws regenerate only on document upload/delete flows).
        """
        ctx = log_context or " (objective change)"

        # Resolve role with safe fallbacks so regeneration is always attempted.
        role = "ADJUDICATOR"
        try:
            strategy_payload = self._workspace_strategy_payload.get(workspace_id) or {}
            candidate_role = (strategy_payload.get("role") or "").strip().upper()
            if candidate_role in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = candidate_role
        except Exception:
            pass
        try:
            ws_ctx = workspace_manager.get_workspace(workspace_id)
            if ws_ctx and ws_ctx.workspace_role:
                ctx_role = (ws_ctx.workspace_role or "").strip().upper()
                if ctx_role in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                    role = ctx_role
        except Exception:
            pass

        async def _runner() -> None:
            try:
                await self._run_objective_change_incremental_refresh(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=role,
                    log_context=ctx,
                )
            except Exception as rean_err:
                logger.warning(
                    "Objective-change incremental refresh{} failed: {}",
                    ctx,
                    rean_err,
                )

        try:
            asyncio.create_task(_runner())
            logger.info(
                "Scheduled objective-change reanalysis{} for workspace {}",
                ctx,
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as rt_err:
            logger.error(
                "Could not schedule objective-change reanalysis{}: {}",
                ctx,
                rt_err,
            )

    async def _run_strategy_analysis_timeline_pipeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Dict[str, Any],
        *,
        is_update: bool,
        analysis_log_context: str,
    ) -> None:
        """
        Strategy → analysis report → scheduled list_of_dates + applicable_laws.
        Runs inside asyncio.create_task so workspace_sync / workspace_created return quickly.
        """
        role = await self._resolve_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            preferred_role=workspace_role,
        )

        logger.info("Using workspace role for background AI pipeline: {}", role)
        try:
            strategies_result = await self._generate_strategies_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=role,
                batch_result=batch_result,
                is_update=is_update,
            )
            if strategies_result and self._socket_client:
                logger.success("✅ Strategies generated (background), emitting...")
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": strategies_result,
                    },
                )
                self._workspace_strategy_payload[workspace_id] = strategies_result
                chips = strategies_result.get("suggestionChips") or []
                if chips:
                    self._workspace_suggestion_chips[workspace_id] = list(chips)
                    self._last_workspace_id_with_chips = workspace_id
                logger.success("🎉 Strategy & Objectives sent to backend (background)")
        except Exception as strategy_error:
            logger.error("Background strategy generation failed: {}", strategy_error)
            logger.exception(strategy_error)
            if self._socket_client:
                try:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "error": str(strategy_error),
                        },
                    )
                except Exception as emit_error:
                    logger.error("Failed to emit strategy error (bg): {}", emit_error)

        await self._emit_analysis_report(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=role,
            batch_result=batch_result,
            is_update=is_update,
            log_context=analysis_log_context,
        )

    def _schedule_strategy_analysis_pipeline(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Dict[str, Any],
        *,
        is_update: bool,
        analysis_log_context: str,
    ) -> None:
        async def _runner() -> None:
            try:
                await self._run_strategy_analysis_timeline_pipeline(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=batch_result,
                    is_update=is_update,
                    analysis_log_context=analysis_log_context,
                )
            except Exception as e:
                logger.exception(
                    "Background strategy/analysis pipeline failed for workspace {}: {}",
                    workspace_id[:12] if workspace_id else "",
                    e,
                )

        try:
            asyncio.create_task(_runner())
            logger.info(
                "Scheduled background strategy + analysis + timeline/laws for workspace {}",
                workspace_id[:12] if workspace_id else "",
            )
        except RuntimeError as e:
            logger.error("Could not schedule strategy/analysis pipeline: {}", e)

    async def _emit_analysis_report(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]] = None,
        is_update: bool = False,
        log_context: str = "",
    ) -> bool:
        """
        Generate analysis report (or use fallback on error), then emit to backend and cache.
        Emit is done outside try/except so it always runs and errors propagate.
        """
        if not self._socket_client:
            return False
        try:
            analysis_report = await self._generate_analysis_report_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                is_update=is_update,
            )
            if not analysis_report:
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
        except Exception as e:
            logger.warning(
                "Analysis report generation failed{}: {}; using fallback",
                log_context,
                e,
            )
            analysis_report = self._analysis_report_fallback(
                (workspace_role or "ADJUDICATOR").strip().upper()
            )
        # Merge with the previously cached version when this is a regeneration
        # (audio re-uploaded, transcript edited, sync after add/edit). Without
        # this, the FE sees v1 from the first emit AND v2 from the second emit
        # with overlapping items — e.g. "Name of the Petitioner's bank and
        # specific branch" (v1) plus "Name of the Petitioner's bank" (v2).
        # The merge unions prev + new and runs LLM semantic dedup so each
        # emitted report is the cumulative state with no semantic duplicates,
        # rejection memory preserved, and identity/score from prev kept.
        if is_update and isinstance(analysis_report, dict):
            try:
                analysis_report = await self._merge_analysis_report_with_previous(
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    new_analysis=analysis_report,
                    log_context=log_context or " (emit)",
                )
            except Exception as merge_err:
                logger.warning(
                    "Analysis report merge with previous failed{}: {}; "
                    "emitting unmerged",
                    log_context, merge_err,
                )
        # Guard: if objective-change refresh is active, skip stale background emits
        # so workspace sees only objective-aligned analysis report.
        suppress_until = self._suppress_analysis_emit_until_ts.get(workspace_id, 0.0)
        if suppress_until and time.time() < suppress_until:
            logger.info(
                "Skipping stale analysis_report emit for workspace {} (suppressed for {:.1f}s more){}",
                workspace_id[:12] if workspace_id else "",
                max(0.0, suppress_until - time.time()),
                log_context,
            )
            return False
        # Cache and set pending re-emit before emit so re-emit on reconnect has data if emit fails
        self._workspace_analysis_payload[workspace_id] = analysis_report
        self._pending_reemit_workspace_id = workspace_id
        self._pending_reemit_user_id = user_id
        await self._socket_client.emit(
            "document_feature_response",
            {
                "featureType": "analysis_report",
                "workspaceId": workspace_id,
                "userId": user_id,
                "payload": analysis_report,
            },
        )
        logger.success("✅ Analysis report sent to backend{}", log_context)
        # Brief yield so the socket can flush before caller continues (reduces risk of disconnect before delivery)
        await asyncio.sleep(0.15)
        # Generate and cache 15-16 suggestion chips for Analysis Report (probing_prompts_request with chatType ANALYSIS_REPORT)
        try:
            role = (workspace_role or "ADJUDICATOR").strip().upper()
            analysis_chips = await self._generate_analysis_report_suggestion_chips(
                workspace_id=workspace_id,
                report=analysis_report,
                role=role,
            )
            if analysis_chips:
                self._workspace_analysis_suggestion_chips[workspace_id] = list(
                    analysis_chips
                )
                logger.info(
                    "Analysis report suggestion chips stored: {} for workspace {}",
                    len(analysis_chips),
                    workspace_id[:12] if workspace_id else "",
                )
        except Exception as chip_err:
            logger.warning("Analysis report chips generation failed: {}", chip_err)
        # List of Dates + Applicable laws: background so intelligence chat can proceed
        self._schedule_list_of_dates_and_applicable_laws(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=workspace_role,
            batch_result=batch_result,
            log_context=log_context or " (after analysis)",
        )
        return True

    async def _generate_and_emit_analysis_report_for_sync(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        batch_result: Optional[Dict[str, Any]],
    ) -> None:
        """
        Generate analysis report and emit to backend. Used as a background task from
        workspace_sync so the long-running Gemini call is not blocked by other socket events
        (e.g. probing_prompts_request). Ensures analysis report is always emitted.
        """
        try:
            analysis_report = await self._generate_analysis_report_from_documents(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                is_update=True,
            )
            if not analysis_report:
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
            # Merge with the previous cached version: drops items the user
            # already saw, preserves rejection memory, keeps identity + score.
            # Without this, a transcript-edit re-upload re-emits the prev
            # missingInfo / weaknesses / risks alongside any new ones, doubling
            # up the list visible in the UI.
            if isinstance(analysis_report, dict):
                analysis_report = await self._merge_analysis_report_with_previous(
                    workspace_id=workspace_id,
                    workspace_role=workspace_role,
                    new_analysis=analysis_report,
                    log_context=" (sync)",
                )
            if self._socket_client and analysis_report:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": analysis_report,
                    },
                )
                self._workspace_analysis_payload[workspace_id] = analysis_report
                logger.success("✅ Analysis report sent to backend (sync)")
            self._schedule_list_of_dates_and_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                log_context=" (sync helper)",
            )
        except asyncio.CancelledError:
            logger.warning("Analysis report (sync) task cancelled; emitting fallback")
            raise
        except Exception as ar_err:
            logger.warning(
                "Analysis report (sync) failed: {}; emitting fallback", ar_err
            )
            try:
                fallback = self._analysis_report_fallback(
                    (workspace_role or "ADJUDICATOR").strip().upper()
                )
                if self._socket_client:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": fallback,
                        },
                    )
                    self._workspace_analysis_payload[workspace_id] = fallback
                    logger.success(
                        "✅ Analysis report (fallback) sent to backend (sync)"
                    )
                self._schedule_list_of_dates_and_applicable_laws(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    workspace_role=workspace_role,
                    batch_result=batch_result,
                    log_context=" (sync fallback)",
                )
            except Exception as e:
                logger.error("Failed to emit analysis report fallback (sync): {}", e)

    # Fallback score bump when AI-based delta is unavailable
    _ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT = 4
    # Fallback score drop when AI-based delta is unavailable for added_missing_information
    _ADDED_MISSING_INFO_SCORE_DROP_DEFAULT = 3
    # Max score when Missing Information / Risks / Weaknesses still have items (100% only when all are empty)
    _ANALYSIS_REPORT_MAX_SCORE_WITH_PENDING_ITEMS = 95
    # Floor for a derived score: an analysed case with real content must never
    # read as 0% (the model occasionally omits/zeroes the score, or its JSON is
    # truncated). Used by _derive_case_resolution_score as the safety net.
    _ANALYSIS_REPORT_DERIVED_SCORE_FLOOR = 30

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
                f"You are a senior legal analyst. Given this case analysis summary (JSON only), "
                f"write exactly 3-5 sentences for an Indian-law workspace. "
                f'Start with "The current score is {case_resolution_score}% because". '
                "Then anchor the explanation to SPECIFIC items from the summary — quote (do not paraphrase generically) "
                "at least one missingInfo item by what is missing and from whom, one weakness or risk by the "
                "named party / date / amount / annexure that triggers it, and one strength by the named fact "
                "(party + date + Annexure) that supports it. If the score is 100, name the top 1-2 strengths "
                "instead and state that no missingInfo / weaknesses / risks were flagged. "
                "Never use generic phrasing like 'gaps in annexures' or 'procedural risks identified' without naming "
                "WHICH gap, WHICH risk, and FOR WHOM. Use names exactly as they appear in the summary; do NOT "
                "invent facts not present in the summary.\n\n"
                f"{summary}"
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
                            await self._socket_client.emit(
                                "document_feature_response",
                                {
                                    "featureType": "analysis_report",
                                    "workspaceId": workspace_id,
                                    "userId": user_id,
                                    "payload": fallback_payload,
                                },
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
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "analysis_report",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": fallback_payload,
                            },
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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": current_strategy,
                    },
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
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": current_report,
                    },
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
            prompt = f"""You are a legal case analyst. The user rejected ONE item from their analysis report as not relevant. Two decisions are required:

1) Is the rejection VALID? It is VALID only when the rejected item is genuinely NOT relevant to THIS case (per the case summary, strategic objective, and remaining items below). If the rejected item IS relevant — i.e. it is a real evidence gap, factual weakness, or procedural risk that this case actually faces — the rejection is INVALID and the user should not be rewarded with a score increase.
2) If VALID, choose an integer scoreIncrease 1-5. The system will ADD this to the current score. If INVALID, set scoreIncrease=0.

**Current case resolution score**: {current_score}%
**Case summary**: {case_summary[:600] or "(not available)"}
**Report-facing summary**: {report_summary[:400] or "(not available)"}
**Strategic objective**: {strategic_objective[:400] or "(not available)"}
**Section from which item was rejected**: {section}
**Rejected item (exact text)**: "{rejected_value[:400]}"
**Remaining counts after this rejection**: missingInfo={remaining_missing}, weaknesses={remaining_weak}, risks={remaining_risks}
{f'**{section_context}**' if section_context else ''}
**Score rule**: When missingInfo, weaknesses, and risks are all 0 (no remaining items), the score can reach 100%. When any remain, score is capped at 95%. {"This rejection leaves no items in any category – score can go to 100%." if can_reach_100 else "Items still remain – score will be capped at 95%."}

VALIDITY GUIDELINES (decide isValidRejection)
- VALID: the rejected item is off-topic, generic boilerplate that doesn't fit this case's facts, addressed by another item, or clearly out-of-scope for the parties/role/jurisdiction.
- INVALID: the rejected item is core to the pleaded facts, supports a key element the case relies on, or directly addresses a real risk this case faces. Generic items that DO apply to this case are still INVALID rejections.

IMPACT RULES (only when isValidRejection=true) – choose scoreIncrease (1-5) based on the SPECIFIC rejected item:
- scoreIncrease MUST be an integer between 1 and 5.
- When this is the last rejection (all counts 0 above), use 4 or 5 so the score can reach 100%.

SECTION GUIDELINES (when isValidRejection=true)

missingInfo (user says this "missing" item is not needed):
- Minor detail (dates, small clarification): scoreIncrease = 1
- Useful but not critical evidence: scoreIncrease = 2–3
- Important missing evidence that would have weakened the case: scoreIncrease = 4–5

riskOrWeakness (user says this risk/weakness does not apply):
- Minor procedural or generic risk: scoreIncrease = 1
- Moderate risk or weakness: scoreIncrease = 2–3
- Major legal risk or strong weakness: scoreIncrease = 4–5

Output ONLY valid JSON, no markdown, no code block:
{{"isValidRejection": <true|false>, "scoreIncrease": <integer 0-5>, "reason": "<one short sentence>"}}
"""
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
            self._workspace_analysis_payload[workspace_id] = report
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": report,
                    },
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

        self._workspace_analysis_payload[workspace_id] = report

        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "analysis_report",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": report,
                },
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

            prompt = f"""You are a legal case analyst. The user manually supplied additional information to address previously-flagged "missing information" items in their analysis report. Two decisions are required:

1) Is the user-supplied text RELATED to this specific case? Use the case summary, missing info, weaknesses, and risks below as ground truth. Generic, off-topic, gibberish, or unrelated text → isRelated=false.
2) If RELATED, choose an integer scoreDecrease between 1 and 5. The system will SUBTRACT this from the current score because the user has just confirmed a real gap existed and the new context may surface additional risks/weaknesses.

**Current case resolution score**: {current_score}%

**Case summary**: {case_summary[:800] or "(not available)"}
**Report-facing summary**: {report_summary[:600] or "(not available)"}
**Strategic objective**: {strategic_objective[:400] or "(not available)"}

**Existing missingInfo items**:
{missing_block}

**Existing weaknesses**:
{weakness_block}

**Existing risks**:
{risk_block}

**User-supplied title**: "{safe_title}"
**User-supplied description**: "{safe_description}"

IMPACT RULES (only when isRelated=true):
- Minor / vague clarification that lightly touches one missing item: scoreDecrease = 1
- Moderate, on-topic information that addresses one missing item but introduces small new uncertainty: scoreDecrease = 2-3
- Highly relevant information that exposes additional gaps, factual weaknesses, or procedural risks: scoreDecrease = 4-5

When isRelated=false set scoreDecrease=0 and reason explains briefly why it is off-topic.

Output ONLY valid JSON, no markdown, no code block:
{{"isRelated": <true|false>, "scoreDecrease": <integer 0-5>, "reason": "<one short sentence>"}}
"""

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
            self._workspace_analysis_payload[workspace_id] = report
            if self._socket_client:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": report,
                    },
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

        self._workspace_analysis_payload[workspace_id] = report

        if self._socket_client:
            await self._socket_client.emit(
                "document_feature_response",
                {
                    "featureType": "analysis_report",
                    "workspaceId": workspace_id,
                    "userId": user_id,
                    "payload": report,
                },
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

    def _normalize_manual_law_relevance(self, raw: Optional[str]) -> str:
        """Map model output to High, Medium, Low, or not relevant."""
        if not raw:
            return "not relevant"
        s = str(raw).strip().lower()
        if s in ("high", "h"):
            return "High"
        if s in ("medium", "med", "m"):
            return "Medium"
        if s in ("low", "l"):
            return "Low"
        if any(
            x in s
            for x in (
                "not relevant",
                "not applicable",
                "irrelevant",
                "none",
                "no relevance",
                "n/a",
            )
        ):
            return "not relevant"
        # Title-case unknown strings for optional freeform per spec
        return str(raw).strip() or "not relevant"

    async def _document_snippet_for_law_relevance(
        self, workspace_id: str, max_chars: int = 12000
    ) -> str:
        """Concatenate recent document text for relevance assessment."""
        try:
            from app.vectorstore.qdrant import vector_store

            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("applicable_law_relevance: vector store read failed: {}", e)
            all_docs = []
        parts: List[str] = []
        for doc in all_docs[:8]:
            content = str(doc.get("content") or "").strip()
            if len(content) < 20:
                continue
            parts.append(content[:2500])
        joined = "\n\n---\n\n".join(parts)
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n[... truncated ...]"
        return joined or "No document text available for this workspace."

    async def _search_kanoon_for_law_relevance(
        self,
        act: str,
        section: str,
        signals: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """
        Same discovery path as applicable_laws: Indian Kanoon first, then Google CSE to site:indiankanoon.org.
        Queries combine the selected act/section with case keywords from the workspace.
        """
        kw = " ".join((signals.get("keywords") or [])[:6])
        title_hint = (signals.get("workspaceTitle") or "")[:80]
        sec_plain = re.sub(r"^section\s*", "", (section or "").strip(), flags=re.I)
        queries = [
            " ".join(p for p in (act, section, kw) if p).strip(),
            " ".join(p for p in (act, f"Section {sec_plain}", kw) if p).strip(),
            " ".join(p for p in (act, section, title_hint) if p).strip(),
        ]
        seen_urls: set = set()
        merged: List[Dict[str, str]] = []
        for q in queries:
            if not q or len(q.strip()) < 5:
                continue
            logger.info("law_relevance: Indian Kanoon query={}", q[:200])
            batch = await self._search_indiankanoon(q)
            for row in batch:
                u = row.get("url") or ""
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    merged.append(row)
                    if len(merged) >= 10:
                        return merged
            if merged:
                return merged
        fallback_q = queries[0] if queries and queries[0] else f"{act} {section}"
        vertex_rows = await vertex_search_indiankanoon(fallback_q)
        for row in vertex_rows:
            u = row.get("url") or ""
            if u and u not in seen_urls:
                seen_urls.add(u)
                merged.append(row)
                if len(merged) >= 10:
                    break
        logger.info(
            "law_relevance: total search rows after Kanoon/Vertex={}",
            len(merged),
        )
        return merged

    def _merge_law_relevance_description(
        self,
        lawyer_desc: Optional[str],
        rationale: str,
        max_len: int = 1600,
    ) -> Optional[str]:
        """Attach evidence-style rationale from documents + Kanoon to optional lawyer description."""
        r = (rationale or "").strip()
        l = (lawyer_desc or "").strip()
        if r and l:
            out = (
                f"{l}\n\nAssessment (case documents and Indian Kanoon search): {r}"
            )
        elif r:
            out = f"Assessment (case documents and Indian Kanoon search): {r}"
        else:
            out = l
        if len(out) > max_len:
            out = out[: max_len - 3] + "..."
        return out or None

    def _apply_law_relevance_consistency_guards(
        self,
        relevance: str,
        rationale: str,
        search_results: List[Dict[str, str]],
        doc_context: str,
    ) -> Tuple[str, str]:
        """
        High/Medium should rest on substance; Low must still reflect something in the case file.
        """
        r = (rationale or "").strip()
        dc = (doc_context or "").strip()
        has_docs = bool(dc) and "No document text available" not in dc
        has_search = len(search_results) > 0
        weak_rationale = len(r) < 55

        if relevance in ("High", "Medium") and weak_rationale:
            relevance = "Low" if has_docs else "not relevant"
            if not r:
                r = (
                    "Insufficient explicit reasoning; downgraded pending clearer case and search tie-in."
                )
        if relevance == "High":
            if not has_search and not (has_docs and len(dc) > 400):
                relevance = "Medium"
                extra = (
                    " Adjusted to Medium: High confidence needs stronger document detail "
                    "and/or Indian Kanoon support."
                )
                r = (r + extra).strip() if r else extra.strip()
        if relevance == "Low":
            if not has_docs:
                relevance = "not relevant"
                r = "No usable case documents to tie this provision to the matter."
            elif len(r) < 35:
                relevance = "not relevant"
                r = (
                    "Low relevance requires at least one concrete fact from the file; "
                    "none was identified."
                )
        return relevance, r.strip()

    async def _assess_law_relevance_with_kanoon_and_llm(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        act: str,
        section: str,
        title: str,
        desc: str,
        user_note: str,
        doc_context: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Run Indian Kanoon/CSE search like applicable_laws, then score relevance with grounded rationale.
        Returns (relevance label, description text including rationale).
        """
        from app.ai.gemini_client import gemini_client

        signals = await self._extract_case_signals_from_documents(
            workspace_id, user_id
        )
        search_results = await self._search_kanoon_for_law_relevance(
            act, section, signals
        )

        sources_text = []
        for idx, row in enumerate(search_results[:8], start=1):
            sources_text.append(
                f"{idx}. title={row.get('title','')} | url={row.get('url','')} | snippet={row.get('snippet','')}"
            )
        source_blob = (
            "\n".join(sources_text)
            if sources_text
            else "No Indian Kanoon / search snippets retrieved (rely on documents only)."
        )

        prompt = f"""You are a senior Indian lawyer. The user selected ONE statute provision; decide if it fits THIS case.

You MUST use BOTH:
(A) the case document excerpt, and
(B) the Indian Kanoon / case-law search lines (titles/snippets/URLs) when present.

Return exactly one JSON object (no markdown), shape:
{{
  "relevance": "High" | "Medium" | "Low" | "not relevant",
  "rationale": "string, 2–5 short sentences"
}}

SCORING (strict):
- **High**: Document facts clearly engage this act+section, AND search results show this provision or very similar charges/relief in comparable fact patterns (name the pattern briefly). Rationale must name at least one **specific document fact** and one **search-line theme** (or state search was empty but document fit is overwhelming—only if documents are very strong).
- **Medium**: Document facts plausibly engage this provision; search supports or is neutral; some uncertainty. Rationale must cite at least one **concrete document fact** and explain the fit.
- **Low**: Only if the **documents** still contain at least one concrete fact that could trigger this provision, but the fit is weak or peripheral. Do **not** pick Low from search alone. Rationale must quote the weak fact you rely on.
- **not relevant**: Provision does not meaningfully apply, or documents lack any hook, or search+documents show mismatch.

Rules:
- Do NOT fabricate facts not present in the document excerpt.
- Do NOT cite IPC unless the selected act is IPC; prefer BNS framing if overlapping.
- If search block is empty, you may still assign Medium or Low only with strong/slight document hooks respectively; **High** requires either search support or unusually clear statutory fit spelled out in the documents.

Selected law:
- Act: {act}
- Section: {section}
{f"- Title: {title}" if title else ""}
{f"- Prior description: {desc}" if desc else ""}
{f"- Lawyer note: {user_note}" if user_note else ""}

Workspace title / keywords (hint): {signals.get("workspaceTitle","")} | {", ".join(signals.get("keywords") or [])}

Case documents (excerpt):
{doc_context}

Indian Kanoon / search results (titles and snippets):
{source_blob}
"""
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.15,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        text = (raw or "").strip()
        if text.startswith("Error generating"):
            return "not relevant", "Assessment failed (model error)."
        if "```" in text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
            if m:
                text = m.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        rel = "not relevant"
        rationale = ""
        if start >= 0 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
                rel = self._normalize_manual_law_relevance(obj.get("relevance"))
                rationale = str(obj.get("rationale") or "").strip()
            except Exception:
                try:
                    open_braces = text.count("{") - text.count("}")
                    open_brackets = text.count("[") - text.count("]")
                    salvaged = text[start : end + 1] + (
                        "]" * max(0, open_brackets)
                    ) + ("}" * max(0, open_braces))
                    obj = json.loads(salvaged)
                    rel = self._normalize_manual_law_relevance(obj.get("relevance"))
                    rationale = str(obj.get("rationale") or "").strip()
                except Exception:
                    m = re.search(r'"relevance"\s*:\s*"([^"]+)"', text)
                    if m:
                        rel = self._normalize_manual_law_relevance(m.group(1))
        rel, rationale = self._apply_law_relevance_consistency_guards(
            rel, rationale, search_results, doc_context
        )
        merged = self._merge_law_relevance_description(desc, rationale)
        return rel, merged

    def _applicable_law_relevance_law_dict(
        self,
        *,
        law_id: Optional[str],
        act: str,
        section: str,
        title: Optional[str],
        description: Optional[str],
        relevance: str,
    ) -> Dict[str, Any]:
        """Build law object for applicable-law relevance responses (omit empty optionals)."""
        # User-facing label: the UI renders relevance as "<value> Relevance", so
        # the internal "not relevant" value reads as "not relevant Relevance".
        # Emit "No" instead so it displays as "No Relevance". Internal logic and
        # comparisons continue to use the "not relevant" canonical value.
        if str(relevance).strip().lower() == "not relevant":
            relevance = "No"
        out: Dict[str, Any] = {
            "act": act,
            "section": section,
            "relevance": relevance,
        }
        if law_id:
            out["id"] = law_id
        if title:
            out["title"] = title
        if description:
            out["description"] = description
        return out

    async def _emit_applicable_law_relevance_to_backend(
        self,
        workspace_id: str,
        user_id: str,
        request_id: Optional[str],
        law_dict: Dict[str, Any],
    ) -> None:
        """
        Backend integration: many gateways only persist AI output on document_feature_response.
        Emit that first (featureType applicable_law_relevance), then the standalone event for spec parity.
        """
        if not self._socket_client:
            logger.warning("applicable_law_relevance: no socket client; cannot emit")
            return

        payload_inner: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "userId": user_id,
            "law": law_dict,
        }
        if request_id:
            payload_inner["requestId"] = request_id

        doc_feature_body: Dict[str, Any] = {
            "featureType": "applicable_law_relevance",
            "workspaceId": workspace_id,
            "userId": user_id,
            "payload": payload_inner,
        }
        try:
            doc_log = json.dumps(doc_feature_body, default=str)
        except Exception:
            doc_log = str(doc_feature_body)
        logger.info(
            "applicable_law_relevance → backend document_feature_response (preview): {}",
            doc_log[:2000] + ("..." if len(doc_log) > 2000 else ""),
        )
        await self._socket_client.emit("document_feature_response", doc_feature_body)
        await asyncio.sleep(0.1)

        standalone: Dict[str, Any] = {
            "workspaceId": workspace_id,
            "userId": user_id,
            "law": law_dict,
        }
        if request_id:
            standalone["requestId"] = request_id
        try:
            solo_log = json.dumps(standalone, default=str)
        except Exception:
            solo_log = str(standalone)
        logger.info(
            "applicable_law_relevance → backend applicable_law_relevance_response (preview): {}",
            solo_log[:2000] + ("..." if len(solo_log) > 2000 else ""),
        )
        await self._socket_client.emit("applicable_law_relevance_response", standalone)
        logger.success(
            "✅ applicable law relevance delivered to backend (document_feature_response + applicable_law_relevance_response) requestId={} relevance={}",
            request_id,
            law_dict.get("relevance"),
        )

    async def on_applicable_law_relevance_request(self, data: Dict[str, Any]) -> None:
        """
        Server asks whether a lawyer-selected act/section is relevant to the case documents.
        Emits document_feature_response (featureType applicable_law_relevance) and
        applicable_law_relevance_response with relevance: High | Medium | Low | not relevant.
        """
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "applicable_law_relevance_request ← backend (received): keys={}",
            list(payload_in.keys()) if isinstance(payload_in, dict) else type(payload_in),
        )
        try:
            req = ApplicableLawRelevanceRequestPayload(**payload_in)
        except Exception as e:
            logger.warning("applicable_law_relevance_request: invalid payload {}: {}", e, payload_in)
            workspace_id = str(payload_in.get("workspaceId") or payload_in.get("workspace_id") or "")
            user_id = str(payload_in.get("userId") or payload_in.get("user_id") or "")
            request_id_raw = payload_in.get("requestId") or payload_in.get("request_id")
            request_id = str(request_id_raw) if request_id_raw else None
            law_raw = payload_in.get("law") if isinstance(payload_in.get("law"), dict) else {}
            if self._socket_client and workspace_id and user_id:
                law_dict = self._applicable_law_relevance_law_dict(
                    law_id=str(law_raw.get("id")) if law_raw.get("id") else None,
                    act=str(law_raw.get("act") or ""),
                    section=str(law_raw.get("section") or ""),
                    title=str(law_raw.get("title") or "").strip() or None,
                    description=str(law_raw.get("description") or "").strip() or None,
                    relevance="not relevant",
                )
                await self._emit_applicable_law_relevance_to_backend(
                    workspace_id, user_id, request_id, law_dict
                )
            else:
                logger.warning(
                    "applicable_law_relevance_request: cannot respond — missing socket or workspaceId/userId"
                )
            return

        workspace_id = req.workspaceId
        user_id = req.userId
        request_id = req.requestId
        law = req.law
        act = (law.act or "").strip()
        section = (law.section or "").strip()

        logger.info(
            "applicable_law_relevance_request ← backend (parsed): workspaceId={} userId={} requestId={} act={} section={}",
            workspace_id[:24] if workspace_id else "",
            user_id[:24] if user_id else "",
            request_id,
            act[:80] if act else "",
            section[:80] if section else "",
        )

        async def _emit_response(
            relevance: str, description: Optional[str] = None
        ) -> None:
            base_law_desc = (law.description or "").strip() or None
            final_desc = description if description else base_law_desc
            law_dict = self._applicable_law_relevance_law_dict(
                law_id=law.id,
                act=act,
                section=section,
                title=(law.title or "").strip() or None,
                description=final_desc,
                relevance=relevance,
            )
            await self._emit_applicable_law_relevance_to_backend(
                workspace_id, user_id, request_id, law_dict
            )

        if not act or not section:
            await _emit_response("not relevant")
            return

        doc_context = await self._document_snippet_for_law_relevance(workspace_id)
        role = "ADJUDICATOR"
        try:
            ctx = workspace_manager.get_workspace(workspace_id)
            if ctx and getattr(ctx, "workspace_role", None):
                role = str(ctx.workspace_role).strip().upper() or role
        except Exception:
            pass

        ud = (law.userDescription or "").strip()
        title = (law.title or "").strip()
        desc = (law.description or "").strip()
        rel, merged_desc = await self._assess_law_relevance_with_kanoon_and_llm(
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_role=role,
            act=act,
            section=section,
            title=title,
            desc=desc,
            user_note=ud,
            doc_context=doc_context,
        )
        await _emit_response(rel, merged_desc)

    async def on_analysis_docx_generate_request(self, data: Dict[str, Any]) -> None:
        """
        Handle analysis_docx_generate_request (Server -> AI_Maverick).
        Performs deterministic text processing and emits analysis_docx_generate_response.
        """
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "analysis_docx_generate_request <- backend (received): keys={}",
            list(payload_in.keys()) if isinstance(payload_in, dict) else type(payload_in),
        )

        if not self._socket_client:
            logger.warning(
                "analysis_docx_generate_request: socket client unavailable, cannot emit response"
            )
            return

        try:
            req = AnalysisDocxGenerateRequestPayload(**payload_in)
            normalized_request = req.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning("analysis_docx_generate_request: invalid payload: {}", e)
            normalized_request = payload_in

        response_body = await asyncio.to_thread(
            process_analysis_docx_request, normalized_request
        )
        try:
            preview = json.dumps(response_body, default=str)
        except Exception:
            preview = str(response_body)
        logger.info(
            "analysis_docx_generate_response -> backend (preview): {}",
            preview[:2000] + ("..." if len(preview) > 2000 else ""),
        )
        await self._socket_client.emit("analysis_docx_generate_response", response_body)
        logger.success(
            "✅ analysis_docx_generate_response emitted for requestId={}",
            response_body.get("requestId"),
        )

    async def on_template_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle template_sync event (Server → AI_Maverick).

        Payload:
        {
            action: "uploaded" | "renamed" | "deleted",
            userId: str,
            templates: [ {id, name, previousName?, category, extension,
                          mimeType?, size?, fileHash?, fileUrl?, path?} ],
            timestamp: str (ISO)
        }

        For "uploaded" the AI downloads each template, extracts its text via
        `file_handlers.process_file_by_extension`, and stores the result in a
        lawyer-scoped Qdrant cache keyed by (userId, template_id). "renamed"
        only patches cached metadata (content unchanged) — keyed on
        template_id, so no stale name-keyed entry is left behind. "deleted"
        removes the cache entry (supports bulk: multiple templates per event).
        """
        payload_in = data if isinstance(data, dict) else {}
        # Log the RAW inbound event exactly as the backend pushed it
        # (top-level keys + action + template count) before validation,
        # so a malformed/unexpected payload is still visible in the logs.
        logger.info(
            "📥 template_sync RECEIVED: action={} userId={} templates={} keys={}",
            (payload_in or {}).get("action"),
            str((payload_in or {}).get("userId") or "")[:12],
            len((payload_in or {}).get("templates") or [])
            if isinstance((payload_in or {}).get("templates"), list)
            else "(not a list)",
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncPayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync: invalid payload (action={}, keys={}): {}",
                (payload_in or {}).get("action"),
                list((payload_in or {}).keys()),
                e,
            )
            return

        action = (payload.action or "").strip().lower()
        user_id = (payload.userId or "").strip()
        if not user_id:
            logger.warning("template_sync: missing userId — dropping event")
            return

        templates = list(payload.templates or [])
        logger.info(
            "📚 template_sync action={} user={} count={}",
            action,
            user_id[:12],
            len(templates),
        )
        # Per-item log so it's clear exactly what the backend pushed.
        # Signed-URL query strings (credentials) are stripped — only the
        # GCS object path is shown so each file is identifiable.
        for idx, tpl in enumerate(templates):
            raw_url = tpl.fileUrl or ""
            url_path = raw_url.split("?", 1)[0] if raw_url else ""
            logger.info(
                "   📄 [{}] id={} name={!r} category={} ext={} "
                "hasFileUrl={} fileHash={} path={}",
                idx,
                tpl.id,
                tpl.name,
                tpl.category,
                tpl.extension,
                bool(raw_url),
                tpl.fileHash,
                tpl.path or url_path or "(none)",
            )

        # Full field-by-field dump of the raw inbound payload for end-to-end
        # verification. Signed-URL credentials are stripped from every
        # template's fileUrl (only the GCS object path is kept) so no
        # credentials ever land in the logs.
        try:
            sanitized = dict(payload_in)
            raw_templates = payload_in.get("templates")
            if isinstance(raw_templates, list):
                redacted = []
                for tpl_raw in raw_templates:
                    if isinstance(tpl_raw, dict):
                        tpl_copy = dict(tpl_raw)
                        fu = tpl_copy.get("fileUrl")
                        if isinstance(fu, str) and fu:
                            tpl_copy["fileUrl"] = fu.split("?", 1)[0]
                        redacted.append(tpl_copy)
                    else:
                        redacted.append(tpl_raw)
                sanitized["templates"] = redacted
            logger.info(
                "📋 template_sync RAW payload (credentials stripped):\n{}",
                json.dumps(sanitized, indent=2, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("template_sync: could not dump raw payload: {}", e)

        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("template_sync: drafting agent unavailable: {}", e)
            return

        if action == "uploaded":
            await self._ingest_template_items(user_id, templates)
            # A newly-ingested template may match suggestions that were built
            # before it was ready — regenerate the panel if the library grew.
            await self._maybe_reemit_drafting_on_templates(user_id)
            # Emit the AI's blueprint proposal for the dashboard approval screen,
            # for each freshly-uploaded file-template that did NOT already carry
            # an approved blueprint (scenario B re-uploads skip this).
            await self._emit_blueprint_proposals(user_id, templates)
        elif action == "formatting_updated":
            # The lawyer approved / edited a template's AI blueprint in the
            # dashboard. File content is unchanged — store the approved
            # blueprint as authoritative (no re-download / re-extract).
            for tpl in templates:
                if not tpl.id:
                    continue
                blueprint = None
                if getattr(tpl, "formatting", None) is not None and tpl.formatting.blueprint is not None:
                    blueprint = tpl.formatting.blueprint.model_dump()
                if blueprint is None:
                    logger.info(
                        "formatting_updated: no blueprint on item {} — skipping",
                        tpl.id,
                    )
                    continue
                await drafting_agent.apply_approved_blueprint(
                    user_id=user_id, template_id=tpl.id, blueprint=blueprint
                )
        elif action == "preset_created":
            # A court / letterhead preset (no file body) — cache its layout for
            # use at DOCX export.
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.store_preset(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Custom",
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
        elif action == "renamed":
            for tpl in templates:
                if not tpl.id or not tpl.name:
                    continue
                await drafting_agent.rename_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    new_name=tpl.name,
                    previous_name=tpl.previousName,
                )
        elif action == "deleted":
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.delete_template(
                    user_id=user_id, template_id=tpl.id
                )
            # Self-heal the drafting panel: demote any suggestion card that was
            # matched to a now-deleted template and re-match it against the
            # current library, so a deleted template can never drive a kickoff.
            await self._maybe_reemit_drafting_on_templates(user_id)
        else:
            logger.warning("template_sync: unknown action {!r}", action)

    async def _emit_blueprint_proposals(
        self, user_id: str, templates: List[Any]
    ) -> None:
        """Emit a ``template_blueprint_proposal`` for each freshly-uploaded
        file-template that did NOT already carry an approved blueprint.

        The AI profiles the template at ingest; here we package that into the
        proposal the dashboard renders on its approval screen. Per-item failures
        are isolated and non-fatal."""
        if not self._socket_client:
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_emit_blueprint_proposals: drafting agent unavailable: {}", e)
            return
        for tpl in templates:
            # Skip presets (no draft body) and re-uploads that already shipped an
            # approved blueprint (those are stored authoritative at ingest).
            if getattr(tpl, "kind", None) == "preset":
                continue
            if (
                getattr(tpl, "formatting", None) is not None
                and tpl.formatting.blueprint is not None
            ):
                continue
            if not tpl.id:
                continue
            try:
                proposal = await drafting_agent.build_blueprint_proposal(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal build failed (template={}): {}", tpl.id, e
                )
                continue
            if not proposal:
                continue
            try:
                await self._socket_client.emit(
                    "template_blueprint_proposal", proposal
                )
                logger.success(
                    "📤 template_blueprint_proposal emitted (template={}, status={}, vars={})",
                    tpl.id,
                    proposal.get("status"),
                    len((proposal.get("blueprint") or {}).get("variables") or []),
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal emit failed (template={}): {}", tpl.id, e
                )

    async def _ingest_template_items(
        self, user_id: str, templates: List[Any]
    ) -> int:
        """Download + extract + cache each template into the in-memory store.

        Shared by the ``template_sync`` ``uploaded`` action and the
        ``template_sync_response`` snapshot. Returns the count ingested.
        Per-item failures are isolated and non-fatal. Re-uses any existing
        cached payload so an unchanged file hash skips re-extraction.
        """
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_ingest_template_items: drafting agent unavailable: {}", e)
            return 0

        ingested = 0
        for tpl in templates:
            if not tpl.id or not tpl.fileUrl:
                logger.warning(
                    "template ingest: skipping item with missing id/fileUrl ({!r})",
                    tpl.id,
                )
                continue
            try:
                previous = await drafting_agent.get_template(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception:
                previous = None
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Document Draft",
                    extension=tpl.extension or "",
                    mime_type=tpl.mimeType,
                    file_url=tpl.fileUrl,
                    path=tpl.path,
                    file_hash=tpl.fileHash,
                    previous_payload=previous if isinstance(previous, dict) else None,
                    kind=getattr(tpl, "kind", None),
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
                ingested += 1
            except Exception as e:
                logger.warning(
                    "template ingest failed (template={}): {}", tpl.id, e
                )
        return ingested

    async def on_template_sync_response(self, data: Dict[str, Any]) -> None:
        """Handle template_sync_response (Server → AI), the reply to our
        ``request_template_sync``. Ingests the full snapshot into the
        in-memory store and resolves the awaiting Future (by requestId)."""
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "📥 template_sync_response RECEIVED: userId={} requestId={} count={} keys={}",
            str((payload_in or {}).get("userId") or "")[:12],
            (payload_in or {}).get("requestId"),
            (payload_in or {}).get("count"),
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncResponsePayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync_response: invalid payload (keys={}): {}",
                list((payload_in or {}).keys()),
                e,
            )
            return

        user_id = (payload.userId or "").strip()
        request_id = payload.requestId
        templates = list(payload.templates or [])

        # Full sanitized dump (signed-URL credentials stripped) for verification.
        try:
            sanitized = dict(payload_in)
            raw_templates = payload_in.get("templates")
            if isinstance(raw_templates, list):
                redacted = []
                for tpl_raw in raw_templates:
                    if isinstance(tpl_raw, dict):
                        tpl_copy = dict(tpl_raw)
                        fu = tpl_copy.get("fileUrl")
                        if isinstance(fu, str) and fu:
                            tpl_copy["fileUrl"] = fu.split("?", 1)[0]
                        redacted.append(tpl_copy)
                    else:
                        redacted.append(tpl_raw)
                sanitized["templates"] = redacted
            logger.info(
                "📋 template_sync_response RAW payload (credentials stripped):\n{}",
                json.dumps(sanitized, indent=2, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("template_sync_response: could not dump raw payload: {}", e)

        try:
            if not user_id:
                logger.warning(
                    "template_sync_response: missing userId — dropping event"
                )
            else:
                ingested = await self._ingest_template_items(user_id, templates)
                logger.success(
                    "📚 template_sync_response ingested {}/{} template(s) for user={}",
                    ingested,
                    len(templates),
                    user_id[:12],
                )
        finally:
            # Always resolve the awaiting future — even if ingestion raised — so
            # ensure_templates_synced (which now waits with no hard time cap)
            # can never hang indefinitely on a failed ingest.
            fut = (
                self._pending_template_sync.get(request_id) if request_id else None
            )
            if fut is not None and not fut.done():
                fut.set_result(True)

        # If this snapshot arrived late (after the ensure_templates_synced
        # timeout) and grew the library, regenerate any drafting panels that
        # were built against the smaller/empty library.
        if user_id:
            await self._maybe_reemit_drafting_on_templates(user_id)

    async def on_request_blueprint_proposal(self, data: Dict[str, Any]) -> None:
        """Handle request_blueprint_proposal (Server → AI), the "Regenerate
        Extra Info" action on the dashboard's blueprint-approval screen.

        Re-profiles the template (forcing a fresh profile even if one exists)
        and emits a fresh ``template_blueprint_proposal`` back. Ingests the
        template from ``fileUrl`` first when the in-memory store is cold."""
        payload_in = data if isinstance(data, dict) else {}
        user_id = str(payload_in.get("userId") or "").strip()
        template_id = str(payload_in.get("templateId") or "").strip()
        file_url = payload_in.get("fileUrl") or ""
        file_hash = payload_in.get("fileHash") or ""
        logger.info(
            "📥 request_blueprint_proposal RECEIVED: userId={} templateId={} hasFileUrl={}",
            user_id[:12],
            template_id,
            bool(file_url),
        )
        if not user_id or not template_id:
            logger.warning(
                "request_blueprint_proposal: missing userId/templateId — dropping event"
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error(
                "request_blueprint_proposal: drafting agent unavailable: {}", e
            )
            return

        # Ensure the template is in the store with extracted text. On a cold
        # cache (AI restart) the template may be absent — ingest it from the
        # supplied fileUrl, deriving name/extension from the object path.
        try:
            tpl = await drafting_agent.get_template(
                user_id=user_id, template_id=template_id
            )
        except Exception:
            tpl = None
        if (not tpl or not (tpl.get("extracted_text") or "").strip()) and file_url:
            url_path = file_url.split("?", 1)[0]
            fname = url_path.rsplit("/", 1)[-1] if url_path else ""
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=template_id,
                    name=fname or template_id,
                    category=(tpl.get("category") if isinstance(tpl, dict) else None)
                    or "Document Draft",
                    extension=ext,
                    mime_type=None,
                    file_url=file_url,
                    path=None,
                    file_hash=file_hash,
                    previous_payload=tpl if isinstance(tpl, dict) else None,
                )
            except Exception as e:
                logger.warning(
                    "request_blueprint_proposal: cold ingest failed (template={}): {}",
                    template_id,
                    e,
                )

        try:
            proposal = await drafting_agent.build_blueprint_proposal(
                user_id=user_id, template_id=template_id, force_reprofile=True
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: proposal build failed (template={}): {}",
                template_id,
                e,
            )
            proposal = None
        if not proposal:
            logger.warning(
                "request_blueprint_proposal: no proposal produced (template={})",
                template_id,
            )
            return
        if not self._socket_client:
            logger.warning(
                "request_blueprint_proposal: no socket client — cannot emit proposal"
            )
            return
        try:
            await self._socket_client.emit(
                "template_blueprint_proposal", proposal
            )
            logger.success(
                "📤 template_blueprint_proposal (regenerated) emitted "
                "(template={}, status={}, vars={})",
                template_id,
                proposal.get("status"),
                len((proposal.get("blueprint") or {}).get("variables") or []),
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: emit failed (template={}): {}",
                template_id,
                e,
            )

    async def ensure_templates_synced(
        self, user_id: str, *, timeout: Optional[float] = None
    ) -> None:
        """Make sure the in-memory template store is warm for ``user_id``.

        If the store already has templates for the user (delta events keep it
        fresh within a session), this is a no-op. Otherwise it emits
        ``request_template_sync`` and awaits the ``template_sync_response``
        snapshot (resolved in :meth:`on_template_sync_response` only AFTER the
        templates finish ingesting), so a cold cache (e.g. right after an AI
        restart) is fully filled before template matching runs.

        ``timeout`` is ``None`` by default: there is NO hard time cap, because a
        template PDF can take longer than any fixed limit to OCR/ingest, and
        cutting off early made matching run against an empty library. The future
        is always resolved by ``on_template_sync_response`` (even if ingestion
        fails), so this await cannot hang indefinitely. Pass an explicit
        ``timeout`` only if a caller deliberately wants to bound the wait.
        """
        if not user_id or not self._socket_client:
            return
        try:
            from app.langgraph.template_store import template_store

            if await template_store.count_for_user(user_id=user_id) > 0:
                return  # already warm this session
        except Exception:
            pass

        request_id = f"tmpl-{(user_id or '')[:8]}-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_event_loop()
        fut: "asyncio.Future" = loop.create_future()
        self._pending_template_sync[request_id] = fut
        try:
            await self._socket_client.emit(
                "request_template_sync",
                {"userId": user_id, "requestId": request_id},
            )
            logger.info(
                "📨 request_template_sync emitted (user={}, requestId={})",
                user_id[:12],
                request_id,
            )
            if timeout is None:
                # No hard time cap — wait until templates are fully ingested.
                await fut
            else:
                await asyncio.wait_for(fut, timeout=timeout)
            logger.info(
                "✅ template snapshot received for user={} (requestId={})",
                user_id[:12],
                request_id,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "request_template_sync timed out after {}s (user={}) — "
                "proceeding with current store",
                timeout,
                user_id[:12],
            )
        except Exception as e:
            logger.warning(
                "request_template_sync failed (user={}): {}", user_id[:12], e
            )
        finally:
            self._pending_template_sync.pop(request_id, None)

    def _normalize_case_title(self, title: str) -> str:
        """
        Normalize a case title to "Petitioner vs Respondent" style with
        Indian-court multi-party shorthand:

        - When a side has 2 parties joined by " & ", reduce to "<first> & Anr".
        - When a side has 3+ parties joined by " & ", reduce to "<first> & Ors".
        - Single-party sides and sides already ending in "& Anr" / "& Ors" are
          left untouched.
        - Also rewrites " v ", " v. ", " versus " (case-insensitive) to " vs ".

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

        normalized = f"{_reduce_side(left)} vs {_reduce_side(right)}"
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
                result["caseTitle"] = self._normalize_case_title(val)
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
                    prompt = f"""You are summarizing a legal case document. The document may be in Hindi, English, or mixed languages.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

TASK: Write a SHORT CASE SUMMARY in English. Requirements:
- Exactly 3 or 4 lines. Each line = one short sentence (max 120 characters).
- Line 1: Name the ACTUAL parties (real names of people/organizations) and the SPECIFIC dispute or issue (e.g. "contract breach", "social media hacking", "defamation", "cricket association dispute").
- Line 2: State ONE or TWO concrete facts or allegations from the document (be specific, not vague).
- Line 3: State the SPECIFIC relief, investigation, or action sought (e.g. "seeks FIR", "requests damages", "demands investigation").
- Line 4 (optional): One more specific detail from the case.

FORBIDDEN PHRASES - DO NOT USE THESE EXACT WORDS:
- "Legal matter based on uploaded documents"
- "The case involves parties and issues requiring legal review"
- "The matter requires appropriate legal action"
- "Supporting documents provide further context"

You MUST extract and name the REAL parties and REAL dispute from the document. If the document mentions names like "Umesh Malik", "BCCI", "Karnataka State Cricket Association", "Shubham Malik", etc., USE THOSE NAMES. If it mentions hacking, contract issues, defamation, etc., STATE THAT SPECIFIC ISSUE.

IMPORTANT: Output exactly 3 or 4 lines. Put each sentence on its own line (use a newline after each sentence). Do not output a single paragraph.

Document content:
{doc_content}

Case summary (3-4 lines, one sentence per line):"""
                else:
                    prompt = f"""CRITICAL: Your previous response was too generic. For the SAME document below, write a SPECIFIC case summary.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

You MUST extract REAL information:
- Line 1: Find and name the ACTUAL complainant/petitioner and respondent/defendant from the document (e.g. if document says "Umesh Malik" or "BCCI" or "Karnataka State Cricket Association", USE THOSE NAMES). Then state the SPECIFIC dispute type (e.g. "social media account hacking", "contract violation", "defamation", "cricket association dispute").
- Line 2: Extract ONE concrete fact from the document (e.g. "hacked social media account", "edited videos made viral", "contract terms violated", "payment not received").
- Line 3: Extract the SPECIFIC relief sought (e.g. "seeks FIR under IT Act", "requests investigation", "demands compensation", "requests contract enforcement").
- Line 4 (optional): One more specific detail.

DO NOT use these phrases: "Legal matter based on uploaded documents", "parties and issues requiring legal review", "appropriate legal action", "Supporting documents provide further context".

Read the document carefully and extract REAL names and REAL facts. Output exactly 3 or 4 lines. Put each sentence on its own line (newline after each sentence).

Document content:
{doc_content}

Specific case summary (3-4 lines, one sentence per line):"""

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
            prompt = f"""Summarize this legal case in exactly 3-4 short sentences in English. Name the actual parties and the actual dispute. Be specific. Put each sentence on its own line (newline after each sentence).

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

{doc_content}

Summary (3-4 lines, one sentence per line):"""
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
        result["caseTitle"] = self._normalize_case_title(result["caseTitle"])
        return result

    async def _generate_fallback_metadata(
        self, combined: str, workspace_id: str
    ) -> Dict[str, Any]:
        """Generate fallback metadata when AI parsing fails - ensures all fields have values."""
        try:
            from app.ai.gemini_client import gemini_client

            gemini_client.initialize()
            doc_preview = combined[:400].replace("\n", " ")
            fallback_prompt = f"""From this document content, extract ACTUAL CASE DETAILS:
1. Title in strict "Petitioner vs Respondent" format. Indian-court multi-party shorthand: 2 parties on a side → "<First> & Anr"; 3+ parties → "<First> & Ors". Never join multiple parties with " & " in the final title. Use exactly " vs " (lowercase, with spaces). If only one party is identifiable, use "<Party> vs <Issue>".
2. One legal category (CRIMINAL LAW, CIVIL LAW, CYBER LAW, etc.)
3. One relevant Indian act
4. A SHORT CASE SUMMARY in English only (3-4 lines). Summarize in your own words; do NOT copy-paste document text. Each line one short sentence.

Document: {doc_preview}

Return JSON: {{"caseTitle": "<First Petitioner> vs <First Respondent> & Anr", "caseCategory": "CIVIL LAW", "subcategories": ["Act Name"], "description": "Line 1: Actual party and issue from documents\\nLine 2: Key facts/allegations from documents\\nLine 3: Relief sought from documents\\nLine 4: Additional relevant details"}}"""
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
                            "caseTitle": self._normalize_case_title(title),
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
            prompt = f"""Based on the document content below, output a JSON object with ALL fields REQUIRED (no empty/null values):

- caseTitle: MUST be in format "Petitioner vs Respondent". Use Indian-court multi-party shorthand: when one side has exactly 2 parties, use "<First Party> & Anr" (e.g. "Pradeep Mehra vs Ethos Limited & Anr"); when 3+ parties, use "<First Party> & Ors" (e.g. "Umesh Malik vs State of Karnataka & Ors"). Never join multiple parties with " & " in the final title — always reduce to first party + "& Anr"/"& Ors". Use exactly " vs " (lowercase, with spaces). If only one party can be identified, use "[Party] vs [Issue/Matter]". MANDATORY field.
- caseCategory: exactly one category in UPPERCASE (e.g. CRIMINAL LAW, CYBER LAW, CIVIL LAW, TRADE MARK LAW). MANDATORY - must be one of: CRIMINAL LAW, CIVIL LAW, CYBER LAW, CONTRACT LAW, PROPERTY LAW, LABOUR LAW, TAX LAW, or similar.
- subcategories: array of 1-3 relevant Indian acts (e.g. ["Bharatiya Nyaya Sanhita", "Information Technology Act, 2000", "The Trade Marks Act, 1999"]). MANDATORY - must have at least 1 act.
- description: a SHORT CASE SUMMARY in English only, exactly 3 to 4 lines (minimum 3, maximum 4). You MUST write a SPECIFIC summary for this case: name the actual parties (people or organizations) and the actual dispute. Do NOT use generic phrases like "Legal matter based on uploaded documents" or "The case involves parties and issues requiring legal review". Write in your own words; do NOT copy-paste the document. Each line one short sentence (under 120 characters). Line 1: actual parties and main issue. Line 2: key facts or allegations. Line 3: relief or action sought. Line 4 (optional): one more relevant detail. Output only the summary text, no prefixes like "Line 1:".

Document content:
{combined}

CRITICAL: All four fields (caseTitle, caseCategory, subcategories, description) are MANDATORY. Do not leave any field empty or null.

Output ONLY this JSON, no markdown: {{"caseTitle": "Pradeep Mehra vs Ethos Limited & Anr", "caseCategory": "CIVIL LAW", "subcategories": ["Consumer Protection Act, 2019"], "description": "MLA Umesh Malik has filed a complaint regarding his son Shubham Malik's hacked social media account.\\nEdited videos and posts were made viral to defame the complainant and damage his political image.\\nThe complainant seeks a high-level investigation and strict action against the perpetrators.\\nThe complaint involves alleged violations under IT Act 2000 and requests preservation of digital evidence."}}"""
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
                (data.get("caseTitle") or "").strip()
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
                title_prompt = f"""From this document content, extract two parties or names and create a case title in format "Party1 vs Party2". If only one party, use "Party vs [Issue]".

Document: {combined[:800]}

Return ONLY the title in format "Name1 vs Name2" or "Name vs Issue", no other text."""
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

    # ========================================================================
    # Connection Events
    # ========================================================================

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

    async def _do_reemit_after_reconnect(self) -> None:
        """Re-emit cached document_feature_response payloads for the pending workspace after reconnect.
        This covers: document_assessments, workspace_metadata, strategy_objective, analysis_report, list_of_dates.
        Clears pending state so it only runs once per disconnect/reconnect cycle."""
        if (
            not self._had_disconnect
            or not self._pending_reemit_workspace_id
            or not self._socket_client
        ):
            return
        workspace_id = self._pending_reemit_workspace_id
        user_id = self._pending_reemit_user_id or ""
        self._had_disconnect = False
        self._pending_reemit_workspace_id = None
        self._pending_reemit_user_id = None
        try:
            # 1) Document assessments (if available). Heal any cached
            # Devanagari transcripts to Hinglish first so workspaces processed
            # before the Hinglish step was added auto-upgrade on the next
            # re-emit. Idempotent — once healed, subsequent calls are no-ops.
            try:
                await self._heal_devanagari_in_document_assessments(workspace_id)
            except Exception as heal_err:
                logger.warning(
                    "Devanagari heal failed for workspace {} (non-fatal): {}",
                    workspace_id[:12] if workspace_id else "",
                    heal_err,
                )
            doc_assess = self._workspace_document_assessments_payload.get(workspace_id)
            if doc_assess:
                await self._socket_client.emit(
                    "document_feature_response",
                    doc_assess,
                )
                logger.success(
                    "✅ Document assessments re-emitted to backend after reconnect"
                )
                await asyncio.sleep(0.1)
            # 2) Workspace metadata (if available)
            metadata = self._workspace_metadata_payload.get(workspace_id)
            if metadata:
                await self._socket_client.emit(
                    "document_feature_response",
                    metadata,
                )
                logger.success(
                    "✅ Workspace metadata re-emitted to backend after reconnect"
                )
                await asyncio.sleep(0.1)
            # 3) Strategy & objectives
            strategies_result = self._workspace_strategy_payload.get(workspace_id)
            if strategies_result:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": strategies_result,
                    },
                )
                logger.success("✅ Strategy re-emitted to backend after reconnect")
                await asyncio.sleep(0.2)
            # 4) Analysis report
            analysis_report = self._workspace_analysis_payload.get(workspace_id)
            if analysis_report:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": analysis_report,
                    },
                )
                logger.success(
                    "✅ Analysis report re-emitted to backend after reconnect"
                )
            # 5) List of dates timeline
            list_of_dates = self._workspace_list_of_dates_payload.get(workspace_id)
            if list_of_dates is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "list_of_dates",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "listOfDates": list_of_dates,
                        },
                    },
                )
                logger.success("✅ List of dates re-emitted to backend after reconnect")
                self._log_list_of_dates_outbound(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    list_of_dates=list_of_dates,
                    context="reconnect-reemit",
                )
            # 6) Applicable laws
            applicable_laws = self._workspace_applicable_laws_payload.get(workspace_id)
            if applicable_laws is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "applicable_laws",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": applicable_laws,
                    },
                )
                logger.success(
                    "✅ Applicable laws re-emitted to backend after reconnect"
                )
            # 7) Procedural timeline
            procedural_timeline = self._workspace_procedural_timeline_payload.get(workspace_id)
            if procedural_timeline is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "procedural_timeline",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {"proceduralTimeline": procedural_timeline},
                    },
                )
                logger.success(
                    "✅ Procedural timeline re-emitted to backend after reconnect"
                )
            # 8) Drafting Assistant — proactive suggestions panel
            drafting_payload = self._workspace_drafting_payload.get(workspace_id)
            if drafting_payload is not None:
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "drafting_assistant",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": drafting_payload,
                    },
                )
                logger.success(
                    "✅ Drafting suggestions re-emitted to backend after reconnect"
                )
        except Exception as e:
            logger.warning("Failed to re-emit after reconnect: {}", e)

    async def on_connect(self, data: Any) -> None:
        """Handle connection event. Re-emit pending document_feature_response if we had disconnected during workspace init."""
        logger.info("Socket connected")
        audit_logger.log_event("socket_connect", {"status": "connected"})
        await self._do_reemit_after_reconnect()

    async def on_connected(self, data: Any) -> None:
        """
        Handle 'connected' event from server.
        Confirms successful connection with userId and clientType.
        If we had a disconnect right after emitting analysis report, re-emit from cache so backend gets the update.
        """
        logger.success("✅ Server confirmed connection as AI_Maverick")
        logger.info(f"Connection details: {data}")
        audit_logger.log_event(
            "socket_connected_confirmed", {"status": "confirmed", "data": data}
        )
        await self._do_reemit_after_reconnect()
        # Re-emit last ai_response if we disconnected right after sending (so backend receives the chat response)
        if self._had_disconnect and self._pending_ai_response and self._socket_client:
            try:
                await self._socket_client.emit_ai_response(self._pending_ai_response)
                logger.success(
                    "✅ AI chat response re-emitted to backend after reconnect (chat: {})",
                    self._pending_ai_response.get("chatId"),
                )
            except Exception as e:
                logger.warning("Failed to re-emit ai_response after reconnect: {}", e)
            finally:
                self._pending_ai_response = None
        if self._had_disconnect:
            self._had_disconnect = False

    async def on_disconnect(self, data: Any) -> None:
        """Handle disconnection event. Mark that we disconnected (so on_connected can re-emit if needed)."""
        self._had_disconnect = True
        logger.info("Socket disconnected")
        audit_logger.log_event("socket_disconnect", {"status": "disconnected"})

    async def on_error(self, data: Any) -> None:
        """Handle error event."""
        logger.error("Socket error received: {}", data)
        audit_logger.log_event("socket_error", {"error": str(data)})

    # ========================================================================
    # Backend-Specified Events (AI_Maverick → Server)
    # ========================================================================

    async def on_workspace_created(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_created event (Server → AI_Maverick).

        Payload:
        {
            workspaceId: str,
            userId: str,
            title: str,
            workspaceRole: str,
            evidenceFiles: [...],
            createdAt: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Initialize role-based agent with workspaceRole
        3. Process evidence files
        4. Store in vector database
        5. Log audit event

        Args:
            data: Workspace creation payload
        """
        try:
            # Validate and parse payload
            payload = WorkspaceCreatedPayload(**data)

            # Store custom instructions if provided (structure, tone, description)
            custom = data.get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get(
                "description"
            )
            if structure or tone or description:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                logger.info(
                    "Custom instructions set: structure={}, tone={}, description={}",
                    structure,
                    tone,
                    (
                        description[:50] + "..."
                        if description and len(description) > 50
                        else description
                    ),
                )

            logger.info(
                "📦 Workspace created: {} - {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.title,
                payload.userId,
                payload.workspaceRole,
            )
            # Set early so re-emit on reconnect works even if socket drops during document_assessments/metadata/strategy/analysis/list_of_dates
            self._pending_reemit_workspace_id = str(payload.workspaceId)
            self._pending_reemit_user_id = str(payload.userId)

            # STEP 1: Validate role (convert to uppercase for validation)
            workspace_role = (
                payload.workspaceRole.upper()
                if payload.workspaceRole
                else "ADJUDICATOR"
            )
            if not RoleEnum.validate(workspace_role):
                logger.warning(
                    "Invalid workspace role '{}', defaulting to ADJUDICATOR",
                    payload.workspaceRole,
                )
                workspace_role = "ADJUDICATOR"
            await self._remember_workspace_role(
                str(payload.workspaceId), str(payload.userId), workspace_role
            )

            logger.info("Initializing workspace with role: {}", workspace_role)

            # Ensure workspace context exists so chat and strategy can resolve role (get_workspace)
            from datetime import datetime as dt

            from app.workspace.context import context_manager as ctx_mgr

            try:
                created_at = (
                    dt.fromisoformat(payload.createdAt.replace("Z", "+00:00"))
                    if getattr(payload, "createdAt", None)
                    else dt.utcnow()
                )
            except Exception:
                created_at = dt.utcnow()
            ctx_mgr.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status="active",
                workspace_role=workspace_role,
                evidence_files=(
                    list(payload.evidenceFiles) if payload.evidenceFiles else []
                ),
                updated_at=created_at,
            )

            # STEP 2: Initialize role-based agent
            try:
                state = await workspace_role_integration.initialize_workspace_with_role(
                    workspace_id=payload.workspaceId,
                    user_id=payload.userId,
                    initial_role=workspace_role,
                )

                logger.success(
                    "✅ Role-based agent initialized for workspace {} with role {}",
                    payload.workspaceId,
                    workspace_role,
                )
            except Exception as role_error:
                logger.error("Failed to initialize role agent: {}", str(role_error))
                # Continue with workspace creation even if role init fails

            # STEP 3: Process evidence files
            if payload.evidenceFiles:
                logger.info(
                    "Processing {} evidence files for workspace {}",
                    len(payload.evidenceFiles),
                    payload.workspaceId,
                )

                # Prepare files for batch processing
                files_to_process = []
                for file in payload.evidenceFiles:
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt,
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file.id:
                        file_dict["_id"] = file.id
                    files_to_process.append(file_dict)

                try:
                    # Process files in batch
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId,
                    )

                    logger.success(
                        "✅ Document processing complete: {}/{} successful, avg readability: {:.2f}%",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0),
                        batch_result.get("average_readability_score", 0),
                    )

                    # Send processing results back to backend (each section isolated so one failure doesn't block the rest — same idea as ex_event)
                    if self._socket_client:
                        # 1) Document assessments — own try/except
                        try:
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get(
                                        "readability_score", 0
                                    ),
                                    "readabilityRating": result.get(
                                        "readability_rating", "Unknown"
                                    ),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get(
                                        "extracted_text_length", 0
                                    ),
                                    "storedInVectorDb": result.get(
                                        "stored_in_vector_db", False
                                    ),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get(
                                        "evidentiary_strength", "N/A"
                                    ),
                                    # Document classification + structured fields.
                                    # documentType: snake_case enum (e.g. aadhaar_card,
                                    # bank_statement, fir, court_order, other).
                                    # extractedFields: masked / safe values for display.
                                    # sensitiveFields: full raw IDs for sealed encrypted
                                    # storage on the backend — must NOT be displayed.
                                    "documentType": result.get(
                                        "document_type", "other"
                                    ),
                                    "extractedFields": result.get(
                                        "extracted_fields", {}
                                    ),
                                    "sensitiveFields": result.get(
                                        "sensitive_fields", {}
                                    ),
                                }
                                # Audio: attach transcript + transcriptStatus.
                                # When transcription has not produced text yet ("pending")
                                # or has errored ("failed"), emit a minimal entry to match
                                # the backend contract (no quality / summary fields).
                                if result.get("is_audio"):
                                    status = result.get("transcript_status", "failed")
                                    transcript_text = result.get("transcript", "") or ""
                                    if status == "ready" and transcript_text:
                                        doc_result["transcript"] = transcript_text
                                        doc_result["transcriptStatus"] = "ready"
                                    else:
                                        # Drop quality/summary fields for not-ready audio
                                        doc_result = {
                                            "documentId": result.get("document_id"),
                                            "fileName": result.get("filename"),
                                            "transcriptStatus": status if status in ("pending", "failed") else "failed",
                                        }
                                if not result.get("success") and "transcriptStatus" not in doc_result:
                                    doc_result["error"] = result.get(
                                        "error", "Unknown error"
                                    )
                                document_results.append(doc_result)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get(
                                            "total_files", 0
                                        ),
                                        "successfulFiles": batch_result.get(
                                            "successful", 0
                                        ),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get(
                                            "average_readability_score", 0
                                        ),
                                    },
                                },
                            }
                            logger.info(
                                f"📤 Emitting document_feature_response (document_assessments) with {len(document_results)} documents"
                            )
                            await self._socket_client.emit(
                                "document_feature_response", document_assessment_payload
                            )
                            # Cache for reliable re-emit after reconnect when socket drops mid-workspace_created
                            self._workspace_document_assessments_payload[
                                payload.workspaceId
                            ] = document_assessment_payload
                            logger.success(
                                "✅ Document assessments sent (or cached for re-emit) to backend"
                            )
                        except Exception as e:
                            logger.error("Failed to emit document_assessments: {}", e)

                        # 2) Workspace metadata — own try/except (same as ex_event)
                        if batch_result.get("successful", 0) > 0:
                            try:
                                meta = (
                                    await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                )
                                workspace_metadata_payload = {
                                    "featureType": "workspace_metadata",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": {
                                        "workspaceTitle": meta.get("caseTitle") or "",
                                        "updateTitle": True,
                                        "caseCategory": meta.get(
                                            "caseCategory", "General"
                                        ),
                                        "subcategories": meta.get("subcategories", []),
                                        "description": meta.get("description", ""),
                                    },
                                }
                                await self._socket_client.emit(
                                    "document_feature_response",
                                    workspace_metadata_payload,
                                )
                                # Cache for reliable re-emit alongside other workspace payloads
                                self._workspace_metadata_payload[
                                    payload.workspaceId
                                ] = workspace_metadata_payload
                                logger.success(
                                    "✅ Workspace metadata sent (or cached for re-emit)"
                                )
                            except Exception as meta_err:
                                logger.warning(
                                    "Failed to emit workspace_metadata: {}",
                                    str(meta_err),
                                )

                        # 3–4) Strategy, analysis, list_of_dates, applicable_laws: background so chat events are not blocked
                        if batch_result.get("successful", 0) > 0:
                            logger.info(
                                "🎯 Scheduling strategy + analysis + list_of_dates + "
                                "applicable_laws + procedural_timeline (background)…"
                            )
                            self._schedule_strategy_analysis_pipeline(
                                workspace_id=str(payload.workspaceId),
                                user_id=str(payload.userId),
                                workspace_role=workspace_role,
                                batch_result=batch_result,
                                is_update=False,
                                analysis_log_context="",
                            )
                        else:
                            # When no files succeeded, still emit analysis report (fallback) so backend always receives it
                            if self._socket_client and batch_result is not None:
                                try:
                                    fallback = self._analysis_report_fallback(
                                        (workspace_role or "ADJUDICATOR")
                                        .strip()
                                        .upper()
                                    )
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        {
                                            "featureType": "analysis_report",
                                            "workspaceId": str(payload.workspaceId),
                                            "userId": str(payload.userId),
                                            "payload": fallback,
                                        },
                                    )
                                    self._workspace_analysis_payload[
                                        payload.workspaceId
                                    ] = fallback
                                    logger.info(
                                        "✅ Analysis report (fallback, 0 successful docs) sent to backend"
                                    )
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        {
                                            "featureType": "list_of_dates",
                                            "workspaceId": str(payload.workspaceId),
                                            "userId": str(payload.userId),
                                            "payload": {
                                                "workspaceId": str(payload.workspaceId),
                                                "userId": str(payload.userId),
                                                "listOfDates": [],
                                            },
                                        },
                                    )
                                    self._workspace_list_of_dates_payload[
                                        payload.workspaceId
                                    ] = []
                                    self._schedule_list_of_dates_and_applicable_laws(
                                        workspace_id=str(payload.workspaceId),
                                        user_id=str(payload.userId),
                                        workspace_role=workspace_role,
                                        batch_result=None,
                                        log_context=" (created, 0 docs success)",
                                    )
                                except Exception as e:
                                    logger.error(
                                        "Failed to emit analysis report fallback (0 success): {}",
                                        e,
                                    )

                except Exception as doc_error:
                    logger.error("Error processing documents: {}", str(doc_error))

                    # Send error notification to backend (per backend spec)
                    if self._socket_client:
                        try:
                            error_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "error": str(doc_error),
                            }
                            await self._socket_client.emit(
                                "document_feature_response", error_payload
                            )
                            logger.error("❌ Document processing error sent to backend")
                        except Exception as emit_error:
                            logger.error(
                                f"Failed to emit error notification: {emit_error}"
                            )

            # When workspace has no evidence files, still emit analysis report (fallback) and empty list of dates
            if not payload.evidenceFiles and self._socket_client:
                try:
                    workspace_role = (
                        (payload.workspaceRole or "ADJUDICATOR").strip().upper()
                    )
                    fallback = self._analysis_report_fallback(workspace_role)
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": payload.workspaceId,
                            "userId": payload.userId,
                            "payload": fallback,
                        },
                    )
                    self._workspace_analysis_payload[payload.workspaceId] = fallback
                    logger.info(
                        "✅ Analysis report (fallback, no docs) sent to backend"
                    )
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": payload.workspaceId,
                            "userId": payload.userId,
                            "payload": {
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "listOfDates": [],
                            },
                        },
                    )
                    self._workspace_list_of_dates_payload[payload.workspaceId] = []
                    logger.info("✅ List of dates (empty, no docs) sent to backend")
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (created, no docs)",
                    )
                except Exception as e:
                    logger.error("Failed to emit analysis report (no docs): {}", e)

            # STEP 4: Log audit event
            audit_logger.log_event(
                "workspace_created_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "title": payload.title,
                    "role": workspace_role,
                    "evidence_files_count": len(payload.evidenceFiles),
                },
            )

            logger.success(
                "🎉 Workspace fully initialized: {} with role {}",
                payload.workspaceId,
                workspace_role,
            )

        except Exception as e:
            logger.error("Error processing workspace_created event: {}", str(e))
            logger.exception(e)
            raise

    async def on_workspace_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle workspace_sync event (Server → AI_Maverick).

        Payload:
        {
            workspaceId: str,
            userId: str,
            title?: str,
            status?: str,
            evidenceFiles?: [...],
            updatedAt: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Update workspace metadata
        3. If deletedDocumentIds: remove from vector store, regenerate strategy from remaining docs
        4. Process new evidence files if any (strategy then based on all docs in workspace)
        5. Log audit event

        Args:
            data: Workspace sync payload
        """
        try:
            # Log raw websocket input for debugging (delete flags, evidenceFiles, etc.)
            try:
                import json

                raw_preview = json.dumps(data, default=str) if data else "null"
                if len(raw_preview) > 1500:
                    raw_preview = raw_preview[:1500] + "..."
                logger.info("📥 workspace_sync RAW input: {}", raw_preview)
            except Exception:
                logger.info("📥 workspace_sync RAW input (repr): {}", repr(data))

            # Validate and parse payload
            payload = WorkspaceSyncPayload(**data)

            # -----------------------------------------------------------------
            # Idempotency guard: skip duplicate/replayed workspace_sync payloads.
            # This prevents duplicate strategy/analysis/list generation when
            # the same sync event is replayed around reconnect.
            # -----------------------------------------------------------------
            try:
                import json

                workspace_id_for_fp = str(payload.workspaceId)
                file_facts = []
                for f in payload.evidenceFiles or []:
                    fid = str(getattr(f, "id", "") or "")
                    file_facts.append(
                        {
                            "id": fid,
                            "isDeleted": bool(
                                getattr(f, "isDeleted", False)
                                or getattr(f, "is_deleted", False)
                            ),
                            "uploadedAt": str(getattr(f, "uploadedAt", "") or ""),
                        }
                    )
                file_facts.sort(
                    key=lambda x: (x.get("id") or "", x.get("uploadedAt") or "")
                )
                fp_obj = {
                    "workspaceId": workspace_id_for_fp,
                    "updatedAt": str(payload.updatedAt or ""),
                    "deletedDocumentIds": sorted(
                        [str(x) for x in (payload.deletedDocumentIds or [])]
                    ),
                    "files": file_facts,
                    "workspaceRole": str(payload.workspaceRole or ""),
                    "title": str(payload.title or ""),
                    "status": str(payload.status or ""),
                }
                fingerprint = json.dumps(fp_obj, sort_keys=True, default=str)
                now_ts = time.time()
                last = self._last_workspace_sync_fingerprint.get(workspace_id_for_fp)
                # 180s window to avoid replay duplicates while still allowing real updates.
                if (
                    last
                    and last.get("fingerprint") == fingerprint
                    and (now_ts - float(last.get("ts") or 0.0)) < 180.0
                ):
                    logger.info(
                        "Skipping duplicate/replayed workspace_sync for workspace {} (fingerprint matched within 180s)",
                        workspace_id_for_fp,
                    )
                    return
                self._last_workspace_sync_fingerprint[workspace_id_for_fp] = {
                    "fingerprint": fingerprint,
                    "ts": now_ts,
                }
            except Exception as fp_err:
                logger.debug(
                    "workspace_sync idempotency guard skipped due to fingerprint error: {}",
                    fp_err,
                )

            # Update custom instructions if provided (structure, tone, description)
            custom = (data or {}).get("customInstructions") or {}
            structure = getattr(payload, "structure", None) or custom.get("structure")
            tone = getattr(payload, "tone", None) or custom.get("tone")
            description = getattr(payload, "description", None) or custom.get(
                "description"
            )
            if structure is not None or tone is not None or description is not None:
                set_custom_instructions(
                    payload.workspaceId,
                    structure=structure,
                    tone=tone,
                    description=description,
                )
                if structure or tone or description:
                    logger.info(
                        "Custom instructions updated: structure={}, tone={}",
                        structure,
                        tone,
                    )

            # Normalize deleted IDs: from deletedDocumentIds and from evidenceFiles with isDeleted: true
            deleted_ids = list(payload.deletedDocumentIds or [])
            if not deleted_ids and isinstance(data, dict):
                deleted_ids = list(
                    data.get("deleted_document_ids")
                    or data.get("deletedDocumentIds")
                    or []
                )
            # Backend may send isDeleted: true on each evidence file instead of/in addition to deletedDocumentIds
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", None) or (
                        getattr(file, "is_deleted", None)
                        if hasattr(file, "is_deleted")
                        else None
                    )
                    if is_deleted:
                        file_id = getattr(file, "id", None) or (
                            getattr(file, "_id", None) if hasattr(file, "_id") else None
                        )
                        if file_id and str(file_id) not in deleted_ids:
                            deleted_ids.append(str(file_id))

            # Backend often sends only the remaining evidenceFiles after a delete (the removed file is omitted)
            # without deletedDocumentIds or isDeleted. Those IDs would stay in the vector store and skip logic below
            # would leave strategy/analysis/list-of-dates/applicable-laws stale (files_to_process becomes empty).
            if payload.evidenceFiles is not None:
                try:
                    non_deleted_evidence = [
                        f
                        for f in payload.evidenceFiles
                        if not (
                            getattr(f, "isDeleted", False)
                            or getattr(f, "is_deleted", False)
                        )
                    ]
                    id_from_file = []
                    for f in non_deleted_evidence:
                        fid = getattr(f, "id", None) or getattr(f, "_id", None)
                        if fid:
                            id_from_file.append(str(fid).strip())
                    # Only infer when every remaining file has an id (avoid false orphan deletion).
                    if non_deleted_evidence and len(id_from_file) == len(
                        non_deleted_evidence
                    ):
                        from app.vectorstore.qdrant import vector_store

                        current_id_set = set(id_from_file)
                        existing_docs = await vector_store.get_all_documents(
                            workspace_id=payload.workspaceId
                        )
                        existing_id_set = {
                            str(d.get("document_id"))
                            for d in (existing_docs or [])
                            if d.get("document_id")
                        }
                        orphan_ids = [
                            i for i in existing_id_set if i not in current_id_set
                        ]
                        for oid in orphan_ids:
                            if oid not in deleted_ids:
                                deleted_ids.append(oid)
                        if orphan_ids:
                            logger.info(
                                "🗑️ Inferred {} removed document(s) (in vector store but not in evidenceFiles): {}",
                                len(orphan_ids),
                                orphan_ids,
                            )
                except Exception as orphan_err:
                    logger.warning(
                        "Vector vs evidenceFiles orphan detection failed: {}",
                        orphan_err,
                    )

            if deleted_ids:
                logger.info(
                    "🗑️ Document IDs to remove from vector store (explicit + inferred): {}",
                    deleted_ids,
                )

            logger.info(
                "🔄 Workspace sync: {} (User: {}, Role: {})",
                payload.workspaceId,
                payload.userId,
                payload.workspaceRole or "unchanged",
            )

            # Create or update workspace context so chat and strategy use correct role (e.g. Mediator from Edit Workspace)
            from datetime import datetime as dt

            from app.workspace.context import context_manager

            updated_at = None
            try:
                if payload.updatedAt:
                    updated_at = dt.fromisoformat(
                        payload.updatedAt.replace("Z", "+00:00")
                    )
            except Exception:
                updated_at = dt.utcnow()
            evidence_for_context = []
            if payload.evidenceFiles:
                for f in payload.evidenceFiles:
                    if getattr(f, "isDeleted", False) or getattr(
                        f, "is_deleted", False
                    ):
                        continue
                    evidence_for_context.append(f)
            context_manager.upsert_workspace(
                payload.workspaceId,
                payload.userId,
                title=payload.title,
                status=payload.status,
                workspace_role=payload.workspaceRole,
                evidence_files=evidence_for_context,
                updated_at=updated_at,
            )
            # Capture the previously-stored role BEFORE _remember_workspace_role
            # overwrites the cache, so we can detect a role-only change below.
            previous_role: Optional[str] = None
            if payload.workspaceRole:
                try:
                    previous_role = await self._restore_workspace_role(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                    )
                except Exception as prev_err:
                    logger.debug(
                        "Could not load previous workspace role: {}", prev_err
                    )
            if payload.workspaceRole:
                await self._remember_workspace_role(
                    str(payload.workspaceId),
                    str(payload.userId),
                    str(payload.workspaceRole),
                )
            resolved_workspace_role = await self._resolve_workspace_role(
                workspace_id=str(payload.workspaceId),
                user_id=str(payload.userId),
                preferred_role=payload.workspaceRole,
            )
            logger.info(
                "Resolved workspace role for sync generation: {}",
                resolved_workspace_role,
            )
            _valid_roles_for_change = {"ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"}
            _prev_upper = (previous_role or "").strip().upper()
            _new_upper = (resolved_workspace_role or "").strip().upper()
            role_changed = (
                _prev_upper in _valid_roles_for_change
                and _new_upper in _valid_roles_for_change
                and _prev_upper != _new_upper
            )
            if role_changed:
                logger.info(
                    "🔁 Workspace role changed: {} → {} (workspace {})",
                    _prev_upper,
                    _new_upper,
                    str(payload.workspaceId)[:12],
                )
                # Early workspace:refresh — mirrors the objective-change pattern
                # (see _on_user_message_request, "objective_change_regeneration_started").
                # Fires BEFORE the deletion / files / skip_reprocess / role-change
                # branches below so the FE shows loading state on role-dependent
                # panels immediately. feature_types matches objective-change exactly.
                if self._socket_client:
                    try:
                        await self._emit_workspace_refresh(
                            workspace_id=str(payload.workspaceId),
                            user_id=str(payload.userId),
                            reason="role_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                            ],
                        )
                    except Exception as refresh_err:
                        logger.warning(
                            "workspace:refresh emit on role-change (early) failed (non-fatal): {}",
                            refresh_err,
                        )

            # Update workspace metadata (for logging)
            updates = {}
            if payload.title:
                updates["title"] = payload.title
            if payload.status:
                updates["status"] = payload.status
            if payload.workspaceRole:
                updates["workspaceRole"] = payload.workspaceRole

            logger.info(f"Workspace updates: {updates}")

            # Handle document deletion: remove from vector store and regenerate strategy from remaining docs
            if deleted_ids:
                from app.vectorstore.qdrant import vector_store

                logger.info(
                    "🗑️ Deleting {} document(s) from vector store: {}",
                    len(deleted_ids),
                    deleted_ids,
                )
                delete_ok = False
                try:
                    await vector_store.delete_documents(
                        workspace_id=payload.workspaceId,
                        document_ids=deleted_ids,
                    )
                    delete_ok = True
                except Exception as del_err:
                    logger.error("Vector store delete failed: {}", str(del_err))
                    logger.exception(del_err)
                if delete_ok:
                    # Resolve role once; list_of_dates + applicable_laws must run after delete even if strategy emit fails.
                    workspace_role = resolved_workspace_role

                    await self._emit_workspace_refresh(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        reason="ai_update_started",
                        feature_types=[
                            "strategy_objective",
                            "analysis_report",
                            "list_of_dates",
                            "applicable_laws",
                            "procedural_timeline",
                        ],
                    )

                    # Strategy: generate or use fallback so backend always receives strategy_objective (e.g. when all docs deleted)
                    strategies_result = None
                    try:
                        strategies_result = (
                            await self._generate_strategies_from_documents(
                                workspace_id=payload.workspaceId,
                                user_id=payload.userId,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as strategy_err:
                        logger.warning(
                            "Strategy generation after delete failed: {}; using fallback",
                            strategy_err,
                        )
                    if not strategies_result:
                        strategies_result = self._strategy_objective_fallback(
                            payload.workspaceId,
                            payload.userId,
                            workspace_role,
                            is_update=True,
                        )
                    try:
                        if self._socket_client:
                            await self._socket_client.emit(
                                "document_feature_response",
                                {
                                    "featureType": "strategy_objective",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": strategies_result,
                                },
                            )
                            self._workspace_strategy_payload[payload.workspaceId] = (
                                strategies_result
                            )
                            logger.success(
                                "🎉 Strategy & Objectives sent after document deletion"
                            )
                    except Exception as strat_emit_err:
                        logger.warning(
                            "Strategy emit after delete failed: {}", strat_emit_err
                        )

                    # Analysis report: always emit valid payload (never null); use fallback when no docs or on error
                    analysis_report = None
                    try:
                        analysis_report = (
                            await self._generate_analysis_report_from_documents(
                                workspace_id=payload.workspaceId,
                                user_id=payload.userId,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as ar_err:
                        logger.warning(
                            "Analysis report after delete failed: {}; using fallback",
                            ar_err,
                        )
                    if not analysis_report or not isinstance(analysis_report, dict):
                        analysis_report = self._analysis_report_fallback(
                            (workspace_role or "ADJUDICATOR").strip().upper()
                        )
                    # Merge with previous so post-deletion regeneration doesn't
                    # re-emit duplicates of items the user already saw.
                    if isinstance(analysis_report, dict):
                        try:
                            analysis_report = await self._merge_analysis_report_with_previous(
                                workspace_id=str(payload.workspaceId),
                                workspace_role=workspace_role,
                                new_analysis=analysis_report,
                                log_context=" (post-delete)",
                            )
                        except Exception as merge_err:
                            logger.warning(
                                "Analysis report merge after delete failed: {}; "
                                "emitting unmerged",
                                merge_err,
                            )
                    try:
                        if self._socket_client:
                            await self._socket_client.emit(
                                "document_feature_response",
                                {
                                    "featureType": "analysis_report",
                                    "workspaceId": payload.workspaceId,
                                    "userId": payload.userId,
                                    "payload": analysis_report,
                                },
                            )
                            self._workspace_analysis_payload[payload.workspaceId] = (
                                analysis_report
                            )
                            logger.success(
                                "✅ Analysis report sent after document deletion"
                            )
                    except Exception as ar_emit_err:
                        logger.warning(
                            "Analysis report emit after delete failed: {}", ar_emit_err
                        )

                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=str(payload.workspaceId),
                        user_id=str(payload.userId),
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (after document deletion)",
                        deleted_doc_ids=list(deleted_ids),
                    )

            # Process new evidence files if any (skip files that are deleted: isDeleted true or in deleted_ids)
            files_to_process = []
            if payload.evidenceFiles:
                for file in payload.evidenceFiles:
                    is_deleted = getattr(file, "isDeleted", False) or getattr(
                        file, "is_deleted", False
                    )
                    if is_deleted:
                        logger.info(
                            "Skipping evidence file (isDeleted=true): {} ({})",
                            getattr(file, "filename", ""),
                            getattr(file, "id", ""),
                        )
                        continue
                    file_id = (
                        getattr(file, "id", None)
                        or (
                            file.__dict__.get("id")
                            if hasattr(file, "__dict__")
                            else None
                        )
                    ) or None
                    if file_id and str(file_id) in deleted_ids:
                        logger.info(
                            "Skipping evidence file (in deleted list): {} ({})",
                            getattr(file, "filename", ""),
                            file_id,
                        )
                        continue
                    file_dict = {
                        "url": file.url,
                        "filename": file.filename,
                        "originalName": file.originalName,
                        "storeFileName": file.storeFileName,
                        "extension": file.extension,
                        "fileHash": file.fileHash,
                        "path": file.path,
                        "uploadedAt": file.uploadedAt,
                    }
                    # Add _id if it exists (Pydantic field is 'id', maps to '_id' in JSON)
                    if file_id:
                        file_dict["_id"] = file_id
                    files_to_process.append(file_dict)

                # Prevent duplicate processing on delete syncs/reconnect:
                # keep only files that are NOT already present in vector store.
                # (If a file already exists and wasn't changed, processing it again causes
                # duplicate strategy/analysis/list-of-dates regeneration.)
                try:
                    from app.vectorstore.qdrant import vector_store

                    existing_docs_after_sync = await vector_store.get_all_documents(
                        workspace_id=payload.workspaceId
                    )
                    existing_ids_after_sync = {
                        str(d.get("document_id"))
                        for d in (existing_docs_after_sync or [])
                        if d.get("document_id")
                    }
                    original_count = len(files_to_process)
                    filtered_files = []
                    for f in files_to_process:
                        fid = str(f.get("_id") or f.get("id") or "").strip()
                        # If file id is known and already in vector store, skip re-processing.
                        if fid and fid in existing_ids_after_sync:
                            continue
                        filtered_files.append(f)
                    files_to_process = filtered_files
                    if original_count != len(files_to_process):
                        logger.info(
                            "Filtered {} already-indexed file(s) from sync batch; remaining to process: {}",
                            original_count - len(files_to_process),
                            len(files_to_process),
                        )
                except Exception as dedupe_err:
                    logger.debug(
                        "Could not de-duplicate sync files against vector store: {}",
                        dedupe_err,
                    )

                if not files_to_process:
                    logger.info(
                        "No new/updated evidence files to process (all skipped or empty list)"
                    )
                else:
                    logger.info(
                        "Processing {} new/updated evidence files",
                        len(files_to_process),
                    )
                    # Fire workspace:refresh IMMEDIATELY — before document
                    # processing, strategy generation, analysis report, etc.
                    # — so the FE shows the loading state on every panel as
                    # soon as the new doc is detected, instead of waiting for
                    # the heavy pipeline to start ~10–20s later. Gated on
                    # has_prior_panels so first-time uploads don't show a
                    # spurious "refreshing" badge for panels that don't exist
                    # yet.
                    has_prior_panels = bool(
                        self._workspace_strategy_payload.get(payload.workspaceId)
                        or self._workspace_analysis_payload.get(payload.workspaceId)
                        or self._workspace_list_of_dates_payload.get(payload.workspaceId)
                        or self._workspace_applicable_laws_payload.get(payload.workspaceId)
                        or self._workspace_procedural_timeline_payload.get(payload.workspaceId)
                    )
                    if has_prior_panels and self._socket_client:
                        try:
                            await self._emit_workspace_refresh(
                                workspace_id=str(payload.workspaceId),
                                user_id=str(payload.userId),
                                reason="ai_update_started",
                                feature_types=[
                                    "strategy_objective",
                                    "analysis_report",
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                ],
                            )
                        except Exception as refresh_err:
                            logger.warning(
                                "Early workspace:refresh emit failed (non-fatal): {}",
                                refresh_err,
                            )

            # Skip re-processing if nothing was deleted and all current files are already in the vector store
            # (e.g. backend sent workspace_sync right after workspace_created with same files)
            skip_reprocess = False
            if not deleted_ids and files_to_process:
                try:
                    from app.vectorstore.qdrant import vector_store

                    existing_docs = await vector_store.get_all_documents(
                        workspace_id=payload.workspaceId
                    )
                    existing_ids = {
                        str(d.get("document_id"))
                        for d in existing_docs
                        if d.get("document_id")
                    }
                    file_ids = [
                        str(f.get("_id") or f.get("id")) or "" for f in files_to_process
                    ]
                    if (
                        len(file_ids) == len(files_to_process)
                        and all(fid.strip() for fid in file_ids)
                        and all(fid in existing_ids for fid in file_ids)
                    ):
                        skip_reprocess = True
                        logger.info(
                            "No new or changed documents (all {} file(s) already in vector store); skipping re-processing and strategy/analysis regeneration",
                            len(files_to_process),
                        )
                except Exception as vec_err:
                    logger.debug(
                        "Could not check existing documents for skip: {}", vec_err
                    )

            if payload.evidenceFiles and files_to_process and not skip_reprocess:
                try:
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=payload.workspaceId,
                        user_id=payload.userId,
                    )

                    logger.success(
                        "✅ Document sync complete: {}/{} successful",
                        batch_result.get("successful", 0),
                        batch_result.get("total_files", 0),
                    )

                    # Send processing results back to backend
                    if self._socket_client:
                        try:
                            # Prepare document results (with summary and evidentiary strength)
                            document_results = []
                            for result in batch_result.get("results", []):
                                doc_result = {
                                    "documentId": result.get("document_id"),
                                    "filename": result.get("filename"),
                                    "success": result.get("success", False),
                                    "readabilityScore": result.get(
                                        "readability_score", 0
                                    ),
                                    "readabilityRating": result.get(
                                        "readability_rating", "Unknown"
                                    ),
                                    "readable": result.get("readable", False),
                                    "extractedTextLength": result.get(
                                        "extracted_text_length", 0
                                    ),
                                    "storedInVectorDb": result.get(
                                        "stored_in_vector_db", False
                                    ),
                                    "summary": result.get("summary", ""),
                                    "evidentiaryStrength": result.get(
                                        "evidentiary_strength", "N/A"
                                    ),
                                    "documentType": result.get(
                                        "document_type", "other"
                                    ),
                                    "extractedFields": result.get(
                                        "extracted_fields", {}
                                    ),
                                    "sensitiveFields": result.get(
                                        "sensitive_fields", {}
                                    ),
                                }
                                # Audio: attach transcript + transcriptStatus.
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
                                            "transcriptStatus": status if status in ("pending", "failed") else "failed",
                                        }
                                if not result.get("success") and "transcriptStatus" not in doc_result:
                                    doc_result["error"] = result.get(
                                        "error", "Unknown error"
                                    )
                                document_results.append(doc_result)

                            # Emit document_feature_response for document assessments (per backend spec)
                            document_assessment_payload = {
                                "featureType": "document_assessments",
                                "workspaceId": payload.workspaceId,
                                "userId": payload.userId,
                                "payload": {
                                    "documentAssessments": document_results,
                                    "summary": {
                                        "totalFiles": batch_result.get(
                                            "total_files", 0
                                        ),
                                        "successfulFiles": batch_result.get(
                                            "successful", 0
                                        ),
                                        "failedFiles": batch_result.get("failed", 0),
                                        "averageReadabilityScore": batch_result.get(
                                            "average_readability_score", 0
                                        ),
                                    },
                                },
                            }

                            await self._socket_client.emit(
                                "document_feature_response", document_assessment_payload
                            )
                            logger.success(
                                "✅ Document assessments (sync) sent to backend"
                            )

                            # Emit workspace_metadata on sync: do NOT send title (send empty) so backend keeps existing title unchanged
                            if batch_result.get("successful", 0) > 0:
                                try:
                                    meta = await self._get_workspace_category_and_subcategory(
                                        workspace_id=payload.workspaceId,
                                        user_id=payload.userId,
                                    )
                                    workspace_metadata_payload = {
                                        "featureType": "workspace_metadata",
                                        "workspaceId": payload.workspaceId,
                                        "userId": payload.userId,
                                        "payload": {
                                            "workspaceTitle": "",
                                            "updateTitle": False,
                                            "caseCategory": meta.get(
                                                "caseCategory", "General"
                                            ),
                                            "subcategories": meta.get(
                                                "subcategories", []
                                            ),
                                            "description": meta.get("description", ""),
                                        },
                                    }
                                    await self._socket_client.emit(
                                        "document_feature_response",
                                        workspace_metadata_payload,
                                    )
                                    logger.success(
                                        "✅ Workspace metadata (sync) sent: title=unchanged, category={}, subcategories={}, description={}",
                                        meta.get("caseCategory"),
                                        meta.get("subcategories"),
                                        (meta.get("description") or "")[:50],
                                    )
                                except Exception as meta_err:
                                    logger.warning(
                                        "Failed to emit workspace_metadata (sync): {}",
                                        str(meta_err),
                                    )

                            if batch_result.get("successful", 0) > 0:
                                workspace_role = resolved_workspace_role
                                logger.info(
                                    "Using workspace role for strategies (sync): {}",
                                    workspace_role,
                                )
                                logger.info(
                                    "🎯 Scheduling strategy + analysis + list_of_dates + "
                                    "applicable_laws + procedural_timeline on sync (background)…"
                                )
                                # NOTE: workspace:refresh is now emitted earlier
                                # in this handler — right after detecting new
                                # files to process — so the FE loader fires
                                # immediately instead of after document
                                # processing completes here.
                                self._schedule_strategy_analysis_pipeline(
                                    workspace_id=str(payload.workspaceId),
                                    user_id=str(payload.userId),
                                    workspace_role=workspace_role,
                                    batch_result=batch_result,
                                    is_update=True,
                                    analysis_log_context=" (sync)",
                                )
                            else:
                                # When no files succeeded on sync, still emit analysis report (fallback)
                                if self._socket_client:
                                    try:
                                        workspace_role = resolved_workspace_role
                                        fallback = self._analysis_report_fallback(
                                            workspace_role
                                        )
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            {
                                                "featureType": "analysis_report",
                                                "workspaceId": payload.workspaceId,
                                                "userId": payload.userId,
                                                "payload": fallback,
                                            },
                                        )
                                        self._workspace_analysis_payload[
                                            payload.workspaceId
                                        ] = fallback
                                        logger.info(
                                            "✅ Analysis report (fallback, 0 successful sync) sent to backend"
                                        )
                                        await self._socket_client.emit(
                                            "document_feature_response",
                                            {
                                                "featureType": "list_of_dates",
                                                "workspaceId": payload.workspaceId,
                                                "userId": payload.userId,
                                                "payload": {
                                                    "workspaceId": payload.workspaceId,
                                                    "userId": payload.userId,
                                                    "listOfDates": [],
                                                },
                                            },
                                        )
                                        self._workspace_list_of_dates_payload[
                                            payload.workspaceId
                                        ] = []
                                        self._schedule_list_of_dates_and_applicable_laws(
                                            workspace_id=str(payload.workspaceId),
                                            user_id=str(payload.userId),
                                            workspace_role=workspace_role,
                                            batch_result=None,
                                            log_context=" (sync, 0 docs success)",
                                        )
                                    except Exception as e:
                                        logger.error(
                                            "Failed to emit analysis report fallback (sync 0 success): {}",
                                            e,
                                        )
                        except Exception as emit_error:
                            logger.error(f"Failed to emit sync results: {emit_error}")
                except Exception as doc_error:
                    logger.error("Error syncing documents: {}", str(doc_error))

            # When we skipped re-processing (no new/changed/deleted docs), re-emit from cache only.
            # Do NOT regenerate — only when user adds/updates/deletes documents do we regenerate (paths above).
            elif skip_reprocess and files_to_process and self._socket_client:
                workspace_id = payload.workspaceId
                user_id = payload.userId
                workspace_role = resolved_workspace_role
                # Re-emit strategy from cache (no regeneration); then analysis_report. Single emit block, no try/except around emits.
                strategies_result = self._workspace_strategy_payload.get(workspace_id)
                if strategies_result:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": strategies_result,
                        },
                    )
                    logger.success(
                        "✅ Strategy & Objectives re-emitted to backend (from cache)"
                    )
                    await asyncio.sleep(
                        0.2
                    )  # Space out emits so backend can process each; reduces disconnect risk
                else:
                    try:
                        strategies_result = (
                            await self._generate_strategies_from_documents(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=workspace_role,
                                batch_result=None,
                                is_update=True,
                            )
                        )
                    except Exception as strategy_err:
                        logger.warning(
                            "Failed to generate strategy on skip_reprocess: {}",
                            strategy_err,
                        )
                        strategies_result = None
                    if strategies_result:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "strategy_objective",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": strategies_result,
                            },
                        )
                        self._workspace_strategy_payload[workspace_id] = (
                            strategies_result
                        )
                        logger.success(
                            "✅ Strategy & Objectives emitted to backend (skip_reprocess, generated once)"
                        )

                analysis_report = self._workspace_analysis_payload.get(workspace_id)
                if analysis_report:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": analysis_report,
                        },
                    )
                    logger.success(
                        "✅ Analysis report re-emitted to backend (from cache)"
                    )
                    await asyncio.sleep(
                        0.15
                    )  # Allow socket to flush before audit/sync complete
                    list_of_dates = self._workspace_list_of_dates_payload.get(
                        workspace_id
                    )
                    if list_of_dates is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "list_of_dates",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": {
                                    "workspaceId": workspace_id,
                                    "userId": user_id,
                                    "listOfDates": list_of_dates,
                                },
                            },
                        )
                        logger.success(
                            "✅ List of dates re-emitted to backend (from cache)"
                        )
                    applicable_laws = self._workspace_applicable_laws_payload.get(
                        workspace_id
                    )
                    if applicable_laws is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "applicable_laws",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": applicable_laws,
                            },
                        )
                        logger.success(
                            "✅ Applicable laws re-emitted to backend (from cache)"
                        )
                    # Procedural timeline ("Upcoming Dates"): re-emit from
                    # cache if present, then schedule a refresh so the FE
                    # reflects any newly-uploaded docs even when the
                    # vector-store said "no new content". Without this the
                    # user saw the four other panels refresh and "Upcoming
                    # Dates" stayed stale or empty.
                    procedural_timeline = (
                        self._workspace_procedural_timeline_payload.get(workspace_id)
                    )
                    if procedural_timeline is not None:
                        await self._socket_client.emit(
                            "document_feature_response",
                            {
                                "featureType": "procedural_timeline",
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "payload": {
                                    "proceduralTimeline": procedural_timeline,
                                },
                            },
                        )
                        logger.success(
                            "✅ Procedural timeline re-emitted to backend (from cache)"
                        )
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (skip_reprocess full-refresh)",
                    )
                else:
                    await self._emit_analysis_report(
                        workspace_id,
                        user_id,
                        workspace_role,
                        batch_result=None,
                        is_update=True,
                        log_context=" (skip_reprocess)",
                    )

            # Role-only change (no new files, no deletions): regenerate role-dependent
            # features (strategy, analysis, applicable_laws) and re-emit role-independent
            # features (list_of_dates, procedural_timeline, document_assessments,
            # workspace_metadata) from cache. CLAUDE.md L39: "role changes invalidate
            # all derived intelligence" — this branch implements that contract for
            # the socket path. Reached only when none of the file/delete/skip_reprocess
            # branches above ran, but the user changed the role on Edit Workspace.
            elif role_changed and self._socket_client:
                workspace_id = payload.workspaceId
                user_id = payload.userId
                workspace_role = resolved_workspace_role
                log_ctx = " (role change)"

                # Hydrate in-memory caches from Qdrant before the re-emit guards
                # below check `.get(workspace_id)`. Without this, a process
                # restart between workspace_created and the role-change sync
                # leaves _workspace_list_of_dates_payload and
                # _workspace_procedural_timeline_payload empty in memory, so the
                # `if cached_X is not None` checks silently skip the re-emits.
                # The helper is idempotent (warm-cache short-circuit).
                try:
                    await self._hydrate_workspace_caches(
                        str(workspace_id), user_id=str(user_id)
                    )
                except Exception as hydrate_err:
                    logger.debug(
                        "Role-change branch: cache hydration failed (non-fatal): {}",
                        hydrate_err,
                    )

                # workspace:refresh was already emitted early (right after
                # role-change detection) to mirror the objective-change pattern;
                # don't re-emit here.

                # Re-emit role-independent panels from cache so the UI stays
                # populated while role-dependent panels regenerate.
                cached_assessments = self._workspace_document_assessments_payload.get(
                    workspace_id
                )
                if cached_assessments:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "document_assessments",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": cached_assessments,
                        },
                    )
                    logger.success(
                        "✅ Document assessments re-emitted{} (from cache)", log_ctx
                    )

                cached_metadata = self._workspace_metadata_payload.get(workspace_id)
                if cached_metadata:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "workspace_metadata",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": cached_metadata,
                        },
                    )
                    logger.success(
                        "✅ Workspace metadata re-emitted{} (from cache)", log_ctx
                    )

                # NOTE: list_of_dates and procedural_timeline are re-emitted
                # from cache AFTER analysis_report (below) to mirror the
                # skip_reprocess branch (L12506+) and the objective-change flow's
                # order: workspace:refresh → strategy → analysis → list_of_dates
                # → procedural_timeline. Emitting them up here (before the
                # strategy/analysis regens) caused the FE to drop the
                # list_of_dates payload, because the early workspace:refresh
                # told the FE to expect a fresh list_of_dates AFTER the
                # role-dependent panels finished regenerating.

                # 3) Regenerate strategy_objective for the new role, emit + cache.
                strategies_result = None
                try:
                    strategies_result = await self._generate_strategies_from_documents(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        is_update=True,
                    )
                except Exception as strategy_err:
                    logger.warning(
                        "Strategy regeneration{} failed: {}", log_ctx, strategy_err
                    )
                if not strategies_result:
                    strategies_result = self._strategy_objective_fallback(
                        workspace_id, user_id, workspace_role, is_update=True
                    )
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "strategy_objective",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": strategies_result,
                    },
                )
                self._workspace_strategy_payload[workspace_id] = strategies_result
                logger.success("🎉 Strategy & Objectives regenerated{}", log_ctx)
                await asyncio.sleep(0.15)

                # 4) Regenerate analysis_report for the new role, emit + cache.
                # Do NOT use self._emit_analysis_report — it auto-schedules
                # list_of_dates/applicable_laws/procedural_timeline regeneration,
                # which would re-run the role-independent features we just
                # re-emitted from cache. Mirror the deletion-path pattern instead.
                analysis_report = None
                try:
                    analysis_report = (
                        await self._generate_analysis_report_from_documents(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=workspace_role,
                            batch_result=None,
                            is_update=True,
                        )
                    )
                except Exception as ar_err:
                    logger.warning(
                        "Analysis report regeneration{} failed: {}",
                        log_ctx,
                        ar_err,
                    )
                if not analysis_report or not isinstance(analysis_report, dict):
                    analysis_report = self._analysis_report_fallback(
                        (workspace_role or "ADJUDICATOR").strip().upper()
                    )
                if isinstance(analysis_report, dict):
                    try:
                        analysis_report = (
                            await self._merge_analysis_report_with_previous(
                                workspace_id=str(workspace_id),
                                workspace_role=workspace_role,
                                new_analysis=analysis_report,
                                log_context=log_ctx,
                            )
                        )
                    except Exception as merge_err:
                        logger.warning(
                            "Analysis report merge{} failed: {}; emitting unmerged",
                            log_ctx,
                            merge_err,
                        )
                await self._socket_client.emit(
                    "document_feature_response",
                    {
                        "featureType": "analysis_report",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": analysis_report,
                    },
                )
                self._workspace_analysis_payload[workspace_id] = analysis_report
                logger.success("✅ Analysis report regenerated{}", log_ctx)
                # Allow socket to flush analysis_report before the next emit
                # (mirrors skip_reprocess branch pattern).
                await asyncio.sleep(0.15)

                # 5) Re-emit list_of_dates from cache (role-independent).
                #    Placed AFTER analysis_report so the FE state machine
                #    accepts it — matches the order used by the skip_reprocess
                #    branch (L12506+) and the objective-change flow.
                cached_list_of_dates = self._workspace_list_of_dates_payload.get(
                    workspace_id
                )
                if cached_list_of_dates is not None:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "listOfDates": cached_list_of_dates,
                            },
                        },
                    )
                    logger.success(
                        "✅ List of dates re-emitted{} (from cache)", log_ctx
                    )

                # 6) Re-emit procedural_timeline from cache (role-independent).
                cached_procedural_timeline = (
                    self._workspace_procedural_timeline_payload.get(workspace_id)
                )
                if cached_procedural_timeline is not None:
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "procedural_timeline",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "proceduralTimeline": cached_procedural_timeline,
                            },
                        },
                    )
                    logger.success(
                        "✅ Procedural timeline re-emitted{} (from cache)", log_ctx
                    )

                # 7) Regenerate applicable_laws only (do not touch list_of_dates
                #    or procedural_timeline — they are role-independent).
                self._schedule_applicable_laws_only(
                    workspace_id=str(workspace_id),
                    user_id=str(user_id),
                    workspace_role=workspace_role,
                    log_context=log_ctx,
                )

            # When sync has no evidence files (e.g. user deleted all docs), emit both strategy and analysis
            # with empty/fallback payload so backend receives both and can show empty state (not null).
            elif not payload.evidenceFiles and self._socket_client:
                try:
                    workspace_id = payload.workspaceId
                    user_id = payload.userId
                    # Explicit [] means "no documents in workspace" — wipe vector DB so re-uploads are clean.
                    # evidenceFiles omitted (None) may be a partial sync; do not delete vectors in that case.
                    if (
                        payload.evidenceFiles is not None
                        and len(payload.evidenceFiles) == 0
                    ):
                        try:
                            from app.vectorstore.qdrant import vector_store

                            await vector_store.delete_workspace(
                                workspace_id=workspace_id
                            )
                            logger.info(
                                "🗑️ Cleared all vectors for workspace {} (evidenceFiles is empty list)",
                                workspace_id,
                            )
                        except Exception as clear_err:
                            logger.warning(
                                "Could not clear vector store for empty evidence list: {}",
                                clear_err,
                            )
                    workspace_role = (
                        resolved_workspace_role
                    )
                    strategy_fallback = self._strategy_objective_fallback(
                        workspace_id, user_id, workspace_role, is_update=True
                    )
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "strategy_objective",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": strategy_fallback,
                        },
                    )
                    self._workspace_strategy_payload[workspace_id] = strategy_fallback
                    analysis_fallback = self._analysis_report_fallback(workspace_role)
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "analysis_report",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": analysis_fallback,
                        },
                    )
                    self._workspace_analysis_payload[workspace_id] = analysis_fallback
                    await self._socket_client.emit(
                        "document_feature_response",
                        {
                            "featureType": "list_of_dates",
                            "workspaceId": workspace_id,
                            "userId": user_id,
                            "payload": {
                                "workspaceId": workspace_id,
                                "userId": user_id,
                                "listOfDates": [],
                            },
                        },
                    )
                    self._workspace_list_of_dates_payload[workspace_id] = []
                    self._schedule_list_of_dates_and_applicable_laws(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=None,
                        log_context=" (sync, no documents)",
                    )
                    logger.success(
                        "✅ Strategy & Analysis report & list of dates (empty) sent to backend (no documents); applicable laws scheduled"
                    )
                except Exception as e:
                    logger.error(
                        "Failed to emit strategy/analysis fallbacks (no docs): {}", e
                    )

            # Log audit event
            audit_logger.log_event(
                "workspace_sync_ai",
                {
                    "workspace_id": payload.workspaceId,
                    "user_id": payload.userId,
                    "updates": updates,
                    "new_files_count": (
                        len(payload.evidenceFiles) if payload.evidenceFiles else 0
                    ),
                    "deleted_document_ids_count": len(deleted_ids),
                    "processed_files_count": len(files_to_process),
                },
            )

            logger.success("✅ Workspace sync completed: {}", payload.workspaceId)

        except Exception as e:
            logger.error("Error processing workspace_sync event: {}", str(e))
            logger.exception(e)
            raise

    async def _handle_drafting_assistant_message(
        self,
        *,
        payload: UserMessageRequestPayload,
        workspace_id: str,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Drive one turn of the Drafting Assistant conversation.

        Builds a lightweight case-context dict from already-cached panels,
        invokes the drafting agent, and emits an augmented `ai_response`
        with `draftStatus`, `openQuestions`, and (when ready) `draftDocument`.
        """
        actionable_id = (payload.actionableId or "").strip()
        if not actionable_id:
            # Without an actionableId we cannot keep session state coherent.
            # Generate a stable fallback so the conversation can still run
            # within the same chat.
            actionable_id = f"user_draft_chat_{chat_id}"

        # Resolve role from workspace context (same logic as the main chat
        # path, but minimised because drafting is role-aware not gated).
        role = "ADJUDICATOR"
        try:
            from app.workspace.manager import workspace_manager

            ctx = workspace_manager.get_workspace(workspace_id)
            if ctx and getattr(ctx, "workspace_role", None):
                role = (ctx.workspace_role or "ADJUDICATOR").upper()
        except Exception:
            pass
        cached_role = await self._restore_workspace_role(
            workspace_id=workspace_id,
            user_id=user_id,
            chat_id=chat_id,
        )
        if cached_role:
            role = cached_role

        # Compose case context for the drafting agent.
        brief = self._build_case_brief_for_drafting(workspace_id)
        case_context = {
            "role": role,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "case_summary": brief.get("case_summary") or "",
            "case_facts_brief": "\n".join(
                line for line in [
                    brief.get("analysis_brief") or "",
                    brief.get("list_of_dates_brief") or "",
                ] if line
            ),
            "applicable_laws_brief": brief.get("applicable_laws_brief") or "",
            "strategy_brief": brief.get("strategy_brief") or "",
        }

        # On kickoff-type turns, enrich the context with the RAW uploaded
        # evidence (not just panel summaries) so the drafting agent can auto-fill
        # variables whose values live only in the documents (invoice numbers,
        # marketing claims, warranty terms, etc.) instead of asking the lawyer
        # for facts the case already contains. Non-fatal — falls back to briefs.
        _kickoffish = (
            (payload.source or "").strip().lower()
            in ("template", "suggesteddraft", "blank")
            or bool(payload.templateId)
            or bool(payload.suggestedDraftId)
        )
        if _kickoffish:
            evidence = await self._evidence_brief_for_drafting(workspace_id)
            if evidence:
                case_context["evidence_brief"] = evidence
                case_context["case_facts_brief"] = "\n\n".join(
                    x for x in [
                        case_context.get("case_facts_brief") or "",
                        "UPLOADED CASE DOCUMENTS (raw evidence — extract any "
                        "case-variable values from here):\n" + evidence,
                    ] if x
                )

        conversation_history: List[Dict[str, Any]] = []
        for item in payload.conversation_history or []:
            role_str = getattr(item, "role", None) or (
                item.get("role") if isinstance(item, dict) else None
            )
            content_str = getattr(item, "content", None) or (
                item.get("content") if isinstance(item, dict) else None
            )
            if role_str and content_str is not None:
                conversation_history.append(
                    {"role": str(role_str).upper(), "content": str(content_str)}
                )

        request_fields = {
            "actionableId": actionable_id,
            "userId": user_id,
            "workspaceId": workspace_id,
            "chatId": chat_id,
            "source": (payload.source or "").strip() or None,
            "suggestedDraftId": payload.suggestedDraftId,
            "suggestionTitle": payload.suggestionTitle,
            "suggestionDescription": payload.suggestionDescription,
            "templateId": payload.templateId,
            "templateUrl": payload.templateUrl,
            "templateOriginalName": payload.templateOriginalName,
            "templateMimeType": payload.templateMimeType,
        }

        # When the turn may need to resolve/match a template (a suggested-draft
        # or explicit template kickoff), make sure the in-memory store is warm
        # first — no-op if already populated this session.
        _src = (payload.source or "").strip().lower()
        if _src in ("suggesteddraft", "template") or payload.templateId:
            await self.ensure_templates_synced(user_id)

        try:
            from app.langgraph.drafting_agent import drafting_agent

            turn = await drafting_agent.handle_turn(
                user_message=payload.content or "",
                case_context=case_context,
                request_fields=request_fields,
                conversation_history=conversation_history,
            )
        except Exception as e:
            logger.error("Drafting agent failed: {}", e)
            logger.exception(e)
            turn = {
                "content": (
                    "I ran into an issue while preparing the draft. "
                    "Please retry, or share any additional details that may help."
                ),
                "draftStatus": "gathering_info",
                "openQuestions": [],
                "draftDocument": None,
            }

        # Emit the augmented ai_response.
        if not self._socket_client:
            logger.error("Socket client unavailable; cannot emit drafting ai_response")
            return

        ai_response_payload: Dict[str, Any] = {
            "chatId": chat_id,
            "workspaceId": workspace_id,
            "userId": user_id,
            "chat_type": "DRAFTING_ASSISTANT",
            "actionableId": actionable_id,
            "content": (turn.get("content") or "").strip(),
            "isDraft": bool(turn.get("isDraft")),
            "draftStatus": turn.get("draftStatus"),
            "openQuestions": turn.get("openQuestions") or [],
            "draftDocument": turn.get("draftDocument"),
            "metadata": {
                "messageId": chat_id,
                "aiModel": settings.gemini_model,
            },
        }
        if ai_response_payload["draftDocument"] is None:
            ai_response_payload.pop("draftDocument", None)

        # Generate drafting-specific chips so the bottom strip in the
        # Drafting Assistant panel shows draft-actionable prompts ("Adjust
        # the Prayer", "Strengthen Grounds", …) instead of the stale
        # intelligence-chat chips that were leaking through before this fix.
        try:
            import random as _random

            chips = await self._generate_drafting_chips(
                workspace_id=workspace_id,
                actionable_id=actionable_id,
                draft_status=turn.get("draftStatus") or "",
                draft_title=(turn.get("draftDocument") or {}).get("title")
                or (turn.get("draftDocument") or {}).get("draftTitle")
                or "",
                open_questions=turn.get("openQuestions") or [],
                last_assistant_message=ai_response_payload["content"],
                last_draft_markdown=(turn.get("draftDocument") or {}).get("content")
                or "",
            )
            if chips:
                # Cache under both keys so on_probing_prompts_request can find
                # them whether BE polls with actionableId or not.
                if actionable_id:
                    self._workspace_drafting_suggestion_chips[actionable_id] = list(chips)
                if workspace_id:
                    self._workspace_drafting_suggestion_chips[workspace_id] = list(chips)
                ai_response_payload["suggestionChips"] = _random.sample(
                    chips, min(4, len(chips))
                )
        except Exception as chip_err:
            logger.warning("Drafting chips emit failed (non-fatal): {}", chip_err)
        # Cache the assistant text so a short "yes" follow-up resolves correctly.
        if ai_response_payload["content"]:
            self._intelligence_last_ai_by_chat[chat_id] = ai_response_payload[
                "content"
            ]
        # _pending_ai_response represents an emit that did NOT make it to BE
        # and must be retried on the next reconnect. If we buffer the payload
        # unconditionally (the old behaviour) every disconnect/reconnect cycle
        # re-emits the last delivered message, producing duplicate drafts in
        # the chat. Detect a confirmed delivery via (no exception thrown) +
        # (socket still connected after the await) — the second guard catches
        # the silent-skip path where emit_ai_response waited for reconnect and
        # then returned without raising.
        emit_succeeded = False
        try:
            await self._socket_client.emit_ai_response(ai_response_payload)
            emit_succeeded = bool(
                getattr(self._socket_client, "is_connected", True)
            )
        except Exception as e:
            logger.error("Failed to emit drafting ai_response: {}", e)

        if emit_succeeded:
            # Drop any stale pending payload from a previous failed emit so
            # reconnect doesn't re-emit it after we just delivered fresh content.
            self._pending_ai_response = None
            logger.success(
                "✅ Drafting ai_response emitted (status={}, version={})",
                ai_response_payload.get("draftStatus"),
                (turn.get("draftDocument") or {}).get("version"),
            )
        else:
            # Emit raised or socket was silently disconnected — keep the
            # payload buffered so on_connected can retry once we reconnect.
            self._pending_ai_response = ai_response_payload
            logger.warning(
                "Drafting ai_response not confirmed delivered — will retry on reconnect"
            )

        audit_logger.log_event(
            "user_drafting_message_processed",
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "actionable_id": actionable_id,
                "draft_status": turn.get("draftStatus"),
                "open_questions": len(turn.get("openQuestions") or []),
                "draft_ready": bool(turn.get("draftDocument")),
            },
        )

    async def on_user_message_request(self, data: Dict[str, Any]) -> None:
        """
        Handle user_message_request event (Server → AI_Maverick).

        Payload:
        {
            message: str,
            chat_type: ChatType,
            conversation_history: [{role: 'USER'|'AI', content: str}],
            workspaceId?: str,
            userId?: str,
            chatId?: str,
            timestamp: str (ISO timestamp)
        }

        Process flow:
        1. Validate payload
        2. Build chat context
        3. Process with Intelligence Chat Agent
        4. Emit ai_response back to server

        Args:
            data: User message request payload
        """
        try:
            # Validate and parse payload
            payload = UserMessageRequestPayload(**data)

            logger.info(
                "💬 User message request: Chat {} (Type: {}, Workspace: {})",
                payload.chatId,
                payload.chat_type,
                payload.workspaceId,
            )
            logger.debug(f"Message: {payload.content[:100]}...")

            # Import here to avoid circular dependencies
            from app.langgraph.chat_models import ChatContext
            from app.langgraph.intelligence_chat_agent import \
                intelligence_chat_agent
            from app.langgraph.role_agent import role_agent

            # STEP 1: Get workspace context (role, documents count) from workspace manager
            workspace_id = payload.workspaceId or "unknown"
            user_id = payload.userId or "unknown"
            chat_id = payload.chatId or f"chat_{datetime.utcnow().timestamp()}"

            # Repopulate in-memory caches from Qdrant when the AI service was
            # restarted between the original workspace_sync and this message.
            # No-op if the cache is already warm or workspace_id is missing.
            if workspace_id and workspace_id != "unknown":
                await self._hydrate_workspace_caches(workspace_id, user_id=user_id)

            # Drafting Assistant short-circuit: when chat_type is
            # DRAFTING_ASSISTANT we hand the turn to the drafting agent
            # instead of running it through the general intelligence chat.
            # The drafting agent owns its own session state keyed by
            # `actionableId` and emits the augmented `ai_response` payload
            # (draftStatus / openQuestions / draftDocument).
            if (payload.chat_type or "").strip().upper() == "DRAFTING_ASSISTANT":
                await self._handle_drafting_assistant_message(
                    payload=payload,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                return

            role_context = {
                "role": "ADJUDICATOR",
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
            role_aliases = {
                "ADJUDICATOR": "ADJUDICATOR",
                "PETITIONER": "PETITIONER",
                "RESPONDENT": "RESPONDENT",
                "MEDIATOR": "MEDIATOR",
            }
            raw_user_text = (payload.content or "").strip()
            cleaned_for_role = re.sub(r"[^A-Za-z]", "", raw_user_text).upper()
            explicit_role_selection = role_aliases.get(cleaned_for_role)
            workspace_state = {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "documents_count": 0,
                "role_selected": False,
            }
            ctx = None
            try:
                from app.workspace.manager import workspace_manager

                ctx = workspace_manager.get_workspace(workspace_id)
                if ctx:
                    workspace_role = (ctx.workspace_role or "ADJUDICATOR").upper()
                    if workspace_role not in (
                        "ADJUDICATOR",
                        "PETITIONER",
                        "RESPONDENT",
                        "MEDIATOR",
                    ):
                        workspace_role = "ADJUDICATOR"
                    role_context["role"] = workspace_role
                    await self._remember_workspace_role(
                        workspace_id, user_id, workspace_role, chat_id=chat_id
                    )
                    role_context_from_meta = ctx.get_metadata("role_context")
                    role_from_meta = ""
                    if isinstance(role_context_from_meta, dict):
                        role_from_meta = str(
                            role_context_from_meta.get("role") or ""
                        ).strip()
                    # Treat role as selected if either workspace_role is set or role metadata exists.
                    workspace_state["role_selected"] = bool(
                        (ctx.workspace_role or "").strip() or role_from_meta
                    )
                    if isinstance(
                        role_context_from_meta, dict
                    ) and role_context_from_meta.get("role"):
                        role_context.update(role_context_from_meta)
                    docs_count = (
                        ctx.get_evidence_file_count()
                        if hasattr(ctx, "get_evidence_file_count")
                        else len(getattr(ctx, "evidence_files", []))
                    )
                    workspace_state["documents_count"] = docs_count
            except Exception as ws_err:
                logger.warning(
                    "Could not load workspace for chat (using defaults): {}",
                    str(ws_err),
                )

            # Restore role from persistent cache (workspace+user and optional chat) when context was cold/reloaded.
            cached_role = await self._restore_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                chat_id=chat_id,
            )
            if cached_role:
                role_context["role"] = cached_role
                workspace_state["role_selected"] = True
                if not ctx:
                    try:
                        from app.workspace.context import context_manager

                        ctx = context_manager.upsert_workspace(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=cached_role,
                        )
                    except Exception:
                        ctx = None
                if ctx:
                    ctx.workspace_role = cached_role
                    role_ctx_meta = ctx.get_metadata("role_context", {})
                    if not isinstance(role_ctx_meta, dict):
                        role_ctx_meta = {}
                    role_ctx_meta = {**role_ctx_meta, "role": cached_role}
                    ctx.set_metadata("role", cached_role)
                    ctx.set_metadata("role_context", role_ctx_meta)
                    role_context.update(role_ctx_meta)

            # Safety: if documents exist but role flag is still false (cache miss/race),
            # don't block the user with role picker again; continue in neutral mode.
            if workspace_state.get("documents_count", 0) > 0 and not workspace_state.get(
                "role_selected"
            ):
                workspace_state["role_selected"] = True
                role_context["role"] = (
                    (role_context.get("role") or "ADJUDICATOR").strip().upper()
                )
                logger.warning(
                    "Role not found for workspace {} with existing documents; bypassing gate with role={}",
                    workspace_id[:12] if workspace_id else "",
                    role_context["role"],
                )

            # If the user explicitly sends a role name in chat, persist it on workspace context.
            if explicit_role_selection:
                try:
                    if not ctx:
                        from app.workspace.context import context_manager

                        ctx = context_manager.upsert_workspace(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=explicit_role_selection,
                        )
                    ctx.workspace_role = explicit_role_selection
                    role_ctx_meta = ctx.get_metadata("role_context", {})
                    if not isinstance(role_ctx_meta, dict):
                        role_ctx_meta = {}
                    role_ctx_meta = {**role_ctx_meta, "role": explicit_role_selection}
                    ctx.set_metadata("role", explicit_role_selection)
                    ctx.set_metadata("role_context", role_ctx_meta)
                    ctx.set_metadata("role_selected_at", datetime.utcnow().isoformat())
                    role_context.update(role_ctx_meta)
                    role_context["role"] = explicit_role_selection
                    workspace_state["role_selected"] = True
                    await self._remember_workspace_role(
                        workspace_id,
                        user_id,
                        explicit_role_selection,
                        chat_id=chat_id,
                    )
                    logger.info(
                        "Role selected from chat and stored on workspace context: {}",
                        explicit_role_selection,
                    )

                    # If user message is only the role selection word, acknowledge and return immediately.
                    if cleaned_for_role in role_aliases:
                        if self._socket_client:
                            ai_response = AIResponsePayload(
                                chatId=chat_id,
                                workspaceId=workspace_id,
                                userId=user_id,
                                content=(
                                    f"Role confirmed as {explicit_role_selection.title()}. "
                                    "I will keep this role for this workspace. "
                                    "You can now ask your legal question."
                                ),
                                metadata=AIResponseMetadata(
                                    messageId=chat_id,
                                    aiModel=settings.gemini_model,
                                ),
                            )
                            payload_dict = ai_response.dict(by_alias=True)
                            await self._socket_client.emit_ai_response(payload_dict)
                            # Do NOT populate _pending_ai_response here — the
                            # role-confirmation ack does not need reconnect
                            # retry, and leaving a stale buffer here caused
                            # this acknowledgement to be re-emitted on the
                            # next disconnect cycle (duplicate-message bug).
                            self._intelligence_last_ai_by_chat[chat_id] = (
                                ai_response.content
                            )
                        audit_logger.log_event(
                            "workspace_role_selected_from_chat",
                            {
                                "workspace_id": workspace_id,
                                "user_id": user_id,
                                "chat_id": chat_id,
                                "role": explicit_role_selection,
                            },
                        )
                        return
                except Exception as role_sel_err:
                    logger.warning(
                        "Failed to persist role selection from chat: {}",
                        str(role_sel_err),
                    )

            # Fetch annexure text from vector store for chat (drafts, answers, etc.)
            try:
                from app.vectorstore.qdrant import vector_store

                all_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
                id_to_annexure_name: Dict[str, str] = {}
                if ctx and getattr(ctx, "evidence_files", None):
                    for ef in ctx.evidence_files:
                        fid = getattr(ef, "id", None)
                        if fid is None:
                            continue
                        disp = (getattr(ef, "filename", None) or "").strip()
                        if disp:
                            id_to_annexure_name[str(fid)] = disp
                # Build a stable annexure-number index from the FULL upload
                # order (oldest first). Annexure N always means the Nth uploaded
                # document, regardless of which subset we end up including.
                doc_by_id: Dict[str, Dict[str, Any]] = {}
                for n, doc in enumerate(all_docs or [], 1):
                    did = doc.get("document_id")
                    if did is None:
                        continue
                    doc_by_id[str(did)] = {
                        "content": (doc.get("content") or "").strip(),
                        "annexure_number": n,
                    }
                total_docs = len(all_docs or [])

                # Retrieval query: the user's message, widened with the previous
                # user turn for short/vague follow-ups ("what about the second one?").
                retrieval_query = (payload.content or "").strip()
                try:
                    prior_user = [
                        (it.content or "")
                        for it in (payload.conversation_history or [])
                        if str(getattr(it, "role", "") or "").upper() == "USER"
                        and getattr(it, "content", None)
                    ]
                    if len(retrieval_query) < 25 and prior_user:
                        retrieval_query = (prior_user[-1] + " " + retrieval_query).strip()
                except Exception:
                    pass

                # Force-include any annexure the user references explicitly, by
                # number/roman numeral or by filename — even if it scores low.
                forced_ids: Set[str] = set()
                um_lower = (payload.content or "").lower()
                for m in _ANNEXURE_REF_RE.finditer(payload.content or ""):
                    num = _roman_or_int(m.group(1))
                    if num is None:
                        continue
                    for did, info in doc_by_id.items():
                        if info["annexure_number"] == num:
                            forced_ids.add(did)
                for did, name in id_to_annexure_name.items():
                    if name and name.lower() in um_lower and did in doc_by_id:
                        forced_ids.add(did)

                # Select which documents to include.
                selected_ids: List[str] = []
                if total_docs <= CHAT_SMALL_WORKSPACE_DOC_THRESHOLD:
                    # Small workspace: include everything (no retrieval-miss risk).
                    selected_ids = [
                        str(d.get("document_id"))
                        for d in (all_docs or [])
                        if d.get("document_id") is not None
                    ]
                else:
                    # Large workspace: semantic search as a document selector.
                    results: List[Dict[str, Any]] = []
                    try:
                        results = await vector_store.search(
                            workspace_id=workspace_id,
                            query=retrieval_query or (payload.content or ""),
                            limit=CHAT_SEARCH_CHUNK_LIMIT,
                        )
                    except Exception as se:
                        logger.warning(
                            "Chat retrieval search failed for workspace {}, "
                            "falling back to upload order: {}",
                            workspace_id,
                            str(se),
                        )
                        results = []

                    seen: Set[str] = set()
                    for r in results:
                        orig = str(r.get("original_document_id") or "")
                        if not orig or orig not in doc_by_id or orig in seen:
                            continue
                        seen.add(orig)
                        selected_ids.append(orig)
                        if len(selected_ids) >= CHAT_MAX_DOCS_INCLUDED:
                            break

                # Merge forced docs (explicit references) into the selection.
                for did in forced_ids:
                    if did not in selected_ids:
                        selected_ids.append(did)

                # Fallback: never send empty annexure context. Degrade to the
                # first N docs by upload order (old behaviour) rather than nothing.
                if not selected_ids:
                    logger.warning(
                        "Chat retrieval produced no documents for workspace {} "
                        "(docs={}); falling back to first {} by upload order",
                        workspace_id,
                        total_docs,
                        CHAT_MAX_DOCS_INCLUDED,
                    )
                    selected_ids = [
                        str(d.get("document_id"))
                        for d in (all_docs or [])[:CHAT_MAX_DOCS_INCLUDED]
                        if d.get("document_id") is not None
                    ]

                # Assemble: ascending annexure order, full combined content per
                # doc (chunks have no overlap, so a figure can straddle a boundary),
                # truncated to the per-doc and total budgets.
                selected_ids = sorted(
                    dict.fromkeys(selected_ids),
                    key=lambda d: doc_by_id.get(d, {}).get("annexure_number", 1_000_000),
                )
                total = 0
                doc_snippets = []
                for did in selected_ids:
                    info = doc_by_id.get(did)
                    if not info or not info["content"]:
                        continue
                    content = info["content"]
                    snippet = content[:CHAT_MAX_CHARS_PER_DOC] + (
                        "..." if len(content) > CHAT_MAX_CHARS_PER_DOC else ""
                    )
                    if total + len(snippet) > CHAT_MAX_TOTAL_CHARS:
                        snippet = snippet[: max(0, CHAT_MAX_TOTAL_CHARS - total)]
                        if not snippet:
                            break
                    entry = {
                        "document_id": did,
                        "content": snippet,
                        "annexure_number": info["annexure_number"],
                    }
                    ann_name = id_to_annexure_name.get(str(did))
                    if ann_name:
                        entry["fileName"] = ann_name
                    doc_snippets.append(entry)
                    total += len(snippet)
                    if total >= CHAT_MAX_TOTAL_CHARS:
                        break
                if doc_snippets:
                    workspace_state["uploaded_documents"] = doc_snippets
                logger.info(
                    "Chat doc context: workspace={} total_docs={} mode={} "
                    "included={} chars={}",
                    workspace_id,
                    total_docs,
                    "small" if total_docs <= CHAT_SMALL_WORKSPACE_DOC_THRESHOLD else "retrieval",
                    len(doc_snippets),
                    total,
                )
            except Exception as doc_err:
                logger.warning(
                    "Could not load documents for chat context: {}", str(doc_err)
                )

            # If user attached a document to this message, download and extract text; cache by chat_id so follow-ups see it
            attachment = getattr(payload, "attachment", None)
            if attachment and getattr(attachment, "url", None):
                try:
                    from app.processing.document_processor import \
                        document_processor

                    filename = (
                        getattr(attachment, "fileName", None) or "attachment"
                    ).strip() or "attachment"
                    extracted = await document_processor.download_and_extract_text(
                        url=attachment.url,
                        filename=filename,
                        max_text_chars=12000,
                    )
                    if extracted:
                        workspace_state["chat_attachment"] = [
                            {"fileName": filename, "content": extracted}
                        ]
                        self._chat_attachment_by_chat[chat_id] = workspace_state[
                            "chat_attachment"
                        ]
                        logger.info(
                            "Chat attachment loaded and cached for chat {}: {} ({} chars)",
                            chat_id,
                            filename,
                            len(extracted),
                        )
                except Exception as att_err:
                    logger.warning("Could not process chat attachment: {}", att_err)
            else:
                # No attachment this message: use cached attachment for this chat so follow-up questions still have the document
                cached = self._chat_attachment_by_chat.get(chat_id)
                if cached:
                    workspace_state["chat_attachment"] = cached
                    logger.debug(
                        "Using cached chat attachment for follow-up (chat_id={}, {} doc(s))",
                        chat_id,
                        len(cached),
                    )

            # -------------------------------------------------------------
            # INTELLIGENCE CHAT PANEL MUTATIONS (USER-DRIVEN)
            # If the lawyer sends update markers, apply them immediately
            # to the cached Strategy/Objectives + Analysis Report and re-emit
            # updated panels to the backend.
            # STRATEGY_OBJECTIVE: new objective text → regenerate objective + next steps + chips;
            # case summary unchanged; emit strategy_objective (skip long chat memo).
            # Informal discussion → full answer may append an offer; user "yes" → regen + analysis refresh.
            # -------------------------------------------------------------
            chat_type_upper = (payload.chat_type or "").strip().upper()
            chat_history_resolved = merge_intelligence_chat_history(
                payload.conversation_history,
                current_user_content=payload.content or "",
                cached_last_ai=self._intelligence_last_ai_by_chat.get(chat_id),
            )
            update_result = {"applied": False}
            objective_regen_ok = False
            objective_ack_kind: Optional[str] = None
            implicit_objective_shift_text: Optional[str] = None
            # Defined here so the ack-builder below can read it even if the
            # try-block raises before the pre-dispatcher initialises it.
            panel_edit_applied: List[str] = []
            try:
                combined_text_for_updates = payload.content or ""
                # Include attachment text if present (so updates can be pasted into a file)
                chat_attachment = workspace_state.get("chat_attachment") or []
                if isinstance(chat_attachment, list) and chat_attachment:
                    for att in chat_attachment:
                        if isinstance(att, dict):
                            combined_text_for_updates += "\n\n" + (
                                att.get("content") or ""
                            )

                um_early = (payload.content or "").strip()

                # -------------------------------------------------------------
                # Pre-dispatcher: free-form panel-edit detector for ANY chat_type.
                # Fires for messages like "remove the 3rd line from case summary",
                # "add a step about WhatsApp chats to possible next steps",
                # "change the date in strategic objective", etc.
                # Cheap keyword precheck avoids the LLM classifier call on every
                # message; only when the message contains at least one edit verb
                # AND one panel-name token do we ask the classifier.
                # -------------------------------------------------------------
                panel_edit_applied: List[str] = []
                _edit_verbs = (
                    "remove", "delete", "drop", "change", "replace", "edit",
                    "modify", "rewrite", "rephrase", "add", "append", "insert",
                    "expand", "shorten", "fix", "correct", "update",
                )
                _panel_words = (
                    "case summary", "report summary", "objective", "strategy",
                    "next step", "action item", " step", " steps", "summary",
                    "wording", "wordings", "phrasing", "language",
                    "analysis report", "analysis", "report", "strength",
                    "strengths", "weakness", "weaknesses", "risk", "risks",
                    "missing info", "missing information",
                    # Rename / find-and-replace cues — a workspace-wide rename
                    # ("correct the name X with Y") names no panel but must still
                    # reach the classifier (rule 12 fans it out to all panels).
                    "name", "spelling", "rename", "everywhere", "workspace",
                )
                _um_lower = um_early.lower()
                _maybe_edit = (
                    bool(um_early)
                    and any(v in _um_lower for v in _edit_verbs)
                    and (
                        any(w in _um_lower for w in _panel_words)
                        # When the chat is explicitly scoped to the Strategy &
                        # Objective panel, the panel is implied — an edit verb
                        # alone (e.g. "change the wordings", "add a few words")
                        # is enough to ask the classifier. Without this, such
                        # messages skip the surgical panel-edit path and fall
                        # through to the full-rewrite regenerator.
                        or chat_type_upper == "STRATEGY_OBJECTIVE"
                    )
                )
                cur_strat_for_edit = (
                    self._workspace_strategy_payload.get(workspace_id) or {}
                )
                if (
                    _maybe_edit
                    and isinstance(cur_strat_for_edit, dict)
                    and (cur_strat_for_edit.get("caseSummary") or "").strip()
                ):
                    try:
                        classification = await self._llm_classify_panel_edit_intent(
                            um_early,
                            current_strategy=cur_strat_for_edit,
                            default_panel=(
                                "strategicObjective"
                                if chat_type_upper == "STRATEGY_OBJECTIVE"
                                else None
                            ),
                        )
                    except Exception as cls_err:
                        logger.warning(
                            "Panel-edit classification failed (non-fatal): {}", cls_err
                        )
                        classification = {}

                    if classification and not classification.get("isQuestionLike"):
                        # WORKSPACE-WIDE RENAME: a name change must replace the
                        # party's name in the EXISTING content of EVERY panel
                        # (no regeneration). Don't rely on the LLM flagging each
                        # panel — force the SAME rename instruction onto all of
                        # them so coverage is exhaustive and deterministic.
                        _is_rename = bool(classification.get("isRename"))
                        if _is_rename:
                            _rename_instr = next(
                                (
                                    (classification.get(k) or {}).get("instruction")
                                    for k in (
                                        "strategicObjective",
                                        "caseSummary",
                                        "reportCaseSummary",
                                        "possibleNextSteps",
                                        "analysisReport",
                                        "listOfDates",
                                    )
                                    if (classification.get(k) or {}).get("instruction")
                                ),
                                "",
                            ) or (um_early or "").strip()
                            if _rename_instr:
                                for k in (
                                    "caseSummary",
                                    "reportCaseSummary",
                                    "strategicObjective",
                                    "possibleNextSteps",
                                    "analysisReport",
                                    "listOfDates",
                                ):
                                    classification[k] = {
                                        "shouldEdit": True,
                                        "instruction": _rename_instr,
                                    }
                        # Emit a single workspace:refresh up-front so the FE
                        # shows the loading state across all four strategy
                        # fields plus the cascaded panels (analysis_report,
                        # list_of_dates, applicable_laws) that will refresh
                        # after the strategy regen completes. We always
                        # emit BEFORE the first regen call so the FE loader
                        # is visible while Gemini works, and we always use
                        # the FE-recognised reason `objective_change_regeneration_started`
                        # so the loading panel actually appears in the UI.
                        _strategy_should_edit = any(
                            (classification.get(k) or {}).get("shouldEdit") is True
                            for k in (
                                "caseSummary",
                                "reportCaseSummary",
                                "strategicObjective",
                                "possibleNextSteps",
                            )
                        )
                        _analysis_should_edit = (
                            (classification.get("analysisReport") or {}).get(
                                "shouldEdit"
                            )
                            is True
                        )
                        _any_should_edit = (
                            _strategy_should_edit or _analysis_should_edit
                        )
                        if _any_should_edit:
                            # Loader for the panels we actually re-emit. A normal
                            # surgical edit touches only strategy_objective and/or
                            # analysis_report. A RENAME touches every panel (name
                            # replaced in existing content), so the loader covers
                            # them all.
                            _refresh_features: List[str] = []
                            if _strategy_should_edit:
                                _refresh_features.append("strategy_objective")
                            if _analysis_should_edit:
                                _refresh_features.append("analysis_report")
                            if _is_rename:
                                for _ft in (
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                ):
                                    if _ft not in _refresh_features:
                                        _refresh_features.append(_ft)
                            try:
                                await self._emit_workspace_refresh(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    reason="objective_change_regeneration_started",
                                    feature_types=_refresh_features,
                                )
                            except Exception as rf_err:
                                logger.warning(
                                    "workspace:refresh emit (panel edit) failed (non-fatal): {}",
                                    rf_err,
                                )

                        so_slot = classification.get("strategicObjective") or {}
                        if so_slot.get("shouldEdit") and so_slot.get("instruction"):
                            # Surgical edit: preserve everything except what
                            # the user named. The legacy
                            # _regenerate_strategy_objective_preserving_case_summary
                            # is intentionally NOT used here — its prompt is
                            # biased toward a full rewrite (labels current
                            # objective "to replace", caps output at 5-6
                            # sentences, forbids repetition) which causes
                            # surgical removes to collapse unrelated content.
                            ok = await self._regenerate_strategic_objective_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=so_slot["instruction"],
                                current_strategy=cur_strat_for_edit,
                                # Suppress per-editor emit: the strategy payload is
                                # emitted ONCE after all strategy fields are edited
                                # (a rename touches 3 fields → one emit, not three
                                # partial "Previous version" snapshots in the UI).
                                emit_result=False,
                            )
                            if ok:
                                panel_edit_applied.append("strategic objective")
                                cur_strat_for_edit = (
                                    self._workspace_strategy_payload.get(workspace_id)
                                    or cur_strat_for_edit
                                )
                            else:
                                logger.warning(
                                    "Strategic objective edit returned False — instruction={!r}",
                                    (so_slot.get("instruction") or "")[:200],
                                )

                        cs_slot = classification.get("caseSummary") or {}
                        rcs_slot = classification.get("reportCaseSummary") or {}
                        if cs_slot.get("shouldEdit") or rcs_slot.get("shouldEdit"):
                            _combined_instr = " ".join(
                                x
                                for x in [
                                    cs_slot.get("instruction"),
                                    rcs_slot.get("instruction"),
                                ]
                                if x
                            ).strip()
                            # Defensive: require at least one non-empty per-panel
                            # instruction. The classifier's _slot rejects empty
                            # instructions, so this should always be non-empty
                            # when we enter this branch — but guard anyway so
                            # we never pass the full multi-panel user message
                            # to the case-summary regen by accident.
                            if _combined_instr:
                                ok = await self._regenerate_case_summary_with_feedback(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_feedback=_combined_instr,
                                    current_strategy=cur_strat_for_edit,
                                    emit_result=False,
                                )
                                if ok:
                                    panel_edit_applied.append("case summary")
                                    cur_strat_for_edit = (
                                        self._workspace_strategy_payload.get(workspace_id)
                                        or cur_strat_for_edit
                                    )
                                else:
                                    logger.warning(
                                        "Case summary edit returned False — instruction={!r}",
                                        _combined_instr[:200],
                                    )

                        ns_slot = classification.get("possibleNextSteps") or {}
                        if ns_slot.get("shouldEdit") and ns_slot.get("instruction"):
                            ok = await self._regenerate_next_steps_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=ns_slot["instruction"],
                                current_strategy=cur_strat_for_edit,
                                emit_result=False,
                            )
                            if ok:
                                panel_edit_applied.append("possible next steps")
                            else:
                                logger.warning(
                                    "Possible next steps edit returned False — instruction={!r}",
                                    (ns_slot.get("instruction") or "")[:200],
                                )

                        ar_slot = classification.get("analysisReport") or {}
                        if ar_slot.get("shouldEdit") and ar_slot.get("instruction"):
                            cur_report_for_edit = (
                                self._workspace_analysis_payload.get(workspace_id)
                                or {}
                            )
                            if (
                                isinstance(cur_report_for_edit, dict)
                                and cur_report_for_edit
                            ):
                                ok = await self._regenerate_analysis_report_with_feedback(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_feedback=ar_slot["instruction"],
                                    current_report=cur_report_for_edit,
                                )
                                if ok:
                                    panel_edit_applied.append("analysis report")
                                else:
                                    logger.warning(
                                        "Analysis report edit returned False — instruction={!r}",
                                        (ar_slot.get("instruction") or "")[:200],
                                    )

                        # Mode 1 — name change propagates to List of Dates: replace
                        # the person's name in the EXISTING dated events only (no
                        # regeneration, no new/removed dates, relevance untouched).
                        # listOfDates is flagged ONLY by the rename / name-assignment
                        # rule (Rule 12), never by a general line edit.
                        lod_slot = classification.get("listOfDates") or {}
                        if lod_slot.get("shouldEdit") and lod_slot.get("instruction"):
                            ok = await self._regenerate_list_of_dates_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=lod_slot["instruction"],
                            )
                            if ok:
                                panel_edit_applied.append("list of dates")
                            else:
                                logger.warning(
                                    "List of dates name edit returned False — instruction={!r}",
                                    (lod_slot.get("instruction") or "")[:200],
                                )

                        # Mode 1 (rename only) — propagate the name change to the
                        # EXISTING Applicable Laws descriptions and the EXISTING
                        # Upcoming Dates (Procedural Timeline) entries. Name-only,
                        # no regeneration, no new/removed laws or milestones. Gated
                        # on isRename so a normal line edit never touches them.
                        if _is_rename and _rename_instr:
                            ok = await self._regenerate_applicable_laws_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=_rename_instr,
                            )
                            if ok:
                                panel_edit_applied.append("applicable laws")
                            ok = await self._regenerate_procedural_timeline_with_feedback(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_feedback=_rename_instr,
                            )
                            if ok:
                                panel_edit_applied.append("upcoming dates")

                        # Coalesced strategy emit: the three strategy editors above
                        # suppressed their own emits (emit_result=False), so the FE
                        # now receives ONE strategy_objective update for the whole
                        # edit instead of a partial snapshot per field — which is
                        # what surfaced as several "Previous version" states during a
                        # rename. (The analysis report is a separate featureType and
                        # emits itself once.)
                        _strategy_panels = (
                            "strategic objective",
                            "case summary",
                            "possible next steps",
                        )
                        if (
                            any(p in panel_edit_applied for p in _strategy_panels)
                            and self._socket_client
                        ):
                            _final_strategy = self._workspace_strategy_payload.get(
                                workspace_id
                            )
                            if isinstance(_final_strategy, dict) and _final_strategy:
                                await self._socket_client.emit(
                                    "document_feature_response",
                                    {
                                        "featureType": "strategy_objective",
                                        "workspaceId": workspace_id,
                                        "userId": user_id,
                                        "payload": _final_strategy,
                                    },
                                )
                                logger.success(
                                    "✅ Strategy panel emitted once after edits {} (workspace={})",
                                    [p for p in panel_edit_applied if p in _strategy_panels],
                                    workspace_id[:12] if workspace_id else "",
                                )

                        if panel_edit_applied:
                            # Reuse the existing skip-chat-agent gate. The ack
                            # block below has a dedicated branch for
                            # objective_ack_kind == "panel_edit_direct".
                            objective_regen_ok = True
                            objective_ack_kind = "panel_edit_direct"
                            logger.success(
                                "✅ Panel edits applied from free-form chat: {} (workspace={})",
                                panel_edit_applied,
                                workspace_id[:12] if workspace_id else "",
                            )
                            # NOTE: intentionally NO _schedule_objective_change_reanalysis
                            # here. These are SURGICAL edits — the strategy fields and
                            # the analysis report were already edited in place above.
                            # Running the doc-based reanalysis would regenerate the
                            # analysis report from documents (clobbering the surgical
                            # edit and re-introducing whatever the user just corrected,
                            # e.g. a misspelled name). list_of_dates, applicable_laws,
                            # procedural_timeline and document assessments are
                            # deliberately left untouched by panel edits.

                # User said yes after we asked whether to update the objective (informal discussion flow)
                if (
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and is_user_affirmation_to_continue(um_early)
                    and not panel_edit_applied
                ):
                    last_ai_for_offer = _last_ai_message_from_history(
                        chat_history_resolved
                    )
                    if not last_ai_for_offer:
                        last_ai_for_offer = (
                            self._intelligence_last_ai_by_chat.get(chat_id) or ""
                        )
                    if self._last_ai_offered_objective_update(last_ai_for_offer):
                        thread_txt = self._format_thread_for_objective_extraction(
                            chat_history_resolved, um_early
                        )
                        instr = await self._summarize_objective_instruction_from_thread(
                            thread_txt,
                            (role_context.get("role") or "ADJUDICATOR"),
                        )
                        cur_strat = self._workspace_strategy_payload.get(workspace_id)
                        if (
                            instr
                            and len(instr.strip()) >= 15
                            and isinstance(cur_strat, dict)
                            and (cur_strat.get("caseSummary") or "").strip()
                        ):
                            await self._emit_workspace_refresh(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                reason="objective_change_regeneration_started",
                                feature_types=[
                                    "strategy_objective",
                                    "analysis_report",
                                    "list_of_dates",
                                    "applicable_laws",
                                    "procedural_timeline",
                                    "drafting_assistant",
                                ],
                            )
                            regen_ok = (
                                await self._regenerate_strategy_objective_preserving_case_summary(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    workspace_role=(
                                        role_context.get("role") or "ADJUDICATOR"
                                    ),
                                    user_objective_instruction=instr,
                                    current_strategy=cur_strat,
                                )
                            )
                            if regen_ok:
                                objective_regen_ok = True
                                objective_ack_kind = "followup_ok"
                                self._schedule_objective_change_reanalysis(
                                    workspace_id=workspace_id,
                                    user_id=user_id,
                                    log_context=(
                                        " (after user confirmed objective update from discussion)"
                                    ),
                                )
                            else:
                                objective_regen_ok = True
                                objective_ack_kind = "followup_clarify"
                        else:
                            objective_regen_ok = True
                            objective_ack_kind = "followup_clarify"

                objective_instruction: Optional[str] = None
                if not objective_regen_ok and chat_type_upper == "STRATEGY_OBJECTIVE":
                    prep = self._extract_intelligence_chat_panel_updates(
                        combined_text_for_updates
                    )
                    objective_instruction = prep.get("strategicObjective")
                    if not objective_instruction:
                        intent_cls = await self._llm_classify_objective_change_intent(
                            combined_text_for_updates,
                            chat_type_upper=chat_type_upper,
                        )
                        mode = str(intent_cls.get("mode") or "none")
                        norm_obj = str(intent_cls.get("objectiveText") or "").strip()
                        is_question_like = bool(
                            intent_cls.get("isQuestionLike") is True
                        )
                        logger.info(
                            "Objective intent classifier: mode={}, isQuestionLike={}, updateRequestedNow={}, objectiveTextLen={}",
                            mode,
                            is_question_like,
                            bool(intent_cls.get("updateRequestedNow") is True),
                            len(norm_obj),
                        )
                        if mode == "direct_update" and not is_question_like:
                            objective_instruction = norm_obj or combined_text_for_updates
                        elif mode == "ask_confirmation" and not is_question_like:
                            # Scoped Strategy & Objective chat: a clear objective
                            # statement here is the user setting the objective, so
                            # apply it DIRECTLY (regenerate + full 5-panel refresh)
                            # rather than only offering "do you want to update?".
                            # Questions are already filtered by
                            # _is_pure_informational_query, so this won't fire on a
                            # query. Without this, an ask_confirmation objective
                            # change skipped the refresh and dates/laws/timeline
                            # never showed the loader.
                            objective_instruction = (
                                norm_obj or combined_text_for_updates
                            )
                            implicit_objective_shift_text = objective_instruction
                if (
                    not objective_regen_ok
                    and objective_instruction
                    and chat_type_upper == "STRATEGY_OBJECTIVE"
                    and not self._is_pure_informational_query(um_early)
                ):
                    cur_strat = self._workspace_strategy_payload.get(workspace_id)
                    if isinstance(cur_strat, dict) and (
                        cur_strat.get("caseSummary") or ""
                    ).strip():
                        await self._emit_workspace_refresh(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            reason="objective_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                                "drafting_assistant",
                            ],
                        )
                        objective_regen_ok = (
                            await self._regenerate_strategy_objective_preserving_case_summary(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_objective_instruction=objective_instruction,
                                current_strategy=cur_strat,
                            )
                        )
                        if objective_regen_ok:
                            objective_ack_kind = "direct"
                            self._schedule_objective_change_reanalysis(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                log_context=" (from direct objective instruction)",
                            )

                update_result = await self._try_apply_intelligence_chat_panel_updates(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    combined_text=combined_text_for_updates,
                    ignore_strategic_objective=bool(objective_regen_ok),
                )
                if update_result.get("applied"):
                    logger.success("✅ Panel updates applied from chat message")
            except Exception as upd_err:
                logger.warning(
                    "Panel update attempt failed; continuing with generic chat. err={}",
                    str(upd_err),
                )

            chat_type_to_module = {
                "ANALYSIS_REPORT": "analysis_report",
                "STRATEGY_OBJECTIVE": "strategy_objective",
                "APPLICABLE_LAWS": "applicable_laws",
                "DRAFTING_ASSISTANT": "drafting_assistant",
                "TEMPLATES_CASE_LAWS": "case_laws",
            }
            active_module = chat_type_to_module.get(chat_type_upper)
            cached_analysis = self._workspace_analysis_payload.get(workspace_id)
            if cached_analysis and isinstance(cached_analysis, dict):
                workspace_state["analysis_report"] = dict(cached_analysis)

            custom_chat_suffix = build_custom_instruction_suffix(workspace_id) or None

            # STEP 2 & 3: Build chat context (history merged above for objective follow-up + graph)
            chat_context = ChatContext(
                workspace_id=workspace_id,
                user_id=user_id,
                role_context=role_context,
                workspace_state=workspace_state,
                active_module=active_module,
                custom_instruction_suffix=custom_chat_suffix,
                chat_history=chat_history_resolved,
            )

            # STEP 4: Process with Intelligence Chat Agent (skip when objective-only regen ran)
            skip_intelligence_llm = bool(objective_regen_ok)
            import random

            ai_update_result: Dict[str, Any] = {"applied": False}
            start_time = datetime.utcnow()
            if skip_intelligence_llm:
                logger.info(
                    "Skipping intelligence chat LLM (strategy objective regen emitted)"
                )
                ack_parts: List[str] = []
                if objective_ack_kind == "panel_edit_direct" and panel_edit_applied:
                    # Free-form panel edit. Short, factual ack — the panel
                    # itself was already emitted via document_feature_response.
                    _fields_human = ", ".join(panel_edit_applied)
                    ack_parts.append(f"Updated: {_fields_human}.")
                elif objective_ack_kind == "followup_clarify":
                    ack_parts.append(
                        "I could not infer a clear new strategic objective from our discussion. "
                        "Please briefly state the priority you want (for example venue, relief, or focus), "
                        "and I will update the strategy and analysis panels."
                    )
                elif objective_ack_kind == "followup_ok":
                    ack_parts.append(
                        "I have updated the strategic objective and possible next steps. "
                        "I am refreshing the analysis report, list of dates, and applicable laws "
                        "to align with this objective based on our discussion."
                    )
                elif objective_ack_kind == "direct" or objective_regen_ok:
                    # Keep chat response short to avoid duplicating strategy panel payload
                    # that is already emitted via document_feature_response.
                    ack_parts.append(
                        "I have updated the strategic objective and possible next steps. "
                        "I am refreshing the analysis report, list of dates, and applicable laws "
                        "to align with this objective based on our discussion."
                    )
                if isinstance(update_result, dict) and update_result.get("applied"):
                    extra = (update_result.get("response_text") or "").strip()
                    if extra:
                        ack_parts.append(extra)
                response_text_to_emit = " ".join(ack_parts).strip() or "Panel updated."
                from types import SimpleNamespace

                response = SimpleNamespace(
                    response_text=response_text_to_emit,
                    message_id=chat_id,
                    tokens_used=0,
                    confidence=SimpleNamespace(value="high"),
                    detected_intent="strategy_objective_refresh",
                )
                processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000
                suggestion_chips_for_payload = []
                try:
                    ws_chips = self._workspace_suggestion_chips.get(workspace_id) or []
                    if ws_chips:
                        suggestion_chips_for_payload = random.sample(
                            ws_chips, min(4, len(ws_chips))
                        )
                except Exception:
                    pass
            else:
                logger.info("🤖 Processing message with Intelligence Chat Agent...")
                response = await intelligence_chat_agent.process_message(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    message_id=chat_id,
                    user_message=payload.content,
                    chat_context=chat_context,
                )
                # If downstream chat intent says STRATEGY_CHANGE in Strategy & Objective chat,
                # force the same direct objective-refresh pipeline (refresh -> strategy regen -> reanalysis).
                detected_intent_upper = str(
                    getattr(response, "detected_intent", "") or ""
                ).upper()
                if (
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and detected_intent_upper == "STRATEGY_CHANGE"
                    and not self._is_pure_informational_query(payload.content or "")
                ):
                    cur_strat = self._workspace_strategy_payload.get(workspace_id)
                    if isinstance(cur_strat, dict) and (
                        cur_strat.get("caseSummary") or ""
                    ).strip():
                        await self._emit_workspace_refresh(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            reason="objective_change_regeneration_started",
                            feature_types=[
                                "strategy_objective",
                                "analysis_report",
                                "list_of_dates",
                                "applicable_laws",
                                "procedural_timeline",
                                "drafting_assistant",
                            ],
                        )
                        regen_ok = (
                            await self._regenerate_strategy_objective_preserving_case_summary(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                workspace_role=(
                                    role_context.get("role") or "ADJUDICATOR"
                                ),
                                user_objective_instruction=(payload.content or "").strip(),
                                current_strategy=cur_strat,
                            )
                        )
                        if regen_ok:
                            self._schedule_objective_change_reanalysis(
                                workspace_id=workspace_id,
                                user_id=user_id,
                                log_context=" (promoted from strategy_change intent)",
                            )
                            objective_regen_ok = True
                            objective_ack_kind = "direct"
                            response_text_to_emit = (
                                "I have updated the strategic objective and possible next steps. "
                                "I am refreshing the analysis report, list of dates, and applicable laws "
                                "to align with this objective based on our discussion."
                            )
                            from types import SimpleNamespace

                            response = SimpleNamespace(
                                response_text=response_text_to_emit,
                                message_id=chat_id,
                                tokens_used=0,
                                confidence=SimpleNamespace(value="high"),
                                detected_intent="strategy_objective_refresh",
                            )
                            processing_time = (
                                datetime.utcnow() - start_time
                            ).total_seconds() * 1000
                            suggestion_chips_for_payload = []
                            try:
                                ws_chips = (
                                    self._workspace_suggestion_chips.get(workspace_id)
                                    or []
                                )
                                if ws_chips:
                                    suggestion_chips_for_payload = random.sample(
                                        ws_chips, min(4, len(ws_chips))
                                    )
                            except Exception:
                                pass
                # If assistant produced panel-update markers, apply them as well.
                if objective_regen_ok:
                    ai_update_result = {"applied": False}
                elif chat_type_upper != "STRATEGY_OBJECTIVE":
                    try:
                        ai_update_result = await self._try_apply_intelligence_chat_panel_updates(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            combined_text=response.response_text or "",
                        )
                        if ai_update_result.get("applied"):
                            logger.success(
                                "✅ Panel updates applied from AI response markers"
                            )
                    except Exception as ai_upd_err:
                        logger.warning(
                            "AI response panel update attempt failed; continuing. err={}",
                            str(ai_upd_err),
                        )
                else:
                    logger.debug(
                        "Skipping AI response marker application for STRATEGY_OBJECTIVE chat to avoid duplicate objective refresh flows"
                    )
                # If we applied deterministic panel updates, override the chat text
                if objective_regen_ok:
                    pass
                elif isinstance(update_result, dict) and update_result.get("applied"):
                    response_text_to_emit = update_result.get("response_text")
                elif isinstance(ai_update_result, dict) and ai_update_result.get(
                    "applied"
                ):
                    cleaned = self._strip_panel_update_markers_from_text(
                        response.response_text or ""
                    )
                    response_text_to_emit = (
                        cleaned
                        if cleaned
                        else ai_update_result.get("response_text")
                        or response.response_text
                    )
                else:
                    response_text_to_emit = response.response_text
                # Informal strategic discussion: offer to update panels (user can reply yes next turn)
                if (
                    not objective_regen_ok
                    and
                    chat_type_upper == "STRATEGY_OBJECTIVE"
                    and OBJECTIVE_UPDATE_OFFER_QUESTION.lower()
                    not in (response_text_to_emit or "").lower()
                ):
                    try:
                        should_append_offer = bool(
                            implicit_objective_shift_text
                        ) or await self._should_offer_objective_update_after_discussion(
                            payload.content or "",
                            response_text_to_emit or "",
                        )
                        if should_append_offer:
                            response_text_to_emit = (
                                (response_text_to_emit or "").rstrip()
                                + "\n\n"
                                + OBJECTIVE_UPDATE_OFFER_QUESTION
                            )
                    except Exception as offer_err:
                        logger.warning(
                            "Objective update offer append failed: {}", offer_err
                        )
                if not objective_regen_ok:
                    processing_time = (
                        datetime.utcnow() - start_time
                    ).total_seconds() * 1000

                    # Generate 10-12 contextual chips, store for this workspace; send random 4 in response (backend polls every few sec and we return random 4 each time)
                    suggestion_chips_for_payload = []
                    try:
                        role_for_chips = (role_context.get("role") or "ADJUDICATOR").upper()
                        generated = await self._generate_contextual_suggestion_chips(
                            workspace_id=workspace_id,
                            role=role_for_chips,
                            user_message=payload.content,
                            ai_response_text=response_text_to_emit,
                            max_chips=12,
                        )
                        if generated:
                            self._workspace_chat_suggestion_chips[workspace_id] = list(
                                generated
                            )
                            suggestion_chips_for_payload = random.sample(
                                generated, min(4, len(generated))
                            )
                            logger.info(
                                "Stored {} contextual chips for workspace; sending random 4 in ai_response (each 4s poll will return random 4 from these {})",
                                len(generated),
                                len(generated),
                            )
                    except Exception as chip_err:
                        logger.warning(
                            "Suggestion chips generation failed: {}", str(chip_err)
                        )

            # STEP 5: Build AI response payload (suggestionChips = random 4 from 10-12; click sends chip as next message)
            ai_response = AIResponsePayload(
                chatId=chat_id,
                workspaceId=workspace_id,
                userId=user_id,
                content=response_text_to_emit,
                metadata=AIResponseMetadata(
                    messageId=response.message_id,
                    aiModel=settings.gemini_model,
                    tokensUsed=response.tokens_used,
                    responseTime=processing_time,
                    confidence=(
                        response.confidence.value
                        if hasattr(response.confidence, "value")
                        else str(response.confidence)
                    ),
                    detectedIntent=response.detected_intent,
                ),
                suggestionChips=(
                    suggestion_chips_for_payload
                    if suggestion_chips_for_payload
                    else None
                ),
            )

            # STEP 6: Emit ai_response back to server. _pending_ai_response is
            # the buffer that on_connected re-emits after a reconnect — we
            # populate it ONLY when the emit was not confirmed delivered, so a
            # successful emit does not get duplicated on the next disconnect
            # cycle. See _handle_drafting_assistant_message for the same fix.
            if self._socket_client:
                payload_dict = ai_response.dict(by_alias=True)
                if payload_dict.get("suggestionChips") is None:
                    payload_dict.pop("suggestionChips", None)
                emit_succeeded = False
                try:
                    await self._socket_client.emit_ai_response(payload_dict)
                    emit_succeeded = bool(
                        getattr(self._socket_client, "is_connected", True)
                    )
                except Exception as emit_err:
                    logger.error("Failed to emit ai_response: {}", emit_err)
                if emit_succeeded:
                    self._pending_ai_response = None
                    logger.success("✅ AI response sent successfully")
                else:
                    self._pending_ai_response = payload_dict
                    logger.warning(
                        "ai_response not confirmed delivered — will retry on reconnect"
                    )
                await self._remember_workspace_role(
                    workspace_id,
                    user_id,
                    (role_context.get("role") or "ADJUDICATOR"),
                    chat_id=chat_id,
                )
                # Cache assistant text for follow-ups like "yes" when conversation_history is incomplete
                _rt = (response_text_to_emit or "").strip()
                if (
                    _rt
                    and "Select one role to proceed" not in _rt
                    and not _rt.startswith(ANNEXURE_UPLOAD_GATE_MESSAGE[:40])
                    and not _rt.lower().startswith("i apologize")
                ):
                    self._intelligence_last_ai_by_chat[chat_id] = _rt
            else:
                logger.error("❌ Socket client not available to emit response")

            # STEP 7: Log audit event
            audit_logger.log_event(
                "user_message_processed",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "message_length": len(payload.content),
                    "response_length": len(response_text_to_emit),
                    "processing_time_ms": processing_time,
                    "intent": response.detected_intent,
                },
            )

        except Exception as e:
            logger.error("Error processing user_message_request: {}", str(e))
            logger.exception(e)

            # Try to send error response back (only if payload was successfully parsed)
            try:
                # Get basic info from data dict for error response
                chat_id = data.get("chatId")
                workspace_id = data.get("workspaceId", "unknown")
                user_id = data.get("userId", "unknown")

                if self._socket_client and chat_id:
                    error_response = AIResponsePayload(
                        chatId=chat_id,
                        workspaceId=workspace_id,
                        userId=user_id,
                        content=f"I apologize, but I encountered an error processing your message: {str(e)}",
                        metadata=AIResponseMetadata(
                            messageId=chat_id,
                            aiModel=settings.gemini_model,
                            error=str(e),
                        ),
                    )
                    await self._socket_client.emit_ai_response(
                        error_response.dict(by_alias=True)
                    )
                    logger.info("Error response sent to user")
            except Exception as emit_error:
                logger.error(f"Failed to emit error response: {emit_error}")

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

    async def on_probing_prompts_request(self, data: Dict[str, Any]) -> None:
        """
        Handle probing_prompts_request from backend.
        - If conversation context is provided (lastUserMessage, lastAiResponse, or conversation_history),
          generate contextual, role-based suggestion chips so they change with the chat.
        - Otherwise return a random 4 from the stored 15-16 strategy chips (role/case-related).
        Chips are sent as prompts[]; when user clicks one, that text goes into the chat as the user message.
        """
        import random

        try:
            workspace_id = (
                data.get("workspaceId") or data.get("workspace_id") or ""
            ).strip()
            chat_type = (data.get("chatType") or data.get("chat_type") or "").strip()
            max_prompts = data.get("maxPrompts") or data.get("max_prompts")
            if max_prompts is None:
                max_prompts = 4
            try:
                max_prompts = int(max_prompts)
            except (TypeError, ValueError):
                max_prompts = 4

            prompts = []
            if chat_type == "ANALYSIS_REPORT":
                # Analysis Report: return random 4 from stored 15-16 analysis-report chips (same pattern as strategy)
                chips = (
                    self._workspace_analysis_suggestion_chips.get(workspace_id) or []
                )
                if not chips:
                    chips = self._fallback_chips_for_analysis_report(16)
                count = min(max_prompts, len(chips))
                prompts = random.sample(chips, count) if count > 0 else []
                logger.info(
                    "probing_prompts: ANALYSIS_REPORT, returning random {} from {} chips",
                    len(prompts),
                    len(chips),
                )
            elif chat_type == "DRAFTING_ASSISTANT":
                # Drafting panel: serve from the drafting-specific chip cache
                # (populated by _handle_drafting_assistant_message after every
                # drafting turn). Prefer per-session chips when BE supplies
                # actionableId; fall back to workspace-keyed chips; then
                # static drafting fallback. This isolates the drafting strip
                # from the intelligence-chat chip cache so the lawyer never
                # sees stale Strategy / Analysis chips here.
                actionable_id_for_chips = (
                    (data.get("actionableId") or data.get("actionable_id") or "").strip()
                )
                chips = (
                    self._workspace_drafting_suggestion_chips.get(actionable_id_for_chips)
                    or self._workspace_drafting_suggestion_chips.get(workspace_id)
                    or []
                )
                source = (
                    "actionable" if (actionable_id_for_chips and chips)
                    else "workspace" if chips
                    else "fallback"
                )
                if not chips:
                    chips = self._fallback_drafting_chips(8)
                count = min(max_prompts, len(chips))
                prompts = random.sample(chips, count) if count > 0 else []
                logger.info(
                    "probing_prompts: DRAFTING_ASSISTANT, returning random {} from {} chips (source={}, actionableId={})",
                    len(prompts),
                    len(chips),
                    source,
                    actionable_id_for_chips[:24] if actionable_id_for_chips else "(none)",
                )
            else:
                # STRATEGY_OBJECTIVE or other: contextual chips, then strategy chips
                # 1) If we have 10-12 stored contextual chips for this workspace, return random 4 each time (so each poll shows different chips)
                stored_chat_chips = (
                    self._workspace_chat_suggestion_chips.get(workspace_id) or []
                )
                if stored_chat_chips:
                    count = min(max_prompts, len(stored_chat_chips))
                    prompts = (
                        random.sample(stored_chat_chips, count) if count > 0 else []
                    )
                    logger.info(
                        "probing_prompts: returning random {} from {} stored chips (rotation)",
                        len(prompts),
                        len(stored_chat_chips),
                    )
                else:
                    # 2) No stored chat chips: if we have conversation context, generate 10-12, store, return random 4
                    last_user = (
                        data.get("lastUserMessage")
                        or data.get("last_user_message")
                        or ""
                    ).strip()
                    last_ai = (
                        data.get("lastAiResponse") or data.get("last_ai_response") or ""
                    ).strip()
                    conv_history = (
                        data.get("conversation_history")
                        or data.get("conversationHistory")
                        or []
                    )
                    if conv_history and (not last_user and not last_ai):
                        for m in reversed(conv_history):
                            r = (m.get("role") or "").upper()
                            c = (m.get("content") or "").strip()
                            if not c:
                                continue
                            if r == "USER" and not last_user:
                                last_user = c[:500]
                            elif r == "AI" and not last_ai:
                                last_ai = c[:500]
                            if last_user and last_ai:
                                break
                    prompts = []
                    if (
                        last_user
                        or last_ai
                        or (conv_history and len(conv_history) >= 2)
                    ):
                        role = "ADJUDICATOR"
                        try:
                            ctx = workspace_manager.get_workspace(workspace_id)
                            if ctx and ctx.workspace_role:
                                role = (ctx.workspace_role or "ADJUDICATOR").upper()
                        except Exception:
                            pass
                        if last_user or last_ai:
                            conv_summary = None
                            if conv_history and len(conv_history) > 2:
                                parts = []
                                for m in conv_history[-4:]:
                                    r = m.get("role") or m.get("role", "")
                                    c = (m.get("content") or "")[:80]
                                    if c:
                                        parts.append(f"{r}: {c}")
                                conv_summary = " | ".join(parts) if parts else None
                            generated = (
                                await self._generate_contextual_suggestion_chips(
                                    workspace_id=workspace_id,
                                    role=role,
                                    user_message=last_user,
                                    ai_response_text=last_ai,
                                    max_chips=12,
                                    conversation_summary=conv_summary,
                                )
                            )
                            if generated:
                                self._workspace_chat_suggestion_chips[workspace_id] = (
                                    generated
                                )
                                count = min(max_prompts, len(generated))
                                prompts = (
                                    random.sample(generated, count) if count > 0 else []
                                )
                                logger.info(
                                    "probing_prompts: generated {} contextual chips, returning random {}",
                                    len(generated),
                                    len(prompts),
                                )
                    # 3) Fallback: strategy chips (random 4 from 15-16)
                    if not prompts:
                        chips = self._workspace_suggestion_chips.get(workspace_id) or []
                        if not chips:
                            strategy_payload = (
                                self._workspace_strategy_payload.get(workspace_id) or {}
                            )
                            arr = strategy_payload.get("suggestionChips") or []
                            if isinstance(arr, list):
                                chips = [str(x).strip() for x in arr if str(x).strip()]
                                if chips:
                                    self._workspace_suggestion_chips[workspace_id] = list(
                                        chips
                                    )
                                    self._last_workspace_id_with_chips = workspace_id
                        if not chips and self._last_workspace_id_with_chips:
                            chips = (
                                self._workspace_suggestion_chips.get(
                                    self._last_workspace_id_with_chips
                                )
                                or []
                            )
                        if not chips:
                            role = "ADJUDICATOR"
                            try:
                                ctx = workspace_manager.get_workspace(workspace_id)
                                if ctx and ctx.workspace_role:
                                    role = (ctx.workspace_role or "ADJUDICATOR").upper()
                            except Exception:
                                pass
                            chips = self._fallback_chips_for_role(role, 16)
                            self._workspace_suggestion_chips[workspace_id] = list(chips)
                            self._last_workspace_id_with_chips = workspace_id
                        if not chips:
                            logger.warning(
                                "probing_prompts_request: no suggestion chips for workspace {}",
                                workspace_id,
                            )
                        count = min(max_prompts, len(chips)) if chips else 0
                        prompts = random.sample(chips, count) if count > 0 else []
                    # 4) Always return at least generic prompts so UI never gets count=0
                    if not prompts:
                        generic = [
                            "Summarize key points from the documents",
                            "Explain the main strengths",
                            "What are the risks or gaps?",
                            "Suggest next steps",
                        ]
                        prompts = generic[:max_prompts]
                        logger.info(
                            "probing_prompts_request: using generic fallback prompts (count={})",
                            len(prompts),
                        )

            response = {
                "workspaceId": data.get("workspaceId"),
                "userId": data.get("userId"),
                "actionableId": data.get("actionableId"),
                "chatId": data.get("chatId"),
                "chatType": data.get("chatType"),
                "viewType": data.get("viewType"),
                "prompts": prompts,
            }
            if data.get("timestamp") is not None:
                response["timestamp"] = data.get("timestamp")
            if self._socket_client:
                import time

                now = time.time()
                last = self._last_probing_emit_time.get(workspace_id) or 0
                if now - last < self.PROBING_THROTTLE_SECONDS:
                    logger.debug(
                        "probing_prompts_response throttled (last emit %.1fs ago), workspaceId=%s",
                        now - last,
                        workspace_id[:12] if workspace_id else "",
                    )
                else:
                    self._last_probing_emit_time[workspace_id] = now
                    await self._socket_client.emit("probing_prompts_response", response)
                    logger.success(
                        "probing_prompts_response emitted: workspaceId={}, count={}",
                        workspace_id[:12] if workspace_id else "",
                        len(prompts),
                    )
            else:
                logger.warning("No socket client to emit probing_prompts_response")
        except Exception as e:
            logger.exception(e)
            logger.error("probing_prompts_request failed: {}", str(e))

    # ========================================================================
    # Legacy Event Handlers (for backward compatibility)
    # ========================================================================

    async def on_workspace_updated(self, data: Dict[str, Any]) -> None:
        """Handle workspace update event (legacy)."""
        try:
            logger.debug("Processing legacy workspace_updated event")
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace updated (legacy): {workspace_id}")

            audit_logger.log_event(
                "workspace_updated_legacy", {"workspace_id": workspace_id}
            )
        except Exception as e:
            logger.error(f"Error processing workspace_updated: {e}")
            raise

    async def on_workspace_deleted(self, data: Dict[str, Any]) -> None:
        """Handle workspace deletion event (legacy)."""
        try:
            workspace_id = data.get("workspace_id") or data.get("workspaceId")
            logger.info(f"Workspace deleted (legacy): {workspace_id}")

            # Delete the workspace
            await workspace_manager.delete_workspace(workspace_id)

            audit_logger.log_event("workspace_deleted", {"workspace_id": workspace_id})
        except Exception as e:
            logger.error(f"Error processing workspace_deleted: {e}")
            raise


# Global event handler instance
event_handler = SocketEventHandler()
