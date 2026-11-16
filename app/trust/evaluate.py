"""
The whole case, evaluated from its evidence: stages 3-6 as one pure function.

``evaluate_case`` takes the case's stored evidence, the document relevance
scores and the reviewers' decisions, and returns every fact, conflict and
review item. It reads nothing and writes nothing, so a recompute is
reproducible from the stored inputs alone, and the whole engine is testable
without Qdrant or a model.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from app.trust import conflicts as conflict_engine
from app.trust.canonical import canonicalise_rows
from app.trust.claims import EXISTS
from app.trust.corroboration import corroborate
from app.trust.router import (
    Bands,
    apply_conflict_decisions,
    fact_decision,
    review_items,
    route,
)


@dataclass(frozen=True)
class TrustParams:
    bands: Bands = field(default_factory=Bands)
    relevance_floor: float = 0.70
    copy_similarity: float = 0.80

    @classmethod
    def from_settings(cls) -> "TrustParams":
        from app.core.config import settings

        return cls(
            bands=Bands(
                trusted=settings.trust_band_trusted,
                review=settings.trust_band_review,
                conflict_penalty=settings.trust_conflict_penalty,
            ),
            relevance_floor=settings.trust_relevance_floor,
            copy_similarity=settings.trust_copy_similarity,
        )


def _fold(
    key: str,
    rows: Sequence[Mapping[str, Any]],
    relevance: Mapping[str, float],
    params: TrustParams,
) -> Dict[str, Any]:
    result = corroborate(
        rows,
        relevance,
        floor=params.relevance_floor,
        copy_similarity=params.copy_similarity,
    )
    first = result["evidence"][0]
    return {
        "fact_key": key,
        "slot_key": first["slot_key"],
        "claim": first["claim"],
        "evidence": result["evidence"],
        "corroboration": result["confidence"],
        "source_groups": result["source_groups"],
        "groups": result["groups"],
        "document_ids": sorted({str(r.get("document_id")) for r in rows}),
    }


def evaluate_case(
    evidence: Sequence[Mapping[str, Any]],
    relevance: Mapping[str, float],
    decisions: Mapping[str, Any],
    params: TrustParams,
) -> Dict[str, Any]:
    """Facts, conflicts and review items for one case.

    Args:
        evidence: Every stored evidence row of the case.
        relevance: ``document_id -> score`` (stage 1).
        decisions: ``conflict_id | fact_key -> {action, match}`` (or a bare
            action); ``match`` re-finds the decision if the keys moved.
        params: Thresholds.

    Returns:
        ``{facts, conflicts, reviews, stats}``; lists sorted by key, so two
        evaluations of the same input are identical.
    """
    rows = canonicalise_rows(evidence)
    asserted: Dict[str, List[Mapping[str, Any]]] = {}
    denied: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        (denied if row.get("denial") else asserted).setdefault(row["fact_key"], []).append(row)

    facts = [_fold(key, asserted[key], relevance, params) for key in sorted(asserted)]
    denials = {key: _fold(key, denied[key], relevance, params) for key in sorted(denied)}

    conflicts, conflict_stats = conflict_engine.detect(facts, denials)
    state = apply_conflict_decisions(conflicts, decisions)

    conflict_ids: Dict[str, List[str]] = {}
    for conflict in conflicts:
        if conflict["resolution_status"] != "unresolved":
            continue
        for key in (conflict["fact_a"], conflict["fact_b"]):
            if key:
                conflict_ids.setdefault(key, []).append(conflict["conflict_id"])

    # A disputed existence disputes everything said about that subject: the
    # amount of a payment one document denies was ever made is not "trusted".
    disputed_subjects: Dict[tuple, List[str]] = {}
    for fact in facts:
        claim = fact["claim"]
        if claim.get("attribute") == EXISTS and fact["fact_key"] in conflict_ids:
            subject = (claim.get("label"), claim.get("subject_key"))
            disputed_subjects.setdefault(subject, []).extend(conflict_ids[fact["fact_key"]])
    for fact in facts:
        claim = fact["claim"]
        inherited = disputed_subjects.get((claim.get("label"), claim.get("subject_key")))
        if inherited and claim.get("attribute") != EXISTS:
            conflict_ids.setdefault(fact["fact_key"], []).extend(inherited)

    for fact in facts:
        key = fact["fact_key"]
        fact["subjects"] = sorted(conflict_engine.subject_variants(fact))
        fact["decision"] = fact_decision(fact, fact["subjects"], decisions)
        fact.update(
            route(
                fact["corroboration"],
                in_unresolved_conflict=key in state["unresolved"] or key in conflict_ids,
                lost_conflict=key in state["losers"],
                decision=fact["decision"] or None,
                bands=params.bands,
            )
        )
        fact["conflict_ids"] = sorted(conflict_ids.get(key, ()))

    reviews = review_items(facts, decisions)
    by_status: Dict[str, int] = {}
    for fact in facts:
        by_status[fact["status"]] = by_status.get(fact["status"], 0) + 1
    by_kind: Dict[str, int] = {}
    for conflict in conflicts:
        by_kind[conflict["conflict_kind"]] = by_kind.get(conflict["conflict_kind"], 0) + 1
    return {
        "facts": facts,
        "conflicts": conflicts,
        "reviews": reviews,
        "stats": {
            "evidence": len(rows),
            "facts": len(facts),
            "denials": len(denials),
            "byStatus": by_status,
            "conflicts": len(conflicts),
            "conflictsByKind": by_kind,
            "unresolvedConflicts": sum(
                1 for c in conflicts if c["resolution_status"] == "unresolved"
            ),
            "reviews": len(reviews),
            **conflict_stats,
        },
    }
