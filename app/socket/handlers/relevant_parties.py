"""Relevant Parties extraction and role classification.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import difflib
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from app.ai.usage import current_usage, meter_usage
from app.prompts.legal_intelligence_system import (
    get_structured_document_feature_system_instruction,
)

from app.socket.event_utils import (
    scrub_app_brand,
)


from app.prompts.relevant_parties_prompts import (
    classify_relevant_parties_prompt,
)


class RelevantPartiesMixin:
    """Relevant Parties extraction and role classification."""

    # Honorific / corporate prefixes stripped from the head of a party name.
    _PARTY_PREFIX_RE = re.compile(
        r"^(?:m/s|m/s\.|messrs|messrs\.|mr|mr\.|mrs|mrs\.|ms|ms\.|dr|dr\.|"
        r"shri|smt|kum|the)\s+",
        re.IGNORECASE,
    )
    # Corporate / entity suffixes stripped from the tail (after prefixes and
    # punctuation are normalised). Order matters — longer variants first so
    # "private limited" is removed before a bare "limited"/"ltd".
    _PARTY_SUFFIX_RE = re.compile(
        r"\s+(?:private\s+limited|pvt\s+ltd|p\s+ltd|private\s+ltd|public\s+limited|"
        r"limited|ltd|llp|inc|incorporated|corporation|corp|company|co|"
        r"and\s+ors|and\s+others|ors|anr|and\s+anr)$",
        re.IGNORECASE,
    )

    @classmethod
    def _party_name_key(cls, name: str) -> str:
        """Canonical identity key for deduping/matching party names.

        The bare lower+collapse key treated obvious variants of the SAME entity
        as different parties, so a non-deterministic re-extraction (e.g. after a
        document delete) re-surfaced them as "new" duplicates — "M/s Pro Sports
        Outlets Pvt. Ltd." vs "PRO SPORTS OUTLETS PVT LTD", "IMPERATIVE ..." vs a
        typo'd "IMPERATIV ...". This canonicaliser strips honorific/corporate
        prefixes, parenthetical tokens like "(P)", corporate suffixes, and all
        punctuation so those collapse to the same key. A residual 1–2 char
        spelling drift is caught separately by ``_party_names_match`` (fuzzy).
        """
        s = re.sub(r"\s+", " ", (name or "").strip().lower())
        # Drop parenthetical qualifiers e.g. "(p)", "(india)" — noise for identity.
        s = re.sub(r"\([^)]*\)", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        # Strip a leading honorific/corporate prefix BEFORE punctuation is
        # normalised, so "m/s"/"messrs."/"mr." still match while intact (may
        # repeat, e.g. "the m/s").
        prev = None
        while prev != s:
            prev = s
            s = cls._PARTY_PREFIX_RE.sub("", s).strip()
        # Normalise all remaining punctuation to spaces ("pvt. ltd." -> "pvt ltd").
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        # Strip trailing corporate/multi-party suffixes (may stack, e.g.
        # "... pvt ltd and ors").
        prev = None
        while prev != s:
            prev = s
            s = cls._PARTY_SUFFIX_RE.sub("", s).strip()
        return s.strip(" .,")

    @classmethod
    def _party_names_match(cls, key_a: str, key_b: str) -> bool:
        """True when two canonical party keys are the same entity.

        Exact canonical match, or a high fuzzy ratio to absorb a residual
        1–2 character spelling drift from a re-extraction ("imperativ" vs
        "imperative"). The 0.92 threshold + shared-first-token guard keep
        genuinely distinct parties (e.g. "ram kumar" vs "shyam kumar") apart.
        """
        if not key_a or not key_b:
            return False
        if key_a == key_b:
            return True
        # Guard against over-merging: require the first token to agree so
        # different lead names never collapse on a coincidental tail match.
        first_a = key_a.split(" ", 1)[0]
        first_b = key_b.split(" ", 1)[0]
        if first_a and first_b and first_a != first_b:
            if difflib.SequenceMatcher(None, first_a, first_b).ratio() < 0.85:
                return False
        return difflib.SequenceMatcher(None, key_a, key_b).ratio() >= 0.92

    def _normalize_relevant_parties_payload(self, raw: Any) -> Dict[str, Any]:
        """Coerce roles to the enum, dedupe by name, cap, stamp ids, scrub brand."""
        items_raw: Any
        if isinstance(raw, dict):
            items_raw = raw.get("parties") or raw.get("relevantParties")
        else:
            items_raw = raw
        if not isinstance(items_raw, list):
            items_raw = []

        out: List[Dict[str, Any]] = []
        seen: List[str] = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            # `details` is the contract field; fall back to legacy `description`.
            details = str(item.get("details") or item.get("description") or "").strip()
            if not name:
                continue
            key = self._party_name_key(name)
            # Fuzzy identity check so name variants of the same entity collapse
            # (see _party_names_match), not just exact-key duplicates.
            if not key or any(self._party_names_match(key, k) for k in seen):
                continue
            seen.append(key)
            out.append(
                {
                    "id": f"party_{len(out) + 1}",
                    "name": name,
                    "role": self._normalize_party_role(item.get("role")),
                    "details": details,
                    # PII — empty on auto-gen; only the user fills these in via
                    # the add/edit_relevant_party panel-edit events.
                    "contactNumber": str(item.get("contactNumber") or "").strip(),
                    "email": str(item.get("email") or "").strip(),
                    "officialIdUrl": str(item.get("officialIdUrl") or "").strip(),
                }
            )
            if len(out) >= 30:
                break
        return scrub_app_brand({"parties": out})

    async def _classify_relevant_parties(
        self,
        workspace_role: str,
        raw_parties: List[Dict[str, Any]],
        evidence_snippet: str,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        """Classify already-extracted parties into the normalized role enum."""
        from app.ai.gemini_client import gemini_client

        if not raw_parties:
            return {"parties": []}

        listing = []
        for idx, p in enumerate(raw_parties, start=1):
            if not isinstance(p, dict):
                continue
            listing.append(
                f"{idx}. name={p.get('name','')} | designation={p.get('designation','')} "
                f"| role_in_matter={p.get('role_in_matter','')} "
                f"| first_mention={p.get('first_mention_doc','')}"
            )
        parties_blob = "\n".join(listing)

        prompt = classify_relevant_parties_prompt(evidence_snippet=evidence_snippet, parties_blob=parties_blob, workspace_role=workspace_role)
        # Authoritative user corrections lead the prompt so they outrank
        # the document content they contradict.
        prompt = self._with_user_facts(workspace_id, prompt)
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.1,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        text = self._prepare_json_candidate(raw or "", prefer="object") or ""
        parsed: Dict[str, Any] = {"parties": []}
        try:
            parsed = json.loads(text)
        except Exception:
            logger.warning("Relevant parties: failed to parse classification JSON")
            parsed = {"parties": []}
        return self._normalize_relevant_parties_payload(parsed)

    def _merge_relevant_parties_with_previous(
        self, workspace_id: str, new_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Union previous + new parties by name; drop user-deleted; preserve edits.

        Previous entries are kept first so any user edits survive a regeneration;
        new (non-duplicate) parties are appended; anything the user explicitly
        deleted is filtered out so it does not silently reappear.
        """

        def _list(payload: Any) -> List[Dict[str, Any]]:
            items = payload.get("parties") if isinstance(payload, dict) else None
            return (
                [p for p in items if isinstance(p, dict)]
                if isinstance(items, list)
                else []
            )

        prev = _list(self._workspace_relevant_parties_payload.get(workspace_id))
        new = _list(new_payload)
        # Re-canonicalise stored deleted keys through the current key logic so a
        # user-deleted party stays deleted even if it was stored under the older
        # (weaker) key form.
        deleted = [
            self._party_name_key(k)
            for k in (self._workspace_deleted_parties.get(workspace_id) or [])
            if str(k or "").strip()
        ]

        merged: List[Dict[str, Any]] = []
        seen: List[str] = []
        for p in prev + new:
            key = self._party_name_key(p.get("name"))
            # Fuzzy identity: a name variant of an already-kept party (prev
            # entries are processed first, so user edits win) or of a
            # user-deleted party is skipped rather than re-added as "new".
            if (
                not key
                or any(self._party_names_match(key, k) for k in seen)
                or any(self._party_names_match(key, k) for k in deleted)
            ):
                continue
            seen.append(key)
            # Coerce to the contract shape so legacy-cached entries (old
            # `description`, no PII keys) survive a regeneration consistently.
            merged.append(
                {
                    "id": p.get("id") or "",
                    "name": str(p.get("name") or "").strip(),
                    "role": self._normalize_party_role(p.get("role")),
                    "details": str(
                        p.get("details") or p.get("description") or ""
                    ).strip(),
                    "contactNumber": str(p.get("contactNumber") or "").strip(),
                    "email": str(p.get("email") or "").strip(),
                    "officialIdUrl": str(p.get("officialIdUrl") or "").strip(),
                }
            )
            if len(merged) >= 30:
                break
        for i, p in enumerate(merged):
            p["id"] = f"party_{i + 1}"
        return {"parties": merged}

    @staticmethod
    def _party_source_doc_ids(party: Dict[str, Any]) -> List[str]:
        """Non-empty ``sourceDocIds`` for a party (the doc(s) it was drawn from)."""
        raw = party.get("sourceDocIds")
        if not isinstance(raw, list):
            return []
        return [str(s).strip() for s in raw if str(s or "").strip()]

    def _merge_relevant_parties_provenance(
        self,
        workspace_id: str,
        prev_parties: List[Dict[str, Any]],
        new_parties: List[Dict[str, Any]],
        *,
        max_items: int = 30,
    ) -> Dict[str, Any]:
        """Union previous + new parties, deduped by fuzzy identity, carrying the
        per-document ``sourceDocIds`` provenance.

        Previous entries are processed first so user edits / user-added parties
        win; a new party matching an existing one folds its ``sourceDocIds`` into
        the kept entry (so a party present in several documents is sourced to all
        of them). User-deleted parties stay dropped, and the ``userAdded`` flag is
        preserved so provenance-based purging never removes a manually-added
        party.
        """
        deleted = [
            self._party_name_key(k)
            for k in (self._workspace_deleted_parties.get(workspace_id) or [])
            if str(k or "").strip()
        ]
        merged: List[Dict[str, Any]] = []
        seen_keys: List[Tuple[str, int]] = []  # (name_key, index into merged)
        for p in list(prev_parties) + list(new_parties):
            name = str(p.get("name") or "").strip()
            key = self._party_name_key(name)
            if not key or any(self._party_names_match(key, k) for k in deleted):
                continue
            hit = next(
                (idx for k, idx in seen_keys if self._party_names_match(key, k)),
                None,
            )
            if hit is not None:
                existing = merged[hit]
                union = list(
                    dict.fromkeys(
                        self._party_source_doc_ids(existing)
                        + self._party_source_doc_ids(p)
                    )
                )
                existing["sourceDocIds"] = union
                if p.get("userAdded"):
                    existing["userAdded"] = True
                continue
            merged.append(
                {
                    "id": p.get("id") or "",
                    "name": name,
                    "role": self._normalize_party_role(p.get("role")),
                    "details": str(
                        p.get("details") or p.get("description") or ""
                    ).strip(),
                    "contactNumber": str(p.get("contactNumber") or "").strip(),
                    "email": str(p.get("email") or "").strip(),
                    "officialIdUrl": str(p.get("officialIdUrl") or "").strip(),
                    "sourceDocIds": self._party_source_doc_ids(p),
                    "userAdded": bool(p.get("userAdded")),
                }
            )
            seen_keys.append((key, len(merged) - 1))
            if len(merged) >= max_items:
                break
        for i, p in enumerate(merged):
            p["id"] = f"party_{i + 1}"
        return {"parties": merged}

    def _tag_parties_against_current_state(
        self, workspace_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Tag each emitted party as matched vs new against the frontend's
        currentState.relevantParties. A party whose name matches a previously
        curated party carries that party's id and ``isMatched=True``; otherwise
        it keeps its ``party_N`` id and ``isMatched=False``.

        Party names have robust canonical matching (`_party_names_match`), so
        this is deterministic — no LLM call needed. Status groups (approved /
        pending) are flattened; curation status is ignored.
        """
        if not isinstance(payload, dict):
            return payload
        current_state = self._workspace_current_state_payload.get(workspace_id) or {}
        cs_parties = (
            current_state.get("relevantParties")
            if isinstance(current_state, dict)
            else None
        )
        prev: List[Dict[str, str]] = []
        if isinstance(cs_parties, dict):
            for group in ("approved", "pending"):
                for it in cs_parties.get(group) or []:
                    if isinstance(it, dict) and it.get("id") and it.get("name"):
                        prev.append(
                            {
                                "id": str(it["id"]),
                                "key": self._party_name_key(it["name"]),
                            }
                        )
        consumed: Set[str] = set()
        for party in payload.get("parties") or []:
            if not isinstance(party, dict):
                continue
            fkey = self._party_name_key(party.get("name"))
            matched_id = None
            for p in prev:
                if p["id"] in consumed or not p["key"]:
                    continue
                if fkey and self._party_names_match(fkey, p["key"]):
                    matched_id = p["id"]
                    consumed.add(p["id"])
                    break
            if matched_id:
                party["id"] = matched_id
                party["isMatched"] = True
            else:
                party["isMatched"] = False
        return payload

    @meter_usage
    async def _generate_and_emit_relevant_parties(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
    ) -> None:
        """Generate and emit document_feature_response with featureType relevant_parties.

        Per-document provenance model (deterministic on delete, incremental on
        upload): each party carries the ``sourceDocIds`` it was extracted from.
        Rather than re-extract the whole workspace on every change (which churned
        "New for Review" as the non-deterministic extractor drifted), this:

          * PURGES parties whose source documents were all deleted;
          * EXTRACTS parties ONLY from documents not previously seen (a new
            upload), tagging them with that document's id;
          * FULL-REFRESHES (re-derives every live doc, keeping only user-added
            parties) when provenance is missing/stale — e.g. first run or a
            legacy cache — so the model self-heals into the provenance world.

        So deleting a document removes exactly that document's parties, and
        uploading one adds only the new document's parties.
        """
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        logger.info(
            "Relevant parties: generation started for workspace {}",
            workspace_id[:12] if workspace_id else "",
        )
        from app.vectorstore.qdrant import vector_store

        try:
            all_docs = await vector_store.get_all_documents(workspace_id=workspace_id)
        except Exception as e:
            logger.warning("Relevant parties: could not fetch documents: {}", e)
            all_docs = []
        live_docs = [
            d
            for d in (all_docs or [])
            if str(d.get("document_id") or "").strip()
            and (d.get("content") or "").strip()
        ]
        live_doc_ids = {str(d.get("document_id")).strip() for d in live_docs}
        content_by_id = {
            str(d.get("document_id")).strip(): (d.get("content") or "")
            for d in live_docs
        }

        prev_payload = self._workspace_relevant_parties_payload.get(workspace_id) or {
            "parties": []
        }
        prev_parties = [
            p for p in (prev_payload.get("parties") or []) if isinstance(p, dict)
        ]

        # Documents that have already contributed at least one party.
        already_seen_docs: Set[str] = set()
        for p in prev_parties:
            already_seen_docs.update(self._party_source_doc_ids(p))
        # Docs present now but never extracted → newly uploaded (or, if NONE of
        # the live docs were ever attributed, a legacy/first-run full refresh).
        new_doc_ids = [d for d in live_doc_ids if d not in already_seen_docs]
        full_refresh = bool(new_doc_ids) and set(new_doc_ids) == live_doc_ids

        # 1) Carry forward prior parties, dropping those orphaned by a deletion.
        if full_refresh:
            # Re-deriving every live doc below — keep only user-added parties;
            # auto-extracted ones (incl. any from now-deleted docs) are replaced.
            carried_prev = [p for p in prev_parties if p.get("userAdded")]
        else:
            carried_prev = []
            for p in prev_parties:
                if p.get("userAdded"):
                    carried_prev.append(p)
                    continue
                src = self._party_source_doc_ids(p)
                if not src:
                    # Unattributed auto entry (legacy) — keep on non-full runs.
                    carried_prev.append(p)
                    continue
                live_src = [s for s in src if s in live_doc_ids]
                if not live_src:
                    continue  # every source document deleted → drop
                p = dict(p)
                p["sourceDocIds"] = live_src
                carried_prev.append(p)

        # 2) Extract parties ONLY from new/unseen documents, per document.
        new_parties: List[Dict[str, Any]] = []
        if new_doc_ids:
            signals = await self._extract_case_signals_from_documents(
                workspace_id, user_id
            )
            evidence = signals.get("evidenceSnippet", "") if isinstance(signals, dict) else ""

            async def _extract_one(doc_id: str) -> Tuple[str, List[Dict[str, Any]]]:
                res = await self._extract_parties_subagent(
                    workspace_id, content_by_id.get(doc_id, "")
                )
                raws = res.get("parties") if isinstance(res, dict) else []
                return doc_id, [r for r in (raws or []) if isinstance(r, dict)]

            results = await asyncio.gather(
                *[_extract_one(d) for d in new_doc_ids], return_exceptions=True
            )
            raw_all: List[Dict[str, Any]] = []
            src_docs_by_key: Dict[str, Set[str]] = {}
            for r in results:
                if isinstance(r, Exception) or not isinstance(r, tuple):
                    logger.warning("Relevant parties: per-doc extraction failed: {}", r)
                    continue
                doc_id, raws = r
                for raw in raws:
                    nm = str(raw.get("name") or "").strip()
                    if not nm:
                        continue
                    raw_all.append(raw)
                    src_docs_by_key.setdefault(
                        self._party_name_key(nm), set()
                    ).add(doc_id)

            if raw_all:
                classified = await self._classify_relevant_parties(
                    role, raw_all, evidence, workspace_id=workspace_id
                )
                for cp in classified.get("parties") or []:
                    if not isinstance(cp, dict):
                        continue
                    ck = self._party_name_key(cp.get("name"))
                    src_ids: Set[str] = set()
                    for rk, dids in src_docs_by_key.items():
                        if self._party_names_match(ck, rk):
                            src_ids |= dids
                    cp = dict(cp)
                    cp["sourceDocIds"] = sorted(src_ids)
                    cp["userAdded"] = False
                    new_parties.append(cp)

        # 3) Merge carried-forward + newly-extracted (fuzzy dedup + provenance).
        payload = self._merge_relevant_parties_provenance(
            workspace_id, carried_prev, new_parties
        )
        # Tag each party matched vs new against the frontend's currentState
        # (carry the FE id on match, else keep party_N). Cache the tagged
        # payload so reconnect re-emits the same ids.
        payload = self._tag_parties_against_current_state(workspace_id, payload)
        self._workspace_relevant_parties_payload[workspace_id] = payload
        logger.info(
            "Relevant parties: {} live doc(s), {} newly-extracted doc(s), "
            "{} party/ies emitted (full_refresh={})",
            len(live_doc_ids),
            len(new_doc_ids),
            len(payload.get("parties", [])),
            full_refresh,
        )
        if not self._socket_client:
            return
        await self._emit_document_feature(
            feature_type="relevant_parties",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=payload,
            usage=current_usage(),
        )
        logger.success(
            "Relevant parties sent to backend ({} parties)",
            len(payload.get("parties", [])),
        )
        await asyncio.sleep(0.1)
