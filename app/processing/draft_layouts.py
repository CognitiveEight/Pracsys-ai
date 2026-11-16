"""Standard layout per document type for drafts without a template (SIM-208).

A legal notice, a court filing, an affidavit, a vakalatnama and an agreement
each have their own conventional look. When no template is used, the Word
file takes the layout of its document family:

- page and margins, body font size, line spacing and alignment;
- the **title block** — everything before the body starts (the salutation,
  "MOST RESPECTFULLY SHOWETH", "This Agreement is made ...", the first
  numbered paragraph) — has its headings and short capitalised lines
  centred: court name, case number, "VERSUS", the document title;
- "VERSUS" / "V/S" lines are centred wherever they are.

The values are conventional defaults, not any court's filing rules; a
lawyer's own template (SIM-203) always wins over them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

LAYOUT_NOTICE = "notice"
LAYOUT_COURT = "court_filing"
LAYOUT_AFFIDAVIT = "affidavit"
LAYOUT_VAKALATNAMA = "vakalatnama"
LAYOUT_AGREEMENT = "agreement"
LAYOUT_DEFAULT = "default"


@dataclass(frozen=True)
class LayoutProfile:
    name: str
    margin_left_in: float = 1.0
    margin_right_in: float = 1.0
    margin_top_in: float = 1.0
    margin_bottom_in: float = 1.0
    body_size: float = 12.0
    line_spacing: float = 1.5
    justify: bool = True
    centre_title_block: bool = True
    page_numbers: bool = True


PROFILES = {
    LAYOUT_NOTICE: LayoutProfile(LAYOUT_NOTICE, line_spacing=1.15),
    LAYOUT_COURT: LayoutProfile(LAYOUT_COURT, margin_left_in=1.5, body_size=14.0,
                                line_spacing=1.5),
    LAYOUT_AFFIDAVIT: LayoutProfile(LAYOUT_AFFIDAVIT, margin_left_in=1.25, body_size=13.0,
                                    line_spacing=1.5),
    LAYOUT_VAKALATNAMA: LayoutProfile(LAYOUT_VAKALATNAMA, line_spacing=1.15),
    LAYOUT_AGREEMENT: LayoutProfile(LAYOUT_AGREEMENT, line_spacing=1.15),
    LAYOUT_DEFAULT: LayoutProfile(LAYOUT_DEFAULT, centre_title_block=False),
}

# Order matters: the first family whose words appear in the title wins.
_FAMILIES = (
    (LAYOUT_VAKALATNAMA, r"vakalat"),
    (LAYOUT_AFFIDAVIT, r"affidavit|verification\b"),
    (LAYOUT_NOTICE, r"\bnotice\b|reply to (the )?notice|\bletter\b|representation|"
                    r"\bfir\b|police complaint|complaint to (the )?(police|sho|ssp)"),
    (LAYOUT_COURT, r"petition|plaint|written statement|application|\bbail\b|appeal|"
                   r"\bsuit\b|revision|complaint under|memo of parties|rejoinder|"
                   r"\breply\b|vakil|caveat|interim"),
    (LAYOUT_AGREEMENT, r"agreement|\bdeed\b|lease|licen[cs]e|\bwill\b|testament|codicil|"
                       r"\bmou\b|memorandum of understanding|power of attorney|\bpoa\b|"
                       r"undertaking|indemnity|\bbond\b|addendum|contract|declaration|"
                       r"settlement|partnership|\brent(al)?\b"),
)


def infer_layout(title: str, document_type: Optional[str] = None) -> LayoutProfile:
    text = " ".join(t for t in (title or "", document_type or "") if t).lower()
    for name, pattern in _FAMILIES:
        if re.search(pattern, text):
            return PROFILES[name]
    return PROFILES[LAYOUT_DEFAULT]


# The first line that starts the body: everything above it is title block.
_BODY_START_RE = re.compile(
    r"^\s*(dear\b|sir\b|madam\b|respected\b|to,?\s*$|most respectfully|the humble|"
    r"the (petitioner|applicant|plaintiff|appellant|complainant|deponent)s?\b|"
    r"i,\s|we,\s|this\s+(agreement|deed|will|lease|memorandum|power|undertaking|indemnity|"
    r"addendum|contract|declaration)\b|know all|whereas|\d{1,3}[.)]\s)",
    re.IGNORECASE,
)
_VERSUS_RE = re.compile(r"^\s*(versus|vs\.?|v/s\.?|v\.)\s*$", re.IGNORECASE)


def starts_body(text: str) -> bool:
    return bool(_BODY_START_RE.match(text or ""))


def is_versus(text: str) -> bool:
    return bool(_VERSUS_RE.match(text or ""))


_PARTY_LINE_RE = re.compile(
    r"(\.{2,}|…|-{2,})?\s*(petitioner|applicant|respondent|plaintiff|defendant|appellant|"
    r"accused|complainant|opposite party|claimant|caveator)s?\s*(no\.?\s*\d+)?\s*$",
    re.IGNORECASE,
)


def is_title_line(text: str) -> bool:
    """A short, mostly-capitalised line: court name, case number, title.

    A cause-title party line ("RAVI SAMPLE ... APPLICANT") is not one: it is
    set left with its role at the end.
    """
    t = (text or "").strip()
    if not t or len(t) > 110 or "\n" in t or _PARTY_LINE_RE.search(t):
        return False
    letters = [c for c in t if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= 0.8 * len(letters)
