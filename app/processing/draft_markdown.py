"""Deterministic clean-up of the drafting model's Markdown (SIM-202).

The draft render step asks the model for one Markdown document. What comes
back is close to GFM but not quite, and every renderer downstream (the
python-docx exporter here, the chat preview, the backend) trips on the same
defects:

- table rows separated by blank lines, which ends a GFM table after its
  header, so every row renders as a line of ``| … |`` text;
- a missing separator row, or rows with more or fewer cells than the header;
- escaped placeholders (``\\<Bank Name\\>``) that show their backslashes;
- headings deeper than ``###`` that some renderers print as ``####`` text;
- stand-alone ``---`` rules that come out as literal dashes;
- ``(a)`` sub-clauses written under a list item without a list marker, which
  CommonMark folds into the parent item's text.

``normalize_draft_markdown`` repairs those, and only those, line by line. It
is pure, idempotent and never calls a model; fenced code is left untouched.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# A placeholder is ``<Label>``: starts with a letter, no nested brackets, no
# newline. HTML-looking tags are excluded (``html`` is off in the renderer,
# but a stray ``<br>`` must not be counted or highlighted as a fill-in).
PLACEHOLDER_RE = re.compile(r"<([A-Za-z][^<>\n]{0,79})>")
_HTML_TAGS = frozenset({
    "br", "br/", "b", "i", "u", "p", "em", "strong", "sup", "sub", "span", "div",
    "hr", "hr/", "table", "tr", "td", "th", "ul", "ol", "li", "small", "s",
})

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_ESCAPED_PLACEHOLDER_RE = re.compile(r"\\?<((?:[^<>\n\\]|\\[_*])+?)\\>|\\<((?:[^<>\n\\]|\\[_*])+?)>")
_DEEP_HEADING_RE = re.compile(r"^(\s{0,3})#{4,6}(\s+)")
_RULE_RE = re.compile(r"^\s{0,3}([-*])(?:\s*\1){2,}\s*$")
_TABLE_LINE_RE = re.compile(r"^\s{0,3}\|")
_SEPARATOR_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])(\s+)\S")
_SUBCLAUSE_RE = re.compile(r"^(\s+)(\((?:[a-z]|[ivxl]{1,5})\)\s+\S.*)$")


def is_placeholder(label: str) -> bool:
    """True when ``<label>`` is a fill-in, not an HTML-looking tag."""
    return label.strip().lower() not in _HTML_TAGS


def count_placeholders(markdown: str) -> int:
    """Number of distinct ``<Label>`` fill-ins left in the draft."""
    seen = {
        m.group(1).strip().lower()
        for m in PLACEHOLDER_RE.finditer(markdown or "")
        if is_placeholder(m.group(1))
    }
    return len(seen)


def _unescape_placeholders(line: str) -> str:
    def _sub(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        inner = inner.replace("\\_", "_").replace("\\*", "*")
        return "<" + inner + ">"

    return _ESCAPED_PLACEHOLDER_RE.sub(_sub, line)


def _split_cells(line: str) -> List[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip() for c in _CELL_SPLIT_RE.split(body)]


def _is_separator(cells: List[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(c) for c in cells)


def _join_row(cells: List[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _rebuild_table(rows: List[str]) -> List[str]:
    """Rebuild a run of pipe lines as one well-formed GFM table."""
    parsed = [_split_cells(r) for r in rows]
    header = parsed[0]
    ncols = len(header)
    out = [_join_row(header)]
    body = parsed[1:]
    if body and _is_separator(body[0]):
        sep = body[0][:ncols] + ["---"] * max(0, ncols - len(body[0]))
        body = body[1:]
    else:
        sep = ["---"] * ncols
    out.append(_join_row(sep))
    for cells in body:
        if _is_separator(cells):
            continue  # a stray second separator from a re-joined block
        if len(cells) > ncols:
            extra = [c for c in cells[ncols:] if c]
            cells = cells[:ncols]
            if extra:
                cells[-1] = " ".join([cells[-1], *extra]).strip()
        elif len(cells) < ncols:
            cells = cells + [""] * (ncols - len(cells))
        out.append(_join_row(cells))
    return out


def _collect_table(lines: List[str], start: int) -> Tuple[List[str], int]:
    """From ``start`` (a pipe line), gather pipe lines separated only by blanks.

    Returns the pipe lines and the index after the last one consumed.
    Trailing blank lines are not consumed.
    """
    rows = [lines[start]]
    i = start + 1
    last = start
    while i < len(lines):
        if _TABLE_LINE_RE.match(lines[i]):
            rows.append(lines[i])
            last = i
            i += 1
            continue
        if not lines[i].strip():
            i += 1
            continue
        break
    return rows, last + 1


def _list_content_indent(line: str) -> Optional[int]:
    m = _LIST_ITEM_RE.match(line)
    if not m:
        return None
    return len(m.group(1)) + len(m.group(2)) + len(m.group(3))


def normalization_enabled() -> bool:
    """``DRAFT_MARKDOWN_NORMALIZE`` — the SIM-202 rollback switch."""
    from app.core.config import settings

    return bool(getattr(settings, "draft_markdown_normalize", True))


def normalize_if_enabled(markdown: str) -> str:
    """``normalize_draft_markdown`` unless the rollback switch is off."""
    return normalize_draft_markdown(markdown) if normalization_enabled() else markdown


def normalize_draft_markdown(markdown: str) -> str:
    """Repair the model's draft Markdown so every renderer reads it the same way.

    Idempotent: ``normalize(normalize(x)) == normalize(x)``.
    """
    if not markdown:
        return markdown or ""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    in_fence = False
    # Content indent of the most recent list item, for (a) sub-clauses.
    parent_indent: Optional[int] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        line = _unescape_placeholders(line)

        # Tables: a run of pipe lines, possibly broken up by blank lines.
        if _TABLE_LINE_RE.match(line):
            rows, nxt = _collect_table(lines, i)
            rows = [_unescape_placeholders(r) for r in rows]
            if len(rows) >= 2:
                if out and out[-1].strip():
                    out.append("")
                out.extend(_rebuild_table(rows))
                out.append("")
                parent_indent = None
                i = nxt
                continue
            out.append(line)
            i += 1
            continue

        # Stand-alone rules: drop them when they cannot be a setext underline.
        if _RULE_RE.match(line) and (not out or not out[-1].strip()):
            i += 1
            continue

        line = _DEEP_HEADING_RE.sub(r"\1###\2", line)

        # "(a) …" indented under a list item without its own marker.
        indent = _list_content_indent(line)
        if indent is not None:
            parent_indent = indent
        elif parent_indent is not None:
            sub = _SUBCLAUSE_RE.match(line)
            if sub:
                line = " " * parent_indent + "- " + sub.group(2)
            elif line.strip() and not line.startswith(" "):
                parent_indent = None

        out.append(line)
        i += 1

    # Collapse runs of blank lines (outside fences) to one.
    collapsed: List[str] = []
    in_fence = False
    for line in out:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence and not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line if line.strip() or in_fence else "")
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    return "\n".join(collapsed)
