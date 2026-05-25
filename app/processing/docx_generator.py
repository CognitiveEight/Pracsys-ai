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

from dataclasses import dataclass, field
from io import BytesIO
from typing import Iterable, List, Optional, Tuple

from loguru import logger

from docx import Document
from docx.document import Document as _DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt


# ---------------------------------------------------------------------------
# Block AST
# ---------------------------------------------------------------------------


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False


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


@dataclass
class BulletList:
    items: List[ListItem] = field(default_factory=list)


@dataclass
class OrderedList:
    items: List[ListItem] = field(default_factory=list)


@dataclass
class Table:
    rows: List[List[List[Run]]] = field(default_factory=list)  # rows[r][c] = runs


Block = object  # union: Heading | Paragraph | BulletList | OrderedList | Table


# ---------------------------------------------------------------------------
# Markdown parser (markdown-it-py)
# ---------------------------------------------------------------------------


def parse_markdown_blocks(md: str) -> List[Block]:
    """Parse Markdown text into a flat sequence of typed blocks.

    Falls back to ``\\n\\n``-split plain paragraphs if ``markdown-it-py``
    raises or is unavailable.
    """
    md = (md or "").strip()
    if not md:
        return []
    try:
        from markdown_it import MarkdownIt

        parser = MarkdownIt("commonmark", {"breaks": False, "html": False})
        tokens = parser.parse(md)
        return list(_tokens_to_blocks(tokens))
    except Exception as exc:
        logger.warning("Markdown parse failed, falling back to plain split: {}", exc)
        return [Paragraph(runs=[Run(text=chunk.strip())])
                for chunk in md.split("\n\n") if chunk.strip()]


def _inline_runs(token) -> List[Run]:
    """Walk an inline token's children → list[Run]."""
    runs: List[Run] = []
    bold_depth = 0
    italic_depth = 0
    children = token.children or []
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
            if ch.content:
                runs.append(Run(text=ch.content,
                                bold=bold_depth > 0,
                                italic=italic_depth > 0))
        elif t == "code_inline":
            if ch.content:
                runs.append(Run(text=ch.content,
                                bold=bold_depth > 0,
                                italic=italic_depth > 0))
        elif t == "softbreak" or t == "hardbreak":
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
            ordered = t == "ordered_list_open"
            items: List[ListItem] = []
            i += 1
            while i < n and tokens[i].type not in ("bullet_list_close",
                                                    "ordered_list_close"):
                if tokens[i].type == "list_item_open":
                    # find inline inside the item
                    j = i + 1
                    item_runs: List[Run] = []
                    while j < n and tokens[j].type != "list_item_close":
                        if tokens[j].type == "inline":
                            item_runs.extend(_inline_runs(tokens[j]))
                        j += 1
                    items.append(ListItem(runs=item_runs or [Run(text="")]))
                    i = j + 1
                else:
                    i += 1
            yield (OrderedList(items=items) if ordered else BulletList(items=items))
            i += 1  # skip *_list_close
            continue
        if t == "table_open":
            rows: List[List[List[Run]]] = []
            current_row: List[List[Run]] = []
            i += 1
            while i < n and tokens[i].type != "table_close":
                tt = tokens[i].type
                if tt == "tr_open":
                    current_row = []
                elif tt in ("th_open", "td_open"):
                    inline = tokens[i + 1] if i + 1 < n else None
                    cell_runs = (_inline_runs(inline)
                                 if inline and inline.type == "inline" else [])
                    current_row.append(cell_runs)
                elif tt == "tr_close":
                    rows.append(current_row)
                i += 1
            yield Table(rows=rows)
            i += 1  # skip table_close
            continue
        if t == "fence" or t == "code_block":
            yield Paragraph(runs=[Run(text=tok.content or "")])
            i += 1
            continue
        if t == "hr":
            yield Paragraph(runs=[Run(text="—" * 20)])
            i += 1
            continue
        # Unknown — skip
        i += 1


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
    for r in runs:
        if not r.text:
            continue
        dr = paragraph.add_run(r.text)
        # Template-inherited font first; markdown emphasis overlays it.
        _apply_run_style(dr, inherit)
        if r.bold:
            dr.bold = True
        if r.italic:
            dr.italic = True


def _safe_set_style(paragraph, style_name: Optional[str]) -> None:
    if not style_name:
        return
    try:
        paragraph.style = style_name
    except Exception:
        # style not defined in this document — leave default
        pass


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

    for block in blocks:
        if isinstance(block, Heading):
            idx = _fuzzy_find_heading(block.text, tpl_index)
            if idx is not None:
                last_matched_heading_idx = idx
                tpl_para = tpl_index[idx]
                p = doc.add_paragraph()
                _safe_set_style(p, tpl_para.style_name)
                if tpl_para.alignment is not None:
                    p.alignment = tpl_para.alignment
                _add_runs(p, block.runs, inherit=tpl_para.run_style)
            else:
                last_matched_heading_idx = None
                heading_style = f"Heading {min(max(block.level, 1), 9)}"
                p = doc.add_paragraph()
                _safe_set_style(p, heading_style)
                _add_runs(p, block.runs)
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
            if not list_style_name:
                list_style_name = "List Number" if ordered else "List Bullet"
            if list_run_style is None and default_body_style:
                # Fall back to the template's body font for list items so
                # they don't visually drift from the surrounding paragraphs.
                list_run_style = default_body_style.run_style
            for item in block.items:
                p = doc.add_paragraph()
                _safe_set_style(p, list_style_name)
                _add_runs(p, item.runs, inherit=list_run_style)
            continue

        if isinstance(block, Table):
            if not block.rows:
                continue
            ncols = max(len(r) for r in block.rows)
            table = doc.add_table(rows=len(block.rows), cols=ncols)
            tbl_style = None
            try:
                if tpl_doc.tables:
                    tbl_style = tpl_doc.tables[0].style.name if tpl_doc.tables[0].style else None
            except Exception:
                tbl_style = None
            try:
                table.style = tbl_style or "Table Grid"
            except Exception:
                pass
            for ri, row in enumerate(block.rows):
                for ci, cell_runs in enumerate(row):
                    if ci >= ncols:
                        continue
                    cell = table.rows[ri].cells[ci]
                    cell.text = ""
                    _add_runs(cell.paragraphs[0], cell_runs)
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

    # Heading styles — bold, sizes 16/14/12.
    for level, size in ((1, 16), (2, 14), (3, 12)):
        try:
            hs = doc.styles[f"Heading {level}"]
            hs.font.name = "Times New Roman"
            hs.font.size = Pt(size)
            hs.font.bold = True
        except Exception:
            pass


def _render_with_defaults(markdown_text: str) -> bytes:
    doc = Document()
    _apply_legal_defaults(doc)

    blocks = parse_markdown_blocks(markdown_text)
    for block in blocks:
        if isinstance(block, Heading):
            level = min(max(block.level, 1), 9)
            p = doc.add_paragraph()
            _safe_set_style(p, f"Heading {level}")
            _add_runs(p, block.runs)
            continue
        if isinstance(block, Paragraph):
            p = doc.add_paragraph()
            _add_runs(p, block.runs)
            continue
        if isinstance(block, (BulletList, OrderedList)):
            style_name = ("List Number"
                          if isinstance(block, OrderedList) else "List Bullet")
            for item in block.items:
                p = doc.add_paragraph()
                _safe_set_style(p, style_name)
                _add_runs(p, item.runs)
            continue
        if isinstance(block, Table):
            if not block.rows:
                continue
            ncols = max(len(r) for r in block.rows)
            table = doc.add_table(rows=len(block.rows), cols=ncols)
            try:
                table.style = "Table Grid"
            except Exception:
                pass
            for ri, row in enumerate(block.rows):
                for ci, cell_runs in enumerate(row):
                    if ci >= ncols:
                        continue
                    cell = table.rows[ri].cells[ci]
                    cell.text = ""
                    _add_runs(cell.paragraphs[0], cell_runs)

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
