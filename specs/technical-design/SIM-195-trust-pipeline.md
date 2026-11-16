# SIM-195: Trust pipeline and contradiction engine — technical design

- **Requirements:** `specs/requirements/SIM-195-trust-pipeline.md`
- **Status:** implemented on the ticket branch, for review

## Shape
A new package, `app/trust/`, runs after ontology extraction in the same tracked
background task. Pure stages, one store, one orchestrator:

| Module | Stage | Pure? |
|---|---|---|
| `claims.py` | 2–3: modality gate, extraction entries → canonical claims | yes |
| `relevance.py` | 1: per-document relevance from chunk embeddings | yes |
| `corroboration.py` | 4: source groups, noisy-OR | yes |
| `conflicts.py` | 5: the contradiction engine | yes |
| `router.py` | 6: bands, review items, decisions — sole status writer | yes |
| `store.py` | evidence and derived records in Qdrant | no |
| `pipeline.py` | `run_trust_pass`, `recompute_workspace`, prompt blocks | no |

Facts, conflicts and review items are **derived**: every pass rebuilds them
from the case's full evidence set (Keel's "recomputed, never incrementally
bumped"). Only evidence rows, document profiles and review decisions are
stored inputs.

## Storage: `<collection>_trust_v1` (payload only, like the tables store)
`record` ∈:
- `evidence` — one per (document, chunk, claim): `fact_key`, the claim,
  `modality`, `extraction_confidence`, `span` (the passage text around the fact,
  ≤ 400 chars, for copy detection and display), `page`, `chunk_id`,
  `valid_from`/`valid_to`, `on_summary_page`, the graph node it came from
  (`node`: label + properties) for re-projection. Carries
  `original_document_id`, so `delete_documents` erases it by filter.
- `doc_profile` — one per document: mean chunk embedding and the last
  relevance score. Also carries `original_document_id`.
- `fact`, `conflict`, `review` — derived. Carry `document_ids` (every
  document involved); `delete_documents` also deletes derived records naming a
  deleted document, then the recompute hook rebuilds them.
- `decision` — a review resolution: keys and action only, no case text.

Point ids are uuid5 of the record identity, so a replay overwrites. Derived
records are replaced by writing the new set and deleting ids not in it.
`delete_workspace` clears the whole collection's workspace slice.

## Stage 1 — relevance (`relevance.py`)
Document vector = mean of its chunks' dense vectors (embedding-failed chunks
skipped). For each document, `s` = cosine to the centroid of the OTHER
documents; `mu`, `sd` = the same leave-one-out statistic over the other
documents. `score = min(1, 2·Φ((s−mu)/sd))`: an average or better document
scores 1.0, an outlier tends to 0. Below `TRUST_RELEVANCE_MIN_DOCS` (10)
documents every score is 1.0 (nothing to calibrate against; a raw cosine is
never used). Log-only: `TRUST_RELEVANCE_THRESHOLD` does not exist on purpose.

## Stage 2 — modality (`extraction.py` prompt + `claims.py`)
The extraction prompt asks for `modality` (asserted | alleged | hedged |
denied) and an optional `as_of` date on each node and edge, outside
`properties` (the graph schema is unchanged by them). `claims.py`:
hedged → dropped, counted; denied → claim with `denial=True`; missing → asserted.
Not written to the graph either: `pipeline.py` drops hedged and denied entries
from the graph write.

## Stage 3 — canonical claims (`claims.py`)
`claims_for_node(label, props)` → `Claim(subject_key, attribute, value_key,
value, kind)`:

| Label | Subject | Attributes (kind) |
|---|---|---|
| Party | canonical name | exists; party_role (role, never compared); is_natural_person (bool) |
| Event | description (+ date unless one-off) | event_date (date) |
| Hearing | hearing date | next_date (date) |
| Filing | filing type + filed_by (+ date unless FIR/plaint/…) | filed_on (date) |
| Order | order date + direction | complied (bool) |
| Obligation | type + owed_by + owed_to | status (status, time-varying); due_date (date) |
| Relief | relief type | quantum (amount); granted (bool) |
| Transaction | reference (else date+amount) | amount (amount); transaction_date (date) |
| Communication | medium + sender + recipient + date | exists |
| CaseReference | case number | year (int); forum (name); status (status, time-varying) |
| Precedent | citation | decision_date (date) |
| other labels | identity | exists |

Edges: subject = the tail's identity, attribute = edge type, value = the head's
identity (kind `relation`). `fact_key = sha1(subject|attribute|value_key)`.
Normalisation reuses `normalize_name`, `parse_date`, `parse_amount`.

## Stage 4 — corroboration (`corroboration.py`)
```
w(e)  = modality_weight[e.modality] × (floor + (1−floor) × relevance(doc))
         × (SUMMARY_PAGE_WEIGHT if e.on_summary_page else 1)
g     = max over the group's evidence of extraction_confidence × w
p     = 1 − ∏(1 − g)
final = p × (1 − penalty × has_unresolved_conflict)
```
Weights: asserted 0.95, alleged 0.85; floor `TRUST_RELEVANCE_FLOOR` 0.70.
Source groups (union-find over a claim's evidence): same document; or spans
whose 5-word-shingle Jaccard ≥ `TRUST_COPY_SIMILARITY` (0.80) — a verbatim
copy. A paper-book summary page (when `app.rag.page_kinds.avoid_pages` is
available) is weighted 0.5: it restates, it does not attest.

## Stage 5 — contradiction engine (`conflicts.py`)
Compares facts sharing `(subject, attribute)` across different source groups.
- **date**: disjoint ranges (precision-aware: "March 2025" vs "14 March 2025"
  is not a conflict).
- **amount**: relative difference > 1%.
- **status**: different status groups (discharged vs outstanding).
- **role**: not compared. Roles change across forums (respondent below,
  appellant above) and cross-complaints; it only feeds the counsel axis,
  per document.
- Recurring subjects (adjournments, instalments, notices, applications) are
  keyed by their date, so two dates are two occurrences; only one-off events
  (`claims._ONE_OFF_EVENT`) are compared on date. Free-text subjects merge
  only with equal numbers and the same date key.
- **bool / int / name**: different values.
- **relation**, single-valued edges only (`PAID_TO`, `FILED_BEFORE`,
  `FILED_BY`): one tail, two heads.
- **counsel**: object-side, curated: one advocate `REPRESENTED_BY` two parties
  whose roles are opposed.
- **denial**: a denial claim against an asserted claim with the same
  `fact_key` (or the same subject for `exists`).
- **succession**: for time-varying attributes (status, CaseReference forum),
  when both sides have `as_of` bounds that do not overlap, no conflict. A
  missing bound is unbounded, so it conflicts.
Conflict id = sha1 of the sorted fact pair, so rediscovery is idempotent.

## Stage 6 — routing (`router.py`)
`route(confidence, conflicted, decision) → status`; the only writer of
`Fact.status`. `shadow_band` is the formula's own band, kept beside `status`.
Decisions are keyed by id AND carry a hashed signature (axis, label,
attribute, subject wordings, values), so a decision is re-found when a new
document re-keys the facts; hashed because a decision has no document id for a
delete to reach. Canonical representatives are the shortest key, stable as
documents arrive.

Review items: `unresolved_conflict` for a fact in an unresolved conflict,
`low_confidence` for a fact in the contested band; one per fact; auto-closed by
construction (derived). Decisions (`resolve_review`): `keep_a`/`keep_b`
(other side dropped), `keep_both`, `dismissed` → conflict resolved;
`confirm_trusted` / `confirm_dropped` → status forced.

## Orchestration (`pipeline.py`)
`ontology.pipeline.ingest_to_knowledge_plane` → when trust is on:
1. the extraction entries (hedged dropped; denied kept for trust only);
2. `run_trust_pass(workspace_id, document_ids, chunks, entries)` under a
   per-workspace lock: replace these documents' evidence and profiles,
   `recompute_workspace`.
3. Graph: CONTRADICTS edges are written between distinct nodes with full
   identity maps (fixes the scalar-key bug); every touched node gets
   `trust_status`, `trust_confidence`, `source_count`.
Extraction runs when `ONTOLOGY_INGEST_ENABLED` or `TRUST_PIPELINE_ENABLED`; the
graph stays optional. Chunks are capped at `TRUST_MAX_CHUNKS_PER_DOCUMENT`
(500), the skip is logged.

A pass stores evidence only for documents the evidence store still holds
(`documents_present`): extraction takes minutes, and a document deleted
meanwhile had nothing to delete then. The delete hook re-deletes under the
trust lock, catching a pass that raced the delete. Every store write checks
`is_workspace_persistence_blocked`, before and inside its worker thread.

Deletion: `QdrantStore.delete_documents` deletes evidence/profile by
`original_document_id` and derived records by `document_ids`, then runs the
registered recompute hook (`set_trust_recompute`), which rebuilds the case and
re-projects surviving facts' nodes into the graph (the graph withdrawal removed
any node last written by the deleted document). `delete_workspace` clears the
collection slice; no recompute.

## Consumers
- `conflict_block(workspace_id)` — a prompt block of unresolved conflicts with
  both sources and pages. Added to `analysis_report` and `strategy_objective`
  context and to chat (`chat_context`), where each side's passage is brought
  into the pack so the model cites `[E#]` and the citation checker verifies it.
- Graph queries filter `trust_status = 'dropped'` and return `trust_status`.

## Schema
`schema.py`: optional `TRUST_PROPERTIES` (`trust_status`, `trust_confidence`,
`source_count`) on every node; `ONTOLOGY_VERSION` 1.3.0; DDL regenerated.

## Config (`app/core/config.py`)
`TRUST_PIPELINE_ENABLED` (false), `TRUST_BAND_TRUSTED` 0.85,
`TRUST_BAND_REVIEW` 0.50, `TRUST_CONFLICT_PENALTY` 0.35,
`TRUST_RELEVANCE_FLOOR` 0.70, `TRUST_RELEVANCE_MIN_DOCS` 10,
`TRUST_COPY_SIMILARITY` 0.80, `TRUST_MAX_CHUNKS_PER_DOCUMENT` 500,
`QDRANT_TRUST_COLLECTION` (optional).

## Logging
One summary line per pass: documents, evidence, facts per status, conflicts per
kind, dropped-hedged, denials. Never values.

## Risks
- Extraction cost: unchanged per chunk; trust adds no model calls.
- A case with thousands of claims: recompute is O(n) grouping plus pairwise
  within a `(subject, attribute)` bucket; buckets are small.
