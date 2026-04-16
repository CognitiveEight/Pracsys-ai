# Release v2.0.0 - 2026-04-16

This release upgrades the Law Firm SimplerToday AI service in `v2.0.0` with more stable document analysis, document generation support, stronger workspace synchronization, and safer applicable-laws handling.

## Highlights

- Versioned analysis generation now uses content-hash caching and request-safe finalize behavior to reduce duplicate DOCX/report output.
- Document generation flow is now integrated so AI outputs can be produced as structured downloadable documents for legal workflows.
- Applicable-laws catalog/search scales better for large datasets and is hardened against unsafe input patterns.
- Workspace refresh, pending approvals, and list-of-dates persistence are now consistent across sync/reset/retry flows.

## Features Delivered

- Added versioned analysis flow with request lifecycle safety and improved DOCX-oriented handling.
- Added document generation support for AI-produced legal outputs, including structured DOCX/report generation workflow integration.
- Added persistence for strategy report fields (including case summary and report cover/contact metadata).
- Added broader applicable-laws master data support with paginated search behavior for large law catalogs.
- Added persistence for pending list-of-dates/parties suggestions and approval status across workspace refreshes.
- Added persistence for approved missing information, strengths, weaknesses, risks, and jurisdiction in refresh-safe flows.

## Quality and Stability Improvements

- Refined workspace refresh and merge behavior for first ingest and subsequent AI refresh operations.
- Reduced noisy logs and improved operational signals for socket/workspace processing.
- Updated dependency set for vulnerability remediation and overall runtime reliability.

## Bug Fixes

- Fixed unreliable applicable-laws append/update behavior under concurrent updates.
- Fixed regex-based search risk paths in applicable-laws query flow.
- Fixed duplicate socket emission scenarios for applicable-laws/workspace updates.
- Fixed replay-cache leakage on workspace reset so stale strategy data is not reused.
- Fixed rejected pending date items reappearing after rejection.
- Fixed user-defined workspace title being overwritten by later AI metadata updates.
- Fixed strategy payload persistence for nested and snake_case fields from AI responses.

## Security Updates

- Hardened applicable-laws search and append/update paths against injection-style input patterns.
- Included dependency vulnerability fixes shipped in this release cycle.

## Compatibility Notes

- Release `2.0.0` is intended to run with backend/frontend contracts aligned to workspace sync, socket events, versioned analysis, and document generation behavior.
- Any custom client consuming socket or workspace payloads should re-validate integration against this release.

---

## Release Process (AI Service)

Use this checklist from a clean working tree:

1. Run quality checks:

   ```bash
   make lint
   make run
   ```

2. Update release docs:
   - Move `Unreleased` entries in `CHANGELOG.md` to the new version section.
   - Update `README.md` only if runtime/configuration behavior changed.

3. Commit and tag:

   ```bash
   git add CHANGELOG.md RELEASE.md README.md
   git commit -m "chore: release vX.Y.Z"
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin main --tags
   ```

4. Post-release verification:
   - Verify AI service connects to socket backend.
   - Verify document feature flows (`analysis_report`, `list_of_dates`, `applicable_laws`) complete successfully.

## Versioning and Commits

- Versioning follows Semantic Versioning (`MAJOR.MINOR.PATCH`).
- Git tags follow `vX.Y.Z`.
- Conventional commits are recommended: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `perf:`.
