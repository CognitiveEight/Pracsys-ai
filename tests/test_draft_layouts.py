"""SIM-208: standard layout per document type (no template). Fictional data."""

from io import BytesIO

import pytest
from docx import Document

from app.processing.docx_generator import render_markdown_to_docx
from app.processing.draft_layouts import (
    LAYOUT_AFFIDAVIT,
    LAYOUT_AGREEMENT,
    LAYOUT_COURT,
    LAYOUT_DEFAULT,
    LAYOUT_NOTICE,
    LAYOUT_VAKALATNAMA,
    infer_layout,
    is_title_line,
    starts_body,
)

CENTER = 1


def _doc(md: str, title: str) -> Document:
    return Document(BytesIO(render_markdown_to_docx(md, template_docx_bytes=None, title=title)))


def _para(doc, startswith: str):
    return next(p for p in doc.paragraphs if p.text.startswith(startswith))


@pytest.mark.parametrize("title,expected", [
    ("Legal Notice for Recovery of Dues", LAYOUT_NOTICE),
    ("Reply Notice to Demand Notice dated 1 March", LAYOUT_NOTICE),
    ("FIR Complaint to SHO", LAYOUT_NOTICE),
    ("Anticipatory Bail Application under Section 482 BNSS", LAYOUT_COURT),
    ("Civil Plaint for Recovery", LAYOUT_COURT),
    ("Written Statement on behalf of Defendant", LAYOUT_COURT),
    ("Affidavit in support of Application", LAYOUT_AFFIDAVIT),
    ("Vakalatnama", LAYOUT_VAKALATNAMA),
    ("Residential Rent Agreement", LAYOUT_AGREEMENT),
    ("Supplemental Addendum to the Shareholders' Agreement", LAYOUT_AGREEMENT),
    ("Last Will and Testament", LAYOUT_AGREEMENT),
    ("General Power of Attorney", LAYOUT_AGREEMENT),
    ("Draft", LAYOUT_DEFAULT),
    ("Parental consent form", LAYOUT_DEFAULT),
])
def test_infer_layout(title, expected):
    assert infer_layout(title).name == expected


def test_court_filing_layout():
    md = ("IN THE HIGH COURT OF EXAMPLE STATE AT TESTVILLE\n\n"
          "BAIL APPLICATION NO. <Number> OF 2026\n\n"
          "IN THE MATTER OF:\n\n"
          "RAVI SAMPLE ... APPLICANT\n\n"
          "VERSUS\n\n"
          "STATE OF EXAMPLE ... RESPONDENT\n\n"
          "# APPLICATION UNDER SECTION 482 BNSS FOR ANTICIPATORY BAIL\n\n"
          "MOST RESPECTFULLY SHOWETH:\n\n"
          "1. That the applicant is a law-abiding citizen.\n"
          "2. That the applicant has not been named in the FIR.\n")
    doc = _doc(md, "Anticipatory Bail Application under Section 482 BNSS")
    sec = doc.sections[0]
    assert round(sec.left_margin.inches, 2) == 1.5
    assert doc.styles["Normal"].font.size.pt == 14
    for line in ("IN THE HIGH COURT", "BAIL APPLICATION NO.", "VERSUS", "APPLICATION UNDER"):
        assert _para(doc, line).alignment == CENTER, line
    # party lines and the body are not centred
    assert _para(doc, "RAVI SAMPLE").alignment != CENTER
    assert _para(doc, "MOST RESPECTFULLY").alignment != CENTER


def test_notice_layout_keeps_letterhead_left():
    md = ("**EXAMPLE & ASSOCIATES**  \nAdvocates  \n12 Sample Street, Testville\n\n"
          "Ref. No. <Reference Number>  \nDate: 1 March 2026\n\n"
          "# LEGAL NOTICE\n\n### SPEED POST / EMAIL\n\n### CONFIDENTIAL\n\nTO\n\n"
          "MR. DEMO EXAMPLE\n\nDear Sir,\n\n1. Under instructions from our client ...\n")
    doc = _doc(md, "Legal Notice for Recovery of Dues")
    assert doc.styles["Normal"].paragraph_format.line_spacing == 1.15
    for line in ("LEGAL NOTICE", "SPEED POST", "CONFIDENTIAL"):
        assert _para(doc, line).alignment == CENTER, line
    assert _para(doc, "EXAMPLE & ASSOCIATES").alignment != CENTER  # letterhead block
    assert _para(doc, "MR. DEMO EXAMPLE").alignment != CENTER  # after "TO": body


def test_agreement_layout_title_centred_body_justified():
    md = ("# RESIDENTIAL RENTAL AGREEMENT\n\n"
          "This Rental Agreement is made at Testville on 1 March 2026.\n\n"
          "BETWEEN\n\nMR. ANAND EXAMPLE (the \"LESSOR\")\n\nAND\n\nMS. PRIYA SAMPLE\n")
    doc = _doc(md, "Residential Rent Agreement")
    assert _para(doc, "RESIDENTIAL RENTAL").alignment == CENTER
    body = _para(doc, "This Rental Agreement")
    assert body.alignment != CENTER  # body started: nothing after is title block
    assert doc.styles["Normal"].paragraph_format.line_spacing == 1.15


def test_untitled_draft_keeps_previous_defaults():
    doc = _doc("CAPITAL LINE\n\nBody.", "Draft")
    assert doc.styles["Normal"].font.size.pt == 12
    assert doc.styles["Normal"].paragraph_format.line_spacing == 1.5
    assert _para(doc, "CAPITAL LINE").alignment != CENTER


def test_helpers():
    assert is_title_line("IN THE COURT OF THE CIVIL JUDGE, TESTVILLE")
    assert not is_title_line("This is ordinary prose.")
    assert starts_body("MOST RESPECTFULLY SHOWETH:")
    assert starts_body("1. That the applicant ...")
    assert starts_body("Dear Sir,")
    assert not starts_body("VERSUS")
