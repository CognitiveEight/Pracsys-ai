"""Which pages of a stored document are an index (SIM-192).

A court paper-book opens with an INDEX: one row per annexure, its title, its
date and its page range, and often the cause title with every party's name.
Anything that finds "the first page that mentions X" lands there, because the
index mentions everything and comes first. Citations should point at the
evidence, so callers use ``index_pages`` to skip those pages unless nothing
else supports the item.

Detection works on the stored text with its ``--- Page N ---`` markers:

* an index starts on a page with a standalone INDEX / TABLE OF CONTENTS /
  CONTENTS heading near the top that also reads like a list of pages;
* it continues over the following pages while each is still a list of page
  ranges and numbered rows (an index spans 2-3 pages in a long paper-book).

Conservative on purpose: a page is only an index when it looks like one, so a
document without an index cites exactly as before.
"""

import hashlib
import re
from typing import Collection, Dict, FrozenSet, Iterable, List, Optional, Tuple

from app.rag.chunker import PAGE_MARKER_RE

_INDEX_HEADING_RE = re.compile(
    r"^\W*(?:index|table\s+of\s+contents|contents)\W*$", re.IGNORECASE
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


def _ranges(page_text: str) -> int:
    return len(_PAGE_RANGE_RE.findall(page_text or ""))


def _numbered_rows(lines) -> int:
    return sum(1 for ln in lines if _NUMBERED_ROW_RE.match(ln))


def starts_index(page_text: str) -> bool:
    """A page with an INDEX-style heading near the top that lists pages."""
    lines = _lines(page_text)
    if not any(_INDEX_HEADING_RE.match(ln) for ln in lines[:_HEADING_WITHIN_LINES]):
        return False
    return _ranges(page_text) >= 2 or bool(_PAGE_COLUMN_RE.search(page_text))


def continues_index(page_text: str) -> bool:
    """A page that is still a list of page ranges and numbered rows."""
    lines = _lines(page_text)
    return _ranges(page_text) >= _MIN_RANGES and _numbered_rows(lines) >= _MIN_ROWS


#: Results by a digest of the text, never the text itself: a deleted case's
#: documents must not stay in memory (the workspace purge cannot reach
#: module-level state). Holds only page numbers.
_INDEX_CACHE: Dict[str, FrozenSet[int]] = {}
_INDEX_CACHE_MAX = 256


def index_pages(text: str) -> FrozenSet[int]:
    """Page numbers of every index in ``text`` (empty when it has none).

    Cached by digest: the panel builders ask once per entry of a document."""
    key = hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()
    hit = _INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_INDEX_CACHE) >= _INDEX_CACHE_MAX:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = found = _find_index_pages(text)
    return found


def _find_index_pages(text: str) -> FrozenSet[int]:
    pages = split_pages(text)
    found = set()
    numbers = sorted(pages)
    i = 0
    while i < len(numbers):
        if not starts_index(pages[numbers[i]]):
            i += 1
            continue
        found.add(numbers[i])
        j = i + 1
        while (
            j < len(numbers)
            and j - i <= _MAX_CONTINUATION_PAGES
            and continues_index(pages[numbers[j]])
        ):
            found.add(numbers[j])
            j += 1
        i = j
    return frozenset(found)


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


def anchor_page(
    text: str, anchors: Iterable[str], *, skip: Optional[Collection[int]] = None
) -> Optional[int]:
    """Page of the first anchor found in ``text``, outside its index pages.

    ``anchors`` are tried in order of preference (case-insensitive, at least 4
    characters). Every occurrence counts, so a date restated in the index is
    placed on the page that actually carries it. An anchor found only in the
    index gives the index page, as a last resort. None when the text has no
    page markers or no anchor is in it: callers must not guess a page then.
    """
    markers = page_markers(text)
    if not markers:
        return None
    skip = index_pages(text) if skip is None else skip
    haystack = text.lower()
    fallback: Optional[int] = None
    for anchor in anchors:
        needle = str(anchor or "").strip().lower()
        if len(needle) < 4:
            continue
        pos = haystack.find(needle)
        while pos >= 0:
            page = _page_at(markers, pos)
            if page is not None and page not in skip:
                return page
            if fallback is None:
                fallback = page
            pos = haystack.find(needle, pos + 1)
    return fallback


def date_forms(iso: str) -> List[str]:
    """An ISO date as documents write it (12.03.2024, 12/03/2024, ...), the
    ISO form included, so a chronology entry can be found on its page."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (iso or "").strip())
    if not m:
        return [iso] if iso else []
    y, mo, d = m.groups()
    forms = [f"{d}.{mo}.{y}", f"{d}/{mo}/{y}", f"{d}-{mo}-{y}", f"{y}-{mo}-{d}",
             f"{int(d)}.{int(mo)}.{y}", f"{int(d)}/{int(mo)}/{y}"]
    return list(dict.fromkeys(forms))
