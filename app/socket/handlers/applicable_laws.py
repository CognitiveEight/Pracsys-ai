"""Applicable laws (acts/sections) generation and feedback.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from app.utils.redact import chars

from app.ai.usage import current_usage, meter_usage
from app.search.vertex_ai_search import search_indiankanoon as vertex_search_indiankanoon
from app.search.case_law_finder import (
    scrape_indiankanoon,
)
from app.prompts.applicable_acts import (
    TARGETED_INDIAN_ACTS,
    format_targeted_acts_for_prompt,
)
from app.prompts.legal_intelligence_system import (
    get_structured_document_feature_system_instruction,
)
from app.workspace.custom_instructions import (build_custom_instruction_suffix)

from app.socket.event_utils import (
    scrub_app_brand,
)


from app.prompts.applicable_laws_prompts import (
    regenerate_applicable_laws_with_feedback_prompt,
    generate_applicable_laws_from_sources_prompt,
)


class ApplicableLawsMixin:
    """Applicable laws (acts/sections) generation and feedback."""

    @meter_usage
    async def _regenerate_applicable_laws_with_feedback(
        self,
        *,
        workspace_id: str,
        user_id: str,
        workspace_role: str,
        user_feedback: str,
        emit_result: bool = True,
        outcome: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Apply a surgical find-and-replace / correction to the EXISTING Applicable
        Laws entries (Mode 1) — a NAME, TERM, DATE, or YEAR change, applied ONLY
        inside each law's ``description``. Same laws, same count/order, and
        `id` / `act` / `section` / `relevance` preserved (statute years are never
        altered). Never regenerates laws from documents. Operates on the cached
        ``_workspace_applicable_laws_payload`` and re-emits featureType=applicable_laws.
        """
        try:
            from app.ai.gemini_client import gemini_client

            current = self._workspace_applicable_laws_payload.get(workspace_id)
            if not isinstance(current, dict):
                return False
            laws = current.get("laws")
            if not isinstance(laws, list) or not laws:
                return False
            instruction = (user_feedback or "").strip()
            if not instruction:
                return False

            role = (workspace_role or "ADJUDICATOR").strip().upper()
            if role not in ("ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"):
                role = "ADJUDICATOR"

            current_json = json.dumps(current, ensure_ascii=True)
            prompt = regenerate_applicable_laws_with_feedback_prompt(current_json=current_json, instruction=instruction)

            # Authoritative user corrections lead the prompt so they
            # outrank the document content they contradict.
            prompt = self._with_user_facts(workspace_id, prompt)
            custom_suffix = build_custom_instruction_suffix(workspace_id)
            if custom_suffix:
                prompt = prompt.rstrip() + "\n\n" + custom_suffix

            raw = await gemini_client.generate(
                prompt=prompt,
                system_instruction=get_structured_document_feature_system_instruction(
                    role
                ),
                temperature=0.1,
                max_tokens=12000,
                response_mime_type="application/json",
            )
            json_str = self._prepare_json_candidate((raw or "").strip(), prefer="object")
            if not json_str:
                return False
            edited = json.loads(json_str)
            edited_laws = edited.get("laws") if isinstance(edited, dict) else None
            if not isinstance(edited_laws, list) or len(edited_laws) != len(laws):
                logger.warning(
                    "Applicable laws edit rejected (count mismatch); keeping cache"
                )
                return False

            # Index-merge, the same contract as _reeval_list_of_dates_relevance:
            # take ONLY `description` from the model and reproduce every other
            # field from the cached row. Rebuilding from the model's output ran
            # it through _normalize_applicable_laws_payload, which reconstructs
            # each row as exactly {id, act, section, description, relevance} and
            # therefore DROPPED the `isMatched` flag written by
            # _tag_laws_against_current_state — which both lost the frontend's
            # matched/new tagging and made the no-op comparison below unequal,
            # so the guard never fired and every edit emitted.
            merged_laws: List[Dict[str, Any]] = []
            for idx, cached_law in enumerate(laws):
                if not isinstance(cached_law, dict):
                    return False
                row = dict(cached_law)
                candidate = edited_laws[idx]
                if isinstance(candidate, dict):
                    new_desc = str(candidate.get("description") or "").strip()
                    if new_desc:
                        row["description"] = new_desc
                merged_laws.append(row)

            # Run the normaliser purely as a validity check (it drops rows with a
            # blank description or an IPC / CrPC / IEA-1872 leak). It used to run
            # AFTER the count guard, so a row the model blanked or contaminated
            # vanished silently with the guard already satisfied. Reject the whole
            # edit on shrinkage instead of shipping a shorter panel.
            _validated = self._normalize_applicable_laws_payload(
                {"laws": [dict(r) for r in merged_laws]}
            )
            _validated_laws = (
                _validated.get("laws") if isinstance(_validated, dict) else None
            )
            if (
                not isinstance(_validated_laws, list)
                or len(_validated_laws) != len(merged_laws)
            ):
                logger.warning(
                    "Applicable laws edit rejected (normalisation dropped {} of {} "
                    "rows — blank description or IPC/CrPC leak); keeping cache",
                    len(merged_laws) - len(_validated_laws or []),
                    len(merged_laws),
                )
                return False

            # Overlay the normalised description/section/relevance back onto the
            # merged rows so cleanup applies without losing `isMatched` or any
            # other field the cached row carried.
            for row, clean in zip(merged_laws, _validated_laws):
                if isinstance(clean, dict):
                    row["description"] = clean.get("description", row.get("description"))
                    row["section"] = clean.get("section", row.get("section"))
                    row["relevance"] = clean.get("relevance", row.get("relevance"))

            edited = dict(current)
            edited["laws"] = merged_laws
            if json.dumps(edited, sort_keys=True, ensure_ascii=True) == json.dumps(
                current, sort_keys=True, ensure_ascii=True
            ):
                logger.info(
                    "Applicable laws edit produced no change — instruction={!r}",
                    chars(instruction),
                )
                if outcome is not None:
                    outcome["status"] = "unchanged"
                return False  # nothing changed

            self._workspace_applicable_laws_payload[workspace_id] = edited

            if emit_result and self._socket_client:
                await self._emit_document_feature(
                    feature_type="applicable_laws",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    payload=edited,
                    usage=current_usage(),
                )
                logger.success(
                    "Applicable laws name edit emitted workspace={}",
                    workspace_id[:12] if workspace_id else "",
                )
            if outcome is not None:
                outcome["status"] = "applied"
            return True
        except Exception as e:
            logger.warning("Applicable laws name edit failed: {}", e)
            return False

    def _normalize_applicable_laws_payload(self, raw: Any) -> Dict[str, Any]:
        """Normalize model/search output into strict applicable_laws payload schema."""
        laws_raw = []
        if isinstance(raw, dict):
            laws_raw = raw.get("laws") or []
        elif isinstance(raw, list):
            laws_raw = raw
        if not isinstance(laws_raw, list):
            laws_raw = []

        def normalize_section_label(value: str) -> str:
            """Normalize noisy section labels and return raw section token."""
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                return ""

            # Remove any repeated/single "Section"/"Sec" prefix variants.
            core = re.sub(
                r"^(?:(?:section|sec)\s*[:.-]?\s*)+",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip(" :.-")
            return core or text

        normalized: List[Dict[str, str]] = []
        relevance_allowed = {"high": "High", "medium": "Medium", "low": "Low"}
        for idx, item in enumerate(laws_raw, start=1):
            if not isinstance(item, dict):
                continue
            act = str(item.get("act") or "").strip()
            section = normalize_section_label(item.get("section"))
            description = str(item.get("description") or "").strip()
            # Guardrail: user explicitly asked to not show IPC / CrPC / Indian
            # Evidence Act 1872 items. Map those to BNS 2023 / BNSS 2023 / BSA
            # 2023 in upstream prompt; here we just drop any leakage.
            act_l = act.lower()
            desc_l = description.lower()
            if (
                re.search(r"\bindian\s*penal\s*code\b", act_l)
                or re.search(r"\bipc\b", act_l)
                or re.search(r"\bipc\b", desc_l)
            ):
                continue
            if (
                re.search(r"\bcode\s*of\s*criminal\s*procedure\b", act_l)
                or re.search(r"\bcr\.?\s*p\.?\s*c\.?\b", act_l)
                or re.search(r"\bcr\.?\s*p\.?\s*c\.?\b", desc_l)
            ):
                continue
            if re.search(r"\bindian\s*evidence\s*act\b", act_l) and re.search(
                r"\b1872\b", act_l
            ):
                continue
            relevance_in = str(item.get("relevance") or "").strip().lower()
            relevance = relevance_allowed.get(relevance_in, "Medium")
            if not act or not section or not description:
                continue
            normalized.append(
                {
                    "id": f"law_{idx}",
                    "act": act,
                    "section": section,
                    "description": description,
                    "relevance": relevance,
                }
            )
            if len(normalized) >= 12:
                break
        # Final safety net: never surface the application's own brand/platform
        # name in any law row (central chokepoint for the applicable_laws path).
        return scrub_app_brand({"laws": normalized})

    @staticmethod
    def _link_laws_to_sources(
        payload: Dict[str, Any], search_results: List[Dict[str, str]]
    ) -> None:
        """Give each law row its Indian Kanoon link and a register status.

        The model reads numbered search results but returns rows without
        their URLs, so the link was lost. A row is linked to a result whose
        title names the same section number and shares a word of the act
        name — a deterministic match, never a guess. ``registerStatus`` comes
        from the statutory register (see app/statutes/register.py): verified,
        invalid (section beyond the Act's range), or unverified.
        """
        from app.statutes.register import annotate_law_rows, section_number

        for row in (payload or {}).get("laws") or []:
            if not isinstance(row, dict):
                continue
            act = str(row.get("act") or "")
            num = section_number(str(row.get("section") or ""))
            act_words = {w for w in re.findall(r"[a-z]{4,}", act.lower())} - {"code", "act", "section"}
            for res in search_results or []:
                title = str(res.get("title") or "")
                url = str(res.get("url") or "")
                if not url or not num:
                    continue
                if re.search(rf"\b{re.escape(num)}\b", title, re.I) and (
                    not act_words or any(w in title.lower() for w in act_words)
                ):
                    row["sourceUrl"] = url
                    row["sourceTitle"] = title
                    break
        annotate_law_rows(payload)

    def _heuristic_applicable_laws_fallback(
        self, evidence_snippet: str
    ) -> Dict[str, Any]:
        """Best-effort law mapping from keywords when model JSON fails or returns empty.

        Designed to produce a BALANCED list across Indian civil law and criminal law:
        criminal-side keywords route to BNS / BNSS / BSA / IT Act / NI Act 138 etc.,
        while civil-side keywords route to Contract Act, Specific Relief Act, TPA,
        Consumer Protection Act, Companies Act, Limitation Act, etc.
        """
        txt = (evidence_snippet or "").lower()
        candidates: List[Dict[str, str]] = []

        def add(
            act: str, section: str, description: str, relevance: str = "Medium"
        ) -> None:
            candidates.append(
                {
                    "id": "",
                    "act": act,
                    "section": section,
                    "description": description,
                    "relevance": relevance,
                }
            )

        # ----------------------------- CRIMINAL LAW -----------------------------
        if re.search(
            r"\b(hack|hacked|phish|otp|password|account|cyber|digital|upi|wallet)\b",
            txt,
        ):
            add(
                "Information Technology Act, 2000",
                "Section 66C",
                "Potential identity / credential misuse through digital systems.",
                "High",
            )
            add(
                "Information Technology Act, 2000",
                "Section 66D",
                "Potential cheating by personation using computer resources.",
                "High",
            )
        if re.search(r"\b(theft|stolen|stole|robbery|burglar|dacoity|loot)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 303",
                "Potential theft of movable property based on allegations.",
                "High",
            )
        if re.search(
            r"\b(cheat|fraud|defraud|forgery|forged|fake document|fabricat)\b", txt
        ):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 318",
                "Potential cheating / dishonest inducement of property.",
                "High",
            )
        if re.search(r"\b(threat|intimidat|blackmail|extort)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 351",
                "Potential criminal intimidation-related conduct.",
                "Medium",
            )
        if re.search(r"\b(assault|hurt|injur|beat|attack|grievous)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 115",
                "Potential voluntarily causing hurt allegations.",
                "Medium",
            )
        if re.search(r"\b(criminal breach of trust|misappropriat|embezzl)\b", txt):
            add(
                "Bharatiya Nyaya Sanhita, 2023",
                "Section 316",
                "Potential criminal breach of trust over entrusted property.",
                "High",
            )
        if re.search(
            r"\b(fir|f\.i\.r\.|police complaint|investigation|charge ?sheet|"
            r"chargesheet|magistrate|bail)\b",
            txt,
        ):
            add(
                "Bharatiya Nagarik Suraksha Sanhita, 2023",
                "Section 173",
                "Registration / handling of information of cognizable offence.",
                "Medium",
            )
        if re.search(r"\b(cheque|cheque bounce|dishonour|dishonor|bounced)\b", txt):
            add(
                "Negotiable Instruments Act, 1881",
                "Section 138",
                "Dishonour of cheque for insufficiency of funds.",
                "High",
            )

        # ------------------------------- CIVIL LAW ------------------------------
        if re.search(
            r"\b(contract|agreement|mou|breach|consideration|offer|acceptance)\b",
            txt,
        ):
            add(
                "Indian Contract Act, 1872",
                "Section 73",
                "Compensation for loss caused by breach of contract.",
                "High",
            )
        if re.search(
            r"\b(specific performance|injunction|declaration|relief|recover possession)\b",
            txt,
        ):
            add(
                "Specific Relief Act, 1963",
                "Section 10",
                "Possible specific performance of a contract is enforceable.",
                "Medium",
            )
            add(
                "Specific Relief Act, 1963",
                "Section 38",
                "Perpetual injunction may be sought to prevent breach of obligation.",
                "Medium",
            )
        if re.search(
            r"\b(sale deed|mortgage|lease|tenant|landlord|gift deed|"
            r"transfer of property|easement)\b",
            txt,
        ):
            add(
                "Transfer of Property Act, 1882",
                "Section 54",
                "Sale-of-immovable-property obligations may govern the transaction.",
                "Medium",
            )
        if re.search(r"\b(suit|plaint|written statement|decree|civil court)\b", txt):
            add(
                "Code of Civil Procedure, 1908",
                "Order VII Rule 1",
                "Framing / maintainability of the plaint.",
                "Medium",
            )
        if re.search(r"\b(time ?bar|barred by limitation|delay|condonation)\b", txt):
            add(
                "Limitation Act, 1963",
                "Section 3",
                "Limitation bar must be tested before the cause of action proceeds.",
                "Medium",
            )
        if re.search(
            r"\b(consumer|deficiency in service|unfair trade|product defect|service provider)\b",
            txt,
        ):
            add(
                "Consumer Protection Act, 2019",
                "Section 35",
                "Consumer complaint for deficiency in service / unfair trade practice.",
                "Medium",
            )
        if re.search(r"\b(company|director|shareholder|board|mismanagement|nclt)\b", txt):
            add(
                "Companies Act, 2013",
                "Section 241",
                "Oppression and mismanagement concerns requiring review.",
                "Medium",
            )
        if re.search(
            r"\b(arbitration|arbitral|arbitrator|seat|venue|award)\b", txt
        ):
            add(
                "Arbitration and Conciliation Act, 1996",
                "Section 11",
                "Appointment of arbitrator if the parties cannot agree.",
                "Medium",
            )
        if re.search(r"\b(gst|invoice|input tax|itc|tax credit)\b", txt):
            add(
                "Central Goods and Services Tax Act, 2017",
                "Section 16",
                "Input tax credit eligibility may be relevant.",
                "Medium",
            )
        if re.search(
            r"\b(divorce|matrimonial|maintenance|guardianship|custody|alimony|"
            r"hindu marriage)\b",
            txt,
        ):
            add(
                "Hindu Marriage Act, 1955",
                "Section 13",
                "Grounds for divorce may be invoked.",
                "Medium",
            )

        return self._normalize_applicable_laws_payload({"laws": candidates})

    async def _search_indiankanoon(self, query: str) -> List[Dict[str, str]]:
        """Search Indian Kanoon directly and return top lightweight results.

        Delegates to the shared ``scrape_indiankanoon`` helper so the
        applicable-laws path and the case-laws path use identical scraping
        behaviour (single source of truth).
        """
        results = await scrape_indiankanoon(query)
        if results:
            preview = [
                {"title": r.get("title", "")[:120], "url": r.get("url", "")}
                for r in results[:5]
            ]
            logger.info("Applicable laws: Indian Kanoon results preview={}", preview)
        else:
            logger.info("Applicable laws: Indian Kanoon returned no results")
        return results

    async def _generate_applicable_laws_from_sources(
        self,
        workspace_id: str,
        workspace_role: str,
        signals: Dict[str, Any],
        search_results: List[Dict[str, str]],
        objective_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate final laws list JSON from case signals + search context.

        When ``objective_context`` (the current strategic objective) is provided,
        the law selection is steered toward that objective/perspective so a
        perspective change re-derives the relevant statutes.
        """
        from app.ai.gemini_client import gemini_client

        sources_text = []
        for idx, row in enumerate(search_results[:8], start=1):
            sources_text.append(
                f"{idx}. title={row.get('title','')} | url={row.get('url','')} | snippet={row.get('snippet','')}"
            )
        source_blob = (
            "\n".join(sources_text)
            if sources_text
            else "No external case snippets available."
        )
        targeted_acts_block = format_targeted_acts_for_prompt()
        targeted_acts_count = len(TARGETED_INDIAN_ACTS)
        objective_block = (
            (
                "CURRENT STRATEGIC OBJECTIVE / CASE PERSPECTIVE — this is the PRIMARY lens for "
                "selecting laws. Select ONLY statutes that ADVANCE this objective. Treat the case "
                "facts/evidence as support for the objective — do NOT list statutes that merely "
                "address the underlying factual incident (e.g. a theft, an accident) UNLESS they "
                "directly serve the objective. When the objective is matrimonial (divorce, "
                "judicial separation, custody, maintenance, restitution), prioritise "
                "matrimonial/family law (e.g. Hindu Marriage Act, 1955; Guardians and Wards Act; "
                "maintenance provisions) and DROP unrelated offence statutes. If the objective "
                "shifts perspective, the law list must shift with it — drop laws that only fit the "
                "earlier perspective:\n" + objective_context.strip()
            )
            if objective_context and objective_context.strip()
            else ""
        )
        prompt = generate_applicable_laws_from_sources_prompt(objective_block=objective_block, self=self, signals=signals, source_blob=source_blob, targeted_acts_block=targeted_acts_block, targeted_acts_count=targeted_acts_count, workspace_id=workspace_id, workspace_role=workspace_role)
        # Authoritative user corrections lead the prompt so they outrank
        # the document content they contradict.
        prompt = self._with_user_facts(workspace_id, prompt)
        raw = await gemini_client.generate(
            prompt=prompt,
            system_instruction=get_structured_document_feature_system_instruction(
                workspace_role
            ),
            temperature=0.2,
            max_tokens=12000,
            response_mime_type="application/json",
        )
        logger.info("Applicable laws: raw LLM response preview={}", chars(raw))
        text = self._prepare_json_candidate(raw or "", prefer="object") or ""
        parsed: Dict[str, Any] = {"laws": []}
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                arr_candidate = self._prepare_json_candidate(raw or "", prefer="array")
                parsed_arr = json.loads(arr_candidate) if arr_candidate else []
                parsed = {"laws": parsed_arr} if isinstance(parsed_arr, list) else {"laws": []}
                if parsed.get("laws"):
                    logger.info("Applicable laws: salvaged laws array from malformed JSON")
            except Exception:
                logger.warning(
                    "Applicable laws: failed to parse Gemini JSON; using heuristic fallback"
                )
                parsed = {"laws": []}

        normalized = self._normalize_applicable_laws_payload(parsed)
        self._link_laws_to_sources(normalized, search_results)
        _statuses = [str(r.get("registerStatus")) for r in normalized.get("laws", []) if isinstance(r, dict)]
        logger.info(
            "Applicable laws: {} row(s) from the model, register status {}",
            len(normalized.get("laws", [])),
            {s: _statuses.count(s) for s in set(_statuses)},
        )
        if not normalized.get("laws"):
            heuristic = self._heuristic_applicable_laws_fallback(
                signals.get("evidenceSnippet", "")
            )
            if heuristic.get("laws"):
                logger.info(
                    "Applicable laws: heuristic fallback generated {} law(s)",
                    len(heuristic.get("laws", [])),
                )
                logger.info(
                    "Applicable laws: final payload from heuristic={}", heuristic
                )
                return heuristic
        return normalized

    def _tag_laws_against_current_state(
        self, workspace_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Tag each applicable-law with ``isMatched`` against the frontend's
        currentState.applicableLaws (matched by act + section). currentState
        laws have no id, so the law keeps its ``law_N`` id and only gains the
        flag. Deterministic — no LLM call needed.
        """
        if not isinstance(payload, dict):
            return payload
        current_state = self._workspace_current_state_payload.get(workspace_id) or {}
        cs_laws = (
            current_state.get("applicableLaws")
            if isinstance(current_state, dict)
            else None
        )
        prev_sigs: Set[str] = set()
        for it in cs_laws or []:
            if isinstance(it, dict):
                sig = self._law_signature(it)
                if sig and sig != "|":
                    prev_sigs.add(sig)
        for law in payload.get("laws") or []:
            if isinstance(law, dict):
                law["isMatched"] = self._law_signature(law) in prev_sigs
        return payload

    @meter_usage
    async def _generate_and_emit_applicable_laws(
        self,
        workspace_id: str,
        user_id: str,
        workspace_role: Optional[str] = None,
        *,
        objective_context: Optional[str] = None,
        merge_with_previous: bool = True,
    ) -> None:
        """Generate and emit document_feature_response with featureType applicable_laws.

        ``objective_context`` (the current strategic objective) steers law
        selection and the Indian Kanoon search query so a case-perspective change
        (e.g. divorce -> judicial separation) re-derives the relevant statutes.
        ``merge_with_previous=False`` REPLACES the cached laws instead of unioning
        them — used on an objective change so stale laws from the old perspective
        are dropped.
        """
        role = (workspace_role or "ADJUDICATOR").strip().upper()
        logger.info(
            "Applicable laws: generation started for workspace {}",
            workspace_id[:12] if workspace_id else "",
        )
        signals = await self._extract_case_signals_from_documents(workspace_id, user_id)
        graph_block = await self._graph_evidence_block("applicable_laws", workspace_id)
        if graph_block:
            signals["evidenceSnippet"] = (signals.get("evidenceSnippet") or "") + "\n\n" + graph_block
        # Bias the search query toward the new objective when provided, so a
        # perspective change actually changes which case-law is fetched.
        search_query = signals.get("query", "")
        if objective_context:
            search_query = (f"{objective_context} {search_query}").strip()[:400]
        search_results = await self._search_indiankanoon(search_query)
        if not search_results:
            search_results = await vertex_search_indiankanoon(search_query)
        logger.info(
            "Applicable laws: gathered {} search result(s) for workspace {}",
            len(search_results),
            workspace_id[:12] if workspace_id else "",
        )

        new_payload = await self._generate_applicable_laws_from_sources(
            workspace_id=workspace_id,
            workspace_role=role,
            signals=signals,
            search_results=search_results,
            objective_context=objective_context,
        )
        if merge_with_previous:
            # Union with the previously cached payload and dedupe so a regeneration
            # after a transcript edit doesn't drop laws the user has already seen
            # while still folding in any genuinely new entries.
            payload = await self._merge_applicable_laws_with_previous(
                workspace_id, new_payload
            )
        else:
            # Objective change: replace stale laws with the freshly-derived set.
            payload = new_payload
        # Tag each law matched vs new against the frontend's currentState
        # (match by act + section). Cache the tagged payload so reconnect
        # re-emits the same flags.
        payload = self._tag_laws_against_current_state(workspace_id, payload)
        self._workspace_applicable_laws_payload[workspace_id] = payload
        if not self._socket_client:
            return
        await self._emit_document_feature(
            feature_type="applicable_laws",
            workspace_id=workspace_id,
            user_id=user_id,
            payload=payload,
            usage=current_usage(),
        )
        logger.success(
            "Applicable laws sent to backend ({} laws)",
            len(payload.get("laws", [])),
        )
        await asyncio.sleep(0.1)
