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

[Unreleased]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/viitoradmin/law-firm-simplertoday-ai/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v2.0.0
[1.0.0]: https://github.com/viitoradmin/law-firm-simplertoday-ai/releases/tag/v1.0.0
