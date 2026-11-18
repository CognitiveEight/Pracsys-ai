"""Write a draft into a template, in place (SIM-204).

The model returns one JSON operation per line (continuation-safe: a cut-off
answer loses at most its last line):

    {"op": "replace", "id": "p7", "text": "The rent is **INR 25,000** ..."}
    {"op": "replace", "id": "t0.r1.c1", "text": "25,000"}
    {"op": "insert_after", "id": "p7", "text": "...", "like": "p7"}
    {"op": "add_row", "id": "t0.r2", "cells": ["Parking", "1,500", "Monthly"]}
    {"op": "delete", "id": "p9"}          (also a row "t0.r3" or a table "t1")
    {"op": "keep", "id": "p3"}            (optional; anything not named is kept)

Every edit keeps the template's own formatting: replaced text takes the
paragraph's first run's font, size and colour (``**bold**``, ``*italic*``
and ``__underline__`` toggle on top); an inserted paragraph or row is a copy
of the anchor (or ``like``) — style, indents, spacing, numbering, borders —
with new text; Word auto-numbering continues by itself. The header, footer,
letterhead and page setup are never touched. Unknown IDs are skipped and
counted, never fatal.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.processing.draft_markdown import PLACEHOLDER_RE, is_placeholder
from app.processing.template_fidelity.checklist import LABEL_RE
from app.processing.template_fidelity.docx_map import Entry, build_map, load

_MARK_RE = re.compile(r"(\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*))")


@dataclass
class FillStats:
    applied: int = 0
    inserted: int = 0
    deleted: int = 0
    rows_added: int = 0
    unknown: int = 0
    invalid: int = 0
    by_op: Dict[str, int] = field(default_factory=dict)


def parse_ops(text: str) -> List[Dict[str, Any]]:
    """JSON lines, a JSON array, or ``{"ops": [...]}``; bad lines are skipped."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    if text.startswith("[") or text.startswith("{\"ops\""):
        try:
            data = json.loads(text, strict=False)
            ops = data.get("ops") if isinstance(data, dict) else data
            return [o for o in ops or [] if isinstance(o, dict)]
        except ValueError:
            pass
    out = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line, strict=False)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ---------------------------------------------------------------------------
# Run building
# ---------------------------------------------------------------------------

def _base_rpr(p) -> Optional[object]:
    for r in p.iter(qn("w:r")):
        t = r.find(qn("w:t"))
        if t is not None and (t.text or "").strip():
            rpr = r.find(qn("w:rPr"))
            return copy.deepcopy(rpr) if rpr is not None else None
    for r in p.iter(qn("w:r")):
        rpr = r.find(qn("w:rPr"))
        if rpr is not None:
            return copy.deepcopy(rpr)
    ppr = p.find(qn("w:pPr"))
    if ppr is not None and ppr.find(qn("w:rPr")) is not None:
        rpr = copy.deepcopy(ppr.find(qn("w:rPr")))
        rpr.tag = qn("w:rPr")
        return rpr
    return None


def _set_flag(rpr, tag: str, on: bool) -> None:
    el = rpr.find(qn(tag))
    if on:
        if el is None:
            el = OxmlElement(tag)
            if tag == "w:u":
                el.set(qn("w:val"), "single")
            rpr.append(el)
        else:
            el.attrib.pop(qn("w:val"), None)
            if tag == "w:u":
                el.set(qn("w:val"), "single")
    elif el is not None:
        rpr.remove(el)


def _make_run(text: str, base, *, bold: Optional[bool] = None, italic: Optional[bool] = None,
              underline: Optional[bool] = None, highlight: bool = False):
    r = OxmlElement("w:r")
    rpr = copy.deepcopy(base) if base is not None else OxmlElement("w:rPr")
    if bold is not None:
        _set_flag(rpr, "w:b", bold)
    if italic is not None:
        _set_flag(rpr, "w:i", italic)
    if underline is not None:
        _set_flag(rpr, "w:u", underline)
    if highlight:
        h = rpr.find(qn("w:highlight"))
        if h is None:
            h = OxmlElement("w:highlight")
            rpr.append(h)
        h.set(qn("w:val"), "yellow")
    if len(rpr):
        r.append(rpr)
    parts = text.split("\t")
    for i, part in enumerate(parts):
        if i:
            r.append(OxmlElement("w:tab"))
        if part:
            t = OxmlElement("w:t")
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t.text = part
            r.append(t)
    return r


def _runs_for(text: str, base) -> List[object]:
    """Runs for ``text`` with **bold**/*italic*/__underline__ and line breaks."""
    out: List[object] = []
    for li, line in enumerate(text.split("\n")):
        if li:
            br = OxmlElement("w:r")
            if base is not None:
                br.append(copy.deepcopy(base))
            br.append(OxmlElement("w:br"))
            out.append(br)
        for piece in _MARK_RE.split(line):
            if not piece:
                continue
            bold = italic = underline = None
            if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
                piece, bold = piece[2:-2], True
            elif piece.startswith("__") and piece.endswith("__") and len(piece) > 4:
                piece, underline = piece[2:-2], True
            elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
                piece, italic = piece[1:-1], True
            pos = 0
            for m in PLACEHOLDER_RE.finditer(piece):
                if not is_placeholder(m.group(1)):
                    continue
                if m.start() > pos:
                    out.append(_make_run(piece[pos:m.start()], base, bold=bold, italic=italic,
                                         underline=underline))
                out.append(_make_run(m.group(0), base, bold=bold, italic=italic,
                                     underline=underline, highlight=True))
                pos = m.end()
            if pos < len(piece):
                out.append(_make_run(piece[pos:], base, bold=bold, italic=italic,
                                     underline=underline))
    return out


def _keeps(r) -> bool:
    """Runs that are not text: images, fields, footnote marks — kept."""
    return any(r.find(qn(t)) is not None for t in
               ("w:drawing", "w:pict", "w:fldChar", "w:instrText", "w:footnoteReference",
                "w:object"))


def set_paragraph_text(p, text: str, *, keep_label: str = "", auto_number: bool = False) -> None:
    """Replace a paragraph's text, keeping its formatting and non-text runs."""
    base = _base_rpr(p)
    text = text if text is not None else ""
    if auto_number:
        # Word writes the number; a label in the text would double it.
        m = LABEL_RE.match(text + " ")
        if m:
            text = text[m.end():].lstrip(" \t")
    elif keep_label and not LABEL_RE.match(text + " "):
        text = keep_label + "\t" + text
    elif keep_label:
        m = LABEL_RE.match(text + " ")
        if m and not text[m.end():m.end() + 1] == "\t":
            text = text[:m.end()].rstrip() + "\t" + text[m.end():].lstrip()
    for child in list(p):
        if child.tag in (qn("w:r"), qn("w:hyperlink"), qn("w:ins"), qn("w:smartTag")):
            if child.tag == qn("w:r") and _keeps(child):
                continue
            p.remove(child)
    for r in _runs_for(text, base):
        p.append(r)


def _set_cell_text(tc, text: str) -> None:
    paras = tc.findall(qn("w:p"))
    if not paras:
        paras = [OxmlElement("w:p")]
        tc.append(paras[0])
    set_paragraph_text(paras[0], text)
    for extra in paras[1:]:
        tc.remove(extra)


def _remove(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    parent.remove(el)
    if parent.tag == qn("w:tc") and parent.find(qn("w:p")) is None:
        parent.append(OxmlElement("w:p"))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_ops(docx_bytes: bytes, ops: List[Dict[str, Any]]) -> Tuple[bytes, FillStats]:
    """Apply fill operations to the template; returns (new .docx bytes, stats)."""
    doc = load(docx_bytes)
    entries: Dict[str, Entry] = build_map(doc).by_id()
    stats = FillStats()
    # The last element inserted after each anchor, so several inserts after
    # one anchor keep the model's order.
    tail: Dict[str, object] = {}
    deleted: List[object] = []

    for op in ops:
        kind = str(op.get("op") or "").lower()
        eid = str(op.get("id") or "")
        stats.by_op[kind] = stats.by_op.get(kind, 0) + 1
        if kind == "keep":
            continue
        entry = entries.get(eid)
        if entry is None:
            stats.unknown += 1
            continue
        try:
            if kind == "replace":
                text = str(op.get("text") if op.get("text") is not None else "")
                if entry.kind == "p":
                    set_paragraph_text(entry.element, text, keep_label=entry.label,
                                       auto_number=entry.auto_number)
                elif entry.kind == "cell":
                    _set_cell_text(entry.element, text)
                else:
                    stats.invalid += 1
                    continue
                stats.applied += 1
            elif kind == "delete":
                if entry.kind in ("p", "row", "table"):
                    deleted.append(entry.element)
                    stats.deleted += 1
                elif entry.kind == "cell":
                    _set_cell_text(entry.element, "")
                    stats.applied += 1
            elif kind == "insert_after":
                like = entries.get(str(op.get("like") or ""))
                src = like if like is not None and like.kind == "p" else entry
                if src.kind != "p":
                    # Anchored on a table: copy the paragraph style of the
                    # nearest paragraph before it.
                    src = _nearest_paragraph(entries, eid)
                    if src is None:
                        stats.invalid += 1
                        continue
                new = copy.deepcopy(src.element)
                set_paragraph_text(new, str(op.get("text") or ""), keep_label="",
                                   auto_number=src.auto_number)
                tail.get(eid, entry.element).addnext(new)
                tail[eid] = new
                stats.inserted += 1
            elif kind == "add_row":
                if entry.kind != "row":
                    stats.invalid += 1
                    continue
                new = copy.deepcopy(entry.element)
                trpr = new.find(qn("w:trPr"))
                if trpr is not None and trpr.find(qn("w:tblHeader")) is not None:
                    trpr.remove(trpr.find(qn("w:tblHeader")))
                cells = [str(c) for c in (op.get("cells") or [])]
                for ci, tc in enumerate(new.findall(qn("w:tc"))):
                    _set_cell_text(tc, cells[ci] if ci < len(cells) else "")
                tail.get(eid, entry.element).addnext(new)
                tail[eid] = new
                stats.rows_added += 1
            else:
                stats.invalid += 1
        except Exception:
            stats.invalid += 1
    for el in deleted:
        _remove(el)
    out = BytesIO()
    doc.save(out)
    return out.getvalue(), stats


def _nearest_paragraph(entries: Dict[str, Entry], eid: str) -> Optional[Entry]:
    ids = list(entries)
    try:
        k = ids.index(eid)
    except ValueError:
        return None
    for j in range(k - 1, -1, -1):
        e = entries[ids[j]]
        if e.kind == "p" and e.text.strip():
            return e
    return None
