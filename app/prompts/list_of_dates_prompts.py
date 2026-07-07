"""Prompt builders for the list_of_dates feature (extracted from handlers, verbatim)."""

from app.prompts.legal_intelligence_system import (
    APP_BRAND_EXCLUSION_DIRECTIVE,
    ENGLISH_ONLY_OUTPUT_DIRECTIVE,
    NAME_CAPITALISATION_DIRECTIVE,
)

__all__ = [
    "regenerate_list_of_dates_with_feedback_prompt",
    "reeval_list_of_dates_relevance_prompt",
    "generate_list_of_dates_from_documents_prompt",
]


def regenerate_list_of_dates_with_feedback_prompt(*, current_json, instruction):
    return (
f"""You are editing a legal case's LIST OF DATES (a JSON array of dated events). Apply ONLY the user's find-and-replace / correction instruction to the EXISTING entries and return the FULL array. This is a surgical edit, NOT a regeneration.

**User's edit instruction** (apply EXACTLY as worded):
{instruction}

CURRENT list of dates (edit THIS array in place):
{current_json}

STRICT RULES:
1. Apply ONLY what the instruction specifies — a correction of a NAME, TERM, DATE, or YEAR:
   - NAME change: replace the party's name wherever it appears in each entry's "title", "description", and "evidenceSnippet". If the party is referenced by ROLE or DESCRIPTION (e.g. "the wife", "the female employee", "the petitioner", "she"), use the role / gender / description context to identify her and apply the supplied name there too — a literal matching token is NOT required.
   - DATE or YEAR change: apply it to the "date" field as well. "date" is an ISO value "YYYY-MM-DD"; when the instruction replaces a year (e.g. 2024 → 2026), rewrite the year of EVERY "date" that carries the old year ("2024-05-25" → "2026-05-25"), and also update any date/year text that appears in "title", "description", and "evidenceSnippet".
2. Keep the SAME number of entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
3. Reproduce "relevance", "sourceDocId", "sourceDocName", "eventType", "pageNumber", and "isIncident" EXACTLY as-is for every entry. Change "date" ONLY when the instruction targets a date or year; otherwise reproduce it unchanged.
4. Change nothing except what the instruction specifies. Every other word / value stays verbatim.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""
    )


def reeval_list_of_dates_relevance_prompt(*, current_json, obj):
    return (
f"""The case OBJECTIVE / perspective has changed. Re-score how RELEVANT each existing dated event is to the NEW objective. Return the SAME array with ONLY the "relevance" field updated.

NEW objective / case perspective:
{obj}

CURRENT list of dates (re-score relevance against the new objective):
{current_json}

STRICT RULES:
1. Keep the SAME entries in the SAME order. Do NOT add, remove, merge, or reorder entries.
2. Change ONLY each entry's "relevance" value — set it to "High", "Medium", or "Low" based on how important that event is to the NEW objective.
3. Reproduce EVERY other field ("date", "title", "description", "evidenceSnippet", "sourceDocId", "sourceDocName", "eventType", "pageNumber", "isIncident") EXACTLY as-is — do NOT rewrite any text.

Return ONLY the complete, valid JSON array (no markdown code fences, no commentary)."""
    )


def generate_list_of_dates_from_documents_prompt(*, combined, current_year, list_of_dates_inventory, next_week_str, today_str, tomorrow_str):
    return (
f"""You are a legal case analyst. From the following annexures, extract a chronological list of FACTUAL, CASE-RELEVANT dates and events.

{ENGLISH_ONLY_OUTPUT_DIRECTIVE}

{APP_BRAND_EXCLUSION_DIRECTIVE}

{NAME_CAPITALISATION_DIRECTIVE}

**Today's date**: {today_str} (current year is {current_year}). Anchor every relative or partial date against this.

{list_of_dates_inventory or "DOCUMENT INVENTORY: (no classified inventory available)"}

**Annexure content** (each file is labeled Annexure 1, Annexure 2, etc. with id and name):
{combined}

**Task**:
1) Identify dates directly tied to this dispute: agreements, payments, notices, hearings where an order/action occurred, deadlines, possession, filings, and key communications.
2) For every such date, write a short, single-sentence description (max 12 words) of what actually happened.
3) Output a JSON array of date entries.

Each entry MUST have:
- "date": string in YYYY-MM-DD format only
- "title": string, 3-5 word title for the event (e.g. "FIR Registered", "Salary Deducted", "Marriage Ceremony", "Possession Delay")
- "description": string, one concise sentence describing what happened (max 12 words, no raw text dumps)
- "sourceDocumentIndex": integer (1-based), which document (1, 2, 3...) the event comes from
- "pageNumber": integer (mandatory, default 1 if unclear)
- "isIncident": boolean (optional, default true)
- "eventType": one of "Incident" | "Agreement" | "Payment" | "Communication" | "Other"
  (procedural event types — Court Hearing, Filing, Deadline, Meeting — belong
  to the Procedural Timeline panel and MUST NOT be used here)
- "relevance": one of "High" | "Medium" | "Low"

This panel records the FACTUAL NARRATIVE of the dispute — the chain of
real-world events that, taken together, explain how the dispute arose, what
each party did, and what concrete consequences followed. The dispute can be
of ANY type (civil, criminal, matrimonial, employment, property, immigration,
consumer, cyber-fraud, IPR, tax, regulatory, etc.) — the same principles apply
across all of them.

CORE TEST FOR EACH CANDIDATE DATE — keep the entry only if ALL three are true:
  (a) The date marks something that ACTUALLY HAPPENED in the past — not a
      future scheduled event, not a deadline, not a planned step. The date
      must be on or before today ({today_str}).
  (b) The event is CASE-RELEVANT — i.e. it directly contributes to the chain
      of cause-and-effect that gave rise to this dispute. Identity-document
      metadata (passport / visa / licence issue or expiry, ID generation
      timestamps, system-generated certificate numbers) is case-relevant
      ONLY when the dispute is ABOUT that document itself (e.g. an immigration
      overstay matter, a passport-fraud case, a visa-revocation challenge).
      In every other dispute, those dates are biographical / administrative
      noise and MUST be dropped.
  (c) The event is FACTUAL, not procedural. Court filings, hearings,
      adjournments, next-date orders, judgments, legal notices, demand
      notices, statutory notices, response deadlines, filing deadlines,
      evidence deadlines, compliance deadlines, limitation-period endings,
      lawyer consultations, and mediation sessions are PROCEDURAL — they
      belong to a separate Procedural Timeline panel, not this one.

ASK YOURSELF before keeping each date:
  - "If a lawyer were narrating the story of this dispute to a colleague,
     would they mention this event?" If no → drop.
  - "Is this date the kind of thing a court would care about because it
     describes the underlying conduct, harm, or transaction?" If yes → keep.
  - "Is this date about WHEN the parties did something to each other (or to
     the case property / instrument)?" If yes → keep.
  - "Is this date about WHEN a court / tribunal / regulator / lawyer scheduled
     or required something?" If yes → drop, it belongs on procedural_timeline.
  - "Would removing this date from the narrative leave a gap, or is it
     orphan metadata?" Drop if it is orphan metadata.

DEDUPLICATION — output ONE entry per real-world event:
  - If multiple parts of the source describe the same underlying event with
    slightly different wording (e.g. "Arrival in India" and "New Delhi
    Arrival" on the same date), emit ONE entry, not two.
  - If the same event is mentioned across multiple annexures, still ONE
    entry. Pick the most specific, factually clearest title and description.
  - Never emit two entries that share the same date AND describe the same
    underlying real-world action.

ALSO EXCLUDE:
- Future / upcoming dates of any kind (planned hearings, expiry deadlines,
  scheduled meetings, requested departure dates).
- Every row of a court listing / hearing table — those are procedural.
- Dates appearing only in "Date of Listing / Date of Upload / Order/Judgement"
  table columns with no substantive context.
- Dates alongside "View PDF", "IA/MA", "Connected Matters", "Contact Us",
  "Feedback", "Sitemap" or other website navigation text.
- Placeholder dates like 11-11-1111 or 9999-99-99.
- Background / historical company information unrelated to this dispute.
- Document-system metadata: "ETA Generation Date", "Document Generation Date",
  certificate creation timestamps, system audit timestamps, etc., unless that
  exact date is a load-bearing fact of the dispute.

Rules:
- Output ONLY a single valid JSON array. No markdown, no code fences, no other text.
- The first character of your response MUST be [ and the last MUST be ].
- Date normalization: output each "date" as YYYY-MM-DD.
- **Indian DD/MM/YYYY convention** — when the source contains a slash- or
  dash-separated numeric date like ``01/05/2026``, ``01-05-2026``, ``01.05.2026``,
  the FIRST number is the DAY and the SECOND number is the MONTH (Indian
  convention). Do NOT interpret it as US MM/DD/YYYY.
    • ``01/05/2026`` → DAY 1, MONTH 5 → ``2026-05-01`` (1 May 2026), NOT
      ``2026-01-05`` (5 January 2026).
    • ``13/04/2026`` → DAY 13, MONTH 4 → ``2026-04-13``.
    • Whenever the first number is > 12, that already proves DD/MM/YYYY
      (e.g. ``25/03/2026`` can only be 25 March). Apply the same rule even
      when both numbers are ≤ 12 — DAY first, MONTH second.
    • Only switch to MM/DD/YYYY if the source EXPLICITLY identifies the
      format (e.g. labels like "Date (MM/DD/YYYY): 01/05/2026") or the date
      sits inside a clearly US/foreign-jurisdiction document.
- **Year resolution when the source mentions a date without a year** (e.g. "2 February", "21 तारीख", "last Tuesday", "the 15th of March"):
  - Default to the CURRENT YEAR ({current_year}) — these are recent events from a live conversation/document, not historical records.
  - Pick the year that places the event ON OR BEFORE today ({today_str}); if "{current_year}-MM-DD" would be in the future relative to today, use {int(current_year) - 1} instead (event must have already happened to be reported).
  - Only omit the entry if the source itself explicitly states a year that is unreadable/contradictory.
- **Relative-time anchors** (e.g. "tomorrow", "today", "कल", "आज", "in 24 hours", "within 24 hours", "by 5 PM tomorrow", "next week", "next Monday"): anchor STRICTLY to today's date ({today_str}). Do NOT derive these from any other date mentioned in the source. Specifically:
  - "tomorrow" / "कल" / "by 5 PM tomorrow" / "by EOD tomorrow" → {tomorrow_str}.
  - "today" / "आज" / "in 24 hours" / "within 24 hours" → {today_str}.
  - "next week" / "in a week" / "in 7 days" → {next_week_str}.
  - "next Monday" / "next Tuesday" etc. → the next occurrence of that weekday strictly AFTER today ({today_str}).
- **Currency / money is NOT a year**. Numbers like "₹2000", "Rs. 7000", "$5000" are AMOUNTS in the case (salary, fines, transaction values), NOT year references. Never use a currency amount as a year for any date entry.
- Sorting: sort by date ascending. Tie-break: sourceDocumentIndex ascending, then title alphabetically.
- Escape quotes inside strings with \\". Keep title ≤ 5 words, description ≤ 12 words.

Example shape:
[{{"date":"{current_year}-01-15","title":"FIR Registered","description":"FIR was lodged at the local police station regarding the incident.","sourceDocumentIndex":1,"pageNumber":3,"isIncident":true,"eventType":"Incident","relevance":"High"}}]
"""
    )

