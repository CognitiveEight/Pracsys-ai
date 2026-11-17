"""
Stage 5 of the trust pipeline: the contradiction engine.

Runs over the **whole case** on every pass, not over one upload: a reply filed
today must be checked against the complaint filed last month. Four axes:

* **attribute** — one subject, one single-valued attribute, two values: a
  one-off event on two disjoint dates, a claim at two amounts, an obligation
  both discharged and outstanding, a case number with two years. Imprecision
  is not disagreement: "March 2025" contains "14 March 2025". A party's role
  is NOT compared: respondent below and appellant above is the ordinary course
  of an appeal, and cross-complaints make complainants accused.
* **relation** — one tail, two heads, for relations that admit only one head:
  a payment ``PAID_TO`` two different parties, a case ``FILED_BEFORE`` two
  forums, a filing ``FILED_BY`` two parties.
* **counsel** — the object-side mirror: one advocate ``REPRESENTED_BY`` two
  parties that one document places on opposed sides. Curated, never inferred:
  plenty of relations are legitimately shared by several subjects (co-accused
  share counsel).
* **denial** — a fact one document asserts and another denies ("received Rs.
  5,00,000" / "never received any amount"). The core disagreement of most
  litigation, and invisible to a comparison of values because the denial has
  no value: it negates the fact itself. A denial meets the assertion on its
  key, on a shared signature (a payment's date and amount when only the
  assertion names its reference), or, for an undated denial of a recurring
  event, on the event's wording.

**Succession is not conflict.** A time-varying attribute (an obligation's
status, a case's forum) stated "as of" two non-overlapping dates is a change
over time: outstanding in March and paid in June. A side with no ``as_of`` is
treated as unbounded, never as unknown, so it conflicts — an undated "paid"
against an undated "outstanding" is exactly the dispute a lawyer must see.

**A conflict needs two documents.** When the only evidence for both values is
one document, that is a drafting artefact or an extraction error, not a
cross-document contradiction.

Pairs are normalised (``fact_a < fact_b``) and the id is a hash of the pair, so
rediscovering a conflict from either side lands on the same record.
"""

import hashlib
from itertools import combinations
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from app.ontology.contradictions import _OPPOSED_ROLES
from app.trust.claims import (
    EXISTS,
    KIND_AMOUNT,
    KIND_BOOL,
    KIND_DATE,
    KIND_EXISTS,
    KIND_INT,
    KIND_NAME,
    KIND_RELATION,
    KIND_ROLE,
    KIND_STATUS,
)

AXIS_ATTRIBUTE = "attribute"
AXIS_RELATION = "relation"
AXIS_COUNSEL = "counsel"
AXIS_DENIAL = "denial"

#: Relations whose tail has exactly one head at a time.
SINGLE_VALUED_RELATIONS = frozenset({"PAID_TO", "FILED_BEFORE", "FILED_BY"})

#: Of those, the ones that can change over time (a transferred case).
TIME_VARYING_RELATIONS = frozenset({"FILED_BEFORE"})

#: Relative difference at or below which two amounts are one figure
#: ("4.5 lakh" vs "4,50,000.00" after rounding).
AMOUNT_TOLERANCE = 0.01

_PAIR_KIND = {
    KIND_DATE: "date",
    KIND_AMOUNT: "quantum",
    KIND_STATUS: "status",
    KIND_ROLE: "party",
    KIND_BOOL: "status",
    KIND_INT: "text",
    KIND_NAME: "text",
    KIND_RELATION: "party",
}


def conflict_id(fact_a: str, fact_b: str, axis: str) -> str:
    a, b = sorted((fact_a, fact_b))
    return hashlib.sha1(f"{a}\x1f{b}\x1f{axis}".encode("utf-8")).hexdigest()


def _docs(fact: Mapping[str, Any]) -> Set[str]:
    return set(fact.get("document_ids") or ())


def _two_documents(a: Iterable[str], b: Iterable[str]) -> bool:
    """True when some document supports one side and a different one the other."""
    a, b = set(a), set(b)
    return any(x != y for x in a for y in b)


def _values_disagree(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Do two values of one slot contradict each other?"""
    kind = a.get("kind")
    if a.get("value_key") == b.get("value_key"):
        return False
    if kind == KIND_DATE:
        if not (a.get("lo") and a.get("hi") and b.get("lo") and b.get("hi")):
            return False
        return a["hi"] < b["lo"] or b["hi"] < a["lo"]
    if kind == KIND_AMOUNT:
        x, y = a.get("number"), b.get("number")
        if x is None or y is None:
            return False
        largest = max(abs(x), abs(y)) or 1.0
        return abs(x - y) / largest > AMOUNT_TOLERANCE
    if kind == KIND_ROLE:
        return False  # see the module docstring
    return kind in (KIND_STATUS, KIND_BOOL, KIND_INT, KIND_NAME, KIND_RELATION)


def _validity(fact: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """The span a fact's evidence says it held over; None if any side is unbounded."""
    lows, highs = [], []
    for row in fact.get("evidence") or ():
        if not row.get("valid_from") or not row.get("valid_to"):
            return None
        lows.append(row["valid_from"])
        highs.append(row["valid_to"])
    if not lows:
        return None
    return min(lows), max(highs)


def is_succession(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Two values of a time-varying slot held over non-overlapping periods."""
    va, vb = _validity(a), _validity(b)
    if va is None or vb is None:
        return False
    return va[1] < vb[0] or vb[1] < va[0]


def _side(fact: Mapping[str, Any], denial: bool = False) -> Dict[str, Any]:
    claim = fact["claim"]
    return {
        "fact_key": fact["fact_key"],
        "value": "" if denial else claim.get("value", ""),
        "value_key": "denial" if denial else claim.get("value_key", ""),
        "denial": denial,
        "sources": [
            {
                "document_id": row.get("document_id"),
                "page": row.get("page"),
                "chunk_id": row.get("chunk_id"),
                "span": row.get("span"),
            }
            for row in (fact.get("evidence") or ())
        ][:3],
        "document_ids": sorted(_docs(fact)),
    }


def _record(
    axis: str, a: Mapping[str, Any], b: Mapping[str, Any], *, b_is_denial: bool = False
) -> Dict[str, Any]:
    left, right = (a, b) if b_is_denial or a["fact_key"] <= b["fact_key"] else (b, a)
    claim = left["claim"]
    kind = "denial" if axis == AXIS_DENIAL else _PAIR_KIND.get(claim.get("kind"), "text")
    sides = [_side(left), _side(right, denial=b_is_denial)]
    return {
        "conflict_id": conflict_id(left["fact_key"], right["fact_key"], axis),
        "axis": axis,
        "conflict_kind": kind,
        "label": claim.get("label"),
        "subject": claim.get("subject"),
        "attribute": claim.get("attribute"),
        "fact_a": left["fact_key"],
        "fact_b": right["fact_key"],
        "sides": sides,
        "document_ids": sorted(_docs(left) | _docs(right)),
        # What extraction called the subject on either side: a reviewer's
        # decision is re-found by these if resolution later re-keys the facts.
        "subjects": sorted(subject_variants(left) | subject_variants(right)),
    }


def subject_variants(fact: Mapping[str, Any]) -> Set[str]:
    out = {str(fact["claim"].get("subject_key") or "")}
    for row in fact.get("evidence") or ():
        if row.get("subject_original"):
            out.add(str(row["subject_original"]))
    out.discard("")
    return out


def _by_slot(facts: Sequence[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    slots: Dict[str, List[Mapping[str, Any]]] = {}
    for fact in facts:
        slots.setdefault(fact["slot_key"], []).append(fact)
    for group in slots.values():
        group.sort(key=lambda f: f["fact_key"])
    return slots


def _party_roles(facts: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Set[str]]]:
    """``party -> document -> roles``: a role means something only within the
    proceeding (document) that assigns it."""
    roles: Dict[str, Dict[str, Set[str]]] = {}
    for fact in facts:
        claim = fact["claim"]
        if claim.get("label") == "Party" and claim.get("attribute") == "party_role":
            for row in fact.get("evidence") or ():
                by_doc = roles.setdefault(claim["subject_key"], {})
                by_doc.setdefault(str(row.get("document_id")), set()).add(claim["value_key"])
    return roles


def _opposed(roles_a: Mapping[str, Set[str]], roles_b: Mapping[str, Set[str]]) -> bool:
    """Some document places the two parties on opposed sides.
    Two co-accused share a role, and ``{"accused"}`` is a subset of every pair
    that contains it, so equal roles are excluded first."""
    for doc in set(roles_a) & set(roles_b):
        if any(
            x != y and {x, y} <= pair
            for x in roles_a[doc]
            for y in roles_b[doc]
            for pair in _OPPOSED_ROLES
        ):
            return True
    return False


def _signatures(fact: Mapping[str, Any]) -> Set[str]:
    return {s for row in fact.get("evidence") or () for s in row.get("signatures") or ()}


def _denied_facts(
    denial: Mapping[str, Any], facts: Sequence[Mapping[str, Any]]
) -> List[Mapping[str, Any]]:
    """The asserted facts one denial contradicts (see the module docstring)."""
    from app.trust.canonical import TEXT_LABELS, same_text_subject

    claim = denial["claim"]
    sigs = _signatures(denial)
    undated = "@" not in str(claim.get("subject_key"))
    out = []
    for fact in facts:
        other = fact["claim"]
        if other.get("label") != claim.get("label") or other.get("attribute") != claim.get(
            "attribute"
        ):
            continue
        if fact["fact_key"] == denial["fact_key"] or (sigs & _signatures(fact)):
            out.append(fact)
        elif (
            claim.get("label") in TEXT_LABELS
            and undated
            and other.get("event_type") == claim.get("event_type")
            and same_text_subject(
                str(claim.get("subject_key")), str(other.get("subject_key")).partition("@")[0]
            )
        ):
            out.append(fact)
    return out


def detect(
    facts: Sequence[Mapping[str, Any]],
    denials: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Every conflict in the case.

    Args:
        facts: Corroborated facts (``fact_key``, ``slot_key``, ``claim``,
            ``evidence``, ``document_ids``), denial evidence excluded.
        denials: ``fact_key -> denial pseudo-fact`` (same shape), the denied
            statements, which are never facts themselves.

    Returns:
        ``(conflicts, stats)``; ``stats`` counts successions recognised.
    """
    found: Dict[str, Dict[str, Any]] = {}
    stats = {"successions": 0}

    def add(record: Dict[str, Any]) -> None:
        found.setdefault(record["conflict_id"], record)

    for group in _by_slot(facts).values():
        claim = group[0]["claim"]
        kind = claim.get("kind")
        if kind == KIND_EXISTS:
            continue
        if kind == KIND_RELATION and claim.get("attribute") not in SINGLE_VALUED_RELATIONS:
            continue
        time_varying = bool(claim.get("time_varying")) or (
            claim.get("attribute") in TIME_VARYING_RELATIONS
        )
        for a, b in combinations(group, 2):
            if not _values_disagree(a["claim"], b["claim"]):
                continue
            if not _two_documents(_docs(a), _docs(b)):
                continue
            if time_varying and is_succession(a, b):
                stats["successions"] += 1
                continue
            axis = AXIS_RELATION if kind == KIND_RELATION else AXIS_ATTRIBUTE
            add(_record(axis, a, b))

    # Object side, curated: one advocate for both sides.
    roles = _party_roles(facts)
    by_advocate: Dict[str, List[Mapping[str, Any]]] = {}
    for fact in facts:
        claim = fact["claim"]
        if claim.get("attribute") == "REPRESENTED_BY":
            by_advocate.setdefault(claim["value_key"], []).append(fact)
    for group in by_advocate.values():
        group.sort(key=lambda f: f["fact_key"])
        for a, b in combinations(group, 2):
            party_a = a["claim"]["subject_key"].partition(":")[2]
            party_b = b["claim"]["subject_key"].partition(":")[2]
            if party_a == party_b:
                continue
            if _opposed(roles.get(party_a, {}), roles.get(party_b, {})):
                add(_record(AXIS_COUNSEL, a, b))

    # Denials: asserted by one document, denied by another.
    for key in sorted(denials):
        denial = denials[key]
        for fact in _denied_facts(denial, facts):
            if not _two_documents(_docs(fact), _docs(denial)):
                continue
            record = _record(AXIS_DENIAL, fact, denial, b_is_denial=True)
            record["fact_b"] = ""
            record["conflict_id"] = conflict_id(fact["fact_key"], "denial:" + key, AXIS_DENIAL)
            add(record)

    conflicts = sorted(found.values(), key=lambda c: c["conflict_id"])
    return conflicts, stats


__all__ = [
    "AXIS_ATTRIBUTE",
    "AXIS_COUNSEL",
    "AXIS_DENIAL",
    "AXIS_RELATION",
    "EXISTS",
    "SINGLE_VALUED_RELATIONS",
    "conflict_id",
    "detect",
    "is_succession",
]
