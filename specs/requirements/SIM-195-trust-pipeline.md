# SIM-195: Trust pipeline and contradiction engine — requirements

- **Jira:** SIM-195
- **Source design:** Keel "Inside the trust pipeline" (engineering reference),
  adapted to this service.

## Problem
The knowledge-graph extraction (`app/ontology/`) writes each fact as a graph
node and stops there.
- The same fact in two documents is one node whose provenance is overwritten by
  the last writer: it is never corroborated, and deleting that last document
  deletes the fact although another document still states it.
- Contradiction detection compares facts inside one ingest batch only. Its
  date, amount and status findings are never written: the CONTRADICTS edge is
  addressed with a single value on nodes whose identity is composite, so the
  writer rejects it.
- Hedged ("may have paid") and denied ("never received") statements become
  facts.
- A fact's only confidence is the extractor's own self-report, and nothing marks
  a fact as disputed.

## Users and outcome
A lawyer asking "what is the agreement date?" or "do the documents contradict
each other?" gets facts that are:
- **trusted** when independent documents support them and nothing contradicts
  them — stated as fact, with the source;
- **contested** when documents disagree or support is thin — stated with both
  sides and both sources;
- **dropped** when the support is too weak — never stated.

## Functional requirements
1. **Relevance (stage 1).** Every document gets a 0–1 score for how well it
   fits the case, calibrated against the case's other documents. Log-only: no
   document is ever rejected for it. It discounts, never zeroes, the weight of
   that document's evidence.
2. **Modality (stage 2).** Each extracted fact carries a modality: asserted,
   alleged, hedged or denied. Hedged facts are discarded. Denied facts are kept
   only as denials: compared against assertions, never served as facts.
3. **Canonical claims (stage 3).** Facts are reduced to
   `(subject, attribute, value)` claims with canonical names, dates (with
   precision), amounts in rupees (lakh/crore) and status groups, so two
   wordings of one fact land on one claim.
4. **Corroboration (stage 4).** Confidence is recomputed from all evidence of a
   claim on every change: per independent source group the best evidence
   counts, groups combine by noisy-OR. Not independent: two passages of one
   document; a passage copied verbatim into another document (a petition
   quoting its annexure, a .docx and its PDF); a paper-book index, list of dates
   or synopsis page.
5. **Contradictions (stage 5)**, across the whole case:
   - attribute conflicts: one event on two disjoint dates, one claim at two
     amounts, one obligation both discharged and outstanding, one party in
     opposed roles, one case number with two years or forums, one order both
     complied with and not;
   - relation conflicts: one payment to two different parties, one case before
     two forums, one advocate representing opposed parties;
   - denial conflicts: a fact asserted by one document and denied by another;
   - succession is not conflict: a status or value stated "as of" two
     non-overlapping dates (outstanding in March, paid in June) is a change over
     time. A missing date is treated as unbounded.
   - Only facts from different source groups conflict.
6. **Routing (stage 6).** confidence ≥ 0.85 → trusted, < 0.50 → dropped,
   otherwise or with an unresolved conflict → contested (confidence penalised
   ×0.65). When a newer document contradicts an existing fact, both become
   contested. One function writes a fact's status. Each contested fact has
   exactly one open review item; a resolved review decision survives
   recomputation.
7. **Use.** Chat and the analysis report / strategy prompts get the case's
   conflicts with both sources. Graph facts carry `trust_status`; dropped facts
   are excluded from graph retrieval.
8. **Deletion.** A deleted document's evidence is erased and the case
   recomputed; a fact stated elsewhere survives. The workspace purge erases
   every trust record.
9. **Off by default.** `TRUST_PIPELINE_ENABLED=false` changes nothing.

## Non-functional
- No new socket payload or event. Review-queue events are a follow-up.
- Never log document text, names or values: counts only.
- The pass never fails the socket flow, is cancelled by a stop, and its model
  calls are billed to the case.

## Out of scope
Review-queue socket events and UI; workspace-level threshold overrides; Keel's
predicate registry (the ontology fixes the relation types).
