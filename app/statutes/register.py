"""
The statutory register — the curated set of provisions the platform trusts.

Work plan task 2.5: *build the statutory register for BNS, BNSS, BSA and the IT
Act; resolve and validate every extracted provision against it.* Section 4.3
gives the reason: Indian statutory references are the most citation-error-prone
element of the output, and an extracted provision that cannot be resolved to a
real section should be flagged as unverified rather than silently emitted. That
is how hallucinated citations are prevented structurally rather than by prompt
instruction.

**The shipped register is a partial seed.** It carries only provisions verified
at authoring time, and says so in its own ``status`` field. A section that is
absent is *unknown*, not *invalid* — :mod:`app.statutes.resolver` reports
``UNVERIFIED`` for it and never claims it does not exist. Completing the
register is SME work; :func:`load_register` accepts an override path so a
completed file can be dropped in without a code change.

The loader validates the file on load, including the cross-check that every
legacy mapping points at a section that actually exists in its successor act.
That check is what catches an error in the mapping table itself.
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple

from loguru import logger

#: Shipped seed register. Override with ``STATUTE_REGISTER_PATH``.
DEFAULT_REGISTER_PATH = Path(__file__).parent / "data" / "register.json"


class RegisterError(ValueError):
    """Raised when the register file is malformed or internally inconsistent."""


def normalize_section_number(value: str) -> str:
    """
    Canonicalise a section token for lookup.

    Handles the surface forms that actually appear in legal documents: a
    ``Section``/``Sec.``/``S.`` prefix, whitespace, and a lowercase sub-section
    letter. ``"sec. 66c"`` and ``"Section 66-C"`` both become ``"66C"``.

    Sub-section parentheses are preserved — ``316(4)`` is a different provision
    from ``316`` and the register distinguishes them.

    Args:
        value: Raw section text.

    Returns:
        The canonical token, or ``""`` when nothing usable remains.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    # Strip one or more "Section"/"Sec"/"S." prefixes and any separator after them.
    text = re.sub(r"^(?:(?:sections?|secs?|ss?)\s*[.:\-]?\s*)+", "", text, flags=re.IGNORECASE)
    text = text.strip(" .:-")
    # "66-C" / "66 C" -> "66C"; the hyphen form is common in older drafting.
    text = re.sub(r"(?<=\d)[\s\-]+(?=[A-Za-z]\b)", "", text)
    # Uppercase only the trailing letter suffix, never the whole token.
    text = re.sub(r"(?<=\d)([a-z])(?![a-z])", lambda m: m.group(1).upper(), text)
    return text.strip()


def normalize_act_name(value: str) -> str:
    """Lowercase, collapse whitespace and drop punctuation, for alias matching."""
    text = re.sub(r"[.,]", "", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Statute:
    """A current act in the register."""

    key: str
    canonical_name: str
    short_title: str
    year: Optional[int]
    framework: str
    total_sections: Optional[int]
    replaces: Optional[str]
    aliases: FrozenSet[str]
    sections: Mapping[str, Mapping[str, object]]

    def has_section(self, section: str) -> bool:
        return normalize_section_number(section) in self.sections

    def heading(self, section: str) -> Optional[str]:
        entry = self.sections.get(normalize_section_number(section))
        return str(entry.get("heading")) if entry and entry.get("heading") else None

    @property
    def coverage(self) -> Optional[float]:
        """Fraction of the act's sections present in the register, if known."""
        if not self.total_sections:
            return None
        return round(len(self.sections) / self.total_sections, 4)


@dataclass(frozen=True)
class LegacyStatute:
    """A superseded act, with the mapping to its successor's sections."""

    key: str
    canonical_name: str
    short_title: str
    year: Optional[int]
    succeeded_by: str
    aliases: FrozenSet[str]
    section_map: Mapping[str, str]

    def successor_section(self, section: str) -> Optional[str]:
        return self.section_map.get(normalize_section_number(section))


@dataclass(frozen=True)
class StatuteRegister:
    """
    The loaded register: current acts, superseded acts and the alias index.

    Attributes:
        version: ``register_version`` from the file.
        status: ``"seed"`` while the register is incomplete, ``"complete"`` once
            an SME has signed it off. Callers surface this so a partial register
            is never mistaken for an authoritative one.
    """

    version: str
    status: str
    coverage: str
    statutes: Mapping[str, Statute]
    legacy: Mapping[str, LegacyStatute]
    _alias_index: Mapping[str, Tuple[str, bool]] = field(repr=False, default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    def lookup_act(self, act_name: str) -> Tuple[Optional[str], bool]:
        """
        Resolve an act name to a register key.

        Matching is exact on the normalised alias set first, then a containment
        pass so ``"Bharatiya Nyaya Sanhita, 2023 (BNS)"`` still resolves. The
        containment pass prefers the longest alias, so ``"indian evidence act"``
        never loses to a shorter substring.

        Args:
            act_name: The act as written in the document or model output.

        Returns:
            ``(key, is_legacy)``; ``(None, False)`` when unrecognised.
        """
        normalized = normalize_act_name(act_name)
        if not normalized:
            return None, False
        hit = self._alias_index.get(normalized)
        if hit:
            return hit
        for alias in sorted(self._alias_index, key=len, reverse=True):
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized):
                return self._alias_index[alias]
        return None, False

    def summary(self) -> Dict[str, object]:
        """Coverage summary, for the register report and the eval harness."""
        return {
            "version": self.version,
            "status": self.status,
            "coverage": self.coverage,
            "statutes": {
                key: {
                    "canonicalName": s.canonical_name,
                    "sectionsInRegister": len(s.sections),
                    "totalSections": s.total_sections,
                    "coverage": s.coverage,
                }
                for key, s in self.statutes.items()
            },
            "legacyMappings": {
                key: len(l.section_map) for key, l in self.legacy.items()
            },
        }


def _build_alias_index(
    statutes: Mapping[str, Statute], legacy: Mapping[str, LegacyStatute]
) -> Dict[str, Tuple[str, bool]]:
    """
    Map every alias to ``(key, is_legacy)``.

    Raises:
        RegisterError: If one alias is claimed by two acts — that would make
            resolution order-dependent, which is exactly the kind of silent
            ambiguity this register exists to remove.
    """
    index: Dict[str, Tuple[str, bool]] = {}
    for key, statute in statutes.items():
        for alias in set(statute.aliases) | {normalize_act_name(statute.canonical_name)}:
            if alias in index and index[alias] != (key, False):
                raise RegisterError(f"Alias {alias!r} claimed by {index[alias][0]} and {key}")
            index[alias] = (key, False)
    for key, lg in legacy.items():
        for alias in set(lg.aliases) | {normalize_act_name(lg.canonical_name)}:
            if alias in index and index[alias] != (key, True):
                raise RegisterError(f"Alias {alias!r} claimed by {index[alias][0]} and {key}")
            index[alias] = (key, True)
    return index


def _validate(register: StatuteRegister) -> List[str]:
    """
    Internal consistency checks. Returns problems; the loader raises on any.

    The important one is the last: a legacy mapping whose target section is not
    in the successor act would resolve a real IPC citation onto a section the
    platform cannot vouch for.
    """
    problems: List[str] = []
    for key, statute in register.statutes.items():
        if not statute.canonical_name:
            problems.append(f"{key}: canonical_name is empty")
        for section in statute.sections:
            if section != normalize_section_number(section):
                problems.append(
                    f"{key} s.{section}: not in canonical form "
                    f"(expected {normalize_section_number(section)!r})"
                )
        if statute.total_sections and len(statute.sections) > statute.total_sections:
            problems.append(
                f"{key}: {len(statute.sections)} sections registered but "
                f"total_sections is {statute.total_sections}"
            )
    for key, lg in register.legacy.items():
        successor = register.statutes.get(lg.succeeded_by)
        if successor is None:
            problems.append(f"{key}: succeeded_by {lg.succeeded_by!r} is not a registered act")
            continue
        for old, new in lg.section_map.items():
            if old != normalize_section_number(old):
                problems.append(f"{key} s.{old}: mapping key not in canonical form")
            if not successor.has_section(new):
                problems.append(
                    f"{key} s.{old} -> {lg.succeeded_by} s.{new}: target section is "
                    "not in the register"
                )
    return problems


def load_register(path: Optional[Path] = None) -> StatuteRegister:
    """
    Load and validate the statutory register.

    Args:
        path: Register file; defaults to the shipped seed.

    Returns:
        The validated register.

    Raises:
        RegisterError: If the file is missing, unparseable, or internally
            inconsistent. Failing closed is deliberate — a silently empty
            register would mark every real citation as unverified.
    """
    register_path = Path(path) if path else DEFAULT_REGISTER_PATH
    try:
        raw = json.loads(register_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegisterError(f"Statutory register not found at {register_path}") from exc
    except json.JSONDecodeError as exc:
        raise RegisterError(f"Statutory register at {register_path} is not valid JSON: {exc}") from exc

    statutes = {
        key: Statute(
            key=key,
            canonical_name=body.get("canonical_name", ""),
            short_title=body.get("short_title", key),
            year=body.get("year"),
            framework=body.get("framework", key),
            total_sections=body.get("total_sections"),
            replaces=body.get("replaces"),
            aliases=frozenset(normalize_act_name(a) for a in body.get("aliases", [])),
            sections={
                normalize_section_number(s): v for s, v in (body.get("sections") or {}).items()
            },
        )
        for key, body in (raw.get("statutes") or {}).items()
    }
    legacy = {
        key: LegacyStatute(
            key=key,
            canonical_name=body.get("canonical_name", ""),
            short_title=body.get("short_title", key),
            year=body.get("year"),
            succeeded_by=body.get("succeeded_by", ""),
            aliases=frozenset(normalize_act_name(a) for a in body.get("aliases", [])),
            section_map={
                normalize_section_number(o): normalize_section_number(n)
                for o, n in (body.get("section_map") or {}).items()
            },
        )
        for key, body in (raw.get("legacy_statutes") or {}).items()
    }

    register = StatuteRegister(
        version=raw.get("register_version", "unknown"),
        status=raw.get("status", "seed"),
        coverage=raw.get("coverage", "partial"),
        statutes=statutes,
        legacy=legacy,
        _alias_index=_build_alias_index(statutes, legacy),
    )

    problems = _validate(register)
    if problems:
        raise RegisterError(
            f"Statutory register at {register_path} failed validation:\n  - "
            + "\n  - ".join(problems)
        )

    logger.info(
        "Statutory register {} loaded ({}, {} acts, {} sections, {} legacy mappings)",
        register.version,
        register.status,
        len(statutes),
        sum(len(s.sections) for s in statutes.values()),
        sum(len(l.section_map) for l in legacy.values()),
    )
    if not register.is_complete:
        logger.info(
            "Statutory seed register is a {}: it supplies the IPC/CrPC/Evidence Act "
            "mappings; section checks use the India Code section index",
            register.status,
        )
    return register


@lru_cache(maxsize=4)
def get_register(path: Optional[str] = None) -> StatuteRegister:
    """Load-once accessor. ``path`` is a string so the cache key stays hashable."""
    return load_register(Path(path) if path else None)


# ---------------------------------------------------------------------------
# Section index: every section of every act fetched from India Code
# ---------------------------------------------------------------------------

#: Built by ``scripts/build_statute_index.py`` from the India Code rebuild.
DEFAULT_SECTION_INDEX_PATH = Path(__file__).parent / "data" / "sections.json"

#: Seed-register keys whose sections the index holds in full.
_SEED_TO_INDEX = {"BNS": "BNS", "BNSS": "BNSS", "BSA": "BSA", "IT_ACT": "IT"}

#: Common, unambiguous abbreviations for indexed acts. Codes alone are never
#: aliases ("CA", "RA", "IT" are words or collide); only these are.
_INDEX_ABBREVIATIONS = {
    "NIA": ("ni act", "n i act"),
    "CPC": ("cpc", "c p c"),
    "IBC": ("ibc", "insolvency code"),
    "SARFAESI": ("sarfaesi", "sarfaesi act"),
    "PMLA": ("pmla",),
    "NDPS": ("ndps", "ndps act"),
    "POCSO": ("pocso", "pocso act"),
    "RERA": ("rera", "rera act"),
    "FEMA": ("fema",),
    "PWDVA": ("pwdva", "dv act", "domestic violence act"),
    "POSH": ("posh act",),
    "UAPA": ("uapa",),
    "RTI": ("rti act",),
    "ACA": ("arbitration act",),
    "CPA": ("consumer protection act",),
    "HMA": ("hma",),
    "MVA": ("mv act", "motor vehicles act"),
}

#: A reference to an order, rule, article or schedule is not a section; its
#: number must never be checked against the act's sections ("Order 39 Rule 1"
#: is not section 39 of the CPC).
_NOT_A_SECTION = re.compile(r"\b(order|rule|article|art|schedule|regulation|form|para(graph)?)\b", re.I)


def section_key(value: str) -> str:
    """Lookup key for a section number: canonical, sub-sections dropped,
    hyphens removed (India Code writes "45-IA", documents write "45IA")."""
    token = normalize_section_number(value)
    token = token.split("(", 1)[0]
    token = re.sub(r"[\s\-]", "", token).upper()
    m = re.match(r"\d+[A-Z]*", token)
    return m.group(0) if m else ""


def _act_key(name: str) -> str:
    text = normalize_act_name(name)
    text = re.sub(r"[()'\-]", " ", text)
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _without_year(key: str) -> str:
    return re.sub(r"\s*\b(1[89]\d\d|20\d\d)\b\s*", " ", key).strip()


@dataclass(frozen=True)
class SectionIndex:
    """Complete section lists of the acts India Code publishes."""

    version: str
    acts: Mapping[str, Mapping[str, object]]
    repealed: Mapping[str, Mapping[str, str]]
    _aliases: Mapping[str, str] = field(repr=False, default_factory=dict)

    def lookup_act(self, act_name: str) -> Optional[str]:
        """Act code for a name: exact on the full name, then on the name
        without its year (only when that is unambiguous), then on a curated
        abbreviation; finally the longest full name contained in the text."""
        key = _act_key(act_name)
        if not key:
            return None
        for candidate in (key, _without_year(key)):
            if candidate in self._aliases:
                return self._aliases[candidate]
        for alias in sorted(self._aliases, key=len, reverse=True):
            if len(alias) >= 12 and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", key):
                return self._aliases[alias]
        return None


def _build_index_aliases(acts, repealed) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    by_bare: Dict[str, set] = {}
    for code, body in list(acts.items()) + list(repealed.items()):
        full = _act_key(str(body.get("name") or ""))
        if full:
            aliases[full] = code
            by_bare.setdefault(_without_year(full), set()).add(code)
    # "Merchant Shipping Act" names both the 1958 and the 2025 Act: no year, no match.
    for bare, codes in by_bare.items():
        if len(codes) == 1 and bare not in aliases:
            aliases[bare] = next(iter(codes))
    for code, abbreviations in _INDEX_ABBREVIATIONS.items():
        if code in acts:
            for abbreviation in abbreviations:
                aliases.setdefault(abbreviation, code)
    return aliases


@lru_cache(maxsize=2)
def get_section_index(path: Optional[str] = None) -> Optional[SectionIndex]:
    """Load-once accessor. Returns None (with a warning) when the index is
    missing or unreadable: the check then falls back to the seed register."""
    index_path = Path(path) if path else DEFAULT_SECTION_INDEX_PATH
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Statute section index unavailable ({}); using the seed register only", exc)
        return None
    acts = raw.get("acts") or {}
    repealed = raw.get("repealed") or {}
    index = SectionIndex(
        version=str(raw.get("index_version", "unknown")),
        acts=acts,
        repealed=repealed,
        _aliases=_build_index_aliases(acts, repealed),
    )
    logger.info(
        "Statute section index {} loaded ({} acts, {} sections, {} repealed acts)",
        index.version, len(acts), sum(len(a.get("sections") or {}) for a in acts.values()), len(repealed),
    )
    return index


# ---------------------------------------------------------------------------
# Row-level checks used by the applicable_laws panel
# ---------------------------------------------------------------------------


def section_number(section: str) -> Optional[str]:
    """Canonical section token for a row's ``section`` text, or None."""
    token = normalize_section_number(section)
    return token or None


def check_provision(act: str, section: str) -> Dict[str, object]:
    """Register status for one applicable-laws row.

    * ``verified``   — the section exists in the act (with its official
      ``heading``);
    * ``invalid``    — the act's full section list is known (India Code) and
      the section is not in it, or it is an omitted section
      (``"omitted": true``); for a seed-only act, the number exceeds the
      act's ``total_sections``;
    * ``unverified`` — the act is unknown, superseded (IPC/CrPC/Evidence Act,
      or ``"repealed": true`` with ``repealedBy``), the reference is not a
      section (an Order, Rule, Article or Schedule), or no number is given.
      Repeal is never ``invalid``: the old act still governs earlier facts.

    Never raises: a missing or malformed register or index degrades to
    ``unverified``.
    """
    num = section_number(section)
    try:
        register: Optional[StatuteRegister] = get_register()
    except RegisterError as exc:
        logger.warning("Statutory register unavailable: {}", exc)
        register = None
    key, is_legacy = register.lookup_act(act) if register else (None, False)
    if is_legacy:
        return {"registerStatus": "unverified", "statute": key}

    index = get_section_index()
    code = _SEED_TO_INDEX.get(key) if key else None
    if index and not code:
        code = index.lookup_act(act)
    statute_label = key or code
    if not num or _NOT_A_SECTION.search(str(section or "")):
        return {"registerStatus": "unverified", **({"statute": statute_label} if statute_label else {})}

    if index and code in index.repealed:
        return {"registerStatus": "unverified", "statute": code, "repealed": True,
                "repealedBy": index.repealed[code].get("repealedBy")}
    if index and code in index.acts:
        entry = (index.acts[code].get("sections") or {}).get(section_key(num))
        if not entry:
            return {"registerStatus": "invalid", "statute": statute_label}
        if entry.get("o"):
            return {"registerStatus": "invalid", "statute": statute_label,
                    "heading": entry.get("h"), "omitted": True}
        return {"registerStatus": "verified", "statute": statute_label, "heading": entry.get("h")}

    if not key or register is None:
        return {"registerStatus": "unverified"}
    statute = register.statutes[key]
    if statute.has_section(num):
        return {"registerStatus": "verified", "statute": key, "heading": statute.heading(num)}
    digits = re.match(r"\d+", num)
    if digits and statute.total_sections and int(digits.group(0)) > statute.total_sections:
        return {"registerStatus": "invalid", "statute": key}
    return {"registerStatus": "unverified", "statute": key}


def annotate_law_rows(payload: object) -> None:
    """Add the register check and the official section title to every
    applicable-laws row, in place. Idempotent.

    Additive fields only: ``registerStatus`` (+ ``statute``, ``omitted``,
    ``repealed``/``repealedBy``), ``heading`` and ``sectionTitle`` — the
    official heading, e.g. "Mode of assignment" for Copyright Act s.19, for
    the panel to show as "Section 19 – Mode of assignment". ``section`` itself
    is never changed: merges, drafting and search all match on it.
    """
    rows = payload.get("laws") if isinstance(payload, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        # Recomputed every time (dictionary lookups only): an edited row
        # ("Section 19" -> "Section 20") must not keep the old title.
        for stale in ("registerStatus", "statute", "heading", "sectionTitle", "omitted", "repealed", "repealedBy"):
            row.pop(stale, None)
        row.update(check_provision(str(row.get("act") or ""), str(row.get("section") or "")))
        if row.get("heading"):
            row["sectionTitle"] = row["heading"]


if __name__ == "__main__":
    print(json.dumps(get_register().summary(), indent=2))
