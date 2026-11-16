"""Format model → text PDF and simulated scan (SIM-207 test corpus support).

``write_pdf`` lays the template out with PyMuPDF's HTML engine (Story), then
draws the running header (letterhead) and page-number footer on every page,
so the PDF looks like one exported from Word. ``make_scan`` rasterises it the
way a phone or photocopier would: lower resolution, slight tilt, blur, noise
and JPEG compression, in an image-only PDF with no text layer.
``simulated_scan_words`` gives the word boxes a perfect OCR would return for
that scan, so the scan reader can be tested without calling Vision.
"""

from __future__ import annotations

import html
import io
import math
from dataclasses import dataclass
from typing import List, Tuple

import pymupdf

from app.processing.template_fidelity.ir import (
    ALIGN_LEFT,
    TCell,
    TemplateIR,
    TImage,
    TPara,
    TTable,
)

_GENERIC = {"times new roman": "serif", "georgia": "serif", "cambria": "serif",
            "arial": "sans-serif", "calibri": "sans-serif", "helvetica": "sans-serif",
            "verdana": "sans-serif", "courier new": "monospace"}


def _family(font: str) -> str:
    return _GENERIC.get((font or "").lower(), "serif")


def _runs_html(para: TPara, base_size: float) -> str:
    out = []
    for r in para.runs:
        if r.br_before:
            out.append("<br/>")
        if not r.text:
            continue
        t = html.escape(r.text)
        if r.size and abs(r.size - base_size) > 0.1:
            t = '<span style="font-size:{:.1f}pt">{}</span>'.format(r.size, t)
        if r.underline:
            t = "<u>{}</u>".format(t)
        if r.italic:
            t = "<i>{}</i>".format(t)
        if r.bold:
            t = "<b>{}</b>".format(t)
        out.append(t)
    return "".join(out) or "&#160;"


def _para_html(para: TPara, ir: TemplateIR) -> str:
    left, first = para.left_indent, para.first_indent
    body = _runs_html(para, ir.size)
    if para.label:
        first_bold = bool(para.runs and para.runs[0].bold)
        lab = html.escape(para.label)
        lab = "<b>{}</b>".format(lab) if first_bold else lab
        body = lab + "&#160;&#160;" + body
        if first == 0:
            left, first = left + 28.0, -28.0
    style = ("text-align:{};margin-left:{:.1f}pt;text-indent:{:.1f}pt;margin-top:{:.1f}pt;"
             "margin-bottom:{:.1f}pt;line-height:{:.2f};").format(
        "justify" if para.align == "justify" else para.align, left, first,
        para.space_before, para.space_after, 1.2 * (para.line_spacing or 1.0))
    return '<p style="{}">{}</p>'.format(style, body)


def _cell_html(cell: TCell, ir: TemplateIR, width: float, borders: bool) -> str:
    inner = "".join(_para_html(TPara(**{**p.__dict__, "space_after": 1.0,
                                        "line_spacing": 1.0}), ir) for p in cell.paras)
    # CSS width excludes padding and border; keep the cell's outer width.
    style = "width:{:.1f}pt;padding:2pt;vertical-align:top;".format(max(width - 4.8, 8.0))
    if borders:
        style += "border:0.8pt solid black;"
    if cell.shade:
        style += "background-color:#{};".format(cell.shade)
    span = ' colspan="{}"'.format(cell.span) if cell.span > 1 else ""
    return '<td{} style="{}">{}</td>'.format(span, style, inner)


def _table_html(tbl: TTable, ir: TemplateIR, text_width: float) -> str:
    ncols = max(sum(max(c.span, 1) for c in row) for row in tbl.rows)
    widths = list(tbl.col_widths[:ncols])
    if len(widths) < ncols or not all(w > 0 for w in widths):
        widths = [text_width / ncols] * ncols
    if sum(widths) > text_width:
        widths = [w * text_width / sum(widths) for w in widths]
    rows = []
    for row in tbl.rows:
        cells, col = [], 0
        for c in row:
            w = sum(widths[col:col + max(c.span, 1)])
            cells.append(_cell_html(c, ir, w, tbl.borders))
            col += max(c.span, 1)
        rows.append("<tr>{}</tr>".format("".join(cells)))
    return '<table style="border-collapse:collapse;width:{:.1f}pt;margin-bottom:4pt">{}</table>'.format(
        sum(widths), "".join(rows))


def _chunks(ir: TemplateIR) -> List[list]:
    """Blocks split at forced page breaks."""
    chunks: List[list] = [[]]
    for b in ir.blocks:
        if isinstance(b, TPara) and b.page_break_before and chunks[-1]:
            chunks.append([])
        chunks[-1].append(b)
    return chunks


def _draw_running(doc: "pymupdf.Document", ir: TemplateIR) -> None:
    p = ir.page
    total = doc.page_count
    for pno in range(total):
        page = doc[pno]
        top = 18.0
        if ir.header:
            y = top
            for b in ir.header:
                if isinstance(b, TImage):
                    rect = pymupdf.Rect(p.margin_left, y, p.margin_left + b.width, y + b.width)
                    page.insert_image(rect, stream=b.data)
                    continue
                if isinstance(b, TPara):
                    for k, line in enumerate(b.text.split("\n")):
                        size = (b.runs[min(k, len(b.runs) - 1)].size or ir.size) if b.runs else ir.size
                        bold = bool(b.runs and b.runs[0].bold and k == 0)
                        font = ("tibo" if bold else "tiro") if _family(ir.font) == "serif" else (
                            "hebo" if bold else "helv")
                        width = pymupdf.get_text_length(line, fontname=font, fontsize=size)
                        x = p.width - p.margin_right - width if b.align == "right" else p.margin_left
                        page.insert_text((x, y + size), line, fontname=font, fontsize=size)
                        y += size * 1.25
        if ir.page_numbers:
            text = ir.page_number_format.replace("{PAGE}", str(pno + 1)).replace(
                "{NUMPAGES}", str(total))
            width = pymupdf.get_text_length(text, fontname="helv", fontsize=9)
            page.insert_text((p.width - p.margin_right - width, p.height - 28.0), text,
                             fontname="helv", fontsize=9)


def write_pdf(ir: TemplateIR) -> bytes:
    """Lay the template out as a text PDF."""
    p = ir.page
    # Named families map to metric-compatible fonts (Times New Roman -> Nimbus
    # Roman, Arial -> Nimbus Sans); the generic "serif" maps to Charis SIL.
    # body margin 0, or MuPDF indents everything by its default 1em.
    css = "body {{margin:0; font-family:'{}', {}; font-size:{:.1f}pt;}} p {{margin:0;}}".format(
        ir.font, _family(ir.font), ir.size)
    text_width = p.width - p.margin_left - p.margin_right
    mediabox = pymupdf.Rect(0, 0, p.width, p.height)
    where = pymupdf.Rect(p.margin_left, p.margin_top, p.width - p.margin_right,
                         p.height - p.margin_bottom)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    for chunk in _chunks(ir):
        parts = []
        for b in chunk:
            if isinstance(b, TPara):
                parts.append(_para_html(b, ir))
            elif isinstance(b, TTable):
                parts.append(_table_html(b, ir, text_width))
        story = pymupdf.Story(html="<body>{}</body>".format("".join(parts)), user_css=css)
        more = True
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
    writer.close()
    doc = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    _draw_running(doc, ir)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


@dataclass(frozen=True)
class ScanPage:
    dpi: int
    angle: float  # degrees, counter-clockwise as PIL rotates
    width_px: int
    height_px: int


def make_scan(pdf_bytes: bytes, *, dpi: int = 150, angle: float = 0.6, blur: float = 0.6,
              noise: float = 6.0, quality: int = 60, seed: int = 7
              ) -> Tuple[bytes, List[ScanPage], List[bytes]]:
    """Rasterise a PDF like a scan. Returns (image-only PDF, page meta, page JPEGs)."""
    import numpy as np
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    meta: List[ScanPage] = []
    jpegs: List[bytes] = []
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(blur))
        arr = np.asarray(img, dtype=np.float32) + rng.normal(0, noise, (img.height, img.width))
        img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "L")
        jb = io.BytesIO()
        img.save(jb, "JPEG", quality=quality)
        data = jb.getvalue()
        jpegs.append(data)
        newp = out.new_page(width=page.rect.width, height=page.rect.height)
        newp.insert_image(newp.rect, stream=data)
        meta.append(ScanPage(dpi, angle, img.width, img.height))
    data = out.tobytes(garbage=3, deflate=True)
    src.close()
    out.close()
    return data, meta, jpegs


def _glyph_words(page) -> List[Tuple[float, float, float, float, str]]:
    """Words with Vision-like boxes: cap height to descender (~0.94 em).

    PDF word boxes include the font's full line height (~1.3 em here), which
    no OCR engine reports.
    """
    out = []
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                size, base = s["size"], s["origin"][1]
                cur: List = []

                def flush() -> None:
                    if cur:
                        out.append((cur[0]["bbox"][0], base - 0.72 * size, cur[-1]["bbox"][2],
                                    base + 0.22 * size, "".join(c["c"] for c in cur)))

                for ch in s.get("chars", []):
                    if ch["c"].isspace():
                        flush()
                        cur = []
                    else:
                        cur.append(ch)
                flush()
    return out


def simulated_scan_words(pdf_bytes: bytes, meta: List[ScanPage]) -> List[List[Tuple[float, float, float, float, str]]]:
    """Word boxes (pixels, in the tilted scan) a perfect OCR would return.

    Test support for the scan reader: the source PDF's words, scaled to the
    scan's resolution and rotated by the same angle about the page centre.
    """
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page, m in zip(src, meta):
        s = m.dpi / 72.0
        cx, cy = m.width_px / 2.0, m.height_px / 2.0
        a = math.radians(m.angle)
        ca, sa = math.cos(a), math.sin(a)

        def rot(x: float, y: float) -> Tuple[float, float]:
            dx, dy = x * s - cx, y * s - cy
            # PIL rotates counter-clockwise on screen (y down): x' = cx + dx*cos + dy*sin
            return cx + dx * ca + dy * sa, cy - dx * sa + dy * ca

        words = []
        for x0, y0, x1, y1, text in _glyph_words(page):
            pts = [rot(x0, y0), rot(x1, y0), rot(x1, y1), rot(x0, y1)]
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            words.append((min(xs), min(ys), max(xs), max(ys), text))
        pages.append(words)
    src.close()
    return pages


__all__ = ["write_pdf", "make_scan", "simulated_scan_words", "ScanPage", "ALIGN_LEFT"]
