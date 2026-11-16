"""Text PDF → format model (SIM-205).

Reads what the PDF itself records for every span — font, size, bold/italic,
position — plus underlines (thin drawn lines under text), bordered tables
(PyMuPDF ``find_tables``, with merged cells and header shading) and images
in the top band (the letterhead logo). ``layout.assemble`` then recovers
paragraphs, labels, spacing, header/footer and borderless tables.

Replaces pdf2docx for templates: pdf2docx reproduces a PDF as positioned
text boxes, which a fill engine cannot edit paragraph by paragraph.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

import pymupdf
from loguru import logger

from app.processing.template_fidelity.ir import ALIGN_CENTER, ALIGN_RIGHT, TCell, TemplateIR, TPara, TRun, TTable
from app.processing.template_fidelity.layout import Line, PageLines, Span, assemble

# A PDF whose pages carry fewer characters than this is treated as a scan.
MIN_TEXT_CHARS_PER_PAGE = 40

_FAMILIES = (
    (("times", "tiro", "tibo", "tiit", "nimbusrom", "liberationserif", "serif", "georgia"),
     "Times New Roman"),
    (("arial", "helv", "hebo", "heit", "nimbussan", "liberationsans", "sans", "calibri"), "Arial"),
    (("courier", "cour", "mono"), "Courier New"),
)


def canonical_font(name: str) -> Optional[str]:
    """Map a PDF font name (``ABCDEF+TimesNewRomanPS-BoldMT``) to a Word family."""
    if not name:
        return None
    n = re.sub(r"^[A-Z]{6}\+", "", name).lower().replace(" ", "").replace("-", "")
    if "georgia" in n:
        return "Georgia"
    if "calibri" in n:
        return "Calibri"
    if "cambria" in n:
        return "Cambria"
    for keys, family in _FAMILIES:
        if any(k in n for k in keys):
            return family
    base = re.sub(r"(bold|italic|oblique|regular|mt|ps|,.*)$", "", n)
    return base or None


# Ligatures ("ﬁ", "ﬂ") are expanded to their letters, or "specific" read from
# a Word-exported PDF would not match the same word anywhere else.
_TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace(" ", " ")


def is_text_pdf(pdf_bytes: bytes) -> bool:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        chars = sum(len(p.get_text("text").strip()) for p in doc)
        return doc.page_count > 0 and chars >= MIN_TEXT_CHARS_PER_PAGE * doc.page_count
    finally:
        doc.close()


def _underlines(page) -> List[Tuple[float, float, float]]:
    """Thin horizontal strokes: (x0, x1, y)."""
    out = []
    for d in page.get_drawings():
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.8 and abs(p2.x - p1.x) > 3:
                    out.append((min(p1.x, p2.x), max(p1.x, p2.x), p1.y))
            elif item[0] == "re":
                r = item[1]
                if r.height < 1.6 and r.width > 3:
                    out.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    # Underlines are often drawn per word; join pieces on the same line.
    out.sort(key=lambda s: (round(s[2], 1), s[0]))
    merged: List[Tuple[float, float, float]] = []
    for x0, x1, y in out:
        if merged and abs(merged[-1][2] - y) < 0.6 and x0 - merged[-1][1] < 6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], x1), merged[-1][2])
        else:
            merged.append((x0, x1, y))
    return merged


def _is_underlined(span_box, strokes) -> bool:
    """A stroke just under the text, about as long as the text.

    A table border also runs under a header cell's text, but across the
    whole cell; a stroke much longer than the text is not an underline.
    """
    x0, y0, x1, y1 = span_box
    w = max(x1 - x0, 1.0)
    for sx0, sx1, sy in strokes:
        if y1 - 3.5 <= sy <= y1 + 3.0:
            overlap = min(x1, sx1) - max(x0, sx0)
            if overlap >= 0.6 * w and (sx1 - sx0) <= 1.35 * w + 4:
                return True
    return False


def _inside(box, region, pad: float = 1.5) -> bool:
    x0, y0, x1, y1 = box
    rx0, ry0, rx1, ry1 = region
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return rx0 - pad <= cx <= rx1 + pad and ry0 - pad <= cy <= ry1 + pad


def _fill_hex(page, rect) -> Optional[str]:
    for d in page.get_drawings():
        fill = d.get("fill")
        r = d.get("rect")
        if fill and r is not None and r.contains(rect.tl + (1, 1)) and r.contains(rect.br - (1, 1)):
            if all(c > 0.97 for c in fill):
                continue
            return "".join("{:02X}".format(int(round(c * 255))) for c in fill[:3])
    return None


def _tables(page, strokes) -> List[Tuple[Tuple[float, float, float, float], TTable]]:
    out = []
    try:
        found = page.find_tables()
    except Exception as exc:
        logger.opt(exception=exc).warning("template pdf table detection failed")
        return out
    for tab in found.tables:
        if tab.row_count < 1 or tab.col_count < 2:
            continue
        xs = sorted({round(c[0], 1) for c in tab.cells if c} | {round(tab.bbox[2], 1)})
        widths = [b - a for a, b in zip(xs, xs[1:])] or []
        rows: List[List[TCell]] = []
        header_bold = True
        for ri, row in enumerate(tab.rows):
            cells: List[TCell] = []
            boxes = list(row.cells)
            ci = 0
            while ci < len(boxes):
                box = boxes[ci]
                span = 1
                while ci + span < len(boxes) and boxes[ci + span] is None:
                    span += 1
                if box is None:
                    ci += 1
                    continue
                rect = pymupdf.Rect(box)
                para = _cell_para(page, rect, strokes)
                if ri == 0 and not all(r.bold for r in para.runs if r.text.strip()):
                    header_bold = False
                cells.append(TCell(paras=[para], span=span, shade=_fill_hex(page, rect)))
                ci += span
            rows.append(cells)
        header_rows = 1 if header_bold and rows and any(c.paras[0].text.strip() for c in rows[0]) else 0
        if header_rows and len(rows) > 1 and all(r.bold for c in rows[1] for p in c.paras
                                                 for r in p.runs if r.text.strip()):
            header_rows = 2
        out.append((tuple(tab.bbox), TTable(rows=rows, col_widths=widths, header_rows=header_rows,
                                            borders=True)))
    return out


def _cell_para(page, rect, strokes) -> TPara:
    # Spans are assigned by their centre: clipping to the cell box drops a
    # glyph that overhangs the border (a right-aligned "5%" lost its "%").
    d = page.get_text("dict", clip=rect + (-6, -2, 6, 2), flags=_TEXT_FLAGS)
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                s["text"] = _clean(s["text"])
    runs: List[TRun] = []
    lines = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", [])
                     if _inside(s["bbox"], tuple(rect), pad=0.5)]
            if spans:
                lines.append({**ln, "spans": spans,
                              "bbox": (min(s["bbox"][0] for s in spans), ln["bbox"][1],
                                       max(s["bbox"][2] for s in spans), ln["bbox"][3])})
    lines.sort(key=lambda ln: (round(ln["bbox"][1]), ln["bbox"][0]))
    prev_y = None
    xs0, xs1 = [], []
    for ln in lines:
        new_line = prev_y is not None and ln["bbox"][1] - prev_y > 2
        for k, s in enumerate(ln["spans"]):
            if not s["text"].strip():
                continue
            bold, italic = _style_flags(s)
            text = s["text"]
            if runs and not new_line and not runs[-1].text.endswith(" "):
                text = " " + text.lstrip() if k == 0 else text
            runs.append(TRun(text.strip() if not runs else text, bold=bold, italic=italic,
                             underline=_is_underlined(s["bbox"], strokes),
                             br_before=new_line and k == 0 and bool(runs)))
        xs0.append(ln["bbox"][0])
        xs1.append(ln["bbox"][2])
        prev_y = ln["bbox"][1]
    para = TPara(runs=runs, space_after=2.0)
    if xs0:
        lpad, rpad = min(xs0) - rect.x0, rect.x1 - max(xs1)
        if rpad < 6 and lpad > 12:
            para.align = ALIGN_RIGHT
        elif abs(lpad - rpad) < 4 and lpad > 8:
            para.align = ALIGN_CENTER
    return para


def _style_flags(span) -> Tuple[bool, bool]:
    name = (span.get("font") or "").lower()
    flags = span.get("flags", 0)
    bold = bool(flags & 16) or "bold" in name or name.endswith(("bo", "-b"))
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    return bold, italic


def read_pages(pdf_bytes: bytes) -> List[PageLines]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages: List[PageLines] = []
    try:
        for page in doc:
            strokes = _underlines(page)
            tables = _tables(page, strokes)
            regions = [bbox for bbox, _ in tables]
            raw: List[Line] = []
            d = page.get_text("dict", flags=_TEXT_FLAGS)
            for b_no, b in enumerate(d.get("blocks", [])):
                for l_no, ln in enumerate(b.get("lines", [])):
                    # MuPDF draws each table cell as its own line object, so
                    # (block, line) separates cells that share a baseline.
                    bi = b_no * 1000 + l_no
                    spans = []
                    for s in ln.get("spans", []):
                        s["text"] = _clean(s["text"])
                        if not s["text"].strip():
                            continue
                        if any(_inside(s["bbox"], r) for r in regions):
                            continue
                        bold, italic = _style_flags(s)
                        spans.append(Span(s["text"], s["bbox"][0], s["bbox"][2], s["size"], bold,
                                          italic, _is_underlined(s["bbox"], strokes),
                                          canonical_font(s.get("font", "")), bi))
                    if spans:
                        x0, y0, x1, y1 = ln["bbox"]
                        raw.append(Line(spans, min(s.x0 for s in spans), y0,
                                        max(s.x1 for s in spans), y1, page.number))
            lines = _merge_baselines(raw)
            images = []
            for info in page.get_images(full=True):
                xref = info[0]
                try:
                    rects = page.get_image_rects(xref)
                    data = doc.extract_image(xref).get("image")
                except Exception:
                    continue
                for r in rects:
                    if data:
                        images.append(((r.x0, r.y0, r.x1, r.y1), data))
            pages.append(PageLines(lines=lines, width=page.rect.width, height=page.rect.height,
                                   images=images, tables=tables))
    finally:
        doc.close()
    return pages


def _merge_baselines(lines: List[Line]) -> List[Line]:
    """Join fragments that sit on the same baseline (PDFs often split them)."""
    lines = sorted(lines, key=lambda ln: (round((ln.y0 + ln.y1) / 2), ln.x0))
    out: List[Line] = []
    for ln in lines:
        if out:
            last = out[-1]
            same_row = abs(((ln.y0 + ln.y1) / 2) - ((last.y0 + last.y1) / 2)) < 0.35 * max(ln.size, 1.0)
            if same_row and ln.x0 >= last.x1 - 1:
                last.spans.extend(ln.spans)
                last.x1 = max(last.x1, ln.x1)
                last.y0, last.y1 = min(last.y0, ln.y0), max(last.y1, ln.y1)
                continue
        out.append(Line(list(ln.spans), ln.x0, ln.y0, ln.x1, ln.y1, ln.page))
    out.sort(key=lambda ln: (ln.y0, ln.x0))
    return out


def read_pdf(pdf_bytes: bytes) -> TemplateIR:
    """Text PDF → format model."""
    return assemble(read_pages(pdf_bytes), source="pdf")
