"""
LangGraph agent for temporal inference in list-of-dates generation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger

from app.prompts.legal_intelligence_system import ENGLISH_ONLY_OUTPUT_DIRECTIVE


def snap_hallucinated_year(
    *,
    date_iso: str,
    document_text: str,
    today_iso: str,
    anchor_year: Optional[int] = None,
    slack_years: int = 3,
    future_tolerance_days: int = 365,
    log_prefix: str = "Date guard",
) -> str:
    """Correct a hallucinated year on a single ``YYYY-MM-DD`` date string.

    Used as a post-LLM defense for list-of-dates / procedural-timeline
    extractors. Gemini sometimes ignores the prompt's "use current year" rule
    and stamps recent partial dates ("2 February", "21 तारीख") with a wildly
    wrong year (commonly 2000). This helper snaps such dates back to the
    current year when there's no anchor, the year is well in the past, and
    the suspicious 4-digit year does not appear anywhere in the source text.

    When the snap to the current year produces a date in the FUTURE, we keep
    it as long as it's within ``future_tolerance_days`` of today — these
    typically represent deadlines, upcoming hearings, or commitments like
    "I'll call you back tomorrow / within 30 days". Only past the tolerance
    do we fall back to the previous year.

    Returns the (possibly-corrected) date string. Returns input unchanged when:
      - input is malformed (not a YYYY-MM-DD shape)
      - an ``anchor_year`` was provided (anchor wins over the snap)
      - the year is within ``slack_years`` of today
      - the suspicious 4-digit year is literally present in ``document_text``
    """
    if not isinstance(date_iso, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        return date_iso
    try:
        year = int(date_iso[:4])
    except ValueError:
        return date_iso
    try:
        current_year_int = int(today_iso[:4])
    except (TypeError, ValueError):
        current_year_int = datetime.utcnow().year

    if anchor_year is not None:
        return date_iso
    if year >= current_year_int - slack_years:
        return date_iso

    # Mask out currency / numeric quantities before checking whether the
    # 4-digit year appears as a genuine year mention in the source. Without
    # this, a transcript that mentions "₹2000 salary" or "Rs. 2000" passes
    # the year-in-source test trivially, the snap doesn't fire, and the LLM's
    # hallucinated year leaks through. Strip the most common Indian currency
    # forms first; numeric prefixes after symbol/word currency markers are
    # treated as money, not year references.
    masked_text = document_text or ""
    masked_text = re.sub(
        r"(?:₹|Rs\.?|INR|\$|€|£)\s*[\d,]+(?:\.\d+)?",
        " ",
        masked_text,
        flags=re.IGNORECASE,
    )
    # Also strip patterns like "2000 rupees" / "2000 rs" / "2000/-".
    masked_text = re.sub(
        r"\b\d[\d,]*\s*(?:rupees?|rs\.?|/-?)\b",
        " ",
        masked_text,
        flags=re.IGNORECASE,
    )
    if re.search(rf"\b{year}\b", masked_text):
        return date_iso

    snapped_year = current_year_int
    try:
        candidate = datetime.strptime(
            f"{snapped_year}-{date_iso[5:]}", "%Y-%m-%d"
        )
        today_dt = datetime.strptime(today_iso, "%Y-%m-%d")
        if candidate > today_dt:
            future_delta_days = (candidate - today_dt).days
            # Allow near-future deadlines (tomorrow, next week, next quarter,
            # next hearing) — these are valid and often what the LLM was
            # actually computing. Only step back a year when the date is
            # implausibly far in the future for an "already happened" event.
            if future_delta_days > future_tolerance_days:
                snapped_year -= 1
    except ValueError:
        pass

    snapped = f"{snapped_year}-{date_iso[5:]}"
    logger.info(
        "{}: snapping hallucinated year {} → {} for date {} "
        "(no anchor, year not in source text)",
        log_prefix, year, snapped_year, date_iso,
    )
    return snapped


class TemporalInferenceState(TypedDict, total=False):
    document_text: str
    source_doc_id: str
    source_doc_name: str
    anchor_year: Optional[int]
    today_str: Optional[str]  # YYYY-MM-DD; pinned to call time, used for year resolution
    raw_response: str
    events: List[Dict[str, Any]]


class ListOfDatesTemporalAgent:
    """LLM-first temporal inference agent for relative date statements."""

    def __init__(self) -> None:
        self._workflow = self._build_graph()

    def _build_graph(self):
        g = StateGraph(TemporalInferenceState)
        g.add_node("infer_temporal_events", self._node_infer_temporal_events)
        g.add_node("validate_and_normalize", self._node_validate_and_normalize)
        g.add_edge(START, "infer_temporal_events")
        g.add_edge("infer_temporal_events", "validate_and_normalize")
        g.add_edge("validate_and_normalize", END)
        return g.compile()

    async def _extract_doc_entities(
        self, document_text: str
    ) -> Dict[str, Any]:
        """
        Pre-pass for list_of_dates: extract the named entities present in this
        single document so the date-generation prompt can anchor each entry's
        description to actors + counterparties + amounts + suits + identifiers
        as written, instead of producing generic "A document was executed"
        descriptions.

        Returns an empty dict on any failure; the caller falls back to the
        existing prompt-only behavior in that case.
        """
        empty: Dict[str, Any] = {
            "parties": [],
            "amounts": [],
            "suit_numbers": [],
            "properties": [],
            "statutes": [],
        }
        if not document_text or not document_text.strip():
            return empty

        from app.ai.gemini_client import gemini_client

        prompt = (
            "You are a focused entity extractor for a single legal document. "
            "Your ONLY job is to list every named PARTY, every MONETARY "
            "AMOUNT, every SUIT/CASE/FIR number, every PROPERTY identifier, "
            "and every STATUTE referenced in the document text below.\n\n"
            "Return ONE JSON object with this shape (no commentary, no markdown):\n\n"
            "{\n"
            '  "parties": [\n'
            '    { "name": "exact name as written (Smt./Shri/Dr./M/s preserved)", '
            '"designation": "role / community / status as written (e.g. plaintiff, defendant, vendor, vendee, advocate, SDM)" }\n'
            "  ],\n"
            '  "amounts": [\n'
            '    { "amount": "in source notation (e.g. Rs. 11,64,000)", '
            '"context": "advance / total consideration / balance / payment / fine / etc.", '
            '"from": "name as written or null", "to": "name as written or null" }\n'
            "  ],\n"
            '  "suit_numbers": [\n'
            '    { "number": "exact identifier (e.g. O.S. No. 827/2019, FIR No. 145/2024)", '
            '"court": "court / forum as written or null", '
            '"filed_by": "name as written or null", '
            '"against": ["names as written"] }\n'
            "  ],\n"
            '  "properties": [\n'
            '    { "identifier": "Arazi No. X / Khasra No. Y / Plot N / Survey No. N etc., exactly as written", '
            '"area": "area exactly as written or null", '
            '"location": "most specific location written or null" }\n'
            "  ],\n"
            '  "statutes": [\n'
            '    { "name": "exact statute name as written (e.g. Specific Relief Act, 1963; BNS, 2023)", '
            '"sections": ["section numbers as written"] }\n'
            "  ]\n"
            "}\n\n"
            "STRICT RULES:\n"
            "- Use names EXACTLY as written; preserve honorifics and "
            "punctuation (do NOT collapse 'Smt. Premlata' to 'Premlata').\n"
            "- Never write 'Party No. 1' / 'the plaintiff' placeholders. Drop "
            "any entry where you cannot find a real name in the document.\n"
            "- Preserve Indian numerical notation for amounts (Rs. 11,64,000 "
            "stays that way; do NOT convert to '11.64 lakh' or '1,164,000').\n"
            "- Use [] for any bucket with no matches.\n"
            "- Return ONLY valid JSON. No commentary, no markdown fences.\n\n"
            "DOCUMENT:\n"
            f"{document_text[:20000]}"
        )

        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.debug(
                "list_of_dates per-doc entity extraction failed (non-fatal): {}", e
            )
            return empty
        if not raw:
            return empty
        try:
            import re as _re
            import json as _json
            # Trim ```json ... ``` fences if the model added them.
            cleaned = raw.strip()
            cleaned = _re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = _re.sub(r"\s*```\s*$", "", cleaned)
            parsed = _json.loads(cleaned)
            if not isinstance(parsed, dict):
                return empty
            for k in empty.keys():
                if not isinstance(parsed.get(k), list):
                    parsed[k] = []
            return parsed
        except Exception as parse_err:
            logger.debug(
                "list_of_dates per-doc entity extraction JSON parse failed (non-fatal): {}",
                parse_err,
            )
            return empty

    async def _node_infer_temporal_events(
        self, state: TemporalInferenceState
    ) -> Dict[str, Any]:
        from app.ai.gemini_client import gemini_client

        document_text = (state.get("document_text") or "").strip()
        if not document_text:
            return {"raw_response": "[]"}

        gemini_client.initialize()
        if not gemini_client.model:
            return {"raw_response": "[]"}

        # "Deep agents" pre-pass: extract the named entities in this document
        # first, so the date-generation prompt below can anchor every entry's
        # description to actors + counterparties + amounts + suits + identifiers
        # as written. Best-effort: empty dict on failure, prompt falls back to
        # original behavior.
        doc_entities = await self._extract_doc_entities(document_text)
        try:
            import json as _json_dye
            doc_entities_block = _json_dye.dumps(
                doc_entities, indent=2, ensure_ascii=False
            )
        except Exception:
            doc_entities_block = "{}"
        _ents_party_count = len(doc_entities.get("parties") or [])
        _ents_amount_count = len(doc_entities.get("amounts") or [])
        _ents_suit_count = len(doc_entities.get("suit_numbers") or [])
        _ents_prop_count = len(doc_entities.get("properties") or [])
        logger.info(
            "🧠 list_of_dates pre-extraction for doc '{}': parties={} amounts={} suits={} properties={}",
            (state.get("source_doc_name") or state.get("source_doc_id") or "?")[:60],
            _ents_party_count,
            _ents_amount_count,
            _ents_suit_count,
            _ents_prop_count,
        )

        from datetime import timedelta as _timedelta
        anchor_year = state.get("anchor_year")
        anchor_txt = str(anchor_year) if isinstance(anchor_year, int) else "unknown"
        today_str = state.get("today_str") or datetime.utcnow().strftime("%Y-%m-%d")
        try:
            today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        except ValueError:
            today_dt = datetime.utcnow()
        tomorrow_str = (today_dt + _timedelta(days=1)).strftime("%Y-%m-%d")
        next_week_str = (today_dt + _timedelta(days=7)).strftime("%Y-%m-%d")
        current_year = today_str[:4]
        try:
            previous_year = str(int(current_year) - 1)
        except ValueError:
            previous_year = current_year

        prompt = f"""You are a legal timeline extractor. Extract only factual, case-relevant dates from the legal text below.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

EXTRACTED FACTS (these entities were already pulled from this document by a focused extraction step — treat them as authoritative ground truth. When you write each date entry's "description", you MUST use these exact names, amounts, suit numbers, and identifiers verbatim. Do NOT introduce "Party No. 1" / "the plaintiff" / "the petitioner" / "the respondent" placeholders. Every actor in a description must come from this list.):

{doc_entities_block}

**Today's date**: {today_str} (current year is {current_year}). Anchor every relative or partial date against this — these documents describe RECENT events.

INCLUDE only dates that represent actual case events, such as:
- Incident/accident dates
- Filing dates (complaints, petitions, motions)
- Court hearing or trial dates where a substantive order/action occurred
- Judgment or order dates
- Service of process / notice dates
- Deadlines set by court or statute
- Key meeting or negotiation dates directly tied to the case
- Dates of medical treatment, injuries, or damages (if case-related)
- Relative/implicit dates (e.g. "five years prior") ONLY if they describe a clear case fact

EXCLUDE the following — do NOT create an entry for them:
- Any date that is a placeholder, dummy, or system-generated (e.g. "1111", "9999", "0000", "11-11-1111")
- General legal citation years, statute enactment years, or publication years
- Background or biographical dates unrelated to the case
- Speculative or hypothetical dates
- Dates that appear ONLY in a court case listing/history table (rows of "Date of Listing | Date of Upload | Order") — from such tables extract ONLY the most significant dates: the case filing date, registration date, and the most recent substantive hearing date (max 3 entries per such document)
- Any date whose only evidence is website navigation text, footer text, or UI elements (e.g. "Feedback", "Copyright Policy", "View PDF", "Skip to main content", "Sitemap")
- Dates already captured by a more specific entry

Rules:
1) Use ONLY evidence from the provided text — do not invent any fact.
2) **Year resolution** — when the source mentions a date WITHOUT an explicit year
   (e.g. "2 February", "21 तारीख", "the 15th of March", "last Tuesday"):
     a. If an Anchor year hint is provided below (≠ "unknown"), use that year.
     b. Otherwise, default to the CURRENT YEAR ({current_year}). These documents
        describe RECENT events, NOT historical ones — never default to {previous_year[:2]}00,
        {previous_year[:2]}10, {previous_year[:2]}20 or similar legacy years.
     c. If "{current_year}-MM-DD" would be in the FUTURE relative to today
        ({today_str}), use {previous_year} instead — a reported event must have
        already happened.
     d. Only skip the entry if the source itself explicitly states a year that
        contradicts both options or is unreadable.
3) If the source uses a fully RELATIVE phrase ("three years ago", "1.5 years
   ago", "for many years", "since long back", "a long time back") and no
   concrete day/month is available, **DROP the entry**. Do NOT synthesize a
   placeholder like ``YYYY-01-01`` or ``YYYY-MM-01``. This panel must contain
   only entries with a concrete day/month/year derived from the source —
   fake-precision dates ("Jan 1 of three years ago") mislead the user.
3a) **Relative-time anchors** — phrases like "tomorrow" / "today" / "कल" / "आज"
    / "in 24 hours" / "within 24 hours" / "by 5 PM tomorrow" / "next week" /
    "next Monday" must anchor STRICTLY to today's date ({today_str}). Do NOT
    derive these from any other date mentioned in the source.
      • "tomorrow" / "कल" / "by 5 PM tomorrow" → {tomorrow_str}
      • "today" / "आज" / "in 24 hours" / "within 24 hours" → {today_str}
      • "next week" / "in a week" / "in 7 days" → {next_week_str}
      • "next Monday/Tuesday/..." → next occurrence of that weekday strictly
        AFTER today ({today_str}).
3b) **Currency / money is NOT a year**. Numbers like "₹2000", "Rs. 7000",
    "$5000" are AMOUNTS in the case (salary, fines, transaction values), NOT
    year references. Never use a currency amount as a year for any date.
3c) **Indian DD/MM/YYYY convention** — when the source contains a slash- or
    dash-separated numeric date like ``01/05/2026``, ``01-05-2026``, or
    ``01.05.2026``, the FIRST number is the DAY and the SECOND is the
    MONTH (Indian convention). Do NOT interpret it as US MM/DD/YYYY.
      • ``01/05/2026`` → DAY 1, MONTH 5 → ``2026-05-01`` (1 May 2026), NOT
        ``2026-01-05`` (5 January 2026).
      • ``13/04/2026`` → DAY 13, MONTH 4 → ``2026-04-13``.
      • Whenever the first number is > 12 that already proves DD/MM/YYYY
        (e.g. ``25/03/2026`` can only be 25 March). Apply the same rule
        even when both numbers are ≤ 12 — DAY first, MONTH second.
      • Only switch to MM/DD/YYYY if the source EXPLICITLY identifies the
        format (e.g. a label like "Date (MM/DD/YYYY): 01/05/2026") or the
        document is clearly from a US/foreign jurisdiction.
4) "description" must be 1-2 short factual sentences (target 25-50 words, max ~350 characters) describing what happened on that date. Name the ACTOR and the COUNTERPARTY using the names as written in the documents (full names + designations / roles when present). Include specific amounts (in the source notation, e.g. Rs. 11,64,000), document/property identifiers (e.g. Arazi No. 262, Khasra No. 285), and suit numbers (e.g. O.S. No. 827/2019) when they appear in the source. Explicitly say WHAT ACTION was taken (e.g. "executed a registered agreement to sell", "filed a Civil Suit", "issued a legal notice", "granted conditional permission"). Reference uploaded files only as Annexure N. Never paste raw document fragments — rewrite into clean prose.
5) "evidenceSnippet" must be a clean, readable excerpt from the source text — never paste a list of dates or PDF links.
6) Output ONLY a JSON array. No extra text.

[
  {{
    "date": "YYYY-MM-DD",
    "title": "short event title (5 words max)",
    "description": "1-2 short factual sentences, ~350 chars max, naming actor + counterparty + amounts + identifiers + suit numbers when present",
    "evidenceSnippet": "clean readable source excerpt proving this date (max 200 chars)",
    "pageNumber": integer (if inferable from page markers in text),
    "isIncident": true|false,
    "eventType": "Court Hearing|Deadline|Meeting|Filing|Other",
    "relevance": "High|Medium|Low"
  }}
]

Anchor year hint (if available): {anchor_txt}

Legal text:
{document_text[:20000]}
"""
        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=12000,
                response_mime_type="application/json",
            )
        except Exception as e:
            logger.warning("Temporal agent LLM call failed: {}", e)
            raw = "[]"
        return {"raw_response": (raw or "[]").strip()}

    async def _node_validate_and_normalize(
        self, state: TemporalInferenceState
    ) -> Dict[str, Any]:
        raw = (state.get("raw_response") or "").strip()
        source_doc_id = str(state.get("source_doc_id") or "").strip()
        source_doc_name = str(state.get("source_doc_name") or "").strip()

        if not raw:
            return {"events": []}

        if "```" in raw:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.I)
            if m:
                raw = (m.group(1) or "").strip()

        arr: Any = []
        try:
            parsed = json.loads(raw)
            arr = parsed if isinstance(parsed, list) else parsed.get("events", [])
        except Exception:
            arr = []

        if not isinstance(arr, list):
            arr = []

        def _extract_page_markers(text: str) -> List[tuple[int, int]]:
            markers: List[tuple[int, int]] = []
            if not text:
                return markers
            for m in re.finditer(r"---\s*Page\s+(\d+)\s*---", text, flags=re.I):
                try:
                    markers.append((m.start(), int(m.group(1))))
                except Exception:
                    continue
            for m in re.finditer(r"--\s*(\d+)\s+of\s+\d+\s*--", text, flags=re.I):
                try:
                    markers.append((m.start(), int(m.group(1))))
                except Exception:
                    continue
            markers.sort(key=lambda x: x[0])
            return markers

        def _resolve_page_from_text(item: Dict[str, Any], text: str) -> int:
            raw_page = item.get("pageNumber")
            if isinstance(raw_page, int) and raw_page > 0:
                return raw_page
            if isinstance(raw_page, str):
                p = raw_page.strip()
                if p.isdigit() and int(p) > 0:
                    return int(p)

            markers = _extract_page_markers(text)
            if not markers:
                return 1

            anchors = [
                str(item.get("evidenceSnippet") or "").strip(),
                str(item.get("date") or "").strip(),
                str(item.get("title") or "").strip(),
                str(item.get("description") or "").strip(),
            ]
            haystack = (text or "").lower()
            pos = -1
            for anchor in anchors:
                if len(anchor) < 4:
                    continue
                pos = haystack.find(anchor.lower())
                if pos >= 0:
                    break

            if pos < 0:
                return markers[0][1] if markers and markers[0][1] > 0 else 1

            page = 1
            for marker_pos, marker_page in markers:
                if marker_pos <= pos:
                    page = marker_page if marker_page > 0 else page
                else:
                    break
            return page if page > 0 else 1

        # Patterns that indicate garbage/scraped-UI content in description or evidence
        _GARBAGE_PATTERNS = re.compile(
            r"View PDF|"                       # any "View PDF" (with or without number)
            r"Date of (Listing|Upload)|"
            r"Listing History|"
            r"Case Status Pending|"
            r"Last Listed|"
            r"Next Listing Date|"
            r"All Parties|"
            r"IA/MA|"
            r"Copyright Policy|"
            r"Feedback|"
            r"Skip to main content|"
            r"Sitemap|"
            r"Hyperlinking Polic|"
            r"Contact Us|"
            r"Screen Reader Access|"
            r"Page last updated|"
            r"Connected Matters",
            re.I,
        )

        def _is_garbage_text(text: str) -> bool:
            """Return True if text looks like raw scraped UI/navigation content."""
            if not text:
                return False
            if _GARBAGE_PATTERNS.search(text):
                return True
            # Raw date-list dump: 3+ DD-MM-YYYY or DD/MM/YYYY tokens in the text
            if len(re.findall(r"\d{2}[-/]\d{2}[-/]\d{4}", text)) >= 3:
                return True
            return False

        document_text = str(state.get("document_text") or "")
        anchor_year = state.get("anchor_year")
        today_str_for_check = (
            state.get("today_str") or datetime.utcnow().strftime("%Y-%m-%d")
        )
        try:
            current_year_int = int(today_str_for_check[:4])
        except (TypeError, ValueError):
            current_year_int = datetime.utcnow().year

        normalized: List[Dict[str, Any]] = []
        seen_dates: set = set()
        for item in arr:
            if not isinstance(item, dict):
                continue
            date_val = str(item.get("date") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_val):
                continue

            # Reject placeholder / out-of-range years (e.g. 1111-11-11, 9999-99-99)
            try:
                year = int(date_val[:4])
                if year < 1900 or year > 2099:
                    continue
            except ValueError:
                continue

            # Reject strictly-future entries — list_of_dates is past-only.
            # Mirrors the same filter on the events.py path so a regeneration
            # that uses only this agent can't leak future dates.
            if date_val > today_str_for_check:
                logger.info(
                    "List of dates agent: dropped future-dated entry "
                    "(date={}, today={}) — list_of_dates is past-only",
                    date_val, today_str_for_check,
                )
                continue

            # Reject fake YYYY-01-01 placeholders synthesized from relative
            # phrases ("three years ago" → 2023-01-01) when the source does
            # NOT actually contain that 4-digit year as a year reference. If
            # the document explicitly mentions the year, we keep the entry —
            # only fake-precision interpolations get dropped.
            if (
                date_val[5:] == "01-01"
                and re.search(rf"\b{year}\b", document_text or "") is None
            ):
                logger.info(
                    "List of dates agent: dropped fake YYYY-01-01 placeholder "
                    "(date={}) — relative phrase only, no concrete day/month "
                    "and year not in source",
                    date_val,
                )
                continue

            # Post-LLM hallucination guard (shared across all date-extraction
            # paths via `snap_hallucinated_year`). Snaps a suspect year to the
            # current year when there's no anchor and the year isn't grounded
            # in the source text.
            snapped = snap_hallucinated_year(
                date_iso=date_val,
                document_text=document_text,
                today_iso=today_str_for_check,
                anchor_year=anchor_year if isinstance(anchor_year, int) else None,
                log_prefix="List of dates agent",
            )
            if snapped != date_val:
                date_val = snapped
                try:
                    year = int(date_val[:4])
                except ValueError:
                    pass

            # Skip duplicate dates (keep first occurrence)
            if date_val in seen_dates:
                continue
            seen_dates.add(date_val)

            title = str(item.get("title") or "").strip()
            evidence = str(item.get("evidenceSnippet") or "").strip()
            raw_desc = str(item.get("description") or "").strip()

            # Drop entries with no title and no evidence
            if not title and not evidence:
                continue

            # Drop entries whose description or evidence is raw garbage/UI text
            if _is_garbage_text(raw_desc) or _is_garbage_text(evidence):
                continue

            # Drop ALL generic "Date Mentioned in Document" entries — this title
            # is the LLM's fallback meaning it could not identify the event, so
            # it is never a factual case-relevant date worth keeping.
            if title.lower() in (
                "date mentioned in document",
                "date mentioned in text",
                "date in document",
                "mentioned date",
            ):
                continue

            # Cap at 350 chars WITHOUT splitting on sentence boundaries. The
            # naive ". " splitter treated abbreviation periods ("Model No.",
            # "Serial No.", "Rs.", "O.S. No.") as sentence ends and chopped
            # rich descriptions mid-clause (e.g. "...(Model No. /Serial No.").
            # Keep the full description and only trim at a word boundary if it
            # is unusually long.
            if raw_desc:
                description = raw_desc.strip()
                if len(description) > 350:
                    description = (
                        description[:350].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
                    )
            else:
                description = ""

            # Drop entries whose final description is too short to be meaningful
            # (e.g. truncated garbage like "Dr." or "Ltd.")
            if description and len(description.strip()) < 8:
                continue

            page_number = _resolve_page_from_text(item, document_text)
            entry = {
                "date": date_val,
                "title": (title or "Inferred Timeline Event"),
                "description": description,
                "sourceDocId": source_doc_id or None,
                "sourceDocName": source_doc_name or None,
                "pageNumber": page_number,
                "isIncident": bool(item.get("isIncident", False)),
                "eventType": (
                    str(item.get("eventType") or "").strip() or "Other"
                ),
                "relevance": (
                    str(item.get("relevance") or "").strip() or "Medium"
                ),
            }
            if source_doc_name:
                entry["source"] = source_doc_name
                entry["sourceAnswer"] = source_doc_name
            normalized.append({k: v for k, v in entry.items() if v is not None and v != ""})
        return {"events": normalized}

    async def infer_temporal_events(
        self,
        *,
        document_text: str,
        source_doc_id: str,
        source_doc_name: str,
        anchor_year: Optional[int],
        today_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        state: TemporalInferenceState = {
            "document_text": document_text,
            "source_doc_id": source_doc_id,
            "source_doc_name": source_doc_name,
            "anchor_year": anchor_year,
            "today_str": today_str or datetime.utcnow().strftime("%Y-%m-%d"),
        }
        result = await self._workflow.ainvoke(state)
        out = result.get("events") if isinstance(result, dict) else []
        return out if isinstance(out, list) else []


list_of_dates_temporal_agent = ListOfDatesTemporalAgent()
