"""A cited chat reply keeps its line breaks, and sheet figures read as shown.

The citation checker splits the reply into sentences and joins them back. A
line break straight after a full stop was never part of any sentence, so it
was dropped: every "* Row N" bullet and "####" heading ran into the line
before it. Separately, a float formula cell (late-payment interest) was
rendered with binary noise (4532.849999999999), which the model then quoted.
Fictional data only; no model, Qdrant or socket calls.
"""

from app.processing.tables import make_cell
from app.rag.citations import _sentences, verify_and_number
from app.rag.evidence import EvidencePack

REPLY = (
    "#### B. Financial Year 2014-15\n\n"
    "* **Row 28 (Q2):** Transaction Amount: Rs. 2,15,850; Interest: Rs. 4,532.85 [E1].\n"
    "* **Row 29 (Q2):** Transaction Amount: Rs. 2,24,000; Interest: Rs. 4,704 [E1].\n"
    "#### C. Financial Year 2016-17\n"
    "1. First point.\n2. Second point.\n\n"
    "The full rows need the original workbook."
)


def _pack():
    pack = EvidencePack()
    pack.add_passage(
        {"content": "Row 28 | 2,15,850 | 4,532.85\nRow 29 | 2,24,000 | 4,704",
         "metadata": {}, "original_document_id": "doc-fictional",
         "document_id": "doc-fictional_chunk_0"},
        annexure=1, file_name="ledger.xlsx",
    )
    return pack


def test_sentences_cover_the_text_exactly():
    assert "".join(s for _, s in _sentences(REPLY)) == REPLY
    assert "".join(s for _, s in _sentences("One.\n\nTwo.\nThree")) == "One.\n\nTwo.\nThree"


def test_cited_reply_keeps_bullets_and_headings_on_their_own_lines():
    pack = _pack()
    label = next(iter(pack.items))
    text, cites, _report = verify_and_number(REPLY.replace("[E1]", f"[{label}]"), pack)
    lines = text.split("\n")
    assert "#### B. Financial Year 2014-15" in lines
    assert "#### C. Financial Year 2016-17" in lines
    assert sum(1 for ln in lines if ln.startswith("* **Row")) == 2
    assert "1. First point." in lines and "2. Second point." in lines
    assert ".*" not in text and ".####" not in text
    assert cites


def test_float_cells_read_as_the_sheet_shows_them():
    # Display text is rounded; the value used for computation stays exact.
    cell = make_cell(4532.849999999999, "#,##0.00")
    assert cell.display == "4532.85" and cell.value == 4532.849999999999
    assert make_cell(2523.7799999999997, "General").display == "2523.78"
    assert make_cell(2523.7799999999997, None).display == "2523.78"
    assert make_cell(4532.849999999999, "0").display == "4533"
    assert make_cell(0.155, "0.0%").display == "15.5%"
    assert make_cell(120180, "#,##0.00").display == "120180"
    assert make_cell(0.1 + 0.2, None).display == "0.3"


def test_half_way_figures_round_up_as_the_sheet_shows_them():
    # Excel rounds half away from zero: 2.5 shown with "0" reads 3, and
    # 1,234.50 shown with "#,##0" reads 1,235. Banker's rounding (the Decimal
    # default) would show 2 and 1234, a figure the sheet never displays.
    assert make_cell(2.5, "0").display == "3"
    assert make_cell(-2.5, "0").display == "-3"
    assert make_cell(0.125, "0.00").display == "0.13"
    assert make_cell(1234.5, "#,##0").display == "1235"
    assert make_cell(0.0045, "0.0%").display == "0.5%"
