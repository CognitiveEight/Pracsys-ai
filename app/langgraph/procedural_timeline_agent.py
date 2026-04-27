"""
LangGraph agent for procedural / upcoming dates extraction.

Three-node pipeline:
  1. extract_per_document  — parallel, self-classifying extraction per document
                             (map step: each doc → its own list of raw entries)
  2. consolidate_timeline  — single LLM call that semantically merges
                             cross-document duplicates and drops noise
                             (reduce step: all entries → clean unified list)
  3. validate_and_merge    — date normalisation, type validation, fuzzy
                             deduplication, chronological sort

Design notes:
  • Per-document extraction runs in PARALLEL via asyncio.gather — a
    10-document workspace completes in ~8 s for this step.
  • The consolidation step is critical when the same event appears in
    multiple documents (e.g. a service registration referenced by its
    SRF number in 3 different annexures). The model merges phrasings
    like "Formal Service Registration" / "Service Request Registered" /
    "Registration of Service Request" into ONE authoritative entry.
  • Classification was removed entirely — the extraction prompt is
    self-classifying, and most classification calls previously failed
    safety checks and defaulted to unknown anyway.
  • Validator does NO domain-specific keyword filtering — the agent
    reasons about what belongs on the timeline.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_safety_error(raw: str) -> bool:
    """Return True when Gemini returned a safety-block error string."""
    s = (raw or "").strip()
    return s.startswith("Error") and (
        "finish_reason" in s
        or "finish reason" in s.lower()
        or "safety" in s.lower()
        or "valid `Part`" in s
    )


def _extract_filename_date_hint(doc_name: str) -> Optional[str]:
    """
    Detect YY-MM-DD / YY MM DD / YYMMDD / YYYY-MM-DD prefixes in filenames
    commonly used for Indian legal documents (e.g. "26 03 13 Legal Notice...").

    Returns the date as YYYY-MM-DD if a plausible prefix is found, else None.
    We assume 2-digit years in 00-49 are 20xx, 50-99 are 19xx.
    """
    if not doc_name:
        return None

    name = doc_name.strip()
    # Strip any leading path separators
    name = name.rsplit("/", 1)[-1]

    # Use `(?=\D|$)` lookahead instead of `\b` so underscore/dot separators are
    # treated as boundaries (Python regex word-boundary treats `_` as a word char).
    patterns = [
        # "2026-03-13 ..."  or  "2026_03_13 ..."  or  "2026.03.13 ..."
        r"^(?P<y>20\d{2})[-_. ](?P<m>\d{1,2})[-_. ](?P<d>\d{1,2})(?=\D|$)",
        # "26 03 13 ..."  or  "26-03-13 ..."  or  "26_03_13 ..."  or  "26.03.13 ..."
        r"^(?P<y>\d{2})[-_. ](?P<m>\d{1,2})[-_. ](?P<d>\d{1,2})(?=\D|$)",
        # "20260313" (compact 4-digit year) — check BEFORE 6-digit to avoid greedy match
        r"^(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})(?=\D|$)",
        # "260313 ..." (compact 2-digit year)
        r"^(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?=\D|$)",
    ]

    for pat in patterns:
        m = re.match(pat, name)
        if not m:
            continue
        try:
            y = int(m.group("y"))
            mo = int(m.group("m"))
            d = int(m.group("d"))
            if y < 100:
                y = 2000 + y if y <= 49 else 1900 + y
            if not (1900 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def _sanitize_content(text: str, max_chars: int = 15000) -> str:
    """
    Strip content that commonly trips Gemini safety filters while preserving
    legitimate legal text (party names, addresses, long references, etc.).

    Removes:
    - Non-printable / control characters
    - Long runs of identical special characters (OCR noise: ||||, ////…)
    - True binary/base64 blobs (35+ chars of [A-Za-z0-9+/] with optional `=`)

    Does NOT strip:
    - Long alphanumeric legal reference numbers, party names, proper nouns.
    """
    if not text:
        return ""
    # Remove non-printable control characters (keep newline, tab, space)
    cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\u00A0-\uFFFF]", " ", text)
    # Collapse runs of 5+ identical non-alphanumeric chars (OCR noise)
    cleaned = re.sub(r"([^a-zA-Z0-9 \n\t])\1{4,}", r"\1", cleaned)
    # Remove true binary/base64 blobs: 35+ chars of [A-Za-z0-9+/] with optional =
    cleaned = re.sub(r"[A-Za-z0-9+/]{35,}={0,2}", "[binary-data]", cleaned)
    # Normalise excessive whitespace
    cleaned = re.sub(r"\n{4,}", "\n\n", cleaned)
    cleaned = re.sub(r" {4,}", "   ", cleaned)
    return cleaned.strip()[:max_chars]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ProceduralTimelineState(TypedDict, total=False):
    workspace_id: str
    today_str: str
    # Input documents: list of {document_id, name, content}
    documents: List[Dict[str, Any]]
    # Raw per-document extractions (may have cross-document duplicates)
    raw_entries: List[Dict[str, Any]]
    # After semantic consolidation across documents
    consolidated_entries: List[Dict[str, Any]]
    # Final validated, formatted timeline
    timeline: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Extraction prompt (self-classifying, comprehensive)
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_INSTRUCTION = """You are an expert Indian legal analyst building the
PROCEDURAL TIMELINE of a legal matter — the chronological list of KEY LEGAL MILESTONES
that shape the case, from the earliest legally-relevant event to the latest deadline.

The procedural timeline is broader than just "court deadlines". It is the **legal journey**
of the matter: the critical moments a lawyer would mark on a case timeline when preparing
a brief.
"""


def _build_extraction_prompt(
    today_str: str,
    doc_name: str,
    excerpt: str,
    filename_date_hint: Optional[str] = None,
) -> str:
    hint_block = ""
    if filename_date_hint:
        hint_block = (
            f"\nFILENAME DATE HINT: the filename prefix parses to "
            f"{filename_date_hint}. If the document body is a legal notice with a "
            f"blank or unclear date field, USE THIS DATE as the notice issue date.\n"
        )

    return f"""{_EXTRACTION_SYSTEM_INSTRUCTION}

Today's date: {today_str}
Document name: {doc_name}
{hint_block}
========================================================================
FIRST — if this is a LEGAL NOTICE, extracting these TWO entries is MANDATORY
========================================================================

If the document contains phrases like "LEGAL NOTICE", "DEMAND NOTICE", "NOTICE
UNDER SECTION ...", "you are called upon to ...", "within N days from receipt
of this notice" — you MUST output BOTH of these entries, no matter what else
you find:

  A. ISSUANCE OF LEGAL NOTICE (type="Other")
     → date = the notice issue date.
     → If the document body has a BLANK placeholder (e.g. "Date: __ March 2026",
       "Date: ____") OR the date field is ambiguous, USE the FILENAME DATE HINT
       above. Indian legal-notice filenames conventionally start with a YY-MM-DD,
       YY MM DD, or YY.MM.DD prefix.

  B. LEGAL NOTICE RESPONSE DEADLINE (type="Deadline")
     → Extract the EXPLICIT deadline date if one is stated (e.g. "close of
       business on 08 April 2026" → 2026-04-08). This is the preferred source.
     → Otherwise compute: deadline = notice_date + response_period (e.g. +15 days).
     → If both are present, the EXPLICIT date wins.

========================================================================
INCLUDE — dates that belong on the procedural timeline
========================================================================

1. PRE-LITIGATION LEGAL MILESTONES (type="Other"):
   Only events that are LEGALLY-SIGNIFICANT TURNING POINTS — not every documented
   interaction. Pick at most 1-2 milestones per event-category.
   Qualifying examples:
     • The FIRST formal service/complaint request registered (SRF No., ticket no.)
     • The FIRST formal grievance email/letter invoking legal rights / demanding remedy
     • Date a statutory/regulatory complaint was lodged (RERA, Consumer Forum intake)

   DO NOT classify as milestone:
     • Shipping/courier events: "receipt of item", "return of product", "declaration of
       return", "protest receipt" — these are commercial logistics, not legal milestones
     • Intermediate service records, repair progress updates, diagnosis reports
     • Follow-up emails that merely repeat an earlier grievance

2. NOTICE ISSUANCE (type="Other"):
   • Date a legal notice / demand notice / statutory notice was SENT by any party.
   • Title example: "Issuance of Legal Notice".

3. RESPONSE / FILING DEADLINE (type="Deadline"):
   • Compute: response_deadline = notice_date + response_period (e.g. 30 days).
     Example: notice dated 13-03-2026 + 26 days → 2026-04-08.
   • Any court-directed filing deadline (written statement, reply, affidavit).
   • Any "complaint will be filed within X days" window end date.

4. COURT HEARING DATES (type="Court Hearing"):
   • Any date the matter is listed before a court, tribunal, forum, or authority.
   • Also use this type for ADJOURNMENT / NEXT DATE entries (next date set when
     a matter is adjourned).

5. EVIDENCE DEADLINES (type="Deadline"):
   • Any deadline for submitting documents, witness lists, affidavits of evidence.

6. COMPLIANCE DATES (type="Deadline"):
   • Any date by which a party must comply with a court/tribunal order.

7. ACTUAL COURT FILINGS (type="Filing"):
   • Past dates on which a petition, complaint, written statement, or reply
     was actually filed in a court / tribunal / forum.

8. CLIENT MEETINGS / CONSULTATIONS (type="Meeting"):
   • Scheduled meetings, mediation sessions, or consultations.

========================================================================
STRICTLY EXCLUDE — do NOT output any of the following
========================================================================

  ✗ Commercial / product dates:
      purchase date, delivery date, warranty start/expiry, repair ETA, service warranty,
      product launch, installation, shipment, account opening, insurance policy period.
  ✗ Day-to-day events that are NOT legal milestones:
      phone calls, internal meetings, staff assignments, courier tracking timestamps.
  ✗ Statutory limitation periods stated abstractly (e.g. "2 years to sue", "3 years from
      cause of action") — those are NOT concrete dates.
  ✗ The underlying cause-of-action event itself (e.g. "date the product broke", "date of
      accident", "date of alleged defect noticed") — unless that same event was formally
      registered/documented with a reference number, in which case include the registration
      as a "milestone" (not the occurrence).
  ✗ Dates more than 3 years in the past OR more than 2 years in the future from today,
      unless the document EXPLICITLY states that date as a scheduled court/hearing event.

========================================================================
RULES
========================================================================

  1. Always convert dates to YYYY-MM-DD. For relative periods ("within N days"),
     compute anchor_date + N days.
  2. Include both past and future dates — past milestones give the case its chronology,
     future dates are upcoming deadlines.
  3. "Milestone vs. commercial event" test: ask "Did this date create a legal record,
     or was it just a commercial transaction?" Only legal records qualify as milestones.
  4. If the document is a photo / receipt / product spec / warranty card / internal memo
     / chat log with no legal-milestone content, return [].
  5. Output at most 8 entries per document — pick the most important ones.

--- DOCUMENT START ---
{excerpt}
--- DOCUMENT END ---

Return ONLY a JSON array. Each entry must have exactly these fields:
  "title"       : 3–6 words naming the milestone/step
                  (e.g. "Service Registration", "Formal Grievance Initiated",
                        "Issuance of Legal Notice", "Legal Notice Response Deadline",
                        "Next Hearing — Arguments")
  "date"        : YYYY-MM-DD (computed if relative)
  "description" : one sentence ≤ 20 words describing the event/obligation
  "type"        : EXACTLY one of these five values (case-sensitive):
                    "Court Hearing" — court / tribunal hearings, adjournments, next dates
                    "Deadline"      — any obligation deadline (response, filing, evidence,
                                       compliance, statutory response window)
                    "Filing"        — an ACTUAL past filing event in a court / tribunal
                                       (petition filed, written statement filed, reply filed)
                    "Meeting"       — scheduled meetings, mediations, consultations
                    "Other"         — pre-litigation milestones, legal-notice issuance,
                                       service registrations, formal grievances, anything
                                       that does not clearly fit the other four buckets

If no qualifying entries exist in this document, return [].
Output ONLY the JSON array — no markdown fences, no commentary."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ProceduralTimelineAgent:
    """
    Two-step LangGraph agent:  extract_per_document → validate_and_merge.

    Extraction happens in parallel across all input documents for speed.
    """

    # Progressive excerpt sizes — try largest first, fall back on safety block
    _EXCERPT_SIZES = (12000, 5000, 1800)

    def __init__(self) -> None:
        self._workflow = self._build_graph()

    # ------------------------------------------------------------------ graph

    def _build_graph(self):
        g = StateGraph(ProceduralTimelineState)
        g.add_node("extract_per_document", self._node_extract_per_document)
        g.add_node("consolidate_timeline", self._node_consolidate_timeline)
        g.add_node("validate_and_merge", self._node_validate_and_merge)
        g.add_edge(START, "extract_per_document")
        g.add_edge("extract_per_document", "consolidate_timeline")
        g.add_edge("consolidate_timeline", "validate_and_merge")
        g.add_edge("validate_and_merge", END)
        return g.compile()

    # ------------------------------------------------------------------ nodes

    async def _extract_one(
        self,
        *,
        doc: Dict[str, Any],
        today_str: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract procedural dates from a single document, retrying with
        progressively smaller excerpts if Gemini safety-blocks.
        """
        from app.ai.gemini_client import gemini_client

        content = (doc.get("content") or "").strip()
        doc_name = str(doc.get("name") or doc.get("document_id") or "document")
        if not content or len(content) < 20:
            return []

        filename_date_hint = _extract_filename_date_hint(doc_name)
        if filename_date_hint:
            logger.info(
                "Procedural timeline: filename date hint doc='{}' → {}",
                doc_name, filename_date_hint,
            )

        entries: List[Dict[str, Any]] = []

        for attempt_idx, max_chars in enumerate(self._EXCERPT_SIZES, 1):
            excerpt = _sanitize_content(content, max_chars=max_chars)
            if not excerpt or len(excerpt) < 20:
                break

            prompt = _build_extraction_prompt(
                today_str, doc_name, excerpt, filename_date_hint=filename_date_hint,
            )

            try:
                raw = await gemini_client.generate(
                    prompt=prompt,
                    temperature=0.0,
                    max_tokens=12000,
                    response_mime_type="application/json",
                )
            except Exception as exc:
                logger.warning(
                    "Procedural timeline: extraction exception doc='{}' attempt={}: {}",
                    doc_name, attempt_idx, exc,
                )
                break

            raw = (raw or "").strip()
            logger.info(
                "Procedural timeline extract: doc='{}' attempt={} ({} chars excerpt) "
                "response ({} chars): {}",
                doc_name, attempt_idx, max_chars, len(raw), raw[:300],
            )

            if _is_safety_error(raw):
                logger.warning(
                    "Procedural timeline extract: safety block doc='{}' attempt={} — "
                    "retrying with smaller excerpt",
                    doc_name, attempt_idx,
                )
                continue

            if not raw or raw.startswith("Error"):
                break

            try:
                parsed = json.loads(raw)
            except Exception as exc:
                logger.warning(
                    "Procedural timeline: JSON parse error doc='{}': {} (raw preview={})",
                    doc_name, exc, raw[:200],
                )
                break

            if isinstance(parsed, list):
                entries = parsed
            elif isinstance(parsed, dict):
                entries = (
                    parsed.get("proceduralTimeline")
                    or parsed.get("items")
                    or parsed.get("dates")
                    or []
                )
            else:
                entries = []
            break

        # Tag entries with source metadata for debugging
        tagged: List[Dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                entry["_source_doc_id"] = doc.get("document_id", "")
                entry["_source_doc_name"] = doc_name
                tagged.append(entry)
        return tagged

    async def _node_extract_per_document(
        self, state: ProceduralTimelineState
    ) -> Dict[str, Any]:
        """
        Run extraction in parallel across all input documents.
        """
        from app.ai.gemini_client import gemini_client

        documents = state.get("documents") or []
        today_str = state.get("today_str") or datetime.utcnow().strftime("%Y-%m-%d")

        if not documents:
            return {"raw_entries": []}

        gemini_client.initialize()
        if not gemini_client.model:
            logger.warning("Procedural timeline: Gemini unavailable — returning empty")
            return {"raw_entries": []}

        # Run all documents concurrently
        tasks = [self._extract_one(doc=doc, today_str=today_str) for doc in documents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_entries: List[Dict[str, Any]] = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "Procedural timeline: extraction task failed doc='{}': {}",
                    documents[idx].get("name", ""), result,
                )
                continue
            if isinstance(result, list):
                all_entries.extend(result)

        logger.info(
            "Procedural timeline: extracted {} raw entries across {} documents",
            len(all_entries), len(documents),
        )
        return {"raw_entries": all_entries}

    async def _node_consolidate_timeline(
        self, state: ProceduralTimelineState
    ) -> Dict[str, Any]:
        """
        Semantic consolidation pass: take all per-document extractions and merge
        entries that describe the SAME legal event even if they were phrased
        differently across documents. Also drops commercial/shipping events the
        per-document step let through.

        This is a single Gemini call over the LIST OF RAW ENTRIES — it does NOT
        re-read the original documents, so it is fast (~3-5 s).
        """
        from app.ai.gemini_client import gemini_client

        raw_entries = state.get("raw_entries") or []
        today_str = state.get("today_str") or datetime.utcnow().strftime("%Y-%m-%d")

        if not raw_entries:
            return {"consolidated_entries": []}

        # If we have very few entries, skip the consolidation call — no duplicates to merge.
        if len(raw_entries) <= 2:
            return {"consolidated_entries": raw_entries}

        gemini_client.initialize()
        if not gemini_client.model:
            return {"consolidated_entries": raw_entries}

        # Present entries to the model with source-doc tags so it can see WHY duplicates arose
        serialised: List[Dict[str, Any]] = []
        for e in raw_entries:
            if not isinstance(e, dict):
                continue
            serialised.append(
                {
                    "title": str(e.get("title") or "").strip(),
                    "date": str(e.get("date") or "").strip(),
                    "description": str(e.get("description") or "").strip(),
                    "type": str(e.get("type") or "").strip(),
                    "source": str(e.get("_source_doc_name") or "").strip(),
                }
            )

        try:
            entries_json = json.dumps(serialised, ensure_ascii=False, indent=2)
        except Exception:
            return {"consolidated_entries": raw_entries}

        prompt = f"""You are consolidating a legal case timeline. Below is a RAW list of
timeline entries extracted from multiple documents of the SAME legal matter. Many entries
describe the SAME underlying event but were phrased differently across documents.

Today's date: {today_str}

TASK — produce the CLEAN legal timeline by:
  1. MERGING near-duplicates: if two or more entries describe the same real-world event
     (within ~10 days of each other AND semantically equivalent — e.g. "Service
     Registration" vs "Formal Service Request Registered"), KEEP ONLY ONE. Pick the
     clearest, most authoritative title and the most precise date.
  2. DROPPING commercial/logistics events that slipped through:
       • shipping/courier events (receipt of item, return of product, "under protest")
       • intermediate repair/diagnosis updates
       • warranty/delivery/purchase events
  3. PRESERVING every unique legal-journey event:
       • First service registration / first formal complaint  (type="Other")
       • First formal grievance / demand letter               (type="Other")
       • Legal notice issuance                                (type="Other")
       • Response / filing / compliance / evidence deadlines  (type="Deadline")
       • Court hearings, adjournments, next-date orders       (type="Court Hearing")
       • Actual past court filings                            (type="Filing")
       • Scheduled meetings / consultations                   (type="Meeting")
  4. **MANDATORY** — if any raw entry concerns a LEGAL NOTICE (title contains
     "Notice", description mentions a legal/demand/statutory notice), OR any
     source-doc name starts with a legal-notice filename prefix
     (e.g. "YY MM DD Legal Notice..."), the final timeline MUST contain BOTH:
       (a) an "Issuance of Legal Notice" entry (type="Other") on the notice date.
           If the notice date is not explicit in the raw entries, derive it from the
           filename prefix (e.g. "26 03 13 Legal Notice ..." → 2026-03-13).
       (b) a "Legal Notice Response Deadline" entry (type="Deadline"). Prefer any
           explicit deadline mentioned in source descriptions; otherwise compute
           notice_date + 15 days (common Indian statutory period) or + 30 days.
     Do NOT skip these two entries even if no raw entry listed them — infer them.
  5. Keep AT MOST 6-8 entries total. Prioritise events in this order:
       response/filing deadlines > court hearings > legal-notice issuance >
       first formal grievance > first service registration > everything else.

RAW ENTRIES:
{entries_json}

OUTPUT — ONLY a JSON array of the consolidated entries, sorted chronologically (oldest
first). Each entry:
  "title"       : 3–6 words (concise, authoritative)
  "date"        : YYYY-MM-DD
  "description" : one sentence ≤ 20 words
  "type"        : EXACTLY one of (case-sensitive):
                    "Court Hearing" | "Deadline" | "Filing" | "Meeting" | "Other"

Output ONLY the JSON array — no markdown fences, no commentary."""

        try:
            raw = await gemini_client.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=8000,
                response_mime_type="application/json",
            )
        except Exception as exc:
            logger.warning("Procedural timeline consolidate: exception {}", exc)
            return {"consolidated_entries": raw_entries}

        raw = (raw or "").strip()
        logger.info(
            "Procedural timeline consolidate: response ({} chars): {}",
            len(raw), raw[:400],
        )

        if _is_safety_error(raw) or not raw or raw.startswith("Error"):
            logger.warning("Procedural timeline consolidate: falling back to raw entries")
            return {"consolidated_entries": raw_entries}

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            logger.warning(
                "Procedural timeline consolidate: JSON parse error {} — using raw entries",
                exc,
            )
            return {"consolidated_entries": raw_entries}

        if isinstance(parsed, list):
            consolidated = parsed
        elif isinstance(parsed, dict):
            consolidated = (
                parsed.get("proceduralTimeline")
                or parsed.get("items")
                or parsed.get("timeline")
                or parsed.get("entries")
                or []
            )
        else:
            consolidated = []

        if not isinstance(consolidated, list) or not consolidated:
            logger.warning(
                "Procedural timeline consolidate: empty result — using raw entries"
            )
            return {"consolidated_entries": raw_entries}

        logger.info(
            "Procedural timeline consolidate: {} raw → {} consolidated",
            len(raw_entries), len(consolidated),
        )
        return {"consolidated_entries": consolidated}

    async def _node_validate_and_merge(
        self, state: ProceduralTimelineState
    ) -> Dict[str, Any]:
        """
        Validate date formats, normalise type labels, deduplicate, sort
        chronologically. Consumes the consolidated entries (preferred) or
        falls back to raw entries if consolidation was skipped.
        """
        # Prefer consolidated entries; fall back to raw_entries
        raw_entries = (
            state.get("consolidated_entries")
            or state.get("raw_entries")
            or []
        )
        today_str = state.get("today_str") or datetime.utcnow().strftime("%Y-%m-%d")
        try:
            today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        except ValueError:
            today_dt = datetime.utcnow()

        # Five canonical types — the only values the frontend understands.
        # Stored as Title Case because that is what the FE renders directly.
        _ALLOWED_TYPES = frozenset(
            {
                "Court Hearing",  # court / tribunal hearings, adjournments, next dates
                "Deadline",       # any obligation deadline (response, filing, evidence,
                                  # compliance, statutory response window)
                "Filing",         # an actual past filing event in a court / tribunal
                "Meeting",        # scheduled meetings, mediations, consultations
                "Other",          # pre-litigation milestones, legal-notice issuance,
                                  # service registrations, formal grievances, etc.
            }
        )
        # Map every plausible variant the model might emit (lowercased) → canonical
        # FE Title Case value. Includes legacy internal names for backward-compat.
        _TYPE_ALIASES: Dict[str, str] = {
            # ----- Court Hearing -----
            "court hearing": "Court Hearing",
            "hearing": "Court Hearing",
            "hearing date": "Court Hearing",
            "next date": "Court Hearing",
            "next hearing": "Court Hearing",
            "adjourned": "Court Hearing",
            "adjournment": "Court Hearing",
            "adjournment date": "Court Hearing",
            "adjourned date": "Court Hearing",
            "tribunal hearing": "Court Hearing",
            "court date": "Court Hearing",

            # ----- Deadline -----
            "deadline": "Deadline",
            "response deadline": "Deadline",
            "filing deadline": "Deadline",
            "notice response": "Deadline",
            "notice response deadline": "Deadline",
            "legal notice deadline": "Deadline",
            "compliance": "Deadline",
            "compliance deadline": "Deadline",
            "compliance order": "Deadline",
            "court order compliance": "Deadline",
            "evidence": "Deadline",
            "evidence deadline": "Deadline",
            "evidence submission": "Deadline",
            "document submission": "Deadline",
            "affidavit submission": "Deadline",
            "due date": "Deadline",
            "limitation": "Deadline",
            "limitation deadline": "Deadline",

            # ----- Filing -----
            "filing": "Filing",
            "court filing": "Filing",
            "petition filed": "Filing",
            "complaint filed": "Filing",
            "written statement filed": "Filing",
            "reply filed": "Filing",
            "filed": "Filing",

            # ----- Meeting -----
            "meeting": "Meeting",
            "consultation": "Meeting",
            "mediation": "Meeting",
            "client meeting": "Meeting",
            "conference": "Meeting",

            # ----- Other (catch-all for milestones / notices / pre-litigation) -----
            "other": "Other",
            "milestone": "Other",
            "pre-litigation": "Other",
            "pre_litigation": "Other",
            "event": "Other",
            "case event": "Other",
            "registration": "Other",
            "service registration": "Other",
            "grievance": "Other",
            "formal grievance": "Other",
            "grievance initiated": "Other",
            "notice": "Other",
            "legal notice": "Other",
            "demand notice": "Other",
            "notice issuance": "Other",
            "notice sent": "Other",
            "issuance": "Other",
            "statutory notice": "Other",
        }

        result: List[Dict[str, Any]] = []
        seen_keys: set = set()

        for item in raw_entries:
            if not isinstance(item, dict):
                continue

            date_val = (item.get("date") or "").strip()
            if not date_val:
                continue

            # Normalise date → YYYY-MM-DD
            parsed_date: Optional[datetime] = None
            try:
                for fmt in (
                    "%Y-%m-%d",
                    "%d/%m/%Y",
                    "%d-%m-%Y",
                    "%B %d, %Y",
                    "%d %B %Y",
                ):
                    try:
                        parsed_date = datetime.strptime(date_val, fmt)
                        date_val = parsed_date.strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
                if parsed_date is None:
                    continue
                year = parsed_date.year
                if year < 1900 or year > 2099:
                    continue
            except Exception:
                continue

            # Sanity-cap: reject dates too far in past / future, UNLESS the entry is
            # a court-scheduled hearing / adjournment (those can legitimately be far out).
            raw_type_check = (item.get("type") or "").strip().lower()
            if raw_type_check not in ("hearing", "adjournment", "court hearing"):
                days_delta = (parsed_date - today_dt).days
                if days_delta > 365 * 2:
                    logger.info(
                        "Procedural timeline: ❌ dropped (too far in future, {} days) "
                        "title='{}' date={} type={}",
                        days_delta, (item.get("title") or "").strip(), date_val,
                        raw_type_check,
                    )
                    continue
                if days_delta < -365 * 3:
                    logger.info(
                        "Procedural timeline: ❌ dropped (too far in past, {} days) "
                        "title='{}' date={} type={}",
                        days_delta, (item.get("title") or "").strip(), date_val,
                        raw_type_check,
                    )
                    continue

            title = (item.get("title") or "").strip()
            description = (item.get("description") or "").strip()
            if not title or not description:
                continue

            # Keep first sentence, cap 150 chars
            first_sentence = re.split(r"(?<=[.!?])\s", description)[0]
            description = first_sentence[:150]

            raw_type_input = (item.get("type") or "").strip()
            raw_type_lower = raw_type_input.lower()
            # 1. Direct match (model emitted exact FE Title Case value).
            if raw_type_input in _ALLOWED_TYPES:
                raw_type = raw_type_input
            else:
                # 2. Alias lookup on lowercased value.
                raw_type = _TYPE_ALIASES.get(raw_type_lower, "")
                # 3. Last-resort: if model emitted any of the FE values with weird
                #    casing (e.g. "court hearing"), recover by Title Case lookup.
                if not raw_type:
                    title_cased = " ".join(w.capitalize() for w in raw_type_lower.split())
                    if title_cased in _ALLOWED_TYPES:
                        raw_type = title_cased
            if not raw_type:
                logger.info(
                    "Procedural timeline: ❌ dropped (unrecognised type '{}') "
                    "title='{}' date={}",
                    item.get("type", ""), title, date_val,
                )
                continue

            # Dedup key: (date, type, first 3 words of title lowercased).
            # Conservative — the LLM consolidation pass handles semantic merging.
            title_words = re.findall(r"[A-Za-z]+", title.lower())
            title_key = " ".join(title_words[:3]) if title_words else title.lower()[:20]
            key = (date_val, raw_type, title_key)
            if key in seen_keys:
                logger.info(
                    "Procedural timeline: 🔁 deduped near-duplicate title='{}' date={}",
                    title, date_val,
                )
                continue
            seen_keys.add(key)

            logger.info(
                "Procedural timeline: ✅ keeping title='{}' date={} type={}",
                title, date_val, raw_type,
            )
            result.append(
                {
                    "title": title,
                    "date": date_val,
                    "description": description,
                    "type": raw_type,
                }
            )

        result.sort(key=lambda x: str(x.get("date") or ""))
        return {"timeline": result}

    # ------------------------------------------------------------------ public

    async def extract(
        self,
        *,
        workspace_id: str,
        documents: List[Dict[str, Any]],
        today_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run the full procedural timeline extraction pipeline.

        Args:
            workspace_id: workspace identifier (for logging)
            documents: list of {document_id, name, content} dicts
            today_str: today's date as YYYY-MM-DD (defaults to UTC now)

        Returns:
            Sorted list of {title, date, description, type} procedural entries.
        """
        if not today_str:
            today_str = datetime.utcnow().strftime("%Y-%m-%d")

        state: ProceduralTimelineState = {
            "workspace_id": workspace_id,
            "today_str": today_str,
            "documents": documents,
        }
        result = await self._workflow.ainvoke(state)
        timeline = result.get("timeline") if isinstance(result, dict) else []
        return timeline if isinstance(timeline, list) else []


# Module-level singleton — mirrors the pattern used by list_of_dates_temporal_agent
procedural_timeline_agent = ProceduralTimelineAgent()
