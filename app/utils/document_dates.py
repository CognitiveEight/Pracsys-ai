"""
The date a document carries in its file name.

Indian legal files are routinely named by date: "25 11 29 Letter KCA.pdf",
"26-03-13 Legal Notice.pdf", "30 Nov BCCI Letter.pdf". For a letter or
representation whose body states no date, the file name is the only record of
when it was written, and without it the List of Dates came back empty. Stored
names also carry an upload prefix ("annexure-1-30_Nov_BCCI_Letter.docx.pdf"),
which the earlier parser — anchored at the start of the name — never got past.

``date_from_document_name`` returns ``(YYYY-MM-DD, year_was_given)``. A name
with no year ("30 Nov") resolves to the most recent such date on or before
today: a document cannot be dated in the future.
"""

import re
from datetime import date, datetime
from typing import Optional, Tuple

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_FULL_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# "annexure-1-30_Nov..." (numbered upload prefix) and "Annexure-30_Nov..."
# (prefix word only — the 30 is the date). Both readings are tried.
_UPLOAD_PREFIX_NUMBERED = re.compile(r"^(?:annexure|annex|exhibit|doc(?:ument)?)[\s_\-:.]*\d+[\s_\-:.]+", re.I)
_UPLOAD_PREFIX_WORD = re.compile(r"^(?:annexure|annex|exhibit|doc(?:ument)?)[\s_\-:.]+", re.I)
_SEP = r"[\s_\-.]"
_NUMERIC = [
    re.compile(rf"^(?P<y>(?:19|20)\d{{2}}){_SEP}(?P<m>\d{{1,2}}){_SEP}(?P<d>\d{{1,2}})(?=\D|$)"),  # 2026-03-13
    re.compile(rf"^(?P<y>\d{{2}}){_SEP}(?P<m>\d{{1,2}}){_SEP}(?P<d>\d{{1,2}})(?=\D|$)"),  # 26 03 13 (YY MM DD)
    re.compile(r"^(?P<y>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})(?=\D|$)"),  # 20260313
    re.compile(r"^(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?=\D|$)"),  # 260313
]
_DAY_MONTH = re.compile(
    rf"^(?P<d>\d{{1,2}})(?:st|nd|rd|th)?{_SEP}+(?P<mon>[A-Za-z]{{3,9}})"
    rf"(?:{_SEP}+(?P<y>(?:19|20)\d{{2}}))?(?=[^A-Za-z]|$)"
)  # 30 Nov / 30_November_2025


def _today(today_iso: Optional[str]) -> date:
    try:
        return datetime.strptime(str(today_iso)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return datetime.utcnow().date()


def date_from_document_name(name: str, today_iso: Optional[str] = None) -> Optional[Tuple[str, bool]]:
    """The document date in a file name, as ``(YYYY-MM-DD, year_was_given)``, or None."""
    raw = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    seen = []
    for candidate in (_UPLOAD_PREFIX_NUMBERED.sub("", raw), _UPLOAD_PREFIX_WORD.sub("", raw), raw):
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
            found = _parse(candidate, today_iso)
            if found:
                return found
    return None


def _parse(base: str, today_iso: Optional[str]) -> Optional[Tuple[str, bool]]:
    for pattern in _NUMERIC:
        m = pattern.match(base)
        if not m:
            continue
        y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
        if y < 100:
            y = 2000 + y if y <= 49 else 1900 + y
        try:
            return date(y, mo, d).isoformat(), True
        except ValueError:
            continue
    m = _DAY_MONTH.match(base)
    if m:
        word = m.group("mon").lower()
        month = _MONTHS.get(word) or _FULL_MONTHS.get(word)  # "Novel" is not November
        if month:
            day = int(m.group("d"))
            if m.group("y"):
                try:
                    return date(int(m.group("y")), month, day).isoformat(), True
                except ValueError:
                    return None
            today = _today(today_iso)
            for year in (today.year, today.year - 1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate <= today:
                    return candidate.isoformat(), False
    return None
