# SIM-163: Fast follow-up uploads and objective changes, objective-safe — technical design

- **Requirements:** `specs/requirements/SIM-163-incremental-update.md`
- **Status:** approved (case owner, 2026-10-26; Phase -1 first)
- **Approver:** tech lead (SimplerToday)
- **Basis:** the user-approved plan for this ticket, corrected where the code
  disagrees ("Corrections to the plan"), then re-prioritised by the user.
- **Priorities:**
  - (a) a 1-5 PDF follow-up upload must be fast;
  - (b) an objective change from chat must be fast;
  - (c) a large first build may stay slow but must not drop dates.
- **Hardware:** one e2-highmem-2 (2 vCPU, 16 GB).

## Summary
An upload already ingests only the new documents. The time goes into a
sequential panel pipeline that re-reads the whole case for Strategy and the
Analysis Report, regenerates everything one panel at a time, and repeats
scrolls, entity extraction and merges. The objective-change cascade has the
same shape: a full analysis regeneration, then dates, laws, case laws and
drafting, one after another.

- **Phase -1 (ships first)** fixes stuck loaders:
  - template sync is bounded (15 minutes) and kept out of the pipeline lock;
  - a 5-minute lock-hold watchdog;
  - a 15-minute hard cap on one run's lock hold, longer with audio;
  - a terminal emit for every failed or cancelled step;
  - strategy failures settle from cache;
  - a failed upload re-emits the case's panels instead of replacing them.
- **Phase 0** adds stage timing and an event-loop lag line.
- **Phase 1:**
  - both pipelines run their independent panels concurrently, with emits held
    in product order by a gate;
  - caches that do not depend on the change are reused;
  - stale document runs are superseded;
  - the cascade no longer waits on upload work it will redo;
  - List of Dates and Procedural Timeline become per-document and
    proportional to the new documents;
  - concurrency is bounded by simple process-wide semaphores sized for 2 vCPU,
    with CPU-bound work kept off the event loop.
- **Phase 2:** persists the objective in force and adds a delta mode for
  Strategy and the Analysis Report on uploads of 1-5 documents. Larger uploads
  take the full rebuild.

## Diagnosis, verified in code (2026-10-26)

### Stuck loaders (Phase -1, ships first)
1. **Template sync can hold the pipeline lock forever.** This is the
   "stuck forever" cause.
   - `ensure_templates_synced` (`template_sync.py:531-603`) emits
     `request_template_sync`, **ignores the emit's bool result** (`:572-575`),
     and with the default `timeout=None` does `await fut` with no bound
     (`:581-583`).
   - The docstring says the future is always resolved by
     `on_template_sync_response`. Two paths break that:
     - the emit is dropped: `SocketClient.emit` returns `False` when the socket
       is down;
     - the backend never answers. Also, a reply that fails schema validation
       returns at `:329-335`, **before** the `try/finally` that resolves the
       future (`:394-402`), so the future is never resolved.

     Stop does not drop this emit: the fence covers only
     `document_feature_response` and `workspace:refresh`
     (`run_control.py:138`). A stop does cancel the waiting task, though.
   - A user with zero templates misses the warm-cache check (`count > 0`,
     `:562`) on every run, so the request goes out every time.
   - Callers:
     - `_generate_and_emit_drafting_suggestions` (`drafting.py:212`), the last
       step of the upload tail (`objective_change.py:609`) and of the cascade
       (`:2338`). Both run inside `_workspace_pipeline_lock`;
     - the chat drafting turn (`chat.py:606`), where the lawyer's turn hangs;
     - `workspace_transferred` (`workspace_events.py:3146`, `force=True`).
   - While the lock is held, these wait forever: later document pipelines
     (`:2591`), the dates/laws runner (`:68`), the cascade (`:2417`), the
     laws-only refresh (`:635`), the reconnect replay `_locked_reemit`
     (`workspace_events.py:76`) and resume (`run_control.py:861`). Every armed
     panel spins until the process restarts.
   - A late snapshot already self-heals the panel:
     `on_template_sync_response` calls `_maybe_reemit_drafting_on_templates`
     (`template_sync.py:404-408`). So bounding the wait loses nothing
     permanent.
2. **Tail steps with no terminal emit on failure.** In
   `_run_list_of_dates_and_applicable_laws` (`objective_change.py:497-619`),
   list_of_dates, applicable_laws, procedural_timeline, relevant_parties and
   drafting only `logger.warning` when they raise. Only case_laws emits a
   cached-or-empty fallback (`:585-602`).
3. **The upload runner ignores cancellation.**
   `_schedule_strategy_analysis_pipeline._runner` (`:2585-2615`) catches only
   `Exception`. A `CancelledError` (shutdown, or a disconnect-driven cancel as
   in the 2026-09-17 incident) strands every loader not yet emitted.
   `_schedule_list_of_dates_and_applicable_laws._locked_runner` (`:90-96`) logs
   the cancel but releases nothing.
4. **The strategy failure path.**
   - `_generate_strategies_from_documents` returns `None` on error
     (`strategy.py:963-965`), so nothing is emitted for `strategy_objective`.
   - If an exception does reach the runner, it emits a raw
     `{"featureType":"strategy_objective","error": str(e)}` through
     `self._socket_client.emit` (`objective_change.py:2549-2562`). That message
     is not buffered when undelivered, has no panel payload (so it is unclear
     the frontend clears the loader), and puts exception text on the wire.
5. **Sync branches after arming 9 loaders.** `ai_update_started`
   (`workspace_events.py:1493-1511`) arms `document_assessments`,
   `strategy_objective`, `analysis_report`, `list_of_dates`, `applicable_laws`,
   `procedural_timeline`, `relevant_parties`, `case_laws` and
   `drafting_assistant`, but only when `has_prior_panels` (`:1484-1491`). Then:
   - **(a) `process_batch` crash** (`except doc_error`, `:1754-1781`): only a
     raw `document_assessments` error is emitted; 8 loaders are stranded.
   - **(b) every new file fails** (`successful == 0`, `:1705-1751`):
     - the case's existing analysis report is **replaced** with
       `_analysis_report_fallback` (`:1710-1722`);
     - `list_of_dates` is emitted as `[]` and the cache wiped (`:1726-1739`).
       That is data loss for an existing case on a failed upload;
     - a full list_of_dates re-extract is scheduled (`batch_result=None`,
       `:1740-1746`);
     - `strategy_objective` is never emitted.
6. **Slowness that looks stuck.** The tail is sequential, so one slow step
   delays every later panel: case-law judgment fetches, Indian Kanoon
   timeouts, a 300 s model timeout plus retries. Phase 1's concurrent ordered
   emits address this; the per-stage timing in this phase tells a slow step
   from a hang.

### Upload path
- **Ingestion is incremental already.** `on_workspace_sync` drops files whose
  id is in Qdrant (`workspace_events.py:1435-1465`).
- **Pipeline is sequential under one lock.**
  - `_schedule_strategy_analysis_pipeline` (`objective_change.py:2575`, runner
    `:2585`) takes `_workspace_pipeline_lock` (`:2591-2597`).
  - It runs `_run_strategy_analysis_timeline_pipeline` (`:2500`): strategy
    (`strategy.py:83`), then `_emit_analysis_report(tail_inline=True)` (`:2630`),
    then `_run_list_of_dates_and_applicable_laws` (`:472`).
  - The tail's steps run one at a time, each in its own `try/except Exception`
    (`:497-619`).
- **`is_update` is unused**: echoed into `isUpdate` only (`strategy.py:248`,
  `:927`); never read by `_generate_analysis_report_from_documents`
  (`analysis_report.py:217`).
- **No supersede.** Nothing cancels a stale queued or running pipeline.
- **Entity cache.** `_extract_case_entities` (`extraction.py:161`) keeps **one
  entry per workspace**, keyed on `sha1(content[:80000])` (`:188-202`).
  - Strategy, analysis and the objective regeneration each pack the corpus
    differently: graph block subtracted or not, `label_kind="Document"`
    (`strategy.py:1295-1330`). Each caller therefore evicts the others' entry.
  - With the graph off, strategy and analysis happen to match.
- **Scrolls.** `get_all_documents` runs ~10-12 times per update, including twice
  in the sync handler (`workspace_events.py:1438`, `:1526`).
- **Dead second scroll.** `skip_reprocess` (`:1521-1550`) can be true only if
  the first scroll raised. Its branch (`:1786-1950`) then re-extracts dates
  from every document (`:1935`).
- **Metadata inline** (`:1640-1644`). Its success line logs `description[:50]`
  (`:1671-1675`), which is a log leak.
- **Matching is sequential** (`objective_change.py:1723-1742`;
  `_match_items_to_previous`, `_shared.py:477`).
- **Dated merge grows with the case.** `_llm_merge_dated_entries_against_previous`
  (`objective_change.py:1258`) sends every prior entry (up to 200) and asks for
  the whole list back (`max_tokens=12000`).
  - The result is not checked with `is_generation_error()`.
  - Undated prior entries are dropped (`:1277-1280`).
- **"Annexure 1"** in incremental date prompts (`list_of_dates.py:303`, `:320`).
- **Lit loader on strategy failure.** `_generate_strategies_from_documents`
  returns `None` (`strategy.py:963-965`), so the error emit
  (`objective_change.py:2549-2563`) never runs. The runner has no
  `CancelledError` handler (`:2610`).

### Objective change from chat (priority b)
The path, in order:

1. **Chat turn** (`chat.py:2257-2298`, likewise `:2417`, `:2677`):
   - `_summarize_objective_instruction_from_thread` (one model call, `:2261`);
   - `_emit_workspace_refresh(..., OBJECTIVE_CHANGE_REFRESH_FEATURES)` arms 6
     loaders (`:2272-2277`);
   - the turn then awaits `_regenerate_strategy_for_new_objective`
     (`strategy.py:1233`). It does this, all inside the chat turn:
     - `get_all_documents` (full scroll, `:1281`);
     - whole-corpus packing at `panel_context_budget()` (`:1288-1330`);
     - `_extract_case_entities` (`:1343`). This is a cache miss whenever
       strategy or analysis ran last, so three parallel sub-agent calls;
     - the main call, `max_tokens=12000`, up to 2 attempts (`:1374-1400`);
     - the re-frame guards on both summaries (`:1445-1526`);
     - a possible re-ground call (`:1553-1574`). Its result is **not** checked
       with `is_generation_error()`;
     - one `strategy_objective` emit (`:1675`);
     - then `_schedule_objective_change_reanalysis` (`chat.py:2292`).
2. **Cascade**: `_schedule_objective_change_reanalysis` (`objective_change.py:2366`)
   takes the pipeline lock (`:2417-2425`), **so it waits for any running upload
   pipeline to finish completely**. It then runs
   `_run_objective_change_incremental_refresh` (`:2002`) strictly in sequence:
   1. Analysis Report, **regenerated from scratch**
      (`_generate_analysis_report_from_documents(is_update=False)`,
      `:2034-2043`). This is another full scroll, whole-corpus packing, graph
      block, entity extraction (a miss again, since the chat turn just replaced
      the single entry) and the main report call. Then possibly a forced
      `_ensure_quality_score_explanation` call (`:2076-2122`). Then
      `_tag_analysis_report_against_current_state` with four sequential
      matching passes (`:2129-2139`). Emit.
   2. List of dates: entries kept, relevance re-scored in one call over every
      date (`_reeval_list_of_dates_relevance`, `:2171-2183`; prompt at
      `list_of_dates.py:~150-251`, `max_tokens=12000`). Preserve fallback.
   3. Applicable laws, **regenerated** with `objective_context`,
      `merge_with_previous=False` (`:2216-2230`). This means
      `_extract_case_signals_from_documents` (another full scroll), an Indian
      Kanoon scrape, the Vertex fallback, a model call and tagging.
   4. Procedural timeline: cache re-emit (`:2257-2275`).
   5. Relevant parties: cache re-emit (`:2281-2293`).
   6. Case laws, **regenerated** (`:2301-2329`). Signals again (another full
      scroll), `_derive_case_law_query` (model), `find_case_law_sources`, then
      select/rank (model).
   7. Drafting, **regenerated** (`:2336-2364`). It reads the strategy,
      analysis, laws and dates caches (`drafting.py:85-166`).
- **What depends on what:**
  - steps 1, 2, 3 and 6 depend only on the new objective, which is already in
    the strategy cache before the cascade starts;
  - steps 4 and 5 depend on nothing;
  - step 7 depends on 1, 2 and 3.

  So 1, 2, 3 and 6 can run concurrently, with emits in product order and
  drafting last.
- **What can be reused:**
  - the document scroll: unchanged documents, one `evidence_version`;
  - entity extraction: same documents, so the same entities;
  - the laws/case-laws signals: computed once;
  - parties and timeline: already re-emitted from cache.

  What must be regenerated: the analysis (a change of perspective, by design,
  `:2022-2033`), laws, case laws, drafting, and the date relevance.
- **Log leaks on this path** (to fix, since the path is touched):
  - `_log_objective_regen_failure` logs `detail` (`objective_change.py:135-144`),
    which callers fill with model-output prefixes (`strategy.py:1386`,
    `:1396-1400`). It is also written to the audit log.
  - The re-frame and re-ground warnings log the dropped figures and the
    ungrounded proper nouns themselves (`strategy.py:1479-1483`,
    `:1491-1495`, `:1548-1551`, `:1598-1601`). These are case facts and names.

### Is the chat-declared objective really overwritten by an upload? Yes.
- It lives only in `_workspace_strategy_payload` (`STRATEGY_PAYLOAD`,
  `events.py:135`).
- It is written by `strategy.py:1663`, `:2173` and `analysis_report.py:2272-2274`.
  No other `CacheKinds` holds it.
- `_generate_strategies_from_documents` (`strategy.py:83-965`) never reads that
  cache. The objective comes from the model (`:334-336`, `:930`).
- The upload pipeline overwrites the cache (`objective_change.py:2536-2543`).
  The analysis then reads the overwritten objective
  (`analysis_report.py:353-359`), as does the score explanation (`:1549`).
- The chat regeneration takes no pipeline lock, so "read the base after the
  lock" is not enough. Phase 2 adds a revision check at write time.

### At scale (correctness only)
- **List of Dates starves documents.** `list_of_dates.py:289-327` splits
  `max(20_000, panel_context_budget() - graph)` evenly, `fit_to_budget`s each
  document, hard-truncates at 1.1 × budget, and makes one 12K-token output
  (`:356`). At 300 documents that is ~660 characters each, on a first build or
  a 40-document upload.
- **Procedural Timeline is per-document but front-cut, unbounded and
  consolidated once.**
  - Each document is cut to its first 12,000 characters (`_EXCERPT_SIZES`,
    `procedural_timeline_agent.py:336`; `_sanitize_content` returns
    `[:max_chars]`, `:104`).
  - `gather` has no limit (`:480-481`).
  - One consolidation call covers all entries (`max_tokens=8000`, `:595-600`).
  - Per-document lines log the document name (`:375-377`, `:406-409`).
- **Strategy and Analysis starve the same way** (`strategy.py:146-197`,
  `analysis_report.py:264-323`). This is documented here and left to the
  follow-up ticket.
- **Relevant parties** is per-document with provenance (`relevant_parties.py:387-532`),
  but its `gather` has no limit (`:488-490`).

### CPU, threads and memory on 2 vCPU
- **On the event loop today, CPU-bound, per document:**
  - `readability_scorer.calculate_score` over the full text
    (`document_processor.py:342`);
  - `scan_text` PII redaction (`:367`), or `redact_tableset` + `render_text`
    for sheets (`:364-365`);
  - `chunk_text` (`qdrant.py:743`);
  - sparse `encode_document` per chunk (`qdrant.py:781`).
- **In threads today:**
  - `process_file_by_extension` via `asyncio.to_thread`
    (`document_processor.py:311`): PyPDF2 parsing, Office parsing, and OCR,
    which spawns its own `ThreadPoolExecutor(OCR_PAGE_WORKERS=4)`
    (`file_handlers.py:533`) whose workers render pages (one `pdftoppm`
    process per page, `:478`) and run `prepare_image`;
  - `parse_workbook` (`:263`);
  - `get_all_documents` reassembly (`qdrant.py:1120`);
  - every Qdrant client call.
- **Default executor exhaustion.** `asyncio.to_thread` uses the loop's default
  executor: `min(32, cpu_count + 4)` = **6 threads** on 2 vCPU. No code sets
  another (no `set_default_executor` in `app/`). A document extraction holds
  one of those threads for its whole OCR run (minutes). Three concurrent
  documents leave three threads for every Qdrant call, including chat
  retrieval.
- **Memory.** OCR keeps up to `OCR_PAGE_WORKERS * 2` = 8 pages in flight per
  document (`file_handlers.py:548`). A 200-DPI A4 page is ~11 MB decoded. Today
  memory is bounded only because documents run one at a time.

## Corrections to the plan
| Plan claim | Finding | Consequence |
|---|---|---|
| Replay guard at `:886-911` reruns everything on a curation-only resend | Guard is at `:891-965`; a same-files resend schedules nothing | Step dropped |
| Entity cache "always misses" | One slot per workspace, evicted by differently packed callers (strategy, analysis, objective regen); graph adds misses | Key on the document set (step 3) |
| "The new run covers every document anyway" | False for incremental dates/timeline and delta | Successor absorbs the stale batch |
| Release loaders when cancelled | On supersede, stale data would clear the new run's loaders | Release only on non-supersede cancels |
| Revive `_merge_analysis_report_with_previous` | Deliberately replaced by tagging (`:1689-1698`); stale comment at `:2032` | Reuse only its user-added guard |
| Base read after the lock is enough | Chat regeneration takes no lock | Revision check at write time |
| PyMuPDF text layer is no output change | It changes text | Render only |
| `skip_reprocess` dead | Reachable on a first-scroll failure | Removed; first scroll retried |
| `INGEST_CONCURRENCY=3` per batch | Concurrent uploads multiply it; the default executor (6 threads) and CPU (2 vCPU) are the real limits | Process-wide semaphores + dedicated extraction pool + CPU slot |
| Objective change was outside the plan | It is a priority (b) and repeats most upload costs | Cascade steps O1-O6 |

## Affected code
| File | Change |
|---|---|
| `app/socket/handlers/objective_change.py` | Stage timing; supersede (`origin=`), batch absorption, `CancelledError` handling, terminal emits; concurrent upload tail and **concurrent cascade** behind the ordered gate; cascade-pending skip; matching `gather`; **new** `_merge_dated_entries_incremental`; `_log_objective_regen_failure` logs reason only; P2 revision check; comment at `:2032` |
| `app/socket/handlers/template_sync.py` | Phase -1: bounded `ensure_templates_synced`, emit result read, future resolved on invalid replies, **new** `_prewarm_templates` |
| `app/socket/handlers/drafting.py` | Phase -1: `templates_ready` keyword; bounded in-lock wait |
| Pipeline-lock callers (`objective_change.py:68`, `:635`, `:2417`, `:2591`; `workspace_events.py:76`; `run_control.py:861`) | Phase -1: through **new** `_hold_pipeline_lock`; prewarm started before the lock |
| `app/socket/handlers/_shared.py` | **New** `_OrderedEmitGate`, `PIPELINE_EMIT_ORDER`, `UPLOAD_REFRESH_FEATURES`; Phase -1 `_hold_pipeline_lock`, `_set_pipeline_stage`, lock watchdog |
| `app/socket/handlers/strategy.py` | Objective regen: memoised scroll, entity cache by document set, `is_generation_error()` on the re-ground call, counts-only guard logs; P2 delta, objective-in-force enforcement and recording |
| `app/socket/handlers/analysis_report.py` | P2 delta; record objective in `_try_apply_intelligence_chat_panel_updates` |
| `app/socket/handlers/extraction.py` | `_extract_case_entities(..., cache_key=None)` |
| `app/socket/handlers/documents.py` | `_extract_case_signals_from_documents` memoised per `(workspace_id, evidence_version)` |
| `app/socket/handlers/list_of_dates.py`, `procedural_timeline.py`, `app/langgraph/procedural_timeline_agent.py` | Per-document extraction, cache, segmentation, assembly; Annexure numbering; new merge; bounded fan-out; id-only logs |
| `app/socket/handlers/relevant_parties.py` | Its per-document `gather` goes under `BACKGROUND_LLM_CONCURRENCY` |
| `app/socket/handlers/workspace_events.py` | Remove second scroll and `skip_reprocess` branch; metadata into the pipeline; `origin="document"`; P2 role-change clear |
| `app/socket/handlers/chat.py` | No logic change beyond what `_regenerate_strategy_for_new_objective` and `_schedule_objective_change_reanalysis` do; stage timing for the chat objective path |
| `app/core/concurrency.py` (**new**) | `doc_processing_slot()`, `background_llm_slot()` (asyncio semaphores); `cpu_slot()` (threading semaphore); `run_cpu(fn, *args)`; `doc_extract_executor` (dedicated `ThreadPoolExecutor`); event-loop lag monitor |
| `app/processing/document_processor.py` | `process_batch` gathers under `doc_processing_slot`; extraction on `doc_extract_executor`; readability, redaction via `run_cpu` |
| `app/processing/file_handlers.py` | Page render + `prepare_image` inside `cpu_slot()`; PyMuPDF rendering behind `PDF_RENDER_ENGINE` |
| `app/vectorstore/qdrant.py` | `get_all_documents` memo; `chunk_text` and sparse encoding via `run_cpu`; `add_document` bumps version |
| `app/workspace/persistence.py`, `app/socket/events.py`, `app/socket/handlers/state_roles.py` | **New** `CacheKinds.OBJECTIVE_IN_FORCE`, `DOCUMENT_DATED_EXTRACTIONS`; **new** handler dicts (below); hydration |
| `app/core/lifecycle.py` | Start/stop the lag monitor; shut down `doc_extract_executor` |
| `app/core/config.py`, `env.example` | **New** settings |
| `app/prompts/` | **New** dated duplicate-check prompt; delta prompts; "objective in force" block |
| `CHANGELOG.md`, `CLAUDE.md` | Async scheduling, objective change, concurrency on 2 vCPU |
| Tests and scripts | **New** `tests/offline/verify_incremental_update.py`, `tests/test_dated_merge.py`, `tests/test_concurrency_limits.py`, `scripts/compare_pdf_render.py`; `tests/test_ocr.py` additions |

### New settings (`app/core/config.py`)
| Setting (env) | Default | Purpose / rollback |
|---|---|---|
| `PIPELINE_CONCURRENT_PANELS` | `true` | Concurrent upload tail **and** cascade; `false` = today's order |
| `PIPELINE_SUPERSEDE_ENABLED` | `true` | `false` = queue behind the lock |
| `CASCADE_SKIPS_UPLOAD_PANELS` | `true` | Upload skips panels a pending cascade will regenerate |
| `DOC_PROCESSING_CONCURRENCY` | `3` | Process-wide documents in `process_document`; `1` = sequential |
| `CPU_WORKERS` | `2` | Process-wide CPU-bound tasks (render, decode, redaction, readability, chunking, sparse encoding) |
| `BACKGROUND_LLM_CONCURRENCY` | `8` | Process-wide per-document model fan-outs (dates, timeline, parties) |
| `PDF_RENDER_ENGINE` | `pymupdf` | `poppler` = per-page `pdftoppm` |
| `PER_DOCUMENT_DATED_EXTRACTION` | `true` | `false` = today's single-prompt dates |
| `DATED_EXTRACTION_SEGMENT_CHARS` | `60000` | Long documents split, never cut |
| `INCREMENTAL_UPDATE_ENABLED` | `true` | `false` = full rebuild (objective preservation stays) |
| `INCREMENTAL_MAX_NEW_DOCS` | `5` | Delta only for uploads up to this many documents |
| `INCREMENTAL_MAX_NEW_CHARS` | `120000` | ...and up to this many new characters |
| `EVENT_LOOP_LAG_WARN_MS` | `250` | Lag monitor warning threshold |
| `TEMPLATE_SYNC_TIMEOUT_SECONDS` | `900` | Phase -1: bound on any `request_template_sync` wait (user decision); a `False` emit returns at once |
| `TEMPLATE_SYNC_IN_LOCK_WAIT_SECONDS` | `5` | Phase -1: how long drafting waits on the prewarm while the lock is held (the prewarm itself keeps waiting up to the template bound, outside the lock) |
| `PIPELINE_RUN_TIMEOUT_SECONDS` | `900` | Phase -1: hard cap on one run's lock hold (user decision) |
| `AUDIO_TIMEOUT_FACTOR` | `1.0` (proposed) | Seconds of cap added per second of audio in the run's batch |
| `PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS` | `3600` (proposed) | Upper bound of the audio-extended cap; used when an audio duration is unknown |
| `PIPELINE_LOCK_WARN_SECONDS` | `300` | Phase -1 informational watchdog WARNING (user decision) |

## Event and payload changes
None. Shapes unchanged. What changes:
- **Timing:** panels arrive sooner.
- **Order is kept:**
  - upload runs: `workspace_metadata` → `strategy_objective` →
    `analysis_report` → `list_of_dates` → `applicable_laws` →
    `procedural_timeline` → `relevant_parties` → `case_laws` →
    `drafting_assistant`;
  - cascade runs: the same order without the first two (the chat turn has
    already emitted `strategy_objective`).
- **`workspace_metadata` on sync** comes from the pipeline.
- **Values:**
  - the objective is preserved across uploads;
  - large cases' dates and timeline cover every document;
  - incremental dates cite the right annexure number.
- **Pending cascade during an upload:** the upload emits no `analysis_report`,
  `applicable_laws`, `case_laws` or `drafting_assistant` of its own; the
  cascade emits them (open question 9).

- **Phase -1 strategy failure:** the raw
  `{"featureType":"strategy_objective","error": "<exception text>"}` emit is
  replaced by the cached (or fallback) `strategy_objective` payload. This
  removes an `error` field that only this path sent. It is a wire-visible
  change: the backend owners should confirm nothing reads it.
- **Phase -1 failed upload of an existing case:** the panels are re-emitted
  unchanged instead of a generic analysis report and an empty List of Dates.

No backend or frontend ticket is needed beyond confirming the `error` field.
The PR and CHANGELOG call out timing, the metadata move, the cascade/upload
interplay and the Phase -1 behaviour.

## Design

### Phase -1: stuck loaders (own PR, ships first)
This phase is independent of Phases 0-2: no concurrency and no new caches.

**S1. Template sync never holds the lock and never waits unbounded.**
- `ensure_templates_synced`:
  - `timeout=None` now means `settings.template_sync_timeout_seconds` (**new**,
    default **900**, the user's decision), so every caller is bounded,
    including `chat.py:606` and `workspace_events.py:3146`;
  - inside a pipeline run whose batch has audio, the prewarm uses the run's
    extended cap instead (S8);
  - the emit's bool is read: `False` logs
    `request_template_sync not delivered` and returns at once, proceeding with
    the current store;
  - `asyncio.TimeoutError` is handled as today (proceed);
  - `CancelledError` is not swallowed;
  - the pending future is always popped (`finally`, as today).
- `on_template_sync_response`: the invalid-payload return (`:329-335`) also
  resolves the future for `payload_in.get("requestId")` before returning.
- **Prewarm outside the lock.** The upload runner
  (`_schedule_strategy_analysis_pipeline._runner`), the dates/laws runner and
  the cascade runner (`_schedule_objective_change_reanalysis._runner`) start
  `self._prewarm_templates(user_id)` (**new**) as a tracked task **before**
  `async with lock`. The task runs the bounded `ensure_templates_synced`.
- `_generate_and_emit_drafting_suggestions` gains
  `templates_ready: Optional[asyncio.Task] = None` (**new**). Inside the lock it
  waits on that task for at most `template_sync_in_lock_wait_seconds`
  (**new**, default 5) via `asyncio.wait`, then proceeds with the current
  store; it does not call `ensure_templates_synced` itself when a task is
  given. Without a task (other callers) it uses the bounded call.
- A library that arrives later updates the panel through
  `_maybe_reemit_drafting_on_templates` (`template_sync.py:404-408`), which
  re-emits drafting outside the pipeline.
- Rule (added to CLAUDE.md): never hold `_workspace_pipeline_lock` across an
  unbounded external round trip.

**S2. Pipeline-lock watchdog.**
- `_workspace_pipeline_lock` callers go through **new**
  `_hold_pipeline_lock(workspace_id, holder: str)`, an async context manager
  wrapping `async with lock`. It records
  `_workspace_pipeline_lock_holder[workspace_id] = {"holder", "stage",
  "acquiredAt"}` (**new**, swept dict) and clears it on exit. `holder` is a
  fixed label: `upload_pipeline`, `dates_laws`, `cascade`, `laws_only`,
  `reemit`, `resume`.
- **New** `_set_pipeline_stage(workspace_id, stage)` updates `stage` at each
  step. The names are those of the Phase 0 stage lines, which this phase
  introduces.
- A watchdog task started in `lifecycle.startup` checks every 60 s and logs
  `pipeline lock held {}s holder={} stage={} cap={}s workspace={}` at WARNING
  once a hold passes `pipeline_lock_warn_seconds` (**new**, **300**). It is
  informational, at most once per 5 minutes per workspace, with ids, labels and
  durations only.
- The watchdog never releases anything. Enforcement is the hard cap in S8.

**S3. Every failed tail step emits a terminal state.**
- `_run_list_of_dates_and_applicable_laws` and the upload runner run with an
  emit observer: `_EMIT_OBSERVER.set(emitted)` (`run_control.py:83`, fed by
  `_should_suppress_emit`, `:275-279`), the mechanism resume already uses
  (`:846`, `:884`). So the run knows which featureTypes reached the socket
  layer.
- In each step's `except Exception` (list_of_dates, applicable_laws,
  procedural_timeline, relevant_parties, drafting, and case_laws, whose inline
  fallback is replaced), call
  `_release_refresh_loaders(ws, uid, [feature], skip=emitted,
  reason="<feature>_failed", workspace_role=role)`.
  `_release_refresh_loaders` already knows the cached-or-empty terminal payload
  for all nine featureTypes (`_shared.py:304-417`) and emits through
  `_emit_document_feature`, so undelivered ones are buffered and the stop
  fence applies.
- This is the per-panel form of Phase 1's ordered-gate rule.

**S4. The runners handle `CancelledError`.**
- Upload runner (`:2585-2615`) and dates/laws runner (`:90-96`): add
  `except asyncio.CancelledError` before `except Exception`. Log, release the
  armed-and-unemitted features (`UPLOAD_REFRESH_FEATURES`, minus
  `document_assessments`, which the handler emits), then re-raise.
- Release only when this run armed loaders (`is_update` with prior panels).
  When Phase 1's supersede lands, a superseded run skips the release, as
  designed there.
- A `finally` also releases anything still unemitted on a normal exit (the
  strategy-`None` case).

**S5. Strategy failure settles with the cached panel.**
- When `_generate_strategies_from_documents` returns `None`, or raises, the
  runner calls
  `_release_refresh_loaders(ws, uid, ["strategy_objective"], skip=emitted,
  reason="strategy_failed")`. That emits the cached strategy, or
  `_strategy_objective_fallback` on a first build, through
  `_emit_document_feature`.
- The raw `{"error": str(e)}` emit (`:2549-2562`) is removed, so no exception
  text goes on the wire. The error is logged once with
  `logger.opt(exception=e)`.
- Note for open question: whether the backend relies on the `error` field for
  anything (none known).

**S6. Sync branches after arming.** A **new** local flag
`armed_features: List[str]` is set where `ai_update_started` is emitted
(`workspace_events.py:1493`); otherwise it is empty.
- **(a) `process_batch` crash** (`:1754`): after today's
  `document_assessments` error emit (kept, with its partial-spend metadata),
  call `_release_refresh_loaders(ws, uid, [f for f in armed_features if f !=
  "document_assessments"], reason="sync_ingest_failed")`. The panels come back
  from cache.
- **(b) every new file failed** (`:1705`):
  - **existing case** (`has_prior_panels`): `_release_refresh_loaders` for
    every armed feature except `document_assessments` (already emitted). No
    fallback replacement, no `[]` emit, no cache wipe, no re-extract;
  - **first build** (no prior panels, nothing armed): today's behaviour stays
    (analysis fallback, empty dates, tail scheduled).
  - `strategy_objective` is covered by the release in the existing-case
    branch.
- Both branches log `sync settled from cache reason={} features={}` (counts
  and fixed tokens).

**S7. Stage timing.** The Phase 0 upload and cascade stage lines ship in this
PR, so a slow step is visible as `stage <name> took` and a hang as a watchdog
warning with its stage.

**S8. Hard cap on one run's hold of the pipeline lock (user decision: 15
minutes, longer with audio).**
- **Applies to:** the upload pipeline runner, the dates/laws runner and the
  cascade runner. In each, the body inside `_hold_pipeline_lock` runs under
  `asyncio.timeout(cap)` (Python 3.11). The cap is measured from lock
  acquisition, so time spent queueing for the lock does not count.
- **Cap:**
  - `pipeline_run_timeout_seconds` (**new**, **900**);
  - when the run's `batch_result` contains audio evidence (results with
    `is_audio`, i.e. the CLAUDE.md audio extensions `.mp3`, `.wav`, `.m4a`,
    `.ogg`, `.flac`, `.opus`, `.webm`, `.aac`), the cap becomes
    `min(900 + audio_timeout_factor × total_audio_seconds,
    pipeline_run_timeout_audio_max_seconds)`, with **new**
    `audio_timeout_factor` (proposed **1.0**) and
    `pipeline_run_timeout_audio_max_seconds` (proposed **3600**);
  - `total_audio_seconds` comes from the duration the transcription path
    already knows, `AudioTranscriptionResult.duration_seconds`
    (`audio_transcription.py:197`, probed by `_probe_duration_seconds`,
    `:549`, and logged at `document_processor.py:580-583`). Today it is not
    carried into the batch result: `audio_extras` (`document_processor.py:288-303`)
    gains **new** internal key `audio_duration_seconds`.
    `_build_document_assessment_envelope` copies named keys only, so nothing
    reaches the wire;
  - if any audio result has no duration (a failed transcription, or a probe
    that returned `None`), the cap is the audio maximum;
  - a superseding run computes its cap from its absorbed batch. The cascade
    has no batch, so it uses 900.
- **When the cap is exceeded:**
  1. `asyncio.timeout` cancels the body; every step's `CancelledError` handler
     re-raises, and `gather` children (Phase 1) are cancelled with it;
  2. the runner catches the resulting `TimeoutError` and logs CRITICAL
     `pipeline run exceeded cap={}s holder={} stage={} workspace={}`, plus an
     audit event `pipeline_run_timeout` (ids, labels, durations only);
  3. it calls `_release_refresh_loaders` for every armed feature not in the run's
     emit-observer set, settling them from cache (or the empty/fallback
     payload);
  4. the `async with` exits, so the lock is freed. Queued pipelines, the
     cascade, the reconnect replay (`workspace_events.py:76`) and resume
     (`run_control.py:861`) proceed.
- **Not covered:** resume (`run_control.py:861`) and the reconnect replay are
  not capped here (open question 15). Their own external waits are bounded by
  S1.
- **Timing context.** Ingestion, including audio transcription, runs **before**
  the lock is taken (`workspace_events.py:1560`; `run_control.py:846-855`). So
  audio lengthens a locked run only through larger transcripts in the panel
  prompts; the extended cap is conservative. Before Phase 1's concurrency
  lands, a large case's sequential tail plus model retries (300 s timeout per
  call) could legitimately approach 900 s. Phase 0 timings must confirm the cap
  (open question 14).

### Phase 0: measurement (first commit)
- **Upload:** `stage {} took {:.1f}s mode={}` for `ingest`, `lock_wait`,
  `metadata`, `strategy`, `analysis_report`, `list_of_dates`,
  `applicable_laws`, `procedural_timeline`, `relevant_parties`, `case_laws`,
  `drafting`, `pipeline_total`.
- **Objective change:** `stage objective_{} took {:.1f}s` for
  `instruction_summary`, `scroll`, `entities`, `strategy_call`, `reground`,
  `strategy_emit`, `cascade_lock_wait`, `cascade_analysis`,
  `cascade_quality_explanation`, `cascade_tagging`, `cascade_dates_relevance`,
  `cascade_laws`, `cascade_case_laws`, `cascade_drafting`, `cascade_total`.
- **Loop lag:** `app/core/concurrency.py` runs a monitor task (started in
  `lifecycle.startup`) that sleeps 100 ms and logs `event_loop_lag ms={}` at
  WARNING above `EVENT_LOOP_LAG_WARN_MS`, at most once per 10 s.
- All lines carry names, counts and durations only. `request_id` is inherited.
- **Baselines** on a fictional 15-document case on an e2-highmem-2, recorded
  in the PR:
  - 1 text PDF upload;
  - 5 text PDF upload;
  - 5 scanned PDF upload;
  - an objective change;
  - an objective change during a 1-PDF upload.

### Phase 1, step 1: supersede stale document pipelines
- `_workspace_document_pipeline[workspace_id]` (**new**, swept) holds the
  pipeline `Task`. The sweep cancels `Task` values (`workspace_purge.py:536-540`).
  `_workspace_document_pipeline_state[workspace_id]` (**new**, swept) holds
  `{"generation", "batch_result", "deleted_doc_ids", "superseded"}`.
- Schedulers gain `origin: str = "other"`. Upload, delete, restore and reload
  callers pass `"document"`. Cascades and `_schedule_applicable_laws_only` stay
  `"other"` and are never superseded.
- A new `"document"` run marks the old one `superseded`, cancels it, and
  absorbs its `batch_result` (dedupe by `document_id`) and `deleted_doc_ids`.
  There is no epoch bump.
- Runners catch `asyncio.CancelledError` first:
  - superseded: log, release nothing, re-raise;
  - otherwise: release the run's unemitted armed loaders, re-raise.

### Phase 1, step 2: concurrent upload panels, priority three first
Replaces the earlier strict `PIPELINE_EMIT_ORDER` gate. The case owner chose on
2026-10-27: "Only after the priority 3" and "Run early, refresh later".

- **Measured before** (local, gate0, 27 files, 2026-10-27):
  - first build: 650.9 s from lock to drafting. Strategy 64 s, Analysis 67 s,
    List of Dates 409 s, Laws 10 s, Timeline 5 s, Parties 53 s, Case laws
    37 s, Drafting 6 s;
  - one-file update: 216 s. Strategy 60 s, Analysis 64 s, Dates 23 s, Laws
    13 s, Timeline 2 s, Parties 12 s, Case laws 36 s, Drafting 6 s.
- **Dependencies** (re-verified 2026-10-27):
  - list_of_dates, applicable_laws, procedural_timeline, relevant_parties and
    case_laws read only their own caches on the upload path. The objective
    is passed to laws and case laws only by the objective-change cascade;
  - analysis reads strategy (the objective);
  - drafting reads strategy, analysis, laws and dates (`drafting.py:85-166`).
- **Shape:** inside the one pipeline task, under the same lock and cap:
  - an `asyncio.TaskGroup` starts the five tail panels at t=0. Each runs
    through `_run_tail_step`, so a failure settles its own panel;
  - the **priority lane** runs in the runner itself: strategy → analysis →
    drafting. Analysis report chips (one model call, about 20 s) run as a
    sibling task, so drafting does not wait for them;
  - **no emit hold:** every panel is sent the moment it is ready, in
    completion order. (An earlier version held the tail panels until the
    lane had emitted; testing showed the panels then arrived as one burst,
    so the hold was removed.) A lane failure never blocks the tail;
  - **first build only** (`is_update=False`): once the tail is done, drafting
    runs once more, because laws and dates now exist. An update's drafting
    reads the previous laws and dates and is not repeated.
- The children inherit the run scope, the emit observer set and the logging
  context. Cancelling the runner (stop, run cap, shutdown) cancels them
  through the TaskGroup. Every child re-raises `CancelledError`, and
  `_run_tail_step` catches only `Exception`, so one failed panel never cancels
  its siblings.
- Stage lines for the tail say `mode=parallel`. The stage recorded for the
  run-cap CRITICAL is the last one started, so it can name any of the
  overlapping steps.
- **Billing note:** an emit reports the case's spend since the previous emit
  (`drain_for_emit`). When panels overlap, one panel's `price` can include
  calls another panel made. The case total (`totalPrice`) and gate0
  reconciliation are unchanged. The PR is labelled `needs-human-review`.
- **Rollback:** `PIPELINE_CONCURRENT_PANELS=false` restores the sequential run
  exactly.
- Other callers of the tail (dates/laws runner, deletion, restore, reload, the
  objective-change cascade) stay sequential in this step.

### Phase 1, step 3: entity cache keyed by document set
- `_extract_case_entities(..., cache_key=document_set_key(all_docs))`, where
  the key is a sha1 over each document's id and a hash of its stored content,
  in order. Strategy, analysis and `_regenerate_strategy_for_new_objective`
  pass it.
- Same documents means a cache hit, however the caller packed its prompt.
  Measured: each panel's graph block changes its per-document budget, so the
  packed text never matched and the update paid about 23 s twice.
- A re-read changes the content hash, so it misses.

### Phase 1, step 14: List of Dates per-document pass, bounded
- The temporal agent pass (`list_of_dates.py`, one or two calls per document)
  and the per-date semantic dedupe (`_dedupe_dated_entries_semantically`) ran
  one call at a time: 276 s and 70 s of the 409 s first build above.
- Both now run with `asyncio.gather` under a semaphore of
  `LIST_OF_DATES_CONCURRENCY` (default 4). Results are merged in the original
  document and date order, so the output is identical to the sequential run.
- `LIST_OF_DATES_CONCURRENCY=1` restores the sequential behaviour.

### Phase 1, step 4: one document scroll
- `QdrantStore.get_all_documents` memoises per workspace on
  `evidence_version(workspace_id)`, returns shallow copies, and keeps only the
  latest version.
- `add_document` (legacy) bumps the version.
- `delete_documents` and `delete_workspace` drop the memo (case text, so the
  purge reaches it). `release_document_memo(ws)` (**new**) drops it after each
  pipeline or cascade run.
- `_extract_case_signals_from_documents` results are memoised the same way, so
  applicable laws and case laws compute signals once per run.
- Sync handler: drop the second scroll and the `skip_reprocess` branch; retry a
  failed first scroll once, then process unfiltered (idempotent).

### Phase 1, step 5: matching passes together
`asyncio.gather` over the sections in `_tag_analysis_report_against_current_state`.

### Phase 1, step 6: metadata off the critical path
- The metadata call moves into chain A, with its own `capture_usage()` scope,
  emitted through the gate.
- It is skipped on an upload when a category is cached.
- Its log line names the category only.

### Phase 1, steps O1-O6: objective-change cascade (priority b)
- **O1, concurrent cascade.** `_run_objective_change_incremental_refresh`
  becomes:
  - `gather` of (analysis → quality-explanation refresh → tagging), the dates
    relevance re-score, the applicable-laws regeneration and the case-laws
    regeneration, plus the timeline and parties cache re-emits;
  - then drafting.
  - Every step keeps its current preserve-and-re-emit fallback and its `seen`
    bookkeeping, and adds `except asyncio.CancelledError` (log, gate `done`,
    re-raise).
  - Emits pass the gate in cascade order: `analysis_report` →
    `list_of_dates` → `applicable_laws` → `procedural_timeline` →
    `relevant_parties` → `case_laws` → `drafting_assistant`.
  - The scheduler's terminal release (`objective_change.py:2463-2481`) is
    unchanged.
  - `PIPELINE_CONCURRENT_PANELS=false` restores the sequence.
- **O2, reuse.** Through steps 3 and 4:
  - the chat-turn regeneration and the cascade analysis share one scroll and
    one entity result;
  - laws and case laws share one signals result.

  The analysis still re-reads the evidence for the new perspective (by design,
  `:2022-2033`). The forced score-explanation refresh (`:2076-2122`) is kept.
- **O3, a cascade does not wait on upload work it will redo.**
  - `_schedule_objective_change_reanalysis` sets
    `_workspace_cascade_pending[workspace_id] = True` (**new**, swept) before
    it waits on the lock, and clears it when it takes the lock.
  - A running document pipeline checks the flag before each of `analysis_report`,
    `applicable_laws`, `case_laws` and `drafting_assistant`. When set (and
    `CASCADE_SKIPS_UPLOAD_PANELS`), it skips them and marks them `done` in its
    gate without emitting.
  - It still runs strategy, `list_of_dates`, `procedural_timeline` and
    `relevant_parties` for the new documents, because the cascade preserves
    those panels and needs them current.
  - The upload's armed loaders for the skipped features are cleared by the
    cascade's emits of the same featureTypes. If the cascade fails, its own
    terminal release covers them.
  - The upload releases nothing for skipped features. The skipped panels are
    recorded as `stage <f> skipped reason=cascade_pending`.
  - Strategy is not skipped: the upload's documents must reach the case
    summary. In Phase 2 the revision check keeps the chat objective.
- **O4, chat-turn path.**
  - `_regenerate_strategy_for_new_objective` uses the memoised scroll and the
    document-set entity key.
  - The re-ground result (`strategy.py:1562-1574`) is checked with
    `is_generation_error()`; on error the original objective stands.
  - The single coalesced `strategy_objective` emit and the
    `refresh_case_summary` licence are unchanged.
- **O5, guards and rules unchanged.**
  - `find_dropped_case_figures`, `find_ungrounded_proper_nouns` and the
    mandated openings still gate the re-frame;
  - one failing summary holds back the other;
  - the objective lands regardless;
  - role reassignment (`refresh_case_summary=False`) and the surgical edit keep
    their behaviour.
- **O6, logging.**
  - `_log_objective_regen_failure` logs and audits `reason` and a size
    (`chars(detail)`), never model output.
  - The guard warnings log counts (`dropped={} figures`,
    `ungrounded={} terms`), not values.

### Phase 1, step 8: bounded concurrency for 2 vCPU
Simple process-wide semaphores in **new** `app/core/concurrency.py`; no quota
modelling.
- **`doc_processing_slot()`:** `asyncio.Semaphore(DOC_PROCESSING_CONCURRENCY)`,
  shared by all workspaces, around each `process_document` in `process_batch`.
  `gather` keeps input order; each call traps its own exception into a failed
  result.
- **`doc_extract_executor`:** a dedicated `ThreadPoolExecutor(max_workers=
  DOC_PROCESSING_CONCURRENCY)` for `process_file_by_extension` and
  `parse_workbook`, used via `loop.run_in_executor` with a copied context. Long
  extractions no longer occupy the default executor (6 threads), which stays
  free for Qdrant calls and chat retrieval. It is shut down in
  `lifecycle.shutdown`.
- **`cpu_slot()`:** a `threading.BoundedSemaphore(CPU_WORKERS)`.
  `run_cpu(fn, *args)` runs `fn` on the default executor inside the slot, with a
  copied context so logging and usage scopes carry over. See the CPU table
  below.
- **`background_llm_slot()`:** `asyncio.Semaphore(BACKGROUND_LLM_CONCURRENCY)`,
  acquired only at per-document fan-out sites:
  - per-document dates and timeline extraction (step 13);
  - the timeline agent's per-document calls;
  - relevant parties' per-document extraction.

  Chat, strategy, analysis and the cascade's single calls never pass through
  it, so chat is never queued behind a large upload.
- **Vision:** not separately limited. Concurrent Vision calls are at most
  `DOC_PROCESSING_CONCURRENCY × OCR_PAGE_WORKERS` (12 by default).
- **Costs:** semaphores change no context. The caller's `capture_usage()`
  accumulator is shared through copied contexts and mutated under its lock
  (`app/ai/usage.py:78-83`). The workspace ledger is charged from the inherited
  logging-context workspace. Totals and attribution equal a sequential run
  (AC-21, AC-66). This exercises the billing path concurrently, so it needs
  human review.

**CPU-bound steps and how each is bounded**
| Step | Where | Today | After |
|---|---|---|---|
| PDF text layer (PyPDF2) and Office/XLSX parsing | `process_file_by_extension`, `parse_workbook` | default executor | `doc_extract_executor` (≤3 documents) |
| Page render (`pdftoppm` process or PyMuPDF pixmap) + `prepare_image` / `jpeg_for_vision` decode | `_ocr_pdf_page`, `_render_page_for_ocr` (`file_handlers.py:469-499`) | 4 threads per document, unbounded across documents | render + encode inside `cpu_slot()`; the Vision request outside it |
| `native_page_jpeg` content-stream parse | `_extract_text_from_pdf_with_ocr` loop (`file_handlers.py:535-543`) | the extraction thread | unchanged (inside `doc_extract_executor`) |
| Readability scoring | `document_processor.py:342` | event loop | `run_cpu` |
| PII redaction `scan_text` / `redact_tableset` + `render_text` | `document_processor.py:364-367` | event loop | `run_cpu` (same functions, same results) |
| `chunk_text` | `qdrant.py:743` | event loop | `run_cpu` |
| Sparse `encode_document` | `qdrant.py:781` | event loop | `run_cpu` (one call per document, all chunks) |
| `get_all_documents` reassembly | `qdrant.py:1120` | default executor | unchanged (memoised, so rarer) |
| Audio (`ffmpeg`) | audio transcription | subprocess, `audio_batch_concurrency` | unchanged (follow-up if measured) |

Threads still share the GIL. These steps run regex and pure-Python loops, so
the lag line (Phase 0) decides whether a process pool is needed (open
question 4).

**Memory.** Rendering happens inside `cpu_slot()`, so at most `CPU_WORKERS`
decoded page images exist at once. Queued work holds only encoded JPEG bytes:
at most `OCR_PAGE_WORKERS × 2` per document × `DOC_PROCESSING_CONCURRENCY`
(24 by default, about 1-2 MB each). The `get_all_documents` memo is released
after each run.

### Phase 1, step 9: OCR page rendering
- `PDF_RENDER_ENGINE=pymupdf` opens each PDF once, renders pages with a pixmap
  at the existing DPI rule, and takes the page count from the same engine.
- Any PyMuPDF error falls back to poppler for the whole document.
- Unchanged: the text layer, `native_page_jpeg`, detection order, retries,
  `layout_text`, `[OCR note: ...]` marks, sentinels and `record_ocr_units`.
- On 2 vCPU this removes one PDF re-parse per scanned page, which is the main
  CPU cost of a scanned upload.
- Gate: `scripts/compare_pdf_render.py` (human-run, billed), 0 regressions.

### Phase 1, step 11: Annexure numbering
`annexure_index_by_doc_id` comes from the full `all_docs` order and is passed to
`_generate_list_of_dates_from_documents` for the header, `doc_id_by_index` and
the name fallback. It is used by every per-document call.

### Phase 1, step 12: dated merge proportional to the new documents
- **New** `dated_entry_day()` (`event_utils.py`) parses entry dates.
- **New** `_merge_dated_entries_incremental`:
  - a new entry collides with prior entries ±1 day (identical string when
    unparseable), or with entries from another new document;
  - non-colliding entries are appended with no model call;
  - collision groups go to one or more small calls asking only which new
    entries are duplicates (`task=dated_merge_<item_type>`,
    `is_generation_error()` checked);
  - on failure, the `(date, lower(title))` rule applies;
  - every prior entry is kept verbatim, undated ones included; `max_keep`
    applies to new entries only.

### Phase 1, step 13: per-document dated extraction
- **Cache:** `_workspace_doc_dated_extractions` (**new**
  `PersistentWorkspaceDict(CacheKinds.DOCUMENT_DATED_EXTRACTIONS)`), shape
  `{documentId: {"contentKey", "extractorVersion", "listOfDates",
  "proceduralTimeline"}}`. `contentKey` is a hash of the stored content, so a
  re-read forces re-extraction. It follows relevant parties' per-document
  provenance model.
- **List of Dates:** each new document goes through
  `_generate_list_of_dates_from_documents([doc], annexure_index_by_doc_id=...)`.
  Per-document grounding, the factual/procedural filter and year snapping are
  unchanged. Documents over `DATED_EXTRACTION_SEGMENT_CHARS` are split on
  `--- Page N ---` markers; nothing is cut.
- **Procedural Timeline:** **new** `procedural_timeline_agent.extract_documents`
  returns validated per-document entries without consolidation. Long documents
  are segmented instead of front-cut. Consolidation runs over the new entries
  only, chunked by date range so no call exceeds its output cap.
- **Bounding:** all per-document calls take `background_llm_slot()`.
- **Assembly:** prior panel (live documents only) + entries of new documents →
  step-12 merge → sort → existing tagging → emit. On a first build the prior is
  empty, so every document's entries are merged and none are dropped.
- **Upload cost:** N new documents → N calls (plus segments), concurrent; zero
  for older documents.
- **Deletion:** the existing fast paths filter entries by `sourceDocId`; the
  cache entry is dropped in the same branch.
- **Logging:** per-document lines log the document id prefix, not the name.
- **Rollback:** `PER_DOCUMENT_DATED_EXTRACTION=false`.

### Phase 2, step 1: persist the objective in force
- **New** `CacheKinds.OBJECTIVE_IN_FORCE`, held in
  `_workspace_objective_in_force` (`PersistentWorkspaceDict`), shape
  `{"objective", "source": "lawyer", "setAt", "revision"}`.
- Recorded by `_record_objective_in_force` (**new**) at `strategy.py:1663`
  (both modes), `:2173` and `analysis_report.py:2274`. All of these are behind
  `mutations_allowed`.
- Hydrated, swept and deleted with the workspace. Cleared on an Edit-Workspace
  role change (open question 5).

### Phase 2, step 2: Strategy delta for 1-5 documents
- **Eligible when all of these hold:**
  - `INCREMENTAL_UPDATE_ENABLED`;
  - an upload with `batch_result`;
  - a cached strategy with a `caseSummary`;
  - the same resolved role;
  - no deletion, restore or reload in the run;
  - new documents ≤ `INCREMENTAL_MAX_NEW_DOCS` and new characters ≤
    `INCREMENTAL_MAX_NEW_CHARS`.

  Otherwise the full rebuild runs (accepted to be slow for large uploads).
- The base is read after the lock, together with the objective `revision`.
- **New** `_generate_strategy_delta`:
  - inputs: the current summaries, objective and next steps; the new documents
    via `fit_to_budget`; `_build_document_inventory`; user facts, custom
    instructions, the confirmed party;
  - the objective is copied, never generated.
- **Guards:** `find_dropped_case_figures`; `find_ungrounded_proper_nouns`
  against the previous summary, new text and entities; the mandated openings;
  then `_clamp_annexure_references`, `snap_years_in_text`, `scrub_app_brand`.
  Any failure → full.
- **Full rebuild:** with an objective in force, an "OBJECTIVE IN FORCE" block
  goes into the prompt and the objective is set deterministically afterwards.
- Log: `strategy mode={} reason={}`.

### Phase 2, step 3: Analysis Report delta for 1-5 documents
- Same eligibility.
- **New** `_generate_analysis_report_delta` returns additions, `resolvedIds`
  and score impact. Applied deterministically:
  - approved and user-added items are never dropped (the guard of
    `_llm_resolve_answered_missing_info`, `objective_change.py:1043-1056`);
  - rejected items are not re-added;
  - `_apply_removed_evidence_missing_info` runs;
  - tagging runs.
- The score explanation is regenerated with the current objective and
  `existing=""`.
- On every path, lawyer-added missingInfo items the report lost are
  re-appended.

### Phase 2, step 4: interleaving with objective changes
- Before the strategy write, compare `revision` with the value read at the
  start. If it changed, take objective, next steps and chips from the cache.
  Keep the generated summaries only if they pass the guards against the
  current cached summaries.
- Combined with O3, an objective change during an upload ends with the chat
  objective, the upload's documents in the summaries, and the cascade's
  panels.

### Phase 2, step 5: fallback reasons
`disabled`, `no_cache`, `role_changed`, `deletion`, `restore`, `reload`,
`too_many_docs`, `too_large`, `model_error`, `guard_figures`, `guard_nouns`,
`guard_opening`, `parse_error`.

## Checklist against CLAUDE.md
- **Role gate:** unchanged resolution through `_resolve_workspace_role`. Delta
  needs the same role. A role change takes the full path and clears the
  objective in force. No literal role is introduced.
- **Long-running work:**
  - pipelines and cascades remain single tasks spawned through
    `_track_background_task`;
  - concurrency inside them uses `asyncio.gather` (the children inherit the
    run scope);
  - per-document results are persisted;
  - reconnect replay is unchanged.
- **Armed loaders / `CancelledError`:**
  - every runner and step catches `CancelledError` first, logs, marks the gate
    and re-raises;
  - upload runs release unemitted armed loaders; superseded runs release
    nothing;
  - with a cascade pending, the upload leaves the four cascade features to the
    cascade, whose terminal release covers them on failure;
  - the cascade's own release (`:2463-2481`) is unchanged;
  - Phase -1: every failed tail step, a cancelled or finished upload run, a
    `process_batch` crash and an all-failed upload release their armed
    features from cache through `_release_refresh_loaders`;
  - no pipeline-lock holder awaits an unbounded external round trip; the
    watchdog reports any hold past 5 minutes;
  - a run that passes its hard cap (900 s, audio-extended) is cancelled, its
    unemitted armed loaders are released from cache, and the lock is freed.
- **Stop fence:** all emits go through `_emit_document_feature` or
  `self._socket_client`; the gate and semaphores hold no client. Under a stop,
  the cascade's children are cancelled with the cascade task; held objective
  changes behave as today.
- **Model calls:** all through the router.
  - Stable `task=` keys: `dated_merge_list_of_dates`,
    `dated_merge_procedural_timeline`, `strategy_delta`,
    `analysis_report_delta`.
  - `is_generation_error()` is checked on each new call and on the re-ground
    call.
  - `GATE0_ENFORCE` is unaffected (no new provider path); a block is not
    retried elsewhere.
- **Paid calls metered:** no new call site below the router or `ocr._call`.
  Contexts are copied through every executor and semaphore, so costs land on
  the right case.
- **Purge:** new handler dicts are keyed by the workspace id and swept by
  `_clear_workspace_memory`: `_workspace_document_pipeline` (Task values, so
  they are cancelled), `_workspace_document_pipeline_state`,
  `_workspace_cascade_pending`, `_workspace_objective_in_force`,
  `_workspace_doc_dated_extractions`. The persisted kinds are deleted by
  `delete_workspace`. The scroll and signals memos are dropped by
  `delete_workspace`. Semaphores and executors hold no workspace data. Purge
  code is unchanged.
- **Ingestion/deletion paths:** no new path. Deletion branches also drop the
  per-document dated entries. Removed-evidence gaps apply in every analysis
  mode.
- **Chat writes gated on `mutations_allowed`:** the objective is recorded only
  on already-gated paths.
- **Logging:** new lines carry names, counts, durations and fixed tokens. Fixed
  leaks:
  - the metadata `description[:50]` line;
  - model output in `_log_objective_regen_failure`;
  - figures and proper nouns in the re-frame and re-ground warnings;
  - document names in the timeline agent.

  `tests/offline/verify_logging.py` must pass.
- **Indian law:** laws and case laws generation and normalisation unchanged.
- **Re-frame guards:** unchanged in the cascade and the chat turn, and reused in
  delta.

## Risks and rollback
| Risk | Mitigation / switch-off |
|---|---|
| The 900 s run cap cuts a legitimately slow run (large case, sequential tail before Phase 1, model retries) | Loaders still settle from cache; the CRITICAL line names the stage; confirm the cap with Phase 0 timings; raise `PIPELINE_RUN_TIMEOUT_SECONDS` |
| Audio duration unknown | Cap falls back to `PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS` |
| Template library not ready within the bound | Drafting matches against the current store; the late snapshot re-emits drafting (`template_sync.py:404-408`); `TEMPLATE_SYNC_TIMEOUT_SECONDS` raises the bound |
| Releasing from cache shows stale panels after a failure | Correct by design: a failed step must not leave a spinner; the next successful run replaces them |
| Backend reads the removed strategy `error` field | Confirm before merge (see "Event and payload changes") |
| CPU saturation on 2 vCPU makes chat slow | `cpu_slot` (2), lag monitor; lower `DOC_PROCESSING_CONCURRENCY`; process pool if needed (open question 4) |
| Default executor starved by extraction | Dedicated `doc_extract_executor` |
| Memory from rendered pages | Render inside `cpu_slot`; bounded queues |
| Gemini/Vision quota at 8 fan-out calls + 12 Vision calls | `BACKGROUND_LLM_CONCURRENCY`, `DOC_PROCESSING_CONCURRENCY` (open question 3) |
| Concurrent cascade overloads Indian Kanoon | Two concurrent fetch sequences per workspace; `PIPELINE_CONCURRENT_PANELS=false` (open question 8) |
| Upload skips panels and the cascade then fails | Cascade preserve fallbacks + terminal release; `CASCADE_SKIPS_UPLOAD_PANELS=false` |
| Ordered gate deadlock | `done` in `finally`; AC-6, AC-7, AC-89 |
| Superseded documents lost | Batch absorption (AC-8) |
| Billing attribution under concurrency | AC-21, AC-66 |
| PyMuPDF differences | Human-run gate; fallback; `PDF_RENDER_ENGINE=poppler`; licence open question 7 |
| Stale scroll or signals memo | `evidence_version` key; legacy bump; copies; drop on delete and run end |
| Delta drift | Guards; 1-5 documents only; `INCREMENTAL_UPDATE_ENABLED=false` |

## Test plan
- **New** `tests/offline/verify_incremental_update.py`: fake socket (`emit`
  returns `True`), fake vector store with `evidence_version`, fake
  extraction/OCR/model with latencies and peak counters, fake usage recorder,
  fictional data only. Groups map to the AC IDs.
- **New** `tests/test_concurrency_limits.py` (semaphores, `cpu_slot` across
  threads, `run_cpu` context copy, executor isolation, lag monitor).
- **New** `tests/test_dated_merge.py`.
- Extend `tests/test_ocr.py` (PyMuPDF, fallback, render inside `cpu_slot`).
- Existing suites unchanged and passing: `verify_objective_change.py` (all
  groups, including 11 and 12), `verify_run_control.py`,
  `verify_missing_info_edit.py`, `verify_document_reload.py`,
  `verify_workspace_lifecycle.py`, `verify_workspace_share.py`,
  `verify_fact_corrections.py`, `verify_ingestion.py`, `verify_logging.py`,
  `verify_rag.py`, `tests/test_tables.py`, `tests/test_usage_ledger.py`,
  `tests/test_llm_router.py`.
- `make lint`, `make test`.
- Human-run, billed:
  - `scripts/compare_pdf_render.py --ocr`;
  - timing on an e2-highmem-2 for (a) and (b) (AC-79, AC-95).
- **Phase -1:**
  - **offline, with fakes:** AC-100 to AC-112, AC-115 and AC-116 to AC-120
    (fake clock for the caps) run in the `VIU` groups
    `stuck_*`, with a fake socket that can drop `request_template_sync` or
    never answer it;
  - **needs `make e2e` / a live backend:** AC-113 (template sync round trip
    against the real backend) and AC-114 (disconnect during an upload against
    the local stand-in backend).
- **`make e2e` required before deploy** of every PR, run by a human (billed).

## Implementation tasks
Phase -1 (PR 0, ships first):
- -1.1 Bounded `ensure_templates_synced`; emit result read; invalid-reply
  resolution; `_prewarm_templates`; drafting `templates_ready`. Tests.
- -1.2 `_hold_pipeline_lock`, `_set_pipeline_stage`, the 5-minute watchdog;
  all six lock sites converted. Tests.
- -1.2b Hard cap (S8): `asyncio.timeout` in the three runners, cap
  computation with audio extension, `audio_duration_seconds` in
  `audio_extras`, CRITICAL log + audit event + release on timeout. Tests with
  a fake clock.
- -1.3 Emit observer on the upload and dates/laws runners; per-step release
  in the tail (S3). Tests.
- -1.4 `CancelledError` and `finally` release in both runners (S4); strategy
  settles from cache (S5). Tests.
- -1.5 Sync branches (S6a, S6b). Tests.
- -1.6 Stage timing lines (S7, shared with Phase 0 task 1).
- -1.7 CHANGELOG ("panels no longer stick; a failed upload no longer replaces
  existing panels"); CLAUDE.md rule on the pipeline lock.

Phase 0 + 1 (PR 1):
1. Stage lines (upload, objective path, cascade) and the lag monitor;
   baselines.
2. `app/core/concurrency.py`: semaphores, `cpu_slot`, `run_cpu`,
   `doc_extract_executor`; wire into `process_batch`, `process_document`,
   OCR render, `add_document_chunks`. Tests.
3. `CancelledError` handling and terminal emits in the upload runner.
4. `_OrderedEmitGate`; concurrent upload tail.
5. Supersede registry, `origin=`, batch absorption.
6. Scroll and signals memos; remove the second scroll and `skip_reprocess`.
7. Entity cache by document set (strategy, analysis, objective regen).
8. Concurrent cascade (O1), cascade-pending skip (O3), chat-path fixes (O4,
   O6).
9. Matching `gather`; metadata into the pipeline.
10. `dated_entry_day`, `_merge_dated_entries_incremental`.
11. Per-document dates and timeline: cache, segmentation, agent
    `extract_documents`, assembly, Annexure numbering, bounded fan-outs
    (including relevant parties).
12. PyMuPDF rendering + `scripts/compare_pdf_render.py`.
13. Docs: `env.example`, `CHANGELOG.md`, CLAUDE.md.

Phase 2 (PR 2):
14. `OBJECTIVE_IN_FORCE`: store, hydration, recording, role-change clear,
    full-rebuild enforcement.
15. Strategy delta (1-5 documents), revision check, reasons.
16. Analysis delta; user-added re-append.
17. Docs: CHANGELOG, CLAUDE.md objective-change section, comment at `:2032`.

## Decisions
- 2026-10-27, case owner: the priority three (strategy, analysis, drafting)
  are sent first; the other five generate in parallel from t=0 and are held
  until the three are sent. Drafting runs right after analysis and, on a first
  build only, once more at the end. This replaces the strict emit order of
  step 2. Ingest concurrency, a knowledge-graph limit and the log-privacy fix
  for names in chronology titles and file names follow separately.
- Re-prioritised by the user: (a) 1-5 PDF uploads and (b) objective changes
  first; large first builds may stay slow.
- Moved to a follow-up ticket: progressive assessments, quota-aware limiters
  (Vision limit, router lanes, per-workspace fairness, Indian Kanoon limit),
  map-reduce for Strategy/Analysis, delta folds for larger uploads, overlapping
  per-document work with ingestion, the search cache, signals from every
  document.
- Simple process-wide semaphores replace the per-batch `INGEST_CONCURRENCY`.
- Plan step 10 (replay guard) and the PyPDF2 text-layer swap are dropped.
- The objective store keeps the objective, not the chat instruction.
- Phase -1 as implemented (2026-10-26):
  - The Phase -1 checks live in their own suite,
    `tests/offline/verify_stuck_loaders.py` (groups named as in the AC table),
    not in `verify_incremental_update.py`, so the first PR ships independently.
  - The dates/laws runner has no arming information from its callers
    (deletion, restore, reload, sync helper), so on a cancel, the cap or a
    normal exit it releases its own tail features
    (`PIPELINE_TAIL_FEATURES`) not yet emitted, not an "armed" list. The upload
    runner releases only the `armed_features` its caller passed.
  - The cascade runner now also installs the emit observer on its `emitted`
    set, so the cap settlement and the scheduler's terminal release skip
    every panel that reached the socket layer (no double emit, AC-117).
  - Stage names: the upload uses `lock_wait`, `ingest`, `metadata`,
    `strategy`, `analysis_report`, the six tail stages and `pipeline_total`
    (`mode=sequential`); the dates/laws runner adds `dates_laws_total`; the
    cascade logs `objective_cascade_*` lines (lock wait, analysis, quality
    explanation, tagging, dates relevance, laws, case laws, drafting, total).
    The chat-turn `objective_*` lines stay in Phase 0.
  - The `description[:50]` log line in the sync metadata emit is left for
    Phase 1 step 6, where that code moves.
