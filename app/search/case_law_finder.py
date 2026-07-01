"""Case-law source aggregator for the Case Laws feature.

Finds relevant Indian court judgments for a workspace query by combining three
complementary sources, in order of preference:

  1. Indian Kanoon direct search (HTML scrape) — broad, free, indexed within
     days of pronouncement.
  2. Vertex AI Search (optional, GCP-configured) — structured fallback over an
     Indian-Kanoon-scoped data store.
  3. Gemini ``google_search`` grounding — real-time web search that returns the
     LATEST judgments plus the source URLs the model relied on.

Every returned row carries a real ``url`` so the downstream Case Laws generator
can enforce its strict "source-verified only" rule. The output shape matches the
existing applicable-laws search rows (``title``/``url``/``snippet``) plus a
``sourceName`` tag for provenance.

The Indian Kanoon scrape lives here as the single source of truth;
``events._search_indiankanoon`` delegates to it so the applicable-laws path and
the case-laws path share identical scraping behaviour.
"""

import asyncio
from html import unescape
from typing import Dict, List, Tuple
from urllib.parse import quote_plus
import re

import httpx
from loguru import logger

from app.observability.trace import tracer
from app.search.vertex_ai_search import (
    search_indiankanoon as vertex_search_indiankanoon,
)


# Indian Kanoon search results render each hit as
#   <div class="result_title"> <a href="/docfragment/<id>/?formInput=...">Case Name vs ... on DATE</a>
# The case name (with the date suffix) lives in this anchor; the canonical
# judgment URL is /doc/<id>/. Matching the result_title anchor (rather than any
# /doc/ link) avoids capturing the boilerplate "Full Document" / citation links,
# which previously surfaced "Full Document" as the title for every row.
_KANOON_RESULT_RE = re.compile(
    r'class="result_title"[^>]*>\s*<a[^>]+href="/docfragment/(?P<id>\d+)/?[^"]*"[^>]*>(?P<title>.*?)</a>',
    flags=re.I | re.S,
)


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", flags=re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n\s*\n\s*\n+")


async def fetch_judgment_text(url: str, *, max_chars: int = 14000) -> str:
    """Fetch a judgment/source page and return cleaned, readable text.

    Used by the Case Laws enrichment pass to ground the digest (ratio, facts,
    holding, relief, citations) in the REAL judgment text rather than a search
    snippet. Indian Kanoon ``/doc/<id>/`` pages and most judgment/news pages are
    plain HTML; we strip scripts/styles/tags and collapse whitespace. Returns an
    empty string on any error so the caller can fall back to snippet-only.
    """
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if res.status_code >= 400:
                logger.warning(
                    "Case-law fetch: {} returned {}", url[:80], res.status_code
                )
                return ""
            html = res.text or ""
    except Exception as e:
        logger.warning("Case-law fetch failed for {}: {}", url[:80], e)
        return ""

    text = _TAG_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


async def scrape_indiankanoon(query: str, *, limit: int = 8) -> List[Dict[str, str]]:
    """Search Indian Kanoon directly and return lightweight result rows.

    Returns ``[{"title", "url", "snippet"}]`` (snippet empty — the listing page
    does not expose reliable snippets). Returns ``[]`` on any HTTP/parse error so
    callers can fall back to other sources.
    """
    if not query.strip():
        return []
    search_url = f"https://indiankanoon.org/search/?formInput={quote_plus(query)}"
    logger.info("Case-law search: Indian Kanoon query={}", query[:180])
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            res = await client.get(search_url, headers={"User-Agent": "Mozilla/5.0"})
            if res.status_code >= 400:
                logger.warning(
                    "Case-law search: Indian Kanoon status {}", res.status_code
                )
                return []
            html = res.text or ""
    except Exception as e:
        logger.warning("Case-law search: Indian Kanoon failed: {}", e)
        return []

    results: List[Dict[str, str]] = []
    seen = set()
    for m in _KANOON_RESULT_RE.finditer(html):
        doc_id = m.group("id") or ""
        title = re.sub(r"<.*?>", " ", m.group("title") or "")
        title = unescape(re.sub(r"\s+", " ", title)).strip()
        if not title or not doc_id:
            continue
        url = f"https://indiankanoon.org/doc/{doc_id}/"
        if url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= limit:
            break
    return results


async def _grounded_case_law_sources(query: str) -> Tuple[str, List[Dict[str, str]]]:
    """Use Gemini Google-Search grounding to surface the latest precedents.

    Returns ``(research_notes, rows)`` where rows are tagged
    ``sourceName="Web (Google Search)"`` with redirect URLs resolved to their
    real destinations, and ``research_notes`` is the model's prose answer.
    Returns ``("", [])`` when grounding is unavailable or yields no web sources.
    """
    # Imported lazily to avoid a circular import at module load (gemini_client
    # has no dependency on this module, but keep the boundary clean).
    from app.ai.gemini_client import gemini_client

    prompt = (
        "Find recent and landmark Indian court judgments (Supreme Court or High "
        "Courts) that are relevant precedents for the following case. Prefer the "
        "most recent authoritative rulings. For each, give the case name, "
        "citation, court, the holding in one line, and the source.\n\n"
        f"Case: {query}"
    )
    try:
        text, sources = await gemini_client.generate_grounded(prompt=prompt)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Case-law search: grounding failed: {}", e)
        return "", []

    # Grounding returns Google redirect wrappers
    # (vertexaisearch.cloud.google.com/grounding-api-redirect/...), which are
    # opaque and not verifiable at a glance. Resolve each to its real
    # destination URL (livelaw, indiankanoon, sci.gov.in, ...) concurrently so
    # the panel shows clean, clickable source links.
    async def _resolve(url: str) -> str:
        if "grounding-api-redirect" not in url:
            return url
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                final = str(resp.url)
                return final or url
        except Exception:
            return url  # fall back to the redirect URL if resolution fails

    raw_urls = [(s.get("url") or "").strip() for s in (sources or [])]
    resolved = await asyncio.gather(
        *[_resolve(u) for u in raw_urls if u], return_exceptions=False
    )
    resolved_iter = iter(resolved)

    rows: List[Dict[str, str]] = []
    for s in sources or []:
        url = (s.get("url") or "").strip()
        if not url:
            continue
        final_url = next(resolved_iter, url) or url
        rows.append(
            {
                "title": (s.get("title") or "").strip(),
                "url": final_url,
                "snippet": "",
                "sourceName": "Web (Google Search)",
            }
        )
    return text or "", rows


@tracer.trace("case_law_find_sources")
async def find_case_law_sources(
    query: str, *, limit: int = 12
) -> Tuple[List[Dict[str, str]], str]:
    """Aggregate case-law sources from Kanoon + Vertex + Gemini grounding.

    Each source is queried independently and its failure is isolated, so one bad
    source never sinks the others. Results are deduped by normalized URL and
    capped at ``limit``, preserving the Kanoon → Vertex → grounding order.

    Returns ``(sources, research_notes)`` where ``sources`` is a list of
    ``{"title", "url", "snippet", "sourceName"}`` and ``research_notes`` is the
    grounding model's prose answer (case names, citations, holdings it found on
    the live web). The notes give the downstream ranker real substance to work
    from when the bare source rows are thin (grounding returns clean URLs but no
    snippets), while every emitted case still maps to a real source URL.
    """
    if not query.strip():
        return [], ""

    collected: List[Dict[str, str]] = []
    research_notes = ""

    # 1) Indian Kanoon direct scrape. Kanoon's search is effectively an AND over
    #    terms, so a long many-term query (title + every case keyword) often
    #    returns zero hits. Retry once with a trimmed query (the most salient
    #    leading terms) so a verbose signals-query still surfaces precedents.
    try:
        kanoon_rows = await scrape_indiankanoon(query)
        if not kanoon_rows:
            short_query = " ".join(query.split()[:8]).strip()
            if short_query and short_query != query:
                logger.info(
                    "Case-law search: Kanoon empty for full query; retrying trimmed"
                )
                kanoon_rows = await scrape_indiankanoon(short_query)
        for row in kanoon_rows:
            row.setdefault("sourceName", "Indian Kanoon")
            collected.append(row)
    except Exception as e:  # pragma: no cover - scrape already guards internally
        logger.warning("Case-law search: Kanoon stage error: {}", e)

    # 2) Vertex AI Search (Indian-Kanoon-scoped data store, optional).
    try:
        for row in await vertex_search_indiankanoon(query):
            row.setdefault("sourceName", "Indian Kanoon")
            collected.append(row)
    except Exception as e:
        logger.warning("Case-law search: Vertex stage error: {}", e)

    # 3) Gemini Google-Search grounding (latest precedents, real-time).
    try:
        research_notes, grounded_rows = await _grounded_case_law_sources(query)
        collected.extend(grounded_rows)
    except Exception as e:  # pragma: no cover - already guards internally
        logger.warning("Case-law search: grounding stage error: {}", e)

    # Dedupe by normalized URL, preserve order.
    out: List[Dict[str, str]] = []
    seen = set()
    for row in collected:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break

    logger.info(
        "Case-law search: {} unique source(s) for query={}", len(out), query[:120]
    )
    return out, research_notes
