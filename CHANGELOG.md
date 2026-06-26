# Changelog

All notable changes to the Law Firm SimplerToday AI Service are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Guaranteed template coverage in AI Suggested Drafts** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). Previously a reusable template surfaced as a suggested-draft card only when the (non-deterministic) suggestion LLM happened to propose a draft of that type for the current case posture — so changing case details (client / brand / new matter) could silently drop an applicable template even though its legal purpose still fit. Now `suggest()` runs a deterministic **template-coverage pass** (`_ensure_template_coverage`): for every ready template not already bound to a matched card, a focused **applicability check** (`build_template_applicability_prompt` / `TEMPLATE_APPLICABILITY_INSTRUCTION`) decides — by the template's **legal purpose + document type**, never its original parties/facts — whether a draft of that kind fits this matter. Applicable templates are **guaranteed** a case-framed card with the template pre-bound (`matchMode="matched"`, prematch locked for the kickoff click). Template-backed cards take priority and the panel cap is raised from 6 to 8 (`_MAX_SUGGESTIONS_WITH_TEMPLATES`) so applicable templates are never crowded out by AI-only suggestions. The suggestion prompt and matcher now also see the fuller `filing_context` (legal purpose) instead of only the coarse `document_purpose`.
- (Document changes here before release)

### Changed

- (Document changes here before release)

### Fixed

- **Firm letterhead no longer repeats on every page of a generated draft** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). A running-header letterhead in a PDF template is extracted into the body of every page — with `--- Page N ---` markers on the OCR path (`file_handlers._extract_text_from_pdf_with_ocr`) and without markers on the text-layer path (`extract_text_from_pdf`) — and, fed verbatim to the render LLM as the structural contract, was reproduced at the top of every page. Template text is now normalised at ingest (`_normalize_template_text`) for **both** paths: the marker-based pass strips `--- Page N ---` markers and collapses the header block that repeats across the majority of pages, and a marker-independent backstop (`_dedupe_leading_repeated_block`) collapses a leading block that recurs verbatim through the body (text-layer PDFs). Both are conservative — only a *leading* block that recurs verbatim is removed, so ordinary body prose and DOCX/single-page extracts are untouched. `DRAFT_RENDER_INSTRUCTION` also instructs the model to treat repeated letterheads / page markers as extraction artifacts (render once, never emit page markers).
- **Literal template placeholders (e.g. `<Client Address>`) are now always captured and asked, instead of shipping unfilled** (`app/langgraph/drafting_agent.py`). The profiler's deterministic placeholder seed only worked for DOCX (`_deterministic_placeholders` returns `[]` for PDF-only templates), so a `<...>` slot in a PDF template relied entirely on the profiler LLM noticing it in prose — and when missed, it was never added to the interactive-variable registry, never asked at kickoff, and rendered as a literal hole. Now literal slots are also detected from the extracted text (`_literal_placeholders_in_text`, reusing the shared `PLACEHOLDER_PATTERNS`) to seed the profiler for PDFs too, and a deterministic safety net (`_ensure_placeholder_variables`) guarantees every high-signal slot (`<...>` / `{{...}}` / `(Pascal_Case)`) reaches the registry so `_ensure_registry_coverage` surfaces it as a kickoff question (such synthesised slots default to `fill_from_case=false` so they are always asked rather than risking a wrong auto-fill).
- **Kickoff questions no longer leak the template's original subject matter into an unrelated case** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). Reusing a watch legal-notice template for a *car* case produced questions like "What is the serial number of the **watch**?" — the profiler had baked the template's original product noun into each variable's `question`/`label`, and the deterministic registry-coverage step asks that text verbatim regardless of the new matter. `TEMPLATE_PROFILE_INSTRUCTION` now requires **domain-neutral wording**: labels/questions must refer to the subject generically ("the product / goods / asset / item"), never the template's original product, brand, or party names. `PROFILE_SCHEMA_VERSION` bumped to 4 so already-cached templates are automatically re-profiled (on the next template sync / reconnect) under the neutral-wording rule rather than serving stale watch-specific questions.
- **Drafting kickoff no longer asks the same question twice** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). The kickoff built its question list from two sources that were only de-duplicated by exact id — the kickoff LLM's own `openQuestions` (umbrella phrasings like "Notice Reference & Date", "Compensation Heads") and `_ensure_registry_coverage`'s per-variable additions (the granular "Notice Reference Number" + "Notice Date", individual compensation heads, etc.) — so semantically-equivalent questions were asked under both Critical and Important. A hybrid de-dup (`_dedup_kickoff_questions`) now runs on the final set: a deterministic identical-token pass (`_dedup_questions_by_tokens`) collapses pure rephrasings (e.g. "Client Professional Background" = "Professional Background"), then an LLM pass (`_consolidate_questions_llm`, new `QUESTION_DEDUP_INSTRUCTION` / `build_question_dedup_prompt`) drops synonym/umbrella-vs-parts duplicates that share no words — keeping the granular questions and dropping the umbrella so every distinct value is still captured. The LLM pass only ever drops existing ids, ignores a result that would remove most questions, and falls back to the input on failure. The profiler prompt (`TEMPLATE_PROFILE_INSTRUCTION` rule 2) is also strengthened to forbid emitting overlapping variables (umbrella + parts, combined + split, synonyms) at the source.
- **Scanned-document OCR noise (rubber-stamp/seal text, page-number footers, editor margin notes) no longer leaks into generated drafts** (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`). A scanned PDF template is OCR'd, so a circular firm seal is read as scrambled fragments that **differ on every page** (`SSOCIATES`, `ASSOCI`, `FUSKH`, `GURUGRAM`, `INDIA`…) and, together with `Page N of M` footers and editor notes, lands between body paragraphs — which the verbatim de-dup (`_normalize_template_text`) cannot remove. Template text is now passed through a one-time LLM cleanup at ingest (`_clean_template_ocr_text`, on a fresh extraction only) that strips letterheads / seal garble / page footers / margin notes while preserving every numbered paragraph, heading, recipient/signature block and literal placeholder verbatim (new `TEMPLATE_CLEANUP_INSTRUCTION` / `build_template_cleanup_prompt`, via `gemini_client.generate_longform`). Long templates are not skipped: they are split on line/paragraph boundaries (`_chunk_text_for_cleanup`) and cleaned chunk-by-chunk in bounded parallel, with mid-document chunks flagged as continuation fragments. An accept/reject gate (`_accept_cleaned_template_text`) runs per chunk and on the reassembled document — rejecting any result that is drastically shorter/longer or drops a literal placeholder and falling back to the original chunk / deterministically-normalised text — so ingest never loses content. `DRAFT_RENDER_INSTRUCTION` also names garbled stamps/seals and page footers as artifacts to ignore.
- (Document changes here before release)

---

## [4.1.0] - 2026-06-12

### Added

- **Chat-driven panel editing from the Strategy & Objective chat**, with two clearly separated modes (`app/socket/events.py`):
  - **Mode 1 — name / word / line edit (surgical).** A NAME change is applied as an exhaustive, workspace-wide rename across the EXISTING content of every panel — Case Summary, Report Summary, Strategic Objective, Possible Next Steps, Analysis Report, List of Dates, Applicable Laws, and Upcoming Dates — with **no regeneration** (no new/removed dates, laws, or items). The intent classifier returns an `isRename` signal and the dispatch fans one rename instruction to all panel editors deterministically (so coverage no longer depends on the LLM flagging each panel). Name changes are applied **semantically** (a party referenced by role/description, e.g. "the female employee", is named correctly even where she appears as "the Petitioner").
  - **Mode 2 — case-perspective / objective change (regeneration).** Declaring a new objective regenerates the Strategic Objective + Possible Next Steps, regenerates the Analysis Report, re-scores List-of-Dates relevance (same entries), regenerates Applicable Laws and Drafting suggestions for the new objective, and preserves the Case Summary + Upcoming Dates.
- New surgical panel editors: `_regenerate_analysis_report_with_feedback`, `_regenerate_list_of_dates_with_feedback`, `_regenerate_applicable_laws_with_feedback`, `_regenerate_procedural_timeline_with_feedback`, plus `_reeval_list_of_dates_relevance` (Mode 2 relevance re-score).
- **Output guardrails** shared across every generated panel and chat (`app/prompts/legal_intelligence_system.py`, injected into the structured + chat system instructions and the inline case-summary / list-of-dates / procedural-timeline prompts):
  - `ENGLISH_ONLY_OUTPUT_DIRECTIVE` — all output in English/Latin script; Devanagari/mixed-script names are transliterated.
  - `APP_BRAND_EXCLUSION_DIRECTIVE` + `scrub_app_brand()` — the platform's own name ("SimplerToday") never appears as a case party/fact (prompt guardrail + deterministic scrub before cache/emit).
  - `NAME_CAPITALISATION_DIRECTIVE` — person names / places / addresses in Title Case, never ALL-CAPS (statutory acronyms preserved).
- `GeminiClient.generate()` now accepts an explicit **`thinking_level`** (default `LOW`) and runs primarily on the `google-genai` SDK with `ThinkingConfig`, falling back to the legacy SDK.

### Changed

- **Objective change fully re-derives the downstream panels.** `_run_objective_change_incremental_refresh` now **regenerates and REPLACES** the Analysis Report for the new objective (previously it merged with the prior report, which kept the old-perspective strengths/risks and preserved the old quality score) and regenerates Applicable Laws with the **objective as the primary law-selection lens** (`merge_with_previous=False`). So changing the objective (e.g. jewellery-theft → divorce) produces an analysis report, quality score, applicable laws, and drafting suggestions framed entirely around the new objective. The incremental-merge helper is unchanged for the document re-upload / `workspace_sync` paths.
- **Procedural Timeline scope narrowed** (`app/langgraph/procedural_timeline_agent.py`): the panel now carries ONLY genuine court/law-set procedural dates — filing deadlines, hearing dates, evidence deadlines, compliance dates, and adjourned/next dates (types `Court Hearing` / `Deadline`). Pre-litigation notices, police complaints / FIR / GD / missing-person reports, lawyer consultations / engagements, meetings, and reconciliation attempts are excluded, classification is LLM-driven (no hardcoded keyword rules), and the agent never invents dates — it returns an empty timeline when none exist.
- **Gemini latency / completeness** governed by `thinking_level` rather than the output ceiling: gemini-3 reasoning tokens are billed against `max_output_tokens`, so the budget is kept moderate and the thinking pass is bounded (`LOW`) to keep responses fast while still completing the full output.
- Drafting template sync no longer applies a hard 30-second cutoff (`ensure_templates_synced`) — suggestion matching waits for the lawyer's templates to finish ingesting instead of matching against an empty library; the awaiting future always resolves so it cannot hang.
- A multi-field chat edit now emits a single consolidated `strategy_objective` payload instead of one partial emit per field (no "previous version" flicker).

### Fixed

- **Objective change left the Analysis Report and Applicable Laws on the OLD objective.** Root cause: the analysis report was merged with the previous report (keeping stale strengths/risks and the old quality score). Now replaced + re-derived for the new objective (see Changed).
- **Intermittent 0% Quality Score** on a populated Analysis Report. `caseResolutionScore` parsing is now robust (accepts `"72"`, `"72%"`, floats) and a content-derived score is used when the model omits/zeroes it, so an analysed case never reads 0%.
- **`finish_reason=MAX_TOKENS` truncation** that cut large structured outputs (e.g. the strategy JSON dropped `strategicObjective` / `possibleNextSteps`) and a follow-on latency regression — both traced to the gemini-3 thinking budget; fixed via `thinking_level` control on `generate()`.
- **Mid-sentence truncation** of panel descriptions. A naive sentence splitter treated abbreviation periods ("Model No.", "SRF No.", "Rs.") as sentence ends and chopped descriptions; List of Dates, Procedural Timeline, and Drafting-suggestion descriptions now trim only at a word boundary.
- Normal questions in the Strategy & Objective chat (e.g. "explain the applicable case laws") no longer trigger an objective regeneration — a question vs objective-update guard (`_is_pure_informational_query` + a strengthened LLM classifier) routes genuine objective changes to the regeneration flow and leaves questions/renames alone.
- A genuine objective change now correctly shows the loader for and re-emits List of Dates / Applicable Laws / Upcoming Dates / Drafting (previously only the Analysis Report refreshed).

---

## [4.0.0] - 2026-06-08

### Added

- Added the **Drafting Assistant** feature (`app/langgraph/drafting_agent.py`, `app/prompts/drafting.py`).
  - New inbound event **`template_sync`** (uploaded / renamed / deleted) for the lawyer-scoped Template Library — each template is downloaded, its text extracted via `app/processing/file_handlers.py`, and persisted in a new lawyer-scoped Qdrant cache (`upsert_user_state_cache` / `get_user_state_cache` / `delete_user_state_cache` / `list_user_state_cache`).
  - New outbound `document_feature_response` with **`featureType: "drafting_assistant"`** — 4–6 proactive draft suggestions (`{title, relevance, description}`) generated at `workspace_created` / `workspace_sync`, cached in `_workspace_drafting_payload`, re-emitted on reconnect.
  - Multi-turn drafting **chat flow** on the existing `user_message_request` ↔ `ai_response` pair (`chat_type="DRAFTING_ASSISTANT"`). The drafting agent keeps per-`actionableId` session state in a new `drafting_session` workspace state cache, asks the lawyer for missing slots, and emits an augmented `ai_response` with `draftStatus`, `openQuestions`, and (when ready) `draftDocument` (Markdown body). When the lawyer picks a template, the template's structural skeleton is preserved verbatim; only case-specific facts are substituted.
  - Lawyer-scoped state cache support in `app/vectorstore/qdrant.py` (mirrors the existing workspace-state cache pattern but keyed by `user_id`, with no vectors).
- Added a dedicated **long-form generation path** `GeminiClient.generate_longform()` (`app/ai/gemini_client.py`) for draft render and draft translation, built on the current **`google-genai`** SDK with native async (`client.aio.models.generate_content`). It exposes explicit **`thinking_level`** control (Gemini 3 thinking models) and falls back to the legacy `generate()` path automatically if the new SDK is unavailable or returns no text — so drafting never hard-fails.
- Added Gemini generation **diagnostics** (`GeminiClient._log_generation_diagnostics`): every Gemini call now logs `finish_reason` and token usage (`prompt` / `output` / `thinking` / `total`), at WARNING when a response is truncated or blocked. Makes draft cut-offs immediately diagnosable in production logs.

### Changed

- Draft render (`_render_and_attach`) and draft translation (`_handle_translate_draft`) in `app/langgraph/drafting_agent.py` now call `generate_longform(...)` with `max_tokens=65536` (the model's full 64k output ceiling) and `thinking_level="HIGH"`. Drafting is the product's core feature, so it uses the deepest reasoning pass for fuller legal structure and more complete drafts. Previously these used the legacy one-shot call capped at 32000 tokens.

### Fixed

- **Incomplete / truncated drafts.** Long drafts (rejoinders, multi-affidavit packs, 10–20 page legal notices) were rendering cut off mid-sentence. Root cause: draft generation ran on the deprecated `google-generativeai` SDK with no control over the Gemini 3 thinking budget (reasoning tokens are billed against `max_output_tokens`) and a 32000-token cap. Fixed by migrating the draft path to the `google-genai` SDK with explicit thinking control and raising the output ceiling to 64k.

---

## [3.0.0] - 2026-04-27

### Added

- Added a new **Procedural Timeline Agent** (`app/langgraph/procedural_timeline_agent.py`) that extracts, consolidates, validates, and emits a legal-journey timeline across uploaded case documents.
- Added a new socket feature payload type: **`procedural_timeline`** (via `document_feature_response`) with reconnect-safe cache and merge behavior.
- Added `app/prompts/applicable_acts.py` to strengthen Indian-law grounding used in timeline and law-relevance reasoning.

### Changed

- Updated strategy, analysis, chat, and applicable-laws behavior to apply a **dual legal lens**: evaluate both **criminal-law** and **civil-law** tracks when facts support both.
- Improved role-conditioned legal prompting so outputs remain role-consistent while still covering both legal branches where relevant.
- Expanded scheduled feature generation flow to include `list_of_dates` → `applicable_laws` → `procedural_timeline` without blocking chat handling.

### Fixed

- Fixed procedural timeline event typing/normalization and downstream payload consistency in socket emission flow.
- Fixed prompt-level behavior drift so criminal/civil coverage is consistently applied across generated legal outputs.

---

## [2.0.1] - 2026-04-23 — Hotfix

### Fixed

- Fixed quality score explanation (`qualityScoreExplanation`) being returned as raw JSON object or wrapped in markdown code fences instead of plain readable text; response is now unwrapped and sanitized before use.
- Fixed quality score explanation appending internal rejection tracking notes (score delta, rejected item snippet) that were visible to end clients; on item rejection the explanation is now fully regenerated from current workspace state with no internal metadata included.
- Fixed list-of-dates agent emitting entries with placeholder/invalid dates (e.g. `1111-11-11`) and UI-scraped garbage text (e.g. "View PDF", "Feedback", "Copyright Policy", "Skip to main content") as date descriptions or evidence snippets.
- Fixed list-of-dates extraction including irrelevant dates such as statute enactment years, legal citation years, and background biographical dates unrelated to the active case.
- Fixed list-of-dates listing-history table rows (columns like "Date of Listing / Date of Upload / Order") producing excessive low-quality entries; extraction now limits such tables to at most three significant dates (case filing, registration, and most recent substantive hearing).
- Improved list-of-dates evidence snippet quality — raw document fragments and bare PDF link lists are rejected as evidence.

---

## [2.0.0] - 2026-04-16

### Added

- AI-driven **versioned analysis** generation for DOCX/report flows with content-hash caching and request-safe finalize behavior.
- **Document generation** for AI outputs, enabling structured legal draft documents from analysis and strategy results.
- **Strategy summary** and report cover field support (`reportCaseSummary`, contact/date metadata) with nested and snake_case payload mapping.
- **Applicable laws** enrichment with larger catalog support and paginated search.
- **List of dates / parties** persistence for pending suggestions and approval lifecycle across refreshes.
- Persistence for approved **missing info**, strengths, weaknesses, risks, and jurisdiction across AI refresh cycles.

### Changed

- Workspace refresh and merge behavior refined for first ingest and incremental sync stability.
- Socket/event observability improved with cleaner logs and better signal-to-noise for AI operations.
- Runtime dependencies updated for reliability and vulnerability remediation.

### Fixed

- Applicable-laws update/search reliability issues fixed, including duplicate event emission edge cases.
- Workspace reset now clears replay/cache state to prevent stale AI strategy data.
- Rejected pending list-of-dates entries no longer reappear after user rejection.
- User-defined workspace title is preserved and not overwritten by AI metadata refresh.
- Strategy and analysis event handling stabilized during workspace refresh.

### Security

- Search/update paths hardened for applicable-laws operations.
- Dependency vulnerability fixes included in this release cycle.

---

## [1.0.0] - 2026-03-06

### Added

- AI Socket.IO integration for workspace sync and real-time intelligence chat responses.
- Document intelligence baseline: file ingestion, extraction pipeline, and workspace-scoped vector retrieval.
- Initial strategy and analysis generation flow for legal workspaces.
- Initial **list-of-dates** extraction and timeline generation support (early version).
- Analysis report flow integrated.

### Fixed

- Stability fixes for early workspace processing and chat event handling.
- Reliability improvements for AI feature-response emission under load.

---

[Unreleased]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.1...v3.0.0
[2.0.1]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v2.0.0
[1.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v1.0.0
