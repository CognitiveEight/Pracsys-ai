"""
Stages 2-3 of the trust pipeline: extraction entries become canonical claims.

An extracted node is a legal entity with properties; corroboration and
contradiction need something narrower to compare: one ``(subject, attribute,
value)`` statement. ``claims_for_entry`` makes that projection, and every value
is reduced to a canonical form first ("4.5 lakh" and "Rs. 4,50,000" are one
amount, "M/s Meridian Traders Pvt Ltd" and "Meridian Traders" one name), so two
wordings of one fact land on one claim and corroborate it instead of counting
as two facts.

The modality gate lives here too. A hedged statement produces nothing. A
denied one produces claims marked ``denial``: they are compared against
assertions (that is a contradiction a lawyer needs to see) but never become
facts.
"""

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.ontology.contradictions import _OPPOSED_ROLES, _status_group, parse_amount
from app.ontology.entity_resolution import normalize_name
from app.ontology.extraction import (
    MODALITY_ALLEGED,
    MODALITY_ASSERTED,
    MODALITY_DENIED,
    MODALITY_HEDGED,
)
from app.ontology.schema import NODES_BY_LABEL
from app.ontology.temporal import parse_date

#: Claim kinds: how two values of one attribute are compared (conflicts.py).
KIND_EXISTS = "exists"
KIND_DATE = "date"
KIND_AMOUNT = "amount"
KIND_STATUS = "status"
KIND_ROLE = "role"
KIND_BOOL = "bool"
KIND_INT = "int"
KIND_NAME = "name"
KIND_RELATION = "relation"

#: Attribute name of the claim that an entity exists at all.
EXISTS = "exists"

#: Passage text kept around a fact, for copy detection and display.
SPAN_CHARS = 400

#: Properties whose value is a phrase from the passage — where the fact "is".
_ANCHOR_PROPS = (
    "canonical_name",
    "description",
    "reference",
    "relief_type",
    "operative_direction",
    "case_number",
    "citation",
    "obligation_type",
    "purpose",
)


@dataclass(frozen=True)
class Claim:
    """One canonical statement about the case.

    Attributes:
        label: Node label or edge type the claim was projected from.
        subject_key: Canonical subject; with ``attribute`` it names what the
            claim is about.
        subject: The subject as written (for display).
        attribute: Which property or relation.
        kind: One of the ``KIND_*`` constants.
        value_key: Canonical value; claims with equal keys corroborate.
        value: The value as written (for display).
        lo / hi: ISO date range for a date value.
        number: Numeric value for an amount or int.
        time_varying: The value may legitimately change over time, so two
            values with disjoint ``as_of`` dates are a succession.
        event_type: Event/Communication type, which canonicalisation must not
            merge across.
    """

    label: str
    subject_key: str
    subject: str
    attribute: str
    kind: str
    value_key: str
    value: str
    lo: str = ""
    hi: str = ""
    number: Optional[float] = None
    time_varying: bool = False
    event_type: str = ""

    @property
    def fact_key(self) -> str:
        return fact_key(self.label, self.subject_key, self.attribute, self.value_key)

    @property
    def slot_key(self) -> str:
        """What the claim is about, without its value: conflicts are per slot."""
        return fact_key(self.label, self.subject_key, self.attribute, "")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def fact_key(label: str, subject_key: str, attribute: str, value_key: str) -> str:
    """Stable identity of one fact (also the review/decision key)."""
    raw = "\x1f".join((label, subject_key, attribute, value_key))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def claim_from_dict(data: Mapping[str, Any]) -> Claim:
    fields = Claim.__dataclass_fields__
    return Claim(**{k: data.get(k) for k in fields if k in data})


# ---------------------------------------------------------------------------
# Value canonicalisation
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _key(value: Any) -> str:
    """Canonical key for free text: lowercased words, punctuation dropped."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _name_key(value: Any) -> str:
    return normalize_name(str(value or "")) or _key(value)


def _date_claim(value: Any) -> Optional[Tuple[str, str, str]]:
    """``(key, lo, hi)`` for a date, or None when it does not parse."""
    parsed = parse_date(str(value or ""))
    if parsed is None:
        return None
    lo, hi = parsed.range_start.isoformat(), parsed.range_end.isoformat()
    return f"{lo}..{hi}", lo, hi


def _role_key(value: Any) -> str:
    role = _key(value)
    for pair in _OPPOSED_ROLES:
        for name in pair:
            if name in role:
                return name
    return role


def _bool_key(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = _key(value)
    if text in ("true", "yes", "y", "1"):
        return "true"
    if text in ("false", "no", "n", "0"):
        return "false"
    return None


def _int_key(value: Any) -> Optional[float]:
    try:
        return float(int(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _identity_key(label: str, ref: Any) -> str:
    """Canonical key of a node reference: a scalar key or an identity map."""
    spec = NODES_BY_LABEL.get(label)
    if isinstance(ref, Mapping):
        names = spec.identity if spec else tuple(sorted(ref))
        parts = [_name_key(ref.get(name)) for name in names]
        return "|".join(parts)
    return _name_key(ref)


def _identity_display(ref: Any) -> str:
    if isinstance(ref, Mapping):
        return ", ".join(_text(v) for v in ref.values() if v not in (None, ""))
    return _text(ref)


# ---------------------------------------------------------------------------
# Node and edge projection
# ---------------------------------------------------------------------------


def _attr(
    label: str,
    subject_key: str,
    subject: str,
    attribute: str,
    kind: str,
    raw: Any,
    *,
    time_varying: bool = False,
    event_type: str = "",
) -> Optional[Claim]:
    """One attribute claim, or None when the value is absent or unparseable."""
    if raw in (None, "") or not subject_key:
        return None
    common = dict(
        label=label,
        subject_key=subject_key,
        subject=subject,
        attribute=attribute,
        kind=kind,
        value=_text(raw),
        time_varying=time_varying,
        event_type=event_type,
    )
    if kind == KIND_DATE:
        parsed = _date_claim(raw)
        if parsed is None:
            return None
        key, lo, hi = parsed
        return Claim(value_key=key, lo=lo, hi=hi, **common)
    if kind == KIND_AMOUNT:
        number = parse_amount(raw)
        if number is None:
            return None
        return Claim(value_key=f"{number:.2f}", number=number, **common)
    if kind == KIND_STATUS:
        group = _status_group(str(raw))
        if group is None:
            return None
        return Claim(value_key=f"group{group}", **common)
    if kind == KIND_ROLE:
        role = _role_key(raw)
        return Claim(value_key=role, **common) if role else None
    if kind == KIND_BOOL:
        flag = _bool_key(raw)
        return Claim(value_key=flag, **common) if flag else None
    if kind == KIND_INT:
        number = _int_key(raw)
        if number is None:
            return None
        return Claim(value_key=str(int(number)), number=number, **common)
    if kind == KIND_NAME:
        name = _name_key(raw)
        return Claim(value_key=name, **common) if name else None
    return None


def _join(*parts: Any) -> str:
    return "|".join(_name_key(p) for p in parts)


#: Events that happen once, so two dates for one of them are a contradiction.
#: Everything else (adjournments, instalments, notices, hearings, payments)
#: recurs, and two dates mean two events: those are keyed by their date too.
#: Deliberately short: a recurring event wrongly listed here invents a date
#: conflict on every case; one missing from it only loses a date check.
_ONE_OFF_EVENT = re.compile(
    r"\b(agreement|contract|deed|executed|incorporat\w*|born|birth|death|died|"
    r"marriage|married|accident|arrest\w*|fir|appoint\w*|terminat\w*|resign\w*|"
    r"retire\w*|joined|joining)\b"
)

#: Filings a party makes once per matter (an FIR, a plaint, a charge-sheet).
#: A second "application" or "petition" is a second filing, not a disputed date.
_ONE_OFF_FILING = re.compile(
    r"\b(fir|plaint|written statement|charge ?sheet|chargesheet|complaint)\b"
)


def is_one_off(*texts: Any) -> bool:
    return any(_ONE_OFF_EVENT.search(_key(t)) for t in texts if t)


def _dated(key: str, when: Any) -> str:
    """Key a recurring subject by its date: two dates are two occurrences."""
    parsed = _date_claim(when)
    return f"{key}@{parsed[0]}" if parsed else key


def _subject(label: str, props: Mapping[str, Any]) -> Tuple[str, str, str]:
    """``(subject_key, subject_display, event_type)`` of a node."""
    if label in ("Party", "Advocate", "Forum", "Statute"):
        name = props.get("canonical_name")
        return _name_key(name), _text(name), ""
    if label == "Event":
        key = _key(props.get("description"))
        if key and not is_one_off(props.get("description"), props.get("event_type")):
            key = _dated(key, props.get("event_date"))
        return key, _text(props.get("description")), _key(props.get("event_type"))
    if label == "Hearing":
        parsed = _date_claim(props.get("hearing_date"))
        return (parsed[0] if parsed else ""), _text(props.get("hearing_date")), ""
    if label == "Filing":
        key = _join(props.get("filing_type"), props.get("filed_by"))
        if not _ONE_OFF_FILING.search(_key(props.get("filing_type"))):
            key = _dated(key, props.get("filed_on"))
        return key, _text(props.get("filing_type")), ""
    if label == "Order":
        parsed = _date_claim(props.get("order_date"))
        return (
            (parsed[0] if parsed else "") + "|" + _key(props.get("operative_direction")),
            _text(props.get("operative_direction")),
            "",
        )
    if label == "Obligation":
        return (
            _join(props.get("obligation_type"), props.get("owed_by"), props.get("owed_to")),
            _text(props.get("obligation_type")),
            "",
        )
    if label == "Relief":
        return _key(props.get("relief_type")), _text(props.get("relief_type")), ""
    if label == "Transaction":
        if props.get("reference"):
            return "ref:" + _key(props.get("reference")), _text(props.get("reference")), ""
        parsed = _date_claim(props.get("transaction_date"))
        amount = parse_amount(props.get("amount"))
        if parsed and amount is not None:
            return f"on:{parsed[0]}|{amount:.2f}", _text(props.get("transaction_date")), ""
        return "", "", ""
    if label == "Communication":
        # Notices and letters recur between the same parties: the date is part
        # of which communication it is, never a value two documents dispute.
        key = _dated(
            _join(props.get("medium"), props.get("sender"), props.get("recipient")),
            props.get("sent_on"),
        )
        return key, _text(props.get("description") or props.get("medium")), _key(
            props.get("medium")
        )
    if label == "CaseReference":
        return _key(props.get("case_number")), _text(props.get("case_number")), ""
    if label == "Precedent":
        return _key(props.get("citation")), _text(props.get("citation")), ""
    spec = NODES_BY_LABEL.get(label)
    identity = spec.identity if spec else ()
    key = "|".join(_name_key(props.get(name)) for name in identity)
    shown = ", ".join(_text(props.get(name)) for name in identity if props.get(name))
    return (key if key.strip("|") else ""), shown, ""


#: label -> [(attribute, kind, time_varying)].
_ATTRIBUTES: Dict[str, Tuple[Tuple[str, str, bool], ...]] = {
    "Party": (("party_role", KIND_ROLE, False), ("is_natural_person", KIND_BOOL, False)),
    "Event": (("event_date", KIND_DATE, False),),
    "Hearing": (("next_date", KIND_DATE, False),),
    "Filing": (("filed_on", KIND_DATE, False),),
    "Order": (("complied", KIND_BOOL, True),),
    "Obligation": (("status", KIND_STATUS, True), ("due_date", KIND_DATE, False)),
    "Relief": (("quantum", KIND_AMOUNT, False), ("granted", KIND_BOOL, True)),
    "Transaction": (("amount", KIND_AMOUNT, False), ("transaction_date", KIND_DATE, False)),
    "CaseReference": (
        ("year", KIND_INT, False),
        ("forum", KIND_NAME, True),
        ("status", KIND_STATUS, True),
    ),
    "Precedent": (("decision_date", KIND_DATE, False),),
}


def signatures(label: str, props: Mapping[str, Any]) -> List[str]:
    """Other keys under which a node's existence can be matched.

    A denial rarely restates the identifier the assertion used: a reply denies
    "Rs. 5,00,000 on 02.02.2024", not "UTR-0001". A payment is therefore also
    known by its date and amount, and a denial meets the assertion on either.
    """
    out = []
    if label == "Transaction":
        if props.get("reference"):
            out.append("ref:" + _key(props.get("reference")))
        parsed = _date_claim(props.get("transaction_date"))
        amount = parse_amount(props.get("amount"))
        if parsed and amount is not None:
            out.append(f"on:{parsed[0]}|{amount:.2f}")
    return [f"{label}:{s}" for s in out]


def claims_for_node(label: str, props: Mapping[str, Any]) -> List[Claim]:
    """Every claim one extracted node makes: that it exists, and its attributes."""
    subject_key, subject, event_type = _subject(label, props)
    if not subject_key:
        return []
    out = [
        Claim(
            label=label,
            subject_key=subject_key,
            subject=subject,
            attribute=EXISTS,
            kind=KIND_EXISTS,
            value_key="",
            value="",
            event_type=event_type,
        )
    ]
    for attribute, kind, time_varying in _ATTRIBUTES.get(label, ()):
        claim = _attr(
            label,
            subject_key,
            subject,
            attribute,
            kind,
            props.get(attribute),
            time_varying=time_varying,
            event_type=event_type,
        )
        if claim is not None:
            out.append(claim)
    return out


def claims_for_edge(edge: Mapping[str, Any]) -> List[Claim]:
    """The claim an extracted relationship makes: tail -[type]-> head."""
    edge_type = str(edge.get("type") or "")
    tail, head = edge.get("from") or {}, edge.get("to")
    if not edge_type or not isinstance(head, Mapping) or edge_type == "CONTRADICTS":
        return []
    tail_label, head_label = str(tail.get("label") or ""), str(head.get("label") or "")
    subject_key = _identity_key(tail_label, tail.get("key"))
    value_key = _identity_key(head_label, head.get("key"))
    if not subject_key.strip("|") or not value_key.strip("|"):
        return []
    return [
        Claim(
            label=edge_type,
            subject_key=f"{tail_label}:{subject_key}",
            subject=_identity_display(tail.get("key")),
            attribute=edge_type,
            kind=KIND_RELATION,
            value_key=f"{head_label}:{value_key}",
            value=_identity_display(head.get("key")),
        )
    ]


# ---------------------------------------------------------------------------
# Evidence rows
# ---------------------------------------------------------------------------


def passage_span(text: str, props: Mapping[str, Any]) -> str:
    """The passage text around a fact: a window on its anchor phrase."""
    text = str(text or "")
    lowered = text.lower()
    for key in _ANCHOR_PROPS:
        needle = str(props.get(key) or "").strip().lower()[:80]
        at = lowered.find(needle) if len(needle) >= 3 else -1
        if at >= 0:
            start = max(0, at - SPAN_CHARS // 2)
            return " ".join(text[start : start + SPAN_CHARS].split())
    return " ".join(text[:SPAN_CHARS].split())


def _as_of(raw: str) -> Tuple[str, str]:
    parsed = parse_date(raw) if raw else None
    if parsed is None:
        return "", ""
    return parsed.range_start.isoformat(), parsed.range_end.isoformat()


def evidence_for_entries(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    chunk_text: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Evidence rows for one batch of extraction output.

    Args:
        nodes / edges: ``extract_facts`` entries (with ``modality``/``as_of``).
        chunk_text: ``chunk_id -> text``, for the passage span.

    Returns:
        ``(rows, counts)``. Hedged entries produce no row; ``counts`` reports
        them, the denials and the rows by modality.
    """
    rows: List[Dict[str, Any]] = []
    counts = {"hedged": 0, "denied": 0, "asserted": 0, "alleged": 0}
    seen = set()
    for entry, is_node in [(n, True) for n in nodes] + [(e, False) for e in edges]:
        modality = str(entry.get("modality") or MODALITY_ASSERTED)
        if modality == MODALITY_HEDGED:
            counts["hedged"] += 1
            continue
        props = entry.get("properties") or {}
        if is_node:
            claims = claims_for_node(str(entry.get("label") or ""), props)
        else:
            claims = claims_for_edge(entry)
        if not claims:
            continue
        counts[modality if modality in counts else MODALITY_ASSERTED] += 1
        document_id = str(props.get("source_document_id") or "")
        chunk_id = str(props.get("chunk_id") or "")
        valid_from, valid_to = _as_of(str(entry.get("as_of") or ""))
        sigs = signatures(str(entry.get("label") or ""), props) if is_node else []
        span = passage_span(chunk_text.get(chunk_id, ""), props)
        for claim in claims:
            # A denial denies the fact as a whole, so only its existence claim
            # (or the relation itself) is a denial; its attributes say nothing.
            if modality == MODALITY_DENIED and claim.attribute not in (EXISTS, claim.label):
                continue
            ident = (document_id, chunk_id, claim.fact_key, modality == MODALITY_DENIED)
            if ident in seen:
                continue
            seen.add(ident)
            rows.append(
                {
                    "fact_key": claim.fact_key,
                    "slot_key": claim.slot_key,
                    "claim": claim.to_dict(),
                    "modality": modality,
                    "denial": modality == MODALITY_DENIED,
                    "extraction_confidence": float(props.get("extraction_confidence") or 0.5),
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "page": props.get("page"),
                    "span": span,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "signatures": sigs if claim.attribute == EXISTS else [],
                    # What the graph holds for this statement, so a fact that
                    # outlives one of its documents can be re-projected.
                    "node": (
                        {"label": entry.get("label"), "properties": dict(props)}
                        if is_node and modality != MODALITY_DENIED
                        else None
                    ),
                    "edge": (
                        {
                            "type": entry.get("type"),
                            "from": entry.get("from"),
                            "to": entry.get("to"),
                            "properties": dict(props),
                        }
                        if not is_node and modality != MODALITY_DENIED
                        else None
                    ),
                }
            )
    return rows, counts


__all__ = [
    "Claim",
    "EXISTS",
    "KIND_AMOUNT",
    "KIND_BOOL",
    "KIND_DATE",
    "KIND_EXISTS",
    "KIND_INT",
    "KIND_NAME",
    "KIND_RELATION",
    "KIND_ROLE",
    "KIND_STATUS",
    "MODALITY_ALLEGED",
    "claim_from_dict",
    "claims_for_edge",
    "claims_for_node",
    "evidence_for_entries",
    "fact_key",
    "passage_span",
]
