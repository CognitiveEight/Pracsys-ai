"""Check table totals in a draft, in code (SIM-209).

Models add columns up wrongly — a capitalisation table once showed 990,000
shares where its rows summed to 1,000,000. Formatting cannot catch that, and
the model must never be trusted with arithmetic. After a draft is rendered,
every Markdown table with a total row is re-added with exact decimals:

- a total row is one whose first non-numeric cell starts with Total, Grand
  total, Sub-total or Aggregate; it totals the rows since the previous total
  row (or the header);
- a column is checked only when its total cell is a number and every row
  above is a number, a dash ("-", "—": nothing) or empty — a placeholder or
  text anywhere makes the column unverifiable, and it is left alone;
- numbers keep their own form: INR/Rs./₹/USD/$ prefixes, % suffixes, Indian
  (1,20,00,000) or international (1,200,000) grouping, decimals;
- percentages allow the rounding their rows carry (half a unit of the last
  decimal per row).

A wrong total is flagged in place as ``<check total: 1,000,000>`` (rendered
highlighted, like every placeholder) or, with ``DRAFT_TOTALS_FIX``, replaced
by the right figure. Pure and deterministic; no model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

_TOTAL_RE = re.compile(r"^\s*(?:\*\*|__)?\s*(grand\s+total|sub-?\s*total|total|aggregate)\b",
                       re.IGNORECASE)
_DASH = {"", "-", "—", "–", "nil", "n/a", "na"}
_NUM_RE = re.compile(
    r"^(?P<pre>(?:INR|Rs\.?|₹|USD|US\$|\$|EUR|€|GBP|£)\s*)?"
    r"(?P<sign>-)?(?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<post>\s*(?:%|/-|lakh|crore|cr\.?)?)$",
    re.IGNORECASE,
)


@dataclass
class TotalIssue:
    table: int
    row: int
    column: int
    stated: str
    computed: str


@dataclass
class _Num:
    value: Decimal
    pre: str
    post: str
    decimals: int
    indian: bool


def _strip(cell: str) -> str:
    c = cell.strip()
    for m in ("**", "__"):
        if c.startswith(m) and c.endswith(m) and len(c) > 2 * len(m):
            c = c[len(m):-len(m)].strip()
    return c


def parse_number(cell: str) -> Optional[_Num]:
    m = _NUM_RE.match(_strip(cell))
    if not m:
        return None
    raw = m.group("num")
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if m.group("sign"):
        value = -value
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    groups = raw.split(".")[0].split(",")
    indian = len(groups) >= 3 and all(len(g) == 2 for g in groups[1:-1])
    return _Num(value, m.group("pre") or "", m.group("post") or "", decimals, indian)


def format_number(value: Decimal, like: _Num) -> str:
    q = Decimal(1).scaleb(-like.decimals) if like.decimals else Decimal(1)
    value = value.quantize(q)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):f}".partition(".")
    if like.indian and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    else:
        whole = f"{int(whole):,}"
    text = whole + ("." + frac if frac else "")
    return f"{like.pre}{sign}{text}{like.post}"


def _split_row(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def _is_sep(cells: List[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c) and any(cells)


def _tables(lines: List[str]) -> List[Tuple[int, int]]:
    """(first, last) line indexes of each pipe table with a separator row."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j + 1 < len(lines) and lines[j + 1].lstrip().startswith("|"):
                j += 1
            if j > i and _is_sep(_split_row(lines[i + 1])):
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def _is_total_row(cells: List[str]) -> bool:
    for c in cells:
        s = _strip(c)
        if not s or parse_number(s):
            continue
        return bool(_TOTAL_RE.match(s))
    return False


def check_table_totals(markdown: str, *, fix: bool = False) -> Tuple[str, List[TotalIssue]]:
    """Recompute every table total; flag (or fix) the wrong ones."""
    if not markdown or "|" not in markdown:
        return markdown, []
    lines = markdown.split("\n")
    issues: List[TotalIssue] = []
    for ti, (first, last) in enumerate(_tables(lines)):
        rows = [_split_row(lines[k]) for k in range(first, last + 1)]
        body_start = 2  # header, separator
        block_start = body_start
        for ri in range(body_start, len(rows)):
            cells = rows[ri]
            if not _is_total_row(cells):
                continue
            block = rows[block_start:ri]
            for ci, cell in enumerate(cells):
                stated = parse_number(cell)
                if stated is None or not block:
                    continue
                total = Decimal(0)
                decimals, ok, n = stated.decimals, True, 0
                for r in block:
                    v = _strip(r[ci]) if ci < len(r) else ""
                    if v.lower() in _DASH:
                        continue
                    num = parse_number(v)
                    if num is None:
                        ok = False
                        break
                    total += num.value
                    decimals = max(decimals, num.decimals)
                    n += 1
                if not ok or n < 2:
                    continue
                tol = Decimal(0)
                if "%" in stated.post:
                    tol = Decimal("0.5").scaleb(-decimals) * n
                if abs(total - stated.value) <= tol:
                    continue
                like = _Num(stated.value, stated.pre, stated.post, decimals,
                            stated.indian or any(parse_number(_strip(r[ci])) and
                                                 parse_number(_strip(r[ci])).indian
                                                 for r in block if ci < len(r)))
                computed = format_number(total, like)
                issues.append(TotalIssue(ti, ri - body_start, ci, _strip(cell), computed))
                if fix:
                    new = computed
                else:
                    new = "{} <check total: {}>".format(_strip(cell), computed)
                if cell.strip().startswith("**"):
                    new = "**{}**".format(new) if fix else "**{}** <check total: {}>".format(
                        _strip(cell), computed)
                cells[ci] = new
            rows[ri] = cells
            lines[first + ri] = "| " + " | ".join(cells) + " |"
            block_start = ri + 1
    return "\n".join(lines), issues


def totals_fix_enabled() -> bool:
    from app.core.config import settings

    return bool(getattr(settings, "draft_totals_fix", False))
