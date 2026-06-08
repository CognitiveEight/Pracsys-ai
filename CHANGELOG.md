# Changelog

All notable changes to the Law Firm SimplerToday AI Service are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- (Document changes here before release)

### Changed

- (Document changes here before release)

### Fixed

- (Document changes here before release)

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
