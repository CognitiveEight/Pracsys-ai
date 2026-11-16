"""Template map: an ID for every paragraph, table, row and cell (SIM-204).

The fill engine edits a template in place, so the model must be able to name
each part of it. ``walk`` visits the body in reading order — paragraphs,
tables (rows and cells), and content controls — and gives each a stable ID:

- ``p12``            a body paragraph
- ``t1``             a table, ``t1.r2`` a row, ``t1.r2.c0`` a cell

``map_text`` renders the map for the prompt: one line per paragraph with its
style, alignment, label and emphasis, and one line per table row with its
cells. The header and footer (letterhead, page numbers) are never listed:
they are never edited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, Iterator, List, Optional

from docx import Document
from docx.oxml.ns import qn

from app.processing.template_fidelity.checklist import docx_label

_ALIGN = {"center": "center", "right": "right", "both": "justify", "left": "left"}


@dataclass
class Entry:
    id: str
    kind: str  # "p" | "table" | "row" | "cell"
    element: object
    text: str = ""
    style: str = ""
    align: str = ""
    label: str = ""
    auto_number: bool = False
    bold: bool = False
    cells: List[str] = field(default_factory=list)  # rows only
    header: bool = False  # rows only: repeating header row


def _p_text(p) -> str:
    out = []
    for node in p.iter():
        if node.tag == qn("w:t"):
            out.append(node.text or "")
        elif node.tag == qn("w:tab"):
            out.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            out.append("\n")
    return "".join(out)


def _numbered_styles(doc) -> set:
    """Style ids whose paragraphs Word numbers (numPr on the style or a base)."""
    by_id = {s.style_id: s for s in doc.styles}
    out = set()
    for sid, style in by_id.items():
        seen, cur = set(), style
        while cur is not None and cur.style_id not in seen:
            seen.add(cur.style_id)
            ppr = cur.element.find(qn("w:pPr"))
            if ppr is not None and ppr.find(qn("w:numPr")) is not None:
                out.add(sid)
                break
            cur = getattr(cur, "base_style", None)
    return out


def _p_info(p, styles: Dict[str, str], numbered: set) -> Dict[str, object]:
    ppr = p.find(qn("w:pPr"))
    style, align, auto = "", "", False
    if ppr is not None:
        ps = ppr.find(qn("w:pStyle"))
        if ps is not None:
            sid = ps.get(qn("w:val")) or ""
            style = styles.get(sid, sid)
            # "List Number" and similar number through their style.
            auto = sid in numbered
        jc = ppr.find(qn("w:jc"))
        if jc is not None:
            align = _ALIGN.get(jc.get(qn("w:val")) or "", "")
        num = ppr.find(qn("w:numPr"))
        if num is not None:
            num_id = num.find(qn("w:numId"))
            # numId 0 explicitly switches numbering off.
            auto = num_id is None or num_id.get(qn("w:val")) != "0"
    text = _p_text(p)
    runs = [r for r in p.iter(qn("w:r")) if r.find(qn("w:t")) is not None]
    bold = bool(runs) and all(
        (r.find(qn("w:rPr")) is not None and r.find(qn("w:rPr")).find(qn("w:b")) is not None)
        for r in runs if "".join(t.text or "" for t in r.iter(qn("w:t"))).strip())
    return {"text": text, "style": style, "align": align, "auto": auto, "bold": bold,
            "label": "" if auto else docx_label(text)}


def _body_children(body) -> Iterator[object]:
    for child in body:
        if child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _body_children(content)
        else:
            yield child


def walk(doc) -> List[Entry]:
    """Every addressable part of the document body, in reading order."""
    styles = {s.style_id: s.name for s in doc.styles}
    numbered = _numbered_styles(doc)
    entries: List[Entry] = []
    pn = tn = 0
    for child in _body_children(doc.element.body):
        if child.tag == qn("w:p"):
            info = _p_info(child, styles, numbered)
            entries.append(Entry("p{}".format(pn), "p", child, info["text"], info["style"],
                                 info["align"], info["label"], info["auto"], info["bold"]))
            pn += 1
        elif child.tag == qn("w:tbl"):
            tid = "t{}".format(tn)
            tn += 1
            entries.append(Entry(tid, "table", child))
            for ri, tr in enumerate(child.findall(qn("w:tr"))):
                rid = "{}.r{}".format(tid, ri)
                trpr = tr.find(qn("w:trPr"))
                header = trpr is not None and trpr.find(qn("w:tblHeader")) is not None
                cells = []
                row = Entry(rid, "row", tr, header=header)
                entries.append(row)
                for ci, tc in enumerate(tr.findall(qn("w:tc"))):
                    ctext = "\n".join(_p_text(p) for p in tc.findall(qn("w:p")))
                    cells.append(ctext)
                    entries.append(Entry("{}.c{}".format(rid, ci), "cell", tc, ctext))
                row.cells = cells
    return entries


@dataclass
class TemplateMap:
    entries: List[Entry]

    def by_id(self) -> Dict[str, Entry]:
        return {e.id: e for e in self.entries}


def build_map(doc) -> TemplateMap:
    return TemplateMap(walk(doc))


def map_text(tmap: TemplateMap, *, max_chars: int = 600) -> str:
    """The map as the model sees it."""
    lines: List[str] = []
    tables: Dict[str, int] = {}
    for e in tmap.entries:
        if e.kind == "row":
            tid = e.id.split(".")[0]
            tables[tid] = max(tables.get(tid, 0), len(e.cells))
    for e in tmap.entries:
        if e.kind == "p":
            tags = [t for t in (e.style if e.style and e.style != "Normal" else "",
                                e.align, "auto-numbered" if e.auto_number else "",
                                "label={}".format(e.label) if e.label else "",
                                "bold" if e.bold else "") if t]
            text = e.text.replace("\n", " / ").replace("\t", " ")
            if len(text) > max_chars:
                text = text[:max_chars] + " …"
            lines.append("{} [{}] {}".format(e.id, "|".join(tags), text) if tags
                         else "{} {}".format(e.id, text or "(empty)"))
        elif e.kind == "table":
            nrows = sum(1 for x in tmap.entries if x.kind == "row" and x.id.startswith(e.id + "."))
            lines.append("{} TABLE {} rows x {} columns".format(e.id, nrows, tables.get(e.id, 0)))
        elif e.kind == "row":
            cells = " | ".join(c.replace("\n", " / ") or "(empty)" for c in e.cells)
            lines.append("{}{} | {}".format(e.id, " [header]" if e.header else "", cells))
    return "\n".join(lines)


def load(docx_bytes: bytes):
    return Document(BytesIO(docx_bytes))


__all__ = ["Entry", "TemplateMap", "build_map", "map_text", "walk", "load", "Optional"]
