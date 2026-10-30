---
title: Case laws and applicable laws
type: feature
repo: Pracsys-ai
owner: AI team
status: current
audience: everyone; engineers below "For engineers"
last_verified: 2026-10-21
jira: SIM-113
related_flows:
  - Evidence to analysis panels
other_side: {}
related_specs: []
---

# Case laws and applicable laws

## In plain words

Two panels tell the lawyer which law matters. Applicable laws lists up to 12
statute sections that bear on the case, framed for current Indian law (the
new criminal codes, not the old penal code). Each section is checked against
an official list of sections taken from India Code, so the panel can show
whether the section really exists, and shows its official title. Case laws
lists up to 12 court judgments. Every judgment must come from a real source
page; the service reads the judgment and checks that its citation actually
appears in it. A lawyer can add a judgment by citation, link or description,
or upload one as a document; the service only adds it if it can be confirmed
and, for uploads, if the document really is a judgment.

## Who uses it

- Lawyers researching the legal basis of a case.
- The backend, which stores both panels and relays manual additions.

The backend and lawyer-app sides of these panels are covered under
[Analysis panels](analysis-panels.md) and its linked pages.

## What the user sees

An Applicable Laws panel (act, section with official title, description,
relevance High / Medium / Low, link to the source) and a Case Laws panel
(cards with citation, court, binding strength, digest and a link to the full
judgment). "Add Case Law" and an upload action add to the Case Laws panel.

## How it works

Applicable laws:

1. Runs in the panel pipeline after the list of dates, and again on objective
   changes. Case signals are pulled from the documents (plus knowledge-graph
   evidence when enabled); the current objective biases the search query.
2. Indian Kanoon is searched directly. If that returns nothing and Vertex AI
   Search is configured, Vertex AI Search is used instead.
3. The model picks sections from the case signals and up to 8 search results.
4. Rows are normalised: IPC, CrPC and Indian Evidence Act 1872 rows are
   dropped, relevance is coerced to High / Medium / Low, rows missing act,
   section or description are dropped, and the list stops at 12 with ids
   `law_1` ... `law_12`. If nothing survives, a keyword heuristic fallback is used.
5. Each row is linked to a search result whose title names the same section
   number and an act word (`sourceUrl`, `sourceTitle`).
6. On normal updates the new list is merged with the previous one (still
   capped at 12); on an objective change it replaces it. Rows are tagged
   `isMatched` against the frontend's `currentState` by act plus section.
7. At emit, `annotate_law_rows` adds `registerStatus`, `statute`, `heading`
   and `sectionTitle` to every row on every path, edits included.

Case laws:

1. A legal-issue query (no party names) is derived from the case signals.
2. Sources are gathered from Indian Kanoon, Vertex AI Search (if configured)
   and Google Search grounding, de-duplicated by URL.
3. The model shortlists cases that map to a real source URL. Each judgment
   is then fetched and digested; `citationVerified` records whether the
   citation string appears in the fetched text. Cards that stay bare are dropped.
4. Binding strength is set from the court (Supreme Court binding, High Court
   binding within the state, commissions and tribunals persuasive).
5. Results merge with the previous panel by source URL (previous first),
   capped at 12, and are emitted as `case_laws`.
6. `add_case_law` (citation, URL or text): resolves a real source, runs the
   same selection pass, merges and re-emits. If nothing verifiable is found,
   the panel is re-emitted unchanged to clear the loader.
7. `case_law_document_request`: downloads the uploaded file, one model pass
   decides whether it is a judgment and extracts the digest, tries to
   resolve a public source URL, merges it into the panel, and always replies
   with `case_law_document_response` (`accepted` true or false with a reason).
8. `applicable_law_relevance_request`: rates a lawyer-chosen act and section
   as High, Medium, Low or not relevant to the case.

## Rules and limits

- No IPC rows in applicable laws, ever. Old-code references are mapped to
  BNS / BNSS / BSA in the prompt and dropped if they leak.
- `registerStatus` is `verified` (with the official heading), `invalid` (the
  act's full section list is known and the section is not in it, or the
  section is omitted), or `unverified` (unknown act, an Order / Rule /
  Article / Schedule reference, a superseded code, or a repealed act).
  Repeal is never `invalid`.
- Absence from the index means `invalid` only for an act that is in the
  index: 165 acts, 12,530 sections in `app/statutes/data/sections.json`.
- `section` is never rewritten; `sectionTitle` is added beside it.
- No case is added without a real source URL. An uploaded judgment with no
  public source keeps the upload link.
- Both lists are capped at 12.

## What can go wrong

| Situation | What the user sees | What the system does |
|---|---|---|
| Indian Kanoon unreachable or empty | Laws still appear, possibly without links | Falls back to Vertex AI Search; then the model works from case signals alone |
| Model returns malformed JSON for laws | A shorter heuristic list | Salvages an array if possible, else uses the heuristic fallback |
| Section does not exist in a known act | Row marked invalid | `check_provision` returns `invalid` |
| Manual case law cannot be verified | The panel stays as it was | Re-emits the unchanged panel to clear the loader |
| Uploaded file is not a judgment or unreadable | A rejection with a reason | Emits `accepted: false` with `rejectionReason` |
| No case-law sources found | An empty Case Laws panel | Emits an empty payload and logs a warning |

## For engineers

### Where the code is

- Applicable laws: `app/socket/handlers/applicable_laws.py`,
  `app/socket/handlers/law_relevance.py`, merge in
  `app/socket/handlers/objective_change.py` (`_merge_applicable_laws_with_previous`)
- Case laws: `app/socket/handlers/case_laws.py`, `app/socket/handlers/panel_edits.py` (`on_add_case_law`)
- Search: `app/search/case_law_finder.py` (Kanoon scrape, grounding,
  judgment fetch), `app/search/vertex_ai_search.py`
- Statute register: `app/statutes/register.py`, `app/statutes/resolver.py`,
  data in `app/statutes/data/`; rebuilt with `scripts/fetch_laws_indiacode.py`
  then `scripts/build_statute_index.py`
- Prompts: `app/prompts/applicable_acts.py`, `app/prompts/applicable_laws_prompts.py`,
  `app/prompts/case_laws_prompts.py`

### Events

- In: `add_case_law`, `case_law_document_request`, `applicable_law_relevance_request`.
- Out: `document_feature_response` (`applicable_laws`, `case_laws`,
  `applicable_law_relevance`), `case_law_document_response`,
  `applicable_law_relevance_response`.
- Full contract: [Backend <-> AI service socket events](../contracts/backend-ai-socket-events.md).

### Data

- Panel caches `_workspace_applicable_laws_payload` and
  `_workspace_case_laws_payload`, persisted to Qdrant.
- Vertex AI Search needs `GCP_PROJECT_ID` and `VERTEX_SEARCH_DATA_STORE_ID`;
  each query is billed as `searchQueries`.

### Security

- Uploaded case-law files are read through the chat-attachment extraction
  path and are not added to the evidence store.
- Search queries for case laws are derived to exclude party names.

### Tests that guard it

- `tests/test_statute_index.py` (`check_provision` against the India Code index)
- `tests/test_statutes.py` (register, legacy IPC / CrPC / Evidence Act mapping)
- `scripts/e2e_local_backend.py` (`make e2e`; billed, run by a human)

### Known gaps

- No automated test covers the IPC filter, the 12-row cap, `citationVerified`,
  `add_case_law` or `case_law_document_request`.
- The case-law merge keeps previous cases first and stops at 12, so a manual
  addition to a full panel appears to be dropped while the upload reply still
  says `accepted: true`. Read from the code; not reproduced.
- Uploaded judgments do not get a `citationVerified` check.
- An invalid `applicable_law_relevance_request` payload is logged whole, and
  `case_law_document_request` logs the document name; both break the logging rules.
- The applicable-laws model result is not checked with `is_generation_error()`;
  a failed call falls through to the heuristic list.

## History

| Date | Change | Spec / commit |
|---|---|---|
| 2026-10-21 | First version, written from the code | SIM-113 |
