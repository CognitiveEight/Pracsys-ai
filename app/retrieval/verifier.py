"""
The claim verifier — unsupported statements do not ship (work-plan task 3.8).

Section 5.3 is explicit that this is *"a pipeline stage with authority to modify
output, not a prompt instruction"*. That distinction is the whole point: a
prompt asking the model to cite correctly produces a model that usually cites
correctly. A stage that removes uncited claims produces the enforced property
the acceptance criteria measure (metric 10.1: claim traceability ≥ 95%).

Each generated claim is matched against the assembled evidence. Three outcomes:

* **supported** — the claim cites a chunk that retrieval actually returned, and
  the claim's content overlaps that chunk.
* **unverified** — the claim has no supporting evidence, but the panel's
  contract permits an explicitly unverified item. Demoted and flagged.
* **dropped** — no support and no place to put an unverified item. Removed.

A claim citing a chunk that was *never retrieved* is treated as unsupported, not
supported. That case is a fabricated citation, and accepting it would make the
traceability metric measure the model's willingness to emit chunk ids rather
than whether the claim is grounded.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Set

from loguru import logger

#: Fraction of a claim's content words that must appear in a candidate chunk for
#: that chunk to count as supporting it. Deliberately low: a claim legitimately
#: paraphrases and compresses its source, and demanding high overlap would drop
#: correct claims. It is a guard against a citation pointing somewhere unrelated,
#: not a paraphrase detector.
DEFAULT_OVERLAP = 0.30

#: Content words a claim must share with a passage before the ratio is trusted.
#: A ratio alone is the wrong guard for a short claim: "the accused absconded to
#: Dubai" shares one word with a passage about the accused and scores 33%, which
#: would clear the threshold on a single incidental word. A claim with fewer
#: content words than this is held to exact containment instead.
MIN_SHARED_CONTENT_WORDS = 2

#: Numeric tokens are load-bearing in legal text: a section number, a quantum, a
#: year. A claim asserting "section 420" is NOT supported by a passage that says
#: "section 318", however much prose the two share — and getting that wrong is
#: precisely the failure this programme exists to prevent. Any number in the
#: claim must therefore appear in the passage for it to count as support.
_NUMERIC_TOKEN = re.compile(r"\b\d[\d,./-]*\b")

#: Words carrying no evidential weight, excluded from overlap.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "their",
        "his",
        "her",
        "they",
        "he",
        "she",
        "we",
        "you",
        "i",
        "not",
        "no",
        "nor",
        "so",
        "such",
        "can",
        "could",
        "may",
        "might",
        "must",
        "shall",
        "should",
        "will",
        "would",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
    }
)


def _content_words(text: str) -> Set[str]:
    """Lowercased content words, stopwords and short tokens removed."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _numbers(text: str) -> Set[str]:
    """Numeric tokens in the text, normalised so ``4,50,000`` matches ``450000``."""
    return {
        match.group(0).replace(",", "").rstrip(".")
        for match in _NUMERIC_TOKEN.finditer(str(text or ""))
    }


def numbers_are_consistent(claim: str, passage: str) -> bool:
    """
    True unless the claim asserts a number the passage does not contain.

    This is the guard that stops "Section 420 applies" being accepted on the
    strength of a passage about section 318: the prose overlaps almost entirely,
    and only the number distinguishes a correct citation from a wrong one.

    A claim with no numbers is trivially consistent.
    """
    claim_numbers = _numbers(claim)
    if not claim_numbers:
        return True
    return claim_numbers <= _numbers(passage)


def overlap_ratio(claim: str, passage: str) -> float:
    """
    Fraction of the claim's content words present in the passage.

    Returns 0.0 in two disqualifying cases rather than scoring them down: when
    the claim asserts a number absent from the passage (see
    :func:`numbers_are_consistent` — a wrong section number is wrong no matter
    how well the prose matches), and when fewer than
    :data:`MIN_SHARED_CONTENT_WORDS` words are shared, which is coincidence
    rather than support.

    Args:
        claim: The generated statement.
        passage: A candidate supporting passage.

    Returns:
        A value in [0, 1]; 0 when the claim has no content words.
    """
    if not numbers_are_consistent(claim, passage):
        return 0.0
    claim_words = _content_words(claim)
    if not claim_words:
        return 0.0
    shared = claim_words & _content_words(passage)
    if len(shared) < min(MIN_SHARED_CONTENT_WORDS, len(claim_words)):
        return 0.0
    return len(shared) / len(claim_words)


@dataclass
class VerifiedClaim:
    """
    One claim after verification.

    Attributes:
        text: The claim.
        status: ``supported`` | ``unverified`` | ``dropped``.
        citations: Chunk ids that support it. Empty unless supported.
        best_overlap: Strongest overlap found, for diagnosis.
        reason: Why it ended up in this state.
    """

    text: str
    status: str
    citations: List[str] = field(default_factory=list)
    best_overlap: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "citations": self.citations,
            "bestOverlap": round(self.best_overlap, 4),
            "reason": self.reason,
        }


def verify_claims(
    claims: Sequence[Any],
    evidence: Sequence[Mapping[str, Any]],
    allow_unverified: bool = False,
    overlap_threshold: float = DEFAULT_OVERLAP,
) -> Dict[str, Any]:
    """
    Verify generated claims against the assembled evidence.

    Args:
        claims: Claim strings, or dicts with ``text`` and optional ``citations``.
        evidence: The assembled evidence (``assemble()['evidence']``).
        allow_unverified: When True, an unsupported claim is demoted to
            ``unverified`` and kept; when False it is dropped. Set per panel —
            some payload contracts have somewhere to put a flagged item and some
            do not.
        overlap_threshold: Content-word overlap required for support.

    Returns:
        ``{claims, supported, unverified, dropped, traceability, citationPayload}``
        where ``traceability`` is the metric-10.1 figure for this generation.
    """
    by_chunk = {str(item.get("chunkId") or ""): item for item in evidence}
    available: Set[str] = set(by_chunk) - {""}

    results: List[VerifiedClaim] = []
    for raw in claims:
        if isinstance(raw, Mapping):
            text = str(raw.get("text") or "")
            claimed = [str(c) for c in (raw.get("citations") or [])]
        else:
            text, claimed = str(raw or ""), []
        if not text.strip():
            continue

        # A cited chunk that retrieval never returned is a fabricated citation:
        # ignore it rather than let it confer support.
        fabricated = [c for c in claimed if c not in available]
        honoured = [c for c in claimed if c in available]

        best_overlap = 0.0
        supporting: List[str] = []
        search_space = honoured or list(available)
        for chunk_id in search_space:
            item = by_chunk.get(chunk_id)
            if not item:
                continue
            ratio = overlap_ratio(text, str(item.get("text") or ""))
            best_overlap = max(best_overlap, ratio)
            if ratio >= overlap_threshold:
                supporting.append(chunk_id)

        if supporting:
            reason = "matched against retrieved evidence"
            if fabricated:
                reason += f"; {len(fabricated)} cited chunk(s) were never retrieved and ignored"
            results.append(
                VerifiedClaim(text, "supported", sorted(set(supporting)), best_overlap, reason)
            )
            continue

        if fabricated and not honoured:
            reason = (
                f"cited {len(fabricated)} chunk(s) that retrieval never returned; "
                "treated as unsupported"
            )
        else:
            mismatched = [
                chunk_id
                for chunk_id in search_space
                if chunk_id in by_chunk
                and not numbers_are_consistent(text, str(by_chunk[chunk_id].get("text") or ""))
            ]
            if mismatched and best_overlap == 0.0:
                reason = (
                    "the claim asserts a number (section, amount or date) that no "
                    "retrieved passage contains"
                )
            else:
                reason = (
                    f"no retrieved passage overlaps this claim "
                    f"(best {best_overlap:.0%}, need {overlap_threshold:.0%})"
                )

        if allow_unverified:
            results.append(VerifiedClaim(text, "unverified", [], best_overlap, reason))
        else:
            results.append(VerifiedClaim(text, "dropped", [], best_overlap, reason))

    supported = [c for c in results if c.status == "supported"]
    unverified = [c for c in results if c.status == "unverified"]
    dropped = [c for c in results if c.status == "dropped"]

    if dropped:
        logger.warning("Claim verifier removed {} unsupported claim(s) before emit", len(dropped))
    if unverified:
        logger.info("Claim verifier demoted {} claim(s) to unverified", len(unverified))

    return {
        "claims": [c.to_dict() for c in results],
        "supported": len(supported),
        "unverified": len(unverified),
        "dropped": len(dropped),
        "traceability": round(len(supported) / len(results), 4) if results else 1.0,
        "citationPayload": [
            {
                "claim": claim.text,
                "citations": [
                    {
                        "documentId": by_chunk[chunk_id].get("documentId"),
                        "chunkId": chunk_id,
                        "page": by_chunk[chunk_id].get("page"),
                    }
                    for chunk_id in claim.citations
                    if chunk_id in by_chunk
                ],
            }
            for claim in supported
        ],
        "survivingClaims": [c.text for c in results if c.status != "dropped"],
    }
