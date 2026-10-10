"""
Metrics for retrieval and citation quality, over a golden set.

Golden set format (JSON Lines, one case per line)::

    {
      "id": "fir-advance-amount",
      "workspace_id": "68c1f0a9e4b0d2a1c3f45678",
      "question": "How much did the complainant pay as advance?",
      "expected": [
        {"document_id": "68c1...671", "page": 2, "quote": "paid Rs. 6,00,000"}
      ],
      "facts": ["6,00,000", "12/03/2024"]
    }

* ``expected`` — the passages a correct answer must rest on. A retrieved
  passage matches when it is from ``document_id`` and, if given, covers
  ``page`` and contains ``quote`` (compared ignoring case, spacing and digit
  grouping).
* ``facts`` — figures/phrases the answer must state (for answer-level checks).

Metrics:

* **recall@k** — share of expected passages found in the top k.
* **MRR** — mean reciprocal rank of the first expected passage.
* **citation precision** — share of citations whose quoted passage contains an
  expected fact or matches an expected passage.
* **citation coverage** — share of answer sentences stating a figure that
  carry a citation.
* **unsupported rate** — share of answers left with a figure no passage holds.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(",", "")).strip().lower()


def passage_matches(hit: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    meta = hit.get("metadata") or {}
    if str(hit.get("original_document_id") or "") != str(expected.get("document_id") or ""):
        return False
    page = expected.get("page")
    if page is not None:
        start, end = meta.get("page_start"), meta.get("page_end") or meta.get("page_start")
        if isinstance(start, int) and not (start <= int(page) <= (end or start)):
            return False
    quote = expected.get("quote")
    if quote and _norm(quote) not in _norm(str(hit.get("content") or "")):
        return False
    return True


def recall_at_k(hits: Sequence[Dict[str, Any]], expected: Sequence[Dict[str, Any]], k: int) -> float:
    if not expected:
        return 1.0
    top = list(hits)[:k]
    found = sum(1 for e in expected if any(passage_matches(h, e) for h in top))
    return found / len(expected)


def reciprocal_rank(hits: Sequence[Dict[str, Any]], expected: Sequence[Dict[str, Any]]) -> float:
    for rank, h in enumerate(hits, 1):
        if any(passage_matches(h, e) for e in expected):
            return 1.0 / rank
    return 0.0


def citation_precision(citations: Sequence[Dict[str, Any]], case: Dict[str, Any]) -> Optional[float]:
    doc_cites = [c for c in citations if c.get("type") == "document"]
    if not doc_cites:
        return None
    facts = [_norm(f) for f in case.get("facts") or []]
    expected_docs = {str(e.get("document_id")) for e in case.get("expected") or []}
    good = 0
    for c in doc_cites:
        quote = _norm(c.get("quote") or "")
        if str(c.get("documentId")) in expected_docs or any(f and f in quote for f in facts):
            good += 1
    return good / len(doc_cites)


_FIG = re.compile(r"\d[\d,./-]{2,}")


def citation_coverage(answer: str) -> Optional[float]:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer or "") if _FIG.search(re.sub(r"\[\d+\]", "", s))]
    if not sentences:
        return None
    cited = sum(1 for s in sentences if re.search(r"\[\d+\]", s))
    return cited / len(sentences)


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# Knowledge-graph metrics (ported from the ontology RAG work). Golden cases may
# carry "parties" ([{canonical_name, aliases}]) and "chronology"
# ([{date, description}]) marked by a lawyer.
# ---------------------------------------------------------------------------


def _words(text: Any) -> set:
    return set(re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()).split())


def entity_resolution_precision(
    resolved: Sequence[Dict[str, Any]], truth: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """One real party = exactly one graph node?

    Precision over resolved parties, plus the two failure modes named
    separately: a *split* (one real party as several nodes — aliasing too
    strict) and a *merge* (several real parties in one node — too loose, and
    the more dangerous one in a legal setting).
    """
    if not resolved:
        return {"score": 1.0 if not truth else 0.0, "resolved": 0}
    index: Dict[str, str] = {}
    for party in truth:
        canonical = str(party.get("canonical_name", ""))
        for alias in [canonical, *(party.get("aliases") or [])]:
            if _norm(alias):
                index[_norm(alias)] = canonical
    correct, merges, seen = 0, [], {}
    for party in resolved:
        canonical = str(party.get("canonical_name", ""))
        matched = {index[_norm(a)] for a in [canonical, *(party.get("aliases") or [])] if _norm(a) in index}
        if len(matched) == 1:
            correct += 1
            seen.setdefault(next(iter(matched)), []).append(canonical)
        elif len(matched) > 1:
            merges.append({"node": canonical, "collapsed": sorted(matched)})
    return {
        "score": correct / len(resolved),
        "resolved": len(resolved),
        "merges": merges,
        "splits": {t: n for t, n in seen.items() if len(n) > 1},
    }


def chronology_completeness(
    produced: Sequence[Dict[str, Any]], truth: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Recall against a lawyer-marked chronology: same date and at least half
    of the truth description's words."""
    if not truth:
        return {"score": 1.0, "groundTruth": 0}
    by_date: Dict[str, List[set]] = {}
    for event in produced:
        by_date.setdefault(_norm(event.get("date")), []).append(_words(event.get("description")))
    found, missed = 0, []
    for event in truth:
        words = _words(event.get("description"))
        candidates = by_date.get(_norm(event.get("date")), [])
        if any(len(words & c) >= max(1, len(words) // 2) for c in candidates) if words else candidates:
            found += 1
        else:
            missed.append(event)
    return {"score": found / len(truth), "groundTruth": len(truth), "missed": missed}
