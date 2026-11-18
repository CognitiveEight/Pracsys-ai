"""Neutral format model of a template (SIM-203).

Readers (text PDF, scan) produce it; ``docx_builder`` turns it into a clean
.docx; the test corpus is written in it. Units are points (1/72 inch) unless
a field says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

ALIGN_LEFT = "left"
ALIGN_CENTER = "center"
ALIGN_RIGHT = "right"
ALIGN_JUSTIFY = "justify"

ROLE_TITLE = "title"
ROLE_HEADING = "heading"
ROLE_BODY = "body"
ROLE_CLAUSE = "clause"  # a paragraph that starts with its own label
ROLE_SIGNATURE = "signature"


@dataclass
class TRun:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    size: Optional[float] = None  # pt; None = paragraph/document default
    font: Optional[str] = None  # None = document default
    br_before: bool = False  # a line break before this run (stacked lines)


@dataclass
class TPara:
    runs: List[TRun] = field(default_factory=list)
    align: str = ALIGN_LEFT
    role: str = ROLE_BODY
    level: int = 0  # heading level (1-4) or clause depth (0-3)
    label: str = ""  # literal numbering label, e.g. "1.", "3.2", "(a)", "ii."
    left_indent: float = 0.0
    first_indent: float = 0.0  # negative = hanging indent
    space_before: float = 0.0
    space_after: float = 6.0
    line_spacing: float = 1.0  # multiple of single spacing
    page_break_before: bool = False
    keep_together: bool = False

    @property
    def text(self) -> str:
        out = []
        for r in self.runs:
            if r.br_before:
                out.append("\n")
            out.append(r.text)
        return "".join(out)


@dataclass
class TCell:
    paras: List[TPara] = field(default_factory=list)
    span: int = 1  # columns this cell covers (gridSpan)
    shade: Optional[str] = None  # hex fill, e.g. "D9D9D9"

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paras)


@dataclass
class TTable:
    rows: List[List[TCell]] = field(default_factory=list)
    col_widths: List[float] = field(default_factory=list)  # pt
    header_rows: int = 0
    borders: bool = True
    align: str = ALIGN_CENTER


@dataclass
class TImage:
    data: bytes
    width: float  # pt
    align: str = ALIGN_LEFT
    mime: str = "image/png"


Block = Union[TPara, TTable, TImage]


@dataclass
class PageSetup:
    width: float = 595.3  # A4
    height: float = 841.9
    margin_left: float = 72.0
    margin_right: float = 72.0
    margin_top: float = 72.0
    margin_bottom: float = 72.0


@dataclass
class TemplateIR:
    blocks: List[Block] = field(default_factory=list)
    header: List[Block] = field(default_factory=list)  # repeats on every page
    footer: List[Block] = field(default_factory=list)
    page_numbers: bool = False  # "Page X of Y" style footer field
    page_number_format: str = "Page {PAGE} of {NUMPAGES}"
    page: PageSetup = field(default_factory=PageSetup)
    font: str = "Times New Roman"
    size: float = 12.0
    source: str = ""  # "docx" | "pdf" | "scan" | "corpus"

    def paragraphs(self) -> List[TPara]:
        """Body paragraphs in reading order, including those in table cells."""
        out: List[TPara] = []
        for b in self.blocks:
            if isinstance(b, TPara):
                out.append(b)
            elif isinstance(b, TTable):
                for row in b.rows:
                    for cell in row:
                        out.extend(cell.paras)
        return out

    def tables(self) -> List[TTable]:
        return [b for b in self.blocks if isinstance(b, TTable)]
