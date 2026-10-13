# Agentic Ontology RAG — implemented foundations

Status of the work plan (`Agentic_Ontology_RAG_Workplan_v1.0.docx`) inside this
repository. This document covers what exists in code, what it does, and what is
deliberately not built yet.

## What is implemented

Three pieces, chosen because they are the plan's own week-one items and none of
them needs Keel deployed, Neo4j running, or SME input to *begin*.

| Work-plan task | Module | State |
|---|---|---|
| 1.1 Gemini provider adapter | `keel/backend/app/services/providers/gemini.py` | Complete — LLM + embeddings, in Keel |
| 2.1 Ontology schema | `app/ontology/schema.py` | Complete — 20 node types, 21 relationship types |
| 2.2 Graph schema migration | `app/ontology/neo4j_ddl.py` | Complete — Cypher for Enterprise and Community |
| 2.3 Ontology-constrained extraction | `app/ontology/extraction.py` | Complete — validation gate; needs an LLM at runtime |
| 2.4 Entity resolution | `app/ontology/entity_resolution.py` | Complete |
| 2.5 Statutory register | `app/statutes/register.py`, `data/register.json` | **Partial seed** — see below |
| 2.6 Legacy statute mapping | `app/statutes/resolver.py` | Complete mechanism, partial data |
| 2.8 Temporal normalisation | `app/ontology/temporal.py` | Complete |
| 2.9 Contradiction detection | `app/ontology/contradictions.py` | Complete — four detectors |
| 3.1 Retrieval planner | `app/retrieval/planner.py` | Complete |
| 3.2 Modality router | `app/retrieval/planner.py` (`route`) | Complete |
| 3.3 Graph query library | `app/retrieval/graph_queries.py` | Complete — 8 traversals, parameterised |
| 3.6 Evidence assembler | `app/retrieval/assembler.py` | Complete |
| 3.7 Coverage critic | `app/retrieval/critic.py` | Complete |
| 3.8 Claim verifier | `app/retrieval/verifier.py` | Complete |
| 3.9 Panel retrieval profiles | `app/retrieval/profiles.py` | Complete — all 8 panels |
| 3.10 Evaluation harness | `app/eval/` | Complete — real runner; needs golden matters |
| 2.2 Graph persistence | `app/graph/store.py` | Complete — Neo4j adapter |
| 2.11 Graph lifecycle | `app/graph/store.py`, `app/socket/handlers/workspace_events.py` | Complete — withdrawal wired into delete, restore and workspace deletion |
| 1.8 Cost bridge | `app/keel/client.py` (`record_keel_usage`) | Complete |
| 1.3/1.5 Ingest bridge | `app/ontology/pipeline.py` | Complete — behind `ONTOLOGY_INGEST_ENABLED` |
| — Keel REST client | `app/keel/client.py` | Complete — the two-plane integration surface |
| 4.x Panel cutover seam | `app/retrieval/panel_bridge.py` | Complete — per-panel flag |
| 5.2 Regression suite | `tests/` | 368 tests, the repo's first |

### The retrieval loop, end to end

Section 5.1's eight steps, and where each lives:

```
① PLAN      app/retrieval/planner.py      plan(panel) -> typed sub-questions
② ROUTE     app/retrieval/planner.py      route(q) -> graph | vector | lexical
③ RETRIEVE  app/graph/store.py (graph) · app/keel/client.py (vector)
④ ASSEMBLE  app/retrieval/assembler.py    dedupe, interleave, budget, provenance
⑤ CRITIQUE  app/retrieval/critic.py       coverage -> replan(bounded) or proceed
⑥ GENERATE  the existing panel agents — unchanged
⑦ VERIFY    app/retrieval/verifier.py     drop or demote unsupported claims
⑧ EMIT      the existing socket payloads — unchanged
```

```
make test            # the pytest suite
make eval            # run the harness over the golden set
make ontology-ddl    # generate data/ontology/schema.{enterprise,community}.cypher
make statute-report  # register coverage summary
```

## The ontology (Section 4)

`app/ontology/schema.py` is the single definition. The Neo4j constraints and
indexes are *generated* from it, so a property cannot drift from the constraint
protecting it.

Two invariants are enforced, in two places each — application-side by
`validate_node` / `validate_edge` for a legible error, and database-side by the
generated DDL for a guarantee:

* **Provenance.** Every node and relationship carries `source_document_id`,
  `chunk_id`, `page` and `extraction_confidence`. A fact without them is
  rejected at write time. This is what makes deletion safe: dependent facts are
  found by their provenance and withdraw with their source (task 2.11).
* **Workspace scoping.** Every node and relationship carries `workspace_id`, and
  every node type gets an index on it. The matter boundary is a property of the
  data, not of the query that happens to be asked.

Two modelling notes where Section 4's table was ambiguous:

* `OCCURRED_ON` is listed as pointing at "(date property)". It is kept in the
  ontology because the retrieval layer asks for it by name, but it is **not**
  emitted as a Neo4j relationship — temporal anchoring lives in `event_date` /
  `hearing_date` / `filed_on`. Reifying a Date node would fork the schema from
  the plan.
* `SUPERSEDES` connects "same type" nodes, so it is validated to reject
  `Order → Communication`.

Neo4j Community cannot enforce composite node keys or property existence.
`generate_schema_statements("community")` emits what it can and lists the rest
under `NOT ENFORCED BY THIS EDITION`, so the gap is visible rather than assumed.

## The statutory register (Sections 4.3, 2.5, 2.6)

**The shipped register is a partial seed and says so in its own `status` field.**
It contains only provisions verified at authoring time:

| Act | Sections in register | Total | Coverage |
|---|---|---|---|
| BNS 2023 | 38 | 358 | 11% |
| BNSS 2023 | 20 | 531 | 4% |
| BSA 2023 | 9 | 170 | 5% |
| IT Act 2000 | 21 | — | — |

Plus 78 legacy mappings (IPC 48, CrPC 21, Evidence Act 9). Completing it is task
2.5 and needs SME sign-off; `STATUTE_REGISTER_PATH` points at a completed file
without a code change.

The resolver implements Section 4.3's three rules, and the distinction between
its outcomes is the point:

| Outcome | Meaning | Emittable |
|---|---|---|
| `resolved` | Section exists in the register | yes |
| `mapped` | Legacy citation carried forward (IPC 420 → BNS 318) | yes |
| `unverified` | Act known, section not in the **partial** register — unknown, **not** invalid | no |
| `unmapped_legacy` | Superseded citation with no mapping; substance cannot be carried forward | no |
| `invalid` | Malformed token, or past the act's known section count | no |

`unverified` versus `invalid` matters: because the register is incomplete,
"absent" must never be reported as "does not exist". Only a section number past
an act's known total is asserted impossible.

The loader validates on load and **fails closed** — a missing or inconsistent
register raises rather than resolving to empty, because an empty register would
silently mark every real citation unverified. One of its checks is that every
legacy mapping targets a section that actually exists in the successor act,
which is what catches an error in the mapping table itself.

### Change of behaviour versus today

Today `_normalize_applicable_laws_payload` **deletes** any row mentioning IPC,
CrPC or the 1872 Evidence Act, so a document citing "IPC 420" contributes
nothing. Mapping it to BNS 318 keeps the legal substance while still honouring
the rule that IPC must not appear in output.

That wiring is supplied as `patches/0001-wire-statute-register-into-applicable-laws.patch`
rather than applied, because `app/socket/handlers/applicable_laws.py` is owned by
`root` in this working tree and is not writable. To apply:

```bash
sudo chown "$USER" app/socket/handlers/applicable_laws.py   # 97 files in the tree are root-owned
git apply patches/0001-wire-statute-register-into-applicable-laws.patch
```

It is gated by `STATUTE_RESOLVER_MODE`, defaulting to `off`:

* `off` — resolver not consulted. Current behaviour, unchanged.
* `report` — resolve and **log** what would change; emit unchanged. Run here
  first: this is the dual-run that establishes the reference rate for acceptance
  metric 10.1 without moving production.
* `enforce` — map legacy citations forward, drop unresolvable ones.

Note that a row reaching the resolver has already survived the legacy regex
filter, so in `enforce` mode mapping only fires for legacy citations that filter
missed. Removing the regex filter so the resolver owns legacy handling end to
end is a separate, flagged change.

## The evaluation harness (Section 10, task 3.10)

Section 13 calls the golden set and the reference measurement *"the single most
important early action"*; Section 9 lists a missing reference measurement as the
highest-priority risk. The harness exists so that measurement can be captured
against the platform **as it is today**, before anything is re-platformed.

Six Section 10.1 metrics are implemented in `app/eval/metrics.py`, each
returning a score, the target, pass/fail and the failing items:

| Metric | Target | Needs SME ground truth |
|---|---|---|
| `retrieval_recall` | ≥ 0.90 | yes |
| `claim_traceability` | ≥ 0.95 | **no** |
| `unresolvable_citations` | < 0.02 | **no** |
| `entity_resolution_precision` | ≥ 0.90 | yes |
| `chronology_completeness` | ≥ 0.90 | yes |
| `content_reachability` | 1.00 | no |

Two are self-contained and can be measured today: claim traceability scores
output against its own retrieval (a citation to a chunk that was never retrieved
counts as fabricated, which is worse than no citation), and unresolvable
citations scores against the register.

Entity resolution reports **merges** and **splits** separately. A merge — two
real parties collapsed into one node — is the more dangerous failure in a legal
setting and needs a different fix from a split.

`diff_reports()` mechanises the Phase 4 cutover gate: it returns `pass` only
when something improved and nothing regressed, with a tolerance so sampling
noise is not read as a regression. `unresolvable_citations` is handled as a
lower-is-better metric.

### What the harness still needs

* **A real runner.** The shipped `stub_runner` returns nothing and scores honest
  zeros; it does not synthesise plausible output. Wiring it to today's panel
  path is what turns the harness into the reference measurement.
* **Golden matters.** `app/eval/golden/example_matter.json` is a synthetic
  worked example demonstrating the format, clearly marked, and must not be
  counted in any reported measurement. Real matters come from the SME under a
  confidentiality arrangement (next step 4).

## The Gemini adapter (task 1.1, in Keel)

`keel/backend/app/services/providers/gemini.py` adds Gemini as a live LLM **and**
embedding provider, so the deployment stays on its existing Google model
relationship instead of adding a second AI vendor for the knowledge plane. It
follows the Groq/DeepSeek idiom — Google's OpenAI-compatible endpoint via the
OpenAI SDK — so it adds **no new dependency**.

Wired through every place Keel requires: the `get_llm_provider` /
`get_embedding_provider` factories, the `llm_provider` / `embedding_provider`
`Literal`s, a `gemini_api_key` `SecretStr`, the provider catalogue (without a
catalogue entry a gemini workspace could not select a chat model), `.env.example`
and `docs/environment.md`. Lints clean under Keel's own ruff rules and has 16
tests in `keel/backend/tests/services/test_llm_providers_gemini.py`.

Two behaviours worth knowing:

* **Embeddings are single-input.** Gemini's compatible route takes one string
  per request, so `embed_batch` fans out concurrently (bounded at 8 in flight)
  and reassembles in input order.
* **Token usage may be absent.** The compatibility layer sometimes omits
  `usage` on embeddings; the adapter reports 0 rather than estimating, so the
  cost log understates rather than invents.

`gemini-embedding-001` is 3072 dims against OpenAI's 1536 — switching after
indexing requires a full re-embed.

## Running it

Everything is off by default. Each backend is opted into independently, and a
partial rollout is a supported state rather than an error.

```bash
# Graph only — extraction persists, retrieval traverses
NEO4J_URL=bolt://localhost:7687
NEO4J_PASSWORD=...
NEO4J_EDITION=community          # or enterprise
ONTOLOGY_INGEST_ENABLED=true     # adds a per-chunk LLM call to every ingest

# Keel as well — vector/lexical retrieval and indexing
KEEL_BASE_URL=http://localhost:8000
KEEL_API_KEY=...
KEEL_WORKSPACES=legal
```

With `NEO4J_URL` set, startup applies the generated ontology migration
(idempotent — every statement is `IF NOT EXISTS`) and **fails hard if the graph
is unreachable**. A configured-but-broken graph silently degrading to "no graph"
would make every panel that reads it quietly wrong.

With `NEO4J_URL` unset, the ontology and retrieval layers still import and run;
they simply have nowhere to persist to, and every report says so.

`make eval` uses whatever is configured and labels the report with it
(`baseline[graph+keel]`), so a zero is never ambiguous between "broken" and
"not wired".

## Not built

* **Phase 0 entirely** — host provisioning, TLS, secret management, backup
  rehearsal, monitoring. Infrastructure, not code.
* **Phase 4 cutover: 6 of 8 panels are live in this checkout.** The shipped
  default for `RETRIEVAL_PANELS` is still empty, so a fresh deployment runs
  every panel on its existing path. The local `.env` sets
  `RETRIEVAL_PANELS=list_of_dates,applicable_laws,case_laws,strategy_objective,
  analysis_report,intelligence_chat` with
  `RETRIEVAL_MODE=augment` — augment only *adds* ranked evidence to the prompt,
  so it cannot make a panel worse. Note both are enabled **without** a baseline
  measurement, so an improvement cannot yet be distinguished from a regression.

  `patches/0002-panel-cutover-seams.patch` has been applied — `git apply`
  recreated the root-owned handlers inside directories the user owns, so no
  `chown` was needed. `relevant_parties` and `procedural_timeline` are
  **deliberately not seamed**: both are per-document extraction pipelines with
  no whole-corpus prompt to augment, and the only quick injection point — a
  synthetic "graph facts" pseudo-document — would make extracted entries cite it
  as their source, corrupting the `sourceDocIds` provenance contract the
  removed-evidence ledger depends on. They need per-panel design, not a seam.

  `drafting_assistant` is excluded by request and has no retrieval profile, so
  `retrieve_for_panel` refuses it even if it is named in the flag.

  The seam adds ranked, cited evidence to a panel's **prompt** and can run its
  output through the claim verifier. It never changes the emitted payload shape,
  so the socket contract holds, and rollback is removing a name from a list.
* **The baseline reference measurement.** `RetrievalRunner` measures the *new*
  path. Measuring the *old* one needs an integration harness that drives a live
  backend and captures emitted socket payloads —
  `app.eval.runners.BaselineRunnerNotWired` says so explicitly rather than
  standing in with a stub.
* **Golden matters.** Still only the synthetic worked example. Real matters come
  from the SME (next step 4), and without them the quality gates have nothing to
  gate on.
* **Statutory register completeness** — 11% of BNS. SME work.
* **Keel job-status tracking driving the emit.** `wait_for_job` exists; the
  handler submits and does not currently wait, because the existing
  `document_assessments` emit is driven by this service's own processing.
  Reconciling the two (task 1.4) is a behavioural change to a live path.
* **Graph inspection tooling (2.10)**, backfill (5.1), load testing (5.3),
  failure drills (5.4).
* **Chunking is a stopgap.** `pipeline.chunk_text` uses overlapping character
  windows. Docling's structure-aware chunking replaces it once ingestion runs
  through Keel; until then extraction needs *some* chunking and this one does
  not pretend to respect clause boundaries.

## Verified against a real Neo4j

`make smoke-graph` was run against Neo4j 5.26.29 Community — **11/11 checks
passed**, with 20 constraints and 119 indexes created:

| Check | Result |
|---|---|
| Real server accepts the generated migration | 117 statements applied, 0 failed |
| Facts written | 3/3, no rejections |
| Replayed ingest does not duplicate | 1 Party node after two ingests |
| Traversals return provenance | yes |
| IPC 420 stored as BNS 318 | yes |
| Coverage gate held on thin evidence | corpus retained, as designed |
| `withdraw_document` is exact | 3 removed, 0 left behind |

The run surfaced one real bug the fakes could not: the chronology query used the
bare `CALL { ... }` subquery form, **deprecated since Neo4j 5.23 and removed in
Neo4j 6**. A 5.26 server warned on every call. Now `CALL () { ... }` — the empty
scope is correct, since the subquery imports no variables, only the
`$workspace_id` parameter. Re-run: zero deprecation warnings.

Note Community created 20 constraints where Enterprise would create 215 — the
composite node keys and property-existence constraints are Enterprise-only and
are enforced application-side instead, exactly as the generator reports.

## A bug the integration test found

Entity resolution ran per extraction pass, so two spellings of one party
arriving in **separate ingests** never met — "M/s Meridian Traders" uploaded
today became a second node beside the "Meridian Traders Private Limited" created
last week. That is the split failure the module exists to prevent, and every
unit test missed it because they all resolved within one batch.

Fixed with `reconcile_with_existing` plus an `existing_entity_names` graph read
before every write. Existing names win — re-canonicalising an entity would
orphan the facts already attached to it — so the first name is kept even when a
fuller form arrives later. Ambiguity is still refused rather than guessed, and a
failed reconciliation read falls back to writing unreconciled rather than losing
the facts.

## A pre-existing issue found along the way

`app/core/lifecycle.py` cannot be imported on its own — there is a circular
import (`app.processing` → `app.vectorstore` → `app.workspace` → `app.socket` →
`app.agents` → `app.vectorstore.memory`). It works in production only because
`app/main.py` happens to import in an order that breaks the cycle. This predates
this work (the unmodified file fails the same way) and was left alone: untangling
it touches six packages and is its own change.
