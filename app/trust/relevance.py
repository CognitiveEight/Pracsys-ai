"""
Stage 1 of the trust pipeline: does this document belong in this case?

There is no declared domain to check against, so each document is compared
with a fingerprint *derived* from the case itself: the centroid of the other
documents' embeddings. A raw cosine is not usable on its own (unrelated legal
text still scores well above zero), so the score is calibrated against the
case's own distribution: how far this document sits from the others compared
with how far they typically sit from each other.

``score = min(1, 2 * Phi(z))`` with ``z`` the document's standardised
leave-one-out similarity. An average or better document scores 1.0, so a
normal document is never penalised; only an outlier falls towards 0.

**Log-only, on purpose.** The score never rejects or hides a document. The
centroid is self-reinforcing: if the first documents on a genuinely new aspect
of a case were gated for not resembling the earlier ones, the case would
narrow itself with every rejection. The score only discounts that document's
evidence in stage 4, never below ``TRUST_RELEVANCE_FLOOR``.

Below ``TRUST_RELEVANCE_MIN_DOCS`` documents there is nothing to calibrate
against and every document scores 1.0. A raw cosine is never used instead.
"""

import math
from typing import Dict, Mapping, Optional, Sequence

#: Spread below which the case's documents are effectively identical, and z is
#: meaningless.
_MIN_SPREAD = 1e-6


def mean_vector(vectors: Sequence[Sequence[float]]) -> Optional[list]:
    """Element-wise mean of equally sized vectors; None when there are none."""
    vectors = [v for v in vectors if v]
    if not vectors:
        return None
    size = len(vectors[0])
    vectors = [v for v in vectors if len(v) == size]
    return [sum(column) / len(vectors) for column in zip(*vectors)]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _leave_one_out(vectors: Mapping[str, Sequence[float]]) -> Dict[str, float]:
    """Each document's cosine to the centroid of all the others."""
    ids = sorted(vectors)
    size = len(vectors[ids[0]])
    total = [0.0] * size
    for doc_id in ids:
        for i, value in enumerate(vectors[doc_id]):
            total[i] += value
    n = len(ids)
    out = {}
    for doc_id in ids:
        others = [(t - v) / (n - 1) for t, v in zip(total, vectors[doc_id])]
        out[doc_id] = _cosine(vectors[doc_id], others)
    return out


def relevance_scores(
    vectors: Mapping[str, Sequence[float]], min_docs: int
) -> Dict[str, float]:
    """Calibrated 0-1 relevance for every document with a vector.

    Args:
        vectors: ``document_id -> mean chunk embedding``.
        min_docs: Documents needed before calibration; fewer all score 1.0.

    Returns:
        ``document_id -> score``. Deterministic for a given input.
    """
    usable = {k: list(v) for k, v in vectors.items() if v}
    if usable:
        size = len(next(iter(usable.values())))
        usable = {k: v for k, v in usable.items() if len(v) == size}
    if len(usable) < max(3, min_docs):
        return {doc_id: 1.0 for doc_id in vectors}

    sims = _leave_one_out(usable)
    scores: Dict[str, float] = {}
    for doc_id, sim in sims.items():
        others = [s for other, s in sims.items() if other != doc_id]
        mu = sum(others) / len(others)
        sd = math.sqrt(sum((s - mu) ** 2 for s in others) / len(others))
        if sd < _MIN_SPREAD:
            scores[doc_id] = 1.0 if sim >= mu - _MIN_SPREAD else 0.0
            continue
        scores[doc_id] = round(min(1.0, 2.0 * _phi((sim - mu) / sd)), 4)
    for doc_id in vectors:
        scores.setdefault(doc_id, 1.0)
    return scores
