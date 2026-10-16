"""Prompt builders for the objective_change feature (extracted from handlers, verbatim)."""


__all__ = [
    "llm_semantic_keep_indexes_prompt",
    "llm_merge_against_previous_prompt",
    "llm_merge_strengths_against_previous_prompt",
    "llm_merge_dated_entries_against_previous_prompt",
    "llm_classify_factual_vs_procedural_prompt",
    "merge_applicable_laws_with_previous_prompt",
    "llm_resolve_answered_missing_info_prompt",
    "llm_match_fresh_against_previous_prompt",
]


def llm_match_fresh_against_previous_prompt(*, item_type, numbered_fresh, numbered_prev):
    """Match freshly-regenerated items to the previously-curated items by MEANING.

    Used for currentState reconciliation: a fresh item that means the same as a
    previous item carries that previous item's identity (isMatched). This is
    identity-matching only — it never rewrites, merges, or drops content.
    """
    return (
f"""You are matching a freshly-regenerated {item_type} list to the version the user previously curated, for the SAME legal case analysis.

PREVIOUS list (already curated by the user — each line is "index. [id] text"):
{numbered_prev}

FRESH list (freshly regenerated — each line is "index. text"):
{numbered_fresh}

YOUR TASK: decide which FRESH items are the SAME (by meaning) as a PREVIOUS item.

RULES (apply strictly):
1. A FRESH item MATCHES a PREVIOUS item only when they express the SAME underlying requirement / risk / fact / concern — even if worded differently or more/less specific.
2. Each FRESH item matches AT MOST ONE previous item; each PREVIOUS item is used AT MOST ONCE. Pick the single best pairing.
3. If a FRESH item introduces a clearly distinct object/requirement not covered by any PREVIOUS item, leave it UNMATCHED (do not force a match).
4. Do NOT rewrite, merge, re-rank, or invent items. Only report pairings.

Return ONLY this JSON, no commentary:
{{"matches":[{{"freshIndex":1,"prevId":"<the id in brackets from PREVIOUS>"}}]}}
Return {{"matches":[]}} if nothing matches.
"""
    )


def llm_resolve_answered_missing_info_prompt(*, numbered_items, entities_block, document_content):
    return (
f"""You are auditing the "Missing Information" list of a legal case analysis after new documents were uploaded.

MISSING INFORMATION ITEMS (currently shown to the user):
{numbered_items}

EXTRACTED FACTS (authoritative entities pulled from ALL uploaded documents):
{entities_block}

DOCUMENT CONTENT (excerpts from ALL uploaded documents):
{document_content}

YOUR TASK: identify which items are now ANSWERED by the documents.

RULES (apply strictly):
1. Mark an item as answered ONLY when a specific fact or passage above DIRECTLY supplies the exact information the item asks for.
2. Quote that fact/passage as evidence. No evidence quote = NOT answered.
3. Partial or inferred coverage is NOT answered. When in doubt, keep the item as missing.
4. Do not rewrite, merge, or re-rank items. Only classify.

Return ONLY this JSON, no commentary:
{{"answered":[{{"index":1,"evidence":"exact quote from facts or documents"}}]}}
Return {{"answered":[]}} if nothing is answered.
"""
    )


def llm_semantic_keep_indexes_prompt(*, item_type, max_keep, numbered):
    return (
f"""You are a strict semantic deduplicator.
Given the numbered {item_type} list below, remove duplicates by MEANING (not wording).
If two or more items mean the same thing, keep only the best, most specific one.
Preserve distinct items even if related.

Return ONLY JSON:
{{"keepIndexes":[1,2,5]}}

Rules:
- keepIndexes must be 1-based indexes from the list.
- No duplicates in keepIndexes.
- Keep original order.
- Keep at most {max_keep} items.

{item_type.upper()}:
{numbered}
"""
    )


def llm_merge_against_previous_prompt(*, item_type, max_keep, numbered_new, numbered_prev):
    return (
f"""You are merging two versions of a {item_type} list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved verbatim, in original order):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new items):
{numbered_new}

YOUR TASK: produce the cumulative list the user should see.

RULES (apply strictly):
1. Keep EVERY item from PREVIOUS verbatim, in its original order.
2. From NEW: append an item ONLY if it expresses a genuinely DIFFERENT meaning from EVERY item already in PREVIOUS.
3. Treat as DUPLICATES (drop the NEW item, do not append):
     - Wording variations of the same concept ("Name of the Petitioner's bank" vs "Name of the Petitioner's bank and specific branch").
     - Paraphrased restatements of the same fact / requirement / risk.
     - Same underlying entity / document / event with slightly different framing.
4. KEEP as new (do append) only when the NEW item refers to a clearly distinct object, requirement, or concern not already covered by any PREVIOUS item.
5. Do NOT merge items together, do NOT rewrite anything — just preserve PREVIOUS verbatim and append the genuine novelties from NEW.
6. Cap the final list at {max_keep} items. If more would qualify, drop the lowest-priority ones from NEW.

Return ONLY this JSON, no commentary:
{{"merged":["...","..."]}}
"""
    )


def llm_merge_strengths_against_previous_prompt(*, max_keep, numbered_new, numbered_prev):
    return (
f"""You are merging two versions of a CASE STRENGTHS list from regenerations of the same legal case analysis.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new strengths):
{numbered_new}

YOUR TASK: produce the cumulative strengths list using indexes:
- Output an array of items, each with a "source" ("prev" or "new") and "index" (1-based).
- Include EVERY item from PREVIOUS (in order), as ``{{"source":"prev","index":N}}``.
- After all PREVIOUS items, append NEW items as ``{{"source":"new","index":M}}`` ONLY if they express a genuinely DIFFERENT strength from EVERY item already in PREVIOUS.
- Drop NEW items that are paraphrased duplicates of any PREVIOUS item.
- Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
    )


def llm_merge_dated_entries_against_previous_prompt(*, item_type, max_keep, numbered_new, numbered_prev):
    return (
f"""You are merging two versions of a {item_type} timeline from regenerations of the same legal case.

Each entry is formatted as ``<date> — <title> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new events):
{numbered_new}

YOUR TASK: produce the cumulative timeline by reference, NOT by rewriting.

RULES (apply strictly):
1. Include EVERY item from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append items from NEW as ``{{"source":"new","index":M}}`` ONLY when they describe a genuinely DIFFERENT event from every PREVIOUS item.

3. **DESCRIPTION is a primary duplicate signal** — weight it equally with title.
   Two entries on the SAME date (or within ~3 days) are DUPLICATES when their
   descriptions describe the same action/observation, EVEN IF the titles are
   worded differently. The model writing each version may pick different
   titles ("Marked as Absconded" vs "Alleged Absconding Date") for the same
   underlying fact — do not treat the title difference as evidence of distinct
   events when the descriptions are about the same thing.

   Two descriptions are "the same action/observation" when they reference:
     • the same actor (the company / the bank / the court / the petitioner),
     • the same action (marked, signed, filed, deposited, dismissed), and
     • the same object/target (the employee, the contract, the petition).
   Allowed paraphrasing: a specific name vs a generic referent
   ("Yogesh" ↔ "the employee"), tense ("marked" ↔ "has marked"), and
   trailing date phrasing ("on February 21st" ↔ "" when the entry's date is
   already 2026-02-21).

   **Worked example (drop the NEW one):**
     PREVIOUS: ``2026-02-21 — Marked as Absconded — The company marked Yogesh as absconded after his visit on February 21st.``
     NEW:      ``2026-02-21 — Alleged Absconding Date — The company marked the employee as absconded after his visit.``
     → Same date, same actor (company), same action (marked absconded), same
       target (Yogesh = "the employee"). DUPLICATE. Drop the NEW entry.

4. **KEEP genuinely distinct events** even when they share a date — multiple
   legal events can occur on the same day. Two entries on the same date are
   distinct when their descriptions reference different actors, actions, OR
   objects (e.g. "Bank notice issued" + "Reply filed" on the same day are two
   different events).

5. Other duplicate patterns to drop:
     - "01-04-2020 Initial Plot Booking" vs "01-04-2020 First booking made".
     - Same legal filing / hearing / deadline on the same or adjacent date
       phrased with different wording.
     - The same notice / order / payment described with title variations.

6. Cap at {max_keep} items total.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
    )


def llm_classify_factual_vs_procedural_prompt(*, entries, numbered):
    return (
f"""You are filtering a chronological list of dates extracted from a legal case file. Each entry has a date, a short title, and a one-sentence description.

Classify each entry as ONE of:
  - "factual"     : a real-world event that actually happened to or between
                    the parties (incident, accident, agreement signed, payment
                    made, marriage, separation, employment joining or quitting,
                    document handed over, transaction, communication exchanged,
                    medical examination, FIR registered as an event, site
                    inspection, possession delay, fraud transaction, etc.).
                    ALSO factual: a legal / demand / statutory notice sent,
                    received or replied to between the parties, and a
                    complaint, suit, petition or FIR that WAS filed on a past
                    date. These are the backbone of an Indian "List of Dates
                    and Events" (e.g. the s.138 NI Act notice and its receipt
                    start the limitation clock), and the Procedural Timeline
                    panel does NOT show them — dropping them here would leave
                    them on no panel at all.
                    Anything a lawyer would mention when narrating "the story
                    of what happened" to a colleague.
  - "procedural"  : ONLY what the separate Procedural Timeline panel shows —
                    a date set BY A COURT / TRIBUNAL / FORUM or BY LAW that
                    controls how the case moves: a hearing or listing date,
                    an adjournment / next date, a court-directed filing,
                    evidence or compliance deadline, a computed limitation or
                    response deadline, a scheduled future meeting or mediation
                    session. Also lawyer-side bookkeeping: lawyer or client
                    consultation, "case initiation", report due, period of
                    judicial inactivity, judge absent, document expiry as a
                    deadline.

Apply general semantic judgment — do NOT rely on specific keywords. The same
entry may be procedural in one case and factual in another:
  - "Visa Expiry" is FACTUAL only when the dispute is itself about visa
    overstay / immigration; otherwise it's procedural / metadata.
  - "Legal Notice Sent" and "Notice Received by Accused" are factual (an
    exchange between the parties); "Last Date to Pay Under the Notice" is
    procedural (a deadline computed by law).
  - "Complaint Filed Before the Magistrate" is factual (it happened);
    "Next Hearing Date" and "Written Statement Due" are procedural.
  - "Marriage" is factual; "Marriage Certificate Filed in Court" is
    procedural.

If the entry's title is a vague placeholder ("Date Mentioned", "Event",
"Case Date") with no concrete event content, classify as "procedural" so it
gets dropped.

Return ONLY this JSON, no commentary:
{{"classifications":["factual","procedural", ...]}}

The array MUST have EXACTLY {len(entries)} entries, in the same order as
the numbered list below.

Entries:
{numbered}
"""
    )


def merge_applicable_laws_with_previous_prompt(*, max_laws, numbered_new, numbered_prev):
    return (
f"""You are merging two versions of an APPLICABLE LAWS list from regenerations of the same Indian legal case.

Each entry is formatted as ``<Act> <Section> — <description>``.

PREVIOUS list (already shown to the user — must be preserved):
{numbered_prev}

NEW list (freshly regenerated — may contain rephrased duplicates of items already in PREVIOUS, or genuinely new applicable provisions):
{numbered_new}

YOUR TASK: produce the cumulative applicable-laws list using indexes.

RULES (apply strictly):
1. Include EVERY entry from PREVIOUS, in original order, as ``{{"source":"prev","index":N}}``.
2. After all PREVIOUS items, append entries from NEW as ``{{"source":"new","index":M}}`` ONLY when they refer to a DIFFERENT statutory provision than every PREVIOUS entry.
3. Treat as DUPLICATES (drop the NEW item, do NOT append):
     - Same Act + same Section number, even if description is rephrased
       ("BNS Section 85 — cruelty by husband" vs "Bharatiya Nyaya Sanhita Section 85 — wife abuse").
     - Different formal name for the same provision (e.g. "BNS Sec. 85" and "BNS s.85").
     - Description rephrasing of an already-included section.
4. KEEP genuinely distinct sections, even from the same Act ("BNS Section 85" and "BNS Section 318" are different provisions).
5. Cap at {max_laws} entries total. Drop the lowest-priority NEW entries first if needed.

Return ONLY this JSON, no commentary:
{{"items":[{{"source":"prev","index":1}},{{"source":"prev","index":2}},{{"source":"new","index":3}}]}}
"""
    )

