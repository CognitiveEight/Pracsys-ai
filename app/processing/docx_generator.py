"""Markdown → DOCX renderer for the drafting assistant.

Two output paths:

1. **Template-skeleton path** (lawyer attached a ``.docx`` template):
   We clone the original template (preserving ``styles.xml``,
   ``numbering.xml``, headers/footers, theme, section properties), clear
   its body, then walk the AI-produced Markdown block-by-block. Each
   block is mapped onto the template's style for the corresponding
   section — heading-anchored fuzzy match locates the template paragraph
   whose style we should inherit, so AI-filled facts visually match the
   surrounding template prose.

2. **Defaults path** (no template — ``suggestedDraft`` / ``blank``):
   A fresh ``Document`` styled for Indian-court filings — Times New
   Roman 12 pt, justified, 1.5 line spacing, 1-inch margins, bold
   headings.

Both paths return the serialised ``.docx`` bytes via ``BytesIO``.

Failure modes are kept narrow: an exception in the template path falls
back to the defaults path (logged); an exception in markdown parsing
falls back to plain ``\\n\\n``-split paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from docx import Document
from docx.document import Document as _DocxDocument
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor

from app.processing.draft_markdown import PLACEHOLDER_RE, is_placeholder


# ---------------------------------------------------------------------------
# Block AST
# ---------------------------------------------------------------------------


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    # A Word line break (Markdown hard break, or a soft break inside a block
    # of short lines such as a signature block). ``text`` is empty.
    br: bool = False


@dataclass
class Heading:
    level: int
    runs: List[Run] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class Paragraph:
    runs: List[Run] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class ListItem:
    runs: List[Run] = field(default_factory=list)
    # Nested lists under this item (``BulletList`` / ``OrderedList``).
    children: List["Block"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class BulletList:
    items: List[ListItem] = field(default_factory=list)


@dataclass
class OrderedList:
    items: List[ListItem] = field(default_factory=list)
    start: int = 1
    delimiter: str = "."


@dataclass
class Table:
    rows: List[List[List[Run]]] = field(default_factory=list)  # rows[r][c] = runs
    # Per-column alignment from the separator row: "left" | "center" | "right".
    aligns: List[Optional[str]] = field(default_factory=list)
    has_header: bool = True


Block = object  # union: Heading | Paragraph | BulletList | OrderedList | Table


# ---------------------------------------------------------------------------
# Markdown parser (markdown-it-py)
# ---------------------------------------------------------------------------


_UNDERSCORE_LINE_RE = re.compile(r"^(\s{0,3})(_{3,})(\s*)$")


def _escape_inline_signature_lines(md: str) -> str:
    """Keep a "_____" signature line inside its signature block.

    CommonMark reads a line of underscores as a thematic break, which cut a
    signature block into three paragraphs (name / line / witness) that could
    land on different pages. When the line sits directly after a text line,
    its underscores are escaped so it stays a line of that paragraph.
    Rendering only; the stored draft is unchanged.
    """
    lines = md.split("\n")
    for i, line in enumerate(lines):
        m = _UNDERSCORE_LINE_RE.match(line)
        if m and i > 0 and lines[i - 1].strip():
            lines[i] = m.group(1) + "\\_" * len(m.group(2)) + m.group(3)
    return "\n".join(lines)


def parse_markdown_blocks(md: str) -> List[Block]:
    """Parse Markdown text into a flat sequence of typed blocks.

    Falls back to ``\\n\\n``-split plain paragraphs if ``markdown-it-py``
    raises or is unavailable.
    """
    md = (md or "").strip()
    if not md:
        return []
    md = _escape_inline_signature_lines(md)
    try:
        from markdown_it import MarkdownIt

        # The commonmark preset ships with GFM tables switched OFF; without
        # this every pipe table in a draft was rendered as lines of text.
        parser = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable("table")
        tokens = parser.parse(md)
        return list(_tokens_to_blocks(tokens))
    except Exception as exc:
        logger.warning("Markdown parse failed, falling back to plain split: {}", exc)
        return [Paragraph(runs=[Run(text=chunk.strip())])
                for chunk in md.split("\n\n") if chunk.strip()]


# A soft break becomes a Word line break when every line of the block is
# short: signature blocks, addresses and "Name: / Address:" stacks are written
# one item per line, and joining them into one line ruins them. Prose lines
# are long, so a wrapped prose paragraph still joins with spaces.
_SHORT_LINE_CHARS = 100
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _short_line_block(children) -> bool:
    lines: List[str] = [""]
    for ch in children:
        if ch.type in ("softbreak", "hardbreak"):
            lines.append("")
        else:
            lines[-1] += getattr(ch, "content", "") or ""
    return len(lines) > 1 and all(len(s.strip()) < _SHORT_LINE_CHARS for s in lines)


def _inline_runs(token) -> List[Run]:
    """Walk an inline token's children → list[Run]."""
    runs: List[Run] = []
    bold_depth = 0
    italic_depth = 0
    children = token.children or []
    soft_as_break = _short_line_block(children)
    for ch in children:
        t = ch.type
        if t == "strong_open":
            bold_depth += 1
        elif t == "strong_close":
            bold_depth = max(0, bold_depth - 1)
        elif t == "em_open":
            italic_depth += 1
        elif t == "em_close":
            italic_depth = max(0, italic_depth - 1)
        elif t == "text":
            # "<br>" is how a model puts a line break inside a table cell
            # (an address in an addressee block). HTML is off in the parser,
            # so it arrives as text; turn it into a real line break.
            for k, piece in enumerate(_BR_TAG_RE.split(ch.content or "")):
                if k:
                    runs.append(Run(text="", br=True))
                if piece:
                    runs.append(Run(text=piece,
                                    bold=bold_depth > 0,
                                    italic=italic_depth > 0))
        elif t == "code_inline":
            if ch.content:
                runs.append(Run(text=ch.content,
                                bold=bold_depth > 0,
                                italic=italic_depth > 0))
        elif t == "hardbreak" or (t == "softbreak" and soft_as_break):
            runs.append(Run(text="", br=True))
        elif t == "softbreak":
            runs.append(Run(text=" ",
                            bold=bold_depth > 0,
                            italic=italic_depth > 0))
        # other inline types (links, images) — flatten to plain text
        elif hasattr(ch, "content") and ch.content:
            runs.append(Run(text=ch.content,
                            bold=bold_depth > 0,
                            italic=italic_depth > 0))
    return runs or [Run(text="")]


def _tokens_to_blocks(tokens) -> Iterable[Block]:
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        t = tok.type
        if t == "heading_open":
            level = int(tok.tag[1:]) if tok.tag.startswith("h") else 1
            inline = tokens[i + 1] if i + 1 < n else None
            runs = _inline_runs(inline) if inline and inline.type == "inline" else []
            yield Heading(level=level, runs=runs)
            i += 3  # heading_open, inline, heading_close
            continue
        if t == "paragraph_open":
            inline = tokens[i + 1] if i + 1 < n else None
            runs = _inline_runs(inline) if inline and inline.type == "inline" else []
            yield Paragraph(runs=runs)
            i += 3
            continue
        if t in ("bullet_list_open", "ordered_list_open"):
            block, i = _parse_list(tokens, i)
            yield block
            continue
        if t == "table_open":
            rows: List[List[List[Run]]] = []
            aligns: List[Optional[str]] = []
            current_row: List[List[Run]] = []
            has_header = False
            i += 1
            while i < n and tokens[i].type != "table_close":
                tt = tokens[i].type
                if tt == "thead_open":
                    has_header = True
                elif tt == "tr_open":
                    current_row = []
                elif tt in ("th_open", "td_open"):
                    inline = tokens[i + 1] if i + 1 < n else None
                    cell_runs = (_inline_runs(inline)
                                 if inline and inline.type == "inline" else [])
                    current_row.append(cell_runs)
                    if tt == "th_open":
                        aligns.append(_cell_alignment(tokens[i]))
                elif tt == "tr_close":
                    rows.append(current_row)
                i += 1
            # GFM always has a header row. An all-empty one ("|  |  |") means
            # the table has none — an addressee block, a party grid — so drop
            # it rather than print a grey, empty first row.
            if rows and has_header and not any(
                    "".join(r.text for r in cell).strip() for cell in rows[0]):
                rows = rows[1:]
                has_header = False
            yield Table(rows=rows, aligns=aligns, has_header=has_header)
            i += 1  # skip table_close
            continue
        if t == "fence" or t == "code_block":
            yield Paragraph(runs=[Run(text=tok.content or "")])
            i += 1
            continue
        if t == "hr":
            # "_____" on its own line is a signature line, which CommonMark
            # also reads as a thematic break; keep it as written. "---" and
            # "***" rules are dropped: headings already separate the parts of
            # a draft, and a row of dashes is never wanted in a legal document.
            if (tok.markup or "").startswith("_"):
                yield Paragraph(runs=[Run(text="_" * max(len(tok.markup or ""), 30))])
            i += 1
            continue
        # Unknown — skip
        i += 1


def _cell_alignment(token) -> Optional[str]:
    style = token.attrGet("style") or ""
    m = re.search(r"text-align:\s*(left|center|right)", style)
    return m.group(1) if m else None


def _parse_list(tokens, i: int) -> Tuple[Block, int]:
    """Parse a (possibly nested) list starting at ``tokens[i]``.

    Returns the block and the index just past its ``*_list_close``.
    """
    open_tok = tokens[i]
    ordered = open_tok.type == "ordered_list_open"
    close_type = "ordered_list_close" if ordered else "bullet_list_close"
    items: List[ListItem] = []
    n = len(tokens)
    i += 1
    while i < n and tokens[i].type != close_type:
        if tokens[i].type != "list_item_open":
            i += 1
            continue
        item = ListItem()
        i += 1
        while i < n and tokens[i].type != "list_item_close":
            tt = tokens[i].type
            if tt in ("bullet_list_open", "ordered_list_open"):
                child, i = _parse_list(tokens, i)
                item.children.append(child)
                continue
            if tt == "inline":
                if item.runs:
                    item.runs.append(Run(text="", br=True))
                item.runs.extend(_inline_runs(tokens[i]))
            i += 1
        if not item.runs:
            item.runs = [Run(text="")]
        items.append(item)
        i += 1  # skip list_item_close
    i += 1  # skip *_list_close
    if ordered:
        try:
            start = int(open_tok.attrGet("start") or 1)
        except (TypeError, ValueError):
            start = 1
        delimiter = ")" if (open_tok.markup or ".") == ")" else "."
        return OrderedList(items=items, start=start, delimiter=delimiter), i
    return BulletList(items=items), i


# ---------------------------------------------------------------------------
# Style snapshot
# ---------------------------------------------------------------------------


@dataclass
class RunStyle:
    font_name: Optional[str] = None
    font_size_pt: Optional[float] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None


@dataclass
class ParaStyle:
    style_name: Optional[str] = None
    alignment: Optional[int] = None
    is_heading: bool = False
    has_numbering: bool = False
    run_style: RunStyle = field(default_factory=RunStyle)
    text_norm: str = ""


def _normalize_text(s: str) -> str:
    import re

    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _snapshot_run_style(paragraph) -> RunStyle:
    """Capture the first non-empty run's formatting."""
    rs = RunStyle()
    for r in paragraph.runs:
        if not r.text:
            continue
        try:
            rs.font_name = r.font.name
            rs.font_size_pt = (r.font.size.pt if r.font.size else None)
            rs.bold = r.font.bold
            rs.italic = r.font.italic
        except Exception:
            pass
        break
    return rs


def _has_numbering(paragraph) -> bool:
    try:
        pPr = paragraph._p.find(qn("w:pPr"))
        if pPr is None:
            return False
        return pPr.find(qn("w:numPr")) is not None
    except Exception:
        return False


def _build_template_index(tpl_doc: _DocxDocument) -> List[ParaStyle]:
    out: List[ParaStyle] = []
    for p in tpl_doc.paragraphs:
        style_name = None
        try:
            style_name = p.style.name if p.style else None
        except Exception:
            style_name = None
        is_heading = bool(style_name and "heading" in style_name.lower())
        out.append(ParaStyle(
            style_name=style_name,
            alignment=p.alignment,
            is_heading=is_heading,
            has_numbering=_has_numbering(p),
            run_style=_snapshot_run_style(p),
            text_norm=_normalize_text(p.text),
        ))
    return out


def _fuzzy_find_heading(needle: str, tpl_index: List[ParaStyle]) -> Optional[int]:
    """Locate the template paragraph whose text best matches ``needle``."""
    if not needle:
        return None
    norm = _normalize_text(needle)
    if not norm:
        return None
    # exact / substring match first
    for idx, ps in enumerate(tpl_index):
        if not ps.is_heading:
            continue
        if ps.text_norm == norm or norm in ps.text_norm or ps.text_norm in norm:
            return idx
    # fuzzy fallback
    try:
        from rapidfuzz import fuzz

        best_idx = None
        best_score = 0
        for idx, ps in enumerate(tpl_index):
            if not ps.is_heading or not ps.text_norm:
                continue
            score = fuzz.partial_ratio(norm, ps.text_norm)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_score >= 75:
            return best_idx
    except Exception:
        pass
    return None


def _body_style_after(tpl_index: List[ParaStyle], heading_idx: int) -> Optional[ParaStyle]:
    for j in range(heading_idx + 1, len(tpl_index)):
        ps = tpl_index[j]
        if ps.is_heading:
            break
        if ps.text_norm:
            return ps
    return None


_PLACEHOLDER_RE = __import__("re").compile(r"\[[^\]\n]+\]")
_FILL_OPENERS = (
    "in the court of",
    "before the",
    "in the high court",
    "before the hon",
)


_PREAMBLE_LOOKAHEAD = 10


def _find_preamble_end(tpl_index: List[ParaStyle]) -> int:
    """Locate where the template's preamble ends and the fill area begins.

    Preamble paragraphs (title labels, lawyer-facing notes, headers that
    sit above the actual draft body) are kept verbatim in the generated
    DOCX. We treat the first paragraph that either contains a
    ``[placeholder]`` token or starts with a recognised court-line opener
    as the beginning of the fill area; everything before it is preamble.

    Only the first ``_PREAMBLE_LOOKAHEAD`` paragraphs are searched — past
    that point we conservatively assume the document has no separable
    preamble and the whole body is fill area (return 0). This prevents
    treating a 300-paragraph template with no marker as one giant
    preamble that would then be duplicated in the output.
    """
    horizon = min(_PREAMBLE_LOOKAHEAD, len(tpl_index))
    for i in range(horizon):
        text = tpl_index[i].text_norm or ""
        if not text:
            continue
        if _PLACEHOLDER_RE.search(text):
            return i
        if any(text.startswith(opener) for opener in _FILL_OPENERS):
            return i
    return 0


def _pick_default_body_style(tpl_index: List[ParaStyle]) -> Optional[ParaStyle]:
    """Pick a sensible 'body Normal' style from the template.

    Used as a fallback inherit-style when an AI paragraph doesn't fall
    under any matched heading. We prefer the longest non-heading
    paragraph since it most likely represents the template's intended
    body voice (font, alignment, etc.).
    """
    best: Optional[ParaStyle] = None
    best_len = 0
    for ps in tpl_index:
        if ps.is_heading or not ps.text_norm:
            continue
        if len(ps.text_norm) > best_len:
            best = ps
            best_len = len(ps.text_norm)
    return best


# ---------------------------------------------------------------------------
# DOCX writer helpers
# ---------------------------------------------------------------------------


def _apply_run_style(docx_run, style: Optional[RunStyle]) -> None:
    if not style:
        return
    try:
        if style.font_name:
            docx_run.font.name = style.font_name
        if style.font_size_pt:
            docx_run.font.size = Pt(style.font_size_pt)
        # only apply bold/italic when explicitly True — never override
        # the AI's own inline emphasis with a template's non-emphasis
    except Exception:
        pass


def _add_runs(paragraph, runs: List[Run], inherit: Optional[RunStyle] = None) -> None:
    """Append ``runs`` to ``paragraph``.

    Every ``<Label>`` fill-in becomes its own yellow-highlighted run so the
    lawyer can find each one at a glance; hard breaks become Word line breaks.
    """
    for r in runs:
        if r.br:
            paragraph.add_run().add_break()
            continue
        if not r.text:
            continue
        pos = 0
        for m in PLACEHOLDER_RE.finditer(r.text):
            if not is_placeholder(m.group(1)):
                continue
            if m.start() > pos:
                _add_one_run(paragraph, r.text[pos:m.start()], r, inherit)
            _add_one_run(paragraph, m.group(0), r, inherit, highlight=True)
            pos = m.end()
        if pos < len(r.text):
            _add_one_run(paragraph, r.text[pos:], r, inherit)


def _add_one_run(paragraph, text: str, r: Run, inherit: Optional[RunStyle],
                 *, highlight: bool = False) -> None:
    dr = paragraph.add_run(text)
    # Template-inherited font first; markdown emphasis overlays it.
    _apply_run_style(dr, inherit)
    if r.bold:
        dr.bold = True
    if r.italic:
        dr.italic = True
    if highlight:
        dr.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _safe_set_style(paragraph, style_name: Optional[str]) -> bool:
    """Apply ``style_name``; False when the document does not define it."""
    if not style_name:
        return False
    try:
        paragraph.style = style_name
        return True
    except Exception:
        # style not defined in this document — leave default
        return False


# ---------------------------------------------------------------------------
# Shared block writers (both render paths)
# ---------------------------------------------------------------------------

# Headings that open a new part of the document start on a new page.
_PAGE_BREAK_HEADING_RE = re.compile(
    r"^\s*(schedule|annexure|annex|appendix|exhibit|execution page|"
    r"in witness whereof|signature page)\b",
    re.IGNORECASE,
)
# A list item whose text already carries its own legal label: "(a)", "(iv)",
# "a.", "3.2", "1)". Rendered as a hanging-indent paragraph with that label,
# never with an extra bullet or a second Word number.
_LABELLED_ITEM_RE = re.compile(
    r"^\s*(\((?:[a-z]{1,2}|[ivxl]{1,5}|\d{1,3})\)|(?:[ivxl]{1,5}|[a-z])[.)]"
    r"|\d{1,3}(?:\.\d{1,3})*[.)]?)\s",
    re.IGNORECASE,
)
_LIST_INDENT = Inches(0.4)


def _starts_new_part(heading: Heading, level: int) -> bool:
    return level <= 3 and bool(_PAGE_BREAK_HEADING_RE.match(heading.text))


def _is_line_block(runs: List[Run]) -> bool:
    """A paragraph made of lines (signature, witness, address stacks)."""
    return any(r.br for r in runs)


def _format_line_block(paragraph) -> None:
    """Lay out a signature/witness/address stack.

    Left-aligned: Word justifies every line that ends in a manual line
    break, which stretched "EXECUTED AS A DEED by:" across the page. Single
    spacing, kept on one page, with a gap before it so consecutive
    signatories read as separate blocks.
    """
    pf = paragraph.paragraph_format
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    pf.line_spacing = 1.15
    pf.space_before = Pt(12)
    pf.space_after = Pt(6)
    pf.keep_together = True


def _keep_with_next(paragraph) -> None:
    try:
        paragraph.paragraph_format.keep_with_next = True
    except Exception:
        pass


def _write_list(doc, block, level: int, *, bullet_style: Optional[str],
                run_style: Optional[RunStyle]) -> None:
    """Write a (nested) list.

    Ordered items are written with their number as text and a hanging
    indent. Word's auto-numbering is not used for them: every "List Number"
    paragraph in a document shares one numbering, so a second list would
    carry on from the first (4, 5, 6 …) in Word.
    """
    ordered = isinstance(block, OrderedList)
    for idx, item in enumerate(block.items):
        p = doc.add_paragraph()
        left = _LIST_INDENT * (level + 1)
        labelled = bool(_LABELLED_ITEM_RE.match(item.text))
        runs = list(item.runs)
        if ordered and not labelled:
            runs = [Run(text="{}{}\t".format(block.start + idx, block.delimiter))] + runs
            labelled = True
        if labelled:
            pf = p.paragraph_format
            pf.left_indent = left
            pf.first_line_indent = -_LIST_INDENT
            _set_tab_stop(p, left)
        else:
            style = bullet_style if level == 0 else None
            if level > 0:
                style = "List Bullet {}".format(min(level + 1, 3))
            if not _safe_set_style(p, style):
                p.paragraph_format.left_indent = left
                runs = [Run(text="•\t")] + runs
                p.paragraph_format.first_line_indent = -_LIST_INDENT
                _set_tab_stop(p, left)
        _add_runs(p, runs, inherit=run_style)
        if _is_line_block(item.runs):
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.keep_together = True
        for child in item.children:
            _write_list(doc, child, level + 1, bullet_style=bullet_style,
                        run_style=run_style)


def _set_tab_stop(paragraph, position) -> None:
    try:
        paragraph.paragraph_format.tab_stops.add_tab_stop(position)
    except Exception:
        pass


def _text_width(doc) -> int:
    try:
        s = doc.sections[-1]
        return int(s.page_width - s.left_margin - s.right_margin)
    except Exception:
        return int(Inches(6.5))


def _shade_cell(cell, fill_hex: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    tc_pr.append(shd)


def _mark_row(row, *, header: bool) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant = OxmlElement("w:cantSplit")
    tr_pr.append(cant)
    if header:
        rep = OxmlElement("w:tblHeader")
        tr_pr.append(rep)


def _column_widths(block: Table, ncols: int, total: int) -> List[int]:
    """Widths proportional to each column's longest cell, within bounds."""
    lengths = [0] * ncols
    for row in block.rows:
        for ci, cell in enumerate(row[:ncols]):
            n = sum(len(r.text) for r in cell)
            lengths[ci] = max(lengths[ci], n)
    weights = [min(max(n, 6), 60) for n in lengths]
    floor = int(Inches(0.8))
    raw = [total * w / sum(weights) for w in weights]
    widths = [max(int(w), floor) for w in raw]
    scale = total / sum(widths)
    return [int(w * scale) for w in widths]


def _write_table(doc, block: Table, *, style_name: Optional[str],
                 run_style: Optional[RunStyle]) -> None:
    if not block.rows:
        return
    ncols = max(len(r) for r in block.rows)
    table = doc.add_table(rows=len(block.rows), cols=ncols)
    if not style_name:
        style_name = "Table Grid"
    try:
        table.style = style_name
    except Exception:
        try:
            table.style = "Table Grid"
        except Exception:
            pass
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    widths = _column_widths(block, ncols, _text_width(doc))
    align_map = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }
    for ri, row in enumerate(block.rows):
        is_header = block.has_header and ri == 0
        docx_row = table.rows[ri]
        _mark_row(docx_row, header=is_header)
        for ci in range(ncols):
            cell = docx_row.cells[ci]
            cell.width = Emu(widths[ci])
            para = cell.paragraphs[0]  # a new cell holds one empty paragraph
            pf = para.paragraph_format
            pf.line_spacing = 1.0
            pf.space_before = Pt(2)
            pf.space_after = Pt(2)
            align = block.aligns[ci] if ci < len(block.aligns) else None
            para.alignment = align_map.get(align or "", WD_ALIGN_PARAGRAPH.LEFT)
            runs = row[ci] if ci < len(row) else []
            if is_header:
                runs = [Run(text=r.text, bold=True, italic=r.italic, br=r.br) for r in runs]
                _shade_cell(cell, "D9D9D9")
            _add_runs(para, runs, inherit=run_style)
    # Breathing room after the table so the next paragraph does not touch it.
    gap = doc.add_paragraph()
    gap.paragraph_format.space_after = Pt(0)


def _clear_body(doc: _DocxDocument, *, preserve_first_n_paragraphs: int = 0) -> None:
    """Remove body content, optionally keeping the first N paragraphs.

    Section properties (``w:sectPr``) are always preserved. When
    ``preserve_first_n_paragraphs`` is set, that many ``w:p`` elements at
    the top of the body survive — used to keep the template's preamble
    (title labels, lawyer-facing notes) verbatim above the rebuilt body.
    """
    body = doc.element.body
    kept = 0
    for child in list(body):
        if child.tag == qn("w:sectPr"):
            continue
        if child.tag == qn("w:p") and kept < preserve_first_n_paragraphs:
            kept += 1
            continue
        body.remove(child)


# ---------------------------------------------------------------------------
# Template-skeleton path
# ---------------------------------------------------------------------------


def _render_with_template_skeleton(
    markdown_text: str,
    template_docx_bytes: bytes,
) -> bytes:
    tpl_doc = Document(BytesIO(template_docx_bytes))
    tpl_index = _build_template_index(tpl_doc)

    # Identify the template's preamble — paragraphs above the actual fill
    # area (e.g. "GENERAL BAIL APPLICATION TEMPLATE (INDIA)" + a lawyer-
    # facing note). These are kept verbatim so the generated DOCX still
    # carries the lawyer's label / instructions at the top.
    preamble_count = _find_preamble_end(tpl_index)

    # Clone the template into a fresh document, then clear its body —
    # but keep the preamble paragraphs intact.
    doc = Document(BytesIO(template_docx_bytes))
    _clear_body(doc, preserve_first_n_paragraphs=preamble_count)

    blocks = parse_markdown_blocks(markdown_text)

    # Default body style used when an AI paragraph has no section-anchored
    # match in the template (common when the template has no Heading-
    # styled paragraphs, e.g. when the lawyer pasted plain text).
    default_body_style = _pick_default_body_style(tpl_index)

    # Track the most recently matched template heading index so paragraphs
    # under it can inherit the section's body style.
    last_matched_heading_idx: Optional[int] = None
    tpl_table_style: Optional[str] = None
    try:
        if tpl_doc.tables and tpl_doc.tables[0].style:
            tpl_table_style = tpl_doc.tables[0].style.name
    except Exception:
        tpl_table_style = None

    for bi, block in enumerate(blocks):
        if isinstance(block, Heading):
            idx = _fuzzy_find_heading(block.text, tpl_index)
            level = min(max(block.level, 1), 9)
            p = doc.add_paragraph()
            if idx is not None:
                last_matched_heading_idx = idx
                tpl_para = tpl_index[idx]
                _safe_set_style(p, tpl_para.style_name)
                if tpl_para.alignment is not None:
                    p.alignment = tpl_para.alignment
                _add_runs(p, block.runs, inherit=tpl_para.run_style)
            else:
                last_matched_heading_idx = None
                if _safe_set_style(p, f"Heading {level}"):
                    _add_runs(p, block.runs)
                else:
                    # Template without that heading style: bold body text,
                    # never a literal "####".
                    _add_runs(p, [Run(text=r.text, bold=True, italic=r.italic, br=r.br)
                                  for r in block.runs],
                              inherit=default_body_style.run_style
                              if default_body_style else None)
            _keep_with_next(p)
            if bi > 0 and _starts_new_part(block, level):
                p.paragraph_format.page_break_before = True
            continue

        if isinstance(block, Paragraph):
            inherit_style: Optional[ParaStyle] = None
            if last_matched_heading_idx is not None:
                inherit_style = _body_style_after(tpl_index, last_matched_heading_idx)
            if inherit_style is None:
                inherit_style = default_body_style
            p = doc.add_paragraph()
            if inherit_style:
                _safe_set_style(p, inherit_style.style_name)
                if inherit_style.alignment is not None:
                    p.alignment = inherit_style.alignment
                _add_runs(p, block.runs, inherit=inherit_style.run_style)
            else:
                _add_runs(p, block.runs)
            if _is_line_block(block.runs):
                _format_line_block(p)
            continue

        if isinstance(block, (BulletList, OrderedList)):
            ordered = isinstance(block, OrderedList)
            # Look for a template paragraph in the current section that has
            # numbering — its style carries the numId we want to inherit.
            list_style_name: Optional[str] = None
            list_run_style: Optional[RunStyle] = None
            if last_matched_heading_idx is not None:
                for j in range(last_matched_heading_idx + 1, len(tpl_index)):
                    ps = tpl_index[j]
                    if ps.is_heading:
                        break
                    if ps.has_numbering and ps.style_name:
                        list_style_name = ps.style_name
                        list_run_style = ps.run_style
                        break
            if not list_style_name or ordered:
                # Ordered items carry their number as text (see _write_list).
                list_style_name = "List Bullet"
            if list_run_style is None and default_body_style:
                # Fall back to the template's body font for list items so
                # they don't visually drift from the surrounding paragraphs.
                list_run_style = default_body_style.run_style
            _write_list(doc, block, 0, bullet_style=list_style_name,
                        run_style=list_run_style)
            continue

        if isinstance(block, Table):
            _write_table(doc, block, style_name=tpl_table_style,
                         run_style=default_body_style.run_style
                         if default_body_style else None)
            continue

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Defaults path
# ---------------------------------------------------------------------------


def _apply_legal_defaults(doc: _DocxDocument) -> None:
    # Page margins — 1 inch on all sides.
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    # Normal style — Times New Roman 12 pt, 1.5 spacing, justified.
    try:
        normal = doc.styles["Normal"]
        normal.font.name = "Times New Roman"
        normal.font.size = Pt(12)
        pf = normal.paragraph_format
        pf.line_spacing = 1.5
        pf.space_after = Pt(6)
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    except Exception:
        pass

    # Heading styles 1-6 — black, bold, Times New Roman, kept with the text
    # that follows. Word's defaults are blue theme headings, and levels 4-6
    # were left unstyled.
    for level, size in ((1, 16), (2, 14), (3, 12), (4, 12), (5, 12), (6, 12)):
        try:
            hs = doc.styles[f"Heading {level}"]
            hs.font.name = "Times New Roman"
            hs.font.size = Pt(size)
            hs.font.bold = True
            hs.font.italic = level >= 5
            hs.font.color.rgb = RGBColor(0, 0, 0)
            # East-Asian font slot, or Word keeps the theme heading font.
            rpr = hs.element.get_or_add_rPr()
            fonts = rpr.find(qn("w:rFonts"))
            if fonts is not None:
                for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                    fonts.attrib.pop(qn(attr), None)
            hpf = hs.paragraph_format
            hpf.space_before = Pt(12 if level <= 2 else 8)
            hpf.space_after = Pt(6)
            hpf.keep_with_next = True
            hpf.alignment = WD_ALIGN_PARAGRAPH.CENTER if level == 1 else WD_ALIGN_PARAGRAPH.LEFT
        except Exception:
            pass


def _add_field(paragraph, instr: str) -> None:
    """Append a Word field (PAGE, NUMPAGES) that Word fills in on open."""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = " {} ".format(instr)
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    shown = OxmlElement("w:t")
    shown.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, code, sep, shown, end):
        run._r.append(el)


def _add_page_number_footer(doc: _DocxDocument) -> None:
    """"Page X of Y" at the bottom right of every page (no-template drafts).

    A template keeps its own header and footer; this is only for drafts
    rendered with the default styles.
    """
    for section in doc.sections:
        p = section.footer.paragraphs[0] if section.footer.paragraphs else \
            section.footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run("Page ")
        _add_field(p, "PAGE")
        p.add_run(" of ")
        _add_field(p, "NUMPAGES")
        for r in p.runs:
            r.font.size = Pt(9)


def _render_with_defaults(markdown_text: str) -> bytes:
    doc = Document()
    _apply_legal_defaults(doc)
    _add_page_number_footer(doc)

    blocks = parse_markdown_blocks(markdown_text)
    for bi, block in enumerate(blocks):
        if isinstance(block, Heading):
            level = min(max(block.level, 1), 9)
            p = doc.add_paragraph()
            _safe_set_style(p, f"Heading {level}")
            _add_runs(p, block.runs)
            if bi > 0 and _starts_new_part(block, level):
                p.paragraph_format.page_break_before = True
            continue
        if isinstance(block, Paragraph):
            p = doc.add_paragraph()
            _add_runs(p, block.runs)
            if _is_line_block(block.runs):
                _format_line_block(p)
            continue
        if isinstance(block, (BulletList, OrderedList)):
            _write_list(doc, block, 0, bullet_style="List Bullet", run_style=None)
            continue
        if isinstance(block, Table):
            _write_table(doc, block, style_name="Table Grid", run_style=None)

    out = BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_markdown_to_docx(
    markdown_text: str,
    *,
    template_docx_bytes: Optional[bytes],
    title: str = "Draft",
) -> bytes:
    """Render the AI's Markdown draft into a ``.docx`` blob.

    Falls back to the defaults path if template rendering fails for any
    reason (the template path raising a docx parsing error should never
    block draft delivery).
    """
    if template_docx_bytes:
        try:
            return _render_with_template_skeleton(markdown_text, template_docx_bytes)
        except Exception as exc:
            logger.warning(
                "Template-skeleton DOCX render failed, falling back to defaults: {}",
                exc,
            )
    return _render_with_defaults(markdown_text)


# ---------------------------------------------------------------------------
# Structure checks (SIM-202) — counts only, safe to log
# ---------------------------------------------------------------------------

_LEFTOVER_MARKUP_RE = re.compile(r"^\s*(\||#{1,6}\s|-{3,}\s*$|\*{3,}\s*$)|\\<|\\>")


def _norm_words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def markdown_structure(markdown_text: str) -> Dict[str, Any]:
    """Tables and top-level section headings the Markdown draft contains."""
    blocks = parse_markdown_blocks(markdown_text)
    return {
        "tables": sum(1 for b in blocks if isinstance(b, Table) and b.rows),
        "headings": [_norm_words(b.text) for b in blocks
                     if isinstance(b, Heading) and b.level <= 2 and _norm_words(b.text)],
    }


def docx_structure(docx_bytes: bytes) -> Dict[str, Any]:
    """Counts describing a rendered .docx: tables, placeholders, leftover markup.

    ``leftover_markup`` counts body paragraphs that still read as Markdown
    (``| …``, ``## …``, ``---``, ``\\<…``); it should always be 0.
    """
    doc = Document(BytesIO(docx_bytes))
    texts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.extend(p.text for p in cell.paragraphs)
    highlighted = 0
    for p in doc.paragraphs:
        for r in p.runs:
            if r.font.highlight_color is not None:
                highlighted += 1
    return {
        "tables": len(doc.tables),
        "leftover_markup": sum(1 for t in texts if _LEFTOVER_MARKUP_RE.search(t)),
        "highlighted_runs": highlighted,
        "text": _norm_words(" ".join(texts)),
    }


def template_fill_lost_structure(markdown_text: str, docx_bytes: bytes) -> bool:
    """True when a filled template dropped tables or sections the draft has.

    The template fill paths edit only the template's own text, so a table or
    clause the draft added never reaches the file. When that happens the
    export should render the full draft in the template's styles instead.
    """
    want = markdown_structure(markdown_text)
    have = docx_structure(docx_bytes)
    if want["tables"] > have["tables"]:
        return True
    headings = want["headings"]
    if len(headings) < 3:
        return False
    missing = sum(1 for h in headings if h not in have["text"])
    return missing * 3 > len(headings)


# ---------------------------------------------------------------------------
# docxtpl path — exact template preservation via Jinja2-annotated DOCX.
#
# Two helpers:
#   * annotate_docx_with_variables() — runs ONCE per template at ingest.
#     Locates every variable region in the lawyer's DOCX (using the
#     AI-produced variable_specs) and rewrites the text of the matching
#     runs in-place to ``{{ var_name }}``. Run-level formatting
#     (font/size/bold/italic) is preserved because we mutate ``run.text``
#     without recreating the run object — this is the key trick.
#   * render_with_docxtpl() — runs PER DRAFT. Takes annotated DOCX bytes
#     plus a {var_name -> value} dict and lets ``docxtpl`` render the
#     final document. Output is byte-identical to the annotated template
#     except for the variable text.
# ---------------------------------------------------------------------------


def _normalize_for_match(s: str) -> str:
    """Whitespace-collapsed lowercase. Used for fuzzy region locating."""
    import re

    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _iter_text_runs(doc: _DocxDocument):
    """Yield every editable text run in the document.

    Walks body paragraphs (including those inside tables) AND header /
    footer paragraphs across all sections. Runs inside text boxes are not
    covered — those are rare in legal templates and require a deeper XML
    walk; we accept that limitation.
    """
    for para in doc.paragraphs:
        for run in para.runs:
            yield run, para
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        yield run, para
    for section in doc.sections:
        for container in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            try:
                for para in container.paragraphs:
                    for run in para.runs:
                        yield run, para
            except Exception:
                # Some headers/footers may not exist; skip silently.
                continue


def _find_paragraph_containing(
    doc: _DocxDocument,
    needle_norm: str,
    *,
    context_before_norm: str = "",
    context_after_norm: str = "",
):
    """Locate the paragraph whose normalized text contains ``needle_norm``.

    Disambiguates by context when provided — preferred match is one whose
    text also contains both context strings.
    """
    if not needle_norm:
        return None

    candidates = []
    for para in doc.paragraphs:
        text_norm = _normalize_for_match(para.text)
        if needle_norm and needle_norm in text_norm:
            candidates.append((para, text_norm))
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    text_norm = _normalize_for_match(para.text)
                    if needle_norm and needle_norm in text_norm:
                        candidates.append((para, text_norm))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    # Disambiguate by context anchors.
    for para, text_norm in candidates:
        if context_before_norm and context_before_norm not in text_norm:
            continue
        if context_after_norm and context_after_norm not in text_norm:
            continue
        return para

    # Fall back to the first candidate — ambiguous but predictable.
    return candidates[0][0]


def _replace_text_in_paragraph_preserving_format(
    paragraph,
    old_text: str,
    new_text: str,
) -> bool:
    """Replace ``old_text`` with ``new_text`` across runs in ``paragraph``.

    Splitting / merging runs is avoided wherever possible: when the entire
    ``old_text`` sits inside a single run we mutate that run's text only,
    preserving its font / size / bold / italic exactly. When it spans
    multiple runs, the leading run absorbs ``new_text`` and the trailing
    runs are cleared (acceptable because variable regions are typically
    homogeneously styled).

    Returns True if a replacement was made.
    """
    if not old_text or not paragraph.runs:
        return False

    full_text = "".join(r.text for r in paragraph.runs)
    if old_text not in full_text:
        return False

    start_idx = full_text.find(old_text)
    end_idx = start_idx + len(old_text)

    # Map character offsets to run indices.
    run_spans = []
    cursor = 0
    for run in paragraph.runs:
        run_spans.append((cursor, cursor + len(run.text), run))
        cursor += len(run.text)

    affected = [
        (s, e, r)
        for (s, e, r) in run_spans
        if not (e <= start_idx or s >= end_idx)
    ]
    if not affected:
        return False

    if len(affected) == 1:
        s, e, run = affected[0]
        local_start = start_idx - s
        local_end = end_idx - s
        run.text = run.text[:local_start] + new_text + run.text[local_end:]
        return True

    # Multi-run span: put ``new_text`` into the first affected run (with
    # whatever leading slice it already had), clear the rest within the
    # region.
    first_s, first_e, first_run = affected[0]
    last_s, last_e, last_run = affected[-1]
    first_local = start_idx - first_s
    last_local = end_idx - last_s
    first_run.text = first_run.text[:first_local] + new_text
    for (s, e, run) in affected[1:-1]:
        run.text = ""
    last_run.text = last_run.text[last_local:]
    return True


def annotate_docx_with_variables(
    template_docx_bytes: bytes,
    variable_specs: List[dict],
) -> Tuple[bytes, List[dict]]:
    """Inject Jinja2 placeholders into a lawyer's DOCX template.

    ``variable_specs`` is the AI-produced list. Each item must carry:

    * ``name``               — snake_case variable name (e.g. ``recipient_name``)
    * ``original_text``      — verbatim text in the template to replace
    * ``context_before``     — 5-10 chars of preceding text (disambiguation)
    * ``context_after``      — 5-10 chars of following text (disambiguation)
    * ``hint`` (optional)    — human description, surfaced to frontend
    * ``multi_paragraph``    — bool, default False

    Returns ``(annotated_docx_bytes, applied_specs)``. Specs whose
    ``original_text`` could not be located are dropped from the second
    return value — caller can compare lengths to detect partial success.

    Run-level formatting (font, size, bold, italic) is preserved because
    we mutate ``run.text`` without re-creating the run.
    """
    if not template_docx_bytes:
        raise ValueError("template_docx_bytes is empty")

    doc = Document(BytesIO(template_docx_bytes))

    applied: List[dict] = []
    seen_names = set()
    for spec in variable_specs or []:
        if not isinstance(spec, dict):
            continue
        name = (spec.get("name") or "").strip()
        original = spec.get("original_text") or ""
        if not name or not original:
            continue
        if name in seen_names:
            # Duplicate names from the AI; skip subsequent occurrences so
            # we don't accidentally point two regions at the same Jinja
            # variable.
            continue

        needle_norm = _normalize_for_match(original)
        ctx_before_norm = _normalize_for_match(spec.get("context_before") or "")
        ctx_after_norm = _normalize_for_match(spec.get("context_after") or "")
        para = _find_paragraph_containing(
            doc,
            needle_norm,
            context_before_norm=ctx_before_norm,
            context_after_norm=ctx_after_norm,
        )
        if para is None:
            logger.debug(
                "annotate_docx: could not locate variable '{}' (original={!r})",
                name,
                original[:60],
            )
            continue

        placeholder = "{{ " + name + " }}"
        # Find the actual substring in the paragraph text that matches our
        # normalized needle, so the replacement preserves the original
        # surrounding whitespace exactly.
        actual_text = para.text
        if original in actual_text:
            replaced = _replace_text_in_paragraph_preserving_format(
                para, original, placeholder
            )
        else:
            # Try a normalized search to find the actual substring bounds.
            actual_norm = _normalize_for_match(actual_text)
            if needle_norm in actual_norm:
                # Fall back to replacing the longest unique substring of
                # the run text that overlaps with needle_norm. Practical
                # heuristic: replace the whole paragraph text in one shot,
                # losing intra-paragraph formatting but preserving the
                # paragraph's style. We only do this when the paragraph is
                # mostly the variable.
                if len(needle_norm) >= 0.7 * max(1, len(actual_norm)):
                    if paragraph_runs := para.runs:
                        paragraph_runs[0].text = placeholder
                        for r in paragraph_runs[1:]:
                            r.text = ""
                        replaced = True
                    else:
                        replaced = False
                else:
                    replaced = False
            else:
                replaced = False

        if replaced:
            seen_names.add(name)
            applied.append(
                {
                    "name": name,
                    "hint": spec.get("hint") or "",
                    "multi_paragraph": bool(spec.get("multi_paragraph", False)),
                }
            )

    out = BytesIO()
    doc.save(out)
    return out.getvalue(), applied


def render_with_docxtpl(
    annotated_docx_bytes: bytes,
    context: dict,
) -> bytes:
    """Render an annotated DOCX with ``docxtpl``.

    ``context`` must be a flat ``{var_name: value}`` mapping. Values are
    coerced to strings; ``None`` becomes an empty string. Multi-paragraph
    values can be passed as ``"para1\\n\\npara2"`` — ``docxtpl`` handles
    newlines inside paragraph-level variables.

    Raises ``RuntimeError`` if ``docxtpl`` is not installed.
    """
    try:
        from docxtpl import DocxTemplate
    except ImportError as exc:
        raise RuntimeError(
            "docxtpl is not installed — run `pip install docxtpl>=0.16.7`"
        ) from exc

    safe_context = {}
    for key, value in (context or {}).items():
        if value is None:
            safe_context[key] = ""
        elif isinstance(value, (list, tuple)):
            safe_context[key] = "\n".join(str(v) for v in value if v is not None)
        else:
            safe_context[key] = str(value)

    tpl = DocxTemplate(BytesIO(annotated_docx_bytes))
    tpl.render(safe_context)
    out = BytesIO()
    tpl.save(out)
    return out.getvalue()
