"""Case-entity extraction sub-agents (parties/timeline/assets).

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import re
from typing import Any, Dict, List

from loguru import logger


from app.socket.event_utils import (
    SPEAKER_LABEL_GUARDRAIL,
)


from app.prompts.extraction_prompts import (
    extract_parties_subagent_prompt,
    extract_timeline_subagent_prompt,
    extract_assets_and_law_subagent_prompt,
)


class ExtractionMixin:
    """Case-entity extraction sub-agents (parties/timeline/assets)."""

    async def _run_extraction_subagent(
        self,
        workspace_id: str,
        prompt: str,
        sub_name: str,
        expected_keys: List[str],
    ) -> Dict[str, Any]:
        """
        Shared driver for the three parallel extraction sub-agents.

        Runs ONE Gemini call (temperature 0.0, JSON mode, max_tokens 12000),
        parses the response, and returns a dict with every ``expected_keys``
        entry populated to a list. Failures are non-fatal: returns
        ``{key: [] for key in expected_keys}`` so other sub-agents and
        downstream synthesis still proceed.
        """
        scaffold: Dict[str, Any] = {k: [] for k in expected_keys}
        from app.ai.gemini_client import gemini_client
        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning(
                "Strategy sub-extractor '{}' failed for workspace {}: {}",
                sub_name,
                workspace_id[:12] if workspace_id else "",
                e,
            )
            return scaffold
        if not raw:
            return scaffold
        try:
            json_str = self._prepare_json_candidate(raw.strip(), prefer="object")
            if not json_str:
                return scaffold
            import json as _json
            parsed = _json.loads(json_str)
            if not isinstance(parsed, dict):
                return scaffold
            for k in expected_keys:
                if not isinstance(parsed.get(k), list):
                    parsed[k] = []
            return parsed
        except Exception as parse_err:
            logger.warning(
                "Strategy sub-extractor '{}' JSON parse failed for workspace {}: {}",
                sub_name,
                workspace_id[:12] if workspace_id else "",
                parse_err,
            )
            return scaffold

    async def _extract_parties_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor A: parties + designations (people and organisations)."""
        prompt = (
            extract_parties_subagent_prompt(combined_document_content=combined_document_content)
        )
        # SPEAKER_LABEL_GUARDRAIL was imported here but never used, so the
        # extractors ran without it and returned diarization placeholders
        # ("Caller", "Receiver") as real entities — which the EXTRACTED
        # FACTS block then authorised for every downstream prompt.
        prompt = prompt + "\n\n" + SPEAKER_LABEL_GUARDRAIL
        return await self._run_extraction_subagent(
            workspace_id, prompt, "parties", ["parties"]
        )

    async def _extract_timeline_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor B: dated events + suit/case/FIR numbers."""
        prompt = (
            extract_timeline_subagent_prompt(combined_document_content=combined_document_content)
        )
        # SPEAKER_LABEL_GUARDRAIL was imported here but never used, so the
        # extractors ran without it and returned diarization placeholders
        # ("Caller", "Receiver") as real entities — which the EXTRACTED
        # FACTS block then authorised for every downstream prompt.
        prompt = prompt + "\n\n" + SPEAKER_LABEL_GUARDRAIL
        return await self._run_extraction_subagent(
            workspace_id, prompt, "timeline", ["dates", "suit_numbers"]
        )

    async def _extract_assets_and_law_subagent(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """Sub-extractor C: properties + amounts + statutes + annexures."""
        prompt = (
            extract_assets_and_law_subagent_prompt(combined_document_content=combined_document_content)
        )
        # SPEAKER_LABEL_GUARDRAIL was imported here but never used, so the
        # extractors ran without it and returned diarization placeholders
        # ("Caller", "Receiver") as real entities — which the EXTRACTED
        # FACTS block then authorised for every downstream prompt.
        prompt = prompt + "\n\n" + SPEAKER_LABEL_GUARDRAIL
        return await self._run_extraction_subagent(
            workspace_id,
            prompt,
            "assets_and_law",
            ["properties", "amounts", "statutes", "annexures"],
        )

    def _merge_case_entities(
        self,
        parties_result: Dict[str, Any],
        timeline_result: Dict[str, Any],
        assets_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine three sub-extractor results into the unified case_entities dict."""
        merged: Dict[str, Any] = {k: [] for k in self._EMPTY_CASE_ENTITIES.keys()}
        if isinstance(parties_result, dict):
            merged["parties"] = list(parties_result.get("parties") or [])
        if isinstance(timeline_result, dict):
            merged["dates"] = list(timeline_result.get("dates") or [])
            merged["suit_numbers"] = list(timeline_result.get("suit_numbers") or [])
        if isinstance(assets_result, dict):
            merged["properties"] = list(assets_result.get("properties") or [])
            merged["amounts"] = list(assets_result.get("amounts") or [])
            merged["statutes"] = list(assets_result.get("statutes") or [])
            merged["annexures"] = list(assets_result.get("annexures") or [])
        return merged

    async def _extract_case_entities(
        self,
        workspace_id: str,
        combined_document_content: str,
    ) -> Dict[str, Any]:
        """
        Pass 1 of the strategy pipeline — "deep agents in spirit".

        Runs three focused sub-extractors in parallel via ``asyncio.gather`` so
        each entity family gets the full 12000-token budget for its slice:

        - parties (people + organisations + designations)
        - timeline (dated events + suit/case/FIR numbers)
        - assets_and_law (properties + amounts + statutes + annexures)

        Combines into the unified ``case_entities`` dict that the synthesis
        pass anchors to. Any sub-extractor that fails contributes empty arrays
        so a single sub-failure never blocks the whole strategy generation.
        """
        if not combined_document_content or not combined_document_content.strip():
            return dict(self._EMPTY_CASE_ENTITIES)
        if combined_document_content.strip() == "No document content available.":
            return dict(self._EMPTY_CASE_ENTITIES)

        # Content-hash short-circuit. Strategy and analysis_report both call this
        # helper inside the same generation pipeline; without the cache we would
        # pay for the 3-parallel-sub-extractor pass twice for the same content.
        import hashlib as _hashlib
        try:
            _content_hash = _hashlib.sha1(
                combined_document_content[:80000].encode("utf-8", errors="ignore")
            ).hexdigest()
        except Exception:
            _content_hash = ""
        if _content_hash and workspace_id:
            _cached = self._workspace_case_entities_cache.get(workspace_id)
            if _cached and _cached[0] == _content_hash:
                logger.info(
                    "case_entities cache HIT for workspace {} (skipping 3 sub-extractors)",
                    workspace_id[:12],
                )
                return _cached[1]

        try:
            parties_result, timeline_result, assets_result = await asyncio.gather(
                self._extract_parties_subagent(
                    workspace_id, combined_document_content
                ),
                self._extract_timeline_subagent(
                    workspace_id, combined_document_content
                ),
                self._extract_assets_and_law_subagent(
                    workspace_id, combined_document_content
                ),
            )
        except Exception as gather_err:
            logger.warning(
                "Strategy extraction (parallel sub-agents) failed for workspace {}: {}",
                workspace_id[:12] if workspace_id else "",
                gather_err,
            )
            return dict(self._EMPTY_CASE_ENTITIES)

        merged = self._merge_case_entities(
            parties_result, timeline_result, assets_result
        )
        # Deterministic backstop for the party exclusions the prompt asks for.
        # These entities become the "EXTRACTED FACTS" block, which downstream
        # prompts describe as "the ONLY organisations, authorities, bodies,
        # parties and places you may name" — so anything that leaks here is not
        # merely tolerated downstream, it is authorised and then propagated into
        # the case summary, the objective and the analysis report as fact.
        # Prompt compliance alone is not enough for that.
        merged["parties"] = self._drop_non_party_entities(merged.get("parties"))
        # Per-sub-agent diagnostic logs so future thin-summary failures can be
        # traced to whichever sub-agent came back empty.
        logger.info(
            "Strategy sub-extractor PARTIES: {} party/entity record(s) for workspace {}",
            len(merged.get("parties") or []),
            workspace_id[:12] if workspace_id else "",
        )
        logger.info(
            "Strategy sub-extractor TIMELINE: {} dated event(s), {} suit/case number(s) for workspace {}",
            len(merged.get("dates") or []),
            len(merged.get("suit_numbers") or []),
            workspace_id[:12] if workspace_id else "",
        )
        logger.info(
            "Strategy sub-extractor ASSETS+LAW: {} property/ies, {} amount(s), {} statute(s), {} annexure(s) for workspace {}",
            len(merged.get("properties") or []),
            len(merged.get("amounts") or []),
            len(merged.get("statutes") or []),
            len(merged.get("annexures") or []),
            workspace_id[:12] if workspace_id else "",
        )
        # Populate cache so a sibling caller (e.g. analysis_report after
        # strategy) sees the same entities without re-running extraction.
        if _content_hash and workspace_id:
            self._workspace_case_entities_cache[workspace_id] = (_content_hash, merged)
        return merged


    # Diarization placeholders emitted by the audio transcription path. They are
    # speaker slots, never names — see SPEAKER_LABEL_GUARDRAIL.
    _SPEAKER_PLACEHOLDER_RE = re.compile(
        r"^(?:caller|receiver|speaker\s*[a-z0-9]?|participant\s*\d*|"
        r"person\s*[a-z0-9]?|unknown(?:\s+speaker)?|agent|customer)$",
        re.IGNORECASE,
    )
    # Version-control roles from an uploaded policy/manual's own front or back
    # matter ("Document Control", "Summary of Changes", "Approvals"). The person
    # authored or signed off the DOCUMENT; they are not in the dispute.
    _DOC_CONTROL_ROLE_RE = re.compile(
        r"\b(?:author|owner|approved\s*by|approver|reviewed\s*(?:and\s*approved)?"
        r"|reviewer|version\s*(?:number|date)|document\s*control|"
        r"summary\s*of\s*changes|initial\s*draft|new\s*logo)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _drop_non_party_entities(cls, parties: Any) -> List[Dict[str, Any]]:
        """Remove extracted "parties" that are not parties to the matter.

        Two families, both observed in production:

        1. Diarization placeholders ("Caller", "Receiver", "Speaker A") from an
           audio annexure, which then appeared in the case summary as though
           they were named individuals.
        2. Document-control metadata from a boilerplate annexure. An uploaded
           Acceptable Use Policy carries an "Approvals" table; its Author,
           Reviewer and "Approved By" names were extracted as case
           participants and reported as "key individuals involved in the
           communications and operations".

        Returns the filtered list; logs what it dropped so the cause stays
        visible rather than silently corrected.
        """
        if not isinstance(parties, list):
            return []
        kept: List[Dict[str, Any]] = []
        dropped: List[str] = []
        for entry in parties:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if cls._SPEAKER_PLACEHOLDER_RE.match(name):
                dropped.append(f"{name} (speaker placeholder)")
                continue
            context = " ".join(
                str(entry.get(k) or "")
                for k in ("designation", "role_in_matter")
            )
            if cls._DOC_CONTROL_ROLE_RE.search(context):
                dropped.append(f"{name} (document-control metadata)")
                continue
            kept.append(entry)
        if dropped:
            logger.info(
                "Dropped {} non-party entity/ies from extraction: {}",
                len(dropped),
                "; ".join(dropped[:8]),
            )
        return kept

    @staticmethod
    def _verify_entities_in_summary(
        summary_text: str,
        entities: Dict[str, Any],
    ) -> Dict[str, List[str]]:
        """
        Programmatic coverage check after synthesis. Returns a dict with three
        buckets of missing items so the caller can issue one consolidated
        retry prompt:

        - ``missing_parties``: party names whose full form OR primary surname
          token does not appear in ``summary_text``.
        - ``missing_suit_numbers``: every ``suit_numbers[i]["number"]`` that
          does not appear verbatim in ``summary_text``.
        - ``missing_key_dates``: up to 8 of the most legally material dates
          (suit filings, registered deeds, notices, court orders) that do not
          appear in either ISO or DD/MM/YYYY form in ``summary_text``.
        """
        result: Dict[str, List[str]] = {
            "missing_parties": [],
            "missing_suit_numbers": [],
            "missing_key_dates": [],
        }
        if not isinstance(summary_text, str) or not summary_text:
            return result
        if not isinstance(entities, dict):
            return result

        # Parties: full name OR primary surname token.
        for party in entities.get("parties", []) or []:
            if not isinstance(party, dict):
                continue
            name = (party.get("name") or "").strip()
            if not name:
                continue
            primary_token = name.split()[-1] if " " in name else name
            if name not in summary_text and primary_token not in summary_text:
                result["missing_parties"].append(name)

        # Suit numbers: verbatim.
        for suit in entities.get("suit_numbers", []) or []:
            if not isinstance(suit, dict):
                continue
            number = (suit.get("number") or "").strip()
            if number and number not in summary_text:
                result["missing_suit_numbers"].append(number)

        # Key dates: pick the top-N most legally material entries.
        _KEY_DATE_TOKENS = (
            "suit", "civil suit", "fir", "complaint", "registered", "sale deed",
            "notice", "legal notice", "order", "judgment", "decree", "writ",
            "filed", "execution", "agreement", "registration",
        )
        key_date_candidates: List[Dict[str, Any]] = []
        for entry in entities.get("dates", []) or []:
            if not isinstance(entry, dict):
                continue
            date_iso = (entry.get("date_iso") or "").strip()
            event = (entry.get("event") or "").strip().lower()
            if not date_iso or not event:
                continue
            if any(tok in event for tok in _KEY_DATE_TOKENS):
                key_date_candidates.append(entry)
        for entry in key_date_candidates[:8]:
            date_iso = (entry.get("date_iso") or "").strip()
            if not date_iso:
                continue
            # Build DD/MM/YYYY alternative for matching.
            dmy = ""
            try:
                y, m, d = date_iso.split("-")
                if len(y) == 4 and len(m) == 2 and len(d) == 2:
                    dmy = f"{d}/{m}/{y}"
            except Exception:
                dmy = ""
            if date_iso not in summary_text and (
                not dmy or dmy not in summary_text
            ):
                result["missing_key_dates"].append(dmy or date_iso)

        return result
