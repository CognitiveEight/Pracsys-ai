"""Shared knowledge base: the kb_* events and chat use of the shelf.

Covers the shelf's bookkeeping (add / replace / erase / full-replace sync, and
erase winning a race with an ingest in flight), the socket handlers, and the
[K#] evidence label and its citation. The vector store, download and
extraction are fakes. Fictional data only; no model, Qdrant or socket calls.
"""

import asyncio
import re

import pytest

from app.core.config import settings
from app.rag import knowledge_base as kb
from app.rag.citations import verify_and_number
from app.rag.evidence import EvidencePack
from app.socket.handlers.knowledge_base import KnowledgeBaseMixin
from app.socket.schemas import Citation

DOC_A = "6720c0ffee0000000000a001"
DOC_B = "6720c0ffee0000000000a002"
DOC_C = "6720c0ffee0000000000a003"


def _doc(doc_id, **extra):
    doc = {
        "documentId": doc_id,
        "title": f"Fictional reference {doc_id[-3:]}",
        "category": "judgment",
        "fileUrl": f"https://storage.example.test/kb/{doc_id}?sig=fictional",
        "mimeType": "application/pdf",
        "sizeBytes": 1024,
        "uploadedAt": "2026-11-04T09:15:00.000Z",
    }
    doc.update(extra)
    return doc


class _FakeStore:
    """The evidence collection, reduced to what the shelf uses."""

    def __init__(self):
        self.chunks = {}  # (workspace_id, document_id) -> (text, metadata)
        self.deletes = []
        self.fail_deletes = 0
        self.write_gate = None

    async def list_document_ids(self, workspace_id):
        return {d for (w, d) in self.chunks if w == workspace_id}

    async def add_document_chunks(self, workspace_id, document_id, text, metadata=None, row_index=None):
        if self.write_gate is not None:
            await self.write_gate.wait()
        self.chunks[(workspace_id, document_id)] = (text, dict(metadata or {}))
        return 2

    async def delete_documents(self, workspace_id, document_ids):
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise RuntimeError("store unavailable")
        self.deletes.append((workspace_id, sorted(document_ids)))
        for d in document_ids:
            self.chunks.pop((workspace_id, d), None)
        return True


@pytest.fixture
def env(monkeypatch):
    store = _FakeStore()
    fetched = []
    gates = {}

    async def download(url, name):
        fetched.append(name)
        gate = gates.get(name)
        if gate is not None:
            await gate.wait()
        return f"FICTIONAL TEXT OF {name}".encode()

    def extract(content, name):
        return content.decode(), None

    monkeypatch.setattr(kb, "_store", lambda: store)
    monkeypatch.setattr(kb, "_download", download)
    monkeypatch.setattr(kb, "_extract_sync", extract)
    monkeypatch.setattr(kb, "_ERASE_BACKOFF_SECONDS", 0.0)
    from app.observability.audit import audit_logger

    # No real audit records from tests; a test that checks them fakes its own.
    monkeypatch.setattr(audit_logger, "log_event", lambda *a, **k: None)
    shelf = kb.KnowledgeBase()
    tasks = []

    def spawn(work, label):
        task = asyncio.ensure_future(work)
        tasks.append(task)
        return task

    async def settle():
        await asyncio.gather(*tasks, return_exceptions=True)

    return {
        "store": store,
        "shelf": shelf,
        "spawn": spawn,
        "settle": settle,
        "fetched": fetched,
        "gates": gates,
    }


def _held(store):
    return {d for (w, d) in store.chunks if w == kb.KB_WORKSPACE_ID}


# ================================================================ the shelf


def test_the_shelf_is_never_a_case():
    assert not re.fullmatch(r"[0-9a-f]{24}", kb.KB_WORKSPACE_ID)
    assert kb.routing_name(DOC_A, "application/pdf") == f"kb_{DOC_A}.pdf"
    assert kb.routing_name(DOC_A, "application/x-unknown") == f"kb_{DOC_A}"


async def test_added_document_is_indexed_under_its_id_without_the_link(env):
    shelf, store = env["shelf"], env["store"]
    assert await shelf.add(_doc(DOC_A, description="Fictional note"), request_id="kba_1", spawn=env["spawn"])
    await env["settle"]()

    text, meta = store.chunks[(kb.KB_WORKSPACE_ID, DOC_A)]
    assert DOC_A in text
    assert meta["knowledge_base"] is True
    assert meta["title"] == "Fictional reference 001"
    assert meta["category"] == "judgment"
    assert meta["description"] == "Fictional note"
    assert "fileUrl" not in meta and not any("sig=" in str(v) for v in meta.values())
    # Never under a case's workspace id.
    assert {w for (w, _d) in store.chunks} == {kb.KB_WORKSPACE_ID}


async def test_resend_is_ignored_and_same_id_replaces(env):
    shelf, store = env["shelf"], env["store"]
    await shelf.add(_doc(DOC_A), request_id="kba_1", spawn=env["spawn"])
    await env["settle"]()
    assert not await shelf.add(_doc(DOC_A), request_id="kba_1", spawn=env["spawn"])
    assert len(env["fetched"]) == 1

    await shelf.add(_doc(DOC_A, category="statute"), request_id="kba_2", spawn=env["spawn"])
    await env["settle"]()
    assert len(env["fetched"]) == 2
    assert _held(store) == {DOC_A}
    assert store.chunks[(kb.KB_WORKSPACE_ID, DOC_A)][1]["category"] == "statute"


async def test_unknown_category_is_filed_as_other(env):
    await env["shelf"].add(_doc(DOC_A, category="memo"), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    assert env["store"].chunks[(kb.KB_WORKSPACE_ID, DOC_A)][1]["category"] == "other"


async def test_deleted_document_is_erased_and_unknown_id_is_a_no_op(env):
    shelf, store = env["shelf"], env["store"]
    await shelf.add(_doc(DOC_A), request_id="kba_1", spawn=env["spawn"])
    await env["settle"]()

    assert await shelf.erase([DOC_A]) == 1
    assert _held(store) == set()
    assert store.deletes[-1] == (kb.KB_WORKSPACE_ID, [DOC_A])
    assert await shelf.erase([DOC_B]) == 1  # nothing held: still safe
    assert await shelf.erase([]) == 0


async def test_erase_during_fetch_stops_the_ingest(env):
    shelf, store = env["shelf"], env["store"]
    env["gates"][kb.routing_name(DOC_A, "application/pdf")] = asyncio.Event()
    await shelf.add(_doc(DOC_A), request_id="kba_1", spawn=env["spawn"])
    await asyncio.sleep(0)

    await shelf.erase([DOC_A])
    await env["settle"]()
    assert _held(store) == set()


async def test_erase_during_write_waits_then_erases(env):
    shelf, store = env["shelf"], env["store"]
    store.write_gate = asyncio.Event()
    await shelf.add(_doc(DOC_A), request_id="kba_1", spawn=env["spawn"])
    for _ in range(5):
        await asyncio.sleep(0)

    eraser = asyncio.ensure_future(shelf.erase([DOC_A]))
    await asyncio.sleep(0)
    assert not eraser.done()  # waits for the write it cannot interrupt
    store.write_gate.set()
    await eraser
    await env["settle"]()
    assert _held(store) == set()


async def test_failed_erase_retries_then_escalates(env):
    shelf, store = env["shelf"], env["store"]
    store.fail_deletes = 1
    await shelf.erase([DOC_A])  # second attempt succeeds
    assert store.deletes == [(kb.KB_WORKSPACE_ID, [DOC_A])]

    store.fail_deletes = kb._ERASE_ATTEMPTS
    with pytest.raises(RuntimeError):
        await shelf.erase([DOC_B])
    assert shelf._held is None  # the next sync re-reads the store


async def test_sync_is_a_full_replace(env):
    shelf, store = env["shelf"], env["store"]
    for d in (DOC_A, DOC_B):
        await shelf.add(_doc(d), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    env["fetched"].clear()

    counts = await shelf.sync([_doc(DOC_B), _doc(DOC_C)], spawn=env["spawn"])
    await env["settle"]()
    assert counts == {"listed": 2, "held": 2, "erased": 1, "ingesting": 1}
    assert _held(store) == {DOC_B, DOC_C}
    assert env["fetched"] == [kb.routing_name(DOC_C, "application/pdf")]  # B not re-fetched


async def test_empty_sync_erases_everything_including_an_ingest_in_flight(env):
    shelf, store = env["shelf"], env["store"]
    await shelf.add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    env["gates"][kb.routing_name(DOC_B, "application/pdf")] = asyncio.Event()
    await shelf.add(_doc(DOC_B), request_id=None, spawn=env["spawn"])
    await asyncio.sleep(0)

    counts = await shelf.sync([], spawn=env["spawn"])
    await env["settle"]()
    assert counts["erased"] == 2
    assert _held(store) == set()


async def test_sync_keeps_a_held_document_whose_item_was_invalid(env):
    shelf, store = env["shelf"], env["store"]
    await shelf.add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    await shelf.sync([], spawn=env["spawn"], keep=[DOC_A])
    assert _held(store) == {DOC_A}


async def test_a_failed_fetch_leaves_nothing_and_does_not_raise(env, monkeypatch):
    async def expired(url, name):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(kb, "_download", expired)
    await env["shelf"].add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    assert _held(env["store"]) == set()


def test_extraction_messages_are_unreadable():
    assert kb.is_unreadable("")
    assert kb.is_unreadable("[Error extracting text from PDF: broken]")
    assert kb.is_unreadable("[PDF content - PDF processing not available]")
    assert not kb.is_unreadable("--- Page 1 ---\nFictional judgment text.")


def test_text_is_redacted_before_it_is_stored():
    text, rows = kb._extract_sync(b"Fictional order. PAN ABCDE1234F on record.", f"kb_{DOC_A}.txt")
    assert "ABCDE1234F" not in text and "Fictional order" in text and rows is None
    with pytest.raises(kb.KnowledgeBaseUnreadable):
        kb._extract_sync(b"   ", f"kb_{DOC_A}.txt")


# ================================================================ handlers


class _Handler(KnowledgeBaseMixin):
    def _track_background_task(self, task, *, label=""):
        self.spawned.append(task)
        return task


@pytest.fixture
def handler(env, monkeypatch):
    import app.socket.handlers.knowledge_base as module

    monkeypatch.setattr(module, "knowledge_base", env["shelf"])
    h = _Handler()
    h.spawned = []
    return h


async def test_handlers_add_delete_and_sync(env, handler):
    await handler.on_kb_document_added({"event": "kb_document_added", "requestId": "kba_1", **_doc(DOC_A)})
    await asyncio.gather(*handler.spawned)
    assert _held(env["store"]) == {DOC_A}

    await handler.on_kb_document_deleted({"documentId": DOC_A, "requestId": "kbd_1"})
    assert _held(env["store"]) == set()

    await handler.on_kb_sync({"documents": [_doc(DOC_B)], "requestId": "kbs_1"})
    await asyncio.gather(*handler.spawned)
    assert _held(env["store"]) == {DOC_B}


async def test_invalid_payloads_change_nothing(env, handler):
    await handler.on_kb_sync({"documents": [_doc(DOC_A)]})
    await asyncio.gather(*handler.spawned)

    await handler.on_kb_document_added({"documentId": DOC_B})  # no fileUrl
    await handler.on_kb_document_deleted({"requestId": "kbd_2"})  # no id
    await handler.on_kb_sync({"syncedAt": "2026-11-06T08:00:00.000Z"})  # no list
    assert _held(env["store"]) == {DOC_A}

    # A listed item without a link is not fetched, and a held copy is kept.
    await handler.on_kb_sync({"documents": [{"documentId": DOC_A, "title": "x"}]})
    assert _held(env["store"]) == {DOC_A}


async def test_handlers_are_registered():
    from app.socket.events import event_handler

    for name in ("kb_document_added", "kb_document_deleted", "kb_sync"):
        assert name in event_handler._handlers


# ================================================================ chat


def _kb_chunk(doc_id, text, *, page=3):
    return {
        "document_id": f"{doc_id}_chunk_0",
        "original_document_id": doc_id,
        "content": text,
        "metadata": {
            "knowledge_base": True,
            "title": "Fictional v. Example (2020)",
            "category": "judgment",
            "page_start": page,
            "page_end": page,
        },
    }


async def test_chat_reads_the_shelf_only_when_it_holds_something(env, monkeypatch):
    calls = []

    async def retrieve(workspace_id, question, **kwargs):
        calls.append(workspace_id)
        return [
            {**_kb_chunk(DOC_A, "Relevant fictional passage."), "rerank_score": 8.0},
            {**_kb_chunk(DOC_B, "Weak fictional passage."), "rerank_score": 5.0},
            _kb_chunk(DOC_C, "Unscored fictional passage."),
        ]

    import app.rag.retriever as retriever

    monkeypatch.setattr(retriever, "retrieve_passages", retrieve)
    shelf = env["shelf"]
    assert await shelf.passages_for_chat("What are the maintenance guidelines?") == []
    assert calls == []  # empty shelf: no search, no model call

    await shelf.add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    hits = await shelf.passages_for_chat("What are the maintenance guidelines?")
    assert calls == [kb.KB_WORKSPACE_ID]
    assert [h["original_document_id"] for h in hits] == [DOC_A]

    monkeypatch.setattr(settings, "knowledge_base_chat_enabled", False)
    assert await shelf.passages_for_chat("What are the maintenance guidelines?") == []


def test_knowledge_base_citation_names_the_title_not_an_annexure():
    pack = EvidencePack()
    case = pack.add_passage(
        {
            "document_id": "case_chunk_0",
            "original_document_id": "case-doc",
            "content": "The fictional notice was served on 05/04/2024.",
            "metadata": {"page_start": 1},
        },
        annexure=1,
        file_name="Fictional notice",
    )
    shelf_item = pack.add_knowledge_base(
        _kb_chunk(DOC_A, "Interim maintenance must be decided within 60 days of filing.")
    )
    assert (case.label, shelf_item.label) == ("E1", "K1")

    answer = (
        "The notice was served on 05/04/2024 [E1]. "
        "The guidelines set a 60 days limit for interim maintenance [K1]."
    )
    text, citations, report = verify_and_number(answer, EvidencePack.from_state(pack.to_state()))
    assert report.clean  # the figure is found in the shelf passage it cites
    assert "[2]" in text
    kb_cite = citations[1]
    assert kb_cite["type"] == "knowledge_base"
    assert kb_cite["title"] == "Fictional v. Example (2020)"
    assert kb_cite["documentId"] == DOC_A and kb_cite["page"] == 3
    assert "annexure" not in kb_cite and "fileName" not in kb_cite
    assert Citation(**kb_cite).type == "knowledge_base"


def test_chat_prompt_shows_the_shelf_apart_from_the_case():
    from app.langgraph.chat_nodes import chat_context_builder_node

    state = {
        "chat_context": {
            "role_context": {"role": "PETITIONER"},
            "workspace_state": {
                "knowledge_base_passages": [_kb_chunk(DOC_A, "Fictional guideline text.")],
            },
        }
    }
    out = chat_context_builder_node(state)
    prompt = out["prompt_context"]
    assert "KNOWLEDGE BASE — SHARED LEGAL REFERENCE LIBRARY" in prompt
    assert "[K1] Fictional v. Example (2020) (judgment)" in prompt
    assert "NOT part of this case's documents" in prompt
    assert out["evidence_index"]["K1"]["kind"] == "knowledge_base"


# ================================================================ panels


def test_case_query_describes_the_case_without_its_text_when_it_can():
    docs = [
        {
            "content": "FICTIONAL BODY THAT SHOULD NOT BE NEEDED",
            "metadata": {
                "document_type": "Legal Notice",
                "extracted_fields": {
                    "subject": "Demand for unpaid fictional invoice",
                    "legal_provisions": ["Section 138, Negotiable Instruments Act"],
                },
            },
        },
        {"content": "Fictional agreement text about delivery terms.", "metadata": {}},
    ]
    query = kb.case_query(docs)
    lines = query.split("\n")
    assert "Legal Notice; Demand for unpaid fictional invoice; Section 138" in lines[0]
    assert "FICTIONAL BODY" not in query
    assert lines[1].startswith("Fictional agreement text")  # no metadata: opening text
    assert "Objective" not in query  # documents only: identical for every panel
    assert kb.case_query([]) == ""


def test_panel_block_is_marked_as_not_the_case_and_respects_its_budget():
    block = kb.render_panel_block(
        [_kb_chunk(DOC_A, "Fictional principle on cheque dishonour."), _kb_chunk(DOC_B, "x" * 3000)],
        max_chars=600,
    )
    assert "NOT this case's documents" in block
    assert "Never take facts, parties, dates or amounts from them" in block
    assert "[Reference 1] Fictional v. Example (2020) (judgment)" in block
    assert "[Reference 2]" not in block  # over budget: left out, not cut
    assert kb.render_panel_block([], max_chars=600) == ""


async def test_case_panels_share_one_search_and_an_erase_invalidates_it(env, monkeypatch):
    calls = []

    async def retrieve(workspace_id, question, **kwargs):
        calls.append(workspace_id)
        return [
            {**_kb_chunk(DOC_A, "Relevant fictional principle."), "rerank_score": 7.0},
            {**_kb_chunk(DOC_B, "Barely related fictional text."), "rerank_score": 3.0},
        ]

    import app.rag.retriever as retriever

    monkeypatch.setattr(retriever, "retrieve_passages", retrieve)
    shelf = env["shelf"]
    assert await shelf.passages_for_case("case-1", "Cheque dishonour notice") == []
    assert calls == []  # empty shelf: no search

    await shelf.add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    first = await shelf.passages_for_case("case-1", "Cheque dishonour notice")
    again = await shelf.passages_for_case("case-1", "Cheque dishonour notice")
    assert [h["original_document_id"] for h in first] == [DOC_A] and again == first
    assert calls == [kb.KB_WORKSPACE_ID]  # the run's other panels reuse it

    await shelf.erase([DOC_B])
    await shelf.passages_for_case("case-1", "Cheque dishonour notice")
    assert len(calls) == 2  # the shelf changed: searched again


class _PanelHost:
    from app.socket.handlers._shared import JsonHelpersMixin as _Mixin

    _knowledge_base_block = _Mixin._knowledge_base_block

    def _current_strategic_objective(self, workspace_id):
        return "Recover the fictional dues"


async def test_only_the_configured_panels_read_the_shelf(env, monkeypatch):
    seen = []

    async def passages_for_case(workspace_id, query):
        seen.append(query)
        return [_kb_chunk(DOC_A, "Fictional principle.")]

    monkeypatch.setattr(kb.knowledge_base, "passages_for_case", passages_for_case)
    host = _PanelHost()
    docs = [{"content": "Fictional notice text.", "metadata": {"document_type": "Legal Notice"}}]

    for panel in ("strategy_objective", "analysis_report", "applicable_laws"):
        assert "SHARED LEGAL REFERENCE LIBRARY" in await host._knowledge_base_block(
            panel, "case-1", docs=docs
        )
    assert seen[0] == "Legal Notice"
    for panel in ("list_of_dates", "procedural_timeline", "relevant_parties"):
        assert await host._knowledge_base_block(panel, "case-1", docs=docs) == ""

    monkeypatch.setattr(settings, "knowledge_base_panels", "")
    assert await host._knowledge_base_block("strategy_objective", "case-1", docs=docs) == ""


async def test_a_shelf_failure_never_blocks_a_panel(monkeypatch):
    async def broken(workspace_id, query):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(kb.knowledge_base, "passages_for_case", broken)
    docs = [{"content": "Fictional notice text.", "metadata": {}}]
    assert await _PanelHost()._knowledge_base_block("analysis_report", "case-1", docs=docs) == ""


def test_chat_prompt_asks_for_the_title_in_the_answer_text():
    from app.langgraph.chat_nodes import chat_context_builder_node

    out = chat_context_builder_node(
        {
            "chat_context": {
                "role_context": {"role": "PETITIONER"},
                "workspace_state": {
                    "knowledge_base_passages": [_kb_chunk(DOC_A, "Fictional guideline text.")]
                },
            }
        }
    )
    # The lawyer app has no citation UI: the title in the text is the source.
    assert "write its title in the sentence itself" in out["prompt_context"]


# ================================================================ added in verification


async def test_a_failed_erase_escalates_as_critical_and_audited(env, monkeypatch):
    """AC-6: after the last attempt the erase is escalated, not just raised."""
    from loguru import logger

    from app.observability.audit import audit_logger

    audited = []
    monkeypatch.setattr(
        audit_logger, "log_event", lambda event, details=None, *a, **k: audited.append((event, details))
    )
    critical = []
    sink = logger.add(lambda m: critical.append(m), level="CRITICAL")
    try:
        env["store"].fail_deletes = kb._ERASE_ATTEMPTS
        with pytest.raises(RuntimeError):
            await env["shelf"].erase([DOC_A])
    finally:
        logger.remove(sink)
    assert len(critical) == 1 and "FAILED" in critical[0]
    assert [e for e, _d in audited] == ["knowledge_base_erase_failed"]
    assert audited[0][1]["document_ids"] == [DOC_A]
    assert env["store"].deletes == []  # every attempt failed


async def test_chat_relevance_floor_is_six_inclusive(env, monkeypatch):
    """AC-10: a passage scoring exactly 6 is used; just under 6 is not."""

    async def retrieve(workspace_id, question, **kwargs):
        return [
            {**_kb_chunk(DOC_A, "Fictional passage at the floor."), "rerank_score": 6.0},
            {**_kb_chunk(DOC_B, "Fictional passage just under it."), "rerank_score": 5.99},
        ]

    import app.rag.retriever as retriever

    monkeypatch.setattr(retriever, "retrieve_passages", retrieve)
    assert settings.knowledge_base_min_relevance == 6.0
    await env["shelf"].add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()
    hits = await env["shelf"].passages_for_chat("What is the fictional limit?")
    assert [h["original_document_id"] for h in hits] == [DOC_A]


async def test_socket_client_routes_the_three_kb_events_to_the_handler(monkeypatch):
    """AC-13: the socket client listens for the three events and dispatches them."""
    import app.socket.client as client_module

    class _FakeSio:
        def __init__(self):
            self.handlers = {}

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def on(self, name, handler=None, **kwargs):
            if handler is not None:
                self.handlers[name] = handler
                return handler

            def deco(fn):
                self.handlers[name] = fn
                return fn

            return deco

    dispatched = []

    async def handle(event, data):
        dispatched.append((event, data))
        return True

    monkeypatch.setattr(client_module.event_handler, "handle", handle)
    client = client_module.SocketClient()
    client.sio = _FakeSio()
    client._register_handlers()
    for name in ("kb_document_added", "kb_document_deleted", "kb_sync"):
        assert name in client.sio.handlers
        await client.sio.handlers[name]({"requestId": f"req_{name}"})
    assert [e for e, _d in dispatched] == ["kb_document_added", "kb_document_deleted", "kb_sync"]


async def test_one_upload_run_searches_the_shelf_once_for_all_three_panels(env, monkeypatch):
    """AC-16: Strategy, Analysis Report and Applicable Laws, built for one case
    in one run, are served by ONE shelf search.

    Each call below passes exactly what its call site passes: all of the
    case's documents (``get_all_documents``), unfiltered, in strategy.py,
    analysis_report.py and applicable_laws.py. The objective written between
    panels on a first run must not split the search.
    """
    calls = []

    async def retrieve(workspace_id, question, **kwargs):
        calls.append(question)
        return [{**_kb_chunk(DOC_A, "Fictional principle on notice periods."), "rerank_score": 7.0}]

    import app.rag.retriever as retriever

    monkeypatch.setattr(retriever, "retrieve_passages", retrieve)
    monkeypatch.setattr(kb, "knowledge_base", env["shelf"])
    await env["shelf"].add(_doc(DOC_A), request_id=None, spawn=env["spawn"])
    await env["settle"]()

    docs = [
        {
            "content": "Fictional legal notice demanding payment of a fictional invoice.",
            "metadata": {"document_type": "Legal Notice"},
        }
    ]
    host = _PanelHost()
    for panel in ("strategy_objective", "analysis_report", "applicable_laws"):
        assert "SHARED LEGAL REFERENCE LIBRARY" in await host._knowledge_base_block(
            panel, "case-1", docs=docs
        )
    assert len(calls) == 1, f"{len(calls)} shelf searches for one run"
