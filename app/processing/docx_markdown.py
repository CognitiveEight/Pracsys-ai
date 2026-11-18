"""Read a lawyer-edited draft .docx back into the drafting Markdown (SIM-219).

The drafting agent keeps a draft as Markdown (``last_draft_markdown``) and
renders it to Word with ``docx_generator``. When the lawyer edits the Word
file in the editor, the next revision has to start from that file, so this
module is the reverse of ``docx_generator._render_with_defaults``:

- ``Heading N`` / ``Title`` paragraphs become ``#``-headings;
- tables become GFM pipe tables (first row is the header row; a cell's own
  paragraphs are joined with ``<br>``, which the renderer turns back into a
  line break; a horizontally merged cell is written once, then left empty);
- bullet paragraphs (``List Bullet`` styles, or a bullet ``numPr``) become
  ``-`` items; Word-numbered paragraphs get their visible number as text, the
  way ``FileProcessor`` reads them for templates;
- bold and italic runs keep ``**`` / ``*``;
- manual line breaks inside a paragraph become Markdown line breaks, so
  signature and address blocks keep their lines.

Headers, footers, footnotes and images are not carried: the generator adds
its own page-number footer, and a draft's text lives in the body. Pure
function; callers check the file with ``docx_review_state`` first (a document
with open tracked changes is refused before it gets here).
"""

from __future__ import annotations

import io
import re
from typing import Iterator, List, Optional, Tuple

_HEADING_STYLE_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_BULLET_STYLE_RE = re.compile(r"^list bullet(?:\s*(\d))?$", re.IGNORECASE)
# A line that Markdown would read as structure when it is plain text.
_BLOCK_LEAD_RE = re.compile(r"^(\s*)(#{1,6}\s|>|\|)")
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _qn(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _on(el) -> bool:
    """A toggle property (``w:b``, ``w:i``) is on unless ``w:val`` says off."""
    if el is None:
        return False
    val = el.get(_qn("w:val"))
    return val is None or val.lower() not in ("0", "false", "off", "none")


def _runs(p_el) -> List[Tuple[str, bool, bool]]:
    """``(text, bold, italic)`` per run, ``"\\n"`` for a line break.

    Reads every ``w:r`` under the paragraph (hyperlinks and content controls
    included) but a drawing's ``mc:Fallback`` copy only once.
    """
    t_tag, tab_tag, br_tag, cr_tag = _qn("w:t"), _qn("w:tab"), _qn("w:br"), _qn("w:cr")
    r_tag, rpr_tag = _qn("w:r"), _qn("w:rPr")
    nbh_tag = _qn("w:noBreakHyphen")
    skip = {id(x) for fb in p_el.iter(_MC_FALLBACK) for x in fb.iter()}
    out: List[Tuple[str, bool, bool]] = []
    for r in p_el.iter(r_tag):
        if id(r) in skip:
            continue
        rpr = r.find(rpr_tag)
        bold = _on(rpr.find(_qn("w:b"))) if rpr is not None else False
        italic = _on(rpr.find(_qn("w:i"))) if rpr is not None else False
        for child in r:
            if child.tag == t_tag:
                if child.text:
                    out.append((child.text, bold, italic))
            elif child.tag == tab_tag:
                out.append((" ", bold, italic))
            elif child.tag == nbh_tag:
                out.append(("-", bold, italic))
            elif child.tag == cr_tag or (
                child.tag == br_tag and child.get(_qn("w:type")) not in ("page", "column")
            ):
                out.append(("\n", False, False))
    return out


def _wrap(text: str, bold: bool, italic: bool) -> str:
    """Emphasis markers around the words only: ``** x**`` does not parse."""
    if not (bold or italic) or not text.strip():
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    mark = "***" if bold and italic else "**" if bold else "*"
    return f"{lead}{mark}{core}{mark}{trail}"


def _inline(runs: List[Tuple[str, bool, bool]], *, plain: bool = False) -> List[str]:
    """Lines of Markdown inline text (split at manual line breaks)."""
    lines: List[str] = []
    current: List[str] = []
    # Merge neighbouring runs with the same formatting first: Word splits a
    # word across runs freely, and "**ab****cd**" would not render.
    merged: List[Tuple[str, bool, bool]] = []
    for text, b, i in runs:
        if text != "\n" and merged and merged[-1][0] != "\n" and merged[-1][1:] == (b, i):
            merged[-1] = (merged[-1][0] + text, b, i)
        else:
            merged.append((text, b, i))
    for text, b, i in merged:
        if text == "\n":
            lines.append("".join(current))
            current = []
            continue
        current.append(text if plain else _wrap(text, b, i))
    lines.append("".join(current))
    return [re.sub(r"[  ]{2,}", " ", ln).strip() for ln in lines]


def _escape_block_lead(line: str) -> str:
    m = _BLOCK_LEAD_RE.match(line)
    if not m:
        return line
    return f"{m.group(1)}\\{line[len(m.group(1)):]}"


def _style_name(paragraph) -> str:
    try:
        return (paragraph.style.name or "").strip() if paragraph.style is not None else ""
    except Exception:
        return ""


def _heading_level(paragraph) -> Optional[int]:
    name = _style_name(paragraph)
    if name.lower() == "title":
        return 1
    m = _HEADING_STYLE_RE.match(name)
    if m:
        return max(1, min(int(m.group(1)), 6))
    return None


def _blocks(parent) -> Iterator:
    """Body paragraphs and tables in reading order, content controls opened."""
    sdt, sdt_content, custom = _qn("w:sdt"), _qn("w:sdtContent"), _qn("w:customXml")
    p_tag, tbl_tag = _qn("w:p"), _qn("w:tbl")
    for child in parent.iterchildren():
        if child.tag in (sdt, custom):
            inner = child.find(sdt_content) if child.tag == sdt else child
            if inner is not None:
                yield from _blocks(inner)
        elif child.tag in (p_tag, tbl_tag):
            yield child


def _cell_text(tc) -> str:
    parts: List[str] = []
    for p in tc.iter(_qn("w:p")):
        lines = [ln for ln in _inline(_runs(p)) if ln]
        if lines:
            parts.append("<br>".join(lines))
    return "<br>".join(parts).replace("|", "\\|")


def _table_markdown(tbl_el, doc) -> List[str]:
    from docx.table import Table

    rows: List[List[str]] = []
    for row in Table(tbl_el, doc).rows:
        cells: List[str] = []
        seen = set()
        for cell in row.cells:
            if id(cell._tc) in seen:
                cells.append("")  # horizontal merge: written once
                continue
            seen.add(id(cell._tc))
            cells.append(_cell_text(cell._tc))
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    # The renderer bolds the header row; GFM already marks it as the header.
    rows[0] = [
        c[2:-2] if len(c) > 4 and c.startswith("**") and c.endswith("**") and "**" not in c[2:-2]
        else c
        for c in rows[0]
    ]

    def line(cells: List[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    out = [line(rows[0]), "| " + " | ".join(["---"] * ncols) + " |"]
    out.extend(line(r) for r in rows[1:])
    return out


def docx_to_draft_markdown(docx_bytes: bytes) -> str:
    """The body of a Word draft as drafting Markdown. Raises on an unreadable
    file (``python-docx`` errors propagate; the caller reports it)."""
    from docx import Document
    from docx.text.paragraph import Paragraph

    from app.processing.file_handlers import FileProcessor

    doc = Document(io.BytesIO(docx_bytes))
    numfmt_map = FileProcessor._docx_numbering_formats(doc)
    counters: dict = {}

    # Each entry is (kind, text). Consecutive list items are joined with a
    # single newline so they stay one list; everything else is a block.
    entries: List[Tuple[str, str]] = []
    for el in _blocks(doc.element.body):
        if el.tag == _qn("w:tbl"):
            table = _table_markdown(el, doc)
            if table:
                entries.append(("block", "\n".join(table)))
            continue

        paragraph = Paragraph(el, doc)
        level = _heading_level(paragraph)
        runs = _runs(el)
        if level is not None:
            text = " ".join(ln for ln in _inline(runs, plain=True) if ln)
            if text:
                entries.append(("block", f"{'#' * level} {text}"))
            continue

        lines = _inline(runs)
        if not any(lines):
            continue
        # Drop empty lines at either end (a trailing break before a table).
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()

        style = _style_name(paragraph)
        bullet = _BULLET_STYLE_RE.match(style)
        marker = FileProcessor._docx_list_marker(paragraph, numfmt_map, counters)
        if bullet or marker.strip() == "•" or lines[0].lstrip().startswith("•"):
            depth = (int(bullet.group(1)) - 1) if bullet and bullet.group(1) else 0
            if marker:
                depth = (len(marker) - len(marker.lstrip(" "))) // 2
            indent = "  " * max(depth, 0)
            first = lines[0].lstrip("•").strip()
            item = [f"{indent}- {first}"] + [f"{indent}  {ln}" for ln in lines[1:]]
            entries.append(("item", "\n".join(item)))
            continue
        if marker:
            lines[0] = marker.lstrip(" ") + lines[0]
        lines = [_escape_block_lead(ln) for ln in lines]
        entries.append(("block", "\n".join(lines)))

    out: List[str] = []
    for i, (kind, text) in enumerate(entries):
        if i and not (kind == "item" and entries[i - 1][0] == "item"):
            out.append("")
        out.append(text)
    return "\n".join(out).strip() + "\n" if out else ""
