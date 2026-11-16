"""Which pages of a stored document to cite only as a last resort (SIM-192).

A court paper-book opens with pages that restate the whole case:

* the INDEX: one row per annexure, its title, its date and its page range,
  often under the cause title with every party's name;
* the advocate's LIST OF DATES and SYNOPSIS: every date and event of the case,
  in summary.

Anything that finds "the first page that mentions X" lands there, because
those pages mention everything and come first. Citations should point at the
evidence (the petition, the annexure), so callers skip ``avoid_pages`` unless
nothing else supports the item.

Detection works on the stored text with its ``--- Page N ---`` markers:

* an index starts on a page with a standalone INDEX / TABLE OF CONTENTS /
  CONTENTS / LIST OF DOCUMENTS heading (or its Hindi form) near the top that
  also reads like a list of pages, and continues while the following pages
  are still a list of page ranges and numbered rows;
* a summary starts on a page with a standalone LIST OF DATES / SYNOPSIS
  heading near the top, and continues until a page opens a new part of the
  paper-book (a court heading, an annexure, another heading) or a few pages
  have passed.

A heading may be split over two lines by the PDF ("LIST OF" / "DATES").
Conservative on purpose: a page is only skipped when it looks like one of
these, so other documents cite exactly as before.

Also here: ``resolve_entry_page``, which places a panel entry on the page that
carries its date or quote, and ``date_forms``, the ways a date is written.
"""

import hashlib
import re
from typing import Callable, Collection, Dict, FrozenSet, Iterable, List, Optional, Tuple

from app.rag.chunker import PAGE_MARKER_RE

_INDEX_HEADING_RE = re.compile(
    r"^\W*(?:index|table\s+of\s+contents|contents|list\s+of\s+documents|index\s+of\s+documents"
    r"|अनुक्रमणिका|विषय\s*सूची|सूची)\W*$",
    re.IGNORECASE,
)
_SUMMARY_HEADING_RE = re.compile(
    r"^\W*(?:(?:brief\s+)?synopsis(?:\s*(?:&|and)\s*list\s+of\s+dates(?:\s*(?:&|and)\s*events)?)?"
    r"|list\s+of\s+dates(?:\s*(?:&|and)\s*events)?|dates\s*(?:&|and)\s*events)\W*$",
    re.IGNORECASE,
)
#: A page that opens a new part of a paper-book: ends a summary run.
_NEW_PART_RE = re.compile(
    r"^\W*(?:in\s+the\s+(?:hon'?ble\s+)?(?:\w+\s+){0,4}(?:court|tribunal|commission|forum)"
    r"|before\s+the\b|annexure\b|memo\s+of\s+parties|affidavit|vakalatnama|index\b)",
    re.IGNORECASE,
)
#: "12-15", "45 – 50": a page range, but never part of a date such as
#: 12-03-2024 or 12/03/2024.
_PAGE_RANGE_RE = re.compile(r"(?<![\d/.\-–])\d{1,4}\s*[-–]\s*\d{1,4}(?![\d/.\-–])")
_NUMBERED_ROW_RE = re.compile(r"^\d{1,3}\s*[.)]?\s+[A-Za-z]")
_PAGE_COLUMN_RE = re.compile(r"\bpage\s*(?:no|nos|number)\b", re.IGNORECASE)

#: The heading must be among the first lines of the page.
_HEADING_WITHIN_LINES = 15
#: Only applied to pages right after a confirmed index heading page, so it can
#: be low: the tail of an index carries just a couple of ranges.
_MIN_RANGES = 2
_MIN_ROWS = 3
#: An index never runs this long; stops a run of tabular annexures (a ledger)
#: from being swallowed after a short index.
_MAX_CONTINUATION_PAGES = 10
#: A list of dates or synopsis runs a few pages; the petition follows it.
_MAX_SUMMARY_CONTINUATION_PAGES = 6


def split_pages(text: str) -> Dict[int, str]:
    """Page number -> that page's text, from ``--- Page N ---`` markers ({} if none)."""
    markers = list(PAGE_MARKER_RE.finditer(text or ""))
    pages: Dict[int, str] = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        pages[int(m.group(1))] = text[m.end():end]
    return pages


def _lines(page_text: str):
    return [ln.strip() for ln in (page_text or "").splitlines() if ln.strip()]


def _head_lines(page_text: str) -> List[str]:
    """The first lines of a page, plus each pair joined: a PDF often splits a
    heading ("LIST OF" / "DATES")."""
    lines = _lines(page_text)[:_HEADING_WITHIN_LINES]
    return lines + [f"{a} {b}" for a, b in zip(lines, lines[1:])]


def _has_heading(page_text: str, heading_re: "re.Pattern[str]") -> bool:
    return any(heading_re.match(ln) for ln in _head_lines(page_text))


def _ranges(page_text: str) -> int:
    return len(_PAGE_RANGE_RE.findall(page_text or ""))


def _numbered_rows(lines) -> int:
    return sum(1 for ln in lines if _NUMBERED_ROW_RE.match(ln))


def starts_index(page_text: str) -> bool:
    """A page with an INDEX-style heading near the top that lists pages."""
    if not _has_heading(page_text, _INDEX_HEADING_RE):
        return False
    return _ranges(page_text) >= 2 or bool(_PAGE_COLUMN_RE.search(page_text))


def continues_index(page_text: str) -> bool:
    """A page that is still a list of page ranges and numbered rows."""
    lines = _lines(page_text)
    return _ranges(page_text) >= _MIN_RANGES and _numbered_rows(lines) >= _MIN_ROWS


def starts_summary(page_text: str) -> bool:
    """A page with a LIST OF DATES / SYNOPSIS heading near the top."""
    return _has_heading(page_text, _SUMMARY_HEADING_RE)


def continues_summary(page_text: str) -> bool:
    """A page that does not open a new part of the paper-book."""
    head = _lines(page_text)[:3]
    return not any(_NEW_PART_RE.match(ln) for ln in head) and not _has_heading(
        page_text, _INDEX_HEADING_RE
    )


def _runs(
    pages: Dict[int, str],
    starts: Callable[[str], bool],
    continues: Callable[[str], bool],
    max_continuation: int,
) -> set:
    found = set()
    numbers = sorted(pages)
    i = 0
    while i < len(numbers):
        if not starts(pages[numbers[i]]):
            i += 1
            continue
        found.add(numbers[i])
        j = i + 1
        while (
            j < len(numbers)
            and j - i <= max_continuation
            and not starts(pages[numbers[j]])
            and continues(pages[numbers[j]])
        ):
            found.add(numbers[j])
            j += 1
        i = j
    return found


#: Results by a digest of the text, never the text itself: a deleted case's
#: documents must not stay in memory (the workspace purge cannot reach
#: module-level state). Holds only page numbers.
_CACHE: Dict[Tuple[str, str], FrozenSet[int]] = {}
_CACHE_MAX = 512


def _cached(kind: str, text: str, compute: Callable[[Dict[int, str]], set]) -> FrozenSet[int]:
    key = (kind, hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest())
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = found = frozenset(compute(split_pages(text)))
    return found


def index_pages(text: str) -> FrozenSet[int]:
    """Page numbers of every index in ``text`` (empty when it has none)."""
    return _cached(
        "index", text, lambda p: _runs(p, starts_index, continues_index, _MAX_CONTINUATION_PAGES)
    )


def summary_pages(text: str) -> FrozenSet[int]:
    """Page numbers of every LIST OF DATES / SYNOPSIS in ``text``."""
    return _cached(
        "summary",
        text,
        lambda p: _runs(p, starts_summary, continues_summary, _MAX_SUMMARY_CONTINUATION_PAGES),
    )


def avoid_pages(text: str) -> FrozenSet[int]:
    """Pages cited only when nothing else supports an item: the index and the
    advocate's list of dates / synopsis. Cached by digest: the panel builders
    ask once per entry of a document."""
    return index_pages(text) | summary_pages(text)


#: Both marker styles the stored text carries: ``--- Page N ---`` and the
#: older ``-- N of M --``.
_ANY_PAGE_MARKER_RE = re.compile(r"---\s*Page\s+(\d+)\s*---|--\s*(\d+)\s+of\s+\d+\s*--", re.I)


def page_markers(text: str) -> List[Tuple[int, int]]:
    """``(offset, page)`` of every page marker in ``text``, in order."""
    return [
        (m.start(), int(m.group(1) or m.group(2)))
        for m in _ANY_PAGE_MARKER_RE.finditer(text or "")
    ]


def _page_at(markers: List[Tuple[int, int]], pos: int) -> Optional[int]:
    page = None
    for at, number in markers:
        if at > pos:
            break
        page = number if number > 0 else page
    return page


#: An anchor without a digit (a title, a phrase) must be this long to place
#: an entry: "Notice" alone is on half the pages of a paper-book.
_MIN_WORD_ANCHOR = 12
#: A date or figure anchor: "3/2024" and longer; never a bare year.
_MIN_FIGURE_ANCHOR = 6


def _anchor_re(anchor: str) -> Optional["re.Pattern[str]"]:
    words = str(anchor or "").split()
    joined = " ".join(words)
    has_digit = bool(re.search(r"\d", joined))
    # A bare year ("2024") or number is on most pages of a case file.
    if len(joined) < (_MIN_FIGURE_ANCHOR if has_digit else _MIN_WORD_ANCHOR):
        return None
    # Any run of whitespace matches, so a line break inside the phrase is
    # fine; never inside a longer number or word (12.03.24 in 12.03.2402).
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![\w]){body}(?![\w])", re.IGNORECASE)


def anchor_pages(text: str, anchors: Iterable[str]) -> List[Tuple[int, int]]:
    """``(anchor_index, page)`` for every occurrence of every anchor, in order."""
    markers = page_markers(text)
    out: List[Tuple[int, int]] = []
    if not markers:
        return out
    for i, anchor in enumerate(anchors):
        rx = _anchor_re(anchor)
        if rx is None:
            continue
        for m in rx.finditer(text):
            page = _page_at(markers, m.start())
            if page is not None:
                out.append((i, page))
    return out


def anchor_page(
    text: str, anchors: Iterable[str], *, skip: Optional[Collection[int]] = None
) -> Optional[int]:
    """Page of the first anchor found in ``text``, outside its avoided pages.

    ``anchors`` are tried in order of preference (case-insensitive; line
    breaks inside a phrase are ignored). Every occurrence counts, so a date
    restated in the index or list of dates is placed on the page that
    actually carries it. An anchor found only on avoided pages gives that
    page, as a last resort. None when the text has no page markers or no
    anchor is in it: callers must not guess a page then.
    """
    skip = avoid_pages(text) if skip is None else skip
    fallback: Optional[int] = None
    for _i, page in anchor_pages(text, anchors):
        if page not in skip:
            return page
        if fallback is None:
            fallback = page
    return fallback


def page_has_anchor(text: str, page: int, anchors: Iterable[str]) -> bool:
    """Whether any anchor is on ``page``: checks a page someone else chose."""
    return any(p == page for _i, p in anchor_pages(text, anchors))


def resolve_entry_page(text: str, claimed: object, anchors: List[str]) -> Optional[int]:
    """The page of a panel entry (List of Dates, Procedural Timeline).

    The page the model claimed is kept only when it is not an avoided page and
    one of the entry's anchors (its date as written, its quote) is on it.
    Otherwise the entry is placed by ``anchor_page``. None when the document
    has no pages (Word without layout, spreadsheet, audio, text) or the entry
    cannot be placed: no page is better than a wrong one.
    """
    if not page_markers(text):
        return None
    skip = avoid_pages(text)
    if isinstance(claimed, str) and claimed.strip().isdigit():
        claimed = int(claimed.strip())
    if (
        isinstance(claimed, int)
        and not isinstance(claimed, bool)
        and claimed > 0
        and claimed not in skip
        and page_has_anchor(text, claimed, anchors)
    ):
        return claimed
    return anchor_page(text, anchors, skip=skip)


_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def date_forms(iso: str) -> List[str]:
    """An ISO date as documents write it, so a chronology entry can be found
    on its page: 12.03.2024, 12/03/2024, 12-03-2024, 12th March 2024,
    12 March, 2024, March 12, 2024, 12-Mar-2024, and the ISO form."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso or "").strip())
    if not m:
        month_only = re.fullmatch(r"(\d{4})-(\d{2})", (iso or "").strip())
        if month_only and 1 <= int(month_only.group(2)) <= 12:
            y, mo = month_only.groups()
            name = _MONTHS[int(mo) - 1]
            return list(dict.fromkeys(
                [f"{name} {y}", f"{name}, {y}", f"{name[:3]} {y}", f"{name[:3]}-{y}",
                 f"{mo}/{y}", f"{mo}.{y}", f"{int(mo)}/{y}", iso.strip()]
            ))
        return [iso] if iso else []
    y, mo, d = m.groups()
    day, month = int(d), int(mo)
    if not 1 <= month <= 12:
        return [iso]
    name = _MONTHS[month - 1]
    short = name[:3]
    forms = [
        f"{d}.{mo}.{y}", f"{d}/{mo}/{y}", f"{d}-{mo}-{y}", f"{y}-{mo}-{d}",
        f"{day}.{month}.{y}", f"{day}/{month}/{y}", f"{day}-{month}-{y}",
        f"{d}.{mo}.{y[2:]}", f"{d}/{mo}/{y[2:]}",
    ]
    for dd in dict.fromkeys([str(day), d, _ordinal(day)]):
        for mn in (name, short):
            forms += [f"{dd} {mn} {y}", f"{dd} {mn}, {y}", f"{dd}-{mn}-{y}", f"{dd} of {mn} {y}"]
    forms += [f"{name} {day}, {y}", f"{name} {_ordinal(day)}, {y}", f"{short} {day}, {y}"]
    return list(dict.fromkeys(forms))
