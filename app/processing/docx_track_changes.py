"""Write a draft revision into the lawyer's own .docx as tracked changes (SIM-220).

SIM-219 revises the lawyer's latest version as Markdown and builds a clean new
Word file. This module instead returns the lawyer's own file with the AI's
edits as Word tracked changes (author "SimplerToday AI"), so the lawyer
accepts or rejects them in the editor like a colleague's. Everything the AI
did not change keeps its XML: formatting, letterhead, numbering, comments,
headers, footers and section properties. Only ``word/document.xml`` is
rewritten; every other part of the package is copied byte for byte.

Deterministic, no model call. Three texts are compared:

- A: the base file's body blocks (paragraphs and tables, reading order, walked
  the way ``docx_markdown`` walks them);
- B: the base Markdown the model was given, post-processed like the delivered
  draft (the caller's job), parsed with the renderer's own parser;
- R: the revised Markdown, parsed the same way.

The edit is B -> R (what the AI changed); A <-> B only locates each B block,
row and word in the file. Text that differs between A and B (the
normaliser's own repairs, a totals marker) is therefore never shown as an AI
change: it is equal in B and R. Formatting-only differences are not tracked.

Anything the mapping cannot do safely returns no file and a fixed reason code
(the caller then builds the SIM-219 clean file): a revision that changes more
than ``max_changed_ratio`` of the base's blocks (``too_many_changes``: an
intent change, a full redraft, a translation), a table whose column count
changed, a changed paragraph holding a field, drawing or content control, a
base the walker cannot line up with B, or any exception.

Pure: bytes and text in, bytes and counts out. Nothing here logs content.
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
DOCUMENT_PART = "word/document.xml"

DEFAULT_AUTHOR = "SimplerToday AI"
DEFAULT_MAX_CHANGED_RATIO = 0.4
# Below this share of B blocks found in the file, the walk and the Markdown
# do not describe the same document.
_MIN_BASE_MATCH = 0.6
# Paired blocks / rows must share at least this much of their words.
_PAIR_SIMILARITY = 0.5
# Cap on the pairing work inside one replaced region (n * m similarity checks).
_PAIR_CELLS_CAP = 40_000

# Reason codes (fixed strings; safe to log).
REASON_OK = "ok"
REASON_NO_CHANGES = "no_changes"
REASON_TOO_MANY = "too_many_changes"
REASON_COLUMNS = "table_columns_changed"
REASON_UNSUPPORTED = "unsupported_structure"
REASON_MISMATCH = "base_mismatch"
REASON_EMPTY = "empty"
REASON_NOT_DOCX = "not_a_docx"
REASON_ERROR = "error"


def W(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


_P, _R, _T, _TBL, _TR, _TC = W("p"), W("r"), W("t"), W("tbl"), W("tr"), W("tc")
_PPR, _RPR, _TRPR, _TCPR = W("pPr"), W("rPr"), W("trPr"), W("tcPr")
_INS, _DEL = W("ins"), W("del")
_CHANGE_MARKS = {W(x) for x in ("ins", "del", "moveFrom", "moveTo", "rPrChange")}
# A changed paragraph holding one of these is not edited word by word.
_COMPLEX = {
    W(x)
    for x in (
        "fldChar", "instrText", "fldSimple", "drawing", "pict", "object",
        "txbxContent", "sdt", "ruby", "ins", "del", "moveFrom", "moveTo",
    )
} | {"{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"}
# A figure ("1,00,000", "18.5", "12/03/2026") is one word: a changed amount
# is shown replaced whole, not digit group by digit group.
_TOKEN_RE = re.compile(r"\n|[^\S\n]+|\d+(?:[.,/:-]\d+)*|\w+|[^\w\s]", re.UNICODE)
_NUMBER_STYLE_RE = re.compile(r"^list number(?:\s*\d)?$", re.IGNORECASE)
_LEAD_MARKER_RE = re.compile(r"^\s*(?:\d{1,9}[.)]|•)\s*")
_WS_RE = re.compile(r"\s+")
_ID_RE = re.compile(rb'\bw:id="(-?\d+)"')

# CT_RPr child order (enough of it to place w:b / w:i correctly).
_RPR_ORDER = [W(x) for x in ("rStyle", "rFonts", "b", "bCs", "i", "iCs")]


class _Unsupported(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class TrackedResult:
    """Outcome of one attempt. ``docx`` is set only when applied."""

    docx: Optional[bytes]
    reason: str
    inserted: int = 0
    deleted: int = 0
    changed_blocks: int = 0
    unmatched: int = 0
    changed_ratio: float = 0.0

    @property
    def applied(self) -> bool:
        return self.docx is not None

    @property
    def has_changes(self) -> bool:
        return self.applied and (self.inserted + self.deleted + self.changed_blocks) > 0


# ---------------------------------------------------------------- tokens


@dataclass
class _Tok:
    text: str
    key: str
    bold: bool = False
    italic: bool = False
    runs: Optional[list] = None  # A side: the w:r elements holding it
    para: int = 0  # A side: index of its paragraph in the sequence
    boundary: bool = False  # A side: a paragraph break inside a cell


def _key(text: str) -> str:
    if text == "\n":
        return "\n"
    if text.isspace():
        return " "
    return text


def _tokenize(text: str) -> List[Tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]


def _md_tokens(runs: Sequence[Tuple[str, bool, bool]]) -> List[_Tok]:
    """Tokens of a Markdown block's runs ``(text, bold, italic)``; ``"\\n"``
    is a line break."""
    text = "".join(t for t, _, _ in runs)
    fmt: List[Tuple[bool, bool]] = []
    for t, b, i in runs:
        fmt.extend([(b, i)] * len(t))
    out = []
    for s, _e, tok in _tokenize(text):
        b, i = fmt[s] if s < len(fmt) else (False, False)
        out.append(_Tok(tok, _key(tok), bold=b, italic=i))
    return out


def _diff_view(toks: List[_Tok]) -> List[int]:
    """Indexes of the tokens that take part in a diff: whitespace at either
    end of a line, and line breaks at either end, are layout, not text."""
    idx = [k for k, t in enumerate(toks) if not t.boundary]
    keep: List[int] = []
    n = len(idx)
    for pos, k in enumerate(idx):
        t = toks[k]
        if t.key == " ":
            prev = toks[idx[pos - 1]].key if pos > 0 else None
            nxt = toks[idx[pos + 1]].key if pos + 1 < n else None
            if prev in (None, "\n") or nxt in (None, "\n"):
                continue
        keep.append(k)
    while keep and toks[keep[0]].key == "\n":
        keep.pop(0)
    while keep and toks[keep[-1]].key == "\n":
        keep.pop()
    # Boundaries are kept so that a cell's paragraphs still line up.
    with_bounds = sorted(set(keep) | {k for k, t in enumerate(toks) if t.boundary})
    return with_bounds


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _strip_marker(text: str) -> str:
    return _LEAD_MARKER_RE.sub("", text, count=1)


# ---------------------------------------------------------------- Markdown side


@dataclass
class _MdBlock:
    kind: str  # heading | para | bullet | number | table
    runs: List[Tuple[str, bool, bool]]
    level: int = 0
    enumerator: str = ""
    rows: Optional[List[List[List[Tuple[str, bool, bool]]]]] = None
    ncols: int = 0

    @property
    def is_table(self) -> bool:
        return self.kind == "table"

    def text(self) -> str:
        return "".join(t for t, _, _ in self.runs)


def _plain_runs(runs) -> List[Tuple[str, bool, bool]]:
    out = []
    for r in runs or []:
        if getattr(r, "br", False):
            out.append(("\n", False, False))
        elif r.text:
            out.append((r.text, bool(r.bold), bool(r.italic)))
    return out


def _md_blocks(markdown: str) -> List[_MdBlock]:
    """The Markdown as the renderer reads it, one block per Word paragraph
    (list items each their own) or table."""
    from app.processing import docx_generator as g

    out: List[_MdBlock] = []

    def add_list(block, depth: int) -> None:
        ordered = isinstance(block, g.OrderedList)
        for k, item in enumerate(block.items):
            runs = _plain_runs(item.runs)
            if _norm("".join(t for t, _, _ in runs)):
                if ordered:
                    out.append(_MdBlock("number", runs, level=depth,
                                        enumerator=f"{block.start + k}{block.delimiter}"))
                else:
                    out.append(_MdBlock("bullet", runs, level=depth))
            for child in item.children:
                add_list(child, depth + 1)

    for block in g.parse_markdown_blocks(markdown or ""):
        if isinstance(block, g.Heading):
            runs = _plain_runs(block.runs)
            if _norm("".join(t for t, _, _ in runs)):
                out.append(_MdBlock("heading", runs, level=block.level))
        elif isinstance(block, g.Paragraph):
            runs = _plain_runs(block.runs)
            if _norm("".join(t for t, _, _ in runs)):
                out.append(_MdBlock("para", runs))
        elif isinstance(block, (g.BulletList, g.OrderedList)):
            add_list(block, 0)
        elif isinstance(block, g.Table):
            rows = [[_plain_runs(c) for c in row] for row in block.rows]
            rows = [r for r in rows if any(_norm("".join(t for t, _, _ in c)) for c in r)]
            if rows:
                ncols = max(len(r) for r in rows)
                rows = [r + [[]] * (ncols - len(r)) for r in rows]
                out.append(_MdBlock("table", [], rows=rows, ncols=ncols))
    return out


def _cell_key(cell_runs) -> str:
    return _norm("".join(t for t, _, _ in cell_runs))


def _md_key(b: _MdBlock) -> str:
    if b.is_table:
        return "T:" + "||".join("|".join(_cell_key(c) for c in row) for row in b.rows)
    return "P:" + _norm(b.text())


# ---------------------------------------------------------------- Word side


def _owned_runs(p) -> list:
    """The ``w:r`` elements of paragraph ``p`` itself: not a text box's
    paragraphs, not a drawing's fallback copy."""
    skip = {id(x) for fb in p.iter(_MC_FALLBACK) for x in fb.iter()}
    out = []
    for r in p.iter(_R):
        if id(r) in skip:
            continue
        anc = r.getparent()
        while anc is not None and anc.tag != _P:
            anc = anc.getparent()
        if anc is p:
            out.append(r)
    return out


def _child_text(child) -> Optional[str]:
    tag = child.tag
    if tag == _T:
        return child.text or ""
    if tag == W("tab"):
        return "\t"
    if tag == W("cr") or (tag == W("br") and child.get(W("type")) not in ("page", "column")):
        return "\n"
    if tag == W("noBreakHyphen"):
        return "-"
    return None


def _para_text(p) -> str:
    parts = []
    for r in _owned_runs(p):
        for c in r:
            t = _child_text(c)
            if t:
                parts.append(t)
    return "".join(parts)


@dataclass
class _ARow:
    tr: object
    cells: List[Optional[object]]  # tc per grid column, None for a merged copy
    keys: List[str]

    @property
    def key(self) -> str:
        return "|".join(self.keys)


@dataclass
class _ABlock:
    el: object
    is_table: bool
    key: str
    kind: str = "para"  # heading | bullet | number | para
    level: int = 0
    word_prefix: bool = False  # Word numbering / bullet style draws the marker
    literal_bullet: bool = False
    rows: Optional[List[_ARow]] = None
    ncols: int = 0


def _cell_paragraphs(tc) -> list:
    return [p for p in tc.iter(_P)]


def _cell_plain(tc) -> str:
    lines = [_norm(_para_text(p)) for p in _cell_paragraphs(tc)]
    return _norm(" ".join(x for x in lines if x))


def _a_blocks(docx_bytes: bytes, plain_root) -> List[_ABlock]:
    """Body blocks of the file in reading order, with what the Markdown
    conversion saw for each (kind, list marker). Analysis uses python-docx
    (styles, numbering); edits go to ``plain_root``, matched by position."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    from app.processing import docx_markdown as dm
    from app.processing.file_handlers import FileProcessor

    doc = Document(io.BytesIO(docx_bytes))
    a_root = doc.element
    a_els = list(a_root.iter(etree.Element))
    p_els = list(plain_root.iter(etree.Element))
    if len(a_els) != len(p_els) or any(x.tag != y.tag for x, y in zip(a_els, p_els)):
        raise _Unsupported(REASON_UNSUPPORTED)
    to_plain = {id(x): y for x, y in zip(a_els, p_els)}

    numfmt_map = FileProcessor._docx_numbering_formats(doc)
    counters: dict = {}
    out: List[_ABlock] = []
    for el in dm._blocks(doc.element.body):
        if el.tag == _TBL:
            rows: List[_ARow] = []
            for row in Table(el, doc).rows:
                cells: List[Optional[object]] = []
                keys: List[str] = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        cells.append(None)
                        keys.append("")
                        continue
                    seen.add(id(cell._tc))
                    tc = to_plain[id(cell._tc)]
                    cells.append(tc)
                    keys.append(_cell_plain(tc))
                if any(keys):
                    rows.append(_ARow(to_plain[id(row._tr)], cells, keys))
            if not rows:
                continue
            ncols = max(len(r.cells) for r in rows)
            for r in rows:
                r.cells += [None] * (ncols - len(r.cells))
                r.keys += [""] * (ncols - len(r.keys))
            key = "T:" + "||".join(r.key for r in rows)
            out.append(_ABlock(to_plain[id(el)], True, key, kind="table", rows=rows, ncols=ncols))
            continue

        paragraph = Paragraph(el, doc)
        level = dm._heading_level(paragraph)
        plain_el = to_plain[id(el)]
        text = _norm(_para_text(plain_el))
        if level is not None:
            if text:
                out.append(_ABlock(plain_el, False, "P:" + text, kind="heading", level=level))
            continue
        if not any(dm._inline(dm._runs(el))):
            continue
        style = dm._style_name(paragraph)
        bullet = dm._BULLET_STYLE_RE.match(style)
        marker = FileProcessor._docx_list_marker(paragraph, numfmt_map, counters)
        literal_bullet = text.startswith("•")
        if bullet or marker.strip() == "•":
            kind, prefix = "bullet", True
        elif marker or _NUMBER_STYLE_RE.match(style):
            # Numbered by Word (directly, or through a "List Number" style):
            # the number is drawn, never text.
            kind, prefix = "number", True
        elif literal_bullet:
            kind, prefix = "bullet", False
        elif re.match(r"^\d{1,9}[.)]\s", text):
            kind, prefix = "number", False
        else:
            kind, prefix = "para", False
        out.append(
            _ABlock(
                plain_el, False, "P:" + _norm(_strip_marker(text)), kind=kind,
                word_prefix=prefix, literal_bullet=literal_bullet,
            )
        )
    return out


# ---------------------------------------------------------------- pairing


# Words for similarity: figures whole, punctuation ignored.
_WORD_RE = re.compile(r"\d+(?:[.,/:-]\d+)*|\w+", re.UNICODE)


def _words(key: str) -> List[str]:
    return _WORD_RE.findall(key)


def _pair(
    xkeys: List[str], ykeys: List[str], xtab: List[bool], ytab: List[bool]
) -> List[Tuple[Optional[int], Optional[int], bool]]:
    """Order-preserving pairing of two block (or row) sequences.

    Equal keys pair first (difflib); inside a replaced region, blocks of the
    same type pair when their words are at least half the same (a small DP,
    so the pairing stays in order). Returns ``(x, y, equal)`` triples; an
    unpaired side is None.
    """
    out: List[Tuple[Optional[int], Optional[int], bool]] = []
    sm = SequenceMatcher(None, xkeys, ykeys, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend((i1 + k, j1 + k, True) for k in range(i2 - i1))
            continue
        xs, ys = list(range(i1, i2)), list(range(j1, j2))
        if not xs or not ys or len(xs) * len(ys) > _PAIR_CELLS_CAP:
            out.extend((i, None, False) for i in xs)
            out.extend((None, j, False) for j in ys)
            continue
        n, m = len(xs), len(ys)
        sim = [[0.0] * m for _ in range(n)]
        for a in range(n):
            wa = _words(xkeys[xs[a]])
            for b in range(m):
                if xtab[xs[a]] != ytab[ys[b]]:
                    continue
                s = SequenceMatcher(None, wa, _words(ykeys[ys[b]]), autojunk=False)
                if s.real_quick_ratio() < _PAIR_SIMILARITY or s.quick_ratio() < _PAIR_SIMILARITY:
                    continue
                r = s.ratio()
                if r >= _PAIR_SIMILARITY:
                    sim[a][b] = r
        score = [[0.0] * (m + 1) for _ in range(n + 1)]
        for a in range(1, n + 1):
            for b in range(1, m + 1):
                best = max(score[a - 1][b], score[a][b - 1])
                if sim[a - 1][b - 1]:
                    best = max(best, score[a - 1][b - 1] + sim[a - 1][b - 1])
                score[a][b] = best
        pairs: List[Tuple[int, int]] = []
        a, b = n, m
        while a > 0 and b > 0:
            if sim[a - 1][b - 1] and score[a][b] == score[a - 1][b - 1] + sim[a - 1][b - 1]:
                pairs.append((a - 1, b - 1))
                a, b = a - 1, b - 1
            elif score[a][b] == score[a - 1][b]:
                a -= 1
            else:
                b -= 1
        pairs.reverse()
        pa = pb = 0
        for a, b in pairs:
            out.extend((xs[k], None, False) for k in range(pa, a))
            out.extend((None, ys[k], False) for k in range(pb, b))
            out.append((xs[a], ys[b], False))
            pa, pb = a + 1, b + 1
        out.extend((xs[k], None, False) for k in range(pa, n))
        out.extend((None, ys[k], False) for k in range(pb, m))
    return out


# ---------------------------------------------------------------- XML edits


class _Writer:
    """Creates change elements with unique ids, one author, one date."""

    def __init__(self, first_id: int, author: str, date: str) -> None:
        self.next_id = first_id
        self.author = author
        self.date = date

    def mark(self, tag: str):
        el = etree.Element(tag)
        el.set(W("id"), str(self.next_id))
        el.set(W("author"), self.author)
        el.set(W("date"), self.date)
        self.next_id += 1
        return el


def _top(el):
    while el.getparent() is not None and el.getparent().tag in (_INS, _DEL):
        el = el.getparent()
    return el


def _clean_rpr(rpr):
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    for child in list(rpr):
        if child.tag in _CHANGE_MARKS:
            rpr.remove(child)
    return rpr


def _set_toggle(rpr, tag: str) -> None:
    for el in rpr.findall(tag):
        rpr.remove(el)
    new = etree.Element(tag)
    pos = 0
    order = _RPR_ORDER.index(tag)
    for k, child in enumerate(rpr):
        if child.tag in _RPR_ORDER and _RPR_ORDER.index(child.tag) < order:
            pos = k + 1
    rpr.insert(pos, new)


def _new_runs(toks: List[_Tok], base_rpr) -> list:
    """``w:r`` elements for inserted tokens: the neighbour's run properties,
    plus bold / italic where the Markdown asked for them."""
    groups: List[Tuple[Tuple[bool, bool], str]] = []
    for t in toks:
        f = (t.bold, t.italic)
        if groups and groups[-1][0] == f:
            groups[-1] = (f, groups[-1][1] + t.text)
        else:
            groups.append((f, t.text))
    runs = []
    for (bold, italic), text in groups:
        r = etree.Element(_R)
        rpr = _clean_rpr(base_rpr)
        if rpr is None and (bold or italic):
            rpr = etree.Element(_RPR)
        if rpr is not None:
            if bold:
                _set_toggle(rpr, W("b"))
            if italic:
                _set_toggle(rpr, W("i"))
            r.append(rpr)
        for k, piece in enumerate(text.split("\n")):
            if k:
                etree.SubElement(r, W("br"))
            if piece:
                t = etree.SubElement(r, _T)
                t.text = piece
                t.set(_XML_SPACE, "preserve")
        runs.append(r)
    return runs


def _delete_runs(runs: list, w: _Writer) -> None:
    """Wrap runs in ``w:del`` (consecutive siblings share one), text as
    ``w:delText``."""
    groups: List[list] = []
    for r in runs:
        if groups and groups[-1][-1].getnext() is r:
            groups[-1].append(r)
        else:
            groups.append([r])
    for group in groups:
        d = w.mark(_DEL)
        group[0].addprevious(d)
        for r in group:
            d.append(r)
            for c in r:
                if c.tag == _T:
                    c.tag = W("delText")
                elif c.tag == W("instrText"):
                    c.tag = W("delInstrText")


def _ensure_ppr(p):
    ppr = p.find(_PPR)
    if ppr is None:
        ppr = etree.Element(_PPR)
        p.insert(0, ppr)
    return ppr


def _mark_paragraph(p, tag: str, w: _Writer) -> None:
    """Mark the paragraph mark itself inserted or deleted."""
    ppr = _ensure_ppr(p)
    rpr = ppr.find(_RPR)
    if rpr is None:
        rpr = etree.Element(_RPR)
        anchor = ppr.find(W("sectPr"))
        if anchor is None:
            anchor = ppr.find(W("pPrChange"))
        if anchor is not None:
            anchor.addprevious(rpr)
        else:
            ppr.append(rpr)
    rpr.insert(0, w.mark(tag))


def _mark_row(tr, tag: str, w: _Writer) -> None:
    trpr = tr.find(_TRPR)
    if trpr is None:
        trpr = etree.Element(_TRPR)
        prex = tr.find(W("tblPrEx"))
        if prex is not None:
            prex.addnext(trpr)
        else:
            tr.insert(0, trpr)
    for child in list(trpr):
        if child.tag in (_INS, _DEL):
            trpr.remove(child)
    trpr.append(w.mark(tag))


def _delete_paragraph(p, w: _Writer) -> None:
    _delete_runs(_owned_runs(p), w)
    _mark_paragraph(p, _DEL, w)


def _first_text_rpr(p):
    for r in _owned_runs(p):
        if any(_child_text(c) for c in r):
            return r.find(_RPR)
    ppr = p.find(_PPR)
    return ppr.find(_RPR) if ppr is not None else None


def _clean_ppr(ppr, *, keep_numbering: bool):
    if ppr is None:
        return None
    ppr = copy.deepcopy(ppr)
    for tag in (W("sectPr"), W("pPrChange")):
        for el in ppr.findall(tag):
            ppr.remove(el)
    if not keep_numbering:
        for el in ppr.findall(W("numPr")):
            ppr.remove(el)
    rpr = ppr.find(_RPR)
    if rpr is not None:
        for child in list(rpr):
            if child.tag in _CHANGE_MARKS:
                rpr.remove(child)
    return ppr


def _new_paragraph(ppr, toks: List[_Tok], run_rpr, w: _Writer):
    p = etree.Element(_P)
    if ppr is not None:
        p.append(ppr)
    if toks:
        ins = w.mark(_INS)
        for r in _new_runs(toks, run_rpr):
            ins.append(r)
        p.append(ins)
    _mark_paragraph(p, _INS, w)
    return p


# ---------------------------------------------------------------- word-level diff


class _TokenSeq:
    """The words of one paragraph, or of one table cell's paragraphs, with
    the runs that hold each word. Building it splits the runs at word
    boundaries (each piece keeps a copy of its run properties), so a single
    word can be wrapped in ``w:del`` without touching its neighbours."""

    def __init__(self, paragraphs: list) -> None:
        self.paras = paragraphs
        self.toks: List[_Tok] = []
        for pi, p in enumerate(paragraphs):
            if any(el.tag in _COMPLEX for el in p.iter()):
                raise _Unsupported(REASON_UNSUPPORTED)
            if pi and self.toks and not self.toks[-1].boundary:
                self.toks.append(_Tok("\n", "\n", para=pi, boundary=True, runs=[]))
            self.toks.extend(self._explode(p, pi))
        self._after: Dict[int, object] = {}

    @staticmethod
    def _explode(p, pi: int) -> List[_Tok]:
        runs = _owned_runs(p)
        pieces: List[Tuple[object, object, int, str]] = []  # run, child, start, text
        pos = 0
        for r in runs:
            for c in r:
                t = _child_text(c)
                if t:
                    pieces.append((r, c, pos, t))
                    pos += len(t)
        text = "".join(x[3] for x in pieces)
        spans = _tokenize(text)
        toks = [_Tok(s, _key(s), runs=[], para=pi) for _, _, s in spans]
        starts = [s for s, _, _ in spans]

        def tok_at(offset: int) -> int:
            lo, hi = 0, len(starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if starts[mid] <= offset:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        by_run: Dict[int, List[Tuple[object, int, str]]] = {}
        for r, c, s, t in pieces:
            by_run.setdefault(id(r), []).append((c, s, t))
        for r in runs:
            rpr = r.find(_RPR)
            new: list = []
            text_children = {id(c): (s, t) for c, s, t in by_run.get(id(r), [])}
            for c in list(r):
                if c.tag == _RPR:
                    continue
                if id(c) not in text_children:
                    # Not text (a page break, a footnote mark): its own run,
                    # never deleted or split.
                    nr = etree.Element(_R)
                    if rpr is not None:
                        nr.append(copy.deepcopy(rpr))
                    nr.append(c)
                    new.append(nr)
                    continue
                s, t = text_children[id(c)]
                if c.tag == _T:
                    # Split this text at token boundaries.
                    cuts = [s] + [x for x in starts if s < x < s + len(t)] + [s + len(t)]
                    for a, b in zip(cuts, cuts[1:]):
                        nr = etree.Element(_R)
                        if rpr is not None:
                            nr.append(copy.deepcopy(rpr))
                        nt = etree.SubElement(nr, _T)
                        nt.text = t[a - s: b - s]
                        nt.set(_XML_SPACE, "preserve")
                        new.append(nr)
                        toks[tok_at(a)].runs.append(nr)
                else:
                    nr = etree.Element(_R)
                    if rpr is not None:
                        nr.append(copy.deepcopy(rpr))
                    nr.append(c)
                    new.append(nr)
                    toks[tok_at(s)].runs.append(nr)
            if not new:
                continue
            for nr in new:
                r.addprevious(nr)
            r.getparent().remove(r)
        return toks

    # -- placement

    def _neighbour_rpr(self, k: Optional[int]):
        """Run properties for text inserted after token ``k`` (None: at the
        start): the run before, else the run after, else the paragraph mark."""
        if k is not None:
            for j in range(k, -1, -1):
                if self.toks[j].runs:
                    return self.toks[j].runs[-1].find(_RPR)
        for t in self.toks[0 if k is None else k + 1:]:
            if t.runs:
                return t.runs[0].find(_RPR)
        for p in self.paras:
            rpr = _first_text_rpr(p)
            if rpr is not None:
                return rpr
        return None

    def _insert_after(self, k: Optional[int], el) -> None:
        if k is not None and k in self._after:
            self._after[k].addnext(el)
            self._after[k] = el
            return
        placed = False
        if k is not None:
            t = self.toks[k]
            if t.runs:
                _top(t.runs[-1]).addnext(el)
                placed = True
            elif t.boundary:
                self._at_para_start(t.para, el)
                placed = True
        if not placed:
            nxt = next((t for t in self.toks[(k if k is not None else -1) + 1:] if t.runs), None)
            if nxt is not None and (k is None or nxt.para == self.toks[k].para):
                _top(nxt.runs[0]).addprevious(el)
            elif k is None:
                self._at_para_start(0, el)
            else:
                self.paras[self.toks[k].para].append(el)
        if k is not None:
            self._after[k] = el

    def _at_para_start(self, pi: int, el) -> None:
        p = self.paras[pi]
        first = next((t for t in self.toks if t.para == pi and t.runs), None)
        if first is not None:
            _top(first.runs[0]).addprevious(el)
        else:
            ppr = p.find(_PPR)
            if ppr is not None:
                ppr.addnext(el)
            else:
                p.insert(0, el)

    # -- the three-way edit

    def apply(self, b_toks: List[_Tok], r_toks: List[_Tok], w: _Writer) -> int:
        """Apply B -> R at the places B's words have in this sequence.
        Returns the number of edits made."""
        av = _diff_view(self.toks)
        bv = _diff_view(b_toks)
        rv = _diff_view(r_toks)
        akeys = [self.toks[k].key for k in av]
        bkeys = [b_toks[k].key for k in bv]
        rkeys = [r_toks[k].key for k in rv]
        b2a: Dict[int, int] = {}
        for blk in SequenceMatcher(None, akeys, bkeys, autojunk=False).get_matching_blocks():
            for d in range(blk.size):
                b2a[blk.b + d] = av[blk.a + d]
        edits = 0
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, bkeys, rkeys, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            dels = [b2a[i] for i in range(i1, i2) if i in b2a]
            del_runs = [r for a in dels for r in (self.toks[a].runs or [])]
            if del_runs:
                _delete_runs(del_runs, w)
                edits += 1
            ins_toks = [r_toks[rv[j]] for j in range(j1, j2)]
            if not ins_toks:
                continue
            ins = w.mark(_INS)
            if dels:
                anchor: Optional[int] = max(dels)
            else:
                anchor = next((b2a[i] for i in range(i1 - 1, -1, -1) if i in b2a), None)
            if anchor is None:
                nxt = next((b2a[i] for i in range(i2, len(bkeys)) if i in b2a), None)
                for r in _new_runs(ins_toks, self._neighbour_rpr(None)):
                    ins.append(r)
                if nxt is not None and self.toks[nxt].runs:
                    _top(self.toks[nxt].runs[0]).addprevious(ins)
                else:
                    self._insert_after(None, ins)
            else:
                for r in _new_runs(ins_toks, self._neighbour_rpr(anchor)):
                    ins.append(r)
                self._insert_after(anchor, ins)
            edits += 1
        return edits


# ---------------------------------------------------------------- block edits


def _md_variant(a: _ABlock, b: _MdBlock) -> List[Tuple[str, bool, bool]]:
    """The Markdown block's runs as they would read in this Word paragraph:
    with its enumerator when the number is typed text, without it when Word
    draws it."""
    runs = list(b.runs)
    if b.kind == "number" and b.enumerator and not a.word_prefix:
        runs = [(b.enumerator + " ", False, False)] + runs
    elif b.kind == "bullet" and a.literal_bullet:
        runs = [("• ", False, False)] + runs
    return runs


class _Planner:
    def __init__(self, a_blocks: List[_ABlock], w: _Writer, body) -> None:
        self.a = a_blocks
        self.w = w
        self.body = body
        self.tables_used: set = set()

    # paragraphs -------------------------------------------------------

    def source_paragraph(self, before: int, kind: str, level: int) -> Optional[_ABlock]:
        paras = [x for x in self.a[: max(before, 0)] if not x.is_table]
        for x in reversed(paras):
            if x.kind == kind and (kind != "heading" or x.level == level):
                return x
        for x in self.a[max(before, 0):]:
            if not x.is_table and x.kind == kind and (kind != "heading" or x.level == level):
                return x
        if paras:
            return paras[-1]
        return next((x for x in self.a if not x.is_table), None)

    def build_paragraph(self, r: _MdBlock, before: int):
        src = self.source_paragraph(before, r.kind, r.level)
        if src is None:
            ppr, rpr, prefix, lit = None, None, False, False
        else:
            same = src.kind == r.kind
            ppr = _clean_ppr(src.el.find(_PPR), keep_numbering=same)
            rpr = _first_text_rpr(src.el)
            prefix = src.word_prefix and same
            lit = src.literal_bullet and same
        runs = list(r.runs)
        if r.kind == "number" and r.enumerator and not prefix:
            runs = [(r.enumerator + "\t", False, False)] + runs
        elif r.kind == "bullet" and not prefix:
            runs = [("•\t" if not lit else "• ", False, False)] + runs
        return _new_paragraph(ppr, _md_tokens(runs), rpr, self.w)

    # tables -----------------------------------------------------------

    @staticmethod
    def _simple_row(tr) -> bool:
        return not any(
            el.tag in (W("gridSpan"), W("vMerge"), W("hMerge")) for el in tr.iter()
        )

    def build_row(self, template_tr, cells: List[list]):
        tr = copy.deepcopy(template_tr)
        tcs = [c for c in tr if c.tag == _TC]
        if len(tcs) != len(cells):
            raise _Unsupported(REASON_COLUMNS)
        for tc, cell_runs in zip(tcs, cells):
            first_p = tc.find(_P)
            ppr = _clean_ppr(first_p.find(_PPR), keep_numbering=True) if first_p is not None else None
            rpr = _first_text_rpr(first_p) if first_p is not None else None
            rpr = copy.deepcopy(rpr) if rpr is not None else None
            for child in list(tc):
                if child.tag != _TCPR:
                    tc.remove(child)
            tc.append(_new_paragraph(ppr, _md_tokens(cell_runs), rpr, self.w))
        _mark_row(tr, _INS, self.w)
        return tr

    def build_table(self, r: _MdBlock):
        template = next(
            (
                x for x in self.a
                if x.is_table and x.ncols == r.ncols and all(self._simple_row(row.tr) for row in x.rows)
            ),
            None,
        )
        if template is not None:
            tbl = copy.deepcopy(template.el)
            for tr in tbl.findall(_TR):
                tbl.remove(tr)
            head_tr = template.rows[0].tr
            body_tr = template.rows[1].tr if len(template.rows) > 1 else head_tr
            for k, row in enumerate(r.rows):
                tbl.append(self.build_row(head_tr if k == 0 else body_tr, row))
            return tbl
        tbl = etree.Element(_TBL)
        tblpr = etree.SubElement(tbl, W("tblPr"))
        tw = etree.SubElement(tblpr, W("tblW"))
        tw.set(W("w"), "0")
        tw.set(W("type"), "auto")
        borders = etree.SubElement(tblpr, W("tblBorders"))
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            b = etree.SubElement(borders, W(side))
            b.set(W("val"), "single")
            b.set(W("sz"), "4")
            b.set(W("space"), "0")
            b.set(W("color"), "auto")
        grid = etree.SubElement(tbl, W("tblGrid"))
        for _ in range(r.ncols):
            etree.SubElement(grid, W("gridCol")).set(W("w"), str(9000 // max(r.ncols, 1)))
        for k, row in enumerate(r.rows):
            tr = etree.SubElement(tbl, _TR)
            for cell_runs in row:
                tc = etree.SubElement(tr, _TC)
                runs = [(t, True if k == 0 else bb, i) for t, bb, i in cell_runs]
                tc.append(_new_paragraph(None, _md_tokens(runs), None, self.w))
            _mark_row(tr, _INS, self.w)
        return tbl

    def delete_row(self, row: _ARow) -> None:
        _mark_row(row.tr, _DEL, self.w)
        for p in row.tr.iter(_P):
            _delete_runs(_owned_runs(p), self.w)

    def edit_cell(self, tc, b_runs, r_runs) -> None:
        if id(tc) in self.tables_used:
            raise _Unsupported(REASON_UNSUPPORTED)  # one cell reached twice (vMerge)
        self.tables_used.add(id(tc))
        if tc.find(".//" + _TBL) is not None:
            raise _Unsupported(REASON_UNSUPPORTED)
        paras = _cell_paragraphs(tc)
        if not paras:
            raise _Unsupported(REASON_UNSUPPORTED)
        seq = _TokenSeq(paras)
        seq.apply(_md_tokens(b_runs), _md_tokens(r_runs), self.w)

    def edit_table(self, a: _ABlock, b: _MdBlock, r: _MdBlock) -> None:
        if not (a.ncols == b.ncols == r.ncols):
            raise _Unsupported(REASON_COLUMNS)
        a_keys = [row.key for row in a.rows]
        b_keys = ["|".join(_cell_key(c) for c in row) for row in b.rows]
        r_keys = ["|".join(_cell_key(c) for c in row) for row in r.rows]
        b2a: Dict[int, int] = {}
        for x, y, _eq in _pair(a_keys, b_keys, [False] * len(a_keys), [False] * len(b_keys)):
            if x is not None and y is not None:
                b2a[y] = x
        anchor_tr = None  # insert new rows after this row (None: before the first)
        for x, y, eq in _pair(b_keys, r_keys, [False] * len(b_keys), [False] * len(r_keys)):
            if x is not None and x in b2a:
                arow = a.rows[b2a[x]]
            else:
                arow = None
            if x is not None and y is not None:
                if arow is None:
                    raise _Unsupported(REASON_MISMATCH)
                if not eq:
                    for c in range(r.ncols):
                        if _cell_key(b.rows[x][c]) == _cell_key(r.rows[y][c]):
                            continue
                        tc = arow.cells[c]
                        if tc is None:
                            raise _Unsupported(REASON_UNSUPPORTED)
                        self.edit_cell(tc, b.rows[x][c], r.rows[y][c])
                anchor_tr = arow.tr
            elif x is not None:
                if arow is not None:
                    self.delete_row(arow)
                    anchor_tr = arow.tr
            else:
                template = anchor_tr if anchor_tr is not None else a.rows[0].tr
                if not self._simple_row(template):
                    template = next((row.tr for row in a.rows if self._simple_row(row.tr)), None)
                if template is None:
                    raise _Unsupported(REASON_UNSUPPORTED)
                new_tr = self.build_row(template, r.rows[y])
                if anchor_tr is not None:
                    anchor_tr.addnext(new_tr)
                else:
                    a.rows[0].tr.addprevious(new_tr)
                anchor_tr = new_tr

    # blocks -------------------------------------------------------------

    def edit_paragraph(self, a: _ABlock, b: _MdBlock, r: _MdBlock) -> None:
        seq = _TokenSeq([a.el])
        seq.apply(_md_tokens(_md_variant(a, b)), _md_tokens(_md_variant(a, r)), self.w)

    def delete_block(self, a: _ABlock) -> None:
        if a.is_table:
            for row in a.rows:
                self.delete_row(row)
        else:
            _delete_paragraph(a.el, self.w)


# ---------------------------------------------------------------- entry points


def _max_id(parts: Dict[str, bytes]) -> int:
    best = 0
    for name, data in parts.items():
        if name.startswith("word/") and name.endswith(".xml"):
            for m in _ID_RE.finditer(data):
                best = max(best, int(m.group(1)))
    return best


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _rezip(original: zipfile.ZipFile, document_xml: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info in original.infolist():
            data = document_xml if info.filename == DOCUMENT_PART else original.read(info.filename)
            zout.writestr(info, data)
    return out.getvalue()


def build_tracked_revision(
    base_docx: bytes,
    base_markdown: str,
    revised_markdown: str,
    *,
    author: str = DEFAULT_AUTHOR,
    date: Optional[datetime] = None,
    max_changed_ratio: float = DEFAULT_MAX_CHANGED_RATIO,
) -> TrackedResult:
    """The base file with B -> R written as tracked changes, or a reason why
    not (``docx`` None). Never raises."""
    try:
        return _build(base_docx, base_markdown, revised_markdown, author, date, max_changed_ratio)
    except _Unsupported as exc:
        return TrackedResult(None, exc.reason)
    except Exception:
        return TrackedResult(None, REASON_ERROR)


def apply_revision_as_tracked_changes(
    base_docx: bytes,
    base_markdown: str,
    revised_markdown: str,
    *,
    author: str = DEFAULT_AUTHOR,
    date: Optional[datetime] = None,
    max_changed_ratio: float = DEFAULT_MAX_CHANGED_RATIO,
) -> Optional[TrackedResult]:
    """Like :func:`build_tracked_revision`, but None on every fallback."""
    res = build_tracked_revision(
        base_docx, base_markdown, revised_markdown,
        author=author, date=date, max_changed_ratio=max_changed_ratio,
    )
    return res if res.applied else None


def _build(
    base_docx: bytes,
    base_markdown: str,
    revised_markdown: str,
    author: str,
    date: Optional[datetime],
    max_changed_ratio: float,
) -> TrackedResult:
    try:
        zf = zipfile.ZipFile(io.BytesIO(base_docx))
    except (zipfile.BadZipFile, ValueError, OSError):
        return TrackedResult(None, REASON_NOT_DOCX)
    with zf:
        names = set(zf.namelist())
        if DOCUMENT_PART not in names:
            return TrackedResult(None, REASON_NOT_DOCX)
        parts = {n: zf.read(n) for n in names if n.startswith("word/") and n.endswith(".xml")}
        root = etree.fromstring(parts[DOCUMENT_PART], _xml_parser())
        body = root.find(W("body"))
        if body is None:
            return TrackedResult(None, REASON_NOT_DOCX)

        a_blocks = _a_blocks(base_docx, root)
        b_blocks = _md_blocks(base_markdown)
        r_blocks = _md_blocks(revised_markdown)
        if not a_blocks or not b_blocks or not r_blocks:
            return TrackedResult(None, REASON_EMPTY)

        # A <-> B: where each block the model saw sits in the file.
        a_keys = [x.key for x in a_blocks]
        b_keys = [_md_key(x) for x in b_blocks]
        r_keys = [_md_key(x) for x in r_blocks]
        b2a: Dict[int, int] = {}
        for x, y, _eq in _pair(
            a_keys, b_keys, [x.is_table for x in a_blocks], [x.is_table for x in b_blocks]
        ):
            if x is not None and y is not None:
                b2a[y] = x
        unmatched = len(b_blocks) - len(b2a)
        if len(b2a) < _MIN_BASE_MATCH * len(b_blocks):
            return TrackedResult(None, REASON_MISMATCH, unmatched=unmatched)

        # B -> R: what the AI changed.
        plan = _pair(
            b_keys, r_keys, [x.is_table for x in b_blocks], [x.is_table for x in r_blocks]
        )
        changed = sum(1 for x, y, eq in plan if x is not None and y is not None and not eq)
        deleted = sum(1 for x, y, _ in plan if x is not None and y is None)
        inserted = sum(1 for x, y, _ in plan if x is None and y is not None)
        ratio = (changed + deleted) / len(b_blocks)
        counts = dict(inserted=inserted, deleted=deleted, changed_blocks=changed,
                      unmatched=unmatched, changed_ratio=round(ratio, 3))
        if ratio > max_changed_ratio:
            return TrackedResult(None, REASON_TOO_MANY, **counts)
        if not (changed or deleted or inserted):
            return TrackedResult(base_docx, REASON_NO_CHANGES, **counts)

        when = (date or datetime.now(timezone.utc)).astimezone(timezone.utc)
        w = _Writer(_max_id(parts) + 1, author, when.strftime("%Y-%m-%dT%H:%M:%SZ"))
        planner = _Planner(a_blocks, w, body)

        # Where the next inserted block goes: after this element, or (None)
        # before the first block of the file.
        anchor = None
        anchor_index = 0  # A index the anchor belongs to (for pPr choice)
        for x, y, eq in plan:
            a = a_blocks[b2a[x]] if x is not None and x in b2a else None
            if x is not None and y is not None and eq:
                if a is not None:
                    anchor, anchor_index = a.el, b2a[x] + 1
                continue
            if x is not None and y is not None and a is not None:
                bb, rb = b_blocks[x], r_blocks[y]
                if a.is_table != bb.is_table:
                    raise _Unsupported(REASON_MISMATCH)
                if a.is_table:
                    planner.edit_table(a, bb, rb)
                else:
                    planner.edit_paragraph(a, bb, rb)
                anchor, anchor_index = a.el, b2a[x] + 1
                continue
            if x is not None and y is None:
                if a is not None:
                    planner.delete_block(a)
                    anchor, anchor_index = a.el, b2a[x] + 1
                continue
            # Inserted (or changed from a block the file does not have).
            rb = r_blocks[y]
            new = planner.build_table(rb) if rb.is_table else planner.build_paragraph(rb, anchor_index)
            if anchor is not None:
                anchor.addnext(new)
            else:
                first = a_blocks[0].el
                first.addprevious(new)
            anchor = new

        xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out = _rezip(zf, xml)
    return TrackedResult(out, REASON_OK, **counts)


__all__: List[str] = [
    "TrackedResult",
    "apply_revision_as_tracked_changes",
    "build_tracked_revision",
    "DEFAULT_AUTHOR",
]
