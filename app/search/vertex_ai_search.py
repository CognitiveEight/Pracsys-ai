"""Google Vertex AI Search wrapper used as the Indian Kanoon fallback.

Replaces the previous Google Custom Search (CSE) helper. The public surface
is intentionally a single async function so the rest of the pipeline keeps
consuming the same ``[{"title", "url", "snippet"}, ...]`` shape.
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings


_retriever: Optional[Any] = None


def _get_retriever() -> Optional[Any]:
    """Lazily build the Vertex AI Search retriever once and reuse it.

    Returns ``None`` when the required GCP settings are missing — callers
    treat this the same as "no results", so local dev without GCP still
    boots and the rest of the pipeline (Kanoon scrape, heuristic fallback,
    Gemini grounding) keeps working.
    """
    global _retriever
    if _retriever is not None:
        return _retriever
    if not (settings.gcp_project_id and settings.vertex_search_data_store_id):
        return None
    try:
        # Imported lazily so the absence of the optional dependency does
        # not break service startup; only operators who configure Vertex
        # AI Search need the package installed.
        from langchain_google_community import VertexAISearchRetriever
    except ImportError as e:
        logger.warning(
            "Vertex AI Search disabled: langchain-google-community not installed ({})",
            e,
        )
        return None
    try:
        _retriever = VertexAISearchRetriever(
            project_id=settings.gcp_project_id,
            location_id=settings.vertex_search_location_id,
            data_store_id=settings.vertex_search_data_store_id,
            max_documents=settings.vertex_search_max_documents,
            engine_data_type=settings.vertex_search_engine_data_type,
            get_extractive_answers=True,
        )
    except Exception as e:
        logger.warning("Vertex AI Search retriever init failed: {}", e)
        return None
    return _retriever


async def search_indiankanoon(query: str) -> List[Dict[str, str]]:
    """Search Vertex AI Search and return Indian-Kanoon-shaped rows.

    Drop-in replacement for the legacy ``_search_google_cse_for_indiankanoon``
    helper: same input contract (single query string), same output shape
    (list of ``{title, url, snippet}`` dicts, capped at 8).
    """
    if not query.strip():
        return []
    retriever = _get_retriever()
    if retriever is None:
        return []

    # Belt-and-braces site filter: harmless on a Kanoon-only website data
    # store, useful on a blended store, and keeps query semantics aligned
    # with the old CSE call.
    constrained = f"site:indiankanoon.org {query}"
    try:
        docs = await retriever.ainvoke(constrained)
    except Exception as e:
        logger.warning("Applicable laws: Vertex AI Search failed: {}", e)
        return []

    out: List[Dict[str, str]] = []
    for doc in docs or []:
        meta = getattr(doc, "metadata", {}) or {}
        # Discovery Engine has used different keys for the source URL across
        # data-store types and SDK versions ("link" / "source" / "uri") —
        # read all of them to stay forward-compatible.
        link = str(
            meta.get("link") or meta.get("source") or meta.get("uri") or ""
        ).strip()
        if "indiankanoon.org" not in link:
            continue
        title = str(meta.get("title") or "").strip()
        snippet = (getattr(doc, "page_content", "") or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600]
        out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= settings.vertex_search_max_documents:
            break

    if out:
        preview = [
            {"title": r.get("title", "")[:120], "url": r.get("url", "")}
            for r in out[:5]
        ]
        logger.info("Applicable laws: Vertex AI Search results preview={}", preview)
    else:
        logger.info("Applicable laws: Vertex AI Search returned no results")
    return out
