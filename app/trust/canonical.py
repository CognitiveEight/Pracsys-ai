"""
Stage 3 across documents: one real-world subject, one subject key.

Extraction canonicalises names within one pass (and against the graph when one
is connected). Documents arrive in many passes, so the trust store re-resolves
over the whole case before anything is compared:

* **Names** (Party, Advocate, Forum, and the same names inside relations) go
  through :func:`app.ontology.entity_resolution.resolve_entities` — the same
  conservative clustering extraction uses.
* **Free-text subjects** (an Event's description, a Relief's type, an
  Obligation's type) merge only at ``fuzz.token_sort_ratio`` >= 90 and only
  within one event type. ``token_set_ratio`` is deliberately not used: it
  scores a subset as 100, so "legal notice sent" would swallow "reply to legal
  notice sent" and invent a date conflict between two different events.

Two more rules keep different things apart: the numbers in two subjects must
be identical ("Cheque No. 104512" is not "Cheque No. 104513"), and a recurring
subject keyed by its date (``claims._dated``) only merges with the same date.

The representative of a cluster is its shortest key, then the first
alphabetically — not the most mentioned, which a later document restating the
fact more often would change, re-keying every fact and orphaning reviewers'
decisions on them.

Both rules err towards a split. A split under-corroborates (a fact stays
single-source a little longer); a wrong merge manufactures conflicts and
attributes one fact's evidence to another, and the two errors are not
symmetric.
"""

import re
from collections import Counter
from typing import Any, Dict, List, Mapping, Sequence

from rapidfuzz import fuzz

from app.ontology.entity_resolution import canonical_index, resolve_entities
from app.trust.claims import KIND_RELATION, claim_from_dict

#: Labels whose subject is a party-like name.
NAME_LABELS = ("Party", "Advocate", "Forum")

#: Labels whose subject is free text from the passage.
TEXT_LABELS = ("Event", "Relief", "Communication")

#: token_sort_ratio at or above which two free-text subjects are one subject.
TEXT_MERGE_THRESHOLD = 90.0


def _name_map(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """normalised name -> canonical normalised name, over the whole case."""
    mentions = []
    for row in rows:
        claim = row["claim"]
        if claim.get("label") in NAME_LABELS and claim.get("subject"):
            mentions.append({"name": claim["subject"], "source_document_id": row["document_id"]})
    if not mentions:
        return {}
    # The cluster comes from entity resolution; its key is the cluster's
    # shortest normalised name (stable as documents arrive), not resolution's
    # display choice, which a fuller spelling in a later document would change.
    clusters: Dict[str, List[str]] = {}
    for surface, canonical in canonical_index(resolve_entities(mentions)).items():
        clusters.setdefault(canonical, []).append(surface)
    out: Dict[str, str] = {}
    for surfaces in clusters.values():
        keys = sorted({k for k in surfaces if k}, key=lambda k: (len(k), k))
        if not keys:
            continue
        for key in keys[1:]:
            out[key] = keys[0]
    return out


def same_text_subject(a: str, b: str) -> bool:
    """Do two free-text subject keys name one subject? (See module docstring.)"""
    base_a, _, when_a = a.partition("@")
    base_b, _, when_b = b.partition("@")
    if when_a != when_b:
        return False
    if re.findall(r"\d+", base_a) != re.findall(r"\d+", base_b):
        return False
    return fuzz.token_sort_ratio(base_a, base_b) >= TEXT_MERGE_THRESHOLD


def _text_map(rows: Sequence[Mapping[str, Any]]) -> Dict[tuple, str]:
    """(label, event_type, subject_key) -> representative subject_key."""
    by_bucket: Dict[tuple, Counter] = {}
    for row in rows:
        claim = row["claim"]
        if claim.get("label") in TEXT_LABELS and claim.get("subject_key"):
            bucket = (claim["label"], claim.get("event_type") or "")
            by_bucket.setdefault(bucket, Counter())[claim["subject_key"]] += 1

    mapping: Dict[tuple, str] = {}
    for bucket, counts in by_bucket.items():
        # Shortest, then alphabetical: deterministic, and stable as documents
        # arrive (see the module docstring).
        ordered = sorted(counts, key=lambda k: (len(k), k))
        representatives: List[str] = []
        for key in ordered:
            for rep in representatives:
                if same_text_subject(key, rep):
                    if key != rep:
                        mapping[(*bucket, key)] = rep
                    break
            else:
                representatives.append(key)
    return mapping


def _remap_ref(ref: str, names: Mapping[str, str]) -> str:
    """Rewrite a relation's ``Label:key`` when it names a resolved entity."""
    label, _, key = ref.partition(":")
    if label in NAME_LABELS and key in names:
        return f"{label}:{names[key]}"
    return ref


def canonicalise_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Evidence rows with subject (and relation) keys resolved across the case.

    ``fact_key`` and ``slot_key`` are recomputed from the rewritten claim, so
    two spellings of one party's role now corroborate one fact. Rows are copied;
    the stored evidence keeps what extraction produced.
    """
    names = _name_map(rows)
    texts = _text_map(rows)
    out: List[Dict[str, Any]] = []
    for row in rows:
        claim = dict(row["claim"])
        label = claim.get("label")
        if label in NAME_LABELS:
            claim["subject_key"] = names.get(claim["subject_key"], claim["subject_key"])
        elif label in TEXT_LABELS:
            key = (label, claim.get("event_type") or "", claim["subject_key"])
            claim["subject_key"] = texts.get(key, claim["subject_key"])
        elif claim.get("kind") == KIND_RELATION:
            claim["subject_key"] = _remap_ref(claim["subject_key"], names)
            claim["value_key"] = _remap_ref(claim["value_key"], names)
        rebuilt = claim_from_dict(claim)
        out.append(
            {
                **row,
                # What extraction called the subject, before resolution: a
                # reviewer's decision is matched on these when keys move.
                "subject_original": row["claim"].get("subject_key"),
                "claim": rebuilt.to_dict(),
                "fact_key": rebuilt.fact_key,
                "slot_key": rebuilt.slot_key,
            }
        )
    return out
