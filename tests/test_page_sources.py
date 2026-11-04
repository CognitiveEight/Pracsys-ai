"""SIM-192: every kind of evidence cites a real page, or none.

* paper-book summaries (LIST OF DATES, SYNOPSIS) are cited only as a last
  resort, like the index;
* a page the model claims is kept only when the entry is on it;
* Word files carry Word's own page layout when it is complete, else no page;
* a photo or scan is Page 1; spreadsheets, audio and text have no page.

Fictional data only; no OCR or model calls.
"""

import zipfile
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement

from app.processing.file_handlers import FileProcessor
from app.rag.page_kinds import (
    anchor_page,
    avoid_pages,
    date_forms,
    index_pages,
    resolve_entry_page,
    summary_pages,
)

COVER = """IN THE HIGH COURT OF FICTIONLAND
W.P. (C) No. 1 of 2026
INDEX
S.No. Particulars Page No.
1. List of Dates 2-3
2. Writ Petition 4-5
3. Annexure P-1 Notice 6
"""
DATES_P2 = """IN THE HIGH COURT OF FICTIONLAND
LIST OF
DATES
12.03.2024 Notice issued to the club by the petitioner.
20.04.2024 Reply sent by the club.
"""
DATES_P3 = """05.05.2024 Election notified for the governing body.
SYNOPSIS
The petitioner challenges the election notified on 05.05.2024.
"""
PETITION_P4 = """IN THE HIGH COURT OF FICTIONLAND
WRIT PETITION
1. On 12th March, 2024 the petitioner issued a notice to the club.
"""
PETITION_P5 = """2. The club replied on 20.04.2024 refusing the request.
"""
ANNEXURE_P6 = """ANNEXURE P-1
LEGAL NOTICE dated 12.03.2024 issued to Fictional Club.
"""
BOOK = "".join(
    f"--- Page {n} ---\n{t}"
    for n, t in enumerate([COVER, DATES_P2, DATES_P3, PETITION_P4, PETITION_P5, ANNEXURE_P6], 1)
)


# ------------------------------------------------------------------ paper-book summaries

def test_list_of_dates_and_synopsis_are_avoided_until_the_petition_starts():
    assert index_pages(BOOK) == {1}
    assert summary_pages(BOOK) == {2, 3}  # "LIST OF" / "DATES" split over two lines
    assert avoid_pages(BOOK) == {1, 2, 3}


def test_a_date_in_the_list_of_dates_is_cited_where_the_petition_states_it():
    assert anchor_page(BOOK, ["20.04.2024"]) == 5


def test_a_date_only_in_the_list_of_dates_still_gets_that_page():
    assert anchor_page(BOOK, ["05.05.2024"]) == 3


def test_a_synopsis_heading_mid_document_does_not_swallow_the_annexures():
    assert 6 not in avoid_pages(BOOK)


def test_ordinary_documents_have_no_summary_pages():
    letter = "--- Page 1 ---\nDear Sir,\nThe list of dates you asked for is below.\n"
    assert summary_pages(letter) == set()


# ------------------------------------------------------------------ dates as written

def test_date_forms_find_a_written_date_across_a_line_break():
    text = "--- Page 1 ---\nnothing\n--- Page 2 ---\nOn 12th March,\n2024 the notice went out.\n"
    assert anchor_page(text, date_forms("2024-03-12")) == 2


def test_date_forms_never_match_inside_a_longer_number():
    text = "--- Page 1 ---\nRef 112.03.2024X\n"
    assert anchor_page(text, date_forms("2024-03-12")) is None


# ------------------------------------------------------------------ the model's page is checked

def _entry(page):
    return resolve_entry_page(BOOK, page, ["", *date_forms("2024-03-12"), "Notice issued"])


def test_a_model_page_carrying_the_date_is_kept():
    assert _entry(4) == 4  # "12th March, 2024" in the petition
    assert _entry(6) == 6


def test_a_model_page_without_the_date_is_corrected():
    # Placed by its date as first written: the notice itself, Annexure P-1.
    assert _entry(5) == 6


def test_a_model_page_on_the_index_or_list_of_dates_is_corrected():
    assert _entry(1) == 6
    assert _entry(2) == 6


def test_no_page_when_the_document_has_no_pages():
    assert resolve_entry_page("Notice dated 12.03.2024", 1, date_forms("2024-03-12")) is None


def test_no_page_when_the_entry_cannot_be_placed():
    assert resolve_entry_page(BOOK, None, date_forms("2023-01-09")) is None


# ------------------------------------------------------------------ Word files

def _docx(pages_in_app_xml, *, breaks=True, table=False) -> bytes:
    doc = Document()
    doc.add_paragraph("Agreement between the fictional parties.")
    second = doc.add_paragraph()
    if breaks:
        second.add_run()._r.append(OxmlElement("w:lastRenderedPageBreak"))
    second.add_run("Clause two continues on the next page.")
    if table:
        t = doc.add_table(rows=1, cols=2)
        t.rows[0].cells[0].text = "Rent"
        t.rows[0].cells[1].text = "Rs. 25,000"
    buf = BytesIO()
    doc.save(buf)
    src = zipfile.ZipFile(BytesIO(buf.getvalue()))
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "docProps/app.xml" and pages_in_app_xml is not None:
                data = (
                    b'<?xml version="1.0" encoding="UTF-8"?><Properties>'
                    b"<Pages>%d</Pages></Properties>" % pages_in_app_xml
                )
            dst.writestr(item, data)
    return out.getvalue()


def test_word_file_gets_wordss_own_pages_when_complete():
    text = FileProcessor.extract_text_from_docx(_docx(2), mark_pages=True)
    assert text.startswith("--- Page 1 ---\nAgreement")
    assert "\n--- Page 2 ---\nClause two" in text


def test_word_file_with_an_incomplete_layout_gets_no_pages():
    text = FileProcessor.extract_text_from_docx(_docx(3), mark_pages=True)
    assert "--- Page" not in text and "Clause two" in text


def test_one_page_word_file_is_page_one():
    text = FileProcessor.extract_text_from_docx(_docx(1, breaks=False), mark_pages=True)
    assert text.startswith("--- Page 1 ---")


def test_word_tables_reach_the_evidence_text():
    data = _docx(2, table=True)
    assert "Rent | Rs. 25,000" in FileProcessor.extract_text_from_docx(data, mark_pages=True)


def test_word_text_for_drafting_is_unchanged():
    data = _docx(2, table=True)
    plain = FileProcessor.extract_text_from_docx(data)
    assert "--- Page" not in plain and "Rent" not in plain  # as before this change


# ------------------------------------------------------------------ images and page-less files

def test_a_photo_is_page_one(monkeypatch):
    from app.processing.ocr import OcrResult

    monkeypatch.setattr(
        FileProcessor, "ocr_image_result", staticmethod(lambda _b: OcrResult(text="Receipt No. 42"))
    )
    assert FileProcessor.extract_text_from_image(b"jpg", mark_pages=True) == (
        "--- Page 1 ---\nReceipt No. 42"
    )
    assert FileProcessor.extract_text_from_image(b"jpg") == "Receipt No. 42"


def test_an_unreadable_photo_keeps_its_sentinel(monkeypatch):
    from app.processing.ocr import OcrResult

    monkeypatch.setattr(FileProcessor, "ocr_image_result", staticmethod(lambda _b: OcrResult()))
    assert FileProcessor.extract_text_from_image(b"jpg", mark_pages=True).startswith("[")


def test_page_less_text_gets_no_page():
    transcript = "[00:12] Speaker A: the notice was sent on 12.03.2024"
    assert resolve_entry_page(transcript, 1, date_forms("2024-03-12")) is None


# ------------------------------------------------------------------ routing passes mark_pages through

def _fake_ocr(monkeypatch, text="Receipt No. 42"):
    from app.processing.ocr import OcrResult

    monkeypatch.setattr(
        FileProcessor, "ocr_image_result", staticmethod(lambda _b: OcrResult(text=text))
    )


def test_routing_marks_a_named_word_file_only_for_evidence():
    data = _docx(2)
    evidence = FileProcessor.process_file_by_extension(data, "agreement.DOCX", mark_pages=True)
    assert evidence.startswith("--- Page 1 ---") and "\n--- Page 2 ---\nClause two" in evidence
    drafting = FileProcessor.process_file_by_extension(data, "agreement.docx")
    assert drafting == FileProcessor.extract_text_from_docx(data)
    assert "--- Page" not in drafting


def test_routing_marks_a_word_file_sniffed_from_its_content():
    # The backend's filename has no extension: the type comes from the bytes.
    data = _docx(2)
    evidence = FileProcessor.process_file_by_extension(data, "upload_0001", mark_pages=True)
    assert evidence.startswith("--- Page 1 ---") and "\n--- Page 2 ---\nClause two" in evidence
    assert "--- Page" not in FileProcessor.process_file_by_extension(data, "upload_0001")


def test_routing_marks_a_named_photo_only_for_evidence(monkeypatch):
    _fake_ocr(monkeypatch)
    for name in ("receipt.JPG", "receipt.png", "receipt.webp"):
        assert FileProcessor.process_file_by_extension(b"img", name, mark_pages=True) == (
            "--- Page 1 ---\nReceipt No. 42"
        )
        assert FileProcessor.process_file_by_extension(b"img", name) == "Receipt No. 42"


def test_routing_marks_a_photo_sniffed_from_its_content(monkeypatch):
    _fake_ocr(monkeypatch)
    for magic in (b"\xff\xd8\xff\xe0", b"\x89PNG\r\n\x1a\n", b"GIF89a"):
        data = magic + b"fictional-pixels"
        assert FileProcessor.process_file_by_extension(data, "upload_0002", mark_pages=True) == (
            "--- Page 1 ---\nReceipt No. 42"
        )
        assert FileProcessor.process_file_by_extension(data, "upload_0002") == "Receipt No. 42"


def test_a_model_page_carrying_only_the_quote_is_kept():
    # No date form on page 5, but the entry's own quote is: the model's page stands.
    anchors = ["club replied on 20.04.2024 refusing", *date_forms("2024-04-21")]
    assert resolve_entry_page(BOOK, 5, anchors) == 5
    # The same quote does not rescue a claimed page it is not on.
    assert resolve_entry_page(BOOK, 4, anchors) == 5


# ------------------------------------------------------------------ verify findings

def test_word_pages_inside_a_content_control_are_not_lost():
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_paragraph("Cover page text.")
    sdt, content = OxmlElement("w:sdt"), OxmlElement("w:sdtContent")
    inner = doc.add_paragraph()
    inner.add_run()._r.append(OxmlElement("w:lastRenderedPageBreak"))
    inner.add_run("Inside control on page two.")
    content.append(inner._p)  # moves the paragraph into the control
    sdt.append(content)
    doc.element.body.insert(1, sdt)
    third = doc.add_paragraph()
    third.add_run()._r.append(OxmlElement("w:lastRenderedPageBreak"))
    third.add_run("Third page body.")
    assert doc.element.body.find(qn("w:sdt")) is not None
    buf = BytesIO()
    doc.save(buf)
    src = zipfile.ZipFile(BytesIO(buf.getvalue()))
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "docProps/app.xml":
                data = b"<Properties><Pages>3</Pages></Properties>"
            dst.writestr(item, data)
    text = FileProcessor.extract_text_from_docx(out.getvalue(), mark_pages=True)
    assert "--- Page 2 ---\nInside control" in text
    assert "--- Page 3 ---\nThird page body." in text


def test_a_month_only_date_is_found_as_written():
    text = "--- Page 1 ---\nINDEX\n1. Reply March 2024 2-3\n2. Notice 4-5\n--- Page 2 ---\nIn March 2024 the club replied.\n"
    assert "March 2024" in date_forms("2024-03")
    assert anchor_page(text, date_forms("2024-03")) == 2


def test_a_bare_year_never_places_an_entry():
    text = "--- Page 1 ---\nFiled in 2024.\n--- Page 2 ---\nRent for 2024 paid.\n"
    assert anchor_page(text, ["2024"]) is None


def test_audio_citation_names_the_quoted_line_not_the_passage_start():
    from app.rag.citations import verify_and_number
    from app.rag.evidence import EvidencePack

    pack = EvidencePack()
    pack.add_passage(
        {
            "content": "[00:05] Speaker A: Hello, this is the club office.\n"
            "[01:10] Speaker B: The rent of Rs. 25,000 was paid on 12.03.2024.\n",
            "original_document_id": "call1",
            "document_id": "call1_chunk_0",
            "metadata": {"time_start": "00:05"},
        },
        annexure=1,
        file_name="call.mp3",
    )
    _, cites, _ = verify_and_number("Rs. 25,000 was paid on 12.03.2024 [E1].", pack)
    assert cites[0]["timestamp"] == "01:10"
    assert cites[0].get("page") is None
