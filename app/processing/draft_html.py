"""Rich-text editor HTML → draft Markdown (SIM-216).

Drafts saved by the in-app editor reach ``draft_docx_build_request`` as HTML.
The Word renderer (``app.processing.docx_generator``) reads Markdown, so the
HTML is turned into the same Markdown structure the drafting model writes:
headings, paragraphs, bold/italic, nested bullet and numbered lists, GFM
tables and line breaks. A table, heading or nested list then comes out of the
renderer exactly as it would from the equivalent Markdown.

The HTML is never trusted:

- ``script``, ``style``, ``iframe`` and the other active/embedded elements are
  dropped with everything inside them;
- unknown tags are unwrapped (their text is kept, the tag is not);
- no attribute is ever copied (``ol start`` is read as an integer, nothing
  else is read at all), so no link, style or event handler survives;
- nesting deeper than ``_MAX_DEPTH`` is flattened.

``<Label>`` fill-ins survive whether the editor escaped them
(``&lt;Bank Name&gt;``) or not (``<Bank Name>``, which an HTML parser would
otherwise read as an unknown tag and drop).

Pure, never calls a model, uses only the standard library parser.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple, Union

from app.processing.draft_markdown import PLACEHOLDER_RE, is_placeholder

# Elements dropped together with their content.
_DROP = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet",
    "noscript", "template", "svg", "math", "canvas", "audio", "video", "head",
    "select", "textarea", "button", "input", "meta", "link", "base",
})
# Parsed as raw text by the HTML parser: never read as a ``<Label>``.
_RAW_TEXT = frozenset({"script", "style"})
# Elements with no closing tag.
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LISTS = frozenset({"ul", "ol"})
_TABLE_SECTIONS = frozenset({"thead", "tbody", "tfoot"})
_CELLS = frozenset({"td", "th"})
_BOLD = frozenset({"strong", "b"})
_ITALIC = frozenset({"em", "i"})
# Block containers that only separate paragraphs.
_GENERIC_BLOCKS = frozenset({
    "div", "section", "article", "main", "header", "footer", "nav", "aside",
    "figure", "figcaption", "center", "address", "details", "summary", "dl",
    "dt", "dd", "body", "html", "caption",
})
# Elements kept as nodes in the tree. Everything else that is not dropped is
# unwrapped: its children stay, the element does not.
_STRUCTURAL = (
    frozenset({"p", "blockquote", "pre", "li", "table", "tr", "br", "hr"})
    | _HEADINGS | _LISTS | _TABLE_SECTIONS | _CELLS | _BOLD | _ITALIC | _GENERIC_BLOCKS
)
_BLOCKS = (
    frozenset({"p", "blockquote", "pre", "li", "table", "tr", "hr"})
    | _HEADINGS | _LISTS | _TABLE_SECTIONS | _CELLS | _GENERIC_BLOCKS
)
# Tag names a raw ``<Word ...>`` is never mistaken for a placeholder under,
# unless the author capitalised it (editors write tags in lower case).
_KNOWN_HTML = _DROP | _VOID | _STRUCTURAL | frozenset({
    "a", "abbr", "b", "bdi", "bdo", "big", "cite", "code", "data", "del", "dfn",
    "font", "ins", "kbd", "label", "mark", "q", "s", "samp", "small", "span",
    "strike", "sub", "sup", "time", "tt", "u", "var", "colgroup", "picture",
    "title", "form", "option",
})

_MAX_DEPTH = 64
_RAW_PLACEHOLDER_RE = re.compile(r"^<[A-Za-z][^<>\"'=\n/:]{0,79}>$")
_BREAK = "\x00"  # block boundary inside inline text; becomes <br> or a space
_WS_RE = re.compile(r"\s+")


class _Node:
    __slots__ = ("tag", "children", "start")

    def __init__(self, tag: str, start: int = 1) -> None:
        self.tag = tag
        self.children: List[Union["_Node", str]] = []
        self.start = start


class _TreeBuilder(HTMLParser):
    """Build a small, sanitised element tree from untrusted HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack: List[_Node] = [self.root]
        self.skipping: List[str] = []

    # -- helpers -----------------------------------------------------------

    def _top(self) -> _Node:
        return self.stack[-1]

    def _text(self, text: str) -> None:
        kids = self._top().children
        if kids and isinstance(kids[-1], str):
            kids[-1] += text
        else:
            kids.append(text)

    def _pop_to(self, tags: frozenset, *, stop: frozenset) -> bool:
        """Close up to and including the nearest open ``tags`` element, unless
        a ``stop`` element is nearer. True when something was closed."""
        for i in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[i].tag
            if tag in tags:
                del self.stack[i:]
                return True
            if tag in stop:
                return False
        return False

    def _implicit_close(self, tag: str) -> None:
        if tag in _BLOCKS:
            # A block closes an open <p>, even through open bold/italic.
            i = len(self.stack) - 1
            while i > 0 and self.stack[i].tag in (_BOLD | _ITALIC):
                i -= 1
            if i > 0 and self.stack[i].tag == "p":
                del self.stack[i:]
        if tag == "li":
            self._pop_to(frozenset({"li"}), stop=_LISTS | {"table"})
        elif tag == "tr":
            self._pop_to(frozenset({"tr"}), stop=frozenset({"table"}))
        elif tag in _CELLS:
            self._pop_to(_CELLS, stop=frozenset({"tr", "table"}))
        elif tag in _TABLE_SECTIONS:
            self._pop_to(_TABLE_SECTIONS, stop=frozenset({"table"}))

    @staticmethod
    def _is_raw_placeholder(tag: str, raw: str) -> bool:
        if tag in _RAW_TEXT or not raw or not _RAW_PLACEHOLDER_RE.match(raw):
            return False
        name = raw[1:].split(None, 1)[0].rstrip(">")
        if tag in _KNOWN_HTML and (name.islower() or name.isupper()):
            return False
        return is_placeholder(raw[1:-1])

    # -- HTMLParser hooks --------------------------------------------------

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.skipping:
            if tag not in _VOID:
                self.skipping.append(tag)
            return
        raw = self.get_starttag_text() or ""
        if self._is_raw_placeholder(tag, raw):
            self._text(raw)
            return
        if tag in _DROP:
            if tag not in _VOID:
                self.skipping.append(tag)
            return
        if tag not in _STRUCTURAL:
            return  # unwrapped: children land in the current node
        self._implicit_close(tag)
        if tag in ("br", "hr"):
            self._top().children.append(_Node(tag))
            return
        if len(self.stack) > _MAX_DEPTH:
            return
        start = 1
        if tag == "ol":
            for key, value in attrs:
                if key == "start" and value and value.strip().isdigit():
                    start = max(1, min(int(value.strip()), 999))
        node = _Node(tag, start)
        self._top().children.append(node)
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.skipping:
            return
        if tag in ("br", "hr"):
            self._implicit_close(tag)
            self._top().children.append(_Node(tag))

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag in self.skipping:
                while self.skipping and self.skipping.pop() != tag:
                    pass
            return
        if tag not in _STRUCTURAL or tag in _VOID:
            return
        self._pop_to(frozenset({tag}), stop=frozenset())

    def handle_data(self, data: str) -> None:
        if not self.skipping and data:
            self._text(data)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

_LINE_START_RE = re.compile(r"^(#{1,6}(?=\s|$)|>|[-+](?=\s)|=+|(\d{1,9})([.)])(?=\s|$))")


def _escape(text: str) -> str:
    """Escape Markdown syntax in plain text; ``<Label>`` fill-ins stay as is."""
    out: List[str] = []
    pos = 0
    for m in PLACEHOLDER_RE.finditer(text):
        if not is_placeholder(m.group(1)):
            continue
        out.append(_escape_plain(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_plain(text[pos:]))
    return "".join(out)


def _escape_plain(text: str) -> str:
    text = text.replace("\\", "\\\\")
    for ch in ("*", "_", "`", "|"):
        text = text.replace(ch, "\\" + ch)
    return text


def _escape_line_start(line: str) -> str:
    m = _LINE_START_RE.match(line)
    if not m:
        return line
    if m.group(2) is not None:  # "1. " would start a numbered list
        return m.group(2) + "\\" + m.group(3) + line[m.end():]
    return "\\" + line


def _wrap(inner: str, mark: str) -> str:
    """``**text**`` with the whitespace kept outside the markers."""
    core = inner.strip(" ")
    if not core or _BREAK in core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip(" "))]
    trail = inner[len(inner.rstrip(" ")):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _inline_raw(node: _Node) -> str:
    parts: List[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(_escape(_WS_RE.sub(" ", child)))
            continue
        tag = child.tag
        if tag == "br":
            parts.append("<br>")
        elif tag == "hr":
            parts.append(_BREAK)
        elif tag in _BOLD:
            parts.append(_wrap(_inline_raw(child), "**"))
        elif tag in _ITALIC:
            parts.append(_wrap(_inline_raw(child), "*"))
        elif tag in _BLOCKS:
            parts.append(_BREAK + _inline_raw(child) + _BREAK)
        else:
            parts.append(_inline_raw(child))
    return "".join(parts)


def _inline(node: _Node, *, join: str = "<br>") -> str:
    """One line of Markdown for ``node``'s content; block boundaries inside it
    become ``join``."""
    text = _WS_RE.sub(" ", _inline_raw(node))
    segments = [s.strip() for s in text.split(_BREAK)]
    line = join.join(s for s in segments if s)
    line = re.sub(r"\s*<br>\s*", "<br>", line)
    line = re.sub(r"^(?:<br>)+|(?:<br>)+$", "", line)
    return _escape_line_start(line)


def _list_lines(node: _Node, indent: int) -> List[str]:
    ordered = node.tag == "ol"
    number = node.start if (ordered and indent == 0) else 1
    lines: List[str] = []
    last_content_indent = indent + 2
    for child in node.children:
        if isinstance(child, str):
            if not child.strip():
                continue
            item = _Node("li")
            item.children.append(child)
            child = item
        if child.tag in _LISTS:  # <ul><li/><ul>…</ul></ul>: belongs to the item above
            lines.extend(_list_lines(child, last_content_indent))
            continue
        if child.tag != "li":
            wrapper = _Node("li")
            wrapper.children.append(child)
            child = wrapper
        marker = f"{number}." if ordered else "-"
        number += 1
        content_indent = indent + len(marker) + 1
        last_content_indent = content_indent
        text_holder = _Node("li")
        nested: List[str] = []
        for part in child.children:
            if isinstance(part, _Node) and part.tag in _LISTS:
                nested.extend(_list_lines(part, content_indent))
            else:
                text_holder.children.append(part)
        text = _inline(text_holder)
        lines.append(" " * indent + marker + (" " + text if text else ""))
        lines.extend(nested)
    return lines


def _table_lines(node: _Node) -> Tuple[List[str], List[str]]:
    """GFM table lines, plus any caption paragraphs found in the table."""
    rows: List[Tuple[List[str], bool]] = []
    captions: List[str] = []

    def walk(n: _Node, in_head: bool) -> None:
        for child in n.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == "tr":
                cells = [c for c in child.children
                         if isinstance(c, _Node) and c.tag in _CELLS]
                texts = [_inline(c) for c in cells]
                header = in_head or (bool(cells) and all(c.tag == "th" for c in cells))
                if texts:
                    rows.append((texts, header))
            elif child.tag in _TABLE_SECTIONS:
                walk(child, child.tag == "thead")
            elif child.tag == "caption":
                cap = _inline(child)
                if cap:
                    captions.append(cap)
            elif child.tag in _CELLS:  # a cell outside any row
                rows.append(([_inline(child)], in_head))
            else:
                walk(child, in_head)

    walk(node, False)
    if not rows:
        return [], captions
    ncols = max(len(cells) for cells, _ in rows)
    if rows[0][1]:
        header, body = rows[0][0], [cells for cells, _ in rows[1:]]
    else:
        # No header row: an all-empty one, which the renderer drops (as it
        # does for the same Markdown).
        header, body = [""] * ncols, [cells for cells, _ in rows]

    def fmt(cells: List[str]) -> str:
        cells = cells + [""] * (ncols - len(cells))
        return "| " + " | ".join(cells) + " |"

    lines = [fmt(header), fmt(["---"] * ncols)]
    lines.extend(fmt(cells) for cells in body)
    return lines, captions


def _blocks(node: _Node, out: List[str]) -> None:
    """Append ``node``'s children to ``out`` as Markdown blocks."""
    buffer = _Node("p")

    def flush() -> None:
        if buffer.children:
            text = _inline(buffer)
            if text:
                out.append(text)
            buffer.children.clear()

    for child in node.children:
        if isinstance(child, str) or child.tag not in _BLOCKS:
            buffer.children.append(child)
            continue
        flush()
        tag = child.tag
        if tag in _HEADINGS:
            text = _inline(child, join=" ")
            if text:
                out.append("#" * int(tag[1]) + " " + text)
        elif tag in _LISTS:
            lines = _list_lines(child, 0)
            if lines:
                out.append("\n".join(lines))
        elif tag == "table":
            lines, captions = _table_lines(child)
            out.extend(captions)
            if lines:
                out.append("\n".join(lines))
        elif tag == "blockquote":
            inner: List[str] = []
            _blocks(child, inner)
            if inner:
                quoted = "\n\n".join(inner).split("\n")
                out.append("\n".join(("> " + ln) if ln else ">" for ln in quoted))
        elif tag == "pre":
            text = _pre_text(child)
            if text:
                out.append(text)
        elif tag == "hr":
            continue  # the renderer drops rules; so does the normaliser
        else:  # p, div-like containers, stray li/tr/cells
            _blocks(child, out)
    flush()


def _pre_text(node: _Node) -> str:
    def raw(n: _Node) -> str:
        parts = []
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag == "br":
                parts.append("\n")
            else:
                parts.append(raw(c))
        return "".join(parts)

    lines = [ln.strip() for ln in raw(node).strip("\n").split("\n")]
    return _escape_line_start("<br>".join(_escape(ln) for ln in lines).strip())


def html_to_markdown(html: str) -> str:
    """Convert editor HTML to draft Markdown. Never raises on malformed HTML."""
    if not html or not html.strip():
        return ""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    blocks: List[str] = []
    _blocks(builder.root, blocks)
    return "\n\n".join(b for b in blocks if b.strip())
