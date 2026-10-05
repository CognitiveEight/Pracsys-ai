"""
PII detection and masking for Indian government documents.

Per project policy (CLAUDE.md, sensitive files / PII), full identifier
numbers must NOT land in Loguru logs, Qdrant chunks, Gemini prompts, or
chat context. This module extracts identifier patterns from raw extracted
text, returns the full values for sealed downstream storage, and produces
a redacted copy of the text for everywhere else.

Masked identifier types (seven total):
    aadhaar_number, pan_number, udyam_number, passport_number,
    voter_id_number, driving_licence_number, bank_account_number.

Deliberately NOT masked:
    - GSTIN — public business identifier (open lookup on the GST portal).
    - Mobile numbers — routinely needed as party contact information in
      Indian legal filings (FIRs, complaints, notices).
    - IFSC codes — public bank-branch identifier; used only as a cue for
      bank-account detection.
    - Names, dates of birth, addresses — kept in clear because legal
      reasoning depends on them. They flow through the LLM extraction
      step (``document_classifier``) and the masked sensitiveFields
      block does not include them.

Masking convention (user-facing): keep the last four characters, mask
the rest with X. For PAN we keep the last digit + check letter so the
masked form remains visually distinguishable from Aadhaar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Identifier patterns
# ---------------------------------------------------------------------------
# Each pattern is anchored on word boundaries to reduce false positives.
# We deliberately keep these conservative: a 12-digit run inside a longer
# numeric blob (e.g. an account number) should not be matched as Aadhaar.

# Aadhaar: 12 digits, optionally separated by space or hyphen into 4-4-4 groups.
# We require the surrounding context to NOT be a longer digit run (handled in
# extract_aadhaar via a post-filter on lookaround-equivalent boundaries).
_AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")

# PAN: 5 letters + 4 digits + 1 letter (Income Tax Department format).
_PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")

# Udyam (MSME): UDYAM-XX-00-0000000 — 2 state letters, 2 digits, 7 digits.
_UDYAM_RE = re.compile(r"\b(UDYAM-[A-Z]{2}-\d{2}-\d{7})\b")

# Indian passport: 1 letter + 7 digits (current series). Older series exist
# but this covers the vast majority and avoids over-matching short codes.
_PASSPORT_RE = re.compile(r"\b([A-PR-WY][0-9]{7})\b")

# Voter ID (EPIC): 3 letters + 7 digits.
_EPIC_RE = re.compile(r"\b([A-Z]{3}[0-9]{7})\b")

# Driving licence: state code + RTO + year + 7-digit serial. Real DLs use
# several formats; this catches the most common SS-RR-YYYY-NNNNNNN style with
# optional separators.
_DL_RE = re.compile(r"\b([A-Z]{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{7})\b")

# IFSC (bank): 4 letters + '0' + 6 alphanumeric. Public information, but
# helpful for classification of bank documents — not masked.
_IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")

# Bank account number: 9–18 digit run that is NOT preceded/followed by more
# digits. We extract these only when an IFSC or "A/c"/"Account" cue is nearby
# (handled in extract_bank_accounts) to keep precision high.
_BANK_ACCT_RE = re.compile(r"(?<!\d)(\d{9,18})(?!\d)")
_BANK_CUE_RE = re.compile(
    r"\b(a/c\s*(?:no\.?|number)?|account\s*(?:no\.?|number)?|acct\.?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Masking helpers
# ---------------------------------------------------------------------------


def _mask_keep_last(value: str, keep: int = 4) -> str:
    """Mask all chars except the last ``keep``; preserves length and separators
    in spirit (we collapse separators on the masked half)."""
    digits_only = re.sub(r"[\s-]", "", value)
    if len(digits_only) <= keep:
        return "X" * len(digits_only)
    return ("X" * (len(digits_only) - keep)) + digits_only[-keep:]


def mask_aadhaar(raw: str) -> str:
    digits = re.sub(r"[\s-]", "", raw)
    if len(digits) != 12:
        return _mask_keep_last(digits, 4)
    return f"XXXX-XXXX-{digits[-4:]}"


def mask_pan(raw: str) -> str:
    if len(raw) != 10:
        return _mask_keep_last(raw, 4)
    return f"{raw[:3]}XX{raw[5:9]}X"


def mask_passport(raw: str) -> str:
    if len(raw) != 8:
        return _mask_keep_last(raw, 4)
    return f"{raw[0]}XXX{raw[-4:]}"


def mask_epic(raw: str) -> str:
    if len(raw) != 10:
        return _mask_keep_last(raw, 4)
    return f"{raw[:3]}XXX{raw[-4:]}"


def mask_dl(raw: str) -> str:
    cleaned = re.sub(r"[\s-]", "", raw)
    return _mask_keep_last(cleaned, 4)


def mask_udyam(raw: str) -> str:
    # UDYAM-XX-00-0000000 → UDYAM-XX-00-XXX1234
    parts = raw.split("-")
    if len(parts) != 4:
        return _mask_keep_last(raw, 4)
    return f"{parts[0]}-{parts[1]}-{parts[2]}-XXX{parts[3][-4:]}"


def mask_bank_account(raw: str) -> str:
    return _mask_keep_last(raw, 4)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PIIMatch:
    """A single detected identifier."""

    field_type: str  # e.g. "aadhaar_number", "pan_number"
    raw: str  # full original value as it appears in text
    masked: str  # masked form for display / vector DB
    span: Tuple[int, int]  # (start, end) in source text


@dataclass
class PIIScanResult:
    """Output of ``scan_text``: full values, masked text, and helper views."""

    redacted_text: str
    matches: List[PIIMatch] = field(default_factory=list)

    def sensitive_fields(self) -> Dict[str, List[str]]:
        """Map of field_type → list of full raw values. Use for sealed
        backend storage only — never log or vector-store this."""
        out: Dict[str, List[str]] = {}
        for m in self.matches:
            out.setdefault(m.field_type, []).append(m.raw)
        return out

    def masked_fields(self) -> Dict[str, List[str]]:
        """Map of field_type → list of masked values. Safe for display."""
        out: Dict[str, List[str]] = {}
        for m in self.matches:
            out.setdefault(m.field_type, []).append(m.masked)
        return out


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def _extract_simple(
    text: str, pattern: re.Pattern, field_type: str, masker
) -> List[PIIMatch]:
    matches: List[PIIMatch] = []
    seen: set = set()
    for m in pattern.finditer(text):
        raw = m.group(0)
        if raw in seen:
            # Still need to redact every occurrence; treat dedup as
            # "don't double-count in sensitive_fields".
            matches.append(
                PIIMatch(field_type, raw, masker(raw), (m.start(), m.end()))
            )
            continue
        seen.add(raw)
        matches.append(
            PIIMatch(field_type, raw, masker(raw), (m.start(), m.end()))
        )
    return matches


def extract_pan(text: str) -> List[PIIMatch]:
    return _extract_simple(text, _PAN_RE, "pan_number", mask_pan)


def extract_udyam(text: str) -> List[PIIMatch]:
    return _extract_simple(text, _UDYAM_RE, "udyam_number", mask_udyam)


def extract_passport(text: str) -> List[PIIMatch]:
    return _extract_simple(text, _PASSPORT_RE, "passport_number", mask_passport)


def extract_epic(text: str) -> List[PIIMatch]:
    return _extract_simple(text, _EPIC_RE, "voter_id_number", mask_epic)


def extract_dl(text: str) -> List[PIIMatch]:
    return _extract_simple(text, _DL_RE, "driving_licence_number", mask_dl)


def _looks_like_real_aadhaar(digits: str) -> bool:
    """Reject obvious non-Aadhaar 12-digit runs even if Verhoeff happens
    to pass. UIDAI does not issue numbers starting with 0 or 1, all-same-
    digit numbers, or pure sequential patterns."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    if digits[0] in ("0", "1"):
        return False
    if len(set(digits)) == 1:  # 222222222222
        return False
    if digits in {"123456789012", "987654321098"}:
        return False
    return True


def extract_aadhaar(text: str, allow_checksum_only: bool = True) -> List[PIIMatch]:
    """Aadhaar-aware extraction with cue-based filtering.

    A 12-digit run is only treated as Aadhaar when EITHER of:
      (a) the surrounding text contains an Aadhaar cue word
          (aadhaar / aadhar / uidai / uid), OR
      (b) the digits pass both the Verhoeff checksum AND the
          ``_looks_like_real_aadhaar`` shape filter.

    This balance avoids masking long account numbers and transaction IDs
    while still catching Aadhaar numbers in OCR output where the header
    word may be mis-recognised.

    ``allow_checksum_only=False`` drops (b): a cue is then required. About
    one random 12-digit number in ten passes Verhoeff, so in a spreadsheet,
    where a column of transaction or roll numbers sits under its own header,
    (b) alone would mask a tenth of them at random.
    """
    matches: List[PIIMatch] = []
    seen: set = set()
    cue_re = re.compile(r"\b(aadhaar|aadhar|uidai|uid)\b", re.IGNORECASE)
    for m in _AADHAAR_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[\s-]", "", raw)
        window_start = max(0, m.start() - 80)
        window_end = min(len(text), m.end() + 40)
        window = text[window_start:window_end]
        has_cue = bool(cue_re.search(window))
        passes_shape = (
            allow_checksum_only
            and _looks_like_real_aadhaar(digits)
            and _verhoeff_valid(digits)
        )
        if not (has_cue or passes_shape):
            continue
        if digits in seen:
            matches.append(
                PIIMatch(
                    "aadhaar_number", digits, mask_aadhaar(digits),
                    (m.start(), m.end()),
                )
            )
            continue
        seen.add(digits)
        matches.append(
            PIIMatch(
                "aadhaar_number", digits, mask_aadhaar(digits),
                (m.start(), m.end()),
            )
        )
    return matches


def extract_bank_accounts(text: str) -> List[PIIMatch]:
    """Bank account numbers — only matched when an account cue word or an
    IFSC code is found in the surrounding 80-char window."""
    matches: List[PIIMatch] = []
    seen: set = set()
    for m in _BANK_ACCT_RE.finditer(text):
        raw = m.group(0)
        # Skip 12-digit runs (handled by Aadhaar extractor) and very short.
        if len(raw) == 12:
            continue
        window_start = max(0, m.start() - 80)
        window_end = min(len(text), m.end() + 40)
        window = text[window_start:window_end]
        has_cue = bool(_BANK_CUE_RE.search(window) or _IFSC_RE.search(window))
        if not has_cue:
            continue
        if raw in seen:
            matches.append(
                PIIMatch(
                    "bank_account_number", raw, mask_bank_account(raw),
                    (m.start(), m.end()),
                )
            )
            continue
        seen.add(raw)
        matches.append(
            PIIMatch(
                "bank_account_number", raw, mask_bank_account(raw),
                (m.start(), m.end()),
            )
        )
    return matches


# ---------------------------------------------------------------------------
# Verhoeff checksum (used by Aadhaar)
# ---------------------------------------------------------------------------

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff_valid(num: str) -> bool:
    if not num.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


# ---------------------------------------------------------------------------
# Public scan + redact API
# ---------------------------------------------------------------------------


_EXTRACTORS = (
    extract_aadhaar,  # before bank accounts so 12-digit runs are claimed
    extract_pan,
    extract_udyam,
    extract_passport,
    extract_epic,
    extract_dl,
    extract_bank_accounts,
)

#: Under ``strict_context``, the shape-only patterns need a word naming them.
#: "A1234567" is a passport number in a KYC form and a part number in an
#: inventory sheet; only the context can tell.
_STRICT_CUES = {
    "passport_number": re.compile(r"\bpassport\b", re.I),
    "voter_id_number": re.compile(r"\b(epic|voter|elector(al)?)\b", re.I),
    "driving_licence_number": re.compile(r"\b(dl|driving|licen[cs]e)\b", re.I),
}
#: The fourth character of a real PAN is the holder type (P person, C company,
#: H HUF, F firm, A AOP, T trust, B BOI, L local authority, J juridical, G govt).
_PAN_HOLDER_TYPES = frozenset("PCHFATBLJG")


def scan_text(text: str, *, strict_context: bool = False) -> PIIScanResult:
    """Detect identifiers and produce a redacted copy of ``text``.

    Overlapping matches are resolved by preferring the earliest match;
    later matches that overlap an already-redacted span are dropped from
    the redaction pass but still recorded (so sensitive_fields is complete).

    ``strict_context`` is for short texts whose context is given explicitly:
    one spreadsheet cell prefixed with its column header
    (``app/processing/tables.py``). Each identifier then needs its cue in that
    text: an Aadhaar number no longer qualifies on checksum alone, passport /
    voter-ID / licence shapes need their own word, and a PAN must carry a real
    holder-type letter.
    """
    if not text:
        return PIIScanResult(redacted_text=text or "", matches=[])

    all_matches: List[PIIMatch] = []
    for extractor in _EXTRACTORS:
        if extractor is extract_aadhaar:
            found = extract_aadhaar(text, allow_checksum_only=not strict_context)
        else:
            found = extractor(text)
        if strict_context:
            found = [
                m
                for m in found
                if (cue := _STRICT_CUES.get(m.field_type)) is None or cue.search(text)
            ]
            found = [
                m
                for m in found
                if m.field_type != "pan_number" or m.raw[3:4] in _PAN_HOLDER_TYPES
            ]
        all_matches.extend(found)

    if not all_matches:
        return PIIScanResult(redacted_text=text, matches=[])

    # Build redacted text by walking matches in order and replacing spans.
    # Resolve overlap: sort by (start, -length) so the longest earliest-starting
    # match wins.
    sorted_matches = sorted(
        all_matches, key=lambda m: (m.span[0], -(m.span[1] - m.span[0]))
    )
    redacted_parts: List[str] = []
    cursor = 0
    used_spans: List[Tuple[int, int]] = []
    for m in sorted_matches:
        start, end = m.span
        if start < cursor:
            # Overlaps a span we've already redacted; skip the redaction
            # but keep the match record.
            continue
        redacted_parts.append(text[cursor:start])
        redacted_parts.append(m.masked)
        used_spans.append((start, end))
        cursor = end
    redacted_parts.append(text[cursor:])
    redacted = "".join(redacted_parts)

    return PIIScanResult(redacted_text=redacted, matches=all_matches)
