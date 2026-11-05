"""
Stage 6 of the trust pipeline: confidence routing.

:func:`route` is the **only** function that decides a fact's served status.
Review decisions do not write a status themselves; they are inputs to
:func:`route`, so there is exactly one place a fact's classification can change.

Bands (``TRUST_BAND_TRUSTED`` / ``TRUST_BAND_REVIEW``):

* ``trusted``   — confidence >= 0.85: stated as fact.
* ``contested`` — between, or in an unresolved conflict: stated with both sides.
* ``dropped``   — below 0.50: kept for audit, never served.

**A deliberate deviation from Keel.** There, an unresolved conflict only
penalises confidence, so a weakly supported side can fall straight to
``dropped``. In a case file a contradiction is itself information: a disputed
date the lawyer never sees is worse than one shown as disputed. A fact in an
unresolved conflict is therefore at least ``contested``. Its confidence is
still penalised, and ``shadow_band`` keeps what the formula alone says.

Both sides of a conflict become contested, not just the newer one: a fact a
later document contradicts must stop being served as unqualified truth.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

STATUS_TRUSTED = "trusted"
STATUS_CONTESTED = "contested"
STATUS_DROPPED = "dropped"

REASON_CONFLICT = "unresolved_conflict"
REASON_LOW_CONFIDENCE = "low_confidence"

#: Decisions on a conflict (keyed by conflict_id).
ACTION_KEEP_A = "keep_a"
ACTION_KEEP_B = "keep_b"
ACTION_KEEP_BOTH = "keep_both"
ACTION_DISMISSED = "dismissed"
CONFLICT_ACTIONS = (ACTION_KEEP_A, ACTION_KEEP_B, ACTION_KEEP_BOTH, ACTION_DISMISSED)

#: Decisions on a fact (keyed by fact_key).
ACTION_CONFIRM_TRUSTED = "confirm_trusted"
ACTION_CONFIRM_DROPPED = "confirm_dropped"
FACT_ACTIONS = (ACTION_CONFIRM_TRUSTED, ACTION_CONFIRM_DROPPED)


@dataclass(frozen=True)
class Bands:
    trusted: float = 0.85
    review: float = 0.50
    conflict_penalty: float = 0.35

    def __post_init__(self) -> None:
        if not 0.0 <= self.review < self.trusted <= 1.0:
            raise ValueError("trust bands need 0 <= review < trusted <= 1")


def band(confidence: float, bands: Bands) -> str:
    if confidence >= bands.trusted:
        return STATUS_TRUSTED
    if confidence < bands.review:
        return STATUS_DROPPED
    return STATUS_CONTESTED


def route(
    corroboration: float,
    *,
    in_unresolved_conflict: bool,
    lost_conflict: bool,
    decision: Optional[str],
    bands: Bands,
) -> Dict[str, Any]:
    """The served status of one fact.

    Args:
        corroboration: Stage 4's noisy-OR confidence.
        in_unresolved_conflict: The fact is a side of an unresolved conflict.
        lost_conflict: A reviewer resolved a conflict in favour of the other side.
        decision: ``confirm_trusted`` / ``confirm_dropped`` on this fact, if any.
        bands: Thresholds.

    Returns:
        ``{status, shadow_band, confidence}``.
    """
    confidence = corroboration * (1.0 - bands.conflict_penalty * in_unresolved_conflict)
    confidence = round(confidence, 4)
    shadow = band(confidence, bands)
    if decision == ACTION_CONFIRM_TRUSTED:
        status = STATUS_TRUSTED
    elif decision == ACTION_CONFIRM_DROPPED or lost_conflict:
        status = STATUS_DROPPED
    elif in_unresolved_conflict:
        status = STATUS_CONTESTED
    else:
        status = shadow
    return {"status": status, "shadow_band": shadow, "confidence": confidence}


def _action(value: Any) -> str:
    """A decision is stored as ``{"action", "match"}``; tests may pass the bare action."""
    return str(value.get("action") if isinstance(value, Mapping) else value or "")


def _h(value: Any) -> str:
    """Signature fields are hashed: a decision outlives the documents it was
    made on (it has no document id for a delete to reach), so it must hold no
    case text — no party name, date or amount."""
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:20]


def conflict_signature(conflict: Mapping[str, Any]) -> Dict[str, Any]:
    """What a conflict is about, independent of the keys resolution assigned.

    Stored with a decision. Fact keys embed the case-wide canonical subject,
    which can move when documents arrive or leave; the subject wordings and the
    two values do not.
    """
    return {
        "axis": conflict.get("axis"),
        "label": conflict.get("label"),
        "attribute": conflict.get("attribute"),
        "subjects": sorted(_h(s) for s in conflict.get("subjects") or ()),
        "values": [_h(side.get("value_key")) for side in conflict.get("sides") or ()],
    }


def fact_signature(fact: Mapping[str, Any], subjects: Sequence[str]) -> Dict[str, Any]:
    claim = fact.get("claim") or {}
    return {
        "label": claim.get("label"),
        "attribute": claim.get("attribute"),
        "value_key": _h(claim.get("value_key")),
        "subjects": sorted(_h(s) for s in subjects),
    }


def _matches(match: Mapping[str, Any], current: Mapping[str, Any], keys: Sequence[str]) -> bool:
    if any(match.get(k) != current.get(k) for k in keys):
        return False
    return bool(set(match.get("subjects") or ()) & set(current.get("subjects") or ()))


def _conflict_decision(
    conflict: Mapping[str, Any], decisions: Mapping[str, Any]
) -> Tuple[str, List[str]]:
    """``(action, losing fact keys)`` for a conflict: by id, else by signature.

    By id, keep_a / keep_b name the sides by position. By signature, the
    stored side order may differ from today's, so the kept side is found by
    its value instead.
    """
    sides = list(conflict.get("sides") or ())
    direct = decisions.get(conflict["conflict_id"])
    if direct is not None:
        action = _action(direct)
        kept = {ACTION_KEEP_A: 0, ACTION_KEEP_B: 1}.get(action)
        if kept is None:
            return action, []
        return action, [
            side["fact_key"]
            for i, side in enumerate(sides)
            if i != kept and not side.get("denial")
        ]
    current = conflict_signature(conflict)
    hashed = current["values"]
    for value in decisions.values():
        match = value.get("match") if isinstance(value, Mapping) else None
        if not match or "axis" not in match:
            continue
        if not _matches(match, current, ("axis", "label", "attribute")):
            continue
        if sorted(match.get("values") or ()) != sorted(hashed):
            continue
        action = _action(value)
        kept = {ACTION_KEEP_A: 0, ACTION_KEEP_B: 1}.get(action)
        if kept is None:
            return action, []
        kept_value = match["values"][kept]
        return action, [
            side["fact_key"]
            for side in sides
            if _h(side.get("value_key")) != kept_value and not side.get("denial")
        ]
    return "", []


def fact_decision(
    fact: Mapping[str, Any], subjects: Sequence[str], decisions: Mapping[str, Any]
) -> str:
    """``confirm_trusted`` / ``confirm_dropped`` for a fact: by key, else by signature."""
    direct = decisions.get(fact["fact_key"])
    if direct is not None:
        return _action(direct)
    current = fact_signature(fact, subjects)
    for value in decisions.values():
        match = value.get("match") if isinstance(value, Mapping) else None
        if match and "value_key" in match and _matches(
            match, current, ("label", "attribute", "value_key")
        ):
            return _action(value)
    return ""


def apply_conflict_decisions(
    conflicts: Sequence[Dict[str, Any]], decisions: Mapping[str, Any]
) -> Dict[str, Any]:
    """Mark resolved conflicts and collect each fact's conflict state.

    Returns:
        ``{unresolved: set(fact_key), losers: set(fact_key)}``; each conflict
        gets ``resolution_status`` and ``resolution_action``.
    """
    unresolved, losers = set(), set()
    for conflict in conflicts:
        action, lost = _conflict_decision(conflict, decisions)
        sides = [k for k in (conflict["fact_a"], conflict["fact_b"]) if k]
        if action in CONFLICT_ACTIONS:
            conflict["resolution_status"] = "resolved"
            conflict["resolution_action"] = action
            losers.update(lost)
        else:
            conflict["resolution_status"] = "unresolved"
            conflict["resolution_action"] = ""
            unresolved.update(sides)
    return {"unresolved": unresolved, "losers": losers}


def review_items(facts: Sequence[Mapping[str, Any]], decisions: Mapping[str, Any]) -> List[Dict]:
    """One pending review item per contested fact without a fact decision.

    Derived on every pass, so an item whose fact routes out of ``contested``
    (more evidence arrived, its conflict was resolved) closes by itself.
    """
    items = []
    for fact in facts:
        if fact["status"] != STATUS_CONTESTED or fact.get("decision") in FACT_ACTIONS:
            continue
        items.append(
            {
                "fact_key": fact["fact_key"],
                "reason": REASON_CONFLICT if fact.get("conflict_ids") else REASON_LOW_CONFIDENCE,
                "status": "pending",
                "conflict_ids": list(fact.get("conflict_ids") or ()),
                "document_ids": list(fact.get("document_ids") or ()),
            }
        )
    return items
