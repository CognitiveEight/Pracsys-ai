"""Pure module-level helpers, constants, and regexes for socket events.

Extracted verbatim from the original events.py. Stdlib-only (a dependency
leaf) so any handler module can import these without circular-import risk.
"""

import re
import uuid
from datetime import date as date_type
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

__all__ = [
    'EventHandler',
    '_STRATEGY_ROLE_LABELS_RE',
    'OBJECTIVE_UPDATE_OFFER_QUESTION',
    'OBJECTIVE_CHANGE_REFRESH_FEATURES',
    'OBJECTIVE_CHANGE_FAILED_MESSAGE',
    'CHAT_SMALL_WORKSPACE_DOC_THRESHOLD',
    'CHAT_SEARCH_CHUNK_LIMIT',
    'CHAT_MAX_DOCS_INCLUDED',
    'CHAT_MAX_CHARS_PER_DOC',
    'CHAT_MAX_TOTAL_CHARS',
    'CHAT_MIN_SCORE_THRESHOLD',
    '_ANNEXURE_REF_RE',
    '_ROMAN_VALUES',
    'SPEAKER_LABEL_GUARDRAIL',
    '_ENUM_PREFIX_RE',
    '_UI_TRUNCATION_RE',
    '_APP_BRAND_RE',
    '_STRENGTH_POINT_MIN_LEN',
    '_ANNEXURE_SINGLE_RE',
    '_ANNEXURE_PLURAL_RE',
    '_OVERVIEW_CONTACTED_DATE_RE',
    '_OVERVIEW_TO_PHONE_RE',
    '_roman_or_int',
    '_derive_report_case_summary',
    '_coerce_analysis_text_item',
    '_strip_enum_prefix',
    '_strip_ui_truncated_tokens',
    'scrub_app_brand',
    '_coerce_analysis_text_list',
    '_mint_item_id',
    '_normalize_match_text',
    'find_ungrounded_proper_nouns',
    'find_dropped_case_figures',
    '_strip_strength_point_statute_boilerplate',
    '_annexure_wording_for_next_steps',
    '_upload_wording',
    '_clamp_annexure_references',
    '_format_document_for_prompt',
    '_normalize_report_mobile_from_overview',
    '_overview_conversation_date_to_iso',
    'extract_overview_conversation_report_contact_meta',
    'drafting_payload_to_wire',
]


EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


_STRATEGY_ROLE_LABELS_RE = re.compile(
    r"\b(?:adjudicator|petitioner|respondent|mediator|client|clients)\b",
    re.I,
)


OBJECTIVE_UPDATE_OFFER_QUESTION = (
    "Do you want me to update the whole workspace based on this new objective "
    "that you have defined?"
)


# The panels whose loaders an objective change arms via `workspace:refresh`.
# Kept in ONE place because a loader is cleared only by a
# document_feature_response for the same featureType: every site that arms this
# list must be able to release exactly this list when the work fails, and a
# copy that drifts leaves a panel spinning with no way to notice.
OBJECTIVE_CHANGE_REFRESH_FEATURES = [
    "strategy_objective",
    "analysis_report",
    "list_of_dates",
    "applicable_laws",
    "procedural_timeline",
    "drafting_assistant",
]

# What the user is told when an objective change could not be applied. It must
# never imply the objective changed: the previous objective is still in force,
# and silently falling through to the general chat agent (the old behaviour)
# left the lawyer reading an unrelated answer beside six spinning panels.
OBJECTIVE_CHANGE_FAILED_MESSAGE = (
    "I could not update the strategic objective just now, so your previous "
    "objective is unchanged. Nothing else in the workspace was modified. "
    "Please send the objective again and I will retry."
)


CHAT_SMALL_WORKSPACE_DOC_THRESHOLD = 15  # at/below this, include every doc


CHAT_SEARCH_CHUNK_LIMIT = 40  # chunks fetched before dedup-to-document


CHAT_MAX_DOCS_INCLUDED = 12  # distinct docs included in large workspaces


CHAT_MAX_CHARS_PER_DOC = 40000  # ~10K tokens/doc


CHAT_MAX_TOTAL_CHARS = 400000  # ~100K tokens of annexure context


CHAT_MIN_SCORE_THRESHOLD = 0.30  # soft signal only (fallback trigger), never a hard drop


_ANNEXURE_REF_RE = re.compile(
    r"annexure[\s\-_]*([0-9]{1,3}|[ivxlcdm]+)\b",
    re.I,
)


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


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


_APP_BRAND_RE = re.compile(r"\bsimpler\s*today(?:\s*\.?\s*ai)?\b", re.IGNORECASE)


_STRENGTH_POINT_MIN_LEN = 60


_ANNEXURE_SINGLE_RE = re.compile(r"\bAnnexure\s+(\d+)\b", re.IGNORECASE)


_ANNEXURE_PLURAL_RE = re.compile(
    r"\bAnnexures\s+(\d+(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s+)\d+)*)\b",
    re.IGNORECASE,
)


_OVERVIEW_CONTACTED_DATE_RE = re.compile(
    r"Contacted\s+SimplerToday(?:\.ai)?\s+on:\s*"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
    re.I,
)


_OVERVIEW_TO_PHONE_RE = re.compile(
    r"To:\s*(\+?91[\s\-]*\d[\d\s\-]{8,18})",
    re.I,
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


def _strip_enum_prefix(text: str) -> str:
    """Strip a single leading enumeration / bullet prefix from a list item."""
    if not text:
        return text
    stripped = _ENUM_PREFIX_RE.sub("", text, count=1)
    return stripped.strip() or text


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


# Non-word runs collapsed to a single space when building a match key, so that
# "S. 125 CrPC" and "Section 125 Cr.P.C." normalize toward the same shape.
_MATCH_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _mint_item_id(section: str) -> str:
    """Mint a stable server-side id for a NEW reconciled item.

    Shape mirrors the frontend id scheme (e.g. ``missingInfo_ab12cd34ef56``):
    the panel/section slug, an underscore, and 12 hex chars. Used for the four
    analysis text sections (missingInfo/risks/weaknesses/relevantJurisdiction)
    and for list-of-dates entries. Parties keep ``party_N`` and laws keep
    ``law_N`` (their existing id schemes) — reconciliation only carries a prior
    id / sets ``isMatched`` for those.
    """
    slug = (section or "item").strip() or "item"
    return f"{slug}_{uuid.uuid4().hex[:12]}"


def _normalize_match_text(text: Any) -> str:
    """Deterministic first-pass match key: lowercase, strip punctuation, and
    collapse whitespace. Two items with the same normalized key are treated as
    the same content without needing an LLM call. Returns ``""`` for empties.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return _MATCH_NORMALIZE_RE.sub(" ", text.lower()).strip()


# Generic legal / forum / connector / honorific tokens that are NOT distinctive
# proper nouns. A multi-word Capitalised phrase is treated as a potential
# fabrication only when, after removing these, it still carries a distinctive
# token that appears in NONE of the allowed source texts.
_GROUNDING_STOPWORDS = frozenset({
    "the", "of", "and", "for", "at", "in", "to", "a", "an", "by", "on", "with",
    "or", "high", "court", "supreme", "district", "sessions", "magistrate",
    "family", "tribunal", "commission", "bench", "judicature", "civil",
    "criminal", "side", "authority", "board", "association", "committee",
    "panel", "council", "office", "bureau", "department", "ministry",
    "government", "state", "union", "territory", "territories", "national",
    "central", "regional", "hon", "ble", "honble", "honourable", "honorable",
    "shri", "smt", "dr", "mr", "ms", "mrs", "secretary", "chairman",
    "chairperson", "president", "convenor", "convener", "member", "members",
    "officer", "general", "manager", "director", "act", "section", "article",
    "annexure", "law", "india", "indian", "petitioner", "respondent",
    "complainant", "accused", "applicant", "appellant", "plaintiff",
    "defendant", "trust", "limited", "ltd", "pvt", "private", "company",
    "corporation", "authority's", "writ", "petition", "case", "matter",
})

# A capitalised head word followed by 1+ more capitalised words (or the
# lowercase connectors of/and/for/the/&/at), e.g. "Bihar Cricket Association",
# "High Court of Judicature at Patna". Single capitalised words are never
# flagged (too noisy / too risky).
_PROPER_NOUN_CANDIDATE_RE = re.compile(
    r"\b[A-Z][A-Za-z&./'-]+(?:\s+(?:of|and|for|the|&|at|[A-Z][A-Za-z&./'-]+)){1,6}"
)
_GROUNDING_WORD_RE = re.compile(r"[a-z0-9]+")


def _grounding_token_set(*texts: str) -> set:
    """Lowercase word-token set built from the allowed source texts."""
    tokens: set = set()
    for t in texts:
        if t:
            tokens.update(_GROUNDING_WORD_RE.findall(t.lower()))
    return tokens


def find_ungrounded_proper_nouns(
    text: Any,
    allowed_texts: Optional[List[str]] = None,
    *,
    max_results: int = 12,
) -> List[str]:
    """Return distinct multi-word proper-noun phrases in ``text`` that name an
    entity / organisation / place absent from every allowed source text
    (documents, extracted entities, the user's own instruction, case summaries).

    Mirrors the document-grounding test used by ``snap_hallucinated_year``: a
    phrase is flagged only when, after dropping generic legal / forum /
    connector words, it still carries a distinctive token that appears in NONE
    of the allowed texts. Conservative by design (multi-word phrases and
    distinctive tokens only) so legitimate forum names and user-chosen venues
    are not flagged. Used to TRIGGER a corrective re-prompt, not to delete text.
    """
    if not text or not isinstance(text, str):
        return []
    allowed = _grounding_token_set(*(allowed_texts or []))
    if not allowed:
        return []
    flagged: List[str] = []
    seen: set = set()
    for m in _PROPER_NOUN_CANDIDATE_RE.finditer(text):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip(" .,;:'\"-")
        key = phrase.lower()
        if key in seen or len(phrase) < 4:
            continue
        distinctive = [
            w for w in _GROUNDING_WORD_RE.findall(key)
            if w not in _GROUNDING_STOPWORDS and len(w) > 2
        ]
        if not distinctive:
            continue
        # Flag when ANY distinctive token is absent from every allowed source.
        if any(w not in allowed for w in distinctive):
            seen.add(key)
            flagged.append(phrase)
        if len(flagged) >= max_results:
            break
    return flagged


#: Magnitude words that make even a one- or two-digit number a case fact
#: ("6 lakh", "18%"). Without these the significance filter below would let
#: "6 lakh" vs "16 lakh" — the exact confusion the user-fact ledger exists to
#: pin — slip through unnoticed.
_FIGURE_MAGNITUDE_WORDS = (
    "lakh", "lakhs", "crore", "crores", "thousand", "million", "billion",
    "percent", "per cent", "%", "hectare", "hectares", "acre", "acres",
)

#: A number as legal documents write it: digit groups with comma separators
#: (Indian or Western) and an optional decimal tail — 11,64,000 / 1,000.50 /
#: 0.263 / 2019.
_FIGURE_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _normalize_figure(raw: str) -> str:
    """Comparable form of a written figure: separators dropped, trailing
    zero-decimals dropped, leading zeros dropped. ``Rs. 11,64,000`` and
    ``1164000`` compare equal; ``0.263`` keeps its decimal."""
    cleaned = (raw or "").replace(",", "").strip(" .")
    if not cleaned:
        return ""
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    if not cleaned:
        return "0"
    intpart, _, frac = cleaned.partition(".")
    intpart = intpart.lstrip("0") or "0"
    return f"{intpart}.{frac}" if frac else intpart


def _significant_figures(text: str) -> Dict[str, str]:
    """Map normalized figure -> as-written form, for the figures in ``text``
    that carry case meaning.

    Significant means: at least three digits (amounts, years, suit numbers,
    identifiers like Arazi No. 262) OR immediately followed by a magnitude
    word. One- and two-digit numbers are otherwise skipped — "3 defendants"
    or a day-of-month re-worded from ``03/04/2019`` to ``3 April 2019`` are
    re-wordings, not dropped facts, and flagging them would veto every
    legitimate re-frame.
    """
    found: Dict[str, str] = {}
    if not text:
        return found
    lowered = text.lower()
    for m in _FIGURE_TOKEN_RE.finditer(text):
        raw = m.group(0).strip(" .,")
        digits = raw.replace(",", "").replace(".", "")
        if not digits:
            continue
        tail = lowered[m.end():m.end() + 12].lstrip(" \t")
        significant = len(digits) >= 3 or any(
            tail.startswith(w) for w in _FIGURE_MAGNITUDE_WORDS
        )
        if not significant:
            continue
        key = _normalize_figure(raw)
        if key:
            found.setdefault(key, raw)
    return found


def find_dropped_case_figures(
    previous: str,
    candidate: str,
    *,
    max_results: int = 12,
) -> List[str]:
    """Return the significant figures present in ``previous`` but missing from
    ``candidate``.

    The counterpart to :func:`find_ungrounded_proper_nouns`: that one catches
    facts the model INVENTED, this one catches facts it DROPPED. Used when a
    panel is re-framed rather than regenerated — a re-frame may re-order and
    re-emphasise, but every amount, year, suit number and identifier the
    previous text asserted must still be there. Comparison is on the
    normalized form, so re-notation (``Rs. 11,64,000`` → ``1164000``) does not
    count as a loss.
    """
    if not previous or not isinstance(previous, str):
        return []
    if not isinstance(candidate, str):
        candidate = ""
    kept = set(_significant_figures(candidate))
    dropped: List[str] = []
    for key, as_written in _significant_figures(previous).items():
        if key not in kept:
            dropped.append(as_written)
        if len(dropped) >= max_results:
            break
    return dropped


def _strip_strength_point_statute_boilerplate(text: str) -> str:
    """
    Remove trailing citation boilerplate from analysis strength points (statutes belong in `laws`, not prose).
    Strips phrases like 'which is a relevant fact under …' and 'admissible under …'.

    Guard: if stripping would collapse the point below ``_STRENGTH_POINT_MIN_LEN``
    characters (i.e. the model produced a label + boilerplate single-sentence form
    like "Youth of the accused, which is a relevant fact under <Act>, YYYY."), we
    return the ORIGINAL prose instead. The universal law-strip in ``app/langgraph/output_filters.py`` will
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


_UPLOAD_DETERMINERS = r"(the|this|that|these|those|each|every|both|all|said|uploaded|attached|enclosed|annexed|submitted)"


def _upload_wording(text: str) -> str:
    """Refer to uploaded files as annexures — and nothing else.

    Product rule: an upload is "Annexure N", never "Document N". Converted:
    "Document N" / "Documents N", and "document(s)" when it names an upload
    ("the document", "this document", "the uploaded documents").

    Deliberately NOT converted, because it is not an upload:
    "identity documents including passports" (real-world documents) and the
    verb in "Annexure 1 documents an official representation". The previous
    blanket replace produced "statutory identity annexures" and "Annexure 1
    annexures an official representation".
    """
    if not (text or "").strip():
        return text or ""
    t = re.sub(r"\b[Dd]ocuments\s+(\d+)\b", r"Annexures \1", text)
    t = re.sub(r"\b[Dd]ocument\s+(\d+)\b", r"Annexure \1", t)

    def _keep_case(word: str, repl: str) -> str:
        return repl.capitalize() if word[:1].isupper() else repl

    t = re.sub(
        rf"\b{_UPLOAD_DETERMINERS}\s+(documents)\b",
        lambda m: f"{m.group(1)} {_keep_case(m.group(2), 'annexures')}",
        t, flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"\b{_UPLOAD_DETERMINERS}\s+(document)\b(?!\s+(?:dated|titled|of\s+title))",
        lambda m: f"{m.group(1)} {_keep_case(m.group(2), 'annexure')}",
        t, flags=re.IGNORECASE,
    )
    return t


def _annexure_wording_for_next_steps(text: str) -> str:
    """Product wording for strategy possibleNextSteps (see ``_upload_wording``)."""
    if not (text or "").strip():
        return ""
    return _upload_wording(text.strip())

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

    # Standard-form instruments (terms of service, privacy statements,
    # acceptable-use policies) are uploaded for reference, but every person and
    # company they name belongs to the DOCUMENT, not the dispute: its publisher,
    # its version-control signatories, its third-party vendors, and role
    # placeholders like "the Client" / "the Service Provider". Read as evidence,
    # a boilerplate template's own Service Provider outranked the actual client
    # in party selection, and a policy's approval table was reported as "key
    # individuals involved in the communications". Label them so every prompt
    # treats them as cited instruments instead.
    try:
        from app.processing.document_classifier import is_policy_boilerplate

        _file_name = str(
            metadata.get("file_name")
            or metadata.get("filename")
            or doc.get("file_name")
            or ""
        )
        if is_policy_boilerplate(_file_name, body, document_type):
            header_lines.append(
                "[CITED INSTRUMENT — standard-form policy/terms uploaded for "
                "reference. Its clauses MAY be cited and the CONTRACTING ENTITY "
                "it is made by or for may well be a party to this matter (a "
                "party often uploads its own standard agreement). But the "
                "INDIVIDUALS it names — document-control authors, owners, "
                "reviewers, 'Approved By' signatories — and the third-party "
                "vendors, analytics providers and regulators it mentions are "
                "part of the DOCUMENT, not the dispute: never describe them as "
                "case participants. Take the FACTS of the dispute (what "
                "happened, when, how much, who did what) from the other "
                "annexures, not from this one.]"
            )
    except Exception:  # pragma: no cover - labelling must never block a prompt
        pass

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


def drafting_payload_to_wire(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize the internal drafting-suggestions payload to the wire contract.

    The drafting agent works with a richer internal card shape (``matchedDrafts``
    with ``matchScore``/``matchReason`` plus mirrored top-level ``templateId`` /
    ``matchMode`` fields) which the ranking, freeze, and reconnect-cache logic
    depend on. The backend/frontend contract is narrower: each suggested draft
    carries only ``{title, relevance, description, matches[]}`` where every match
    is ``{templateId, templateName, category, matchPercent}`` and ``matches`` is
    ``[]`` when nothing matched.

    This transform runs only at the emit boundary — the in-memory cache keeps the
    internal shape. Pure and defensive: unknown/non-dict entries are skipped and
    match entries without a ``templateId`` are dropped.
    """
    out_cards: List[Dict[str, Any]] = []
    for card in (payload or {}).get("suggestedDrafts") or []:
        if not isinstance(card, dict):
            continue

        raw_matches = card.get("matchedDrafts")
        if isinstance(raw_matches, list) and raw_matches:
            source_matches = raw_matches
        elif (card.get("templateId") or "").strip():
            # Legacy single-template card (no matchedDrafts list): synthesize a
            # one-item matches array from the top-level template fields.
            source_matches = [
                {
                    "templateId": card.get("templateId"),
                    "templateName": card.get("templateName"),
                    "category": card.get("category"),
                    "matchScore": card.get("matchScore"),
                }
            ]
        else:
            source_matches = []

        matches: List[Dict[str, Any]] = []
        for md in source_matches:
            if not isinstance(md, dict):
                continue
            tid = (md.get("templateId") or "").strip()
            if not tid:
                continue
            matches.append(
                {
                    "templateId": tid,
                    "templateName": md.get("templateName"),
                    "category": md.get("category"),
                    "matchPercent": md.get("matchScore"),
                }
            )

        out_cards.append(
            {
                "title": card.get("title"),
                "relevance": card.get("relevance") or "Medium",
                "description": card.get("description"),
                "matches": matches,
            }
        )

    return {"suggestedDrafts": out_cards}
