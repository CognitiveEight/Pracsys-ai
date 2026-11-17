"""
Stage 4 of the trust pipeline: dedup and corroboration.

Every evidence row for one ``fact_key`` is folded into one fact. Its confidence
is recomputed from the **full** evidence set every time, never bumped
incrementally, so the number is always reproducible from the stored evidence:

    w(e)  = modality_weight × (floor + (1 − floor) × relevance(doc))
            × SUMMARY_PAGE_WEIGHT if the passage is on a paper-book summary page
    g     = max over a source group of extraction_confidence × w
    p     = 1 − ∏ (1 − g)                     (noisy-OR across groups)

Evidence is grouped **before** it is combined, and only independent groups
compound. Not independent:

* two passages of one document;
* a passage copied into another document — a petition quoting its annexure,
  a paper-book carrying the agreement, a .docx and its PDF twin. Detected as
  5-word-shingle Jaccard >= ``TRUST_COPY_SIMILARITY`` between the two spans.

Without grouping, uploading one agreement twice, or filing a petition that
quotes it, would make every fact in it look independently corroborated.
"""

import re
from typing import Any, Dict, List, Mapping, Sequence

from app.trust.claims import MODALITY_ALLEGED

#: How much a statement's form says about the fact. An allegation is one
#: party's case, not an attested fact.
MODALITY_WEIGHTS = {"asserted": 0.95, MODALITY_ALLEGED: 0.85}

#: A paper-book INDEX / LIST OF DATES / SYNOPSIS restates the case; it does not
#: attest to anything.
SUMMARY_PAGE_WEIGHT = 0.5

_SHINGLE = 5


def shingles(text: str) -> frozenset:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    if len(words) < _SHINGLE:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(" ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def source_groups(rows: Sequence[Mapping[str, Any]], copy_similarity: float) -> List[List[int]]:
    """Partition evidence row indexes into independent source groups.

    Union-find: one document is one group, and a verbatim copy joins the group
    of what it copies. Groups are returned sorted, so the result is
    deterministic.
    """
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    first_of_doc: Dict[str, int] = {}
    for i, row in enumerate(rows):
        doc = str(row.get("document_id") or "")
        if doc in first_of_doc:
            union(i, first_of_doc[doc])
        else:
            first_of_doc[doc] = i

    spans = [shingles(row.get("span") or "") for row in rows]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if find(i) != find(j) and _jaccard(spans[i], spans[j]) >= copy_similarity:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values())


def evidence_weight(row: Mapping[str, Any], relevance: Mapping[str, float], floor: float) -> float:
    """``w(e)`` from the formula above."""
    modality = MODALITY_WEIGHTS.get(str(row.get("modality")), MODALITY_WEIGHTS["asserted"])
    score = float(relevance.get(str(row.get("document_id")), 1.0))
    weight = modality * (floor + (1.0 - floor) * score)
    if row.get("on_summary_page"):
        weight *= SUMMARY_PAGE_WEIGHT
    return weight


def corroborate(
    rows: Sequence[Mapping[str, Any]],
    relevance: Mapping[str, float],
    *,
    floor: float,
    copy_similarity: float,
) -> Dict[str, Any]:
    """Fold one fact's (non-denial) evidence into a confidence.

    Returns:
        ``{confidence, source_groups, groups, evidence}`` where ``groups`` lists
        each group's best score and documents, and ``evidence`` is the rows in
        a stable order with their weight.
    """
    ordered = sorted(
        rows, key=lambda r: (str(r.get("document_id")), str(r.get("chunk_id")), str(r.get("span")))
    )
    groups_idx = source_groups(ordered, copy_similarity)
    groups = []
    remaining = 1.0
    for members in groups_idx:
        best = 0.0
        for i in members:
            conf = max(0.0, min(1.0, float(ordered[i].get("extraction_confidence") or 0.0)))
            best = max(best, conf * evidence_weight(ordered[i], relevance, floor))
        remaining *= 1.0 - best
        groups.append(
            {
                "score": round(best, 4),
                "document_ids": sorted({str(ordered[i].get("document_id")) for i in members}),
            }
        )
    return {
        "confidence": round(1.0 - remaining, 4),
        "source_groups": len(groups),
        "groups": groups,
        "evidence": ordered,
    }
