"""Scanned PDF / photo → format model (SIM-206).

Scans carry no text layer and no styles, so the format is measured:

- **Geometry** from OCR word boxes (Google Vision, ``ocr.ocr_word_boxes``):
  the page is de-skewed from the slope of the words, boxes are converted to
  points, words are grouped into lines, and font size comes from glyph
  height. ``layout.assemble`` then recovers paragraphs, alignment, indents,
  labels, spacing, header/footer and borderless tables, as for a PDF.
- **Style from the pixels**, no model needed: bold from ink density inside
  each word box, underline from a dark run just under a word, the letterhead
  logo from dark non-text area in the top band.
- **An optional Gemini vision pass** (one call per page, via the router)
  adds what pixels cannot settle: tables with their cell texts and header
  rows, and the font family. A failed or disabled pass leaves the
  measurements standing.

OCR and vision are injectable callables so the reader is tested on
simulated scans without calling either service.
"""

from __future__ import annotations

import io
import json
import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from app.processing.template_fidelity.ir import ALIGN_RIGHT, TCell, TemplateIR, TPara, TRun, TTable
from app.processing.template_fidelity.layout import Line, PageLines, Span, assemble, snap

Word = Tuple[float, float, float, float, str]
OcrFn = Callable[[bytes], List[Word]]
VisionFn = Callable[[bytes, List[str]], Awaitable[Optional[Dict[str, Any]]]]

# Glyph-box height (cap height to descender) as a fraction of the font size.
_GLYPH_EM = 0.94
_SIZES = (8, 9, 10, 10.5, 11, 12, 13, 14, 16, 18, 20, 22, 24, 28)
_A4 = (595.3, 841.9)


@dataclass
class ScanPageInput:
    image: bytes  # PNG/JPEG of one page
    width_pt: Optional[float] = None  # page size when known (scanned PDF)
    height_pt: Optional[float] = None


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _gray(image: bytes):
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image)).convert("L")
    return np.asarray(img, dtype=np.uint8), img.width, img.height


def _skew_angle(words: List[Word]) -> float:
    """Median slope (degrees) between neighbouring words on the same line."""
    slopes = []
    ws = sorted(words, key=lambda w: (w[1] + w[3]) / 2)
    for i, a in enumerate(ws):
        ha = a[3] - a[1]
        best = None
        # Look both ways: on a page tilted so lines climb to the right, the
        # next word sits slightly higher and sorts *before* this one.
        for b in ws[max(0, i - 40):i] + ws[i + 1:i + 40]:
            if b[0] <= a[2] or b[0] - a[2] > 3 * ha:
                continue
            dy = (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2
            if abs(dy) < 0.6 * ha and (best is None or b[0] < best[0]):
                best = b
        if best is not None:
            dx = (best[0] + best[2]) / 2 - (a[0] + a[2]) / 2
            dy = (best[1] + best[3]) / 2 - (a[1] + a[3]) / 2
            if dx > 0:
                slopes.append(math.degrees(math.atan2(dy, dx)))
    return statistics.median(slopes) if len(slopes) >= 5 else 0.0


def _rotate_words(words: List[Word], angle: float, w: int, h: int) -> List[Word]:
    """Undo a tilt of ``angle`` degrees (as measured by ``_skew_angle``)."""
    if abs(angle) < 0.05:
        return list(words)
    a = math.radians(-angle)
    ca, sa = math.cos(a), math.sin(a)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    cx, cy = w / 2.0, h / 2.0
    out = []
    for x0, y0, x1, y1, t in words:
        # The upright box of a tilted word is taller the longer the word
        # (H = h·cos + w·sin). Solve for the word's own width and height, or
        # long words read as a bigger font than short ones on the same line.
        bw, bh = x1 - x0, y1 - y0
        denom = c * c - s * s
        tw = max((bw * c - bh * s) / denom, 1.0) if denom > 0.5 else bw
        th = max((bh * c - bw * s) / denom, 1.0) if denom > 0.5 else bh
        mx, my = (x0 + x1) / 2 - cx, (y0 + y1) / 2 - cy
        nx, ny = cx + mx * ca - my * sa, cy + mx * sa + my * ca
        out.append((nx - tw / 2, ny - th / 2, nx + tw / 2, ny + th / 2, t))
    return out


def _straighten(gray, angle: float):
    """Rotate the page image to undo a measured tilt (same centre and sense
    as ``_rotate_words``)."""
    import numpy as np
    from PIL import Image

    img = Image.fromarray(gray).rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    return np.asarray(img, dtype=np.uint8)


def _ink(gray, box, pad: int = 0) -> float:
    import numpy as np

    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, gray.shape[1]), min(y1 + pad, gray.shape[0])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(np.mean(gray[y0:y1, x0:x1] < 128))


def _underlined(gray, box) -> bool:
    import numpy as np

    # An underline sits just under the baseline, i.e. inside the descender
    # zone at the bottom of the glyph box, not below it. Letters resting on
    # the baseline leave gaps; an underline is one continuous dark row.
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    h = max(y1 - y0, 4)
    lo, hi = max(y1 - int(0.3 * h), 0), min(y1 + max(3, int(0.35 * h)), gray.shape[0])
    if hi <= lo or x1 <= x0:
        return False
    band = gray[lo:hi, max(x0, 0):min(x1, gray.shape[1])] < 140
    if band.size == 0:
        return False
    return bool(np.max(np.mean(band, axis=1)) > 0.85)


def _logo(gray, words_px: List[Word]) -> Optional[Tuple[Tuple[int, int, int, int], bytes]]:
    """A dark, non-text area in the top band (the letterhead logo)."""
    import numpy as np
    from PIL import Image

    h, w = gray.shape
    band = (gray[: int(h * 0.15)] < 110).copy()
    for x0, y0, x1, y1, _t in words_px:
        if y0 < h * 0.16:
            band[max(int(y0) - 3, 0):int(y1) + 3, max(int(x0) - 3, 0):int(x1) + 3] = False
    cols = np.where(band.sum(axis=0) > 3)[0]
    rows = np.where(band.sum(axis=1) > 3)[0]
    if cols.size < 10 or rows.size < 10:
        return None
    # Largest run of consecutive dark columns.
    runs, start = [], cols[0]
    for a, b in zip(cols, cols[1:]):
        if b - a > 4:
            runs.append((start, a))
            start = b
    runs.append((start, cols[-1]))
    c0, c1 = max(runs, key=lambda r: r[1] - r[0])
    sub = band[:, c0:c1 + 1]
    rr = np.where(sub.sum(axis=1) > 2)[0]
    if rr.size == 0:
        return None
    r0, r1 = int(rr[0]), int(rr[-1])
    if (c1 - c0) < w * 0.03 or (r1 - r0) < h * 0.02:
        return None
    crop = Image.fromarray(gray[r0:r1 + 1, c0:c1 + 1])
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    return (int(c0), r0, int(c1) + 1, r1 + 1), buf.getvalue()


# ---------------------------------------------------------------------------
# Words → lines
# ---------------------------------------------------------------------------

_PUNCT = re.compile(r"^[\W_]+$")


def _smooth_styles(row: List[Tuple[Word, bool, bool]]) -> List[Tuple[Word, bool, bool]]:
    """Ink density is unreliable on tiny boxes: punctuation takes its
    neighbours' bold/underline, and a line that is mostly bold is bold."""
    out = list(row)
    for k, (w, b, u) in enumerate(out):
        if _PUNCT.match(w[4]):
            prev = out[k - 1] if k else None
            nxt = out[k + 1] if k + 1 < len(out) else None
            ref = prev or nxt
            if ref is not None:
                out[k] = (w, ref[1], ref[2] or (nxt[2] if nxt else False))
    chars = sum(len(w[4]) for w, _, _ in out) or 1
    bold_chars = sum(len(w[4]) for w, b, _ in out if b)
    if bold_chars >= 0.75 * chars:
        out = [(w, True, u) for w, _, u in out]
    elif bold_chars <= 0.15 * chars:
        out = [(w, False, u) for w, _, u in out]
    return out


def _lines(words_pt: List[Tuple[Word, bool, bool]], page_no: int) -> List[Line]:
    items = sorted(words_pt, key=lambda it: ((it[0][1] + it[0][3]) / 2, it[0][0]))
    rows: List[List[Tuple[Word, bool, bool]]] = []
    for it in items:
        w = it[0]
        cy, hh = (w[1] + w[3]) / 2, w[3] - w[1]
        if rows:
            last = rows[-1]
            ly = statistics.median((x[0][1] + x[0][3]) / 2 for x in last)
            lh = statistics.median(x[0][3] - x[0][1] for x in last)
            if abs(cy - ly) < 0.5 * max(lh, hh):
                last.append(it)
                continue
        rows.append([it])
    # Glyph heights on a scan carry a few percent of noise — enough to read a
    # 12 pt line as 13 pt. Lines within 10% of the body size are the body size.
    raw = [statistics.median(it[0][3] - it[0][1] for it in row) / _GLYPH_EM for row in rows]
    weights: Dict[float, int] = {}
    for row, r in zip(rows, raw):
        k = snap(r, _SIZES)
        weights[k] = weights.get(k, 0) + len(row)
    body = max(weights, key=weights.get) if weights else 12.0
    lines: List[Line] = []
    for row, r in zip(rows, raw):
        row.sort(key=lambda it: it[0][0])
        row[:] = _smooth_styles(row)
        size = body if abs(r - body) <= 0.1 * body else snap(r, _SIZES)
        spans = [Span(it[0][4] + " ", it[0][0], it[0][2], size, bold=it[1], underline=it[2])
                 for it in row]
        if spans:
            spans[-1].text = spans[-1].text.rstrip()
        # Median word top/bottom, not min/max: one capital or descender would
        # otherwise move the line and blur every line-pitch measurement.
        lines.append(Line(spans, row[0][0][0], statistics.median(it[0][1] for it in row),
                          row[-1][0][2], statistics.median(it[0][3] for it in row), page_no))
    return lines


# ---------------------------------------------------------------------------
# Vision (tables, font family)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _apply_vision_tables(pg: PageLines, tables: List[Dict[str, Any]], body_size: float) -> None:
    """Replace the OCR lines a vision table covers with the table itself."""
    for t in tables or []:
        rows = [[str(c) for c in r] for r in (t.get("rows") or []) if isinstance(r, list)]
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        want = [_norm(" ".join(r)) for r in rows]
        texts = [_norm(ln.text) for ln in pg.lines]
        start = _find_row(texts, want[0], 0)
        if start is None:
            start = _find_row(texts, want[1], 0)
            if start is None:
                continue
        end = start
        for wr in want[1:]:
            k = _find_row(texts, wr, end + 1, window=3)
            if k is not None:
                end = k
        region = pg.lines[start:end + 1]
        ncols = max(len(r) for r in rows)
        header_rows = int(t.get("header_rows") or 0)
        x0 = min(ln.x0 for ln in region)
        x1 = max(ln.x1 for ln in region)
        cells_rows: List[List[TCell]] = []
        for ri, r in enumerate(rows):
            cells = []
            for c in r + [""] * (ncols - len(r)):
                para = TPara(runs=[TRun(c, bold=ri < header_rows)] if c else [], space_after=2.0)
                if c and re.fullmatch(r"[\d,.%\s-]+", c) and any(ch.isdigit() for ch in c):
                    para.align = ALIGN_RIGHT
                cells.append(TCell(paras=[para]))
            cells_rows.append(cells)
        tbl = TTable(rows=cells_rows, col_widths=[(x1 - x0) / ncols] * ncols,
                     header_rows=min(header_rows, len(rows) - 1),
                     borders=bool(t.get("borders", True)))
        pg.tables.append(((x0, region[0].y0, x1, region[-1].y1), tbl))
        pg.lines = pg.lines[:start] + pg.lines[end + 1:]


def _find_row(texts: List[str], want: str, start: int, window: int = 400) -> Optional[int]:
    toks = set(want.split())
    if not toks:
        return None
    # Both ways: the row's words are in the line AND the line is mostly the
    # row's words — "No. | Reserved Matter" must not match the clause "No
    # Reserved Matter shall be undertaken ...".
    best, best_score = None, 0.0
    for k in range(start, min(len(texts), start + window)):
        have = set(texts[k].split())
        if not have:
            continue
        common = len(toks & have)
        recall, precision = common / len(toks), common / len(have)
        score = 2 * recall * precision / (recall + precision) if common else 0.0
        if score > best_score:
            best, best_score = k, score
    return best if best_score >= 0.75 else None


VISION_PROMPT = """You are given one scanned page of a legal document template and the
text lines an OCR engine read from it, numbered L0, L1, ...

Return ONLY JSON:
{"font": "serif" | "sans",
 "tables": [{"rows": [["cell", "cell", ...], ...], "header_rows": 0|1|2, "borders": true|false}],
 "bold_lines": [line numbers whose text is bold],
 "underlined_lines": [line numbers whose text is underlined]}

Rules: list a table only if the page shows a grid of cells (with or without
ruled lines). Copy cell text exactly as printed, one string per cell, empty
string for an empty cell, rows top to bottom. Do not include body paragraphs
or signature lines as tables.

OCR LINES:
"""


async def gemini_vision(image: bytes, lines: List[str]) -> Optional[Dict[str, Any]]:
    """One routed Gemini vision call for a page. None on any failure."""
    from app.ai.gemini_client import gemini_client
    from app.llm.errors import is_generation_error

    prompt = VISION_PROMPT + "\n".join("L{}: {}".format(i, t) for i, t in enumerate(lines))
    mime = "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"
    raw = await gemini_client.generate_with_images(
        prompt, [(image, mime)], task="template_scan_vision", max_tokens=8192)
    if not raw or is_generation_error(raw):
        logger.warning("template scan vision pass failed; using measured styles only")
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("template scan vision pass returned no JSON")
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def read_scan(pages_in: Sequence[ScanPageInput], *, ocr: Optional[OcrFn] = None,
                    vision: Optional[VisionFn] = None) -> TemplateIR:
    """Scanned pages → format model.

    ``ocr`` defaults to Vision OCR (run in a worker thread); ``vision`` is the
    optional Gemini pass (``gemini_vision``) — pass None to skip it.
    """
    import asyncio

    from app.processing.ocr import ocr_word_boxes

    ocr_fn = ocr or ocr_word_boxes
    pages: List[PageLines] = []
    fonts: List[str] = []
    for pno, pin in enumerate(pages_in):
        gray, wpx, hpx = await asyncio.to_thread(_gray, pin.image)
        words_px = await asyncio.to_thread(ocr_fn, pin.image)
        if not words_px:
            continue
        angle = _skew_angle(words_px)
        straight = _rotate_words(words_px, angle, wpx, hpx)
        if pin.width_pt and pin.height_pt:
            wpt, hpt = pin.width_pt, pin.height_pt
        else:
            # A photo: assume A4 in the image's orientation.
            wpt, hpt = _A4 if hpx >= wpx else (_A4[1], _A4[0])
        s = wpt / wpx
        # Straighten the pixels too, so style is read with the straightened
        # boxes: on a tilted page an underline climbs across a long word and
        # no single pixel row is fully dark.
        if abs(angle) >= 0.05:
            gray = await asyncio.to_thread(_straighten, gray, angle)
        dens = [_ink(gray, w[:4]) for w in straight]
        med = statistics.median(dens) if dens else 0.0
        words_pt = []
        for w, d in zip(straight, dens):
            bold = med > 0 and d > 1.22 * med
            under = _underlined(gray, w[:4])
            words_pt.append(((w[0] * s, w[1] * s, w[2] * s, w[3] * s, w[4]), bold, under))
        lines = _lines(words_pt, pno)
        pg = PageLines(lines=lines, width=wpt, height=hpt)
        logo = _logo(gray, straight)
        if logo is not None:
            (x0, y0, x1, y1), data = logo
            pg.images.append(((x0 * s, y0 * s, x1 * s, y1 * s), data))
        if vision is not None:
            try:
                v = await vision(pin.image, [ln.text for ln in pg.lines])
            except Exception as exc:
                logger.opt(exception=exc).warning("template scan vision pass raised")
                v = None
            if v:
                for i in v.get("bold_lines") or []:
                    if isinstance(i, int) and 0 <= i < len(pg.lines):
                        for sp in pg.lines[i].spans:
                            sp.bold = True
                for i in v.get("underlined_lines") or []:
                    if isinstance(i, int) and 0 <= i < len(pg.lines):
                        for sp in pg.lines[i].spans:
                            sp.underline = True
                body = statistics.median([ln.size for ln in pg.lines]) if pg.lines else 12.0
                _apply_vision_tables(pg, v.get("tables") or [], body)
                if v.get("font") in ("serif", "sans"):
                    fonts.append(v["font"])
        pages.append(pg)
    font = None
    if fonts:
        font = "Arial" if fonts.count("sans") > fonts.count("serif") else "Times New Roman"
    ir = assemble(pages, source="scan", width_tol=7.0, font_hint=font)
    logger.info("template scan read pages={} paragraphs={} tables={} header={} vision={}",
                len(pages), len(ir.paragraphs()), len(ir.tables()), bool(ir.header),
                vision is not None)
    return ir
