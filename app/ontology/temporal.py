"""
Temporal normalisation — dates the chronology can actually be built from
(work-plan task 2.8).

Legal documents state dates in every form a human might: ``14.03.2025``,
``14th March 2025``, ``March 2025``, ``on or about 14 March 2025``, ``within 15
days of the said notice``. A chronology panel needs one comparable
representation, and — just as importantly — needs to know **how precise** each
one is, because "March 2025" and "14 March 2025" cannot be ordered against each
other with the same confidence.

Three things this module does that a naive parser does not:

* **Day-first by default.** ``03/04/2025`` is 3 April in Indian practice and
  4 March in American. Getting this backwards silently corrupts every
  chronology, so the convention is explicit and configurable, never inferred.
* **Precision is a first-class output.** ``date_precision`` ∈
  ``day | month | year | approximate`` rides with every parsed date and is
  written onto the ontology's Event/Hearing/Filing nodes.
* **Relative expressions resolve against an anchor.** ``"within 15 days of the
  notice"`` becomes a real date only when the anchor date is known; otherwise it
  is returned unresolved rather than silently anchored to today.

Ordering uses :func:`sort_key`, which sorts a less precise date to the *start*
of its range so a bare "March 2025" precedes "14 March 2025" — the conservative
choice for a chronology, since the alternative is asserting an ordering the
document does not support.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

#: Ontology's ``date_precision`` values, coarsest last.
PRECISION_DAY = "day"
PRECISION_MONTH = "month"
PRECISION_YEAR = "year"
PRECISION_APPROXIMATE = "approximate"

PRECISION_ORDER = (PRECISION_DAY, PRECISION_MONTH, PRECISION_YEAR, PRECISION_APPROXIMATE)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

#: Hedges that make an otherwise exact date approximate. Legal drafting uses
#: these deliberately, and the graph must not launder them into certainty.
_APPROXIMATION_MARKERS = (
    "on or about",
    "on or around",
    "circa",
    "approximately",
    "approx",
    "in or about",
    "sometime in",
    "around",
    "about",
)

_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

# 14.03.2025 · 14/03/2025 · 14-03-2025 · 2025-03-14
_NUMERIC_DATE = re.compile(r"\b(\d{1,4})[./-](\d{1,2})[./-](\d{2,4})\b")
# 14 March 2025 · 14 Mar 2025
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
# March 14, 2025
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
# March 2025
_MONTH_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
# the year 2025 (only with an explicit cue — a bare 4-digit number is not a date)
_BARE_YEAR = re.compile(r"\b(?:year|in|of)\s+(\d{4})\b", re.IGNORECASE)
# within 15 days / after 30 days / 15 days from
_RELATIVE_DAYS = re.compile(
    r"\b(?:with[ei]n|after|from|of)?\s*(\d{1,4})\s*(day|week|month|year)s?\b", re.IGNORECASE
)


class TemporalError(ValueError):
    """Raised when a caller asks for something the input cannot support."""


@dataclass(frozen=True)
class ParsedDate:
    """
    One normalised date.

    Attributes:
        value: The resolved date. For month precision this is the 1st; for year
            precision the 1st of January — see :attr:`range_start` /
            :attr:`range_end` for the span the text actually asserts.
        precision: One of :data:`PRECISION_ORDER`.
        raw: The source text this came from.
        is_relative: True when it was resolved from a relative expression.
        anchor: The anchor date a relative expression was resolved against.
    """

    value: date
    precision: str
    raw: str = ""
    is_relative: bool = False
    anchor: Optional[date] = None

    @property
    def range_start(self) -> date:
        """First date the source text could mean."""
        return self.value

    @property
    def range_end(self) -> date:
        """
        Last date the source text could mean.

        Day and approximate precision span a single day (approximate widens the
        *confidence*, not the range — the document still names one day).
        """
        if self.precision == PRECISION_MONTH:
            if self.value.month == 12:
                return date(self.value.year, 12, 31)
            return date(self.value.year, self.value.month + 1, 1) - timedelta(days=1)
        if self.precision == PRECISION_YEAR:
            return date(self.value.year, 12, 31)
        return self.value

    @property
    def iso(self) -> str:
        return self.value.isoformat()

    def to_dict(self) -> Dict[str, object]:
        """Serialisable form matching the ontology's Event date properties."""
        return {
            "event_date": self.iso,
            "date_precision": self.precision,
            "raw": self.raw,
            "range_start": self.range_start.isoformat(),
            "range_end": self.range_end.isoformat(),
            "is_relative": self.is_relative,
            "anchor": self.anchor.isoformat() if self.anchor else None,
        }


def _two_digit_year(year: int) -> int:
    """
    Expand a two-digit year.

    Legal corpora run decades back, so a two-digit year is read as 19xx when it
    would otherwise land in the future — ``98`` is 1998, not 2098.
    """
    if year >= 100:
        return year
    current = date.today().year % 100
    return 2000 + year if year <= current else 1900 + year


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    """Construct a date, returning None rather than raising on 31 February."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_approximate(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _APPROXIMATION_MARKERS)


def parse_date(
    text: str,
    day_first: bool = True,
    anchor: Optional[date] = None,
) -> Optional[ParsedDate]:
    """
    Parse the first date expression in ``text``.

    Args:
        text: Free text that may contain a date.
        day_first: Interpret an ambiguous numeric date as DD/MM/YYYY. True by
            default for Indian legal drafting. Set False only for a corpus known
            to be US-formatted — getting this wrong corrupts chronology silently.
        anchor: Date to resolve a relative expression against. Without it, a
            relative expression returns None rather than being anchored to today.

    Returns:
        The parsed date, or None when no date expression is present (or a
        relative one cannot be resolved).
    """
    if not text:
        return None
    cleaned = _ORDINAL_SUFFIX.sub("", str(text))
    approximate = _is_approximate(cleaned)

    def finish(value: Optional[date], precision: str) -> Optional[ParsedDate]:
        if value is None:
            return None
        return ParsedDate(
            value=value,
            precision=PRECISION_APPROXIMATE if approximate else precision,
            raw=str(text).strip(),
        )

    match = _DAY_MONTH_YEAR.search(cleaned)
    if match and match.group(2).lower().rstrip(".") in _MONTHS:
        day, month_name, year = match.groups()
        return finish(
            _safe_date(int(year), _MONTHS[month_name.lower().rstrip(".")], int(day)),
            PRECISION_DAY,
        )

    match = _MONTH_DAY_YEAR.search(cleaned)
    if match and match.group(1).lower().rstrip(".") in _MONTHS:
        month_name, day, year = match.groups()
        return finish(
            _safe_date(int(year), _MONTHS[month_name.lower().rstrip(".")], int(day)),
            PRECISION_DAY,
        )

    match = _NUMERIC_DATE.search(cleaned)
    if match:
        first, second, third = (int(g) for g in match.groups())
        if len(match.group(1)) == 4:  # ISO: YYYY-MM-DD
            return finish(_safe_date(first, second, third), PRECISION_DAY)
        year = _two_digit_year(third)
        day, month = (first, second) if day_first else (second, first)
        parsed = _safe_date(year, month, day)
        if parsed is None and day <= 12:
            # Day-first was wrong for this value (e.g. 03/14/2025); fall back
            # rather than dropping a real date, and say so.
            parsed = _safe_date(year, day, month)
            if parsed is not None:
                logger.debug(
                    "Numeric date {!r} is invalid day-first; read as month-first",
                    match.group(0),
                )
        return finish(parsed, PRECISION_DAY)

    match = _MONTH_YEAR.search(cleaned)
    if match and match.group(1).lower().rstrip(".") in _MONTHS:
        month_name, year = match.groups()
        return finish(
            _safe_date(int(year), _MONTHS[month_name.lower().rstrip(".")], 1),
            PRECISION_MONTH,
        )

    match = _BARE_YEAR.search(cleaned)
    if match:
        return finish(_safe_date(int(match.group(1)), 1, 1), PRECISION_YEAR)

    relative = resolve_relative(cleaned, anchor)
    if relative is not None:
        return relative

    return None


def resolve_relative(text: str, anchor: Optional[date]) -> Optional[ParsedDate]:
    """
    Resolve a relative expression such as ``"within 15 days"`` against an anchor.

    Args:
        text: The expression.
        anchor: The date it is relative to. **Required** — without it the
            expression is unresolvable and None is returned. Anchoring to
            "today" would fabricate a date that changes every time the pipeline
            runs.

    Returns:
        The resolved date, or None when there is no relative expression or no
        anchor to resolve it against.
    """
    match = _RELATIVE_DAYS.search(text or "")
    if not match:
        return None
    if anchor is None:
        logger.debug("Relative expression {!r} has no anchor; left unresolved", text)
        return None

    quantity, unit = int(match.group(1)), match.group(2).lower()
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
    # Month/year arithmetic in days is an approximation, and the precision says so.
    precision = PRECISION_DAY if unit in ("day", "week") else PRECISION_APPROXIMATE
    return ParsedDate(
        value=anchor + timedelta(days=quantity * days),
        precision=precision,
        raw=str(text).strip(),
        is_relative=True,
        anchor=anchor,
    )


def sort_key(parsed: ParsedDate) -> Tuple[date, int]:
    """
    Ordering key for a chronology.

    Sorts by the earliest date the text could mean, then by precision (finer
    first). A bare "March 2025" therefore precedes "14 March 2025": the document
    does not place it later, and asserting that it does would be an ordering the
    evidence has not established.
    """
    return parsed.range_start, PRECISION_ORDER.index(parsed.precision)


def build_chronology(
    events: Sequence[Mapping[str, object]],
    day_first: bool = True,
) -> Dict[str, object]:
    """
    Normalise and order a set of events into a chronology.

    Args:
        events: Items with a ``date`` (raw text or ISO) and a ``description``.
            An optional ``anchor`` (ISO date) resolves a relative expression.
        day_first: See :func:`parse_date`.

    Returns:
        ``{events, unparsed, precisionCounts}``. ``events`` is ordered and each
        carries its normalised date and precision; ``unparsed`` holds the items
        whose date could not be read, so nothing is silently dropped from a
        chronology that claims to be complete.
    """
    parsed_events: List[Tuple[ParsedDate, Mapping[str, object]]] = []
    unparsed: List[Dict[str, object]] = []

    for event in events:
        raw = event.get("date")
        anchor_raw = event.get("anchor")
        anchor = None
        if anchor_raw:
            try:
                anchor = date.fromisoformat(str(anchor_raw))
            except ValueError:
                logger.warning("Ignoring unparseable anchor {!r}", anchor_raw)
        parsed = parse_date(str(raw or ""), day_first=day_first, anchor=anchor)
        if parsed is None:
            unparsed.append(
                {"date": raw, "description": event.get("description"), "reason": "no date found"}
            )
            continue
        parsed_events.append((parsed, event))

    parsed_events.sort(key=lambda pair: sort_key(pair[0]))

    precision_counts: Dict[str, int] = {p: 0 for p in PRECISION_ORDER}
    ordered: List[Dict[str, object]] = []
    for parsed, event in parsed_events:
        precision_counts[parsed.precision] += 1
        ordered.append(
            {
                "date": parsed.iso,
                "date_precision": parsed.precision,
                "description": event.get("description"),
                "raw_date": parsed.raw,
                "is_relative": parsed.is_relative,
                **{
                    key: value
                    for key, value in event.items()
                    if key not in {"date", "description", "anchor"}
                },
            }
        )

    if unparsed:
        logger.warning(
            "{} of {} chronology entries had no readable date and are excluded from the ordering",
            len(unparsed),
            len(events),
        )
    return {"events": ordered, "unparsed": unparsed, "precisionCounts": precision_counts}


def derive_precedes_edges(chronology: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """
    Derive the ontology's ``PRECEDES`` edges from an ordered chronology.

    Only consecutive pairs whose ranges do **not** overlap get an edge: if one
    event is "March 2025" and the next is "14 March 2025", the document has not
    established which came first, and a derived edge asserting it would be a
    fact the evidence does not support.

    Args:
        chronology: The ``events`` list from :func:`build_chronology`.

    Returns:
        ``{from, to, basis}`` triples ready to be written as PRECEDES edges.
    """
    edges: List[Dict[str, object]] = []
    for earlier, later in zip(chronology, chronology[1:], strict=False):
        early_precision = str(earlier.get("date_precision", PRECISION_DAY))
        later_precision = str(later.get("date_precision", PRECISION_DAY))
        if (
            early_precision == PRECISION_DAY
            and later_precision == PRECISION_DAY
            and str(earlier["date"]) < str(later["date"])
        ):
            edges.append(
                {
                    "from": earlier.get("description"),
                    "to": later.get("description"),
                    "basis": f"{earlier['date']} < {later['date']}",
                }
            )
    return edges
