"""
Passage retrieval for chat: query rewriting, hybrid search, reranking, diversity.

The previous chat retrieval ran one dense search, used the hits only to *pick
documents*, then sent each picked document's first 40,000 characters — so a
match on page 30 of a long pleading never reached the model. This returns the
matching passages themselves, with their page numbers.

Pipeline:

1. **Rewrite** — one cheap model call turns the question (plus the previous
   user turn, for follow-ups like "and the second one?") into up to three
   self-contained search queries and a list of exact terms (section numbers,
   case numbers, amounts, names). Falls back to the raw question.
2. **Hybrid search** per query (dense + sparse, RRF-fused in Qdrant), then
   reciprocal-rank fusion across queries.
3. **Rerank** — one cheap model call scores the top candidates 0–10 against
   the question; passages below the threshold are dropped. Falls back to the
   fused order if the call fails.
4. **Diversity** — at most ``per_document`` passages from one document, so one
   long annexure cannot crowd out the others.

Every model call here uses the low thinking level and is metered through
``gemini_client`` like any other call.
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

_RRF_K = 60


def _json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def rewrite_query(
    question: str, previous_user_turn: str = "", *, failed_queries: Sequence[str] = ()
) -> List[str]:
    """Self-contained search queries for ``question`` (the question first)."""
    from app.ai.gemini_client import gemini_client, is_generation_error

    base = [q for q in [question.strip()] if q]
    prompt = (
        "Rewrite a lawyer's question about their uploaded case documents into "
        "search queries for a document search engine.\n"
        "Return JSON only: {\"queries\": [\"...\"], \"exact_terms\": [\"...\"]}\n"
        "- queries: 1 to 3 short, self-contained queries. Resolve pronouns and "
        "follow-ups using the previous question. Keep legal terms, names, "
        "dates, amounts and section numbers exactly as written.\n"
        "- exact_terms: identifiers that must match literally (section and case "
        "numbers, FIR numbers, amounts, dates, names). Empty list if none.\n\n"
        f"Previous question: {previous_user_turn.strip()[:500] or '(none)'}\n"
        f"Question: {question.strip()[:1500]}"
    )
    if failed_queries:
        prompt += (
            "\n\nThese queries found nothing relevant — try different wording, "
            "synonyms, or the underlying legal concept:\n"
            + "\n".join(f"- {q}" for q in failed_queries[:5])
        )
        base = []
    try:
        raw = await gemini_client.generate(
            prompt,
            temperature=0.0,
            max_tokens=600,
            thinking_level="LOW",
            response_mime_type="application/json",
        )
        if is_generation_error(raw):
            return base
        parsed = _json_object(raw) or {}
        queries = [str(q).strip() for q in (parsed.get("queries") or []) if str(q).strip()]
        exact = [str(t).strip() for t in (parsed.get("exact_terms") or []) if str(t).strip()]
        out = list(dict.fromkeys(base + queries[:3]))
        if exact:
            out.append(" ".join(exact[:8]))
        return out[:5]
    except Exception as err:
        logger.debug("Query rewrite failed; using the question as is: {}", err)
        return base


async def rerank(
    question: str, passages: Sequence[Dict[str, Any]], *, keep_min: float = 4.0
) -> List[Dict[str, Any]]:
    """Score passages 0–10 for relevance and drop the ones below ``keep_min``.

    Returns the passages sorted best first, each with ``rerank_score``. On any
    failure the input order is returned unchanged.
    """
    from app.ai.gemini_client import gemini_client, is_generation_error

    if not passages:
        return []
    numbered = "\n\n".join(
        f"[{i}] {str(p.get('content') or '')[:900]}" for i, p in enumerate(passages, 1)
    )
    prompt = (
        "Score how useful each passage is for answering the question, from 0 "
        "(irrelevant) to 10 (directly answers it). A passage that states a "
        "fact, date, amount or provision the answer needs scores high even if "
        "short.\n"
        "Return JSON only: {\"scores\": [{\"i\": 1, \"s\": 7}, ...]} covering every passage.\n\n"
        f"Question: {question.strip()[:1500]}\n\nPassages:\n{numbered}"
    )
    try:
        raw = await gemini_client.generate(
            prompt,
            temperature=0.0,
            max_tokens=1500,
            thinking_level="LOW",
            response_mime_type="application/json",
        )
        if is_generation_error(raw):
            return list(passages)
        parsed = _json_object(raw) or {}
        scores: Dict[int, float] = {}
        for row in parsed.get("scores") or []:
            try:
                scores[int(row.get("i"))] = float(row.get("s"))
            except (TypeError, ValueError, AttributeError):
                continue
        if not scores:
            return list(passages)
        ranked = []
        for i, p in enumerate(passages, 1):
            s = scores.get(i, 0.0)
            if s >= keep_min:
                ranked.append({**p, "rerank_score": s})
        ranked.sort(key=lambda p: p["rerank_score"], reverse=True)
        # Never return nothing when the search did find something: an empty
        # context produces a worse answer than a weak one flagged as weak.
        return ranked or [{**p, "rerank_score": scores.get(i, 0.0)} for i, p in enumerate(passages[:3], 1)]
    except Exception as err:
        logger.debug("Rerank failed; keeping fused order: {}", err)
        return list(passages)


def _fuse(result_lists: Sequence[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    scores: Dict[str, float] = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for results in result_lists:
        for rank, hit in enumerate(results):
            key = str(hit.get("document_id") or hit.get("id"))
            by_id.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return [by_id[k] for k in sorted(scores, key=scores.get, reverse=True)]


def _diversify(hits: Sequence[Dict[str, Any]], per_document: int, limit: int) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for h in hits:
        doc = str(h.get("original_document_id") or "")
        if counts.get(doc, 0) >= per_document:
            continue
        counts[doc] = counts.get(doc, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _all_weak(ranked: Sequence[Dict[str, Any]], keep_min: float = 4.0) -> bool:
    """True when reranking scored nothing as relevant (only the fallback
    passages survived)."""
    scored = [p.get("rerank_score") for p in ranked if p.get("rerank_score") is not None]
    return bool(scored) and max(scored) < keep_min


async def retrieve_passages(
    workspace_id: str,
    question: str,
    *,
    previous_user_turn: str = "",
    limit: int = 16,
    candidates: int = 40,
    per_document: int = 5,
    document_ids: Optional[List[str]] = None,
    use_rewrite: bool = True,
    use_rerank: bool = True,
) -> List[Dict[str, Any]]:
    """Best passages for ``question``, as stored chunk dicts with scores."""
    from app.vectorstore.qdrant import vector_store

    queries = (
        await rewrite_query(question, previous_user_turn)
        if use_rewrite
        else [q for q in [question] if q.strip()]
    )
    if not queries:
        return []
    if not await vector_store.ensure_workspace_indexed(workspace_id):
        hits = await vector_store.search(workspace_id, question, limit=limit)
        return _diversify(hits, per_document, limit)
    lists = []
    for q in queries:
        try:
            lists.append(
                await vector_store._hybrid_search(
                    workspace_id, q, limit=candidates // 2, document_ids=document_ids
                )
            )
        except Exception as err:
            logger.warning("Hybrid search failed for one query: {}", err)
    fused = _fuse(lists)[:candidates]
    ranked = await rerank(question, fused) if use_rerank else fused
    if use_rerank and use_rewrite and _all_weak(ranked):
        # Grade → rewrite → retrieve once more: nothing scored as relevant, so
        # the wording, not the documents, is the likely problem.
        retry_queries = await rewrite_query(
            question, previous_user_turn, failed_queries=queries
        )
        retry_lists = []
        for q in retry_queries:
            try:
                retry_lists.append(
                    await vector_store._hybrid_search(
                        workspace_id, q, limit=candidates // 2, document_ids=document_ids
                    )
                )
            except Exception as err:
                logger.warning("Retry search failed for one query: {}", err)
        if retry_lists:
            retry_ranked = await rerank(question, _fuse(retry_lists)[:candidates])
            if not _all_weak(retry_ranked):
                logger.info("Retrieval retry with rewritten queries found relevant passages")
                ranked = retry_ranked
    picked = _diversify(ranked, per_document, limit)
    logger.info(
        "Retrieved {} passage(s) for workspace {} ({} quer(ies), {} candidate(s))",
        len(picked),
        workspace_id,
        len(queries),
        len(fused),
    )
    return picked
