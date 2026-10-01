"""
Entity resolution — one real-world thing, one node (work-plan task 2.4).

The ontology's value depends on this. If "M/s Meridian Traders", "Meridian
Traders Pvt Ltd" and "the complainant company" become three Party nodes, then
the party network is wrong, the obligation chain is broken, and every panel that
reads the graph inherits the error.

Two failure modes, and they are not symmetric:

* A **split** — one real party appearing as several nodes — makes the graph
  incomplete. Bad, recoverable by widening aliasing.
* A **merge** — several real parties collapsed into one node — makes the graph
  *wrong*, and in a legal setting attributes one party's obligations to another.

This module is therefore deliberately conservative: it merges only on strong
evidence, and :func:`resolve_parties` reports every merge it made with the
score that justified it, so an SME can audit the decisions rather than trust
them. Acceptance metric 10.1 measures precision over the resolved set.

Resolution runs per workspace. Two parties in different matters are never
candidates for merging, however alike their names.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from loguru import logger
from rapidfuzz import fuzz

#: Similarity at or above which two names are treated as the same entity.
#: Tuned high: a merge is the dangerous error, so the bar to make one is high.
DEFAULT_MERGE_THRESHOLD = 92.0

#: Corporate suffixes stripped before comparison. "Meridian Traders Private
#: Limited" and "Meridian Traders Pvt Ltd" must reduce to the same core.
#: Sorted longest-first at module load: "co" would otherwise match before
#: "& co" and leave a dangling ampersand behind.
_CORPORATE_SUFFIXES_RAW = (
    "private limited",
    "pvt limited",
    "pvt ltd",
    "private ltd",
    "p ltd",
    "limited",
    "ltd",
    "llp",
    "llc",
    "incorporated",
    "inc",
    "corporation",
    "corp",
    "company",
    "co",
    "and sons",
    "& sons",
    "and co",
    "& co",
)

_CORPORATE_SUFFIXES = tuple(sorted(_CORPORATE_SUFFIXES_RAW, key=len, reverse=True))

#: Honorifics and filing prefixes that carry no identity.
_HONORIFICS = (
    "m/s",
    "messrs",
    "mr",
    "mrs",
    "ms",
    "miss",
    "shri",
    "smt",
    "sri",
    "kum",
    "dr",
    "prof",
    "adv",
    "advocate",
    "hon'ble",
    "honble",
    "the",
)

#: Referential phrases that name a role, not a party. These must never be used
#: as a canonical name, and never as evidence for a merge — "the accused" in two
#: documents may well be two different people.
_ROLE_REFERENTS = frozenset(
    {
        "the complainant",
        "complainant",
        "the accused",
        "accused",
        "the petitioner",
        "petitioner",
        "the respondent",
        "respondent",
        "the appellant",
        "appellant",
        "the applicant",
        "applicant",
        "the defendant",
        "defendant",
        "the plaintiff",
        "plaintiff",
        "the opposite party",
        "opposite party",
        "the company",
        "the complainant company",
        "the said company",
        "the deceased",
        "the victim",
    }
)


def normalize_name(name: str) -> str:
    """
    Reduce a name to its comparable core.

    Lowercases, strips punctuation, drops honorifics and corporate suffixes, and
    collapses whitespace. ``"M/s Meridian Traders Private Limited."`` and
    ``"MERIDIAN TRADERS PVT LTD"`` both reduce to ``"meridian traders"``.

    Args:
        name: A name as it appeared in a document.

    Returns:
        The normalised core, or ``""`` if nothing identifying remains.
    """
    text = str(name or "").lower().strip()
    if not text:
        return ""
    text = re.sub(r"[.,;:'\"()\[\]]", " ", text)
    # A trailing ampersand is left behind when "& Co" is stripped as "co".
    text = re.sub(r"\s+&\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    for honorific in _HONORIFICS:
        text = re.sub(rf"^{re.escape(honorific)}\b\s*", "", text).strip()

    changed = True
    while changed:
        changed = False
        for suffix in _CORPORATE_SUFFIXES:
            pattern = rf"\s+{re.escape(suffix)}$"
            if re.search(pattern, text):
                text = re.sub(pattern, "", text).strip()
                changed = True

    return re.sub(r"\s+", " ", text).strip()


def is_role_referent(name: str) -> bool:
    """
    True when the name identifies a role rather than a person or organisation.

    ``"the accused"`` is not an identity: two documents using it may mean two
    different people, so it can never justify a merge or serve as a canonical
    name.
    """
    return " ".join(str(name or "").lower().split()).strip(".,") in _ROLE_REFERENTS


#: Shared tokens required before a subset match is trusted. ``token_set_ratio``
#: scores a subset at 100, which is right for "Bharat Steel" vs "Bharat Steel
#: Corporation" and badly wrong for "Nair" vs "Ravindran Nair" — a bare surname
#: would absorb every person who shares it.
_MIN_SHARED_TOKENS_FOR_SUBSET = 2


def name_similarity(left: str, right: str) -> float:
    """
    Similarity of two names in [0, 100], after normalisation.

    Uses token-set ratio, which is order-insensitive and tolerates one name
    carrying extra tokens — the common case with Indian party names where a
    patronymic or initial appears in one document and not another.

    One correction to that ratio: it scores a strict subset at 100. That is
    correct for a dropped corporate suffix but dangerous for a bare surname, so
    a subset sharing fewer than :data:`_MIN_SHARED_TOKENS_FOR_SUBSET` tokens
    falls back to the order-sensitive ratio, which scores it far lower.
    """
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0

    a_tokens, b_tokens = set(a.split()), set(b.split())
    shared = a_tokens & b_tokens
    is_subset = a_tokens <= b_tokens or b_tokens <= a_tokens
    if is_subset and len(shared) < _MIN_SHARED_TOKENS_FOR_SUBSET:
        return float(fuzz.token_sort_ratio(a, b))
    return float(fuzz.token_set_ratio(a, b))


def is_initialised_form(name: str) -> bool:
    """
    True when a name uses at least one initial in place of a full given name.

    ``"R. Nair"`` and ``"K. S. Iyer"`` are initialised; ``"Ravindran Nair"`` is
    not. These are extremely common in Indian filings and cause splits, but they
    are also ambiguous — ``"R. Nair"`` fits both *Ravindran* and *Rajesh* Nair —
    so they are resolved in a separate, guarded pass.
    """
    tokens = normalize_name(name).split()
    return len(tokens) > 1 and any(len(t) == 1 for t in tokens[:-1])


def initials_compatible(full_name: str, abbreviated: str) -> bool:
    """
    True when ``abbreviated`` could be an initialised form of ``full_name``.

    Requires the final token (the surname) to match exactly, and every earlier
    token of the abbreviation to be either the same word or a single letter
    matching that word's first character. ``"R. Nair"`` is compatible with
    ``"Ravindran Nair"``; ``"S. Nair"`` is not; ``"R. Menon"`` is not.

    Compatibility alone is never enough to merge — see :func:`resolve_entities`,
    which merges an abbreviation only when exactly one cluster is compatible.
    """
    full_tokens = normalize_name(full_name).split()
    abbrev_tokens = normalize_name(abbreviated).split()
    if len(full_tokens) < 2 or len(abbrev_tokens) < 2:
        return False
    if len(abbrev_tokens) > len(full_tokens):
        return False
    if full_tokens[-1] != abbrev_tokens[-1]:
        return False
    for abbrev_token, full_token in zip(abbrev_tokens[:-1], full_tokens[:-1], strict=False):
        if abbrev_token == full_token:
            continue
        if len(abbrev_token) == 1 and full_token.startswith(abbrev_token):
            continue
        return False
    return True


@dataclass
class ResolvedEntity:
    """
    One canonical entity and every surface form that resolved onto it.

    Attributes:
        canonical_name: The chosen name — the longest non-referential surface
            form, on the principle that the fullest form is the most
            identifying.
        aliases: Every other surface form seen, in first-seen order.
        mentions: Number of source mentions that resolved here.
        source_document_ids: Documents this entity was seen in.
        merge_evidence: For each alias merged on similarity rather than exact
            match, the score that justified it. This is the audit trail.
        ambiguous_with: Canonical names this entity could equally have merged
            into. Non-empty means resolution deliberately declined to choose —
            an initialised name matching two full names, for instance. Left as
            a separate node and surfaced for a human.
    """

    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    mentions: int = 0
    source_document_ids: List[str] = field(default_factory=list)
    merge_evidence: List[Dict[str, object]] = field(default_factory=list)
    ambiguous_with: List[str] = field(default_factory=list)

    @property
    def all_names(self) -> List[str]:
        return [self.canonical_name, *self.aliases]

    def to_dict(self) -> Dict[str, object]:
        return {
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "mentions": self.mentions,
            "source_document_ids": sorted(set(self.source_document_ids)),
            "merge_evidence": list(self.merge_evidence),
            "ambiguous_with": list(self.ambiguous_with),
        }


def _pick_canonical(names: Sequence[str]) -> str:
    """
    Choose the canonical form from a cluster's surface forms.

    Prefers the longest non-referential name: the fullest form carries the most
    identifying detail. Falls back to the longest name overall only when every
    form is referential, which happens when a party is never named outright.
    """
    real = [n for n in names if not is_role_referent(n)]
    pool = real or list(names)
    return max(pool, key=lambda n: (len(normalize_name(n)), len(n)))


def resolve_entities(
    mentions: Sequence[Mapping[str, object]],
    threshold: float = DEFAULT_MERGE_THRESHOLD,
) -> List[ResolvedEntity]:
    """
    Cluster surface forms into canonical entities.

    Three passes, ordered so that the least ambiguous evidence decides first and
    the most ambiguous can never create a wrong merge:

    1. **Full names.** Greedy clustering on exact normalised match, then on
       similarity at or above ``threshold``. Greedy rather than globally optimal
       because a merge must be explainable — an SME reviewing one needs the
       single comparison that caused it.
    2. **Initialised names** (``"R. Nair"``). Attached only when *exactly one*
       cluster is initials-compatible. Compatible with two clusters means the
       document genuinely does not say which; it becomes its own entity with
       ``ambiguous_with`` populated rather than being guessed.
    3. **Role referents** (``"the accused"``). Attached only on an exact
       normalised match with a name already in a cluster. They can never pull
       two real parties together.

    Args:
        mentions: Items with ``name`` and optionally ``source_document_id``.
        threshold: Similarity in [0, 100] required to merge two full names.
            Raising it produces more splits; lowering it produces more merges —
            and merges are the error that matters.

    Returns:
        Resolved entities, most-mentioned first.
    """
    clusters: List[ResolvedEntity] = []
    cluster_names: List[List[str]] = []

    def add_to_cluster(
        index: int,
        name: str,
        document_id: object,
        score: Optional[float],
        against: str,
    ) -> None:
        entity = clusters[index]
        entity.mentions += 1
        if document_id:
            entity.source_document_ids.append(str(document_id))
        if name not in cluster_names[index]:
            cluster_names[index].append(name)
            # Recorded whenever the normalised forms differ. A subset match
            # scores 100 but is still an inference, and the audit trail must
            # show it rather than presenting it as an exact repeat.
            if score is not None and normalize_name(name) != normalize_name(against):
                entity.merge_evidence.append(
                    {"merged": name, "with": against, "score": round(score, 2)}
                )

    def new_cluster(name: str, document_id: object) -> int:
        entity = ResolvedEntity(canonical_name=name, mentions=1)
        if document_id:
            entity.source_document_ids.append(str(document_id))
        clusters.append(entity)
        cluster_names.append([name])
        return len(clusters) - 1

    # Bucket the mentions by how trustworthy their evidence is.
    full_names, initialised, referents = [], [], []
    for mention in mentions:
        name = str(mention.get("name") or "").strip()
        if not name or not normalize_name(name):
            continue
        item = (name, mention.get("source_document_id"))
        if is_role_referent(name):
            referents.append(item)
        elif is_initialised_form(name):
            initialised.append(item)
        else:
            full_names.append(item)

    # --- Pass 1: full names ------------------------------------------------
    for name, document_id in full_names:
        normalized = normalize_name(name)
        best_index, best_score, best_against = None, 0.0, ""
        for index, names in enumerate(cluster_names):
            for existing in names:
                if normalize_name(existing) == normalized:
                    best_index, best_score, best_against = index, 100.0, existing
                    break
                score = name_similarity(existing, name)
                if score >= threshold and score > best_score:
                    best_index, best_score, best_against = index, score, existing
            if best_score == 100.0:
                break
        if best_index is None:
            new_cluster(name, document_id)
        else:
            add_to_cluster(best_index, name, document_id, best_score, best_against)

    # --- Pass 2: initialised names, only when unambiguous ------------------
    for name, document_id in initialised:
        normalized = normalize_name(name)
        exact = next(
            (
                i
                for i, names in enumerate(cluster_names)
                if any(normalize_name(n) == normalized for n in names)
            ),
            None,
        )
        if exact is not None:
            add_to_cluster(exact, name, document_id, 100.0, name)
            continue

        compatible = [
            (i, next(n for n in names if initials_compatible(n, name)))
            for i, names in enumerate(cluster_names)
            if any(initials_compatible(n, name) for n in names)
        ]
        if len(compatible) == 1:
            index, against = compatible[0]
            add_to_cluster(index, name, document_id, 99.0, against)
        elif len(compatible) > 1:
            index = new_cluster(name, document_id)
            clusters[index].ambiguous_with = sorted(
                clusters[i].canonical_name for i, _ in compatible
            )
            logger.warning(
                "'{}' is initials-compatible with {} entities; left unresolved "
                "rather than guessed: {}",
                name,
                len(compatible),
                clusters[index].ambiguous_with,
            )
        else:
            new_cluster(name, document_id)

    # --- Pass 3: role referents, exact match only --------------------------
    for name, document_id in referents:
        normalized = normalize_name(name)
        exact = next(
            (
                i
                for i, names in enumerate(cluster_names)
                if any(normalize_name(n) == normalized for n in names)
            ),
            None,
        )
        if exact is None:
            new_cluster(name, document_id)
        else:
            add_to_cluster(exact, name, document_id, 100.0, name)

    # Recompute canonical/aliases now that every cluster is complete.
    for entity, names in zip(clusters, cluster_names, strict=True):
        canonical = _pick_canonical(names)
        entity.canonical_name = canonical
        entity.aliases = [n for n in names if n != canonical]

    clusters.sort(key=lambda e: (-e.mentions, e.canonical_name))
    logger.debug("Entity resolution: {} mentions -> {} entities", len(mentions), len(clusters))
    return clusters


def resolve_parties(
    mentions: Sequence[Mapping[str, object]],
    threshold: float = DEFAULT_MERGE_THRESHOLD,
) -> Dict[str, object]:
    """
    Resolve party mentions and report the merges for SME review.

    Args:
        mentions: Items with ``name`` and optionally ``source_document_id``.
        threshold: Merge threshold, see :func:`resolve_entities`.

    Returns:
        ``{entities, mergeCount, mergesForReview, unresolvedReferents}`` where
        ``mergesForReview`` is every similarity-based merge with its score,
        ``unresolvedReferents`` names entities whose only surface forms are role
        referents, and ``ambiguousAbbreviations`` names initialised forms that
        fitted more than one entity. All three need a human to resolve.
    """
    entities = resolve_entities(mentions, threshold)
    merges = [
        {"canonical": entity.canonical_name, **evidence}
        for entity in entities
        for evidence in entity.merge_evidence
    ]
    unresolved = [
        entity.canonical_name
        for entity in entities
        if all(is_role_referent(n) for n in entity.all_names)
    ]
    ambiguous = [
        {"name": entity.canonical_name, "couldBe": entity.ambiguous_with}
        for entity in entities
        if entity.ambiguous_with
    ]
    if unresolved:
        logger.warning(
            "{} resolved entities are named only by role referent - a human must "
            "identify them: {}",
            len(unresolved),
            unresolved,
        )
    return {
        "entities": [e.to_dict() for e in entities],
        "mergeCount": len(merges),
        "mergesForReview": merges,
        "unresolvedReferents": unresolved,
        "ambiguousAbbreviations": ambiguous,
    }


def canonical_index(entities: Sequence[ResolvedEntity]) -> Dict[str, str]:
    """
    Map every normalised surface form to its canonical name.

    Used when writing the graph: an extracted fact naming "M/s Meridian Traders"
    is attached to the node canonically named "Meridian Traders Private Limited".
    """
    index: Dict[str, str] = {}
    for entity in entities:
        for name in entity.all_names:
            normalized = normalize_name(name)
            if normalized:
                index[normalized] = entity.canonical_name
    return index


def canonicalise(name: str, index: Mapping[str, str]) -> str:
    """Return the canonical form of ``name``, or ``name`` unchanged if unknown."""
    return index.get(normalize_name(name), name)


def reconcile_with_existing(
    new_names: Sequence[str],
    existing_names: Sequence[str],
    threshold: float = DEFAULT_MERGE_THRESHOLD,
) -> Dict[str, str]:
    """
    Map new surface forms onto entities that already exist in the graph.

    Resolution within one extraction pass is not enough. Documents arrive in
    separate ingests, so "M/s Meridian Traders" in today's upload and "Meridian
    Traders Private Limited" in last week's never meet inside a single call —
    and the matter ends up with two nodes for one company. That is precisely the
    **split** this module exists to prevent, and it only shows up across ingests.

    Existing names win: a name already in the graph is the canonical form, and
    re-canonicalising it would orphan every fact already attached to it. A new
    name that matches nothing is returned unchanged and becomes its own entity.

    Ambiguity is refused, not guessed, on the same principle as
    :func:`resolve_entities`: a new name matching two existing entities equally
    is left alone rather than merged into whichever sorted first.

    Args:
        new_names: Surface forms from this ingest.
        existing_names: Canonical names already in the graph for this workspace.
        threshold: Similarity required to merge.

    Returns:
        ``{new_name: canonical_name}`` for every new name that resolved onto an
        existing entity. Names with no match are absent from the mapping.
    """
    if not existing_names:
        return {}

    mapping: Dict[str, str] = {}
    for name in new_names:
        if not name or not normalize_name(name):
            continue
        normalized = normalize_name(name)

        exact = [e for e in existing_names if normalize_name(e) == normalized]
        if exact:
            mapping[name] = exact[0]
            continue

        if is_role_referent(name):
            continue  # a role referent may only match exactly

        if is_initialised_form(name):
            compatible = [e for e in existing_names if initials_compatible(e, name)]
            if len(compatible) == 1:
                mapping[name] = compatible[0]
            elif len(compatible) > 1:
                logger.warning(
                    "'{}' is initials-compatible with {} existing entities; left "
                    "unresolved rather than guessed",
                    name,
                    len(compatible),
                )
            continue

        scored = [
            (name_similarity(existing, name), existing)
            for existing in existing_names
            if name_similarity(existing, name) >= threshold
        ]
        if not scored:
            continue
        scored.sort(reverse=True)
        best_score, best_name = scored[0]
        if len(scored) > 1 and scored[1][0] == best_score:
            logger.warning(
                "'{}' matches {} existing entities equally at {:.1f}; left "
                "unresolved rather than guessed",
                name,
                len(scored),
                best_score,
            )
            continue
        mapping[name] = best_name

    if mapping:
        logger.info(
            "Reconciled {} new name(s) onto entities already in the graph", len(mapping)
        )
    return mapping
