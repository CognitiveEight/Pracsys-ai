"""
Provision resolution — validate a citation, or say plainly that you cannot.

Work plan tasks 2.5 and 2.6, and Section 4.3's three rules:

1. Provisions are resolved against the curated register, so a section number is
   validated to exist within the act it is attributed to.
2. Legacy IPC / CrPC / Evidence Act references found in older documents are
   **mapped to their successor provisions and retained as historical aliases** —
   the fact is carried forward, not discarded.
3. A provision that cannot be resolved is **flagged as unverified rather than
   silently emitted**.

Point 2 is a behavioural change from what the platform does today. The current
``applicable_laws`` normaliser deletes any row mentioning IPC, CrPC or the 1872
Evidence Act, so a document that cites "IPC 420" contributes nothing at all.
Mapping it to BNS 318 keeps the legal substance and still honours the rule that
IPC must not appear in output.

Point 3 matters as much as point 1: because the shipped register is a partial
seed, "not in the register" means *unknown*, never *invalid*. :class:`Resolution`
distinguishes the two, and only ``INVALID`` asserts that a section does not
exist — which this resolver never returns for an act whose register coverage is
incomplete.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence


from app.statutes.register import (
    StatuteRegister,
    get_register,
    normalize_section_number,
)


class Resolution(str, Enum):
    """
    Outcome of resolving one citation.

    Attributes:
        RESOLVED: The act is known and the section exists in the register.
        MAPPED: A superseded citation carried forward to its successor
            provision, which itself resolved. The original is kept as an alias.
        UNVERIFIED: The act is known but the section is not in the register, or
            the act itself is unrecognised. Not a claim that it is wrong — the
            register is incomplete and says so.
        UNMAPPED_LEGACY: A superseded citation with no mapping entry. The legal
            substance cannot be carried forward, so it must not be emitted as a
            current provision.
        INVALID: The section cannot exist — a malformed token, or a number past
            the act's known section count.
    """

    RESOLVED = "resolved"
    MAPPED = "mapped"
    UNVERIFIED = "unverified"
    UNMAPPED_LEGACY = "unmapped_legacy"
    INVALID = "invalid"


#: Outcomes safe to emit as a current statutory provision.
EMITTABLE = frozenset({Resolution.RESOLVED, Resolution.MAPPED})

#: A plausible section token: digits, optional letter suffix, optional
#: parenthesised sub-clauses. Anything else is malformed rather than unknown.
_SECTION_TOKEN = re.compile(r"^\d{1,4}[A-Z]{0,2}(?:\(\w{1,4}\))*$")


@dataclass(frozen=True)
class ResolvedProvision:
    """
    One citation, resolved.

    Attributes:
        resolution: What happened. See :class:`Resolution`.
        act_key: Register key of the *current* act, e.g. ``"BNS"``.
        act_name: Canonical name of the current act.
        short_title: Citation-style short title, e.g. ``"BNS"``, ``"IT Act"``.
        framework: ``BNS`` / ``BNSS`` / ``BSA`` / ``IT_ACT``.
        section: Section number in the current act.
        heading: Section heading from the register, when known.
        original_act: The act as cited, when it differed.
        original_section: The section as cited, when it differed.
        is_legacy_source: True when the citation came from a superseded act.
        note: Human-readable explanation, safe to log or show an SME.
    """

    resolution: Resolution
    act_key: Optional[str] = None
    act_name: Optional[str] = None
    short_title: Optional[str] = None
    framework: Optional[str] = None
    section: Optional[str] = None
    heading: Optional[str] = None
    original_act: Optional[str] = None
    original_section: Optional[str] = None
    is_legacy_source: bool = False
    note: str = ""

    @property
    def emittable(self) -> bool:
        """True when this may be emitted as a current statutory provision."""
        return self.resolution in EMITTABLE

    @property
    def citation(self) -> Optional[str]:
        """``"BNS 318"`` style label for the resolved provision."""
        if not self.section or not (self.short_title or self.act_key):
            return None
        return f"{self.short_title or self.act_key} {self.section}"

    def to_dict(self) -> Dict[str, object]:
        """Serialisable form, for payload annotation and the eval harness."""
        return {
            "resolution": self.resolution.value,
            "actKey": self.act_key,
            "actName": self.act_name,
            "shortTitle": self.short_title,
            "framework": self.framework,
            "section": self.section,
            "heading": self.heading,
            "originalAct": self.original_act,
            "originalSection": self.original_section,
            "isLegacySource": self.is_legacy_source,
            "note": self.note,
        }


def _section_is_malformed(section: str) -> bool:
    """True when the token cannot be a section number at all."""
    return not section or not _SECTION_TOKEN.match(section)


def _base_section_number(section: str) -> Optional[int]:
    """Leading integer of a section token, for the out-of-range check."""
    match = re.match(r"^(\d{1,4})", section)
    return int(match.group(1)) if match else None


def resolve_provision(
    act: str,
    section: str,
    register: Optional[StatuteRegister] = None,
) -> ResolvedProvision:
    """
    Resolve one statutory citation against the register.

    Args:
        act: The act as cited, in any of its surface forms.
        section: The section as cited, with or without a ``Section`` prefix.
        register: Register to resolve against; defaults to the shipped one.

    Returns:
        A :class:`ResolvedProvision`. Never raises on bad input — an
        unresolvable citation is a result, not an error, because the caller's
        job is to flag it rather than to fail.
    """
    reg = register or get_register()
    raw_act, raw_section = str(act or "").strip(), str(section or "").strip()
    canonical_section = normalize_section_number(raw_section)
    act_key, is_legacy = reg.lookup_act(raw_act)

    if act_key is None:
        return ResolvedProvision(
            resolution=Resolution.UNVERIFIED,
            section=canonical_section or None,
            original_act=raw_act or None,
            original_section=raw_section or None,
            note=(
                f"Act {raw_act!r} is not in the statutory register "
                f"({reg.version}); cannot validate the citation"
            ),
        )

    if _section_is_malformed(canonical_section):
        source = reg.legacy[act_key] if is_legacy else reg.statutes[act_key]
        name, short_title = source.canonical_name, source.short_title
        return ResolvedProvision(
            resolution=Resolution.INVALID,
            act_key=None if is_legacy else act_key,
            act_name=name,
            short_title=short_title,
            original_act=raw_act or None,
            original_section=raw_section or None,
            is_legacy_source=is_legacy,
            note=f"{raw_section!r} is not a well-formed section number",
        )

    if is_legacy:
        return _resolve_legacy(reg, act_key, raw_act, raw_section, canonical_section)

    statute = reg.statutes[act_key]
    if statute.has_section(canonical_section):
        return ResolvedProvision(
            resolution=Resolution.RESOLVED,
            act_key=act_key,
            act_name=statute.canonical_name,
            short_title=statute.short_title,
            framework=statute.framework,
            section=canonical_section,
            heading=statute.heading(canonical_section),
            original_act=raw_act,
            original_section=raw_section,
            note="Validated against the statutory register",
        )

    base = _base_section_number(canonical_section)
    if statute.total_sections and base and base > statute.total_sections:
        return ResolvedProvision(
            resolution=Resolution.INVALID,
            act_key=act_key,
            act_name=statute.canonical_name,
            short_title=statute.short_title,
            framework=statute.framework,
            section=canonical_section,
            original_act=raw_act,
            original_section=raw_section,
            note=(
                f"{statute.short_title} has {statute.total_sections} sections; "
                f"s.{canonical_section} cannot exist"
            ),
        )

    return ResolvedProvision(
        resolution=Resolution.UNVERIFIED,
        act_key=act_key,
        act_name=statute.canonical_name,
        short_title=statute.short_title,
        framework=statute.framework,
        section=canonical_section,
        original_act=raw_act,
        original_section=raw_section,
        note=(
            f"s.{canonical_section} is not in the {reg.status} register for "
            f"{statute.short_title} — unknown, not invalid"
        ),
    )


def _resolve_legacy(
    reg: StatuteRegister,
    legacy_key: str,
    raw_act: str,
    raw_section: str,
    canonical_section: str,
) -> ResolvedProvision:
    """Carry a superseded citation forward to its successor provision."""
    legacy = reg.legacy[legacy_key]
    successor_section = legacy.successor_section(canonical_section)
    successor = reg.statutes.get(legacy.succeeded_by)

    if successor is None or successor_section is None:
        return ResolvedProvision(
            resolution=Resolution.UNMAPPED_LEGACY,
            act_name=legacy.canonical_name,
            short_title=legacy.short_title,
            original_act=raw_act,
            original_section=raw_section,
            is_legacy_source=True,
            note=(
                f"{legacy.short_title} s.{canonical_section} has no mapping to "
                f"{legacy.succeeded_by} in the {reg.status} register; cannot be "
                "emitted as a current provision"
            ),
        )

    return ResolvedProvision(
        resolution=Resolution.MAPPED,
        act_key=successor.key,
        act_name=successor.canonical_name,
        short_title=successor.short_title,
        framework=successor.framework,
        section=successor_section,
        heading=successor.heading(successor_section),
        original_act=raw_act,
        original_section=raw_section,
        is_legacy_source=True,
        note=(
            f"{legacy.short_title} s.{canonical_section} superseded by "
            f"{successor.short_title} s.{successor_section}; original retained as "
            "a historical alias"
        ),
    )


def resolve_many(
    citations: Sequence[Dict[str, str]],
    register: Optional[StatuteRegister] = None,
) -> List[ResolvedProvision]:
    """
    Resolve a batch of ``{"act": ..., "section": ...}`` citations.

    Args:
        citations: Items with ``act`` and ``section`` keys; others are ignored.
        register: Register to resolve against.

    Returns:
        One resolution per input citation, in order.
    """
    reg = register or get_register()
    return [
        resolve_provision(item.get("act", ""), item.get("section", ""), reg)
        for item in citations
        if isinstance(item, dict)
    ]


def unresolvable_rate(resolutions: Sequence[ResolvedProvision]) -> float:
    """
    Fraction of citations that could not be emitted as current provisions.

    This is acceptance metric 10.1 — *unresolvable statutory citations reaching
    output, target < 2%*. Measured on what the platform would emit, so
    ``MAPPED`` counts as resolved.

    Args:
        resolutions: Output of :func:`resolve_many`.

    Returns:
        A value in [0.0, 1.0]; 0.0 for an empty input.
    """
    if not resolutions:
        return 0.0
    bad = sum(1 for r in resolutions if not r.emittable)
    return round(bad / len(resolutions), 4)


def resolution_report(resolutions: Sequence[ResolvedProvision]) -> Dict[str, object]:
    """Counts per outcome plus the metric-10.1 rate, for the eval harness."""
    counts: Dict[str, int] = {r.value: 0 for r in Resolution}
    for item in resolutions:
        counts[item.resolution.value] += 1
    return {
        "total": len(resolutions),
        "counts": counts,
        "unresolvableRate": unresolvable_rate(resolutions),
        "emittable": sum(1 for r in resolutions if r.emittable),
    }
