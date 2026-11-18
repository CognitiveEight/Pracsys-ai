"""Format model → clean, editable .docx (SIM-205 / SIM-206).

The output is what a lawyer would have typed in Word: real paragraphs with
their own font, size, alignment, indents and spacing; numbering labels kept
as text with a hanging indent (exactly what the source showed); real tables
with widths, merged cells and borders; the letterhead and running text in the
header; page numbers as Word fields in the footer. That .docx is then the
template the fill engine (``docx_fill``) writes drafts into.
"""

from __future__ import annotations

from io import BytesIO
from typing import List, Optional

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from app.processing.template_fidelity.ir import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_RIGHT,
    ROLE_HEADING,
    ROLE_TITLE,
    Block,
    TCell,
    TemplateIR,
    TImage,
    TPara,
    TTable,
)

_ALIGN = {
    ALIGN_CENTER: WD_ALIGN_PARAGRAPH.CENTER,
    ALIGN_RIGHT: WD_ALIGN_PARAGRAPH.RIGHT,
    ALIGN_JUSTIFY: WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _set_run_font(run, name: Optional[str]) -> None:
    if not name:
        return
    run.font.name = name
    rpr = run._r.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), name)


def _style_defaults(doc, ir: TemplateIR) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = ir.font
    normal.font.size = Pt(ir.size)
    rpr = normal.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), ir.font)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    pf = normal.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = 1.0
    for level in range(1, 5):
        try:
            hs = doc.styles["Heading {}".format(level)]
        except KeyError:
            continue
        hs.font.color.rgb = RGBColor(0, 0, 0)
        hs.font.name = ir.font
        hfonts = hs.element.get_or_add_rPr().find(qn("w:rFonts"))
        if hfonts is not None:
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                hfonts.attrib.pop(qn(attr), None)


def _page_setup(doc, ir: TemplateIR) -> None:
    s = doc.sections[0]
    p = ir.page
    s.page_width = Pt(p.width)
    s.page_height = Pt(p.height)
    s.left_margin = Pt(p.margin_left)
    s.right_margin = Pt(p.margin_right)
    s.top_margin = Pt(p.margin_top)
    s.bottom_margin = Pt(p.margin_bottom)
    s.header_distance = Pt(max(18.0, min(36.0, p.margin_top / 2)))
    s.footer_distance = Pt(max(18.0, min(36.0, p.margin_bottom / 2)))


def _text_width(ir: TemplateIR) -> float:
    return ir.page.width - ir.page.margin_left - ir.page.margin_right


def write_para(paragraph, para: TPara, ir: TemplateIR) -> None:
    """Format ``paragraph`` (already created) as ``para``."""
    if para.role in (ROLE_HEADING, ROLE_TITLE) and para.level:
        try:
            paragraph.style = "Heading {}".format(min(max(para.level, 1), 4))
        except Exception:
            pass
    pf = paragraph.paragraph_format
    paragraph.alignment = _ALIGN.get(para.align, WD_ALIGN_PARAGRAPH.LEFT)
    left, first = para.left_indent, para.first_indent
    if para.label and first == 0:
        hang = max(18.0, min(36.0, 6.0 * len(para.label) + 10.0))
        left, first = left + hang, -hang
    pf.left_indent = Pt(max(left, 0.0))
    pf.first_line_indent = Pt(first)
    pf.space_before = Pt(max(para.space_before, 0.0))
    pf.space_after = Pt(max(para.space_after, 0.0))
    pf.line_spacing = para.line_spacing or 1.0
    if para.page_break_before:
        pf.page_break_before = True
    if para.keep_together:
        pf.keep_together = True
    if para.role in (ROLE_HEADING, ROLE_TITLE):
        pf.keep_with_next = True
    if para.label:
        try:
            pf.tab_stops.add_tab_stop(Pt(max(left, 0.0)))
        except Exception:
            pass
        first_run = para.runs[0] if para.runs else None
        lab = paragraph.add_run(para.label + "\t")
        _format_run(lab, first_run, ir, para)
    for r in para.runs:
        if r.br_before:
            paragraph.add_run().add_break()
        if not r.text:
            continue
        run = paragraph.add_run(r.text)
        _format_run(run, r, ir, para)


def _format_run(run, r, ir: TemplateIR, para: TPara) -> None:
    if r is None:
        return
    if r.bold:
        run.bold = True
    if r.italic:
        run.italic = True
    if r.underline:
        run.underline = True
    size = r.size or None
    if size:
        run.font.size = Pt(size)
    elif para.role in (ROLE_HEADING, ROLE_TITLE):
        run.font.size = Pt(ir.size)
    if r.font and r.font != ir.font:
        _set_run_font(run, r.font)
    run.font.color.rgb = RGBColor(0, 0, 0)


def _shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _write_table(container, tbl: TTable, ir: TemplateIR, width: float) -> None:
    if not tbl.rows:
        return
    ncols = max(sum(max(c.span, 1) for c in row) for row in tbl.rows)
    widths = list(tbl.col_widths[:ncols])
    if len(widths) < ncols or not all(w > 0 for w in widths):
        widths = [width / ncols] * ncols
    scale = min(1.0, width / sum(widths)) if sum(widths) else 1.0
    widths = [w * scale for w in widths]
    if hasattr(container, "add_table") and container.__class__.__name__ == "Document":
        table = container.add_table(rows=len(tbl.rows), cols=ncols)
    else:
        table = container.add_table(len(tbl.rows), ncols, Pt(sum(widths)))
    try:
        table.style = "Table Grid" if tbl.borders else None
    except Exception:
        pass
    table.alignment = {
        ALIGN_CENTER: WD_TABLE_ALIGNMENT.CENTER,
        ALIGN_RIGHT: WD_TABLE_ALIGNMENT.RIGHT,
    }.get(tbl.align, WD_TABLE_ALIGNMENT.LEFT)
    table.autofit = False
    for ri, row in enumerate(tbl.rows):
        docx_row = table.rows[ri]
        tr_pr = docx_row._tr.get_or_add_trPr()
        tr_pr.append(OxmlElement("w:cantSplit"))
        if ri < tbl.header_rows:
            tr_pr.append(OxmlElement("w:tblHeader"))
        col = 0
        for cell_ir in row:
            span = max(cell_ir.span, 1)
            if col >= ncols:
                break
            end = min(col + span, ncols) - 1
            cell = docx_row.cells[col]
            if end > col:
                cell = cell.merge(docx_row.cells[end])
            cell.width = Pt(sum(widths[col:end + 1]))
            _fill_cell(cell, cell_ir, ir)
            col = end + 1
        for ci in range(ncols):
            docx_row.cells[ci].width = Pt(widths[ci])


def _fill_cell(cell, cell_ir: TCell, ir: TemplateIR) -> None:
    paras = cell_ir.paras or [TPara()]
    for i, para in enumerate(paras):
        p = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        cell_para = TPara(**{**para.__dict__, "space_before": para.space_before,
                             "space_after": min(para.space_after, 2.0)})
        write_para(p, cell_para, ir)
    if cell_ir.shade:
        _shade(cell, cell_ir.shade)


def _write_image(container, img: TImage) -> None:
    p = container.add_paragraph()
    p.alignment = _ALIGN.get(img.align, WD_ALIGN_PARAGRAPH.LEFT)
    try:
        p.add_run().add_picture(BytesIO(img.data), width=Pt(img.width))
    except Exception:
        # An image python-docx cannot read is dropped, never fatal.
        pass


def _write_blocks(container, blocks: List[Block], ir: TemplateIR, width: float,
                  *, reuse_first: bool = False) -> None:
    for i, block in enumerate(blocks):
        if isinstance(block, TPara):
            if reuse_first and i == 0 and container.paragraphs:
                p = container.paragraphs[0]
            else:
                p = container.add_paragraph()
            write_para(p, block, ir)
        elif isinstance(block, TTable):
            _write_table(container, block, ir, width)
        elif isinstance(block, TImage):
            _write_image(container, block)


def _page_number_paragraph(footer, ir: TemplateIR) -> None:
    from app.processing.docx_generator import add_field

    p = footer.add_paragraph() if footer.paragraphs and footer.paragraphs[0].text else (
        footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph())
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    fmt = ir.page_number_format or "Page {PAGE} of {NUMPAGES}"
    parts = fmt.replace("{NUMPAGES}", "\x00NUMPAGES\x00").replace("{PAGE}", "\x00PAGE\x00")
    for piece in parts.split("\x00"):
        if piece in ("PAGE", "NUMPAGES"):
            add_field(p, piece)
        elif piece:
            p.add_run(piece)
    for r in p.runs:
        r.font.size = Pt(9)


def build_docx(ir: TemplateIR) -> bytes:
    """Render the format model to .docx bytes."""
    doc = Document()
    _style_defaults(doc, ir)
    _page_setup(doc, ir)
    width = _text_width(ir)
    section = doc.sections[0]
    if ir.header:
        section.header.is_linked_to_previous = False
        _write_blocks(section.header, ir.header, ir, width, reuse_first=True)
    if ir.footer or ir.page_numbers:
        section.footer.is_linked_to_previous = False
        _write_blocks(section.footer, ir.footer, ir, width, reuse_first=True)
        if ir.page_numbers:
            _page_number_paragraph(section.footer, ir)
    # Document() starts with no body paragraph; write blocks in order.
    _write_blocks(doc, ir.blocks, ir, width)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()
