# SIM-163: Fast follow-up uploads and objective changes, objective-safe — acceptance criteria

- **Requirements:** `specs/requirements/SIM-163-incremental-update.md`
- **Design:** `specs/technical-design/SIM-163-incremental-update.md`

Each criterion is testable and maps to exactly one automated check, or is
marked **manual** with the reason. Unless stated otherwise, checks live in the
**new** `tests/offline/verify_incremental_update.py` (`VIU`). It uses:
- a fake socket whose `emit` returns `True` and records `(event, featureType)`;
- an in-memory fake vector store with `evidence_version`;
- fake extraction, OCR and model calls with configurable latencies and
  in-flight peak counters;
- a scripted `gemini_client` keyed by `task=`, with per-document call tags;
- a fake usage recorder;
- fictional case data only.

IDs are stable. Criteria withdrawn by the re-prioritisation are listed at the
end.

## Phase -1: stuck loaders (ships first)
The "Kind" column says whether a check runs offline with fakes or needs the
human-run `make e2e` / a live backend.

| ID | Given | When | Then | Kind | Verified by |
|---|---|---|---|---|---|
| AC-100 | a user with zero templates; a fake socket that never answers `request_template_sync` | an upload pipeline reaches drafting | drafting proceeds after `TEMPLATE_SYNC_IN_LOCK_WAIT_SECONDS` (the prewarm keeps waiting outside the lock, bounded by `TEMPLATE_SYNC_TIMEOUT_SECONDS`); `drafting_assistant` is emitted; the pipeline lock is released; a second upload and a reconnect replay (`_locked_reemit`) then run and emit | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_sync_no_answer` |
| AC-101 | the fake socket returns `False` for `request_template_sync` | `ensure_templates_synced` runs | it returns at once with `request_template_sync not delivered`; no future left pending | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_sync_undelivered` |
| AC-102 | a pending template-sync request | a `template_sync_response` arrives with an invalid payload | the pending future is resolved; the waiter proceeds | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_sync_invalid_reply` |
| AC-103 | a snapshot arrives after the bound expired | `on_template_sync_response` ingests it | drafting is re-emitted via `_maybe_reemit_drafting_on_templates`, outside the pipeline lock | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_sync_late` |
| AC-104 | a chat drafting turn (`chat.py:606`) or `workspace_transferred` with no template reply; fake clock | the turn runs | it continues after `TEMPLATE_SYNC_TIMEOUT_SECONDS` (900 s) | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_sync_other_callers` |
| AC-105 | a pipeline holding the lock in stage `case_laws` past `PIPELINE_LOCK_WARN_SECONDS` (300 s, fake clock) | the watchdog ticks | one informational WARNING `pipeline lock held … holder=upload_pipeline stage=case_laws cap=900s`, rate-limited to once per 5 minutes; no content; the watchdog itself releases nothing | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_lock_watchdog` |
| AC-106 | each of list_of_dates, applicable_laws, procedural_timeline, relevant_parties, case_laws and drafting in turn raises inside the tail | the tail runs | that feature gets exactly one terminal `document_feature_response` (cached payload if any, else empty); later steps still run and emit | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_tail_step_failure` |
| AC-107 | a step emitted, then raised | the tail handles the exception | no second emit for that feature (emit observer) | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_no_double_emit` |
| AC-108 | an upload pipeline cancelled (not superseded) after strategy emitted | the runner unwinds | every other armed feature gets a terminal emit from cache; the cancellation is logged and re-raised | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_runner_cancelled` |
| AC-109 | the dates/laws runner cancelled mid-tail | it unwinds | its unemitted features are released from cache; re-raised | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_dates_runner_cancelled` |
| AC-110 | `_generate_strategies_from_documents` returns `None` / raises (cached strategy exists; and a first build without one) | the upload runner runs | `strategy_objective` is emitted with the cached payload (fallback on a first build) through `_emit_document_feature`; no message carries an `error` field or exception text; later panels emit | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_strategy_failure` |
| AC-111 | an existing case; 9 loaders armed by `ai_update_started` | `process_batch` raises | the `document_assessments` error emit is kept; the other 8 features each get one terminal emit from cache; caches unchanged | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_ingest_crash` |
| AC-112 | an existing case with cached analysis and dates; every new file fails (`successful == 0`) | the sync completes | every armed feature (`strategy_objective` included) is re-emitted from cache unchanged; `_workspace_analysis_payload` and `_workspace_list_of_dates_payload` are unchanged; no full re-extract is scheduled. On a first build (nothing armed) today's fallback behaviour is unchanged | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_all_files_failed` |
| AC-113 | a real backend with a template library | an upload and an objective change via `make e2e` | `drafting_assistant` arrives with template matches; no `pipeline lock held` warning; every armed panel settles | **e2e** | **manual**: `make e2e` (billed, real round trip) |
| AC-114 | the local stand-in backend | the socket is dropped mid-upload and reconnects | every armed panel settles (buffered delivery or release); the next upload runs | **e2e** | **manual**: `make e2e` disconnect scenario |
| AC-115 | an upload with a deliberately slow fake case-laws step (300 s) | the pipeline runs | `stage case_laws took` shows the delay; the watchdog reports `stage=case_laws` if it passes the warning threshold; later panels still settle | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_slow_step_visible` |
| AC-116 | an upload run (no audio) whose fake case-laws step never returns; fake clock | the run passes `PIPELINE_RUN_TIMEOUT_SECONDS` (900 s) of lock hold | the run is cancelled; one CRITICAL `pipeline run exceeded cap=900s … stage=case_laws` plus a `pipeline_run_timeout` audit event (no content); every armed feature not yet emitted gets exactly one terminal emit from cache; the lock is free; a queued cascade, a reconnect replay and a resume then run | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_run_cap` |
| AC-117 | an objective-change cascade whose drafting step never returns; fake clock | it passes 900 s | same as AC-116 for the cascade's features; the scheduler's terminal release does not double-emit | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_cascade_cap` |
| AC-118 | a batch containing two audio results with `audio_duration_seconds` 1,200 and 600 | the upload run's cap is computed | cap = `min(900 + 1.0 × 1,800, 3,600)` = 2,700 s; the run is not cut at 900 s; it is cut after 2,700 s (fake clock); the template prewarm for that run uses the same cap | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_audio_cap` |
| AC-119 | a batch with one audio result whose duration is unknown (`None`) | the cap is computed | cap = `PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS` (3,600 s); the cap never exceeds that maximum for any durations | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_audio_cap_unknown` |
| AC-120 | a fake socket that accepts `request_template_sync` but never answers; fake clock | `ensure_templates_synced` runs with no explicit timeout | it returns after exactly `TEMPLATE_SYNC_TIMEOUT_SECONDS` (900 s), not before; logs a timeout warning; the pending future is removed. With a `False` emit it returns at once (AC-101) | fakes | `tests/offline/verify_stuck_loaders.py` group `stuck_template_wait_900` |

## Reject one Analysis Report item (ships on its own)
A reject used to remove every item sharing two informative words with the
rejected text, and the rejection memory then suppressed every later item
sharing them. Fictional items; no model, socket or Qdrant calls.

| ID | Given | When | Then | Kind | Verified by |
|---|---|---|---|---|---|
| AC-130 | four distinct Missing Information items, several sharing words ("copy", "booking", "email") | the lawyer rejects one | only that item is removed; the emitted `analysis_report` and the cache keep the other three | fakes | `tests/test_reject_matching.py::test_rejecting_one_gap_keeps_the_other_three`, `::test_one_reject_removes_one_item_not_its_word_neighbours` |
| AC-131 | the rejected text differs from the item only in case, spacing or punctuation | the reject is applied | the item is removed | unit | `tests/test_reject_matching.py::test_same_text_ignoring_case_and_punctuation_is_removed` |
| AC-132 | the same item text appears twice | it is rejected | both copies are removed, nothing else | unit | `tests/test_reject_matching.py::test_every_same_text_copy_is_removed` |
| AC-133 | items carried as `{"point": …}` objects | one is rejected | only that object is removed | unit | `tests/test_reject_matching.py::test_dict_items_match_on_point` |
| AC-134 | the report was regenerated and the item slightly re-worded since the lawyer saw it | the old wording is rejected | only the single closest item is removed | unit | `tests/test_reject_matching.py::test_a_near_identical_rewording_removes_only_the_closest_item` |
| AC-135 | a rejected value that is unrelated, or only a fragment of an item | the reject is applied | nothing is removed | unit | `tests/test_reject_matching.py::test_an_unrelated_or_partial_value_removes_nothing`, `::test_word_neighbours_score_zero` |
| AC-136 | one item was rejected | a later rebuild checks every item against the rejection memory | only the rejected item is suppressed | fakes | `tests/test_reject_matching.py::test_rejection_memory_suppresses_only_the_rejected_gap` |
| AC-137 | the reject path runs | it logs | no log line carries the item text (sizes only, via `chars`) | review | `analysis_report.py` reject log lines; `tests/offline/verify_logging.py` static scan |
| AC-140 | a rebuild re-worded an item the frontend already had (same id, different text) | the lawyer rejects the frontend's wording | our item with that id is removed and both wordings are remembered as rejected; with no currentState the text rules apply | fakes | `tests/test_reject_matching.py::test_reworded_item_is_resolved_by_id`, `::test_no_current_state_falls_back_to_text` |
| AC-141 | a Missing Information reject at 40% | the batch is applied | the score is 45% and the report sent carries `rejectionFeedback` with `scoreChange` 5, `isValidRejection` true, `scoreIncrease` 5 and the model's `reason`, matching `RejectionFeedback` | fakes | `tests/test_reject_matching.py::test_reject_reports_its_reason_and_increase` |
| AC-142 | a weakness reject / a risk reject | the batch is applied | the score rises by 5 / 3 | fakes | `tests/test_reject_matching.py::test_risk_or_weakness_reject_adds_its_section_weight` |
| AC-145 | five rejects in one batch | the batch is applied | every item's weight counts (no batch cap); a duplicate in the batch counts once | fakes | `tests/test_reject_matching.py::test_bulk_reject_counts_every_item`, `::test_duplicate_in_one_batch_counts_once` |
| AC-146 | items remain after a reject | the score is raised | it is capped at 95%; removing the last item reaches 100% | fakes | `tests/test_reject_matching.py::test_score_is_capped_at_95_until_the_last_item_goes` |
| AC-147 | the reason call fails | the batch is applied | the score still rises and a fixed reason is sent | fakes | `tests/test_reject_matching.py::test_reason_failure_still_raises_the_score` |
| AC-148 | a chat update resolves 1 missing item and removes 1 risk at 40% | the markers are applied | the score is 48% (same weights as a reject), capped at 95% while items remain | fakes | `tests/test_reject_matching.py::test_chat_marker_updates_use_the_reject_weights` |
| AC-149 | every Missing Information, weakness and risk item rejected | a document is uploaded and the report rebuilt | the analysis prompt lists the dismissed items; a section the rejection filter empties is refilled with new items (the refill is told what was dismissed); new items beside dismissed ones survive | fakes | `tests/test_rejected_items_regeneration.py` (incl. `::test_refill_that_repeats_dismissed_items_is_filtered_too`, `::test_refill_model_error_leaves_no_dismissed_item`) |
| AC-143 | a reject re-sent for an item already removed | the batch is applied | `reason` says it was already removed; `scoreChange` 0 | fakes | `tests/test_reject_matching.py::test_stale_reject_says_already_removed` |
| AC-144 | any reject | the report is sent | `rejectionFeedback` is on that emit only, never in the cached report (no replay) | fakes | `tests/test_reject_matching.py::test_reject_reports_its_reason_and_increase`, `::test_stale_reject_says_already_removed` |

## (a) Follow-up upload: pipeline, ordering, supersede

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-1 | a workspace with cached panels | a `workspace_sync` upload runs | one `stage <name> took` line per stage plus `pipeline_total`, each with the run's `request_id`, none containing document or objective text | `VIU` group `stage_timing` |
| AC-2 | concurrent panels on; the tail panels scripted to finish before strategy, at staggered times | the upload pipeline runs | each panel is emitted the moment it finishes, in completion order; nothing waits for the priority lane | `tests/test_parallel_panels.py::test_ready_panels_are_sent_at_once`, `::test_each_panel_is_sent_when_it_finishes` |
| AC-3 | concurrent panels on | the upload pipeline runs | dates, laws, timeline, parties and case laws generation overlap strategy generation | `tests/test_parallel_panels.py::test_tail_overlaps_strategy` |
| AC-4 | `PIPELINE_CONCURRENT_PANELS=false` | the upload pipeline runs | sequential generation in product order, drafting once | `tests/test_parallel_panels.py::test_sequential_flag` |
| AC-5 | a first build (`is_update=False`) | the upload pipeline runs | drafting runs after analysis and once more after the tail; on an update it runs once | `tests/test_parallel_panels.py::test_first_build_refreshes_drafting`, `::test_update_drafts_once` |
| AC-6 | applicable_laws raises, or strategy raises | the pipeline runs | the other panels still emit; the failed panel gets a terminal emit; nothing waits forever | `tests/test_parallel_panels.py::test_failed_tail_panel_settles`, `::test_lane_failure_does_not_block_other_panels`; strategy raising in parallel mode: `tests/offline/verify_stuck_loaders.py` group `stuck_strategy_failure` |
| AC-7 | `_generate_strategies_from_documents` returns `None` | the upload pipeline runs | `strategy_objective` gets a terminal emit; later panels emit; no armed loader left lit | `VIU` group `strategy_none_releases` |
| AC-8 | pipeline A (documents X) running | an upload (documents Y) schedules pipeline B | A is cancelled; B covers X and Y in dates, timeline and (Phase 2) strategy/analysis | `VIU` group `supersede_absorbs_batch` |
| AC-9 | pipeline A superseded mid-run | A handles its cancellation | A emits no `document_feature_response`; B emits every armed feature once | `VIU` group `supersede_no_release` |
| AC-10 | an objective-change cascade holds or waits on the lock | an upload schedules a document pipeline | the cascade is not cancelled and completes | `VIU` group `cascade_not_superseded` |
| AC-11 | a document pipeline running | cancelled for a non-supersede reason | logs, releases its unemitted armed loaders, re-raises | `VIU` group `cancel_releases` |
| AC-12 | `PIPELINE_SUPERSEDE_ENABLED=false` | a second upload arrives | it waits on the lock; nothing cancelled | `VIU` group `supersede_flag` |
| AC-13 | unchanged documents, graph on or off | strategy and analysis each request entities | the three entity sub-extractors run once in total (document-set key) | `tests/test_parallel_panels.py::test_entity_cache_document_set` |
| AC-14 | an upload pipeline | it runs end to end | the store is scrolled once per `evidence_version`; later calls get equal copies; the memo is released at run end | `VIU` group `single_scroll` |
| AC-15 | a memoised scroll | `add_document_chunks`, `add_document`, `delete_documents` or `delete_workspace` runs | the next call reads the store again | `VIU` group `scroll_invalidation` |
| AC-16 | a caller mutates a returned document dict | the next caller reads the memo | it sees unmodified content | `VIU` group `scroll_copy` |
| AC-17 | the first sync scroll raises once | `workspace_sync` with one new file | retried; the file is processed once; no "skip re-process" re-emit or full-corpus date re-extraction | `VIU` group `scroll_retry` |
| AC-18 | an analysis report and a `currentState` | tagging runs | the four reconcile calls overlap; result equals the sequential one | `VIU` group `matching_gather` |
| AC-19 | `workspace_metadata` cached with a category | an upload | no category call; no `workspace_metadata` emit; the handler awaits no panel model call | `VIU` group `metadata_skip` |
| AC-20 | no cached category | an upload | `workspace_metadata` emitted by the pipeline before `strategy_objective`; its log line has no description text | `VIU` group `metadata_in_pipeline` |
| AC-30 | a cached List of Dates; new dates collide with no prior entry within ±1 day nor with another new document | a 1-document upload | **zero** dated-merge calls; prior entries kept verbatim; new entries appended | `VIU` group `dated_merge_no_collision` |
| AC-31 | one new entry on the same day as two prior entries (of 50) | list_of_dates assembles | exactly one dated-merge call whose prompt holds only those two prior entries and the new one | `VIU` group `dated_merge_collision_subset` |
| AC-32 | the merge model returns an error sentinel or bad JSON | list_of_dates assembles | `is_generation_error()` checked; `(date, title)` fallback; all prior entries kept | `VIU` group `dated_merge_failure` |
| AC-33 | prior entries include undated ones and more than `max_keep` | a merge runs | every prior entry kept | `VIU` group `dated_merge_keeps_all_prior` |
| AC-34 | the inputs of AC-30 and AC-31 | procedural_timeline assembles | same results; consolidation over new entries only | `VIU` group `timeline_merge` |
| AC-35 | full, partial and unparseable dates; ±1-day, month and year edges; cross-document groups | `dated_entry_day` and grouping run | collisions as designed | `tests/test_dated_merge.py` |
| AC-36 | a case with four documents | a fifth is uploaded | its prompt header is `Annexure 5` and the reference maps back to its id | `VIU` group `annexure_numbering` |
| AC-79 | a fictional 15-document case on an e2-highmem-2 | uploads of 1 text PDF, 5 text PDFs and 5 scanned PDFs via `make run`, before and after each phase | stage lines recorded; the targets in the requirements met (1 PDF ≤ 90 s, 5 PDFs ≤ 150 s after Phase 2; `document_assessments` ≤ 30 s for one text PDF) | **manual**: real backend, billed calls, production-sized VM |

| AC-138 | 6 documents, `LIST_OF_DATES_CONCURRENCY=4` | the temporal pass and the per-date dedupe run | at most 4 calls run at once, and the merged result equals the sequential result | `tests/test_parallel_panels.py::test_dates_pass_bounded_same_result`, `::test_dedupe_bounded_same_result` |
| AC-139 | concurrent panels on | the run is cancelled (stop or cap) while tail panels wait on the hold | every child is cancelled and nothing held reaches the wire; the runner releases unemitted armed loaders | `tests/test_parallel_panels.py::test_cancel_cancels_children`; `tests/offline/verify_stuck_loaders.py` group `stuck_runner_cancelled` (runs in parallel mode) |

## (b) Objective change from chat

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-86 | concurrent panels on; cached panels; a new objective in the strategy cache | the cascade runs | analysis regeneration, date relevance, laws and case laws overlap; emits arrive `analysis_report` → `list_of_dates` → `applicable_laws` → `procedural_timeline` → `relevant_parties` → `case_laws` → `drafting_assistant`; drafting starts after analysis, dates and laws are cached | `VIU` group `cascade_concurrent_ordered` |
| AC-87 | unchanged documents | an objective change runs end to end (chat turn + cascade) | zero entity sub-agent calls after the first; one store scroll; laws and case laws share one signals computation | `VIU` group `cascade_reuse` |
| AC-88 | applicable laws raises in the cascade | the cascade runs | the laws preserve fallback emits; other panels unaffected; order kept; every armed feature gets a terminal emit | `VIU` group `cascade_step_failure` |
| AC-89 | the cascade's analysis step is cancelled (stop) | the cascade unwinds | children are cancelled; the gate does not hang; the scheduler's terminal release runs as today (fenced under stop) | `VIU` group `cascade_cancel` |
| AC-90 | an upload pipeline running when the lawyer confirms an objective change | the cascade is scheduled | the upload skips `analysis_report`, `applicable_laws`, `case_laws`, `drafting_assistant` (logged `skipped reason=cascade_pending`) and still emits strategy, dates, timeline and parties for the new documents; the cascade then emits the four features, covering the new documents; no upload `analysis_report` is emitted after the cascade's; every armed feature gets a terminal emit | `VIU` group `cascade_preempts_upload_panels` |
| AC-91 | `CASCADE_SKIPS_UPLOAD_PANELS=false` | the same as AC-90 | the upload runs every panel; the cascade runs after it | `VIU` group `cascade_skip_flag` |
| AC-92 | the re-ground call returns an error sentinel | `_regenerate_strategy_for_new_objective` runs | `is_generation_error()` checked; the first-pass objective lands; no exception | `VIU` group `reground_error` |
| AC-93 | a re-frame dropping a figure, an ungrounded noun, a model give-up | the objective path logs | log and audit lines carry counts, reasons and sizes only: no figure values, proper nouns or model output | `VIU` group `objective_logs_no_content` |
| AC-94 | the existing objective-change suite | it runs after the change | all groups pass, including re-frame guards, "objective lands regardless", role reassignment and the surgical edit | existing `tests/offline/verify_objective_change.py` |
| AC-95 | a fictional 15-document case on an e2-highmem-2 | an objective change via chat, idle and during a 1-PDF upload | targets met: confirmation → `strategy_objective` ≤ 75 s; then every cascade panel terminal ≤ 90 s; with an upload, the cascade starts ≤ 15 s after the upload's strategy step | **manual**: real backend, billed calls, production-sized VM |

## Running on 2 vCPU

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-21 | 300 fake files with scripted OCR units, tokens and embed tokens | `process_batch` runs with defaults and again with every limit at 1 | the ingest scope totals and the workspace ledger are identical | `VIU` group `ingest_billing_equal` |
| AC-22 | 300 fake files, some failing | `process_batch` runs concurrently | results in input order; failures are failed results, not exceptions | `VIU` group `ingest_order_and_failure` |
| AC-23 | two workspaces uploading 20 fake files each at once | both batches run | in-flight `process_document` never exceeds `DOC_PROCESSING_CONCURRENCY` in total | `VIU` group `ingest_bound` |
| AC-62 | a 300-document fake first build with latencies | it runs end to end | it completes; peaks never exceed `DOC_PROCESSING_CONCURRENCY`, `CPU_WORKERS` or `BACKGROUND_LLM_CONCURRENCY`; every document is in the manifest | `VIU` group `scale_300_bounded` |
| AC-63 | `BACKGROUND_LLM_CONCURRENCY` saturated by per-document fan-outs | a `user_message_request` and an objective-change cascade run | their model calls start without waiting on the background semaphore | `VIU` group `chat_not_queued` |
| AC-64 | three documents in processing with fake CPU-bound steps | ingestion runs | readability, redaction, chunking, sparse encoding and page rendering run through `run_cpu` / `cpu_slot` (none on the event-loop thread); concurrent CPU tasks never exceed `CPU_WORKERS` across threads | `tests/test_concurrency_limits.py::test_cpu_bound_steps_bounded_and_off_loop` |
| AC-65 | long fake extractions occupying `doc_extract_executor` | a Qdrant call goes through `asyncio.to_thread` | it runs immediately on the default executor (not queued behind extraction) | `tests/test_concurrency_limits.py::test_extraction_does_not_starve_default_executor` |
| AC-66 | two workspaces ingest and extract concurrently | both finish | each ledger holds exactly its own units and tokens; totals equal two sequential runs | `VIU` group `per_workspace_billing` |
| AC-96 | scanned PDFs rendered by three documents at once | OCR runs | decoded page images alive at once never exceed `CPU_WORKERS`; the Vision request runs outside `cpu_slot` | `tests/test_ocr.py::test_render_inside_cpu_slot_bounds_images` |
| AC-97 | the lag monitor running | the loop is blocked for longer than `EVENT_LOOP_LAG_WARN_MS` | one `event_loop_lag` warning, rate-limited, with no content | `tests/test_concurrency_limits.py::test_lag_monitor_warns_once` |
| AC-98 | a 5-PDF scanned upload on an e2-highmem-2 | a chat question is asked mid-upload | lag lines stay below 250 ms; the reply time is within 20% of an idle reply | **manual**: production-sized VM, billed calls |
| AC-24 | an image-only PDF, fake Vision | OCR with `PDF_RENDER_ENGINE=pymupdf` | pages rendered from one opened document; rules and marks unchanged; no `pdftoppm` call | `tests/test_ocr.py::test_pymupdf_render_path` |
| AC-25 | PyMuPDF raises on open or a page | OCR runs | whole-document poppler fallback; reads as before | `tests/test_ocr.py::test_pymupdf_failure_falls_back_to_poppler` |
| AC-26 | a `native_page_jpeg` page | OCR runs with either engine | the native JPEG is sent | `tests/test_ocr.py::test_native_jpeg_rule_unchanged_with_pymupdf` |
| AC-27 | the local PDF corpus | `scripts/compare_pdf_render.py --ocr` | 0 regressions (≤2% OCR character loss per page, none unreadable, same native/render decisions) | **manual**: billed Vision calls, local PDFs |

## (c) Dates at scale (correctness)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-67 | a 300-document first build, each with a unique planted date | list_of_dates assembles | every planted date is in `listOfDates`, attributed to its document | `VIU` group `dates_no_starvation_first_build` |
| AC-68 | a 50-document case with cached per-document extractions | a 40-document upload | exactly 40 list_of_dates and 40 timeline extraction calls (tagged by new ids); zero for older documents | `VIU` group `upload_40_calls_only_new` |
| AC-69 | a document longer than `DATED_EXTRACTION_SEGMENT_CHARS` with a date on its last page | both dated panels extract | the date appears (no front cut) | `VIU` group `long_document_segments` |
| AC-70 | cached entries for documents P and Q | P deleted; Q re-read | P's entries leave both panels and its cache entry is dropped; Q re-extracted once | `VIU` group `per_doc_cache_delete_reload` |
| AC-71 | persisted per-document caches; restart | a 1-document upload | no extraction calls for older documents | `VIU` group `per_doc_cache_restart` |

## Phase 2: objective safety and delta (1-5 documents)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| AC-37 | a lawyer sets an objective in chat (edit permission) | it lands | `OBJECTIVE_IN_FORCE` holds it with a bumped `revision`; persisted; hydrated cold | `VIU` group `objective_recorded` |
| AC-38 | an objective in force; uploads of 1 and of 5 documents | the pipeline runs | `strategy mode=delta`; `strategicObjective` byte-identical to the one in force; analysis uses it | `VIU` group `delta_keeps_objective` |
| AC-39 | an objective in force; an upload of 6 documents (or over `INCREMENTAL_MAX_NEW_CHARS`) | the pipeline runs | `mode=full reason=too_many_docs` (or `too_large`); `strategicObjective` is the one in force | `VIU` group `full_keeps_objective` |
| AC-40 | no objective recorded | a full rebuild | behaviour equals today's | `VIU` group `full_without_objective` |
| AC-41 | a delta drops a figure / adds an ungrounded noun / loses the opening | guards run | `mode=full` with `guard_figures` / `guard_nouns` / `guard_opening` | `VIU` group `delta_guards` |
| AC-42 | the delta call errors, times out or is blocked | strategy delta runs | `reason=model_error`, full rebuild; no retry elsewhere for a block | `VIU` group `delta_model_error` |
| AC-43 | no cache / role change / deletion / restore / reload | the pipeline runs | matching reason token | `VIU` group `fallback_reasons` |
| AC-44 | an upload has read its base | a chat objective change lands before the strategy write | the emitted and cached objective is the chat one | `VIU` group `objective_change_mid_upload` |
| AC-45 | a chat change landed before the upload took the lock | delta runs | the new objective is the base | `VIU` group `objective_change_before_upload` |
| AC-46 | approved, lawyer-added and edited missingInfo items | a delta marks them resolved | they remain with ids; edited wording kept | `VIU` group `delta_keeps_curated` |
| AC-47 | a lawyer-added item | a full rebuild drops it | it is re-appended | `VIU` group `full_keeps_user_added` |
| AC-48 | a rejected item and a removed document | delta analysis runs | rejected item absent; removed-evidence gap present | `VIU` group `delta_rejected_and_removed` |
| AC-49 | an eligible upload | delta analysis runs | score explanation regenerated with the current objective, `existing=""` | `VIU` group `delta_score_explanation` |
| AC-50 | an Edit-Workspace role change | the sync is handled | objective in force cleared; next pipeline `reason=role_changed` | `VIU` group `role_change_clears_objective` |
| AC-51 | a chat role reassignment | it lands | objective in force updated, not cleared | `VIU` group `chat_reassign_keeps_objective` |

## Negative and edge cases
| ID | Case | Expected | Verified by |
|---|---|---|---|
| AC-52 | **Duplicate `requestId` / replayed `workspace_sync`** within 180 s | swallowed: no pipeline, no supersede | `VIU` group `duplicate_sync` |
| AC-53 | Replay after 180 s, same files | no pipeline, nothing superseded, no extraction calls | `VIU` group `late_replay` |
| AC-54 | Curation-only resend | `currentState` cached; no pipeline; no model call | `VIU` group `curation_only` |
| AC-55 | **Stop** mid-upload-pipeline | children cancelled; no fenced emit; gate does not hang; next run unaffected | `VIU` group `stop_mid_pipeline` |
| AC-56 | Stop, then a new upload | new run emits normally | `VIU` group `stop_then_upload` |
| AC-57 | **Socket disconnect**: `emit` returns `False` for analysis_report (upload or cascade) | turn passes; later panels emit; envelope buffered and re-sent on reconnect before the cached replay | `VIU` group `disconnect_buffered` |
| AC-58 | Reconnect replay during a running pipeline or cascade | replay waits on the lock and replays final caches | existing `verify_run_control.py` |
| AC-59 | **`view` collaborator** asks for an objective change | no objective recorded, no strategy write, no cascade; next upload keeps the owner's objective | `VIU` group `view_collaborator` |
| AC-60 | **Model error / timeout / block** in any concurrent panel, cascade step or per-document extraction | fails alone; terminal emit; order kept | `VIU` group `panel_model_error` |
| AC-61 | **Workspace purge** during an upload or cascade | pipeline task cancelled by the sweep; `_workspace_document_pipeline(_state)`, `_workspace_cascade_pending`, `_workspace_objective_in_force`, `_workspace_doc_dated_extractions`, the scroll and signals memos hold nothing for the workspace; persisted kinds gone; no late write recreates them | `VIU` group `purge_clears_new_state` |

## Non-functional
- No new log line contains payloads, document or objective text, names or URLs
  (`tests/offline/verify_logging.py` passes). The leaks fixed in this change are
  covered by AC-20 and AC-93.
- Emit `metadata` keeps its shape; concurrency changes neither totals nor
  attribution (AC-21, AC-66).
- Existing suites pass unchanged (list in the design's test plan).
- **End to end (manual):** `make e2e` before deploy, run by a human (billed).
  Also check that an objective set in chat survives a later upload on the
  fictional case.

## Withdrawn by the re-prioritisation (moved to the follow-up ticket)
AC-28, AC-29 (search cache); AC-72, AC-73, AC-74 (per-document work
overlapping ingestion); AC-75 (search signals from every document); AC-76,
AC-77 (progressive `document_assessments`); AC-78 (Indian Kanoon limit);
AC-80 to AC-85 (map-reduce, digests, delta folds, panel-coverage golden case).
Earlier AC-64 (process-wide Vision limit) and AC-65 (per-workspace fairness)
are replaced by the new AC-64 and AC-65 above.

## Definition of done
- [ ] Reject fix PR: AC-130 to AC-137 pass offline
- [ ] Phase -1 PR: AC-100 to AC-112 and AC-115 to AC-120 pass offline; AC-113 and AC-114 signed off from a `make e2e` run; backend confirms nothing reads the removed strategy `error` field
- [ ] Every AC above has a passing check, or a signed-off manual result (AC-27, AC-79, AC-95, AC-98, AC-113, AC-114, `make e2e`)
- [ ] `make lint` and `make test` pass
- [ ] `make e2e` run (handlers, panels and ingestion touched)
- [ ] `CHANGELOG.md`: no payload shape change; emit timing; `workspace_metadata` on sync from the pipeline; cascade/upload interplay; dates cover every document; objective preserved; new settings
- [ ] Concurrency defaults confirmed against quota (requirements open question 3)
- [ ] PR labelled `needs-human-review` (billing path exercised concurrently; new persisted stores of case content; PII redaction moved to a worker thread)
- [ ] Follow-up ticket created for the withdrawn scope (requirements open question 10)
- [ ] CLAUDE.md async-scheduling, objective-change and concurrency sections updated
