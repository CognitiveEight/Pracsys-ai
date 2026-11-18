"""Apply a lawyer's chosen court or letterhead format to a draft (SIM-217).

The lawyer picks a format when starting a draft; the backend sends it as
``formatLayout`` (``{id, name, type, formatting}``). This module applies its
``formatting`` to an **existing** Word file:

- ``page``: font, size, line spacing, page size, orientation, margins, body
  alignment, a diagonal watermark and a page number;
- ``letterhead``: a header and footer built from the Left / Center / Right
  blocks (text, a safe subset of HTML, or images). It replaces any header or
  footer the file already had;
- ``court``: the court heading block inserted at the top of the body (court,
  jurisdiction, document type, case number, custom fields), skipping any line
  the draft already starts with.

It works on the .docx, not on Markdown, so the same function can re-apply a
format to a file the lawyer has edited. Body text is never changed except for
the inserted court block.

Bad settings never raise: what is valid is applied, the rest is skipped and
reported (``unknown_keys`` / ``invalid_values``, key paths only, never
values). Only unreadable .docx bytes raise. Images come only through the
injected ``fetch_image``; a failed image skips that block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.parser import parse_xml
from docx.shared import Emu, Inches, Mm, Pt

from app.processing.format_images import image_kind

FetchImage = Callable[[str], Optional[bytes]]

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class FormatResult:
    """The formatted file plus counts for the one summary log line."""

    docx_bytes: bytes
    format_type: str = "none"
    page_settings: int = 0
    watermark: str = "unchanged"
    page_number: str = "unchanged"
    letterhead_blocks: int = 0
    court_blocks: int = 0
    court_duplicates_skipped: int = 0
    images_ok: int = 0
    images_failed: int = 0
    unknown_keys: List[str] = field(default_factory=list)
    invalid_values: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Known keys (anything else is reported as unknown and ignored)
# ---------------------------------------------------------------------------

_PAGE_KEYS = {
    "font", "fontSize", "lineSpacing", "pageSize", "orientation", "margins",
    "alignment", "watermark", "pageNumber",
}
_MARGIN_KEYS = {"top", "bottom", "left", "right"}
_COURT_SINGLE = ("courtHeader", "jurisdiction", "documentType", "caseNumber")
_COURT_KEYS = set(_COURT_SINGLE) | {"customFields"}
_COURT_BLOCK_KEYS = {
    "text", "font", "fontSize", "bold", "italic", "underline", "uppercase",
    "lineSpacing", "align", "label", "value", "id",
}
_SIDES = ("Left", "Center", "Right")
_SIDE_ATTRS = (
    "Text", "Html", "FontFamily", "FontSize", "Spacing", "LineSpacing", "Bold",
    "Italic", "Underline", "Align", "Show", "Image",
)
_LETTERHEAD_KEYS = {
    "headerType", "footerType", "headerImageUrl", "footerImageUrl", "pageApplication",
} | {f"{prefix}{attr}{side}" for prefix in ("header", "footer")
     for attr in _SIDE_ATTRS for side in _SIDES}
_TOP_KEYS = {"page", "court", "letterhead"}

# ---------------------------------------------------------------------------
# Value parsing (each returns None for anything it cannot read)
# ---------------------------------------------------------------------------

_LENGTH_RE = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*(in|inch|inches|cm|mm|pt|\")?\s*$", re.I)
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(pt|px)?\s*$", re.I)
_MAX_PAGE_LENGTH = Inches(22)

_PAGE_SIZES = {
    "a4": (Mm(210), Mm(297)),
    "letter": (Inches(8.5), Inches(11)),
    "legal": (Inches(8.5), Inches(14)),
}
_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "centre": WD_ALIGN_PARAGRAPH.CENTER,
    "middle": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "justified": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "both": WD_ALIGN_PARAGRAPH.JUSTIFY,
}
_WATERMARKS = {"DRAFT", "CONFIDENTIAL", "URGENT"}
# (width pt, height pt) of the diagonal text, as Word sizes its own watermarks.
_WATERMARK_BOX = {"DRAFT": (412, 165), "CONFIDENTIAL": (527, 132), "URGENT": (450, 150)}

# Characters XML 1.0 does not allow; python-docx raises on them.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _clean(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return _XML_BAD.sub("", text)


def _as_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
    return None


def parse_length(v: Any) -> Optional[int]:
    """"1in", "2.54cm", "25mm", "72pt", bare number = inches → EMU, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        num, unit = float(v), "in"
    elif isinstance(v, str):
        m = _LENGTH_RE.match(v)
        if not m:
            return None
        num, unit = float(m.group(1)), (m.group(2) or "in").lower()
    else:
        return None
    if unit in ("in", "inch", "inches", '"'):
        emu = Inches(num)
    elif unit == "cm":
        emu = Mm(num * 10)
    elif unit == "mm":
        emu = Mm(num)
    else:  # pt
        emu = Pt(num)
    if emu < 0 or emu > _MAX_PAGE_LENGTH:
        return None
    return int(emu)


def parse_font_size(v: Any) -> Optional[float]:
    """"12pt", "12", 12, "16px" → points (4-96), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        size = float(v)
    elif isinstance(v, str):
        m = _SIZE_RE.match(v)
        if not m:
            return None
        size = float(m.group(1))
        if (m.group(2) or "").lower() == "px":
            size *= 0.75
    else:
        return None
    if not 4 <= size <= 96:
        return None
    return round(size * 2) / 2  # Word stores half-points


def parse_line_spacing(v: Any) -> Optional[float]:
    """"1.5", 1.5, "150%", "single", "double" → multiple (0.5-5), else None."""
    if isinstance(v, bool):
        return None
    words = {"single": 1.0, "double": 2.0}
    if isinstance(v, (int, float)):
        val = float(v)
    elif isinstance(v, str):
        s = v.strip().lower()
        if s in words:
            val = words[s]
        else:
            pct = s.endswith("%")
            s = s.rstrip("%").strip()
            try:
                val = float(s)
            except ValueError:
                return None
            if pct:
                val /= 100.0
    else:
        return None
    if not 0.5 <= val <= 5:
        return None
    return val


_FONT_OK = re.compile(r"^[A-Za-z0-9 \-_.]{1,64}$")


def parse_font(v: Any) -> Optional[str]:
    """First family of a CSS-style list ("Times New Roman, serif"), else None."""
    if not isinstance(v, str):
        return None
    first = v.split(",")[0].strip().strip("'\"").strip()
    if not first or not _FONT_OK.match(first):
        return None
    return first


def parse_align(v: Any):
    if not isinstance(v, str):
        return None
    return _ALIGN.get(v.strip().lower())


def parse_page_number(v: Any) -> Tuple[Optional[str], Optional[str], bool]:
    """``(horizontal, vertical, valid)``; ``(None, None, True)`` means none."""
    if not isinstance(v, str):
        return None, None, False
    s = v.strip().lower()
    if s in ("none", "", "off", "no", "false"):
        return None, None, True
    parts = [p for p in re.split(r"[\s_\-]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", v.strip()).lower()) if p]
    horiz = {"left": "left", "center": "center", "centre": "center", "middle": "center",
             "right": "right"}
    h = vpos = None
    for p in parts:
        if p in horiz and h is None:
            h = horiz[p]
        elif p in ("top", "bottom") and vpos is None:
            vpos = p
        else:
            return None, None, False
    if h is None or vpos is None:
        return None, None, False
    return h, vpos, True


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def extract_formatting(format_layout: Any) -> Tuple[str, Dict[str, Any]]:
    """``(type, formatting)`` from a formatLayout; ``("none", {})`` if unusable.

    Accepts the full ``{type, formatting}`` object, a JSON string of it, or a
    bare formatting dict.
    """
    if isinstance(format_layout, str):
        try:
            format_layout = json.loads(format_layout)
        except (ValueError, TypeError):
            return "none", {}
    if not isinstance(format_layout, dict):
        return "none", {}
    formatting = format_layout.get("formatting")
    if isinstance(formatting, str):
        try:
            formatting = json.loads(formatting)
        except (ValueError, TypeError):
            formatting = None
    if formatting is None and _TOP_KEYS & set(format_layout):
        formatting = format_layout
    if not isinstance(formatting, dict):
        return "none", {}
    ftype = format_layout.get("type")
    ftype = ftype.strip().lower() if isinstance(ftype, str) else ""
    if ftype not in ("court", "letterhead"):
        ftype = ""
    return ftype, formatting


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

_THEME_FONT_ATTRS = ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")
_SYMBOL_FONTS = {"symbol", "wingdings", "wingdings 2", "wingdings 3", "webdings"}


def _set_rfonts(rpr, name: str) -> None:
    fonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), name)
    for attr in _THEME_FONT_ATTRS:
        fonts.attrib.pop(qn(attr), None)


def _run_font(run, name: Optional[str]) -> None:
    if name:
        _set_rfonts(run._r.get_or_add_rPr(), name)


def _style_chain(style):
    seen = 0
    while style is not None and seen < 20:
        yield style
        style = style.base_style
        seen += 1


def _is_heading_para(paragraph) -> bool:
    try:
        for st in _style_chain(paragraph.style):
            n = (st.name or "").lower()
            if n.startswith("heading") or n in ("title", "subtitle"):
                return True
    except Exception:
        return False
    return False


def _effective_alignment(paragraph):
    if paragraph.alignment is not None:
        return paragraph.alignment
    try:
        for st in _style_chain(paragraph.style):
            if st.paragraph_format.alignment is not None:
                return st.paragraph_format.alignment
    except Exception:
        return None
    return None


def _has_line_break(paragraph) -> bool:
    for br in paragraph._p.iter(qn("w:br")):
        if br.get(qn("w:type")) in (None, "textWrapping"):
            return True
    return False


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", text.casefold())).strip()


def _block_pattern(text: str) -> Optional[re.Pattern]:
    """Case/space-insensitive pattern; blank runs ("____", "...") match anything short."""
    pieces = re.split(r"_{2,}|\.{3,}|…", text)
    normed = [_norm(p) for p in pieces]
    if not any(normed):
        return None
    body = r".{0,40}?".join(re.escape(p) for p in normed)
    return re.compile(body)


# ---------------------------------------------------------------------------
# Header / footer helpers
# ---------------------------------------------------------------------------


def _clear_story(hf) -> None:
    el = hf._element
    for child in list(el):
        el.remove(child)


def _ensure_paragraph(hf) -> None:
    """A header/footer must end with a paragraph (also after a table)."""
    el = hf._element
    children = [c for c in el if c.tag in (qn("w:p"), qn("w:tbl"), qn("w:sdt"))]
    if not children or children[-1].tag == qn("w:tbl"):
        p = hf.add_paragraph()
        if children:  # only a spacer after a table: keep it tiny
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            rpr = p._p.get_or_add_pPr()
            mark = OxmlElement("w:rPr")
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), "2")
            mark.append(sz)
            rpr.append(mark)


def _text_width(section) -> int:
    try:
        width = int(section.page_width) - int(section.left_margin) - int(section.right_margin)
        if width > 0:
            return width
    except Exception:
        pass
    return int(Inches(6))


def _remove_borders(table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is not None:
        tbl_pr.remove(borders)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "nil")
        borders.append(e)
    # Schema order: tblBorders comes before shd / tblLayout / tblCellMar /
    # tblLook; Word rejects a file with it out of place.
    later = None
    for tag in ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
                "w:tblCaption", "w:tblDescription"):
        later = tbl_pr.find(qn(tag))
        if later is not None:
            break
    if later is not None:
        later.addprevious(borders)
    else:
        tbl_pr.append(borders)
    style = tbl_pr.find(qn("w:tblStyle"))
    if style is not None:
        tbl_pr.remove(style)


_PAGE_TEXT_RE = re.compile(r"^(page)?\s*\d*\s*((of|/)\s*\d*)?$", re.I)


def _is_page_number_paragraph(p_el) -> bool:
    has_field = False
    for instr in p_el.iter(qn("w:instrText")):
        if re.search(r"\bPAGE\b", instr.text or ""):
            has_field = True
    for fld in p_el.iter(qn("w:fldSimple")):
        if re.search(r"\bPAGE\b", fld.get(qn("w:instr")) or ""):
            has_field = True
    if not has_field:
        return False
    text = "".join(t.text or "" for t in p_el.iter(qn("w:t")))
    return bool(_PAGE_TEXT_RE.match(text.strip()))


def _remove_paragraph(p_el) -> None:
    parent = p_el.getparent()
    if parent is None:
        return
    if parent.tag == qn("w:tc") and len(parent.findall(qn("w:p"))) <= 1:
        for child in list(p_el):
            if child.tag != qn("w:pPr"):
                p_el.remove(child)
        return
    parent.remove(p_el)
    # A page-number gallery wraps its paragraph in a content control; an
    # empty content control is invalid, so drop it with its last paragraph.
    if parent.tag == qn("w:sdtContent") and len(parent) == 0:
        sdt = parent.getparent()
        if sdt is not None and sdt.getparent() is not None:
            sdt.getparent().remove(sdt)


def _all_stories(doc):
    """Every header/footer part that has its own definition."""
    seen = set()
    for section in doc.sections:
        for attr in ("header", "first_page_header", "even_page_header",
                     "footer", "first_page_footer", "even_page_footer"):
            try:
                hf = getattr(section, attr)
                if hf.is_linked_to_previous:
                    continue
                el = hf._element
            except Exception:
                continue
            if id(el) in seen:
                continue
            seen.add(id(el))
            yield hf


def _target_stories(doc, kind: str):
    """Header (or footer) parts shown on pages: default, first page, even pages."""
    even = False
    try:
        even = bool(doc.settings.odd_and_even_pages_header_footer)
    except Exception:
        pass
    out = []
    for i, section in enumerate(doc.sections):
        variants = [kind]
        if section.different_first_page_header_footer:
            variants.append(f"first_page_{kind}")
        if even:
            variants.append(f"even_page_{kind}")
        for attr in variants:
            hf = getattr(section, attr)
            if i > 0 and hf.is_linked_to_previous:
                continue
            if i == 0 and hf.is_linked_to_previous:
                hf.is_linked_to_previous = False
            out.append(hf)
    return out


# ---------------------------------------------------------------------------
# Safe HTML subset → lines of styled runs
# ---------------------------------------------------------------------------

Segment = Tuple[str, bool, bool, bool]  # text, bold, italic, underline


class _SafeHtml(HTMLParser):
    _BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    _DROP = {"script", "style", "head", "title", "template", "noscript", "iframe", "object"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: List[List[Segment]] = [[]]
        self.b = self.i = self.u = 0
        self.drop = 0

    def _newline(self) -> None:
        if self.lines[-1]:
            self.lines.append([])

    def handle_starttag(self, tag, attrs):  # attrs deliberately ignored
        tag = tag.lower()
        if tag in self._DROP:
            self.drop += 1
        elif tag in ("b", "strong"):
            self.b += 1
        elif tag in ("i", "em"):
            self.i += 1
        elif tag == "u":
            self.u += 1
        elif tag == "br":
            self.lines.append([])
        elif tag in self._BLOCK:
            self._newline()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br":
            self.lines.append([])

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._DROP:
            self.drop = max(0, self.drop - 1)
        elif tag in ("b", "strong"):
            self.b = max(0, self.b - 1)
        elif tag in ("i", "em"):
            self.i = max(0, self.i - 1)
        elif tag == "u":
            self.u = max(0, self.u - 1)
        elif tag in self._BLOCK:
            self._newline()

    def handle_data(self, data):
        if self.drop:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and not self.lines[-1]:
            return
        self.lines[-1].append((text, self.b > 0, self.i > 0, self.u > 0))


def html_to_lines(html: str) -> List[List[Segment]]:
    parser = _SafeHtml()
    try:
        parser.feed(_clean(html))
        parser.close()
    except Exception:
        return []
    lines = parser.lines
    # Trim leading/trailing spaces per line and drop empty edge lines.
    out: List[List[Segment]] = []
    for line in lines:
        segs = [s for s in line if s[0]]
        if segs:
            first = segs[0]
            segs[0] = (first[0].lstrip(), *first[1:])
            last = segs[-1]
            segs[-1] = (last[0].rstrip(), *last[1:])
            segs = [s for s in segs if s[0]]
        out.append(segs)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


# ---------------------------------------------------------------------------
# The formatter
# ---------------------------------------------------------------------------


class _Formatter:
    def __init__(self, doc, result: FormatResult, fetch_image: Optional[FetchImage]):
        self.doc = doc
        self.r = result
        self.fetch_image = fetch_image
        self.page_font: Optional[str] = None

    # -- reporting -----------------------------------------------------------

    def unknown(self, path: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.\[\]]", "?", path)[:80]
        if safe not in self.r.unknown_keys:
            self.r.unknown_keys.append(safe)

    def invalid(self, path: str) -> None:
        if path not in self.r.invalid_values:
            self.r.invalid_values.append(path)

    def check_keys(self, data: Dict[str, Any], known: set, prefix: str) -> None:
        for k in data:
            if k not in known:
                self.unknown(f"{prefix}.{k}" if prefix else str(k))

    # -- page ----------------------------------------------------------------

    def apply_page(self, page: Dict[str, Any]) -> Tuple[Optional[str], Any]:
        """Returns ``(watermark, page_number)`` to place after the letterhead."""
        self.check_keys(page, _PAGE_KEYS, "page")
        doc = self.doc

        # Page size and orientation.
        size_key = page.get("pageSize")
        dims = None
        if size_key is not None:
            dims = _PAGE_SIZES.get(str(size_key).strip().lower())
            if dims is None:
                self.invalid("page.pageSize")
        orient = page.get("orientation")
        landscape = None
        if orient is not None:
            o = str(orient).strip().lower()
            if o == "landscape":
                landscape = True
            elif o == "portrait":
                landscape = False
            else:
                self.invalid("page.orientation")
        if dims is not None or landscape is not None:
            for section in doc.sections:
                w, h = dims if dims else (section.page_width, section.page_height)
                if w is None or h is None:
                    continue
                w, h = int(w), int(h)
                want_land = landscape if landscape is not None else (w > h)
                short, long_ = min(w, h), max(w, h)
                if want_land:
                    section.orientation = WD_ORIENT.LANDSCAPE
                    section.page_width, section.page_height = Emu(long_), Emu(short)
                else:
                    section.orientation = WD_ORIENT.PORTRAIT
                    section.page_width, section.page_height = Emu(short), Emu(long_)
            self.r.page_settings += int(dims is not None) + int(landscape is not None)

        # Margins.
        margins = page.get("margins")
        if margins is not None:
            if not isinstance(margins, dict):
                self.invalid("page.margins")
            else:
                self.check_keys(margins, _MARGIN_KEYS, "page.margins")
                for side in ("top", "bottom", "left", "right"):
                    if side not in margins:
                        continue
                    emu = parse_length(margins.get(side))
                    if emu is None:
                        self.invalid(f"page.margins.{side}")
                        continue
                    for section in doc.sections:
                        setattr(section, f"{side}_margin", Emu(emu))
                    self.r.page_settings += 1

        # Font, size, line spacing, alignment.
        font = None
        if page.get("font") is not None:
            font = parse_font(page.get("font"))
            if font is None:
                self.invalid("page.font")
        size = None
        if page.get("fontSize") is not None:
            size = parse_font_size(page.get("fontSize"))
            if size is None:
                self.invalid("page.fontSize")
        spacing = None
        if page.get("lineSpacing") is not None:
            spacing = parse_line_spacing(page.get("lineSpacing"))
            if spacing is None:
                self.invalid("page.lineSpacing")
        align = None
        if page.get("alignment") is not None:
            a = str(page.get("alignment")).strip().lower()
            if a in ("justified", "justify", "both"):
                align = WD_ALIGN_PARAGRAPH.JUSTIFY
            elif a == "left":
                align = WD_ALIGN_PARAGRAPH.LEFT
            else:
                self.invalid("page.alignment")
        if font:
            self.page_font = font
            self._apply_font(font)
            self.r.page_settings += 1
        if size:
            self._apply_size(size)
            self.r.page_settings += 1
        if spacing:
            self._apply_spacing(spacing)
            self.r.page_settings += 1
        if align is not None:
            self._apply_alignment(align)
            self.r.page_settings += 1

        # Watermark and page number are placed after the letterhead (which
        # replaces the header and footer they live in).
        watermark: Optional[str] = None
        if page.get("watermark") is not None:
            wm = str(page.get("watermark")).strip().upper()
            if wm in _WATERMARKS:
                watermark = wm
            elif wm == "NONE" or wm == "":
                watermark = "NONE"
            else:
                self.invalid("page.watermark")
        page_number: Any = None
        if page.get("pageNumber") is not None:
            h, v, ok = parse_page_number(page.get("pageNumber"))
            if not ok:
                self.invalid("page.pageNumber")
            else:
                page_number = (h, v) if h else "none"
        return watermark, page_number

    def _body_paragraphs(self):
        return list(self.doc.paragraphs)

    def _apply_font(self, font: str) -> None:
        doc = self.doc
        try:
            defaults = doc.styles.element.find(qn("w:docDefaults"))
            if defaults is not None:
                rpr_default = defaults.find(qn("w:rPrDefault"))
                rpr = rpr_default.find(qn("w:rPr")) if rpr_default is not None else None
                if rpr is not None:
                    _set_rfonts(rpr, font)
        except Exception:
            pass
        for style in doc.styles:
            try:
                if style.type not in (WD_STYLE_TYPE.PARAGRAPH, WD_STYLE_TYPE.CHARACTER):
                    continue
                name = (style.name or "")
                rpr = style.element.rPr
                own_fonts = rpr is not None and rpr.rFonts is not None
                if name == "Normal" or name.startswith("Heading") or name in (
                        "Title", "Subtitle") or own_fonts:
                    if own_fonts:
                        cur = (rpr.rFonts.get(qn("w:ascii")) or "").lower()
                        if cur in _SYMBOL_FONTS:
                            continue
                    _set_rfonts(style.element.get_or_add_rPr(), font)
            except Exception:
                continue
        # Direct formatting on body runs (a filled template carries its own).
        body = doc.element.body
        for r in body.iter(qn("w:r")):
            rpr = r.rPr
            if rpr is None or rpr.rFonts is None:
                continue
            cur = (rpr.rFonts.get(qn("w:ascii")) or "").lower()
            if cur in _SYMBOL_FONTS or r.find(qn("w:sym")) is not None:
                continue
            _set_rfonts(rpr, font)

    def _apply_size(self, size: float) -> None:
        doc = self.doc
        try:
            doc.styles["Normal"].font.size = Pt(size)
        except Exception:
            pass
        # Runs whose explicit size is the body's usual size follow the new
        # one; deliberately larger or smaller runs keep theirs.
        counts: Dict[str, int] = {}
        runs = []
        for p in self._body_paragraphs():
            if _is_heading_para(p):
                continue
            for r in p._p.iter(qn("w:r")):
                rpr = r.rPr
                sz = rpr.find(qn("w:sz")) if rpr is not None else None
                if sz is not None and sz.get(qn("w:val")):
                    counts[sz.get(qn("w:val"))] = counts.get(sz.get(qn("w:val")), 0) + 1
                    runs.append(rpr)
        if not counts:
            return
        usual = max(counts, key=counts.get)
        half_points = str(int(round(size * 2)))
        for rpr in runs:
            for tag in ("w:sz", "w:szCs"):
                el = rpr.find(qn(tag))
                if el is not None and el.get(qn("w:val")) == usual:
                    el.set(qn("w:val"), half_points)

    def _apply_spacing(self, spacing: float) -> None:
        doc = self.doc
        try:
            doc.styles["Normal"].paragraph_format.line_spacing = spacing
        except Exception:
            pass
        line = str(int(round(240 * spacing)))
        for p in self._body_paragraphs():
            if _is_heading_para(p):
                continue
            ppr = p._p.pPr
            sp = ppr.find(qn("w:spacing")) if ppr is not None else None
            if sp is not None and sp.get(qn("w:line")) is not None:
                sp.set(qn("w:line"), line)
                sp.set(qn("w:lineRule"), "auto")

    def _apply_alignment(self, align) -> None:
        doc = self.doc
        try:
            doc.styles["Normal"].paragraph_format.alignment = align
        except Exception:
            pass
        for p in self._body_paragraphs():
            if _is_heading_para(p):
                continue
            eff = _effective_alignment(p)
            if eff in (WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.RIGHT):
                continue
            if align == WD_ALIGN_PARAGRAPH.JUSTIFY:
                # Word stretches a justified line that ends in a manual break:
                # address and signature blocks would spread across the page.
                if _has_line_break(p):
                    continue
                if not p.text.strip():
                    continue
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            elif eff == WD_ALIGN_PARAGRAPH.JUSTIFY:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # -- images --------------------------------------------------------------

    def get_image(self, url: Any) -> Optional[bytes]:
        if not isinstance(url, str) or not url.strip():
            return None
        data = None
        if self.fetch_image is not None:
            try:
                data = self.fetch_image(url.strip())
            except Exception:
                data = None
        if not isinstance(data, (bytes, bytearray)) or image_kind(bytes(data)) is None:
            self.r.images_failed += 1
            return None
        return bytes(data)

    def add_picture(self, paragraph, data: bytes, width: int) -> bool:
        try:
            paragraph.add_run().add_picture(BytesIO(data), width=Emu(max(int(width), int(Inches(0.5)))))
        except Exception:
            self.r.images_failed += 1
            return False
        self.r.images_ok += 1
        return True

    # -- letterhead ----------------------------------------------------------

    def _block_spec(self, lh: Dict[str, Any], prefix: str, side: str) -> Optional[Dict[str, Any]]:
        def get(attr):
            return lh.get(f"{prefix}{attr}{side}")

        show = _as_bool(get("Show"))
        if get("Show") is not None and show is None:
            self.invalid(f"letterhead.{prefix}Show{side}")
        if show is False:
            return None
        html_key = f"{prefix}Html{side}"
        lines: List[List[Segment]]
        if html_key in lh and lh.get(html_key) is not None:
            html = lh.get(html_key)
            lines = html_to_lines(html) if isinstance(html, str) else []
            base_bold = None
        else:
            text = _clean(get("Text"))
            lines = [[(ln.strip(), False, False, False)] if ln.strip() else []
                     for ln in text.replace("\r\n", "\n").split("\n")]
            while lines and not lines[-1]:
                lines.pop()
            while lines and not lines[0]:
                lines.pop(0)
            base_bold = _as_bool(get("Bold"))
        spec = {
            "lines": lines,
            "bold": base_bold,
            "italic": _as_bool(get("Italic")),
            "underline": _as_bool(get("Underline")),
            "font": parse_font(get("FontFamily")),
            "size": parse_font_size(get("FontSize")),
            "spacing": parse_line_spacing(
                get("Spacing") if get("Spacing") is not None else get("LineSpacing")),
            "align": parse_align(get("Align")),
            "image": get("Image"),
        }
        for attr, key in (("FontFamily", "font"), ("FontSize", "size"), ("Align", "align")):
            if get(attr) not in (None, "") and spec[key] is None:
                self.invalid(f"letterhead.{prefix}{attr}{side}")
        return spec

    def _write_block(self, paragraph, spec: Dict[str, Any], default_align) -> None:
        paragraph.alignment = spec["align"] if spec["align"] is not None else default_align
        pf = paragraph.paragraph_format
        pf.space_after = Pt(0)
        pf.space_before = Pt(0)
        if spec["spacing"]:
            pf.line_spacing = spec["spacing"]
        font = spec["font"] or self.page_font
        for li, line in enumerate(spec["lines"]):
            if li > 0:
                paragraph.add_run().add_break()
            for text, b, i, u in line:
                run = paragraph.add_run(_clean(text))
                _run_font(run, font)
                if spec["size"]:
                    run.font.size = Pt(spec["size"])
                bold = b if spec["bold"] is None else (spec["bold"] or b)
                if bold or spec["bold"] is False:
                    run.bold = bool(bold)
                if i or spec["italic"] is not None:
                    run.italic = bool(i or spec["italic"])
                if u or spec["underline"] is not None:
                    run.underline = bool(u or spec["underline"])

    def _build_story(self, hf, lh: Dict[str, Any], prefix: str, image_mode: bool,
                     full_image_key: str, width: int) -> int:
        """Write one header or footer; returns the number of blocks written."""
        if image_mode and isinstance(lh.get(full_image_key), str) and lh.get(full_image_key).strip():
            data = self.get_image(lh.get(full_image_key))
            if data is None:
                return 0
            p = hf.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            return 1 if self.add_picture(p, data, width) else 0

        defaults = {"Left": WD_ALIGN_PARAGRAPH.LEFT, "Center": WD_ALIGN_PARAGRAPH.CENTER,
                    "Right": WD_ALIGN_PARAGRAPH.RIGHT}
        blocks: Dict[str, Dict[str, Any]] = {}
        for side in _SIDES:
            spec = self._block_spec(lh, prefix, side)
            if spec is None:
                continue
            if image_mode:
                data = self.get_image(spec["image"]) if spec["image"] else None
                if data is None:
                    continue
                spec["image_bytes"] = data
            elif not spec["lines"]:
                continue
            blocks[side] = spec
        if not blocks:
            return 0

        written = 0
        if len(blocks) == 1:
            side, spec = next(iter(blocks.items()))
            p = hf.add_paragraph()
            if image_mode:
                p.alignment = spec["align"] if spec["align"] is not None else defaults[side]
                written += int(self.add_picture(p, spec["image_bytes"], width // 3))
            else:
                self._write_block(p, spec, defaults[side])
                written += 1
            return written

        table = hf.add_table(1, 3, Emu(width))
        _remove_borders(table)
        col = width // 3
        for idx, side in enumerate(_SIDES):
            cell = table.cell(0, idx)
            cell.width = Emu(col)
            spec = blocks.get(side)
            p = cell.paragraphs[0]
            if spec is None:
                continue
            if image_mode:
                p.alignment = spec["align"] if spec["align"] is not None else defaults[side]
                written += int(self.add_picture(p, spec["image_bytes"], col - int(Inches(0.1))))
            else:
                self._write_block(p, spec, defaults[side])
                written += 1
        return written

    def apply_letterhead(self, lh: Dict[str, Any]) -> None:
        self.check_keys(lh, _LETTERHEAD_KEYS, "letterhead")
        doc = self.doc
        app = lh.get("pageApplication")
        first_only = False
        if app is not None:
            a = str(app).strip().lower()
            if a == "first":
                first_only = True
            elif a != "all":
                self.invalid("letterhead.pageApplication")

        def mode(key: str) -> bool:
            v = lh.get(key)
            if v is None:
                return False
            s = str(v).strip().lower()
            if s not in ("text", "image"):
                self.invalid(f"letterhead.{key}")
                return False
            return s == "image"

        header_image = mode("headerType")
        footer_image = mode("footerType")

        # The letterhead replaces whatever header/footer the file had.
        try:
            doc.settings.odd_and_even_pages_header_footer = False
        except Exception:
            pass
        sections = list(doc.sections)
        first = sections[0]
        for i, section in enumerate(sections):
            if i == 0:
                section.different_first_page_header_footer = first_only
                for attr in ("header", "footer", "first_page_header", "first_page_footer",
                             "even_page_header", "even_page_footer"):
                    hf = getattr(section, attr)
                    hf.is_linked_to_previous = False
                    _clear_story(hf)
            else:
                section.different_first_page_header_footer = False
                for attr in ("header", "footer", "first_page_header", "first_page_footer",
                             "even_page_header", "even_page_footer"):
                    getattr(section, attr).is_linked_to_previous = True

        width = _text_width(first)
        target_header = first.first_page_header if first_only else first.header
        target_footer = first.first_page_footer if first_only else first.footer
        self.r.letterhead_blocks += self._build_story(
            target_header, lh, "header", header_image, "headerImageUrl", width)
        self.r.letterhead_blocks += self._build_story(
            target_footer, lh, "footer", footer_image, "footerImageUrl", width)
        for attr in ("header", "footer", "first_page_header", "first_page_footer",
                     "even_page_header", "even_page_footer"):
            _ensure_paragraph(getattr(first, attr))

    # -- court ---------------------------------------------------------------

    def apply_court(self, court: Dict[str, Any]) -> None:
        self.check_keys(court, _COURT_KEYS, "court")
        items: List[Tuple[str, Dict[str, Any]]] = []
        for key in _COURT_SINGLE:
            block = court.get(key)
            if block is None:
                continue
            if isinstance(block, str):
                block = {"text": block}
            if not isinstance(block, dict):
                self.invalid(f"court.{key}")
                continue
            self.check_keys(block, _COURT_BLOCK_KEYS, f"court.{key}")
            items.append((key, block))
        fields = court.get("customFields")
        if fields is not None:
            if not isinstance(fields, list):
                self.invalid("court.customFields")
            else:
                for n, block in enumerate(fields):
                    if not isinstance(block, dict):
                        self.invalid(f"court.customFields[{n}]")
                        continue
                    self.check_keys(block, _COURT_BLOCK_KEYS, "court.customFields[]")
                    items.append(("customField", block))

        prepared: List[Tuple[str, Dict[str, Any]]] = []
        for key, block in items:
            text = _clean(block.get("text")).strip() if isinstance(block.get("text"), str) else ""
            if not text and key == "customField":
                label = _clean(block.get("label")).strip() if block.get("label") is not None else ""
                value = _clean(block.get("value")).strip() if block.get("value") is not None else ""
                text = f"{label}: {value}" if label and value else (value or label)
            if not text:
                continue
            if _as_bool(block.get("uppercase")):
                text = text.upper()
            prepared.append((text, block))
        if not prepared:
            return

        # The model often writes the court heading itself: skip a block the
        # draft already starts with. The window grows with the block count so
        # a second application never duplicates the block it inserted.
        window = 8 + len(prepared)
        opening = []
        for p in self.doc.paragraphs:
            t = _norm(p.text)
            if t:
                opening.append(t)
            if len(opening) >= window:
                break

        body = self.doc.element.body
        anchor = None
        for child in body:
            if child.tag != qn("w:sectPr"):
                anchor = child
                break
        inserted = []
        for text, block in prepared:
            pattern = _block_pattern(text)
            if pattern is not None and any(pattern.search(o) for o in opening):
                self.r.court_duplicates_skipped += 1
                continue
            p = self.doc.add_paragraph()
            if anchor is not None:
                anchor.addprevious(p._p)
            self._style_court(p, text, block)
            inserted.append(p)
            self.r.court_blocks += 1
        if inserted:
            inserted[-1].paragraph_format.space_after = Pt(12)

    def _style_court(self, p, text: str, block: Dict[str, Any]) -> None:
        align = parse_align(block.get("align"))
        if block.get("align") is not None and align is None:
            self.invalid("court.align")
        p.alignment = align if align is not None else WD_ALIGN_PARAGRAPH.CENTER
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.keep_with_next = True
        spacing = parse_line_spacing(block.get("lineSpacing")) if block.get(
            "lineSpacing") is not None else None
        if spacing:
            pf.line_spacing = spacing
        font = parse_font(block.get("font")) or self.page_font
        size = parse_font_size(block.get("fontSize")) if block.get("fontSize") is not None else None
        for li, line in enumerate(text.replace("\r\n", "\n").split("\n")):
            if li > 0:
                p.add_run().add_break()
            run = p.add_run(line)
            _run_font(run, font)
            if size:
                run.font.size = Pt(size)
            for attr in ("bold", "italic", "underline"):
                v = _as_bool(block.get(attr))
                if v is not None:
                    setattr(run, attr, v)

    # -- watermark & page number ---------------------------------------------

    def remove_watermarks(self) -> None:
        for hf in _all_stories(self.doc):
            for p_el in list(hf._element.iter(qn("w:p"))):
                hit = False
                for el in p_el.iter():
                    if isinstance(el.tag, str) and el.tag.endswith("}shape") and (
                            el.get("id") or "").startswith("PowerPlusWaterMarkObject"):
                        hit = True
                        break
                if not hit:
                    continue
                for r in list(p_el.findall(qn("w:r"))):
                    if any(isinstance(e.tag, str) and e.tag.endswith("}shape")
                           and (e.get("id") or "").startswith("PowerPlusWaterMarkObject")
                           for e in r.iter()):
                        p_el.remove(r)
                if not "".join(t.text or "" for t in p_el.iter(qn("w:t"))).strip() and \
                        p_el.find(qn("w:r")) is None:
                    _remove_paragraph(p_el)

    def ensure_paragraphs(self) -> None:
        for hf in _all_stories(self.doc):
            _ensure_paragraph(hf)

    def add_watermark(self, text: str) -> None:
        w, h = _WATERMARK_BOX.get(text, (450, 150))
        font = (self.page_font or "Calibri").replace('"', "")
        for n, hf in enumerate(_target_stories(self.doc, "header"), start=1):
            xml = _WATERMARK_XML.format(n=n, w=w, h=h, font=font, text=text)
            p = parse_xml(xml)
            hf._element.append(p)

    def remove_page_numbers(self) -> None:
        for hf in _all_stories(self.doc):
            for p_el in list(hf._element.iter(qn("w:p"))):
                if _is_page_number_paragraph(p_el):
                    _remove_paragraph(p_el)

    def add_page_number(self, horizontal: str, vertical: str) -> None:
        from app.processing.docx_generator import add_field

        kind = "header" if vertical == "top" else "footer"
        align = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                 "right": WD_ALIGN_PARAGRAPH.RIGHT}[horizontal]
        for hf in _target_stories(self.doc, kind):
            p = hf.add_paragraph()
            p.alignment = align
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            add_field(p, "PAGE")
            if self.page_font:
                for r in p.runs:
                    _run_font(r, self.page_font)


_WATERMARK_XML = (
    '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office">'
    '<w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>'
    '<w:r><w:rPr><w:noProof/></w:rPr><w:pict>'
    '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" '
    'path="m@7,l@8,m@5,21600l@6,21600e">'
    '<v:formulas>'
    '<v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/>'
    '<v:f eqn="sum 0 0 @2"/><v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>'
    '<v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/><v:f eqn="if @0 @4 21600"/>'
    '<v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>'
    '<v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/>'
    '</v:formulas>'
    '<v:path textpathok="t" o:connecttype="custom" '
    'o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" '
    'o:connectangles="270,180,90,0"/>'
    '<v:textpath on="t" fitshape="t"/>'
    '<v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
    '<o:lock v:ext="edit" text="t" shapetype="t"/>'
    '</v:shapetype>'
    '<v:shape id="PowerPlusWaterMarkObject{n}" o:spid="_x0000_s20{n:02d}" '
    'type="#_x0000_t136" '
    'style="position:absolute;margin-left:0;margin-top:0;width:{w}pt;height:{h}pt;'
    'rotation:315;z-index:-251657216;mso-position-horizontal:center;'
    'mso-position-horizontal-relative:margin;mso-position-vertical:center;'
    'mso-position-vertical-relative:margin" o:allowincell="f" '
    'fillcolor="silver" stroked="f">'
    '<v:fill opacity=".5"/>'
    '<v:textpath style="font-family:&quot;{font}&quot;;font-size:1pt" string="{text}"/>'
    '</v:shape></w:pict></w:r></w:p>'
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_format_layout(
    docx_bytes: bytes,
    format_layout: Any,
    *,
    fetch_image: Optional[FetchImage] = None,
) -> FormatResult:
    """Apply a chosen court or letterhead format to an existing .docx.

    Raises only when ``docx_bytes`` cannot be opened as a Word file. Any bad
    setting is skipped and reported in the result.
    """
    doc = Document(BytesIO(docx_bytes))  # the only call allowed to raise
    result = FormatResult(docx_bytes=docx_bytes)
    ftype, formatting = extract_formatting(format_layout)
    if not formatting:
        return result
    result.format_type = ftype or "unknown"
    f = _Formatter(doc, result, fetch_image)
    f.check_keys(formatting, _TOP_KEYS, "")

    def safely(step: str, fn, *args):
        try:
            return fn(*args)
        except Exception:
            f.invalid(f"{step}:error")
            return None

    watermark = page_number = None
    page = formatting.get("page")
    if isinstance(page, dict):
        out = safely("page", f.apply_page, page)
        if out:
            watermark, page_number = out
    elif page is not None:
        f.invalid("page")

    lh = formatting.get("letterhead")
    court = formatting.get("court")
    use_letterhead = ftype == "letterhead" or (not ftype and isinstance(lh, dict))
    use_court = ftype == "court" or (not ftype and isinstance(court, dict))
    if use_letterhead:
        if isinstance(lh, dict):
            safely("letterhead", f.apply_letterhead, lh)
        elif lh is not None:
            f.invalid("letterhead")
    if use_court:
        if isinstance(court, dict):
            safely("court", f.apply_court, court)
        elif court is not None:
            f.invalid("court")

    if watermark is not None:
        safely("watermark", f.remove_watermarks)
        if watermark != "NONE":
            safely("watermark", f.add_watermark, watermark)
        result.watermark = watermark.lower() if watermark == "NONE" else watermark
        result.page_settings += 1
    if page_number is not None:
        safely("pageNumber", f.remove_page_numbers)
        if page_number == "none":
            result.page_number = "none"
        else:
            h, v = page_number
            safely("pageNumber", f.add_page_number, h, v)
            result.page_number = f"{h}_{v}"
        result.page_settings += 1
    if watermark is not None or page_number is not None:
        safely("headerFooter", f.ensure_paragraphs)

    out = BytesIO()
    doc.save(out)
    result.docx_bytes = out.getvalue()
    return result


def summary_fields(result: FormatResult) -> Dict[str, Any]:
    """Counts and enum values for the ``draft_format applied`` log line."""
    names = ",".join(sorted(result.unknown_keys)[:10])
    return {
        "type": result.format_type,
        "page": result.page_settings,
        "watermark": result.watermark,
        "page_number": result.page_number,
        "letterhead_blocks": result.letterhead_blocks,
        "court_blocks": result.court_blocks,
        "court_duplicates": result.court_duplicates_skipped,
        "images_ok": result.images_ok,
        "images_failed": result.images_failed,
        "unknown_keys": len(result.unknown_keys),
        "unknown_names": names or "-",
        "invalid": len(result.invalid_values),
    }


# ---------------------------------------------------------------------------
# Session helpers (used by the drafting agent)
# ---------------------------------------------------------------------------

_IMAGE_LINK_KEYS = {"headerImageUrl", "footerImageUrl"} | {
    f"{prefix}Image{side}" for prefix in ("header", "footer") for side in _SIDES
}


def select_format_layout(*candidates: Any) -> Optional[Dict[str, Any]]:
    """The first candidate that carries usable formatting, as a dict.

    Callers pass the request's top-level ``formatLayout`` first, then
    ``draftOrigin.formatLayout``. JSON strings are accepted.
    """
    for cand in candidates:
        if isinstance(cand, str):
            try:
                cand = json.loads(cand)
            except (ValueError, TypeError):
                continue
        if not isinstance(cand, dict):
            continue
        _, formatting = extract_formatting(cand)
        if formatting:
            return cand
    return None


def without_image_links(format_layout: Dict[str, Any]) -> Dict[str, Any]:
    """A copy safe to persist: letterhead image links removed.

    Image links are short-lived signed links; they are used for the turn that
    brought them and never stored. The backend sends the format on every
    drafting message, so the next turn brings fresh ones.
    """
    try:
        data = json.loads(json.dumps(format_layout, default=str))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("formatting"), str):
        try:
            data["formatting"] = json.loads(data["formatting"])
        except (ValueError, TypeError):
            data["formatting"] = None
    holder = data.get("formatting") if isinstance(data.get("formatting"), dict) else data
    lh = holder.get("letterhead") if isinstance(holder, dict) else None
    if isinstance(lh, dict):
        for key in list(lh):
            if key in _IMAGE_LINK_KEYS:
                lh.pop(key, None)
    return data
