#!/usr/bin/env python3
"""
verify_rag.py — checks for the retrieval pipeline (chunking, hybrid search,
re-indexing, deletion) and the model-client fixes.

The rules under test:

  * chunking keeps page numbers and rebuilds the document byte for byte;
  * sparse tokens keep legal identifiers (s.302, 498A, CR-2026-45871, amounts);
  * whole documents come back in reading order with their metadata — the old
    store sorted chunks alphabetically and dropped the metadata header;
  * re-ingesting a document replaces it (no duplicate or stale tail chunks);
  * an exact legal token is found by hybrid search even when the dense match
    is poor, and a failed embedding never becomes a searchable zero vector;
  * legacy workspaces are re-indexed in chunk order on first use;
  * deleting a document or a workspace clears BOTH collections;
  * MINIMAL thinking is downgraded on Gemini 3.8 Flash; embeddings of the
    wrong size or all zeros are rejected, not padded.

Needs a local Qdrant (docker-compose up qdrant). The script pins
QDRANT_URL to http://localhost:6333, uses two uniquely named temporary
collections and deletes them at the end. Embeddings are stubbed — no API key,
no network calls to Google.

USAGE
    python verify_rag.py
"""

import asyncio
import hashlib
import math
import os
import random
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
settings.qdrant_url = "http://localhost:6333"
settings.qdrant_api_key = None
settings.qdrant_collection_name = f"verify_rag_{RUN}"
settings.qdrant_chunks_collection = f"verify_rag_{RUN}_evidence"

from app.ai.gemini_client import GeminiClient, gemini_client  # noqa: E402
from app.rag.chunker import chunk_text, reassemble  # noqa: E402
from app.rag.sparse import tokens  # noqa: E402
from app.vectorstore.qdrant import QdrantStore  # noqa: E402
from qdrant_client.models import PointStruct  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


# --------------------------------------------------------------- stub embedder

DIM = 768
FAIL_EMBED = {"on": False}


def _vec(text: str):
    """Deterministic bag-of-words embedding: similar words → similar vectors."""
    v = [0.0] * DIM
    for tok in tokens(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    if not any(v):
        v[0] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


async def _embed_text(text, task_type="RETRIEVAL_DOCUMENT"):
    if FAIL_EMBED["on"]:
        raise RuntimeError("stub embedding outage")
    return _vec(text)


async def _embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
    if FAIL_EMBED["on"]:
        raise RuntimeError("stub embedding outage")
    return [_vec(t) for t in texts]


gemini_client.embed_text = _embed_text
gemini_client.embed_texts = _embed_texts
gemini_client.is_available = lambda: True


def _document(pages: int, seed: int, first_page: int = 1) -> str:
    rnd = random.Random(seed)
    out = []
    for n in range(first_page, first_page + pages):
        paras = "\n\n".join(
            f"{n}.{k} " + " ".join(rnd.choice(
                ["The", "petitioner", "respondent", "court", "paid", "amount",
                 "notice", "hearing", "filed", "evidence", "agreement"]) for _ in range(rnd.randint(20, 90)))
            + "."
            for k in range(1, rnd.randint(2, 6))
        )
        out.append(f"--- Page {n} ---\nPART {n} STATEMENT OF FACTS\n{paras}")
    return "\n\n".join(out)


# ------------------------------------------------------------------- checks

def check_chunker():
    print("\n[chunker]")
    ok_exact = ok_pages = True
    for seed in range(20):
        text = _document(random.Random(seed).randint(1, 9), seed)
        chunks = chunk_text(text)
        rebuilt = reassemble([{"content": c.text, "overlap_prev": c.overlap_prev} for c in chunks])
        ok_exact &= rebuilt == text
        for c in chunks:
            first = text.rfind("--- Page ", 0, c.char_start + c.overlap_prev + 1)
            ok_pages &= c.page_start is not None
    check("chunks rebuild the document byte for byte (20 random documents)", ok_exact)
    check("every chunk of a paginated document has a page number", ok_pages)
    c = chunk_text("--- Page 3 ---\nsingle page text here, long enough " * 3)
    check("page numbers are physical (a document starting on page 3 says 3)",
          c and c[0].page_start == 3)
    check("text without page markers gets no invented page", chunk_text("plain text " * 50)[0].page_start is None)
    check("empty text gives no chunks", chunk_text("") == [])
    toks = tokens("Under s.302 and Section 498A, FIR CR-2026-45871, paid ₹6,00,000")
    check("legal identifiers survive tokenisation",
          all(t in toks for t in ["s.302", "302", "498a", "cr-2026-45871", "45871", "600000"]),
          str(toks))


async def check_store(store: QdrantStore):
    print("\n[store]")
    ws = f"ws_{RUN}"
    doc_a = _document(6, 1)
    await store.add_document_chunks(ws, "docA", doc_a, {"filename": "FIR.pdf", "document_type": "fir", "uploaded_at": "2026-09-01T00:00:00Z"})
    docs = await store.get_all_documents(ws)
    check("the whole document comes back exactly, in reading order",
          len(docs) == 1 and docs[0]["content"] == doc_a.strip())
    check("document metadata is returned (prompt header no longer empty)",
          docs and docs[0]["metadata"].get("document_type") == "fir" and docs[0]["filename"] == "FIR.pdf")
    check("the page count is known", docs and docs[0]["page_count"] == 6)

    before = len(await store.get_document_chunks(ws, ["docA"]))
    await store.add_document_chunks(ws, "docA", doc_a, {"filename": "FIR.pdf"})
    after = len(await store.get_document_chunks(ws, ["docA"]))
    check("re-ingesting the same document does not duplicate chunks", before == after and before > 0)
    await store.add_document_chunks(ws, "docA", _document(2, 1), {"filename": "FIR.pdf"})
    shorter = len(await store.get_document_chunks(ws, ["docA"]))
    check("a shorter re-read leaves no stale tail chunks", 0 < shorter < before)

    needle = ("--- Page 4 ---\nThe complainant alleges cruelty punishable under "
              "Section 498A in FIR CR-2026-45871.\n\n")
    await store.add_document_chunks(ws, "docB", _document(3, 7) + "\n\n" + needle + _document(2, 8, first_page=5), {"filename": "Complaint.pdf"})
    hits = await store.search(ws, "498A", limit=3)
    top = hits[0] if hits else {}
    check("an exact legal token is the top hybrid hit",
          top.get("original_document_id") == "docB" and "498A" in (top.get("content") or ""),
          str([(h.get("original_document_id"), (h.get("content") or "")[:40]) for h in hits]))
    meta = top.get("metadata") or {}
    check("the hit's page range covers the page the text is on",
          isinstance(meta.get("page_start"), int)
          and meta["page_start"] <= 4 <= (meta.get("page_end") or 0),
          str(meta))

    FAIL_EMBED["on"] = True
    try:
        await store.add_document_chunks(ws, "docC", "--- Page 1 ---\nUnique token ZXQ-9911 appears in this sworn affidavit text. " * 5, {"filename": "Affidavit.pdf"})
    finally:
        FAIL_EMBED["on"] = False
    stored_c = await store.get_document_chunks(ws, ["docC"])
    check("a failed embedding keeps the chunk, flagged",
          stored_c and all((p.get("metadata") or {}).get("embedding_failed") for p in stored_c))
    hits_c = await store.search(ws, "ZXQ-9911", limit=3)
    check("…and it is still found by its exact token",
          any(h.get("original_document_id") == "docC" for h in hits_c))
    docs = await store.get_all_documents(ws)
    check("…and whole-document reads still include it",
          any(d["document_id"] == "docC" for d in docs))

    await store.delete_documents(ws, ["docB"])
    check("deleting a document removes its chunks",
          not await store.get_document_chunks(ws, ["docB"]))
    return ws


async def check_legacy_migration(store: QdrantStore):
    print("\n[re-index]")
    ws = f"legacy_{RUN}"
    words = [f"w{i}" for i in range(2500)]
    parts = [" ".join(words[i:i + 1000]) for i in range(0, len(words), 1000)]
    # Chunk texts that sort alphabetically in the WRONG order (the old bug).
    parts = [f"zzz {parts[0]}", f"mmm {parts[1]}", f"aaa {parts[2]}"]
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=_vec(t),
            payload={"workspace_id": ws, "document_id": f"old_chunk_{i}", "content": t,
                     "original_document_id": "old", "uploaded_at": "2026-01-01",
                     "metadata": {"chunk_index": i, "filename": "Old.pdf", "document_type": "notice"}},
        )
        for i, t in enumerate(parts)
    ]
    store.client.upsert(collection_name=store.collection_name, points=points)
    docs = await store.get_all_documents(ws)
    check("a legacy document is re-indexed on first use",
          len(docs) == 1 and docs[0]["document_id"] == "old")
    check("…in chunk order, not alphabetical order",
          docs and docs[0]["content"].startswith("zzz w0") and docs[0]["content"].rstrip().endswith("w2499"))
    check("…keeping its metadata",
          docs and docs[0]["metadata"].get("document_type") == "notice")
    await store.delete_workspace(ws)
    legacy_left = store.client.count(collection_name=store.collection_name,
                                     count_filter=store._search_filter_all_chunks(ws)).count
    new_left = store.client.count(collection_name=store.chunks_collection,
                                  count_filter=store._chunk_filter(ws)).count
    check("deleting a workspace clears BOTH collections", legacy_left == 0 and new_left == 0,
          f"legacy={legacy_left} evidence={new_left}")


async def check_model_client():
    print("\n[model client]")
    saved = settings.gemini_model
    try:
        settings.gemini_model = "gemini-3.8-flash"
        check("MINIMAL thinking becomes LOW on Gemini 3.8 Flash",
              GeminiClient._ng_thinking_level("MINIMAL").name == "LOW")
        settings.gemini_model = "gemini-3-flash-preview"
        check("MINIMAL is kept on models that accept it",
              GeminiClient._ng_thinking_level("MINIMAL").name == "MINIMAL")
    finally:
        settings.gemini_model = saved

    real = GeminiClient()

    class _Resp:
        def __init__(self, vecs):
            self.embeddings = [type("E", (), {"values": v})() for v in vecs]

    class _Models:
        def __init__(self, vecs):
            self.vecs = vecs

        async def embed_content(self, **kw):
            return _Resp(self.vecs)

    class _Client:
        def __init__(self, vecs):
            self.aio = type("A", (), {"models": _Models(vecs)})()

    settings.embedding_max_attempts = 1
    for label, vecs in [("wrong-size embedding is rejected", [[0.1] * 3072]),
                        ("all-zero embedding is rejected", [[0.0] * DIM])]:
        real._get_ng_client = lambda v=vecs: _Client(v)
        real._initialized = True
        try:
            await real.embed_text("x")
            check(label, False, "no error raised")
        except Exception as e:
            check(label, "EmbeddingError" in type(e).__name__, repr(e))
    real._get_ng_client = lambda: _Client([[3.0, 4.0] + [0.0] * (DIM - 2)])
    v = await real.embed_text("x")
    check("embeddings are unit length", abs(sum(x * x for x in v) - 1.0) < 1e-9)


def check_page_markers():
    print("\n[pdf pages]")
    from app.processing.file_handlers import _strip_repeated_pdf_boilerplate as strip
    pages = ["Body of page one is here.", "Body of page two.", "", "Body of page four."]
    marked = strip(pages, mark_pages=True)
    check("evidence text is marked with physical page numbers",
          "--- Page 1 ---" in marked and "--- Page 4 ---" in marked and "--- Page 3 ---" not in marked)
    check("callers that did not ask for markers get plain text", "--- Page" not in strip(pages))




# ------------------------------------------------------------ chat citations

GEN_CALLS = []
ANSWERS = []  # queued chat answers for the stubbed generate


async def _stub_generate(prompt, system_instruction=None, temperature=0.7, max_tokens=0, **kw):
    GEN_CALLS.append(prompt)
    if "Rewrite a lawyer's question" in prompt:
        return '{"queries": ["cheque dishonour amount"], "exact_terms": ["004512"]}'
    if "Score how useful each passage" in prompt:
        n = prompt.count("\n[") + 1
        return '{"scores": [' + ",".join('{"i": %d, "s": 8}' % i for i in range(1, n + 1)) + ']}'
    return ANSWERS.pop(0) if ANSWERS else "No answer queued."


def check_citation_checker():
    print("\n[citation checker]")
    from app.rag.citations import verify_and_number
    from app.rag.evidence import EvidencePack

    pack = EvidencePack()
    pack.add_passage({"content": "--- Page 3 ---\nThe complainant paid Rs. 6,00,000 on 12/03/2024.\n--- Page 4 ---\nCheque no. 004512 was dishonoured on 20/03/2024.",
                      "original_document_id": "d1", "document_id": "d1_chunk_0",
                      "metadata": {"page_start": 3, "page_end": 4}}, annexure=1, file_name="FIR.pdf")
    pack.add_passage({"content": "--- Page 1 ---\nLegal notice dated 05/04/2024 demanding ₹6,00,000.",
                      "original_document_id": "d2", "document_id": "d2_chunk_0",
                      "metadata": {"page_start": 1, "page_end": 1}}, annexure=2, file_name="Notice.pdf")
    pack.add_case_law({"title": "A v. B", "citation": "(2014) 9 SCC 129", "sourceUrl": "https://indiankanoon.org/doc/1/"})
    text, cites, rep = verify_and_number(
        "The cheque no. 004512 was dishonoured on 20/03/2024 [E2]. Notice was sent on 05/04/2024 [E2]. "
        "Territorial jurisdiction follows the drawee bank [C1][E7]. The claim is 9,99,999 [E1].", pack)
    by_index = {c["index"]: c for c in cites}
    check("an invented label is removed", "E7" in rep.invalid_labels and "[E7]" not in text)
    check("a figure cited to the wrong passage is re-pointed to the right one",
          any(c.get("documentId") == "d1" for c in cites) and rep.repointed)
    check("a figure no passage contains is reported unsupported",
          any("999999" in figs for _, figs in rep.unsupported))
    check("labels are renumbered [1], [2]… in order of appearance",
          "[1]" in text and "[E" not in text and sorted(by_index) == list(range(1, len(cites) + 1)))
    doc_cite = next(c for c in cites if c.get("documentId") == "d1")
    check("the page comes from the quote's own position, not the chunk start",
          doc_cite["page"] == 4 and "004512" in (doc_cite.get("quote") or ""), str(doc_cite))
    law = next((c for c in cites if c["type"] == "case_law"), {})
    check("a case-law citation carries its URL", law.get("url") == "https://indiankanoon.org/doc/1/")


async def check_chat_pipeline(store: QdrantStore, ws: str):
    print("\n[chat pipeline]")
    import app.vectorstore.qdrant as qmod
    from app.langgraph import chat_nodes
    from app.rag.chat_context import build_chat_evidence
    from app.socket.events import event_handler as H

    qmod.vector_store = store  # the chat layer imports it lazily
    gemini_client.generate = _stub_generate
    docs = await store.get_all_documents(ws)
    annexure_of = {d["document_id"]: i for i, d in enumerate(docs, 1)}
    names = {d["document_id"]: d.get("filename") or "" for d in docs}

    full = await build_chat_evidence(
        workspace_id=ws, question="What was paid?", previous_user_turn="",
        annexure_of=annexure_of, names=names, forced_ids=[],
        max_total_chars=400_000, full_mode_max_docs=15)
    pack, mode, uploaded = full
    check("a small workspace goes in whole, as labelled passages",
          mode == "full" and uploaded and "[E1]" in uploaded[0]["content"])
    check("passage headers carry page numbers", "(p. " in uploaded[0]["content"] or "(pp. " in uploaded[0]["content"])

    GEN_CALLS.clear()
    first_doc = docs[0]["document_id"]
    retr = await build_chat_evidence(
        workspace_id=ws, question="What was paid?", previous_user_turn="",
        annexure_of=annexure_of, names=names, forced_ids=[first_doc],
        max_total_chars=400, full_mode_max_docs=15)
    pack_r, mode_r, uploaded_r = retr
    check("a large workspace uses retrieval (rewrite + rerank)",
          mode_r == "retrieval" and any("Rewrite a lawyer" in g for g in GEN_CALLS)
          and any("Score how useful" in g for g in GEN_CALLS))
    check("an annexure the lawyer named is always included",
          any(e["document_id"] == first_doc for e in uploaded_r))

    # Answer node: invented label + unsupported figure, repaired on the retry.
    label = next(iter(pack.items))
    item = pack.items[label]
    fig = next(iter(__import__("app.rag.citations", fromlist=["_figures"])._figures(item.text)), None)
    state = {
        "user_message": "What does the file say?", "intent": "EXPLAIN", "role": "PETITIONER",
        "prompt_context": "CONTEXT", "evidence_index": pack.to_state(),
        "chat_context": {"workspace_state": {"documents_count": len(docs), "role_selected": True,
                                             "analysis_report": {"x": 1}}, "chat_history": []},
    }
    first_sentence = item.text.split("\n")[-1][:80]
    ANSWERS[:] = [
        f"The file records {first_sentence} [{label}][E99]. The total claimed is 7,77,77,777 [{label}].",
        f"The file records {first_sentence} [{label}]. The total claimed is not stated in the documents.",
    ]
    out = await chat_nodes.explain_why_node(state)
    cites = out.get("citations") or []
    check("the answer node returns structured citations", cites and cites[0]["type"] == "document")
    check("an invented label never reaches the reply", "E99" not in out["response_text"] and "[E" not in out["response_text"])
    check("an unsupported figure triggers one repair and is gone after it",
          "7,77,77,777" not in out["response_text"] and len(ANSWERS) == 0, out["response_text"])

    payload_cites = H._citations_for_payload(cites, out["response_text"])
    check("citations reach the ai_response payload", payload_cites and payload_cites[0].index == 1)
    check("a citation whose marker was edited out is not sent",
          H._citations_for_payload(cites, "text without markers") is None)

    ANSWERS[:] = ["The total claimed is 7,77,77,777 [%s]." % label, "Still 7,77,77,777 [%s]." % label]
    out2 = await chat_nodes.explain_why_node(state)
    check("if the repair fails the figure is flagged, not silently asserted",
          chat_nodes.UNVERIFIED_NOTE.strip() in out2["response_text"], out2["response_text"])


async def check_panel_citations(store: QdrantStore):
    print("\n[panel citations]")
    import app.vectorstore.qdrant as qmod
    from app.rag import attribution

    qmod.vector_store = store
    ws = f"panel_{RUN}"
    await store.add_document_chunks(ws, "fir", "--- Page 1 ---\nFIRST INFORMATION REPORT\n\n"
        "--- Page 2 ---\nOn 12/03/2024 the complainant paid Rs. 6,00,000 to the accused as advance for the flat.\n\n"
        "--- Page 3 ---\nThe accused stopped answering calls and the possession was never handed over.",
        {"filename": "FIR.pdf", "uploaded_at": "2026-01-01"})
    await store.add_document_chunks(ws, "notice", "--- Page 1 ---\nLEGAL NOTICE\n\n--- Page 2 ---\n"
        "By notice dated 05/04/2024 the complainant demanded refund of ₹6,00,000 within 15 days.",
        {"filename": "Notice.pdf", "uploaded_at": "2026-01-02"})

    dates = {"listOfDates": [
        {"date": "2024-04-05", "title": "Legal notice issued", "description": "Refund demanded within 15 days",
         "sourceDocId": "fir", "sourceDocName": "FIR.pdf", "pageNumber": 1},
        {"date": "2024-03-12", "title": "Advance paid", "description": "Complainant paid the accused",
         "sourceDocId": "fir", "pageNumber": 1},
        {"date": "2030-01-01", "title": "Invented event", "description": "Nothing supports this"},
    ]}
    n = await attribution.attach_panel_citations(ws, "list_of_dates", dates)
    e0, e1, e2 = dates["listOfDates"]
    check("a date attributed to the wrong document is re-pointed to the one containing it",
          e0["sourceDocId"] == "notice" and e0["sourceDocName"] == "Notice.pdf", str(e0))
    check("a guessed page number is replaced by the page the quote is on",
          e1["pageNumber"] == 2 and e0["pageNumber"] == 2, f"{e1.get('pageNumber')}, {e0.get('pageNumber')}")
    check("the evidence snippet is verbatim from the document",
          "6,00,000" in (e1.get("evidenceSnippet") or "") and e1["citation"]["annexure"] == 1)
    check("an item nothing supports gets no citation (not a wrong one)", "citation" not in e2)

    report = {"weaknesses": [{"id": "w1", "text": "Possession was never handed over by the accused", "isMatched": False},
                             "a bare string item"],
              "risks": [], "strengths": [{"point": "Refund demand of ₹6,00,000 was made in writing", "laws": []}]}
    await attribution.attach_panel_citations(ws, "analysis_report", report)
    check("analysis items gain a citation to the passage that supports them",
          report["weaknesses"][0].get("citations", [{}])[0].get("page") == 3
          and report["strengths"][0].get("citations", [{}])[0].get("documentId") == "notice")
    check("a string item is left untouched (its type never changes)", report["weaknesses"][1] == "a bare string item")

    strat = {"caseSummary": "The complainant paid Rs. 6,00,000 on 12/03/2024 as an advance for a flat. "
                            "A legal notice dated 05/04/2024 demanded a refund."}
    await attribution.attach_panel_citations(ws, "strategy_objective", strat)
    check("each case-summary sentence is traced to its source",
          len(strat.get("caseSummaryCitations") or []) == 2)

    await store.delete_documents(ws, ["notice"])
    again = {"listOfDates": [{"date": "2024-04-05", "title": "Legal notice issued",
                              "description": "Refund demanded", "sourceDocId": "notice"}]}
    await attribution.attach_panel_citations(ws, "list_of_dates", again)
    check("a deleted document is never cited (the evidence cache follows deletions)",
          (again["listOfDates"][0].get("citation") or {}).get("documentId") != "notice")
    await store.delete_workspace(ws)


def check_metrics():
    print("\n[eval metrics]")
    from app.rag.eval.metrics import citation_coverage, citation_precision, recall_at_k, reciprocal_rank
    hits = [{"original_document_id": "x", "content": "noise", "metadata": {"page_start": 1}},
            {"original_document_id": "fir", "content": "paid Rs. 6,00,000 on 12/03/2024", "metadata": {"page_start": 2, "page_end": 2}}]
    exp = [{"document_id": "fir", "page": 2, "quote": "paid rs. 600000"}]
    check("recall@k counts an expected passage in the top k", recall_at_k(hits, exp, 2) == 1.0 and recall_at_k(hits, exp, 1) == 0.0)
    check("MRR is the reciprocal rank of the first expected passage", reciprocal_rank(hits, exp) == 0.5)
    check("citation precision rewards citations to the expected passage",
          citation_precision([{"type": "document", "documentId": "fir", "quote": "x"},
                              {"type": "document", "documentId": "other", "quote": "y"}], {"expected": exp}) == 0.5)
    check("citation coverage is the share of figure-bearing sentences that are cited",
          citation_coverage("Paid 6,00,000 [1]. Notice on 05/04/2024. No figures here.") == 0.5)


class FakeGraph:
    """In-memory stand-in with the GraphStore API the service calls."""

    def __init__(self):
        self.facts = []          # written nodes (with provenance properties)
        self.withdrawn_docs = []
        self.withdrawn_ws = []
        self.fail_workspace_withdraw = False
        self.fail_query = False

    async def write_facts(self, workspace_id, nodes, edges):
        self.facts.extend(n for n in nodes)
        return type("W", (), {"nodes_written": len(nodes), "edges_written": len(edges), "rejected": []})()

    async def withdraw_document(self, workspace_id, document_id):
        self.withdrawn_docs.append(document_id)
        before = len(self.facts)
        self.facts = [f for f in self.facts if f["properties"].get("source_document_id") != document_id]
        return before - len(self.facts)

    async def withdraw_workspace(self, workspace_id):
        if self.fail_workspace_withdraw:
            raise RuntimeError("graph unavailable")
        self.withdrawn_ws.append(workspace_id)
        self.facts = []
        return 0

    async def query(self, name, workspace_id, limit=200):
        if self.fail_query:
            raise RuntimeError("graph unavailable")
        if name == "existing_entity_names":
            return []
        return [
            {"canonical_name": f["properties"].get("canonical_name"),
             "source_document_id": f["properties"].get("source_document_id"),
             "chunk_id": f["properties"].get("chunk_id"),
             "page": f["properties"].get("page")}
            for f in self.facts if f.get("label") == "Party"
        ]


async def check_graph_layer(store: QdrantStore):
    print("\n[knowledge graph]")
    import json as _json
    import app.vectorstore.qdrant as qmod
    from app.core.config import settings as cfg
    from app.core.knowledge_plane import _withdrawal_hook
    from app.ontology.pipeline import ingest_to_knowledge_plane
    from app.rag.chat_context import build_chat_evidence
    from app.socket.events import event_handler as H

    qmod.vector_store = store
    ws = f"graph_{RUN}"
    await store.add_document_chunks(ws, "fir", "--- Page 1 ---\nFIRST INFORMATION REPORT\n\n"
        "--- Page 3 ---\nThe complainant Asha Rao paid the accused Vikram Sethi Rs. 6,00,000 as advance.",
        {"filename": "FIR.pdf", "uploaded_at": "2026-01-01"})

    def complete(system, user):
        nodes = []
        if "Vikram Sethi" in user:
            nodes = [{"label": "Party", "properties": {"canonical_name": "Vikram Sethi", "party_role": "accused"},
                      "confidence": 0.95}]
        return _json.dumps({"nodes": nodes, "edges": []})

    graph = FakeGraph()
    report = await ingest_to_knowledge_plane(ws, graph_store=graph, document_ids=["fir"], complete=complete)
    party = next((f for f in graph.facts if f.get("label") == "Party"), None)
    check("ontology facts are extracted from the evidence chunks", report.nodes_written >= 1 and party is not None,
          str(report.to_dict()))
    props = (party or {}).get("properties") or {}
    check("each fact carries OUR chunk id and the physical page",
          str(props.get("chunk_id", "")).startswith("fir_chunk_") and props.get("page") == 3, str(props))
    await ingest_to_knowledge_plane(ws, graph_store=graph, document_ids=["fir"], complete=complete)
    check("re-ingesting a document replaces its facts (no stale duplicates)",
          len([f for f in graph.facts if f.get("label") == "Party"]) == 1 and "fir" in graph.withdrawn_docs)

    # chat: graph facts tied to the passage they came from
    saved_panels = cfg.retrieval_panels
    cfg.retrieval_panels = "intelligence_chat"
    try:
        docs = await store.get_all_documents(ws)
        ann = {d["document_id"]: i for i, d in enumerate(docs, 1)}
        pack, _mode, _up = await build_chat_evidence(
            workspace_id=ws, question="Who is the accused?", previous_user_turn="",
            annexure_of=ann, names={}, forced_ids=[], max_total_chars=400_000,
            full_mode_max_docs=15, graph_store=graph)
        fact_line = next((l for l in pack.graph_facts if "Vikram Sethi" in l), "")
        label = fact_line.rsplit("[", 1)[-1].rstrip("]") if fact_line else ""
        check("chat receives graph facts, each ending in its source passage label",
              fact_line and label in pack.items and pack.items[label].chunk_id == props.get("chunk_id"),
              str(pack.graph_facts))
        graph.fail_query = True
        pack2, _m, _u = await build_chat_evidence(
            workspace_id=ws, question="Who is the accused?", previous_user_turn="",
            annexure_of=ann, names={}, forced_ids=[], max_total_chars=400_000,
            full_mode_max_docs=15, graph_store=graph)
        check("graph down: chat still gets its passages (graph never gates a turn)",
              pack2.items and not pack2.graph_facts)
        graph.fail_query = False

        H._graph_store = graph
        cfg.retrieval_panels = "list_of_dates"
        block = await H._graph_evidence_block("list_of_dates", ws)
        check("an enabled panel gets a graph evidence block", "RETRIEVED EVIDENCE" in block and "[G1]" in block)
        check("a panel not in RETRIEVAL_PANELS gets nothing",
              await H._graph_evidence_block("analysis_report", ws) == "")
        H._graph_store = None
        check("no graph connected: panels get nothing", await H._graph_evidence_block("list_of_dates", ws) == "")
    finally:
        cfg.retrieval_panels = saved_panels
        H._graph_store = None

    # deletion: every delete withdraws graph facts; a failed purge raises
    store.set_graph_withdrawal(_withdrawal_hook(graph))
    try:
        await store.delete_documents(ws, ["fir"])
        check("deleting a document withdraws its graph facts",
              "fir" in graph.withdrawn_docs and not [f for f in graph.facts if f["properties"].get("source_document_id") == "fir"])
        graph.fail_workspace_withdraw = True
        raised = False
        try:
            await store.delete_workspace(ws)
        except Exception:
            raised = True
        check("a workspace purge FAILS LOUDLY if the graph cannot be cleared", raised)
        graph.fail_workspace_withdraw = False
        await store.delete_workspace(ws)
        check("a successful purge clears the graph too", ws in graph.withdrawn_ws)
    finally:
        store.set_graph_withdrawal(None)


async def _main():
    check_chunker()
    check_citation_checker()
    check_metrics()
    check_page_markers()
    await check_model_client()
    store = QdrantStore()
    try:
        await store.initialize()
    except Exception as e:
        print(f"\nQdrant not reachable on localhost:6333 — store checks skipped ({e})")
        return
    try:
        ws = await check_store(store)
        await check_chat_pipeline(store, ws)
        await check_panel_citations(store)
        await check_graph_layer(store)
        await check_legacy_migration(store)
    finally:
        for name in (store.collection_name, store.chunks_collection):
            try:
                store.client.delete_collection(name)
            except Exception:
                pass


def main():
    asyncio.run(_main())
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for n in failed:
        print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
