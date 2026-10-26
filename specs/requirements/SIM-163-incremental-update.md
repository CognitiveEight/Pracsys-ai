# SIM-163: Adding a document to an existing case rebuilds every panel from scratch and overwrites the chat-set objective

- **Jira:** SIM-163 (Bug, priority High, label `performance`; no parent epic, no links)
- **Owner:** product owner (SimplerToday)
- **Status:** draft
- **Target branch flow:** fix/SIM-163-incremental-update → development → staging → main
  (three PRs from this branch line, in order: Phase -1 stuck loaders, then
  Phase 0 + Phase 1, then Phase 2)

## Problem
Four things hurt lawyers today, in this order of priority.

0. **Panels spin forever after an upload or an objective change.**
   - `workspace:refresh` arms the panels, but sometimes `list_of_dates`,
     `procedural_timeline` ("upcoming dates") or `analysis_report` never
     arrive. The spinner runs until the page is reloaded.
   - Once it happens, every later upload, objective change, reconnect replay
     and "Finish update" for that case also hangs, until the service restarts.
   - Separately, uploading files that all fail to read can **wipe** an existing
     case's Analysis Report and List of Dates.
1. **A follow-up upload of 1-5 PDFs is slow.** Adding one or a few documents to
   an existing case takes as long as the first build of the case, or longer.
   Every panel spins for minutes although only a handful of documents are new.
   Uploading again while that runs queues a second full rebuild behind the
   first. This is where lawyers feel the lag most.
2. **Changing the strategy or objective in intelligence chat is slow.** The
   chat acknowledgement waits on a full re-read of the case, and then six
   panels refresh one after another. Analysis, applicable laws, case laws and
   drafting are each regenerated in turn.
3. **The update silently undoes the lawyer's own work.**
   - A strategic objective declared in chat is replaced by one the model
     re-derives from the documents. The Analysis Report and its score
     explanation then follow the wrong objective.
   - Missing-information items the lawyer added themselves disappear.
   - Approved items survive only if the model happens to regenerate them.

At scale there is a correctness problem as well. Lawyers routinely upload 30-40
PDFs at once, and sometimes 200-300. At that size the List of Dates squeezes
every document into one prompt of about 200K characters: at 300 documents each
gets about 660 characters, so most dates are silently lost. The Procedural
Timeline reads only the first 12,000 characters of each document.

Production runs on **one GCP e2-highmem-2 (2 vCPU, 16 GB RAM)**. CPU-heavy
document work (page rendering, redaction, chunking) competes with the event
loop that serves chat, so concurrency must be bounded for that machine.

## Goal
- **(0) Ships first:** every loader armed by an upload or an objective change
  always gets a terminal panel. No external round trip can hold the case's
  pipeline lock forever. A failed upload never replaces an existing case's
  panels.
- **(a)** A 1-5 PDF upload to an existing case refreshes the panels from the
  case's current state in a fraction of today's time:
  - the objective in force (including one set in chat), curated items and
    cached panels are kept;
  - only the new documents are read;
  - panels still appear in the fixed product order.
- **(b)** An objective change from chat lands and refreshes its panels quickly:
  - the panels it changes are regenerated concurrently, reusing everything the
    objective does not affect;
  - the re-frame guards and the "objective lands regardless" rule stay.
- **(c)** No document's dates are dropped, whatever the case size. A first
  build of a 30-300 PDF case may stay slow; that is accepted.
- The service stays responsive to chat on a 2-vCPU machine during uploads.

## Scope
- In:
  - **Phase -1, stuck loaders (first PR, ships before everything else):**
    - template sync for the Drafting Assistant (`ensure_templates_synced` in
      `app/socket/handlers/template_sync.py`): the wait is bounded, an
      undelivered request counts as "no sync", and the pipeline lock is never
      held across the backend round trip;
    - the template-sync wait is bounded at 15 minutes (user decision). A
      request that could not be sent counts as "no sync" immediately;
    - a hard cap on how long one upload pipeline or objective-change cascade
      may hold the case's pipeline lock (user decision): 15 minutes, extended
      when the upload contains audio by the audio's duration, up to a
      maximum. At the cap, the run is stopped, logged as CRITICAL with its
      stage, every panel it armed is settled from cache, and queued work,
      reconnect replay and resume proceed;
    - an informational watchdog log when a case's pipeline lock has been held
      for 5 minutes, naming the holder and the stage;
    - every upload-tail step that fails emits its cached (or empty) panel as
      its terminal state;
    - a cancelled upload pipeline releases the loaders it armed (except when
      superseded);
    - a failed strategy step emits the cached strategy instead of a raw
      `{error}` message, and a strategy step that returns nothing is settled
      the same way;
    - after a `process_batch` crash, every armed loader is released from
      cache;
    - when every file in an upload to an existing case fails, the case's
      panels are re-emitted unchanged, not replaced or wiped;
    - Phase 0's per-stage timing (below) is included, so a slow step can be
      told apart from a hang.
  - **Phase 0, measurement.** Stage timing lines for the upload pipeline, the
    chat objective change and its cascade. An event-loop lag line.
  - **Phase 1: upload path (a), no change to panel content except where it was
    being lost:**
    - a newer document update supersedes a stale document pipeline without
      losing its documents;
    - panels are generated concurrently and still **emitted in product order**;
    - the case-entity extraction is cached by document set, not prompt text;
    - one document scroll per update;
    - the `currentState` matching passes run together;
    - the workspace-metadata call is moved off the critical path;
    - per-document extraction for List of Dates and Procedural Timeline,
      cached per document, merged proportionally to the new documents. An
      upload reads only its new documents, and nothing is front-cut;
    - incremental date prompts number a new document by its real annexure
      position;
    - the effectively unreachable "skip re-process" branch is removed;
    - lit-loader and cancellation gaps on the touched paths are fixed.
  - **Phase 1: objective change (b):**
    - the cascade regenerates analysis, list-of-dates relevance, applicable
      laws and case laws concurrently, with emits in product order and
      drafting last;
    - caches are reused: entities, document scroll, laws/case-laws signals;
    - an objective change arriving during an upload no longer waits for the
      upload to regenerate panels the cascade will replace anyway;
    - log leaks in the objective path are fixed, and the unchecked re-ground
      result is checked.
  - **Phase 1: running on 2 vCPU:**
    - bounded concurrency with simple, process-wide semaphores: documents in
      processing, CPU-bound work (about 2 threads in total) and background
      per-document model fan-outs;
    - document extraction gets its own thread pool, so it cannot exhaust the
      default executor that Qdrant calls use;
    - no more rendered page images in memory than CPU workers;
    - faster OCR page rendering: one parse per document.
  - **Phase 2: objective safety and delta for small uploads:**
    - the objective in force is persisted and never silently replaced by an
      upload, full rebuild included;
    - delta mode for Strategy and the Analysis Report on an upload of 1-5
      documents, with the existing fact guards. Larger uploads take the full
      rebuild, which may stay slow;
    - lawyer-added and approved Missing Information items survive an upload;
    - an objective change landing before or during an upload becomes the base.
- Out of scope, follow-up ticket (first-build throughput or large-upload
  quality only):
  - Progressive `document_assessments` emits during a large upload.
  - Quota-aware limiters beyond simple semaphores: a process-wide Vision
    limit, router-level interactive/bulk lanes, per-workspace fairness caps, an
    Indian Kanoon concurrency limit.
  - Map-reduce (per-document digests, hierarchical reduce) for Strategy and
    the Analysis Report on large cases. Their per-document starvation at 30+
    documents is documented but not fixed here.
  - Delta folds for uploads larger than 5 documents.
  - Starting per-document work while the rest of a large batch is still
    ingesting.
  - Caching Indian Kanoon / Vertex search results.
  - Applicable-laws search signals from every document (today: the first 20).
- Out (not doing at all):
  - Any socket payload, `featureType` or schema change.
  - Replacing the PyPDF2 text layer (an output change; not the bottleneck).
  - Removing `currentState` from the `workspace_sync` replay fingerprint (no
    gain; see design).
  - Splitting the objective change's single coalesced `strategy_objective` emit.
  - The LangGraph `strategy_agent.py` path (not used by sockets).
  - Restoring approved items on the full-rebuild path (open question 6).

## Users and surfaces
- Inbound:
  - `workspace_sync` (upload, the main path);
  - `user_message_request` (objective change, role reassignment);
  - `workspace_created` (bounded concurrency, per-document dates);
  - `workspace_version_restore` and `document_reload_request` (supersede only).
- Outbound: `document_feature_response` for `workspace_metadata`,
  `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`,
  `procedural_timeline`, `relevant_parties`, `case_laws`, `drafting_assistant`;
  `workspace:refresh` (arming unchanged); `ai_response` (objective ack). Shapes
  unchanged.

## Constraints
- [ ] Changes a socket payload or `featureType` — no shape change. Emit
      **timing** changes; emit order kept.
- [x] Role-dependent behaviour — delta mode only for the same validated role;
      a role change takes the full path; role still resolved by
      `_resolve_workspace_role`.
- [x] Touches personal data, PII redaction or the workspace purge:
  - **new** persisted per-workspace stores hold case content: the objective in
    force and the per-document dated extractions;
  - both must be reached by the existing purge; the purge code is not changed;
  - PII redaction moves to a worker thread with identical behaviour.
- [x] Adds a paid call (model, OCR, STT, search):
  - per-document date and timeline calls on upload, a narrower merge call and
    delta prompts;
  - paid calls on an upload scale with the new documents;
  - concurrency changes **how** costs reach each scope (copied contexts);
    totals and per-workspace attribution must equal a sequential run.
- [x] Indian-law output (BNS/BNSS/BSA; no IPC in `applicable_laws`) — laws and
      case laws now run concurrently in the cascade; their generation and
      normalisation are unchanged.
- [x] Shared workspaces: a `view` collaborator must not be able to mutate — the
      objective store is written only on chat paths already gated by
      `mutations_allowed`.

Also in force (CLAUDE.md):
- every armed loader is paired with a terminal emit;
- Stop is a fence: detached work goes through `_track_background_task`, and
  emits go through `self._socket_client`;
- removed-evidence gaps still appear;
- re-frame guards (`find_dropped_case_figures`, `find_ungrounded_proper_nouns`,
  mandated opening) apply to every delta and re-frame;
- the new objective lands regardless of the re-frame;
- logs carry sizes and counts, never content.

## Success measure
- **(0) Stuck loaders:**
  - no permanently held pipeline lock: every hold ends by the cap. Few
    `pipeline run exceeded cap` CRITICAL lines in production logs over two
    weeks after release, each with a stage name to act on;
  - every `workspace:refresh` arming is followed by a terminal
    `result <featureType> delivered=` line for each armed feature in the same
    `request_id` (sampled from logs);
  - no user reports of a permanent spinner.

All targets below are p50, measured from `request_id`-filtered stage lines on a
fictional 15-document case on an e2-highmem-2 via `make run`. They are proposed
and are confirmed or adjusted after the Phase 0 baseline (open question 1).
- **(a) Follow-up upload**, `workspace_sync` received → last panel's terminal
  emit:

  | Upload | After Phase 1 | After Phase 2 |
  |---|---|---|
  | 1 text PDF (≤30 pages) | ≤ 50% of today's time | ≤ 90 s |
  | 5 text PDFs | — | ≤ 150 s |
  | Each scanned page | — | adds ≤ 3 s |

  `document_assessments` arrives within 30 s for one text PDF.
- **(b) Objective change:**
  - lawyer's confirmation → `strategy_objective` emit ≤ 75 s (CLAUDE.md
    records ~2 minutes today);
  - `strategy_objective` → every cascade panel terminal ≤ 90 s;
  - with an upload in flight, the cascade starts within 15 s of the upload's
    strategy step finishing.
- **Responsiveness:** during a 5-PDF scanned upload, the event-loop lag line
  stays under 250 ms and a chat reply's time is within 20% of an idle reply.
- **Merge:** a 1-document upload whose dates do not collide with existing ones
  makes zero dated-merge model calls.
- **Correctness at scale (offline):** a 300-document first build puts every
  document's dates into the List of Dates; a 40-document upload makes one
  extraction call per new document and none for older ones.
- **Objective:** after an upload, an objective set in chat is unchanged
  (offline test, and checked in `make e2e`).
- **PDF rendering:** 0 regressions on the local PDF corpus (human-run, billed).

## Open questions
1. Timing targets above: confirm after the Phase 0 baseline.
2. Delta-mode bounds: at most 5 new documents (from the user) and a character
   cap so the new documents fit nearly whole in one prompt (proposed 120,000
   characters). Confirm the cap.
3. Concurrency defaults for an e2-highmem-2:
   - `DOC_PROCESSING_CONCURRENCY=3` documents in processing;
   - `CPU_WORKERS=2` CPU-bound threads in total;
   - `BACKGROUND_LLM_CONCURRENCY=8` concurrent per-document model calls in
     background fan-outs;
   - OCR stays at 4 page workers per document, so at most 12 Vision calls in
     flight.

   Are these within the project's Gemini and Vision quotas?
4. Thread-based CPU offload still shares the GIL. If the lag line exceeds
   250 ms after Phase 1, should redaction and chunking move to a single-worker
   process pool (more memory, more complexity)?
5. Should an Edit-Workspace role change clear the lawyer's objective? Proposed:
   yes. A role reassignment made in chat keeps it.
6. Should the full-rebuild path carry forward approved items the model did not
   regenerate? Proposed: a separate ticket.
7. PyMuPDF is AGPL-3.0 unless commercially licensed. It is already installed
   through `pdf2docx`; using it directly needs a licence decision.
8. The cascade now runs applicable laws and case laws at the same time, so two
   Indian Kanoon fetch sequences overlap per workspace. Is that acceptable to
   the site?
9. When an objective change arrives during an upload, the upload skips the
   panels the cascade will regenerate (analysis, applicable laws, case laws,
   drafting). Is a short window where those panels show the pre-upload
   content acceptable? The cascade includes the new documents, since it runs
   after the upload's ingestion.
10. Follow-up ticket for the out-of-scope items above: create it now, or after
    Phase 1 ships?
11. Template sync: the overall wait is 900 s (decided). Drafting waits only
    5 s for it while holding the pipeline lock, and the wait continues outside
    the lock. A library that arrives later still updates the Drafting
    Assistant through the existing late-snapshot re-emit. Acceptable, given
    the decision that the template wait is 900 s?
12. Should the cap's CRITICAL line also raise an alert (it already writes an
    audit event)?
13. Audio extension of the caps: `AUDIO_TIMEOUT_FACTOR=1.0` (one extra second
    per second of audio) and `PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS=3600`
    (also used when a duration is unknown). Confirm both. Transcription runs
    before the lock is taken, so audio lengthens the locked run only through
    larger transcripts.
14. Is 900 s enough for a legitimate run on a large case before Phase 1's
    concurrency ships (sequential tail, 300 s model timeout per call plus
    retries)? Confirm with Phase 0 timings, or raise the cap until Phase 1
    lands.
15. Should resume (`workspace_resume_request`) and the reconnect replay get
    the same hard cap? They are not capped in this design.
16. Does the backend read the `error` field of the strategy failure emit that
    Phase -1 removes?
