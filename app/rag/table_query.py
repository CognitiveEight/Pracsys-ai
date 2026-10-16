"""
Exact calculations over spreadsheet rows, for chat.

Retrieval shows the model a handful of passages, so a question such as "how
many players failed?" or "total paid to vendors in March" over a 500-row sheet
used to be answered from whichever 5 or 6 blocks came back, with no sign that
the rest existed. A model adding up rows by eye is wrong in any case.

So the arithmetic is done here, in code, over every stored row:

1. :func:`plan_queries`: the model turns the question into at most
   ``MAX_QUERIES`` small JSON specs (table, filters, operation, column). It
   sees only the workbook manifests (sheet and column names, kinds, and the
   exact values of categorical columns), never the rows.
2. :func:`validate`: every column must exist in that sheet, every value
   must parse as that column's type, and every operation must suit the column
   ("sum" of a phone-number column is refused). A spec that fails is reported,
   never repaired by guessing.
3. :func:`execute`: AND-ed filters and the operation run over every data row,
   with exact decimal arithmetic. The sheet's own Total rows are excluded
   (they would count twice), and so are cells with no usable value, which are
   counted and stated.
4. :func:`render`: a deterministic statement of what was computed, over
   which rows, and the result. It becomes a ``[T#]`` evidence item, so the
   citation checker verifies every figure the answer quotes against it, as for
   any passage.

When a calculation cannot be made, the turn says so (``TableAnswers.notes``)
and the prompt forbids stating a count or total from partial rows.

No SQL, no ``eval``: specs are plain data, interpreted against a fixed set of
operations. Nothing here logs row values or the question.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from app.processing.tables import (
    KIND_BOOLEAN,
    KIND_DATE,
    KIND_NUMBER,
    KIND_TEXT,
    _decimal,
    format_indian,
    format_number,
    parse_date_text,
    parse_number_text,
)

MAX_QUERIES = 3
LIST_LIMIT = 50
GROUP_LIMIT = 30
_MAX_PLAN_PROMPT_CHARS = 14_000
_MAX_VALUE_CHARS = 200

#: Questions that ask for a computation. Deliberately broad: it only decides
#: whether the planner (one small model call) runs, and the planner itself
#: returns no query when the question needs none.
_CUE_RE = re.compile(
    r"\b(how\s+many|how\s+much|number\s+of|count|counts|total|totals|sum|"
    r"average|avg|mean|highest|lowest|maximum|minimum|max|min|most|least|"
    r"largest|smallest|top\s+\d+|bottom\s+\d+|list\s+(all|every|the)|"
    r"which\s+\w+|who\s+(all|has|have|had|is|are|was|were)|more\s+than|"
    r"less\s+than|greater\s+than|fewer\s+than|above|below|between|exceed\w*|"
    r"at\s+least|at\s+most|percent\w*|proportion|share|ratio|distinct|unique|"
    r"each|per)\b|%",
    re.I,
)

OPERATIONS = ("count", "sum", "average", "min", "max", "count_distinct", "distinct", "list", "percent")
FILTER_OPS = (
    "eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte",
    "between", "in", "is_empty", "not_empty",
)
_NUMERIC_OPS = ("sum", "average")
_ORDERED_OPS = ("min", "max")


@dataclass
class TableRef:
    """One sheet the planner may query, under a short handle."""

    ref: str
    document_id: str
    file_name: str
    annexure: Optional[int]
    sheet: Dict[str, Any]  # one entry of tables.manifest()["sheets"]

    @property
    def columns(self) -> List[Dict[str, Any]]:
        return list(self.sheet.get("columns") or [])

    def column_index(self, name: Any) -> Optional[int]:
        key = _norm(name)
        for i, col in enumerate(self.columns):
            if _norm(col.get("name")) == key:
                return i
        return None


@dataclass
class Filter:
    column: int
    op: str
    value: Any = None  # coerced to the column's type


@dataclass
class Query:
    ref: TableRef
    operation: str
    column: Optional[int] = None
    filters: List[Filter] = field(default_factory=list)
    base_filters: List[Filter] = field(default_factory=list)
    group_by: Optional[int] = None
    columns: List[int] = field(default_factory=list)  # for "list"
    limit: int = LIST_LIMIT


@dataclass
class Result:
    query: Query
    text: str
    row_start: Optional[int]
    row_end: Optional[int]


@dataclass
class TableAnswers:
    results: List[Result] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    has_tables: bool = False


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


# ---------------------------------------------------------------------------
# Deciding and planning
# ---------------------------------------------------------------------------


def table_refs(
    manifests: Sequence[Dict[str, Any]], annexure_of: Dict[str, int], names: Dict[str, str]
) -> List[TableRef]:
    """Queryable sheets of the workspace's current documents, numbered 1..n."""
    refs: List[TableRef] = []
    for m in sorted(manifests, key=lambda m: annexure_of.get(m["document_id"], 10**6)):
        did = m["document_id"]
        if did not in annexure_of:
            continue  # a document no longer in the workspace
        for sheet in (m.get("manifest") or {}).get("sheets") or []:
            refs.append(
                TableRef(
                    ref=str(len(refs) + 1),
                    document_id=did,
                    file_name=names.get(did) or m.get("filename") or "",
                    annexure=annexure_of.get(did),
                    sheet=sheet,
                )
            )
    return refs


def needs_calculation(question: str, refs: Sequence[TableRef]) -> bool:
    """Whether to plan: the question asks for a computation, or names a sheet
    or column of one of the workspace's spreadsheets."""
    if not refs or not (question or "").strip():
        return False
    if _CUE_RE.search(question):
        return True
    q = _norm(question)
    for ref in refs:
        names = [ref.sheet.get("sheet")] + [c.get("name") for c in ref.columns]
        if any(len(_norm(n)) >= 4 and _norm(n) in q for n in names):
            return True
    return False


def _describe_refs(refs: Sequence[TableRef]) -> str:
    lines: List[str] = []
    for ref in refs:
        s = ref.sheet
        where = f"Annexure {ref.annexure}" if ref.annexure else "Spreadsheet"
        lines.append(
            f"[{ref.ref}] {where} ({ref.file_name}), sheet \"{s.get('sheet')}\": "
            f"{s.get('data_rows')} data rows (rows {s.get('first_row')}-{s.get('last_row')})"
        )
        for col in ref.columns:
            line = f"    - {col.get('name')} ({col.get('kind')})"
            if col.get("values"):
                line += " values: " + json.dumps(col["values"][:50], ensure_ascii=False)
            elif col.get("examples"):
                line += " e.g. " + json.dumps(col["examples"][:5], ensure_ascii=False)
            lines.append(line)
    text = "\n".join(lines)
    if len(text) > _MAX_PLAN_PROMPT_CHARS:
        text = text[:_MAX_PLAN_PROMPT_CHARS] + "\n    ... (more columns not shown)"
    return text


_PLAN_INSTRUCTIONS = """You translate a lawyer's question into calculations over spreadsheet tables.
You do not answer the question and you never see the rows: a program runs your
calculations exactly over every row.

Return JSON only: {"queries": [ ... ]} with at most 3 queries, or {"queries": []}
when the question needs no count, total, average, minimum, maximum, percentage,
distinct values or filtered list of rows from these tables.

Each query:
{"table": "<table number>",
 "operation": "count" | "sum" | "average" | "min" | "max" | "count_distinct" | "distinct" | "list" | "percent",
 "column": "<column name>" or null,
 "filters": [{"column": "<column name>", "op": "<op>", "value": <value>}],
 "base_filters": [...],
 "group_by": "<column name>" or null,
 "columns": ["<column name>", ...],
 "limit": <number>}

Rules:
- Use table numbers and column names exactly as listed. Never invent a column.
- "op" is one of: eq, ne, contains, not_contains, gt, gte, lt, lte, between
  (value [low, high], inclusive), in (value [a, b, ...]), is_empty, not_empty.
  Filters are combined with AND.
- For a column that lists its values, filter with one of those exact values.
- Dates as YYYY-MM-DD. Numbers as plain numbers (no commas or currency).
- "count" counts rows; "count_distinct"/"distinct" need "column".
- "sum"/"average" need a number column; "min"/"max" a number or date column.
- "percent" = rows matching "filters" as a percentage of rows matching
  "base_filters" (all rows when empty).
- "list" returns the matching rows; "columns" optionally limits what is shown.
- "group_by" splits count/sum/average/min/max per value of that column.
"""


def _json_object(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None


async def plan_queries(
    question: str, previous_user_turn: str, refs: Sequence[TableRef]
) -> Optional[List[Dict[str, Any]]]:
    """Raw query specs from the model, or None if planning failed."""
    from app.ai.gemini_client import gemini_client, is_generation_error

    prompt = (
        _PLAN_INSTRUCTIONS
        + "\nTables:\n"
        + _describe_refs(refs)
        + f"\n\nPrevious question: {previous_user_turn.strip()[:500] or '(none)'}"
        + f"\nQuestion: {question.strip()[:1500]}"
    )
    try:
        raw = await gemini_client.generate(
            prompt,
            temperature=0.0,
            max_tokens=1200,
            thinking_level="LOW",
            response_mime_type="application/json",
        )
    except Exception as err:
        logger.warning("Spreadsheet query planning failed: {}", err)
        return None
    if is_generation_error(raw):
        logger.warning("Spreadsheet query planning returned a generation error")
        return None
    parsed = _json_object(raw)
    if parsed is None or not isinstance(parsed.get("queries"), list):
        logger.warning("Spreadsheet query planning returned no usable JSON")
        return None
    return [q for q in parsed["queries"][:MAX_QUERIES] if isinstance(q, dict)]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class SpecError(ValueError):
    """A query spec that cannot be run as written."""


def _column_kind(ref: TableRef, index: int) -> str:
    return str(ref.columns[index].get("kind") or KIND_TEXT)


def _coerce(ref: TableRef, index: int, value: Any) -> Any:
    """A filter value as the column's type, or SpecError."""
    kind = _column_kind(ref, index)
    name = ref.columns[index].get("name")
    if isinstance(value, (list, dict)):
        raise SpecError(f'a single value was expected for column "{name}"')
    text = str(value if value is not None else "").strip()[:_MAX_VALUE_CHARS]
    if kind == KIND_NUMBER:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _decimal(value)
        parsed = parse_number_text(text)
        if parsed is None:
            raise SpecError(f'"{text}" is not a number (column "{name}")')
        return _decimal(parsed[0])
    if kind == KIND_DATE:
        iso = parse_date_text(text[:10]) if len(text) >= 10 else parse_date_text(text)
        if iso is None:
            raise SpecError(f'"{text}" is not a date (column "{name}")')
        return iso
    if kind == KIND_BOOLEAN:
        low = text.casefold()
        if low in ("true", "yes", "y", "1"):
            return "true"
        if low in ("false", "no", "n", "0"):
            return "false"
        raise SpecError(f'"{text}" is not true/false (column "{name}")')
    return _norm(text)


def _filters(ref: TableRef, raw: Any) -> List[Filter]:
    out: List[Filter] = []
    for f in raw or []:
        if not isinstance(f, dict):
            raise SpecError("a filter is not an object")
        index = ref.column_index(f.get("column"))
        if index is None:
            raise SpecError(f'there is no column "{f.get("column")}" in sheet "{ref.sheet.get("sheet")}"')
        op = str(f.get("op") or "").strip().lower()
        if op not in FILTER_OPS:
            raise SpecError(f'unknown filter "{op}"')
        kind = _column_kind(ref, index)
        if op in ("gt", "gte", "lt", "lte", "between") and kind not in (KIND_NUMBER, KIND_DATE):
            raise SpecError(
                f'"{op}" needs a number or date column; "{ref.columns[index].get("name")}" is {kind}'
            )
        value: Any = None
        if op in ("is_empty", "not_empty"):
            value = None
        elif op == "between":
            pair = f.get("value")
            if not isinstance(pair, list) or len(pair) != 2:
                raise SpecError('"between" needs [low, high]')
            value = (_coerce(ref, index, pair[0]), _coerce(ref, index, pair[1]))
        elif op == "in":
            items = f.get("value")
            if not isinstance(items, list) or not items:
                raise SpecError('"in" needs a list of values')
            value = [_coerce(ref, index, v) for v in items[:100]]
        elif op in ("contains", "not_contains"):
            value = _norm(f.get("value"))[:_MAX_VALUE_CHARS]
            if not value:
                raise SpecError(f'"{op}" needs a value')
        else:
            value = _coerce(ref, index, f.get("value"))
        out.append(Filter(index, op, value))
    return out


def validate(spec: Dict[str, Any], refs: Sequence[TableRef]) -> Query:
    """A runnable Query from a raw spec, or SpecError saying why not."""
    ref = next((r for r in refs if r.ref == str(spec.get("table") or "").strip()), None)
    if ref is None:
        raise SpecError(f'unknown table "{spec.get("table")}"')
    operation = str(spec.get("operation") or "").strip().lower()
    if operation not in OPERATIONS:
        raise SpecError(f'unknown operation "{operation}"')
    query = Query(ref=ref, operation=operation)

    col_name = spec.get("column")
    if col_name not in (None, ""):
        query.column = ref.column_index(col_name)
        if query.column is None:
            raise SpecError(f'there is no column "{col_name}" in sheet "{ref.sheet.get("sheet")}"')
    needs_column = operation in _NUMERIC_OPS + _ORDERED_OPS + ("count_distinct", "distinct")
    if needs_column and query.column is None:
        raise SpecError(f'"{operation}" needs a column')
    if query.column is not None:
        kind = _column_kind(ref, query.column)
        name = ref.columns[query.column].get("name")
        if operation in _NUMERIC_OPS and kind != KIND_NUMBER:
            raise SpecError(f'"{operation}" needs a number column; "{name}" is {kind}')
        if operation in _ORDERED_OPS and kind not in (KIND_NUMBER, KIND_DATE):
            raise SpecError(f'"{operation}" needs a number or date column; "{name}" is {kind}')

    query.filters = _filters(ref, spec.get("filters"))
    query.base_filters = _filters(ref, spec.get("base_filters"))
    if operation == "percent" and not query.filters:
        raise SpecError('"percent" needs filters saying which rows to count')

    group = spec.get("group_by")
    if group not in (None, ""):
        if operation not in ("count",) + _NUMERIC_OPS + _ORDERED_OPS:
            raise SpecError(f'"group_by" does not apply to "{operation}"')
        query.group_by = ref.column_index(group)
        if query.group_by is None:
            raise SpecError(f'there is no column "{group}" in sheet "{ref.sheet.get("sheet")}"')

    for name in spec.get("columns") or []:
        index = ref.column_index(name)
        if index is None:
            raise SpecError(f'there is no column "{name}" in sheet "{ref.sheet.get("sheet")}"')
        query.columns.append(index)
    try:
        query.limit = max(1, min(LIST_LIMIT, int(spec.get("limit") or LIST_LIMIT)))
    except (TypeError, ValueError):
        query.limit = LIST_LIMIT
    return query


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _cell(row: Dict[str, Any], index: int) -> Tuple[str, Any, str]:
    """``(display, value, kind)`` of one cell of a stored row."""
    def at(key: str, default: Any) -> Any:
        seq = row.get(key) or []
        return seq[index] if index < len(seq) else default

    return str(at("display", "") or ""), at("values", None), str(at("kinds", KIND_TEXT) or KIND_TEXT)


def _typed(ref: TableRef, index: int, row: Dict[str, Any]) -> Any:
    """A cell's comparable value for its column's kind, or None if it has none."""
    display, value, kind = _cell(row, index)
    col_kind = _column_kind(ref, index)
    if col_kind == KIND_NUMBER:
        return _decimal(value) if kind == KIND_NUMBER else None
    if col_kind == KIND_DATE:
        return str(value)[:10] if kind == KIND_DATE and value else None
    if col_kind == KIND_BOOLEAN:
        if isinstance(value, bool):
            return "true" if value else "false"
        return _norm(display) or None
    return _norm(display) or None


def _matches(ref: TableRef, f: Filter, row: Dict[str, Any]) -> bool:
    display = _cell(row, f.column)[0]
    if f.op == "is_empty":
        return not display.strip()
    if f.op == "not_empty":
        return bool(display.strip())
    value = _typed(ref, f.column, row)
    if f.op == "contains":
        return f.value in _norm(display)
    if f.op == "not_contains":
        return f.value not in _norm(display)
    if value is None:
        # No usable value never satisfies a comparison; "ne" included, so a
        # blank cell is not silently counted as "not Paid".
        return False
    if f.op == "eq":
        return value == f.value
    if f.op == "ne":
        return value != f.value
    if f.op == "in":
        return value in f.value
    if f.op == "between":
        low, high = f.value
        return low <= value <= high
    return {
        "gt": value > f.value,
        "gte": value >= f.value,
        "lt": value < f.value,
        "lte": value <= f.value,
    }[f.op]


def _fig(value: Decimal) -> str:
    """A figure as plain digits plus Indian grouping, so the answer may quote
    either form and the citation checker still finds it."""
    plain = format_number(value)
    if abs(value) < 1000 and value == value.to_integral_value():
        return plain
    places = 2 if value != value.to_integral_value() else None
    grouped = format_indian(value, places)
    return plain if grouped.replace(",", "") == plain else f"{plain} ({grouped})"


def _average(values: List[Decimal]) -> Tuple[Decimal, bool]:
    """The mean, and whether it was rounded (to 2 decimal places)."""
    mean = sum(values) / Decimal(len(values))
    rounded = mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (rounded, rounded != mean)


def _filters_text(ref: TableRef, filters: Sequence[Filter]) -> str:
    def show(v: Any) -> str:
        if isinstance(v, Decimal):
            return format_number(v)
        return f'"{v}"' if isinstance(v, str) else str(v)

    words = {
        "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
        "contains": "contains", "not_contains": "does not contain",
    }
    parts = []
    for f in filters:
        name = f'"{ref.columns[f.column].get("name")}"'
        if f.op == "is_empty":
            parts.append(f"{name} is empty")
        elif f.op == "not_empty":
            parts.append(f"{name} is not empty")
        elif f.op == "between":
            parts.append(f"{name} between {show(f.value[0])} and {show(f.value[1])}")
        elif f.op == "in":
            parts.append(f"{name} is one of " + ", ".join(show(v) for v in f.value))
        else:
            parts.append(f"{name} {words[f.op]} {show(f.value)}")
    return " AND ".join(parts)


def _aggregate(ref: TableRef, query: Query, rows: List[Dict[str, Any]]) -> Tuple[str, int]:
    """The result phrase for one group of rows, and how many rows of it had no
    usable value in the aggregated column."""
    op = query.operation
    if op == "count":
        return f"{len(rows)} row(s)", 0
    values = [_typed(ref, query.column, r) for r in rows]
    present = [v for v in values if v is not None]
    missing = len(values) - len(present)
    name = f'"{ref.columns[query.column].get("name")}"'
    if not present:
        return f"no usable values in {name}", missing
    if op == "sum":
        return f"sum of {name} = {_fig(sum(present))}", missing
    if op == "average":
        mean, rounded = _average(present)
        note = " (rounded to 2 decimal places)" if rounded else ""
        return f"average of {name} = {_fig(mean)}{note} over {len(present)} value(s)", missing
    if op in ("min", "max"):
        best = min(present) if op == "min" else max(present)
        shown = _fig(best) if isinstance(best, Decimal) else best
        where = [r.get("row") for r, v in zip(rows, values) if v == best][:5]
        return (
            f"{'minimum' if op == 'min' else 'maximum'} of {name} = {shown} "
            f"(row {', '.join(str(w) for w in where)})",
            missing,
        )
    raise SpecError(f'"{op}" cannot be grouped')


def execute(query: Query, stored_rows: Sequence[Dict[str, Any]]) -> Result:
    """Run a validated query over a sheet's stored rows."""
    ref = query.ref
    sheet = ref.sheet
    data = [r for r in stored_rows if not r.get("is_total")]
    totals = [r for r in stored_rows if r.get("is_total")]
    where = f"Annexure {ref.annexure} ({ref.file_name})" if ref.annexure else ref.file_name
    head = (
        f'Computed by the system from every row of sheet "{sheet.get("sheet")}" in {where}: '
        f"{len(data)} data row(s)"
    )
    if data:
        head += f" (rows {data[0].get('row')}-{data[-1].get('row')})"
    notes: List[str] = []
    if totals:
        notes.append(
            f"{len(totals)} row(s) labelled Total ("
            + ", ".join(f"row {r.get('row')}" for r in totals)
            + ") excluded so nothing is counted twice"
        )
    if sheet.get("uncomputed_formulas"):
        notes.append(
            f"{sheet['uncomputed_formulas']} formula cell(s) in this sheet have no saved value and "
            "were treated as empty"
        )

    base = [r for r in data if all(_matches(ref, f, r) for f in query.base_filters)]
    matched = [r for r in base if all(_matches(ref, f, r) for f in query.filters)]
    lines = [head + "."]
    if query.base_filters:
        lines.append(f"Base: {_filters_text(ref, query.base_filters)} ({len(base)} row(s)).")
    if query.filters:
        lines.append(f"Filter: {_filters_text(ref, query.filters)}.")
    lines.append(f"Matching rows: {len(matched)}.")

    op = query.operation
    missing = 0
    if op == "percent":
        if base:
            pct = (Decimal(len(matched)) * 100 / Decimal(len(base))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            lines.append(
                f"Percentage: {len(matched)} of {len(base)} row(s) = {format_number(pct)}%."
            )
        else:
            lines.append("Percentage: no rows to compare against.")
    elif op in ("count_distinct", "distinct"):
        counts: Dict[str, int] = {}
        shown: Dict[str, str] = {}
        for r in matched:
            display = _cell(r, query.column)[0].strip()
            if not display:
                missing += 1
                continue
            key = _norm(display)
            counts[key] = counts.get(key, 0) + 1
            shown.setdefault(key, display)
        name = f'"{ref.columns[query.column].get("name")}"'
        if op == "count_distinct":
            lines.append(f"Distinct values of {name}: {len(counts)}.")
        else:
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            body = "; ".join(f"{shown[k]}: {n} row(s)" for k, n in ranked[:GROUP_LIMIT])
            more = f" ({len(ranked) - GROUP_LIMIT} more values not listed)" if len(ranked) > GROUP_LIMIT else ""
            lines.append(f"Values of {name}: {body}{more}.")
    elif op == "list":
        cols = query.columns or list(range(len(ref.columns)))
        header = " | ".join(str(ref.columns[i].get("name")) for i in cols)
        shown_rows = matched[: query.limit]
        listing = [f"row {r.get('row')}: " + " | ".join(_cell(r, i)[0] for i in cols) for r in shown_rows]
        if len(matched) > len(shown_rows):
            lines.append(f"First {len(shown_rows)} of {len(matched)} matching rows shown:")
        lines.append(f"Columns: {header}")
        lines.extend(listing)
    elif query.group_by is not None:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        shown_names: Dict[str, str] = {}
        for r in matched:
            display = _cell(r, query.group_by)[0].strip() or "(empty)"
            key = _norm(display)
            groups.setdefault(key, []).append(r)
            shown_names.setdefault(key, display)
        name = f'"{ref.columns[query.group_by].get("name")}"'
        parts = []
        for key, grows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:GROUP_LIMIT]:
            phrase, miss = _aggregate(ref, query, grows)
            missing += miss
            parts.append(f"{shown_names[key]}: {phrase}")
        more = f" ({len(groups) - GROUP_LIMIT} more groups not listed)" if len(groups) > GROUP_LIMIT else ""
        lines.append(f"By {name}: " + "; ".join(parts) + more + ".")
    else:
        phrase, missing = _aggregate(ref, query, matched)
        if op != "count":
            lines.append(f"Result: {phrase}.")
    if missing:
        notes.append(f"{missing} matching row(s) had no usable value in the computed column and were left out")
    if notes:
        lines.append("Note: " + "; ".join(notes) + ".")
    return Result(
        query=query,
        text="\n".join(lines),
        row_start=data[0].get("row") if data else None,
        row_end=data[-1].get("row") if data else None,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def answer_table_questions(
    *,
    workspace_id: str,
    question: str,
    previous_user_turn: str,
    annexure_of: Dict[str, int],
    names: Dict[str, str],
) -> TableAnswers:
    """Compute what the question asks of the workspace's spreadsheets.

    Never raises: the caller's chat turn must not fail because a calculation
    did. Failures come back as ``notes`` the prompt states plainly.
    """
    from app.vectorstore.qdrant import vector_store

    out = TableAnswers()
    try:
        manifests = await vector_store.get_table_manifests(workspace_id)
    except Exception as err:
        logger.warning("Spreadsheet manifests unavailable for workspace {}: {}", workspace_id, err)
        return out
    refs = table_refs(manifests, annexure_of, names)
    out.has_tables = bool(refs)
    if not needs_calculation(question, refs):
        return out

    specs = await plan_queries(question, previous_user_turn, refs)
    if specs is None:
        out.notes.append(
            "The system could not work out a calculation over the spreadsheet(s) for this question."
        )
        return out
    rows_cache: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for spec in specs:
        try:
            query = validate(spec, refs)
        except SpecError as bad:
            logger.info("Spreadsheet query rejected: {}", bad)
            out.notes.append(f"A requested calculation could not be run: {bad}.")
            continue
        key = (query.ref.document_id, int(query.ref.sheet.get("sheet_index") or 0))
        if key not in rows_cache:
            try:
                rows_cache[key] = await vector_store.get_table_rows(workspace_id, *key)
            except Exception as err:
                logger.warning("Spreadsheet rows unavailable for document {}: {}", key[0], err)
                out.notes.append("The spreadsheet rows could not be read, so nothing was computed.")
                continue
        try:
            out.results.append(execute(query, rows_cache[key]))
        except SpecError as bad:
            out.notes.append(f"A requested calculation could not be run: {bad}.")
        except Exception as err:  # a bug here must not cost the lawyer the answer
            logger.opt(exception=err).error("Spreadsheet calculation failed: {}", err)
            out.notes.append("A requested calculation failed, so nothing was computed for it.")
    logger.info(
        "Spreadsheet calculations: {} planned, {} computed, {} note(s)",
        len(specs),
        len(out.results),
        len(out.notes),
    )
    return out
