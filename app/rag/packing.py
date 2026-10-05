"""
Fit a document into a character budget without throwing away its end.

Every panel generator used to share a fixed budget between documents and cut
each one from the front (``content[:per_doc_cap]``). With 20 documents a
40-page pleading kept only its first ~5,000 characters, so the prayer, the
operative order, the annexure list and every late date were never seen — and
nothing in the output said so.

:func:`fit_to_budget` keeps, in document order:

* the opening (parties, title, the first facts),
* the closing (prayer, orders, signatures, dates of execution), and
* in between, the paragraphs that matter most for the panel being built,
  scored against that panel's profile (dates for a chronology, statutory
  references for applicable laws, and so on),

marking every cut with ``[… omitted …]`` so the model knows the text is
partial. The ``--- Page N ---`` marker governing each kept paragraph is carried
with it, so page resolution downstream still works.

It is deterministic and cheap: pure text scoring, no model or vector calls.
"""

import re
from typing import Dict, List, Pattern, Tuple

from app.core.config import settings

_PAGE_MARKER_RE = re.compile(r"^---\s*Page\s+\d+\s*---\s*$", re.I)
OMISSION = "[… omitted …]"
#: The label every spreadsheet row block opens with (app/processing/tables.py).
_SHEET_BLOCK_RE = re.compile(r"^Sheet: (.+?)(?: \(hidden\))? \(rows? (\d+)(?:-(\d+))?\)$")

_DATE = (
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b(?:19|20)\d{2}\b"
)
_LAW = (
    r"\b(?:section|sec\.|s\.|u/s|article|rule|order|clause)\s*\d+[a-z]?\b"
    r"|\b(?:bns|bnss|bsa|ipc|crpc|cpc|it act|act,? \d{4})\b"
)
_PARTY = (
    r"\b(?:petitioner|respondent|appellant|complainant|accused|plaintiff|defendant"
    r"|applicant|opposite party|vendor|vendee|landlord|tenant|employer|employee)\b"
    r"|\b(?:shri|smt|mr|mrs|ms|m/s)\.?\s+[a-z]"
)
_MONEY = r"(?:₹|rs\.?|inr)\s*[\d,]+|\b\d{1,3}(?:,\d{2,3})+\b|\blakh|\bcrore"
_CORE = (
    r"\b(?:prayer|relief|allegation|alleged|admitted|denied|breach|default|notice"
    r"|order|judgment|decree|directed|evidence|affidavit|agreement|contract"
    r"|payment|paid|refund|compensation|damages|hearing|adjourn|filed|served"
    r"|issued|dated|executed|signed|summons|warrant|bail|fir|chargesheet)\b"
)

#: Which signals matter for which panel. Weights are relative.
PROFILES: Dict[str, List[Tuple[Pattern, float]]] = {
    "dates": [(re.compile(_DATE, re.I), 3.0), (re.compile(_CORE, re.I), 1.0)],
    "laws": [(re.compile(_LAW, re.I), 3.0), (re.compile(_CORE, re.I), 1.0)],
    "parties": [(re.compile(_PARTY, re.I), 3.0), (re.compile(_CORE, re.I), 0.5)],
    "general": [
        (re.compile(_CORE, re.I), 1.5),
        (re.compile(_MONEY, re.I), 2.0),
        (re.compile(_DATE, re.I), 1.0),
        (re.compile(_LAW, re.I), 1.0),
        (re.compile(_PARTY, re.I), 0.5),
    ],
}

#: Share of the budget reserved for the opening and closing of a document.
_HEAD_SHARE = 0.30
_TAIL_SHARE = 0.15


def panel_context_budget() -> int:
    """Total characters of document text a panel prompt may carry."""
    return int(getattr(settings, "panel_context_chars", 200_000) or 200_000)


def _paragraphs(text: str) -> List[Tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer(r"\n[ \t]*\n", text):
        if text[start : m.start()].strip():
            spans.append((start, m.start()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _score(text: str, profile: str) -> float:
    rules = PROFILES.get(profile) or PROFILES["general"]
    words = max(1, len(text.split()))
    raw = sum(weight * len(pattern.findall(text)) for pattern, weight in rules)
    # Density, lightly favouring substantive paragraphs over one-liners.
    return raw / (words ** 0.5)


def _page_marker_before(text: str, offset: int) -> str:
    idx = text.rfind("--- Page", 0, offset + 1)
    if idx == -1:
        return ""
    line_end = text.find("\n", idx)
    line = text[idx : line_end if line_end != -1 else len(text)].strip()
    return line if _PAGE_MARKER_RE.match(line) else ""


def _omission_marker(content: str, paras: List[Tuple[int, int]], cut: range) -> str:
    """The cut marker. For spreadsheet blocks it names the rows left out, and
    points at the workbook summary, whose figures cover every row, so a panel
    never presents a total over the rows it happened to keep."""
    ranges: List[List] = []
    for i in cut:
        a, b = paras[i]
        first_line = content[a:b].lstrip().split("\n", 1)[0]
        m = _SHEET_BLOCK_RE.match(first_line)
        if not m:
            return OMISSION
        sheet, lo = m.group(1), int(m.group(2))
        hi = int(m.group(3) or lo)
        if ranges and ranges[-1][0] == sheet:
            ranges[-1][2] = hi
        else:
            ranges.append([sheet, lo, hi])
    if not ranges:
        return OMISSION
    spans = "; ".join(
        f'sheet "{sh}" row {lo}' if lo == hi else f'sheet "{sh}" rows {lo}-{hi}'
        for sh, lo, hi in ranges
    )
    return (
        f"[… omitted: {spans}. Counts and totals over every row are in the "
        "workbook summary at the top of this document …]"
    )


def fit_to_budget(content: str, budget: int, profile: str = "general") -> str:
    """Return ``content`` unchanged if it fits, else a faithful excerpt.

    The excerpt is the opening, the closing and the best-scoring middle
    paragraphs for ``profile``, in document order, with each cut marked.
    """
    content = content or ""
    if budget <= 0 or len(content) <= budget:
        return content
    paras = _paragraphs(content)
    if len(paras) < 3:
        head = int(budget * 0.7)
        return f"{content[:head]}\n{OMISSION}\n{content[-(budget - head):]}"

    head_budget = int(budget * _HEAD_SHARE)
    tail_budget = int(budget * _TAIL_SHARE)
    keep = set()
    used = 0
    for i, (a, b) in enumerate(paras):
        if used + (b - a) > head_budget and keep:
            break
        keep.add(i)
        used += b - a
    tail_used = 0
    for i in range(len(paras) - 1, -1, -1):
        a, b = paras[i]
        if i in keep or tail_used + (b - a) > tail_budget:
            break
        keep.add(i)
        tail_used += b - a
    used += tail_used

    middle = sorted(
        (i for i in range(len(paras)) if i not in keep),
        key=lambda i: _score(content[paras[i][0] : paras[i][1]], profile),
        reverse=True,
    )
    for i in middle:
        a, b = paras[i]
        cost = (b - a) + 40  # room for a page marker / omission line
        if used + cost > budget:
            continue
        keep.add(i)
        used += cost

    out: List[str] = []
    prev = -1
    for i in sorted(keep):
        a, b = paras[i]
        piece = content[a:b].strip()
        if prev != -1 and i != prev + 1:
            out.append(_omission_marker(content, paras, range(prev + 1, i)))
            marker = _page_marker_before(content, a)
            if marker and not piece.startswith("--- Page"):
                out.append(marker)
        out.append(piece)
        prev = i
    if prev != len(paras) - 1:
        out.append(_omission_marker(content, paras, range(prev + 1, len(paras))))
    return "\n\n".join(out)
