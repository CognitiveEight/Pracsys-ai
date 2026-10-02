"""
Build the evidence a chat turn may cite.

Two modes, chosen by size:

* **full** — the workspace fits the chat budget: every document goes in, whole,
  laid out as consecutive labelled passages (overlap removed, so nothing is
  repeated). Nothing can be missed, and every sentence is citable.
* **retrieval** — the workspace is too large: the best passages for the
  question (see app/rag/retriever.py), plus the top passages of any annexure
  the lawyer named explicitly, in document and reading order.

Either way the result is an :class:`EvidencePack` (label → passage, page,
annexure) and the ``uploaded_documents`` blocks the prompt renders, where each
passage carries its ``[E#]`` label.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from app.rag.evidence import EvidencePack, render_document_block


def _body_len(chunk: Dict[str, Any]) -> int:
    meta = chunk.get("metadata") or {}
    overlap = meta.get("overlap_prev") if isinstance(meta.get("overlap_prev"), int) else 0
    return max(0, len(str(chunk.get("content") or "")) - overlap)


def _position(chunk: Dict[str, Any]) -> int:
    idx = (chunk.get("metadata") or {}).get("chunk_index")
    return idx if isinstance(idx, int) else 0


async def build_chat_evidence(
    *,
    workspace_id: str,
    question: str,
    previous_user_turn: str,
    annexure_of: Dict[str, int],
    names: Dict[str, str],
    forced_ids: Iterable[str],
    max_total_chars: int,
    full_mode_max_docs: int,
    graph_store: Any = None,
) -> Optional[Tuple[EvidencePack, str, List[Dict[str, Any]]]]:
    """Return ``(pack, mode, uploaded_documents)``, or None if the chunk store
    cannot serve this workspace (the caller then uses the legacy context)."""
    from app.rag.retriever import retrieve_passages
    from app.vectorstore.qdrant import vector_store

    chunks = await vector_store.get_document_chunks(workspace_id)
    chunks = [c for c in chunks if str(c.get("original_document_id") or "") in annexure_of]
    if not chunks:
        return None

    total = sum(_body_len(c) for c in chunks)
    doc_count = len({c.get("original_document_id") for c in chunks})
    if doc_count <= full_mode_max_docs and total <= max_total_chars:
        mode, selected = "full", chunks
    else:
        mode = "retrieval"
        hits = await retrieve_passages(
            workspace_id, question, previous_user_turn=previous_user_turn, limit=20
        )
        # The annexure the lawyer named goes FIRST, so a tight budget drops
        # general matches rather than the document they asked about.
        forced_hits: List[Dict[str, Any]] = []
        forced = [d for d in dict.fromkeys(forced_ids) if d in annexure_of]
        for did in forced:
            doc_hits = await retrieve_passages(
                workspace_id,
                question,
                document_ids=[did],
                limit=6,
                use_rewrite=False,
                use_rerank=False,
            )
            if not doc_hits:  # the named annexure must be present either way
                doc_hits = [c for c in chunks if c.get("original_document_id") == did][:6]
            forced_hits.extend(doc_hits)
        hits = forced_hits + hits
        seen, selected, used = set(), [], 0
        for h in hits:
            key = h.get("document_id")
            if key in seen:
                continue
            size = len(str(h.get("content") or ""))
            # At least one passage of each named annexure, even over budget.
            is_forced = h in forced_hits and not any(
                s.get("original_document_id") == h.get("original_document_id") for s in selected
            )
            if used + size > max_total_chars and not is_forced:
                continue
            seen.add(key)
            selected.append(h)
            used += size

    selected.sort(
        key=lambda c: (annexure_of.get(str(c.get("original_document_id") or ""), 10**6), _position(c))
    )
    pack = EvidencePack()
    by_doc: Dict[str, List[Any]] = {}
    for chunk in selected:
        did = str(chunk.get("original_document_id") or "")
        item = pack.add_passage(
            chunk,
            annexure=annexure_of.get(did),
            file_name=names.get(did),
            drop_overlap=(mode == "full"),
        )
        if item is not None:
            by_doc.setdefault(did, []).append(item)

    if graph_store is not None:
        try:
            await _add_graph_facts(
                pack, by_doc, chunks, workspace_id, question, graph_store, annexure_of, names
            )
        except Exception as graph_err:  # the graph improves chat; it never gates it
            logger.warning("Graph facts skipped for this turn: {}", graph_err)

    uploaded: List[Dict[str, Any]] = []
    for did, items in by_doc.items():
        entry: Dict[str, Any] = {
            "document_id": did,
            "annexure_number": annexure_of.get(did),
            "content": render_document_block(items),
        }
        if names.get(did):
            entry["fileName"] = names[did]
        uploaded.append(entry)
    uploaded.sort(key=lambda e: e.get("annexure_number") or 10**6)
    logger.info(
        "Chat evidence: mode={} documents={} passages={} graph_facts={}",
        mode,
        len(uploaded),
        len(pack.items),
        len(pack.graph_facts),
    )
    return pack, mode, uploaded


#: Graph facts added to one chat turn, at most.
_MAX_GRAPH_FACTS = 30


async def _add_graph_facts(
    pack: EvidencePack,
    by_doc: Dict[str, List[Any]],
    chunks: List[Dict[str, Any]],
    workspace_id: str,
    question: str,
    graph_store: Any,
    annexure_of: Dict[str, int],
    names: Dict[str, str],
) -> None:
    """Add ontology facts (parties, chronology, reliefs, obligations) for this
    turn, each tied to the passage it was extracted from.

    Only when ``intelligence_chat`` is in ``RETRIEVAL_PANELS``. A fact whose
    source passage is not yet in the pack brings that passage in, so the
    model can cite it and the citation checker can verify the quote.
    """
    from app.retrieval.panel_bridge import is_enabled, retrieve_for_panel

    if not is_enabled("intelligence_chat"):
        return
    evidence = await retrieve_for_panel(
        "intelligence_chat", workspace_id, graph_store=graph_store, question=question
    )
    facts = [e for e in evidence.evidence if e.get("modality") == "graph"][:_MAX_GRAPH_FACTS]
    if not facts:
        return
    label_of_chunk = {item.chunk_id: label for label, item in pack.items.items()}
    chunk_by_id = {c.get("document_id"): c for c in chunks}
    for fact in facts:
        chunk_id = fact.get("chunkId")
        label = label_of_chunk.get(chunk_id)
        if label is None and chunk_id in chunk_by_id:
            did = str(chunk_by_id[chunk_id].get("original_document_id") or "")
            item = pack.add_passage(
                chunk_by_id[chunk_id], annexure=annexure_of.get(did), file_name=names.get(did)
            )
            if item is not None:
                by_doc.setdefault(did, []).append(item)
                label_of_chunk[chunk_id] = label = item.label
        if label:
            pack.graph_facts.append(f"- {fact.get('text')} [{label}]")
