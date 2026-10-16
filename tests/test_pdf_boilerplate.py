"""The running-header strip must never delete body words.

PyPDF2 emits some PDFs one word per line. A long body word repeated across
pages ("verification", 12 chars, on 3 of 4 pages) used to pass as a running
header and was deleted everywhere: "BCCI Age Verification Committee" became
"BCCI Age Committee" in every panel built from the document.
"""

from app.processing.file_handlers import _strip_repeated_pdf_boilerplate


def _word_per_line(text: str) -> str:
    return "\n \n".join(text.split())


def test_repeated_single_word_is_body_text_not_a_header():
    pages = [_word_per_line(f"Page {n} text: oversight by the BCCI Age Verification Committee.") for n in range(1, 5)]
    out = _strip_repeated_pdf_boilerplate(pages)
    assert out.count("Verification") == 4
    assert out.count("Committee.") == 4


def test_real_running_header_is_still_removed():
    header = "Karnataka State Cricket Association - Confidential Correspondence"
    pages = [f"{header}\nBody paragraph number {n} about the selection." for n in range(1, 5)]
    out = _strip_repeated_pdf_boilerplate(pages)
    assert header not in out
    assert out.count("Body paragraph") == 4


def test_publisher_watermark_still_removed_on_one_page():
    out = _strip_repeated_pdf_boilerplate(["SCC Online Web Edition\nThe judgment text."])
    assert "SCC Online" not in out and "The judgment text." in out
