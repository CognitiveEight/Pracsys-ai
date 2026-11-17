# SIM-195: Trust pipeline — acceptance criteria

All checks use fictional documents.

- **AC-1 Corroboration.** A fact asserted in two different documents has 2
  source groups and a higher confidence than the same fact from one document.
- **AC-2 Same-document evidence.** Two passages of one document stating a fact
  form one source group.
- **AC-3 Copies are not independent.** A passage copied verbatim from one
  document into another forms one source group.
- **AC-4 Date conflict.** Two documents dating the same event on disjoint dates
  produce one `date` conflict; both facts are `contested`. "March 2025" vs
  "14 March 2025" produces none.
- **AC-5 Amount conflict.** "Rs. 4,50,000" and "4.5 lakh" do not conflict;
  4.5 lakh vs 5 lakh does.
- **AC-6 Succession.** Obligation "outstanding" as of 2024-03-01 and "paid" as
  of 2024-06-01 in two documents produce no conflict; with no dates they do.
- **AC-7 Denial.** A payment asserted in one document and denied in another
  produces a `denial` conflict; the denial is never a fact.
- **AC-8 Hedged.** A hedged statement produces no evidence and no fact.
- **AC-9 Relation conflicts.** One transaction paid to two different parties
  conflicts; one advocate representing parties one document places on opposed
  sides conflicts; co-accused sharing counsel do not. A party's role differing
  across documents (respondent below, appellant above) is not a conflict.
- **AC-10 Routing.** Bands at 0.85 / 0.50; an unresolved conflict multiplies
  confidence by 0.65 and routes contested; exactly one pending review item per
  contested fact; a `keep_a` decision survives recompute and drops side b.
- **AC-11 Relevance.** Below 10 documents every score is 1.0; with a clear
  outlier among 12 documents the outlier scores below the others and an
  average document scores 1.0.
- **AC-12 Determinism.** Recomputing twice from the same evidence gives the
  same facts, conflicts and ids.
- **AC-13 Deletion.** Deleting a document removes its evidence; a fact also
  stated in another document survives with 1 source group; a conflict
  involving the deleted document disappears.
- **AC-14 Purge.** `delete_workspace` leaves no trust record for the
  workspace.
- **AC-15 Graph.** CONTRADICTS edges for composite-identity nodes are written
  with full identity maps; nodes receive `trust_status`; graph queries exclude
  dropped nodes.
- **AC-16 Prompt block.** The conflict block names both sides with document
  and page; chat brings both passages into the pack with `[E#]` labels.
- **AC-17 Off switch.** With `TRUST_PIPELINE_ENABLED=false`: no trust records,
  no `trust_status` on graph nodes, no chunk cap, no conflict block in any
  prompt. Deliberately unconditional (a fix to extraction itself, in the
  CHANGELOG): the prompt asks for `modality`/`as_of`, and hedged or denied
  statements are never written to the graph.
- **AC-20 Recurring events.** Two adjournments, instalments or notices on two
  dates are two events, not a date conflict; a one-off event (an agreement,
  an FIR) on two dates is. Subjects whose numbers differ ("Cheque No. 104512"
  / "104513") never merge.
- **AC-21 Decisions are durable.** A decision survives a new document that
  re-keys the facts (matched on a hashed signature) and holds no case text.
- **AC-22 Late and racing writes.** A trust pass for a document deleted during
  its extraction stores nothing; a pass racing a delete is cleaned up by the
  delete hook; nothing is written while the workspace purge is in progress.
- **AC-18 No leaks.** No trust log line contains a fact value or span.
- **AC-19 (manual).** On a real case with the pipeline on, the analysis report
  and chat mention a real cross-document conflict with both sources.
