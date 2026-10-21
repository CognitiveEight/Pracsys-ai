---
title: Nothing is cited unless it was checked
type: decision
repo: law-firm-simplertoday-ai
owner: AI team
status: accepted
audience: everyone; engineers below "What this means"
last_verified: 2026-10-21
jira: SIM-113
number: DR-AI-0003
date: 2026-10-01
decided_by: AI team lead (role)
---

# DR-AI-0003 Nothing is cited unless it was checked

## In plain words

Lawyers rely on the AI's references to find the source of a fact. A reference
that points to the wrong page, or a figure that is not in the document, is
worse than no reference at all. We decided that the service never trusts the
AI model to cite correctly. In chat, every reference the model gives is checked
against the passage it points to. Made-up references are removed, numbers are
checked against the source, and a number found nowhere is flagged in the reply,
which is then marked as low confidence. In the case panels, the model is not
asked to cite at all. The service finds the source itself afterwards, copies
the quote word for word from the stored document and works out the page. If it
cannot find a good source, it shows no reference rather than a weak one.

## Why we needed to decide

- Chat replies had no citations, and panel items carried page numbers the model
  had guessed. A list-of-dates entry could point at a document that does not
  contain the date.
- Asking ten different prompts to cite, and trusting each to comply, is
  fragile and hard to test.
- The backend's own rule is to check every AI reply before trusting it
  (backend DR-0013,
  https://simplertoday.atlassian.net/wiki/spaces/PRACENG/pages/4227166). This
  is the AI service's half of that.

## Options we considered

| Option | Pros | Cons |
|---|---|---|
| Ask the model to cite and pass its citations through | Simple | Invented labels, guessed pages and unsupported figures reach the lawyer |
| Ask the model to cite, then verify and repair in code | Catches invented labels and wrong figures; model still chooses the passage | Needs labelled evidence packs and a checker |
| Attribute deterministically after generation, no model citation | Quote is always verbatim; page is where the quote is | Only works where items can be matched to passages (panels) |

## What we decided and why

Both checked approaches, each where it fits:

- **Chat** (`app/rag/chat_context.py`, `app/rag/citations.py`): evidence is
  given to the model as labelled passages (`[E#]` documents, `[C#]` case laws,
  `[S#]` statutes, `[T#]` table calculations). `verify_and_number` removes
  labels not in the pack, checks every figure in a cited sentence against the
  cited passage, re-points the citation if the figure is in another passage,
  reports figures found nowhere, and renumbers to `[1]`, `[2]`. One repair pass
  follows (`CITATION_REPAIR_ENABLED`, default on). A figure still unsupported
  is flagged inline and the governance validator sets confidence to `LOW`
  (`app/langgraph/chat_nodes.py`). `ai_response.citations` keeps only
  citations whose marker is still in the text.
- **Panels** (`app/rag/attribution.py`, `attach_panel_citations`): run at the
  one emit chokepoint, `_emit_document_feature` in
  `app/socket/handlers/chat.py`. The quote is copied from the stored passage,
  the page comes from the `--- Page N ---` marker before the quote, and a dated
  entry whose date is not in its claimed document is re-pointed. No good match,
  no citation. All added fields are additive; an item's type never changes.
- **Laws and case laws:** each law row gets `registerStatus` and `sectionTitle`
  from the statute register; case laws get `citationVerified` (citation found
  in the fetched judgment).

## What this means

- Any new evidence source must enter the pack with a label so the checker can
  verify it (graph facts carry their source `[E#]` label).
- Any new panel goes through `_emit_document_feature` so it is attributed.
- Measure changes with `scripts/eval_rag.py` (citation precision, coverage,
  unsupported rate).
- Guarded by `tests/offline/verify_rag.py` (needs a local Qdrant),
  `tests/test_chat_state.py`, `tests/test_tables.py`, `tests/test_statutes.py`.
- Socket payload fields are documented in
  [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-01 | Accurate retrieval with verified citations (chat checker, panel attribution) | 6bb3cbc |
| 2026-10-05 | Spreadsheet citations name sheet and row | a765e3a |
| 2026-10-21 | First version, written from the code | SIM-113 |
