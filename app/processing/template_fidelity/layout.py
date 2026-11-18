"""Positioned text lines → format model (shared by the PDF and scan readers).

A reader gives each page as ``Line`` objects (text spans with their box and
style, in points, top-left origin). This module recovers what a lawyer laid
out: running header and footer (and "Page X of Y"), margins, body font and
size, paragraphs (including stacked signature/address lines), alignment,
indents and hanging labels, spacing, line spacing, headings and title,
forced page breaks, and borderless tables found from column gaps.

Everything is measured from the document itself (medians and clusters), so
no threshold assumes a particular template.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.processing.template_fidelity.checklist import LABEL_RE
from app.processing.template_fidelity.ir import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_LEFT,
    ALIGN_RIGHT,
    ROLE_BODY,
    ROLE_CLAUSE,
    ROLE_HEADING,
    ROLE_SIGNATURE,
    ROLE_TITLE,
    PageSetup,
    TCell,
    TemplateIR,
    TImage,
    TPara,
    TRun,
    TTable,
)


@dataclass
class Span:
    text: str
    x0: float
    x1: float
    size: float
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font: Optional[str] = None
    # Source text block (PDF block or OCR block); -1 unknown. Two table cells
    # are always different blocks, however small the visual gap between them.
    block: int = -1


@dataclass
class Line:
    spans: List[Span]
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0
    # Set while grouping: a short line stacked under the previous one
    # (signature/address blocks) — joined with a line break, not a space.
    stacked: bool = False

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.spans if s.text.strip())

    @property
    def size(self) -> float:
        sizes = [s.size for s in self.spans if s.text.strip()]
        return statistics.median(sizes) if sizes else 0.0

    @property
    def bold(self) -> bool:
        chars = [(len(s.text.strip()), s.bold) for s in self.spans if s.text.strip()]
        total = sum(c for c, _ in chars)
        return total > 0 and sum(c for c, b in chars if b) >= 0.8 * total


@dataclass
class PageLines:
    lines: List[Line]
    width: float
    height: float
    images: List[Tuple[Tuple[float, float, float, float], bytes]] = field(default_factory=list)
    tables: List[Tuple[Tuple[float, float, float, float], TTable]] = field(default_factory=list)


_DIGITS = re.compile(r"\d+")
# A clause label with its punctuation: "1.", "1.1", "(a)", "ii.", "Clause 3".
_PUNCT_LABEL_RE = re.compile(
    r"^(clause\s+\d+\.?|\d{1,3}(\.\d{1,3})*[.)]|\d{1,3}(\.\d{1,3})+|\([a-z0-9]{1,5}\)"
    r"|(?:[ivxl]{1,5}|[a-z])[.)])$",
    re.IGNORECASE)
_PAGE_NO = re.compile(r"^\s*(page\s*)?\d+\s*((of|/)\s*\d+)?\s*$", re.IGNORECASE)
_COMMON_SIZES = (8, 9, 10, 10.5, 11, 12, 13, 14, 16, 18, 20, 22, 24, 28)
_SPACINGS = (1.0, 1.15, 1.5, 2.0)


def snap(value: float, choices: Sequence[float]) -> float:
    return min(choices, key=lambda c: abs(c - value))


def _snap_pt(value: float, step: float = 3.0) -> float:
    return round(value / step) * step


# ---------------------------------------------------------------------------
# Running header / footer
# ---------------------------------------------------------------------------

def _band_key(line: Line) -> str:
    return _DIGITS.sub("#", line.text.lower()).strip()


def split_running(pages: List[PageLines]) -> Tuple[List[Line], List[Line], Optional[str]]:
    """Pull repeated top/bottom lines out of the body.

    Returns (header lines from page 1, footer lines from page 1, page-number
    format or None). A line is running when the same text (digits ignored)
    sits in the top or bottom band of most pages. With a single page, top
    band lines above the first body text and next to a logo are header.
    """
    n = len(pages)
    header: List[Line] = []
    footer: List[Line] = []
    page_fmt: Optional[str] = None
    if not n:
        return header, footer, page_fmt
    counts: Counter = Counter()
    for pg in pages:
        seen = set()
        for ln in pg.lines:
            if ln.y1 < pg.height * 0.14 or ln.y0 > pg.height * 0.9:
                k = ("t" if ln.y1 < pg.height * 0.14 else "b", _band_key(ln))
                if k not in seen:
                    seen.add(k)
                    counts[k] += 1
    need = max(2, int(0.6 * n + 0.5)) if n > 1 else 1
    for pi, pg in enumerate(pages):
        keep: List[Line] = []
        for ln in pg.lines:
            top = ln.y1 < pg.height * 0.14
            bottom = ln.y0 > pg.height * 0.9
            if bottom and _PAGE_NO.match(ln.text):
                page_fmt = _page_format(ln.text)
                continue
            key = ("t" if top else "b", _band_key(ln))
            running = (top or bottom) and n > 1 and counts[key] >= need
            if not running and n == 1 and top and pg.images:
                running = _beside_logo(ln, pg)
            if running:
                if pi == 0:
                    (header if top else footer).append(ln)
                continue
            keep.append(ln)
        pg.lines = keep
    return header, footer, page_fmt


def _beside_logo(ln: Line, pg: PageLines) -> bool:
    for (x0, y0, x1, y1), _ in pg.images:
        if y1 < pg.height * 0.18 and ln.y0 <= y1 + 6:
            return True
    return False


def _page_format(text: str) -> str:
    t = text.strip()
    nums = list(_DIGITS.finditer(t))
    if len(nums) >= 2:
        return t[:nums[0].start()] + "{PAGE}" + t[nums[0].end():nums[1].start()] + "{NUMPAGES}" + t[nums[1].end():]
    if nums:
        return t[:nums[0].start()] + "{PAGE}" + t[nums[0].end():]
    return "Page {PAGE} of {NUMPAGES}"


# ---------------------------------------------------------------------------
# Borderless tables from column gaps
# ---------------------------------------------------------------------------

def _segments(line: Line, gap: float) -> List[List[Span]]:
    segs: List[List[Span]] = [[]]
    prev: Optional[Span] = None
    for s in line.spans:
        if not s.text.strip():
            continue
        pdf = s.block >= 0 and prev is not None and prev.block >= 0
        # In a PDF, words of one style share a span (spaces included); two
        # same-style spans with a gap between them are two cells.
        same_style = prev is not None and (s.bold, s.italic, s.size, s.font) == (
            prev.bold, prev.italic, prev.size, prev.font)
        if prev is not None and (s.x0 - prev.x1 > gap or (
                pdf and (s.block != prev.block or same_style) and s.x0 - prev.x1 > 1.5)):
            segs.append([])
        segs[-1].append(s)
        prev = s
    return [g for g in segs if g]


def find_gap_tables(pg: PageLines, body_size: float, pitch: float) -> None:
    """Detect tables laid out with whitespace columns; move them to ``pg.tables``.

    Rows are lines broken by wide gaps; consecutive rows form a candidate
    table. Columns are the union of the cells' x-ranges over the rows with
    the most cells — so a right-aligned number and a left-aligned word in the
    next column stay apart, and a header cell that spans two columns maps to
    both. A candidate is kept only when most cells fall cleanly into one
    column, which prose with stretched word gaps never does.
    """
    gap = max(5.5, 0.5 * body_size)
    # Group consecutive rows; a one-segment line indented inside the table
    # right under a row is the wrapped second line of one of its cells.
    groups: List[dict] = []
    cur: Optional[dict] = None
    for i, ln in enumerate(pg.lines):
        segs = _segments(ln, gap)
        # "(a)   text" or "1.   text" is a hanging clause label, not a table
        # row; a bare number beside a wide gap ("1 | Amendment ...") is a cell.
        first_seg = " ".join(s.text for s in segs[0]).strip() if segs else ""
        label_pair = len(segs) == 2 and bool(_PUNCT_LABEL_RE.match(first_seg))
        is_row = len(segs) >= 2 and not label_pair and "____" not in ln.text
        close = cur is not None and i == cur["last"] + 1 and \
            ln.y0 - pg.lines[cur["last"]].y0 <= 2.2 * pitch
        if is_row:
            if close:
                cur["rows"].append((i, segs))
            else:
                cur = {"rows": [(i, segs)], "conts": {}}
                groups.append(cur)
            cur["last"] = i
        elif close and len(segs) == 1 and ln.y0 - pg.lines[cur["last"]].y0 <= 1.6 * pitch \
                and ln.x0 > min(s[0][0].x0 for _, s in cur["rows"]) + 10:
            cur["conts"].setdefault(cur["rows"][-1][0], []).append((i, segs[0]))
            cur["last"] = i
        else:
            cur = None
    taken = set()
    for g in groups:
        grp = g["rows"]
        if len(grp) < 2:
            continue
        cols, header_ext, header_at = _columns(grp)
        if len(cols) < 2:
            continue
        table_rows: List[List[TCell]] = []
        clean = total = 0
        for ri, (_idx, segs) in enumerate(grp):
            row: List[TCell] = []
            used = 0
            for seg in segs:
                a, b = seg[0].x0, seg[-1].x1
                if header_ext is not None and ri > header_at:
                    # Body rows of a table with a header: one column each, the
                    # header it overlaps most, else the nearest header.
                    hit = [_nearest_header(a, b, header_ext)]
                else:
                    # A cell belongs to the columns holding a real share of
                    # it; a number grazing the next column does not span it.
                    hit = [k for k, (c0, c1) in enumerate(cols)
                           if min(b, c1) - max(a, c0) > max(1.0, 0.35 * min(b - a, c1 - c0))]
                    if not hit:
                        mid = (a + b) / 2
                        hit = [min(range(len(cols)),
                                   key=lambda k: abs((cols[k][0] + cols[k][1]) / 2 - mid))]
                total += 1
                clean += len(hit) == 1
                first, last = hit[0], hit[-1]
                para = TPara(runs=_merge_runs(seg), space_after=2.0)
                if first < used:
                    # Same column as the previous cell: it is more of that cell.
                    if row and para.runs:
                        para.runs[0].text = " " + para.runs[0].text.lstrip()
                        row[-1].paras[0].runs.extend(para.runs)
                    continue
                while used < first:
                    row.append(TCell(paras=[TPara()]))
                    used += 1
                if _is_amount(" ".join(s.text for s in seg)):
                    para.align = ALIGN_RIGHT
                row.append(TCell(paras=[para], span=last - first + 1))
                used = last + 1
            while used < len(cols):
                row.append(TCell(paras=[TPara()]))
                used += 1
            for _ci, seg in g["conts"].get(_idx, []):
                _append_to_cell(row, cols, seg)
            table_rows.append(row)
        if total == 0 or clean < 0.75 * total:
            continue
        taken.update(i for i, _ in grp)
        taken.update(ci for conts in g["conts"].values() for ci, _ in conts)
        lines = [pg.lines[i] for i in sorted({i for i, _ in grp} | {
            ci for conts in g["conts"].values() for ci, _ in conts})]
        x0 = min(ln.x0 for ln in lines)
        x1 = max(ln.x1 for ln in lines)
        starts = [c0 for c0, _ in cols] + [x1 + 4]
        widths = [max(starts[k + 1] - starts[k], 24.0) for k in range(len(cols))]
        header_rows = 0
        for _i, segs in grp:
            if all(s.bold for seg in segs for s in seg):
                header_rows += 1
            else:
                break
        header_rows = min(header_rows, len(grp) - 1)
        tbl = TTable(rows=table_rows, col_widths=widths, header_rows=header_rows, borders=False)
        pg.tables.append(((x0, lines[0].y0, x1, lines[-1].y1), tbl))
    if taken:
        pg.lines = [ln for i, ln in enumerate(pg.lines) if i not in taken]


def _channel(a: float, b: float, covered: List[Tuple[float, float]]) -> float:
    """Column boundary between header cells ending at ``a`` and starting at ``b``.

    The widest stretch of [a, b] that no body cell covers — the whitespace
    channel between the two columns. Centred headers alone would put the
    boundary inside a wide column.
    """
    if b <= a:
        return (a + b) / 2
    spans = sorted((max(x0, a), min(x1, b)) for x0, x1 in covered if x1 > a and x0 < b)
    best, best_w, pos = (a + b) / 2, -1.0, a
    for x0, x1 in spans:
        if x0 - pos > best_w:
            best_w, best = x0 - pos, (pos + x0) / 2
        pos = max(pos, x1)
    if b - pos > best_w:
        best = (pos + b) / 2
    return best


def _append_to_cell(row: List[TCell], cols: List[Tuple[float, float]], seg: List[Span]) -> None:
    """Add a wrapped line to the cell of ``row`` it sits under."""
    mid = (seg[0].x0 + seg[-1].x1) / 2
    target, pos = 0, 0
    for k, cell in enumerate(row):
        c0 = cols[min(pos, len(cols) - 1)][0]
        c1 = cols[min(pos + cell.span - 1, len(cols) - 1)][1]
        if c0 - 6 <= mid <= c1 + 6:
            target = k
            break
        pos += cell.span
    runs = _merge_runs(seg)
    if runs:
        runs[0].br_before = bool(row[target].paras[0].runs)
        row[target].paras[0].runs.extend(runs)


def _nearest_header(a: float, b: float, header: List[Tuple[float, float]]) -> int:
    """Column of a body cell [a, b]: the header it overlaps most, else the nearest."""
    overlaps = [min(b, h1) - max(a, h0) for h0, h1 in header]
    best = max(range(len(header)), key=lambda k: overlaps[k])
    if overlaps[best] > 0:
        return best
    return min(range(len(header)),
               key=lambda k: max(header[k][0] - b, a - header[k][1], 0.0))


def _columns(grp: List[Tuple[int, List[List[Span]]]]
             ) -> Tuple[List[Tuple[float, float]], Optional[List[Tuple[float, float]]], int]:
    """Column x-ranges, plus the header cells' extents and row index when a
    header row with one cell per column exists.

    With such a header, body cells are placed by the header they sit under: a
    column can hold left-aligned "-" and right-aligned "5,000" that never
    overlap, so whitespace alone cannot separate it. Without one, columns are
    the union of cell ranges over the body rows with the most cells (header
    rows left out: a centred header need not overlap its body cells).
    """
    most_all = max(len(segs) for _, segs in grp)
    x_lo = min(seg[0].x0 for _, segs in grp for seg in segs)
    x_hi = max(seg[-1].x1 for _, segs in grp for seg in segs)
    for hi, (_i, segs) in enumerate(grp):
        if not all(s.bold for seg in segs for s in seg):
            break
        if len(segs) == most_all:
            body_ranges = [(sg[0].x0, sg[-1].x1) for _, ss in grp for sg in ss
                           if not all(s.bold for s in sg)]
            edges = [x_lo] + [_channel(segs[k - 1][-1].x1, segs[k][0].x0, body_ranges)
                              for k in range(1, len(segs))] + [x_hi]
            cols = [(edges[k], edges[k + 1]) for k in range(len(segs))]
            return cols, [(sg[0].x0, sg[-1].x1) for sg in segs], hi
    body = [(i, segs) for i, segs in grp
            if not all(s.bold for seg in segs for s in seg)] or grp
    most = max(len(segs) for _, segs in body)
    ranges = sorted((seg[0].x0, seg[-1].x1) for _, segs in body if len(segs) == most for seg in segs)
    cols: List[Tuple[float, float]] = []
    for a, b in ranges:
        if cols and a <= cols[-1][1] + 1:
            cols[-1] = (cols[-1][0], max(cols[-1][1], b))
        else:
            cols.append((a, b))
    return cols, None, -1


def _cluster(values: List[float], tol: float) -> List[float]:
    out: List[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


def _is_amount(text: str) -> bool:
    t = text.replace(",", "").replace("%", "").replace("INR", "").strip()
    return bool(t) and t.replace(".", "", 1).isdigit()


# ---------------------------------------------------------------------------
# Paragraphs
# ---------------------------------------------------------------------------

def _merge_runs(spans: List[Span], font: Optional[str] = None, size: Optional[float] = None
                ) -> List[TRun]:
    runs: List[TRun] = []
    prev: Optional[Span] = None
    for s in spans:
        text = s.text
        if not text.strip():
            continue
        if prev is not None and s.x0 - prev.x1 > 0.15 * max(s.size, 1.0) and runs \
                and not runs[-1].text.endswith(" ") and not text.startswith(" "):
            text = " " + text
        style = (s.bold, s.italic, s.underline,
                 None if size and abs(s.size - size) < 0.75 else snap(s.size, _COMMON_SIZES),
                 None if (not s.font or s.font == font) else s.font)
        if runs and (runs[-1].bold, runs[-1].italic, runs[-1].underline,
                     runs[-1].size, runs[-1].font) == style:
            runs[-1].text += text
        else:
            runs.append(TRun(text, bold=s.bold, italic=s.italic, underline=s.underline,
                             size=style[3], font=style[4]))
        prev = s
    return runs


@dataclass
class _Metrics:
    left: float
    right: float
    size: float
    font: Optional[str]
    pitch: Dict[float, float]  # body size -> typical baseline pitch inside a paragraph
    # True when most long lines end flush on the right margin. In ragged text
    # only the single longest line does (it *defines* the measured margin).
    justified: bool = False


def _metrics(pages: List[PageLines]) -> _Metrics:
    lines = [ln for pg in pages for ln in pg.lines if ln.text.strip()]
    sizes: Counter = Counter()
    fonts: Counter = Counter()
    for ln in lines:
        for s in ln.spans:
            n = len(s.text.strip())
            sizes[round(s.size * 2) / 2] += n
            if s.font:
                fonts[s.font] += n
    size = sizes.most_common(1)[0][0] if sizes else 12.0
    x0s = sorted(ln.x0 for ln in lines)
    long_x1 = sorted(ln.x1 for ln in lines if ln.x1 - ln.x0 > 200) or sorted(ln.x1 for ln in lines)
    left = x0s[int(0.05 * (len(x0s) - 1))] if x0s else 72.0
    # The furthest a long line reaches is the right margin; a percentile
    # undershoots on ragged-right (left-aligned) text. Tables and running
    # header/footer lines are already out of ``lines``.
    right = long_x1[-1] if long_x1 else 520.0
    if not lines:
        for pg in pages:
            for (tx0, _ty0, tx1, _ty1), _t in pg.tables:
                left, right = tx0, max(right, tx1)
    # Line pitch *inside* a paragraph: measured only after lines that wrap
    # (reach the right margin), which certainly continue the same paragraph.
    # Measuring every consecutive pair picks up the gap *between* one-line
    # paragraphs in documents made mostly of them.
    wrap_at = right - 0.12 * (right - left)
    pitches: Counter = Counter()
    loose: Counter = Counter()
    for pg in pages:
        for a, b in zip(pg.lines, pg.lines[1:]):
            d = b.y0 - a.y0
            if 0 < d < 3 * size and abs(a.size - size) < 0.75 and abs(b.size - size) < 0.75:
                loose[float(round(d))] += 1
                if a.x1 >= wrap_at:
                    pitches[float(round(d))] += 1
    if pitches:
        # The smallest recurring pitch: a paragraph whose last line happens
        # to be full is followed by the paragraph gap, not the line pitch.
        top = max(pitches.values())
        base = min(k for k, c in pitches.items() if c >= max(1, 0.3 * top))
    elif loose:
        base = min(k for k in loose if k >= 0.95 * size) if any(k >= 0.95 * size for k in loose) else 1.2 * size
    else:
        base = 1.2 * size
    long_lines = [ln for ln in lines if ln.x1 - ln.x0 > 0.6 * (right - left)]
    flush = sum(1 for ln in long_lines if ln.x1 >= right - 3.0)
    justified = len(long_lines) >= 3 and flush >= 0.45 * len(long_lines) and flush >= 3
    if not justified and pages:
        # Ragged-right text never reaches the right margin; mirror the left
        # one so centring is judged against the real text area.
        right = max(right, pages[0].width - left)
    return _Metrics(left, right, size, fonts.most_common(1)[0][0] if fonts else None,
                    {size: base}, justified)


def _align(lines: List[Line], m: _Metrics, width_tol: float, *, labelled: bool = False) -> str:
    mid = (m.left + m.right) / 2
    full = [abs(ln.x1 - m.right) <= width_tol for ln in lines]
    if labelled:
        # A numbered clause is never centred, even when its text happens to
        # sit in the middle of the line.
        return ALIGN_JUSTIFY if m.justified and len(lines) >= 2 and all(full[:-1]) else ALIGN_LEFT
    centered = all(abs((ln.x0 + ln.x1) / 2 - mid) <= max(6.0, width_tol) and ln.x0 > m.left + 12
                   for ln in lines)
    if centered:
        return ALIGN_CENTER
    if len(lines) == 1 and lines[0].bold and lines[0].x0 <= m.left + 12 \
            and lines[0].x1 >= m.right - 12:
        # A bold line filling the whole width (a long title) looks the same
        # centred or left-aligned; titles and headings are centred.
        return ALIGN_CENTER
    if all(abs(ln.x1 - m.right) <= width_tol for ln in lines) and lines[0].x0 > m.left + 0.25 * (m.right - m.left):
        return ALIGN_RIGHT
    if m.justified and len(lines) >= 2 and all(full[:-1]):
        return ALIGN_JUSTIFY
    return ALIGN_LEFT


def _label_split(line: Line) -> Tuple[str, Optional[float]]:
    """(label, x where the text after the label starts)."""
    m = LABEL_RE.match(line.text + " ")
    if not m:
        return "", None
    label = re.sub(r"\s+", " ", m.group(1)).strip()
    consumed = ""
    for i, s in enumerate(line.spans):
        consumed = (consumed + " " + s.text.strip()).strip()
        if len(consumed) >= len(label):
            if consumed == label and i + 1 < len(line.spans):
                return label, line.spans[i + 1].x0
            return label, None
    return label, None


def _strip_label(spans: List[Span], label: str) -> List[Span]:
    out = list(spans)
    need = label
    while out and need:
        t = out[0].text.strip()
        if need.startswith(t):
            need = need[len(t):].strip()
            out.pop(0)
        elif t.startswith(need):
            first = out[0]
            out[0] = Span(t[len(need):].lstrip(), first.x0, first.x1, first.size, first.bold,
                          first.italic, first.underline, first.font)
            need = ""
        else:
            break
    return out


def build_paragraphs(pages: List[PageLines], m: _Metrics, *, width_tol: float = 3.0
                     ) -> List[Tuple[int, float, TPara]]:
    """Group lines into paragraphs. Returns (page, y, paragraph) in reading order."""
    out: List[Tuple[int, float, TPara]] = []
    pitch = m.pitch.get(m.size, 1.2 * m.size)
    short_limit = m.left + 0.62 * (m.right - m.left)
    for pi, pg in enumerate(pages):
        cur: List[Line] = []
        gap_before = 0.0

        def flush() -> None:
            if cur:
                out.append((pi, cur[0].y0, _make_para(list(cur), m, pitch, gap_before, width_tol)))

        prev: Optional[Line] = None
        for ln in pg.lines:
            if not ln.text.strip():
                continue
            if prev is None:
                cur, gap_before, prev = [ln], 0.0, ln
                continue
            d = ln.y0 - prev.y0
            same_size = abs(ln.size - prev.size) < 0.75
            label, _ = _label_split(ln)
            prev_short = prev.x1 < short_limit
            cont_x = _continuation_x(cur)
            new = (
                d > pitch * 1.18 + 1.5
                or not same_size
                or bool(label)
                or ln.bold != prev.bold and (ln.bold or prev.bold) and len(prev.text) < 90
                or (cont_x is not None and abs(ln.x0 - cont_x) > 6 and not prev_short)
            )
            stacked = (not new and prev_short and _short(ln, short_limit)
                       and abs(ln.x0 - prev.x0) <= 6 and not label)
            if not new and prev_short and not stacked:
                new = True
            if new:
                flush()
                gap_before = max(0.0, d - pitch)
                cur = [ln]
            else:
                ln.stacked = stacked
                cur.append(ln)
            prev = ln
        flush()
    return out


def _short(ln: Line, limit: float) -> bool:
    return ln.x1 < limit


def _continuation_x(cur: List[Line]) -> Optional[float]:
    if len(cur) >= 2:
        return cur[1].x0
    label, after = _label_split(cur[0])
    if label and after is not None:
        return after
    return None


def _make_para(lines: List[Line], m: _Metrics, pitch: float, gap_before: float,
               width_tol: float) -> TPara:
    first = lines[0]
    label, after = _label_split(first)
    spans: List[Span] = []
    runs: List[TRun] = []
    for i, ln in enumerate(lines):
        lspans = _strip_label(ln.spans, label) if i == 0 and label else ln.spans
        r = _merge_runs(lspans, m.font, m.size)
        if not r:
            continue
        if i > 0:
            if ln.stacked:
                r[0].br_before = True
            elif runs and not runs[-1].text.endswith((" ", "-")):
                r[0].text = " " + r[0].text.lstrip()
        if runs and not r[0].br_before and (runs[-1].bold, runs[-1].italic, runs[-1].underline,
                                            runs[-1].size, runs[-1].font) == (
                r[0].bold, r[0].italic, r[0].underline, r[0].size, r[0].font):
            runs[-1].text += r[0].text
            r = r[1:]
        runs.extend(r)
        spans.extend(lspans)
    for r in runs:
        r.text = re.sub(r"\s+", " ", r.text)
    if runs:
        runs[0].text = runs[0].text.lstrip()
    align = _align(lines, m, width_tol, labelled=bool(label))
    if label:
        text_x = after if after is not None else (lines[1].x0 if len(lines) > 1 else first.x0 + 24)
        left = text_x - m.left
        first_indent = first.x0 - text_x
    else:
        cont = lines[1].x0 if len(lines) > 1 else first.x0
        left = (cont if align in (ALIGN_LEFT, ALIGN_JUSTIFY) else m.left) - m.left
        first_indent = (first.x0 - cont) if (len(lines) > 1 and align in (ALIGN_LEFT, ALIGN_JUSTIFY)) else 0.0
    if align in (ALIGN_CENTER, ALIGN_RIGHT):
        left, first_indent = 0.0, 0.0
    if len(lines) >= 2:
        pitches = [b.y0 - a.y0 for a, b in zip(lines, lines[1:])]
        ls = snap(statistics.median(pitches) / (1.2 * max(first.size, 1.0)), _SPACINGS)
    else:
        ls = snap(pitch / (1.2 * max(m.size, 1.0)), _SPACINGS)
    is_bold = first.bold and all(ln.bold for ln in lines)
    text = " ".join(ln.text for ln in lines)
    role, level = ROLE_BODY, 0
    if label:
        role = ROLE_CLAUSE
    elif is_bold and len(text) < 100 and not any(ln.stacked for ln in lines[1:]):
        role, level = ROLE_HEADING, 2
    if "____" in text or any(ln.stacked for ln in lines[1:]):
        if not label and len(lines) > 1 or "____" in text:
            role = ROLE_SIGNATURE if role != ROLE_HEADING else role
    return TPara(runs=runs, align=align, role=role, level=level, label=label,
                 left_indent=max(0.0, _snap_pt(left)), first_indent=_snap_pt(first_indent),
                 space_before=_snap_pt(gap_before, 2.0), space_after=0.0,
                 line_spacing=ls, keep_together=role == ROLE_SIGNATURE)


def _assign_levels(paras: List[TPara]) -> None:
    """Clause depth from the distinct label indents, title from size and position."""
    starts = sorted({round(p.left_indent + p.first_indent) for p in paras if p.label})
    levels = _cluster([float(s) for s in starts], 8.0)
    for p in paras:
        if p.label:
            x = p.left_indent + p.first_indent
            p.level = min(range(len(levels)), key=lambda k: abs(levels[k] - x)) if levels else 0


def assemble(pages: List[PageLines], *, source: str, width_tol: float = 3.0,
             font_hint: Optional[str] = None) -> TemplateIR:
    """Full pipeline: running text, tables, paragraphs → ``TemplateIR``."""
    if not pages:
        return TemplateIR(source=source)
    header_lines, footer_lines, page_fmt = split_running(pages)
    m = _metrics(pages)
    for pg in pages:
        find_gap_tables(pg, m.size, m.pitch.get(m.size, 1.2 * m.size))
    paras = build_paragraphs(pages, m, width_tol=width_tol)
    _assign_levels([p for _, _, p in paras])

    # Title: the largest bold text near the top of page 1, else the first centred bold line.
    page1 = [p for pi, _, p in paras if pi == 0]
    if page1:
        def psize(p: TPara) -> float:
            return max((r.size or m.size) for r in p.runs) if p.runs else m.size
        cands = [p for p in page1[:6] if p.runs and all(r.bold for r in p.runs if r.text.strip())]
        if cands:
            t = max(cands, key=psize)
            if psize(t) > m.size + 0.75 or t.align == ALIGN_CENTER:
                t.role, t.level = ROLE_TITLE, 1

    # Interleave tables with paragraphs by page and y; mark forced page breaks.
    items: List[Tuple[int, float, object]] = [(pi, y, p) for pi, y, p in paras]
    for pi, pg in enumerate(pages):
        for (x0, y0, x1, y1), t in pg.tables:
            items.append((pi, y0, t))
    items.sort(key=lambda it: (it[0], it[1]))
    blocks: List[object] = []
    last_bottom: Dict[int, float] = {}
    for pi, pg in enumerate(pages):
        ys = [ln.y1 for ln in pg.lines] + [tb[3] for tb, _ in pg.tables]
        last_bottom[pi] = max(ys) if ys else 0.0
    prev_page = 0
    for pi, _y, obj in items:
        if isinstance(obj, TPara) and pi > prev_page:
            prev_h = pages[pi - 1].height
            if last_bottom.get(pi - 1, prev_h) < prev_h * 0.72 and obj.role in (ROLE_HEADING, ROLE_TITLE):
                obj.page_break_before = True
            obj.space_before = 0.0 if not obj.page_break_before else obj.space_before
        prev_page = max(prev_page, pi)
        blocks.append(obj)

    # Spacing: express the gap above each paragraph as space_before; body
    # paragraphs separated by a uniform gap get it as space_after instead.
    body_gaps = [p.space_before for p in blocks if isinstance(p, TPara) and p.role == ROLE_BODY]
    common = Counter(body_gaps).most_common(1)[0][0] if body_gaps else 0.0
    for p in blocks:
        if isinstance(p, TPara):
            p.space_after = common
            p.space_before = max(0.0, p.space_before - common)

    # Majority body alignment for single-line paragraphs whose alignment is ambiguous.
    multi = Counter(p.align for p in blocks if isinstance(p, TPara) and p.role in (ROLE_BODY, ROLE_CLAUSE))
    body_align = ALIGN_JUSTIFY if multi.get(ALIGN_JUSTIFY, 0) >= max(1, multi.get(ALIGN_LEFT, 0) // 2) else ALIGN_LEFT
    for p in blocks:
        if isinstance(p, TPara) and p.role in (ROLE_BODY, ROLE_CLAUSE) and p.align == ALIGN_LEFT \
                and body_align == ALIGN_JUSTIFY and len(p.text) < 120:
            p.align = ALIGN_JUSTIFY if len(p.text) > 60 else ALIGN_LEFT
    ls_vals = [p.line_spacing for p in blocks if isinstance(p, TPara) and len(p.text) > 120]
    if ls_vals:
        doc_ls = Counter(ls_vals).most_common(1)[0][0]
        for p in blocks:
            if isinstance(p, TPara) and len(p.text) <= 120:
                p.line_spacing = doc_ls

    pg0 = pages[0]
    # Ragged-right text never touches the right margin, so its line ends
    # cannot measure it; with nothing justified, mirror the left margin
    # (unless a table shows the text area reaching further).
    if not any(isinstance(p, TPara) and p.align == ALIGN_JUSTIFY for p in blocks):
        table_right = max((tb[2] for pg in pages for tb, _ in pg.tables), default=0.0)
        m.right = max(m.right, pg0.width - m.left, min(table_right, pg0.width - 18))
    top_body = min((ln.y0 for ln in pg0.lines), default=72.0)
    bottoms = [pg.height - max((ln.y1 for ln in pg.lines), default=pg.height - 72.0) for pg in pages]
    page = PageSetup(
        width=pg0.width, height=pg0.height,
        margin_left=_snap_pt(m.left), margin_right=_snap_pt(pg0.width - m.right),
        margin_top=_snap_pt(max(36.0, top_body - 2)),
        margin_bottom=_snap_pt(max(36.0, min(bottoms) - 2)),
    )
    ir = TemplateIR(blocks=blocks, page=page, font=font_hint or m.font or "Times New Roman",
                    size=snap(m.size, _COMMON_SIZES), source=source,
                    page_numbers=page_fmt is not None,
                    page_number_format=page_fmt or "Page {PAGE} of {NUMPAGES}")
    ir.header = _running_blocks(header_lines, pg0, m, width_tol, top=True)
    ir.footer = _running_blocks(footer_lines, pg0, m, width_tol, top=False)
    return ir


def _running_blocks(lines: List[Line], pg: PageLines, m: _Metrics, width_tol: float,
                    *, top: bool) -> List[object]:
    blocks: List[object] = []
    if top:
        for (x0, y0, x1, y1), data in pg.images:
            if y1 < pg.height * 0.2:
                align = ALIGN_RIGHT if x0 > pg.width * 0.6 else (
                    ALIGN_CENTER if x0 > pg.width * 0.35 else ALIGN_LEFT)
                blocks.append(TImage(data=data, width=max(12.0, x1 - x0), align=align))
    if not lines:
        return blocks
    group: List[Line] = []

    def flush() -> None:
        if not group:
            return
        runs: List[TRun] = []
        for i, ln in enumerate(group):
            r = _merge_runs(ln.spans, m.font, m.size)
            if r and i:
                r[0].br_before = True
            runs.extend(r)
        right = all(abs(ln.x1 - m.right) <= 8 for ln in group)
        center = all(abs((ln.x0 + ln.x1) / 2 - (m.left + m.right) / 2) <= 8 for ln in group)
        blocks.append(TPara(runs=runs, align=ALIGN_RIGHT if right else (ALIGN_CENTER if center else ALIGN_LEFT),
                            space_after=0.0))

    for ln in lines:
        if group and (abs(ln.size - group[-1].size) > 0.75 or ln.bold != group[-1].bold):
            flush()
            group = []
        group.append(ln)
    flush()
    return blocks
