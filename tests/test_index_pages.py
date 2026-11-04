"""SIM-192: citations point at the evidence page, not the paper-book index.

A paper-book's INDEX names every party and restates every annexure's date, so
"first page that matches" landed there. Fictional paper-book only.
"""

from app.rag.attribution import Attributor
from app.rag.page_kinds import index_pages
from app.socket.handlers.party_details import details_page, first_mention_page

INDEX_P1 = """IN THE HIGH COURT OF FICTIONLAND
W.P. (C) No. 1 of 2026
Arun Mehta & Ors ... Petitioners
Versus
Fictional Club ... Respondent
INDEX
S.No. Particulars Page No.
1. Memo of Parties 3-4
2. Synopsis and List of Dates 5-6
3. Notice dated 12.03.2024 7-9
"""
INDEX_P2 = """4. Reply dated 20.04.2024 10-12
5. Annexure P-1 Bye-laws 13-30
6. Annexure P-2 Minutes 31-35
7. Vakalatnama 36-37
"""
MEMO_P3 = """MEMO OF PARTIES
1. Arun Mehta, House No. 12, Lake View Road, Kothrud, Pune 411001,
Mobile 9876543210 ... Petitioner No. 1
"""
NOTICE_P4 = """LEGAL NOTICE
This notice dated 12.03.2024 is issued on behalf of Arun Mehta to the
Fictional Club regarding the conduct of its annual election.
"""
PAPER_BOOK = "".join(
    f"--- Page {n} ---\n{t}" for n, t in enumerate([INDEX_P1, INDEX_P2, MEMO_P3, NOTICE_P4], 1)
)

PROSE = (
    "--- Page 1 ---\nIndex of the agreement is below. The parties agree as follows.\n"
    "--- Page 2 ---\nThe term runs from 2019-2020 and renews annually.\n"
)
BALANCE_SHEET = "--- Page 1 ---\n" + "\n".join(
    f"{i}. Item {i} 12-15 4,500.00 3,200.00" for i in range(1, 30)
)


# ------------------------------------------------------------------ detection

def test_index_and_its_continuation_are_detected():
    assert index_pages(PAPER_BOOK) == {1, 2}


def test_documents_without_an_index_have_none():
    assert index_pages(PROSE) == set()
    assert index_pages(BALANCE_SHEET) == set()  # tabular, but no INDEX heading
    assert index_pages("no page markers at all\nINDEX\n1. A 3-4\n2. B 5-6") == set()


def test_an_index_heading_without_page_references_is_not_an_index():
    text = "--- Page 1 ---\nINDEX\nThis report covers the consumer price movement.\n"
    assert index_pages(text) == set()


def test_dates_are_not_page_ranges():
    page = "--- Page 1 ---\nINDEX\n1. Notice 12-03-2024\n2. Reply 20-04-2024\n"
    assert index_pages(page) == set()


# ------------------------------------------------------------------ relevant parties

def test_party_cites_the_page_with_its_details_not_the_index():
    party = {"contactNumber": "9876543210", "address": "House No. 12, Lake View Road, Pune 411001"}
    skip = index_pages(PAPER_BOOK)
    assert details_page(PAPER_BOOK, party, skip=skip) == 3


def test_party_without_details_cites_its_first_non_index_mention():
    skip = index_pages(PAPER_BOOK)
    assert first_mention_page(PAPER_BOOK, ["Arun Mehta"]) == 1  # the old answer
    assert first_mention_page(PAPER_BOOK, ["Arun Mehta"], skip=skip) == 3
    assert details_page(PAPER_BOOK, {}, skip=skip) is None


def test_party_named_only_in_the_index_gets_no_page():
    skip = index_pages(PAPER_BOOK)
    assert first_mention_page(PAPER_BOOK, ["Ors"], skip=skip) is None


# ------------------------------------------------------------------ shared attribution

def _attributor(with_index_pages: bool) -> Attributor:
    chunks = [
        {
            "content": f"--- Page {n} ---\n{t}",
            "original_document_id": "doc1",
            "document_id": f"doc1_chunk_{n}",
            "metadata": {"page_start": n, "page_end": n},
        }
        for n, t in enumerate([INDEX_P1, INDEX_P2, MEMO_P3, NOTICE_P4], 1)
    ]
    pages = {"doc1": index_pages(PAPER_BOOK)} if with_index_pages else None
    return Attributor(chunks, {"doc1": 1}, {"doc1": "paper-book.pdf"}, pages)


def test_dated_entry_cites_the_annexure_page_not_the_index():
    # A chronology title as panels write it: the index row matches it as well
    # as the notice itself does, which is what made the index win.
    cite = _attributor(True).locate(
        "Notice dated", document_id="doc1", anchor="12.03.2024"
    )
    assert cite and cite["page"] == 4


def test_without_index_information_the_index_won_the_tie():
    # The behaviour this ticket fixes: same match, earlier passage wins.
    cite = _attributor(False).locate(
        "Notice dated", document_id="doc1", anchor="12.03.2024"
    )
    assert cite and cite["page"] == 1


def test_item_found_only_in_the_index_still_gets_cited():
    cite = _attributor(True).locate("Reply to the notice", document_id="doc1", anchor="20.04.2024")
    assert cite and cite["page"] == 2
