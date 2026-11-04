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


# ------------------------------------------------------------------ dates and timeline fallback

def test_anchor_page_skips_the_index_restating_the_date():
    from app.rag.page_kinds import anchor_page

    assert anchor_page(PAPER_BOOK, ["12.03.2024"]) == 4


def test_anchor_page_uses_the_index_only_as_a_last_resort():
    from app.rag.page_kinds import anchor_page

    assert anchor_page(PAPER_BOOK, ["20.04.2024"]) == 2  # only the index has it


def test_anchor_page_never_guesses():
    from app.rag.page_kinds import anchor_page

    assert anchor_page(PAPER_BOOK, ["01.01.1999"]) is None
    assert anchor_page("no markers 12.03.2024", ["12.03.2024"]) is None


def test_timeline_ignores_a_model_page_that_is_the_index():
    from app.langgraph.procedural_timeline_agent import ProceduralTimelineAgent

    resolve = ProceduralTimelineAgent._resolve_page_number
    item = {"title": "Legal notice issued", "date": "12.03.2024", "pageNumber": 1}
    assert resolve(item, PAPER_BOOK) == 4
    assert resolve({**item, "pageNumber": 4}, PAPER_BOOK) == 4  # a checked page is kept
    # A page the date is not on is corrected, not trusted.
    assert resolve({**item, "pageNumber": 3}, PAPER_BOOK) == 4


def test_timeline_unplaceable_entry_gets_no_page_not_page_one():
    from app.langgraph.procedural_timeline_agent import ProceduralTimelineAgent

    item = {"title": "Hearing adjourned", "date": "01.01.1999"}
    assert ProceduralTimelineAgent._resolve_page_number(item, PAPER_BOOK) == 0


# ------------------------------------------------------------------ chat citations

def _chat_pack():
    from app.rag.evidence import EvidencePack

    pack = EvidencePack()
    for n, text in enumerate([INDEX_P1, INDEX_P2, MEMO_P3, NOTICE_P4], 1):
        item = pack.add_passage(
            {
                "content": f"--- Page {n} ---\n{text}",
                "original_document_id": "doc1",
                "document_id": f"doc1_chunk_{n}",
                "metadata": {"page_start": n, "page_end": n},
            },
            annexure=1,
            file_name="paper-book.pdf",
        )
        item.index_pages = (1, 2)
    return pack


def test_chat_citation_of_the_index_moves_to_the_page_with_the_date():
    from app.rag.citations import verify_and_number

    _, cites, report = verify_and_number("The notice is dated 12.03.2024 [E1].", _chat_pack())
    assert [c["page"] for c in cites] == [4]
    assert report.repointed


def test_chat_citation_of_the_index_moves_to_the_memo_of_parties():
    from app.rag.citations import verify_and_number

    _, cites, _ = verify_and_number(
        "Arun Mehta of Lake View Road, Kothrud is Petitioner No. 1 [E1].", _chat_pack()
    )
    assert [c["page"] for c in cites] == [3]


def test_chat_uncited_date_is_attached_to_the_real_page_not_the_index():
    from app.rag.citations import verify_and_number

    _, cites, _ = verify_and_number("A notice was issued on 12.03.2024.", _chat_pack())
    assert [c["page"] for c in cites] == [4]


def test_chat_index_only_fact_keeps_its_index_citation():
    from app.rag.citations import verify_and_number

    _, cites, _ = verify_and_number("A reply was filed on 20.04.2024 [E2].", _chat_pack())
    assert [c["page"] for c in cites] == [2]


def test_chat_marks_index_passages_for_the_model():
    from app.rag.evidence import location_label

    pack = _chat_pack()
    assert "paper-book index" in location_label(pack.items["E1"])
    assert "paper-book index" not in location_label(pack.items["E3"])


def test_chat_retrieval_ranks_index_chunks_last():
    from app.rag.chat_context import _index_pages_by_doc, _on_index

    chunks = [
        {
            "content": f"--- Page {n} ---\n{t}",
            "original_document_id": "doc1",
            "metadata": {"page_start": n, "page_end": n, "chunk_index": n, "overlap_prev": 0},
        }
        for n, t in enumerate([INDEX_P1, INDEX_P2, MEMO_P3, NOTICE_P4], 1)
    ]
    index_of = _index_pages_by_doc(chunks)
    assert index_of == {"doc1": (1, 2)}
    ranked = sorted(chunks, key=lambda c: _on_index(c, index_of))
    assert [c["metadata"]["page_start"] for c in ranked] == [3, 4, 1, 2]


# ------------------------------------------------------------------ list of dates resolvers (AC-7)
# The two List of Dates resolvers are nested functions; they are driven
# through their callers with the model and the LLM post-passes faked.


async def test_list_of_dates_agent_page_skips_the_index_and_never_guesses():
    import json

    from app.langgraph.list_of_dates_agent import ListOfDatesTemporalAgent

    raw = json.dumps(
        [
            {  # the generator placed it on the index page
                "date": "2024-03-12",
                "title": "Legal notice issued",
                "description": "A legal notice was issued to the club.",
                "evidenceSnippet": "This notice dated 12.03.2024 is issued",
                "pageNumber": 1,
            },
            {  # its date is only in the index: the index page, as a last resort
                "date": "2024-04-20",
                "title": "Reply received",
                "description": "A reply to the notice was received.",
            },
            {  # nothing in the text places it
                "date": "2023-07-09",
                "title": "Annual meeting held",
                "description": "The club held its annual meeting.",
            },
        ]
    )
    out = await ListOfDatesTemporalAgent()._node_validate_and_normalize(
        {
            "raw_response": raw,
            "document_text": PAPER_BOOK,
            "source_doc_id": "doc1",
            "source_doc_name": "paper-book.pdf",
            "today_str": "2026-01-01",
        }
    )
    pages = {e["date"]: e.get("pageNumber") for e in out["events"]}
    assert pages == {"2024-03-12": 4, "2024-04-20": 2, "2023-07-09": None}


# The handler only grounds DD/MM/YYYY-style dates, so its paper-book uses them.
_SLASH_BOOK = "".join(
    f"--- Page {n} ---\n{t}"
    for n, t in enumerate(
        [
            INDEX_P1.replace("12.03.2024", "12/03/2024"),
            INDEX_P2.replace("20.04.2024", "20/04/2024"),
            MEMO_P3,
            NOTICE_P4.replace("12.03.2024", "12/03/2024"),
        ],
        1,
    )
)


async def test_list_of_dates_handler_page_skips_the_index(monkeypatch):
    import json

    from app.ai.gemini_client import gemini_client
    from app.langgraph import list_of_dates_agent
    from app.socket.events import event_handler as h

    assert index_pages(_SLASH_BOOK) == {1, 2}
    model_out = json.dumps(
        [
            {  # placed on the index by the generator; its title is on page 4
                "date": "2024-03-12",
                "title": "Legal Notice",
                "description": "A legal notice was issued to the club.",
                "sourceDocumentIndex": 1,
                "pageNumber": 1,
                "eventType": "Communication",
            },
            {  # no page from the generator; its date is only in the index
                "date": "2024-04-20",
                "title": "Reply received",
                "description": "A reply to the notice was received.",
                "sourceDocumentIndex": 1,
                "eventType": "Communication",
            },
        ]
    )

    async def _generate(*_a, **_kw):
        return model_out

    async def _none(*_a, **_kw):
        return []

    async def _same(entries, **_kw):
        return entries

    async def _no_graph(*_a, **_kw):
        return ""

    monkeypatch.setattr(gemini_client, "initialize", lambda: None)
    monkeypatch.setattr(gemini_client, "is_available", lambda: True)
    monkeypatch.setattr(gemini_client, "generate", _generate)
    monkeypatch.setattr(
        list_of_dates_agent.list_of_dates_temporal_agent, "infer_temporal_events", _none
    )
    monkeypatch.setattr(h, "_graph_evidence_block", _no_graph)
    monkeypatch.setattr(h, "_llm_classify_factual_vs_procedural", _same)
    monkeypatch.setattr(h, "_dedupe_dated_entries_semantically", _same)

    out = await h._generate_list_of_dates_from_documents(
        "ws-sim192-fictional",
        "user-fictional",
        [{"document_id": "doc1", "content": _SLASH_BOOK}],
    )
    pages = {e["date"]: e.get("pageNumber", "absent") for e in out}
    # Written date forms are searched (20/04/2024), so the index-only entry
    # gets the index page as a last resort rather than none.
    assert pages == {"2024-03-12": 4, "2024-04-20": 2}


# ------------------------------------------------------------------ chat evidence wiring (AC-9)


def _store_chunks():
    return [
        {
            "content": f"--- Page {n} ---\n{t}",
            "original_document_id": "doc1",
            "document_id": f"doc1_chunk_{n}",
            "metadata": {"page_start": n, "page_end": n, "chunk_index": n, "overlap_prev": 0},
        }
        for n, t in enumerate([INDEX_P1, INDEX_P2, MEMO_P3, NOTICE_P4], 1)
    ]


async def test_chat_evidence_marks_index_passages_and_drops_them_first(monkeypatch):
    from app.rag import chat_context, retriever
    from app.vectorstore.qdrant import vector_store

    chunks = _store_chunks()

    async def _get_chunks(_ws):
        return [dict(c) for c in chunks]

    async def _retrieve(*_a, **_kw):
        return [dict(c) for c in chunks]  # retrieval ranks the index first

    async def _no_tables(*_a, **_kw):
        return None

    monkeypatch.setattr(vector_store, "get_document_chunks", _get_chunks)
    monkeypatch.setattr(retriever, "retrieve_passages", _retrieve)
    monkeypatch.setattr(chat_context, "_add_table_calculations", _no_tables)
    common = dict(
        workspace_id="ws-sim192-fictional",
        question="When was the notice issued?",
        previous_user_turn="",
        annexure_of={"doc1": 1},
        names={"doc1": "paper-book.pdf"},
        forced_ids=[],
    )

    # Whole-document mode: the index passages are marked for the model.
    pack, mode, _ = await chat_context.build_chat_evidence(
        **common, max_total_chars=100_000, full_mode_max_docs=5
    )
    assert mode == "full"
    marked = sorted(i.page_start for i in pack.items.values() if i.is_index_only())
    assert marked == [1, 2]

    # Retrieval mode with room for two passages: the real pages win the budget.
    budget = len(chunks[2]["content"]) + len(chunks[3]["content"])
    pack, mode, _ = await chat_context.build_chat_evidence(
        **common, max_total_chars=budget, full_mode_max_docs=0
    )
    assert mode == "retrieval"
    assert sorted(i.page_start for i in pack.items.values()) == [3, 4]


def test_index_pages_cache_keeps_no_document_text():
    from app.rag import page_kinds

    index_pages(PAPER_BOOK)
    cached = list(page_kinds._CACHE.items())
    assert cached and all(len(k[1]) == 64 and "Arun" not in k[1] for k, _ in cached)
    assert all(isinstance(v, frozenset) for _, v in cached)


def test_date_forms_cover_how_documents_write_dates():
    from app.rag.page_kinds import date_forms

    forms = date_forms("2024-03-02")
    assert {"02.03.2024", "02/03/2024", "2.3.2024", "2024-03-02"} <= set(forms)
