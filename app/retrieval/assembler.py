"""
Evidence assembly — dedupe, order, budget, and attach provenance (task 3.6).

Section 5.1 step 4. Between retrieval and generation sits the step that decides
what the model actually sees: candidates arrive from three modalities, overlap,
and together exceed any sane token budget.

Three properties this step must guarantee, because everything downstream assumes
them:

* **Every passage carries provenance.** A passage without ``document_id`` and
  ``chunk_id`` cannot be cited, so :func:`assemble` drops it and says so. The
  claim verifier and the citation payload are both lookups over this — Section
  5.3's "attaching a source to a claim is a lookup rather than a second
  inference" is only true if this holds.
* **The budget is a hard cap.** Exceeding it does not degrade output, it
  truncates the prompt somewhere arbitrary. Assembly stops at the cap and
  reports what it dropped.
* **Dropping is reported, never silent.** ``droppedForBudget`` is what tells the
  coverage critic that thin support might be a budget problem rather than a
  retrieval one.

Ordering interleaves the sub-questions rather than concatenating them, so a
sub-question whose evidence sorts late is not starved by the budget.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

#: Characters per token. Rough, and deliberately conservative: assembly must not
#: overshoot the real budget, and a precise count needs the model's tokenizer.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for budgeting. Never returns 0 for non-empty text."""
    if not text:
        return 0
    return max(1, len(str(text)) // CHARS_PER_TOKEN)


@dataclass
class Evidence:
    """
    One retrieved element, ready to be cited.

    Attributes:
        text: The passage or the rendered graph fact.
        document_id / chunk_id / page: Provenance. The first two are mandatory.
        score: Relevance from retrieval or reranking; higher is better.
        modality: Which modality produced it.
        sub_question_key: Which sub-question it answers.
        metadata: Anything else the panel wants to carry through.
    """

    text: str
    document_id: str
    chunk_id: str
    page: Optional[int] = None
    score: float = 0.0
    modality: str = ""
    sub_question_key: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_provenance(self) -> bool:
        return bool(self.document_id) and bool(self.chunk_id)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def citation(self) -> Dict[str, Any]:
        """The citation payload for a claim supported by this evidence."""
        return {
            "documentId": self.document_id,
            "chunkId": self.chunk_id,
            "page": self.page,
            "modality": self.modality,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "documentId": self.document_id,
            "chunkId": self.chunk_id,
            "page": self.page,
            "score": round(float(self.score), 4),
            "modality": self.modality,
            "subQuestionKey": self.sub_question_key,
            **({"metadata": self.metadata} if self.metadata else {}),
        }


def from_chunk(
    chunk: Mapping[str, Any], modality: str, sub_question_key: str = ""
) -> Optional[Evidence]:
    """
    Build an :class:`Evidence` from a retrieved chunk.

    Args:
        chunk: Retrieval hit with the passage under ``text``, ``chunk_text`` or
            ``excerpt`` (Keel's name), plus ``document_id``, ``chunk_id`` and
            optionally ``page`` and ``score``/``similarity``.
        modality: Which modality produced it.
        sub_question_key: Which sub-question it answers.

    Returns:
        The evidence, or None when it lacks provenance — an uncitable passage is
        worse than no passage, because it makes an unverifiable claim look
        supported.
    """
    # `excerpt` is Keel's name for the passage (schemas/search.py::EvidenceItem);
    # without it every Keel hit had no text and was dropped as unciteable.
    text = str(
        chunk.get("text") or chunk.get("chunk_text") or chunk.get("excerpt") or ""
    ).strip()
    document_id = str(chunk.get("document_id") or chunk.get("documentId") or "")
    chunk_id = str(chunk.get("chunk_id") or chunk.get("chunkId") or "")
    if not text or not document_id or not chunk_id:
        return None
    page = chunk.get("page")
    return Evidence(
        text=text,
        document_id=document_id,
        chunk_id=chunk_id,
        page=int(page) if isinstance(page, (int, float)) else None,
        score=float(chunk.get("score") or chunk.get("similarity") or 0.0),
        modality=modality,
        sub_question_key=sub_question_key,
        metadata={
            k: v
            for k, v in chunk.items()
            if k
            not in {
                "text",
                "chunk_text",
                "excerpt",
                "document_id",
                "documentId",
                "chunk_id",
                "chunkId",
                "page",
                "score",
                "similarity",
            }
        },
    )


def render_graph_fact(row: Mapping[str, Any]) -> str:
    """
    Render a graph row as a compact factual line for the prompt.

    Provenance keys are excluded from the rendered text — they travel as
    structure, and repeating them in the prose wastes budget without helping the
    model.
    """
    skip = {"source_document_id", "chunk_id", "page", "workspace_id", "extraction_confidence"}
    parts = [
        f"{key}={value}"
        for key, value in row.items()
        if key not in skip and value not in (None, "", [], {})
    ]
    return "; ".join(parts)


def from_graph_row(
    row: Mapping[str, Any], sub_question_key: str = "", score: float = 1.0
) -> Optional[Evidence]:
    """
    Build an :class:`Evidence` from a graph query row.

    Graph facts default to ``score=1.0``: they are exact structural matches, not
    ranked similarities, and scoring them below a fuzzy vector hit would let the
    budget evict the precise answer in favour of an approximate one.
    """
    document_id = str(row.get("source_document_id") or "")
    chunk_id = str(row.get("chunk_id") or "")
    if not document_id or not chunk_id:
        return None
    text = render_graph_fact(row)
    if not text:
        return None
    page = row.get("page")
    return Evidence(
        text=text,
        document_id=document_id,
        chunk_id=chunk_id,
        page=int(page) if isinstance(page, (int, float)) else None,
        score=score,
        modality="graph",
        sub_question_key=sub_question_key,
    )


def _interleave(groups: Mapping[str, List[Evidence]]) -> List[Evidence]:
    """
    Round-robin across sub-questions, best-first within each.

    Straight score ordering would let one prolific sub-question consume the
    whole budget; interleaving guarantees every sub-question gets its best
    evidence in before any gets its second-best.
    """
    ordered_groups = [sorted(items, key=lambda e: -e.score) for items in groups.values()]
    result: List[Evidence] = []
    index = 0
    while True:
        added = False
        for group in ordered_groups:
            if index < len(group):
                result.append(group[index])
                added = True
        if not added:
            return result
        index += 1


def assemble(
    candidates: Sequence[Evidence],
    token_budget: int,
    min_score: float = 0.0,
) -> Dict[str, Any]:
    """
    Deduplicate, order, budget and finalise the evidence set.

    Args:
        candidates: Everything the modalities returned.
        token_budget: Hard cap on assembled evidence tokens.
        min_score: Drop candidates scoring below this before budgeting.

    Returns:
        ``{evidence, tokensUsed, tokenBudget, droppedForBudget,
        droppedNoProvenance, droppedLowScore, deduplicated, coverageByKey}``.
    """
    without_provenance = [c for c in candidates if not c.has_provenance]
    usable = [c for c in candidates if c.has_provenance]

    low_score = [c for c in usable if c.score < min_score]
    usable = [c for c in usable if c.score >= min_score]

    # Deduplicate on (chunk_id, text), keeping the highest-scoring occurrence.
    #
    # The key deliberately includes the text. Keying on chunk_id alone is right
    # for a vector passage — one chunk is one passage, and a chunk that answers
    # two sub-questions should be paid for once and credited to both — but it is
    # badly wrong for graph facts, where one chunk legitimately yields many
    # distinct facts. On a real matter it collapsed 11 retrieved rows (7 parties,
    # 3 reliefs, an obligation) into 3 items and handed the panel 645 characters
    # against a 12,000-token budget, discarding almost everything the graph had
    # produced. Including the text keeps vector behaviour identical — the text
    # *is* the chunk — while letting distinct facts from one passage survive.
    best_by_key: Dict[tuple, Evidence] = {}
    keys_by_fact: Dict[tuple, List[str]] = {}
    for candidate in usable:
        fact_key = (candidate.chunk_id, candidate.text.strip())
        keys_by_fact.setdefault(fact_key, []).append(candidate.sub_question_key)
        existing = best_by_key.get(fact_key)
        if existing is None or candidate.score > existing.score:
            best_by_key[fact_key] = candidate
    deduplicated = len(usable) - len(best_by_key)

    grouped: Dict[str, List[Evidence]] = {}
    for evidence in best_by_key.values():
        grouped.setdefault(evidence.sub_question_key, []).append(evidence)

    selected: List[Evidence] = []
    dropped_for_budget: List[Evidence] = []
    used = 0
    for evidence in _interleave(grouped):
        cost = evidence.tokens
        if used + cost > token_budget:
            dropped_for_budget.append(evidence)
            continue
        selected.append(evidence)
        used += cost

    coverage: Dict[str, int] = {}
    for evidence in selected:
        fact_key = (evidence.chunk_id, evidence.text.strip())
        for key in set(keys_by_fact.get(fact_key, [evidence.sub_question_key])):
            coverage[key] = coverage.get(key, 0) + 1

    if without_provenance:
        logger.warning(
            "{} retrieved items had no provenance and were dropped — they could "
            "not have been cited",
            len(without_provenance),
        )
    if dropped_for_budget:
        logger.info(
            "Evidence assembly dropped {} items for the {}-token budget",
            len(dropped_for_budget),
            token_budget,
        )

    return {
        "evidence": [e.to_dict() for e in selected],
        "tokensUsed": used,
        "tokenBudget": token_budget,
        "droppedForBudget": len(dropped_for_budget),
        "droppedNoProvenance": len(without_provenance),
        "droppedLowScore": len(low_score),
        "deduplicated": deduplicated,
        "coverageByKey": coverage,
        # Unique: several distinct facts now legitimately share one chunk, and
        # this is a "was this chunk retrieved?" lookup for the claim verifier.
        "availableChunkIds": list(dict.fromkeys(e.chunk_id for e in selected)),
    }
