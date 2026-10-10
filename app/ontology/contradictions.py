"""
Contradiction detection — conflicts between documents, found structurally
(work-plan task 2.9).

Section 5.2 lists "do any documents contradict each other?" as a question only
the graph can answer: it needs *resolved* facts compared across documents, which
is exactly what the ontology produces and a similarity search cannot. Section 1
promises contradictions are "detected structurally and surfaced as explicit
findings" — so this module emits ``CONTRADICTS`` edges, not prose.

Four detectors, each looking at one kind of disagreement:

* **date** — the same event placed on two different days.
* **quantum** — the same claim or transaction at two different amounts.
* **status** — the same obligation reported both discharged and outstanding.
* **party_role** — the same party cast as complainant in one document and
  accused in another.

Two design rules that keep the output usable:

**A conflict needs two different sources.** Two facts from the same document
disagreeing is a parsing artefact or a document that contradicts itself in
drafting; either way it is not a cross-document contradiction and this module
does not report it as one.

**Imprecision is not disagreement.** "March 2025" and "14 March 2025" do not
conflict — one contains the other. The date detector compares the ranges from
:mod:`app.ontology.temporal`, so only genuinely disjoint dates are reported.
Reporting overlap as conflict would bury the real findings.
"""

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from app.ontology.entity_resolution import normalize_name
from app.ontology.temporal import parse_date

#: Values of the CONTRADICTS edge's ``conflict_kind`` property.
CONFLICT_DATE = "date"
CONFLICT_QUANTUM = "quantum"
CONFLICT_STATUS = "status"
CONFLICT_PARTY = "party"
CONFLICT_TEXT = "text"

#: Obligation/relief status words that cannot both be true of one obligation.
_STATUS_GROUPS = (
    frozenset({"discharged", "satisfied", "complied", "performed", "paid", "closed", "fulfilled"}),
    frozenset(
        {"outstanding", "pending", "unpaid", "breached", "defaulted", "not complied", "due"}
    ),
)

#: Adversarial role pairs. One party holding both across two documents is a real
#: finding — usually a mis-extraction, occasionally a genuinely disputed record.
_OPPOSED_ROLES = (
    frozenset({"complainant", "accused"}),
    frozenset({"petitioner", "respondent"}),
    frozenset({"plaintiff", "defendant"}),
    frozenset({"appellant", "respondent"}),
    frozenset({"claimant", "opposite party"}),
)

_AMOUNT = re.compile(
    r"(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d{1,2})?)\s*(lakh|lakhs|crore|crores)?", re.IGNORECASE
)


@dataclass
class Contradiction:
    """
    One detected conflict, ready to be written as a CONTRADICTS edge.

    Attributes:
        conflict_kind: One of the CONFLICT_* constants; the edge property.
        detector: Which detector raised it; the edge property.
        subject: What the two facts are both about.
        left / right: The two conflicting facts, each with its provenance.
        explanation: Why these two cannot both be true, in one sentence.
    """

    conflict_kind: str
    detector: str
    subject: str
    left: Dict[str, Any]
    right: Dict[str, Any]
    explanation: str

    def to_edge(self, workspace_id: str) -> Dict[str, Any]:
        """
        Render as a CONTRADICTS edge payload.

        Provenance points at the *left* fact's source, since an edge carries one
        provenance block; the right-hand source is preserved in the properties
        so the finding stays two-sided and auditable.
        """
        return {
            "type": "CONTRADICTS",
            "from": {"label": self.left.get("label"), "key": self.left.get("key")},
            "to": {"label": self.right.get("label"), "key": self.right.get("key")},
            "properties": {
                "conflict_kind": self.conflict_kind,
                "detector": self.detector,
                "workspace_id": workspace_id,
                "source_document_id": self.left.get("source_document_id"),
                "chunk_id": self.left.get("chunk_id"),
                "page": self.left.get("page"),
                "extraction_confidence": min(
                    float(self.left.get("confidence", 0.5) or 0.5),
                    float(self.right.get("confidence", 0.5) or 0.5),
                ),
            },
            "explanation": self.explanation,
            "counterpartSource": self.right.get("source_document_id"),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_kind": self.conflict_kind,
            "detector": self.detector,
            "subject": self.subject,
            "left": self.left,
            "right": self.right,
            "explanation": self.explanation,
        }


def _different_documents(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two facts come from different source documents."""
    a, b = left.get("source_document_id"), right.get("source_document_id")
    return bool(a) and bool(b) and a != b


def parse_amount(text: Any) -> Optional[float]:
    """
    Parse an Indian-format monetary amount into rupees.

    Handles ``"Rs. 4,50,000"``, ``"₹4.5 lakh"``, ``"INR 2 crore"`` and bare
    numbers. Lakh/crore multipliers are applied, so ``"4.5 lakh"`` and
    ``"450000"`` compare equal rather than reading as a conflict.

    Args:
        text: The amount as written, or a number.

    Returns:
        The value in rupees, or None if no amount is present.
    """
    if isinstance(text, (int, float)):
        return float(text)
    match = _AMOUNT.search(str(text or ""))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    if unit.startswith("lakh"):
        value *= 100_000
    elif unit.startswith("crore"):
        value *= 10_000_000
    return value


def _date_ranges_disjoint(left_raw: Any, right_raw: Any) -> Optional[Tuple[str, str]]:
    """
    Return the two ISO dates when they genuinely disagree, else None.

    None covers both "same date" and "one contains the other" — a month-precision
    date overlapping a day-precision one is imprecision, not contradiction.
    """
    left, right = parse_date(str(left_raw or "")), parse_date(str(right_raw or ""))
    if left is None or right is None:
        return None
    if left.range_end < right.range_start or right.range_end < left.range_start:
        return left.iso, right.iso
    return None


def detect_date_conflicts(events: Sequence[Mapping[str, Any]]) -> List[Contradiction]:
    """
    Find the same event placed on two disjoint dates by two documents.

    Events are matched by normalised description, so paraphrase across documents
    still pairs up. Only disjoint date ranges are reported.

    Args:
        events: Items with ``description``, ``date`` and provenance fields.

    Returns:
        The contradictions found.
    """
    by_subject: Dict[str, List[Mapping[str, Any]]] = {}
    for event in events:
        subject = normalize_name(str(event.get("description") or ""))
        if subject:
            by_subject.setdefault(subject, []).append(event)

    found: List[Contradiction] = []
    for _subject, group in by_subject.items():
        for left, right in combinations(group, 2):
            if not _different_documents(left, right):
                continue
            disagreement = _date_ranges_disjoint(left.get("date"), right.get("date"))
            if disagreement is None:
                continue
            left_iso, right_iso = disagreement
            found.append(
                Contradiction(
                    conflict_kind=CONFLICT_DATE,
                    detector="date",
                    subject=str(left.get("description")),
                    left={**left, "label": "Event", "key": left_iso},
                    right={**right, "label": "Event", "key": right_iso},
                    explanation=(
                        f"'{left.get('description')}' is dated {left_iso} in "
                        f"{left.get('source_document_id')} but {right_iso} in "
                        f"{right.get('source_document_id')}; the two dates cannot both hold."
                    ),
                )
            )
    return found


def detect_quantum_conflicts(
    claims: Sequence[Mapping[str, Any]], tolerance: float = 0.01
) -> List[Contradiction]:
    """
    Find the same claim or transaction stated at two different amounts.

    Args:
        claims: Items with ``subject``, ``amount`` and provenance fields.
        tolerance: Relative difference treated as the same figure, absorbing
            rounding between "4.5 lakh" and "450,000".

    Returns:
        The contradictions found.
    """
    by_subject: Dict[str, List[Mapping[str, Any]]] = {}
    for claim in claims:
        subject = normalize_name(str(claim.get("subject") or ""))
        if subject:
            by_subject.setdefault(subject, []).append(claim)

    found: List[Contradiction] = []
    for _subject, group in by_subject.items():
        for left, right in combinations(group, 2):
            if not _different_documents(left, right):
                continue
            left_amount, right_amount = (
                parse_amount(left.get("amount")),
                parse_amount(right.get("amount")),
            )
            if left_amount is None or right_amount is None:
                continue
            largest = max(abs(left_amount), abs(right_amount)) or 1.0
            if abs(left_amount - right_amount) / largest <= tolerance:
                continue
            found.append(
                Contradiction(
                    conflict_kind=CONFLICT_QUANTUM,
                    detector="quantum",
                    subject=str(left.get("subject")),
                    left={**left, "label": "Relief", "key": f"{left_amount:.2f}"},
                    right={**right, "label": "Relief", "key": f"{right_amount:.2f}"},
                    explanation=(
                        f"'{left.get('subject')}' is stated as {left_amount:,.2f} in "
                        f"{left.get('source_document_id')} but {right_amount:,.2f} in "
                        f"{right.get('source_document_id')}."
                    ),
                )
            )
    return found


def _status_group(status: str) -> Optional[int]:
    normalized = " ".join(str(status or "").lower().split())
    for index, group in enumerate(_STATUS_GROUPS):
        if any(word in normalized for word in group):
            return index
    return None


def detect_status_conflicts(obligations: Sequence[Mapping[str, Any]]) -> List[Contradiction]:
    """
    Find one obligation reported both discharged and outstanding.

    Args:
        obligations: Items with ``subject``, ``status`` and provenance fields.

    Returns:
        The contradictions found.
    """
    by_subject: Dict[str, List[Mapping[str, Any]]] = {}
    for obligation in obligations:
        subject = normalize_name(str(obligation.get("subject") or ""))
        if subject:
            by_subject.setdefault(subject, []).append(obligation)

    found: List[Contradiction] = []
    for _subject, group in by_subject.items():
        for left, right in combinations(group, 2):
            if not _different_documents(left, right):
                continue
            left_group, right_group = (
                _status_group(left.get("status")),
                _status_group(right.get("status")),
            )
            if left_group is None or right_group is None or left_group == right_group:
                continue
            found.append(
                Contradiction(
                    conflict_kind=CONFLICT_STATUS,
                    detector="status",
                    subject=str(left.get("subject")),
                    left={**left, "label": "Obligation", "key": str(left.get("status"))},
                    right={**right, "label": "Obligation", "key": str(right.get("status"))},
                    explanation=(
                        f"'{left.get('subject')}' is '{left.get('status')}' in "
                        f"{left.get('source_document_id')} but '{right.get('status')}' in "
                        f"{right.get('source_document_id')}."
                    ),
                )
            )
    return found


def detect_party_role_conflicts(parties: Sequence[Mapping[str, Any]]) -> List[Contradiction]:
    """
    Find one party cast in two opposed roles across documents.

    Matching is on the **canonical** name, so this runs after entity resolution
    — before it, two spellings of one party would never be compared.

    Args:
        parties: Items with ``canonical_name``, ``party_role`` and provenance.

    Returns:
        The contradictions found.
    """
    by_party: Dict[str, List[Mapping[str, Any]]] = {}
    for party in parties:
        name = normalize_name(str(party.get("canonical_name") or ""))
        if name:
            by_party.setdefault(name, []).append(party)

    found: List[Contradiction] = []
    for _name, group in by_party.items():
        for left, right in combinations(group, 2):
            if not _different_documents(left, right):
                continue
            left_role = " ".join(str(left.get("party_role") or "").lower().split())
            right_role = " ".join(str(right.get("party_role") or "").lower().split())
            if not left_role or not right_role or left_role == right_role:
                continue
            if not any({left_role, right_role} <= pair for pair in _OPPOSED_ROLES):
                continue
            found.append(
                Contradiction(
                    conflict_kind=CONFLICT_PARTY,
                    detector="party_role",
                    subject=str(left.get("canonical_name")),
                    left={**left, "label": "Party", "key": str(left.get("canonical_name"))},
                    right={**right, "label": "Party", "key": str(right.get("canonical_name"))},
                    explanation=(
                        f"'{left.get('canonical_name')}' is the {left_role} in "
                        f"{left.get('source_document_id')} but the {right_role} in "
                        f"{right.get('source_document_id')}."
                    ),
                )
            )
    return found


def detect_contradictions(
    workspace_id: str,
    events: Sequence[Mapping[str, Any]] = (),
    claims: Sequence[Mapping[str, Any]] = (),
    obligations: Sequence[Mapping[str, Any]] = (),
    parties: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """
    Run every detector and return CONTRADICTS edges plus a summary.

    Args:
        workspace_id: Written onto every emitted edge.
        events: For the date detector.
        claims: For the quantum detector.
        obligations: For the status detector.
        parties: For the party-role detector (post entity resolution).

    Returns:
        ``{contradictions, edges, countsByKind, total}``.
    """
    found: List[Contradiction] = [
        *detect_date_conflicts(events),
        *detect_quantum_conflicts(claims),
        *detect_status_conflicts(obligations),
        *detect_party_role_conflicts(parties),
    ]

    counts: Dict[str, int] = {}
    for item in found:
        counts[item.conflict_kind] = counts.get(item.conflict_kind, 0) + 1

    if found:
        logger.info(
            "Contradiction detection found {} cross-document conflicts: {}",
            len(found),
            counts,
        )
    return {
        "contradictions": [c.to_dict() for c in found],
        "edges": [c.to_edge(workspace_id) for c in found],
        "countsByKind": counts,
        "total": len(found),
    }
