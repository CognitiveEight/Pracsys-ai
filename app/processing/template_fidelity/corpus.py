"""Fictional template corpus (SIM-207). Test data only — every name, number and
address here is invented.

Six document types, each in several layout variants that differ on purpose
(font, size, numbering style, title alignment, letterhead, page numbers,
line spacing, table borders), so nothing downstream can depend on one
layout. ``build_corpus()`` returns them as format models; ``pdf_writer``
turns each into a text PDF and a simulated scan.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.processing.template_fidelity.ir import (
    ALIGN_CENTER,
    ALIGN_JUSTIFY,
    ALIGN_LEFT,
    ALIGN_RIGHT,
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

DOC_TYPES = (
    "residential_rent",
    "commercial_rent",
    "simple_will",
    "complex_will",
    "simple_sha",
    "complex_sha",
)


@dataclass(frozen=True)
class Variant:
    name: str
    font: str = "Times New Roman"
    size: float = 12.0
    numbering: str = "decimal"  # decimal | clause | alpha
    title_align: str = ALIGN_CENTER
    body_align: str = ALIGN_JUSTIFY
    letterhead: bool = False
    page_numbers: bool = False
    line_spacing: float = 1.15
    table_borders: bool = True
    table_header: bool = True
    margins: float = 72.0


VARIANTS: Tuple[Variant, ...] = (
    Variant("classic"),
    Variant("modern", font="Arial", size=11.0, numbering="clause", title_align=ALIGN_LEFT,
            body_align=ALIGN_LEFT, letterhead=True, page_numbers=True, line_spacing=1.0,
            margins=60.0),
    Variant("firm", font="Times New Roman", size=12.0, numbering="alpha",
            letterhead=True, page_numbers=True, line_spacing=1.5, table_header=False),
    Variant("plain", font="Arial", size=12.0, numbering="decimal", body_align=ALIGN_LEFT,
            line_spacing=1.0, table_borders=False, margins=90.0),
)


# ---------------------------------------------------------------------------
# Numbering schemes
# ---------------------------------------------------------------------------

_ROMAN = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x")


def _label(scheme: str, path: Sequence[int]) -> str:
    """Label for a clause at ``path`` (1-based indexes per level)."""
    depth = len(path)
    if scheme == "clause":
        if depth == 1:
            return "Clause {}.".format(path[0])
        if depth == 2:
            return "{}.{}".format(path[0], path[1])
        if depth == 3:
            return "({})".format(chr(96 + path[2]))
        return "({})".format(_ROMAN[path[3] - 1])
    if scheme == "alpha":
        if depth == 1:
            return "{}.".format(path[0])
        if depth == 2:
            return "({})".format(chr(96 + path[1]))
        return "({})".format(_ROMAN[path[-1] - 1])
    # decimal
    if depth == 1:
        return "{}.".format(path[0])
    if depth == 2:
        return "{}.{}".format(path[0], path[1])
    if depth == 3:
        return "({})".format(chr(96 + path[2]))
    return "({})".format(_ROMAN[path[3] - 1])


# A clause tree: (heading or text, children). A first-level node with
# children is a numbered heading; a leaf is a numbered clause.
Clause = Tuple[str, list]


class _Writer:
    def __init__(self, variant: Variant) -> None:
        self.v = variant
        self.blocks: list = []
        self.top = 0  # last top-level clause number, so numbering runs on after tables

    # paragraphs -----------------------------------------------------------
    def para(self, text: str, *, align: Optional[str] = None, bold: bool = False,
             italic: bool = False, underline: bool = False, space_after: float = 8.0,
             space_before: float = 0.0, role: str = "body", level: int = 0,
             size: Optional[float] = None, page_break: bool = False,
             keep: bool = False) -> TPara:
        runs: List[TRun] = []
        for i, line in enumerate(text.split("\n")):
            runs.append(TRun(line, bold=bold, italic=italic, underline=underline,
                             size=size, br_before=i > 0))
        p = TPara(runs=runs, align=align or self.v.body_align, role=role, level=level,
                  space_after=space_after, space_before=space_before,
                  line_spacing=self.v.line_spacing, page_break_before=page_break,
                  keep_together=keep)
        self.blocks.append(p)
        return p

    def title(self, text: str) -> None:
        self.para(text, align=self.v.title_align, bold=True, role=ROLE_TITLE, level=1,
                  size=self.v.size + 4, space_after=14.0)

    def heading(self, text: str, *, page_break: bool = False) -> None:
        self.para(text, align=ALIGN_LEFT if self.v.title_align == ALIGN_LEFT else ALIGN_CENTER,
                  bold=True, role=ROLE_HEADING, level=2, space_before=10.0,
                  space_after=6.0, page_break=page_break, underline=self.v.numbering == "alpha")

    def clauses(self, tree: List[Clause]) -> None:
        for text, children in tree:
            self.top += 1
            self._clause(text, children, [self.top])

    def _clause(self, text: str, children: list, path: List[int]) -> None:
        label = _label(self.v.numbering, path)
        depth = len(path) - 1
        is_heading = depth == 0 and bool(children)
        indent = 36.0 * depth
        hang = 28.0 if self.v.numbering != "clause" or depth else 54.0
        p = TPara(runs=[TRun(text, bold=is_heading)], align=ALIGN_LEFT if is_heading
                  else self.v.body_align, role=ROLE_CLAUSE, level=depth, label=label,
                  left_indent=indent + hang, first_indent=-hang,
                  space_before=6.0 if is_heading else 0.0, space_after=6.0,
                  line_spacing=self.v.line_spacing)
        if is_heading:
            p.runs[0].bold = True
        self.blocks.append(p)
        for j, (ctext, cchildren) in enumerate(children, 1):
            self._clause(ctext, cchildren, path + [j])

    def table(self, header: Optional[List[str]], rows: List[List[str]],
              widths: Optional[List[float]] = None, merged_header: Optional[List[Tuple[str, int]]] = None
              ) -> None:
        def cell(text: str, bold: bool = False, align: str = ALIGN_LEFT, shade=None,
                 span: int = 1) -> TCell:
            return TCell(paras=[TPara(runs=[TRun(t, bold=bold, br_before=k > 0)
                                            for k, t in enumerate(text.split("\n"))],
                                      align=align, space_after=2.0)],
                         shade=shade, span=span)

        out: List[List[TCell]] = []
        hdr_rows = 0
        if merged_header:
            out.append([cell(t, True, ALIGN_CENTER, "D9D9D9" if self.v.table_borders else None, s)
                        for t, s in merged_header])
            hdr_rows += 1
        if header and self.v.table_header:
            out.append([cell(h, True, ALIGN_CENTER, "D9D9D9" if self.v.table_borders else None)
                        for h in header])
            hdr_rows += 1
        for r in rows:
            out.append([cell(c, align=ALIGN_RIGHT if _is_amount(c) else ALIGN_LEFT) for c in r])
        self.blocks.append(TTable(rows=out, col_widths=list(widths or []), header_rows=hdr_rows,
                                  borders=self.v.table_borders))
        self.para("", space_after=4.0)

    def signature(self, lines: List[str]) -> None:
        self.para("\n".join(lines), align=ALIGN_LEFT, role=ROLE_SIGNATURE, keep=True,
                  space_before=14.0, space_after=6.0)


def _is_amount(text: str) -> bool:
    t = text.replace(",", "").replace("INR", "").replace("%", "").strip()
    return bool(t) and t.replace(".", "", 1).isdigit()


# ---------------------------------------------------------------------------
# Letterhead
# ---------------------------------------------------------------------------


def _logo_png() -> bytes:
    """A small fictional logo (a dark square with a lighter ring)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (120, 120), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 110, 110), fill=(30, 50, 90))
    d.ellipse((30, 30, 90, 90), outline=(220, 220, 220), width=6)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _letterhead(v: Variant) -> list:
    return [
        TImage(data=_logo_png(), width=40.0, align=ALIGN_LEFT),
        TPara(runs=[TRun("EXAMPLE & ASSOCIATES", bold=True, size=v.size + 2)],
              align=ALIGN_RIGHT, space_after=0.0),
        TPara(runs=[TRun("Advocates and Solicitors", italic=True, size=v.size - 1),
                    TRun("12 Sample Street, Testville 400001", size=v.size - 2, br_before=True)],
              align=ALIGN_RIGHT, space_after=4.0),
    ]


# ---------------------------------------------------------------------------
# Document types (fictional content)
# ---------------------------------------------------------------------------

_LESSOR = "MR. ANAND EXAMPLE, aged about 52 years, residing at 4 Fiction Road, Testville"
_LESSEE = "MS. PRIYA SAMPLE, aged about 34 years, residing at 9 Demo Lane, Sampleton"


def _witnesses(w: _Writer) -> None:
    w.heading("WITNESSES")
    w.signature(["1. Signature: ____________________", "Name: <Witness 1 Name>",
                 "Address: <Witness 1 Address>"])
    w.signature(["2. Signature: ____________________", "Name: <Witness 2 Name>",
                 "Address: <Witness 2 Address>"])


def _residential_rent(w: _Writer) -> None:
    w.title("RESIDENTIAL RENTAL AGREEMENT")
    w.para("This Rental Agreement is made at <Place> on this <Day> day of <Month>, <Year>.")
    w.para("BETWEEN", align=ALIGN_CENTER, bold=True)
    w.para(_LESSOR + " (hereinafter called the \"LESSOR\") of the ONE PART;")
    w.para("AND", align=ALIGN_CENTER, bold=True)
    w.para(_LESSEE + " (hereinafter called the \"LESSEE\") of the OTHER PART.")
    w.para("WHEREAS the Lessor is the owner of the residential flat described in the "
           "Schedule below and has agreed to let it to the Lessee on the following terms:")
    w.clauses([
        ("TERM", [("The lease is for eleven (11) months commencing on <Start Date>.", []),
                  ("It may be renewed by mutual written consent.", [])]),
        ("RENT AND DEPOSIT", [("The Lessee shall pay rent as set out in the table below.", []),
                              ("Rent is payable on or before the 5th day of each month.", [])]),
    ])
    w.table(["Particular", "Amount (INR)", "Due"],
            [["Monthly rent", "25,000", "5th of every month"],
             ["Security deposit", "75,000", "On signing"],
             ["Maintenance", "2,500", "Monthly"]],
            [200, 120, 150])
    w.clauses([
        ("USE AND CARE", [("The Lessee shall use the premises for residence only.", []),
                          ("The Lessee shall not:", [
                              ("sub-let the premises;", []),
                              ("make structural changes; or", []),
                              ("keep inflammable material.", [])])]),
        ("TERMINATION", [("Either party may end this Agreement with one month's notice.", [])]),
    ])
    w.heading("SCHEDULE", page_break=True)
    w.table(["Item", "Description"],
            [["Flat", "Flat No. 101, First Floor, Sample Residency"],
             ["Area", "850 sq. ft. carpet"], ["Parking", "One covered car park"]],
            [150, 320])
    w.para("IN WITNESS WHEREOF the parties have signed this Agreement on the date above.")
    w.signature(["LESSOR", "____________________", "(ANAND EXAMPLE)"])
    w.signature(["LESSEE", "____________________", "(PRIYA SAMPLE)"])
    _witnesses(w)


def _commercial_rent(w: _Writer) -> None:
    w.title("DEED OF LEASE (COMMERCIAL PREMISES)")
    w.para("This Deed of Lease is executed at <Place> on <Date>.")
    w.para("BETWEEN", align=ALIGN_CENTER, bold=True)
    w.para("EXAMPLE ESTATES PRIVATE LIMITED, a company having its office at 1 Model Park, "
           "Testville (the \"Lessor\");")
    w.para("AND", align=ALIGN_CENTER, bold=True)
    w.para("DEMO TRADERS LLP, having its office at 77 Mock Avenue, Sampleton (the \"Lessee\").")
    w.clauses([
        ("GRANT AND TERM", [("The Lessor grants a lease of the premises for five (5) years.", []),
                            ("The first thirty-six (36) months are a lock-in period.", [])]),
        ("RENT AND ESCALATION", [("Rent, exclusive of GST, is payable as follows:", [])]),
    ])
    w.table(["Lease Year", "Monthly Rent (INR)", "Escalation"],
            [["Year 1", "1,50,000", "-"], ["Year 2", "1,57,500", "5%"],
             ["Year 3", "1,65,375", "5%"], ["Year 4", "1,73,644", "5%"],
             ["Year 5", "1,82,326", "5%"]], [150, 170, 150])
    w.clauses([
        ("SECURITY DEPOSIT", [("An interest-free deposit of INR 9,00,000 is payable.", []),
                              ("It is refundable on vacant possession, less:", [
                                  ("unpaid rent and charges;", []),
                                  ("cost of repairing damage beyond normal wear;", [])])]),
        ("FIT-OUT", [("The Lessee may carry out interior works with prior consent.", [])]),
        ("GOVERNING LAW", [("This Deed is governed by the laws of India.", [])]),
    ])
    w.heading("SCHEDULE OF PREMISES", page_break=True)
    w.table(None, [["Unit", "Shop 4, Ground Floor, Example Mall"],
                   ["Carpet area", "1,100 sq. ft."], ["Car parks", "Two"]], [160, 310])
    w.signature(["For EXAMPLE ESTATES PRIVATE LIMITED", "____________________",
                 "Authorised Signatory"])
    w.signature(["For DEMO TRADERS LLP", "____________________", "Designated Partner"])
    _witnesses(w)


def _simple_will(w: _Writer) -> None:
    w.title("LAST WILL AND TESTAMENT")
    w.para("I, RAMESH EXAMPLE, aged about 68 years, residing at 3 Fiction Road, Testville, "
           "being of sound mind, make this my last Will on <Date>.")
    w.clauses([
        ("I revoke all my earlier Wills and codicils.", []),
        ("I appoint my son, AJAY EXAMPLE, as the Executor of this Will.", []),
        ("I give my residential flat at Testville to my wife, SUNITA EXAMPLE.", []),
        ("I give all my remaining property in equal shares to my children.", []),
    ])
    w.para("Signed by the Testator in our presence, and by us in the presence of the "
           "Testator and of each other.")
    w.signature(["____________________", "TESTATOR"])
    _witnesses(w)


def _complex_will(w: _Writer) -> None:
    w.title("WILL")
    w.para("THIS IS THE LAST WILL of me, MEERA SAMPLE, aged about 71 years, residing at "
           "21 Demo Gardens, Sampleton.")
    w.clauses([
        ("REVOCATION", [("I revoke all previous Wills and testamentary dispositions.", [])]),
        ("EXECUTORS AND TRUSTEES", [
            ("I appoint KIRAN SAMPLE and NEHA SAMPLE jointly as Executors.", []),
            ("If either cannot act, the other shall act alone.", [])]),
        ("SPECIFIC BEQUESTS", [
            ("I bequeath the assets in Schedule A as follows:", [
                ("my jewellery to my daughter NEHA SAMPLE;", []),
                ("my shares in Example Holdings to my son KIRAN SAMPLE;", []),
                ("my car to my grandson, subject to Clause 4.", [])])]),
        ("TRUST FOR MINORS", [
            ("Any share of a beneficiary under 21 years shall be held in trust.", [
                ("The Trustees may apply income for education and maintenance;", [
                    ("including school and college fees;", []),
                    ("including medical expenses.", [])]),
                ("The capital vests on the beneficiary attaining 21 years.", [])])]),
        ("RESIDUE", [("The residue of my estate shall go to my children equally.", [])]),
    ])
    w.heading("SCHEDULE A - ASSETS", page_break=True)
    w.table(["No.", "Asset", "Particulars", "Approx. Value (INR)"],
            [["1", "Flat", "Flat 5B, Demo Gardens, Sampleton", "1,20,00,000"],
             ["2", "Shares", "10,000 shares of Example Holdings Ltd", "45,00,000"],
             ["3", "Jewellery", "Gold and diamond jewellery in bank locker", "30,00,000"],
             ["4", "Vehicle", "Sedan car, Reg. No. XX-00-XX-0000", "8,00,000"]],
            [40, 90, 230, 110])
    w.para("Signed by the Testatrix in our joint presence.")
    w.signature(["____________________", "TESTATRIX"])
    _witnesses(w)


def _simple_sha(w: _Writer) -> None:
    w.title("SHAREHOLDERS' AGREEMENT")
    w.para("This Agreement is made on <Date> between the shareholders listed in the table "
           "below and EXAMPLE TECH PRIVATE LIMITED (the \"Company\").")
    w.table(["Shareholder", "Shares", "Holding (%)"],
            [["Arun Example", "6,000", "60.00"], ["Bela Sample", "4,000", "40.00"]],
            [220, 120, 120])
    w.clauses([
        ("BOARD", [("The Board shall have three directors.", []),
                   ("Each shareholder holding 25% or more may appoint one director.", [])]),
        ("TRANSFER OF SHARES", [("No shareholder shall transfer shares except:", [
            ("to an affiliate; or", []), ("after offering them first to the others.", [])])]),
        ("DEADLOCK", [("A deadlock shall be referred to mediation.", [])]),
        ("GOVERNING LAW", [("This Agreement is governed by the laws of India.", [])]),
    ])
    w.signature(["ARUN EXAMPLE", "____________________"])
    w.signature(["BELA SAMPLE", "____________________"])
    w.signature(["For EXAMPLE TECH PRIVATE LIMITED", "____________________", "Director"])


def _complex_sha(w: _Writer) -> None:
    w.title("SHAREHOLDERS' AND SUBSCRIPTION AGREEMENT")
    w.para("This Agreement is entered into on <Date> by and among EXAMPLE FOODS PRIVATE "
           "LIMITED (the \"Company\"), the Promoters and SAMPLE VENTURES FUND I (the "
           "\"Investor\").")
    w.clauses([
        ("DEFINITIONS", [("\"Board\" means the board of directors of the Company.", []),
                         ("\"Reserved Matters\" means the matters listed in Schedule 2.", [])]),
        ("SUBSCRIPTION", [("The Investor shall subscribe to Series A shares as follows:", [])]),
    ])
    w.table(["Shareholder", "Equity", "Series A", "Equity", "Series A"],
            [["Promoter 1", "5,000", "-", "5,000", "-"],
             ["Promoter 2", "3,000", "-", "3,000", "-"],
             ["Investor", "-", "-", "-", "2,000"],
             ["Total", "8,000", "-", "8,000", "2,000"]],
            [150, 80, 80, 80, 80],
            merged_header=[("", 1), ("Pre-Closing", 2), ("Post-Closing", 2)])
    w.clauses([
        ("GOVERNANCE", [("The Investor may appoint one director.", []),
                        ("No Reserved Matter shall be undertaken without Investor consent.", [])]),
        ("TRANSFER RESTRICTIONS", [
            ("Promoters shall not transfer shares during the lock-in.", []),
            ("After the lock-in, transfers are subject to:", [
                ("a right of first refusal in favour of the Investor;", []),
                ("a tag-along right; and", [
                    ("the tag-along applies to a sale of 10% or more;", []),
                    ("it is exercisable within 30 days.", [])]),
                ("a drag-along right after five years.", [])])]),
        ("ANTI-DILUTION", [("The Investor has broad-based weighted average protection.", [])]),
        ("DISPUTE RESOLUTION", [("Disputes go to arbitration seated at Mumbai.", [])]),
    ])
    w.heading("SCHEDULE 2 - RESERVED MATTERS", page_break=True)
    w.table(["No.", "Reserved Matter"],
            [["1", "Amendment of the Articles of Association"],
             ["2", "Issue of new securities"],
             ["3", "Annual budget and business plan"],
             ["4", "Related party transactions above INR 10,00,000"]], [50, 420])
    w.signature(["For EXAMPLE FOODS PRIVATE LIMITED", "____________________", "Director"])
    w.signature(["For SAMPLE VENTURES FUND I", "____________________", "Authorised Signatory"])
    w.signature(["PROMOTER 1", "____________________"])


_BUILDERS: Dict[str, Callable[[_Writer], None]] = {
    "residential_rent": _residential_rent,
    "commercial_rent": _commercial_rent,
    "simple_will": _simple_will,
    "complex_will": _complex_will,
    "simple_sha": _simple_sha,
    "complex_sha": _complex_sha,
}


def build_template(doc_type: str, variant: Variant) -> TemplateIR:
    """One fictional template as a format model."""
    w = _Writer(variant)
    _BUILDERS[doc_type](w)
    m = variant.margins
    ir = TemplateIR(blocks=w.blocks, font=variant.font, size=variant.size,
                    page=PageSetup(margin_left=m, margin_right=m, margin_top=m + 18,
                                   margin_bottom=m),
                    page_numbers=variant.page_numbers, source="corpus")
    if variant.letterhead:
        ir.header = _letterhead(variant)
    return ir


def build_corpus(doc_types: Sequence[str] = DOC_TYPES,
                 variants: Sequence[Variant] = VARIANTS) -> List[Tuple[str, Variant, TemplateIR]]:
    return [(t, v, build_template(t, v)) for t in doc_types for v in variants]


# Re-exported for tests that need to know which paragraphs are signature blocks.
__all__ = ["DOC_TYPES", "VARIANTS", "Variant", "build_template", "build_corpus",
           "ROLE_SIGNATURE", "ROLE_HEADING"]
