"""Format checklist: does a .docx follow its source template? (SIM-207)

Compares a rendered .docx with the template's format model on the things a
lawyer notices: body font and size, title text and alignment, headings,
numbering labels, tables (count, columns, cell text), letterhead text and
logo, page numbers, margins, line spacing, underlines and signature blocks
kept together. Used by the corpus tests for every reader (docx, PDF, scan)
and by the fill tests.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from io import BytesIO
from typing import Dict, List, Optional

from docx import Document
from docx.oxml.ns import qn

from app.processing.template_fidelity.ir import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_LEFT,
    ALIGN_RIGHT,
    ROLE_HEADING,
    ROLE_SIGNATURE,
    ROLE_TITLE,
    TemplateIR,
    TImage,
    TPara,
)

LABEL_RE = re.compile(
    r"^\s*(Clause\s+\d+\.?|\d{1,3}(?:\.\d{1,3})*[.)]?|\((?:[a-z]{1,2}|[ivxl]{1,5}|\d{1,3})\)"
    r"|(?:[ivxl]{1,5}|[a-z])[.)])(?=\s|\t)",
    re.IGNORECASE,
)
_ALIGN_NAMES = {0: ALIGN_LEFT, 1: ALIGN_CENTER, 2: ALIGN_RIGHT, 3: ALIGN_JUSTIFY}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\t", " ")).strip().lower()


def _para_align(p) -> str:
    a = p.alignment
    if a is None:
        a = p.style.paragraph_format.alignment if p.style is not None else None
    return _ALIGN_NAMES.get(int(a) if a is not None else 0, ALIGN_LEFT)


def _run_font(run, doc) -> Optional[str]:
    if run.font.name:
        return run.font.name
    try:
        return doc.styles["Normal"].font.name
    except KeyError:
        return None


def _run_size(run, para, doc) -> Optional[float]:
    if run.font.size:
        return run.font.size.pt
    if para.style is not None and para.style.font.size:
        return para.style.font.size.pt
    try:
        s = doc.styles["Normal"].font.size
        return s.pt if s else None
    except KeyError:
        return None


def docx_label(text: str) -> str:
    m = LABEL_RE.match(text or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def check_format(ir: TemplateIR, docx_bytes: bytes) -> Dict[str, bool]:
    """One boolean per format property; all True means the format was kept."""
    doc = Document(BytesIO(docx_bytes))
    paras = list(doc.paragraphs)
    texts = [_norm(p.text) for p in paras]
    body_text = " \n ".join(texts)
    out: Dict[str, bool] = {}

    # Body font and size (majority over runs with text).
    fonts: Counter = Counter()
    sizes: List[float] = []
    for p in paras:
        for r in p.runs:
            if r.text.strip():
                fonts[_run_font(r, doc)] += len(r.text)
                s = _run_size(r, p, doc)
                if s:
                    sizes.append(s)
    out["body_font"] = bool(fonts) and fonts.most_common(1)[0][0] == ir.font
    out["body_size"] = bool(sizes) and abs(statistics.median(sizes) - ir.size) <= 0.6

    # Title text and alignment.
    titles = [b for b in ir.blocks if isinstance(b, TPara) and b.role == ROLE_TITLE]
    if titles:
        t = _norm(titles[0].text)
        hit = next((p for p, n in zip(paras, texts) if n == t), None)
        out["title"] = hit is not None and _para_align(hit) == titles[0].align
    # Headings present and bold.
    heads = [b for b in ir.blocks if isinstance(b, TPara) and b.role in (ROLE_HEADING,)]
    if heads:
        ok = 0
        for h in heads:
            hn = _norm(h.text)
            hit = next((p for p, n in zip(paras, texts) if n == hn), None)
            if hit is not None and any(r.bold or (hit.style is not None and hit.style.font.bold)
                                       for r in hit.runs if r.text.strip()):
                ok += 1
        out["headings"] = ok >= 0.9 * len(heads)

    # Numbering labels, in order.
    want = [re.sub(r"\s+", " ", b.label) for b in ir.blocks
            if isinstance(b, TPara) and b.label]
    if want:
        got = [docx_label(p.text) for p in paras]
        got = [g for g in got if g]
        matched = sum(1 for a, b in zip(want, got) if a.lower() == b.lower())
        out["numbering"] = matched >= 0.9 * len(want)

    # Tables: count, columns, cell text.
    src_tables = ir.tables()
    if src_tables or doc.tables:
        out["table_count"] = len(src_tables) == len(doc.tables)
        cols_ok, cells, found = True, 0, 0
        for st, dt in zip(src_tables, doc.tables):
            ncols = max(sum(max(c.span, 1) for c in row) for row in st.rows)
            cols_ok &= len(dt.columns) == ncols
            dtexts = {_norm(c.text) for row in dt.rows for c in row.cells}
            for row in st.rows:
                for c in row:
                    if c.text.strip():
                        cells += 1
                        found += _norm(c.text) in dtexts
        out["table_columns"] = cols_ok
        out["table_cells"] = cells == 0 or found >= 0.9 * cells

    # Letterhead text and logo in the header.
    section = doc.sections[0]
    head_xml = section.header._element.xml
    head_text = _norm(" ".join(p.text for p in section.header.paragraphs))
    head_paras = [b for b in ir.header if isinstance(b, TPara) and b.text.strip()]
    if head_paras:
        out["letterhead_text"] = all(_norm(b.text.split("\n")[0]) in head_text
                                     for b in head_paras)
    if any(isinstance(b, TImage) for b in ir.header):
        out["letterhead_logo"] = "<w:drawing" in head_xml or "<w:pict" in head_xml
    foot_xml = section.footer._element.xml
    out["page_numbers"] = ("PAGE" in foot_xml) == ir.page_numbers

    # Margins (within 9 pt).
    pg = ir.page
    out["margins"] = (abs(section.left_margin.pt - pg.margin_left) <= 9
                      and abs(section.right_margin.pt - pg.margin_right) <= 9)

    # Line spacing of body paragraphs (median, within 0.2).
    src_ls = [b.line_spacing for b in ir.blocks if isinstance(b, TPara) and b.text.strip()
              and b.role not in (ROLE_TITLE, ROLE_HEADING)]
    got_ls = []
    for p in paras:
        if p.text.strip():
            ls = p.paragraph_format.line_spacing
            if isinstance(ls, float):
                got_ls.append(ls)
    if src_ls and got_ls:
        out["line_spacing"] = abs(statistics.median(src_ls) - statistics.median(got_ls)) <= 0.2

    # Underlined text kept underlined.
    under = [_norm(r.text) for b in ir.paragraphs() for r in b.runs if r.underline and r.text.strip()]
    if under:
        got_under = {_norm(r.text) for p in paras for r in p.runs if r.underline}
        out["underline"] = sum(1 for u in under if any(u in g for g in got_under)) >= 0.9 * len(under)

    # Signature blocks kept on one page.
    sigs = [b for b in ir.blocks if isinstance(b, TPara) and b.role == ROLE_SIGNATURE]
    if sigs:
        ok = 0
        for s in sigs:
            first = _norm(s.text.split("\n")[0])
            hit = next((p for p, n in zip(paras, texts) if n.startswith(first)), None)
            if hit is not None and (hit.paragraph_format.keep_together
                                    or len(hit._p.findall(".//" + qn("w:br"))) == 0):
                ok += 1
        out["signature_blocks"] = ok >= 0.9 * len(sigs)

    out["no_markup"] = not re.search(r"(^|\n) ?(\||#{1,6} |-{3,}$)", body_text)
    return out


def score(checks: Dict[str, bool]) -> float:
    return sum(checks.values()) / len(checks) if checks else 0.0


def failed(checks: Dict[str, bool]) -> List[str]:
    return [k for k, v in checks.items() if not v]
