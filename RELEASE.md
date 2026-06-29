# Release v4.2.0 - 2026-06-29

This release (`v4.2.0`) hardens the **Drafting Assistant** end-to-end so that AI suggested drafts and template-backed drafts are reliable, faithful to the lawyer's templates, and free of extraction noise. It is backward compatible with `v4.1.0` — no socket event or payload-shape changes.

## Highlights

- **Applicable templates are now guaranteed a suggested-draft card.** A deterministic template-coverage pass checks each ready template's legal purpose against the matter and surfaces it as a case-framed card with the template pre-bound, instead of relying on the suggestion LLM to happen to propose it. The suggestions panel cap is raised from 6 to 8 so templates are never crowded out.
- **New Custom AI Draft mode (`draftMode="custom"`)** lets a lawyer generate a case-framed draft directly from case context when no suitable template exists, bypassing template resolution.
- **Every template variable is confirmed by the lawyer** — nothing is silently auto-filled. A global auto-fill quality gate rejects informal labels, contact ids, and vague values, and recipient email and each cost/compensation head are always asked individually.
- **Cleaner, more faithful rendered drafts.** Repeated PDF letterheads, scanned-template OCR noise (seal/stamp garble, page footers, margin notes), and the template's original subject matter no longer leak into the generated draft, literal placeholders (e.g. `<Client Address>`) are always captured and asked, and duplicate kickoff questions are de-duplicated.
- **Faster long-draft rendering.** Draft render and translation now run at `thinking_level="LOW"`, removing a latency/truncation regression while preserving the full 64k output ceiling. Draft language now persists across revision turns.

## Features Delivered

- Deterministic template-coverage pass (`_ensure_template_coverage`) with a per-template applicability check (`build_template_applicability_prompt` / `TEMPLATE_APPLICABILITY_INSTRUCTION`); panel cap raised to 8 (`_MAX_SUGGESTIONS_WITH_TEMPLATES`).
- `draftMode="custom"` kickoff path that skips template resolution/profiling and drafts from case context.
- Content/substantiation clauses captured as interactive template variables; profile schema bumped to **v4** so cached templates auto re-profile on the next template sync / reconnect.

## Quality and Stability Improvements

- One-time LLM template-text cleanup at ingest (`_clean_template_ocr_text`) that strips letterheads, seal garble, page footers, and margin notes while preserving every numbered paragraph, heading, recipient/signature block, and literal placeholder verbatim, with an accept/reject gate and chunked processing for long templates.
- Verbatim letterhead/page-marker de-duplication (`_normalize_template_text`, `_dedupe_leading_repeated_block`) on both the OCR and text-layer extraction paths.
- Literal placeholder capture for PDF templates (`_literal_placeholders_in_text`, `_ensure_placeholder_variables`) so high-signal slots always reach the kickoff questions.
- Domain-neutral kickoff wording (no leaking the template's original product/brand/party into an unrelated matter) and hybrid kickoff question de-duplication (`_dedup_kickoff_questions`).

## Bug Fixes

- Recipient email and hardcoded demand/compensation amounts are always captured as per-matter variables and asked, never carried over from the template.
- Draft language persists across revisions; translate instruction tightened for abbreviations and numerals.
- Draft render no longer emits preamble text; clicking a different suggested-draft/template card re-kicks the conversation cleanly.

## Compatibility Notes

- Drop-in replacement for `v4.1.0`. No new or changed socket events or payload shapes — all changes are internal to the Drafting Assistant generation and template-profiling paths.
- Cached templates are automatically re-profiled under the new profile schema (**v4**) on the next `template_sync` or reconnect; no manual migration is required.

---

# Release v4.1.0 - 2026-06-12

This release (`v4.1.0`) adds chat-driven panel editing from the Strategy & Objective chat and hardens legal-intelligence outputs (English-only, Title-case names, brand exclusion, robust quality score), plus Gemini thinking-budget control to keep responses fast and complete.

## Highlights

- **Edit panels from chat**, with two clearly separated modes: a surgical **name / word / line edit** applied workspace-wide across all panels with no regeneration, and an **objective / case-perspective change** that re-derives the strategic objective, analysis report, applicable laws, quality score, and drafting suggestions for the new objective.
- **Output guardrails** shared across every panel and chat: English/Latin-script output only (non-Latin names transliterated), the platform's own name never surfaced as a case party, and person/place names in normal Title Case.
- **Procedural Timeline scope narrowed** to genuine court/law procedural dates only (filing deadlines, hearings, evidence deadlines, compliance dates, adjourned/next dates); the agent never invents dates.
- **Gemini latency/completeness governed by `thinking_level`** (default `LOW`) on `GeminiClient.generate()` rather than by inflating the output ceiling, fixing `MAX_TOKENS` truncation of large structured outputs.

## Bug Fixes

- Objective change now replaces (not merges) the Analysis Report and re-derives Applicable Laws for the new objective.
- Intermittent 0% quality score on a populated report fixed (robust score parsing + content-derived fallback).
- Mid-sentence truncation of panel descriptions fixed (trim only at a word boundary, not at abbreviation periods).
- Informational questions in the Strategy & Objective chat no longer trigger an objective regeneration.

## Compatibility Notes

- Backward compatible with `v4.0.0`. No new feature payload types; behavior changes are in chat-driven editing and output formatting.

---

# Release v4.0.0 - 2026-06-08

This major release (`v4.0.0`) introduces the **Drafting Assistant** — a lawyer-scoped Template Library plus an in-workspace drafting panel and multi-turn drafting chat.

## Highlights

- New **Drafting Assistant** feature: proactive suggested drafts per workspace and a multi-turn drafting chat that gathers missing facts and renders full Markdown drafts.
- New lawyer-scoped **Template Library** with a new inbound `template_sync` event (uploaded / renamed / deleted) and lawyer-scoped Qdrant state cache; uploaded templates are reused as a verbatim structural skeleton.
- New outbound `document_feature_response` with `featureType: "drafting_assistant"`, cached and re-emitted on reconnect.
- Dedicated long-form generation path `GeminiClient.generate_longform()` on the `google-genai` SDK (64k output ceiling, explicit thinking control) plus per-call generation diagnostics (`finish_reason` + token usage).

## Bug Fixes

- Fixed incomplete / truncated long drafts by migrating the draft path to the `google-genai` SDK with explicit thinking control and raising the output ceiling to 64k.

## Compatibility Notes

- Major release: a new `drafting_assistant` feature type and a new `template_sync` inbound event are added. Backend/frontend consumers that maintain feature/event allow-lists must include them.

---

# Release v3.0.0 - 2026-04-27

This major release (`v3.0.0`) introduces the new procedural timeline capability and upgrades legal reasoning behavior across agents to consistently evaluate both civil-law and criminal-law branches where facts support both.

## Highlights

- New **Procedural Timeline Agent** builds a cleaner legal-journey timeline (events, filings, hearings, orders) across uploaded case documents.
- New feature payload **`procedural_timeline`** is emitted via `document_feature_response` and is cached for reconnect-safe re-emission.
- Strategy, analysis, applicable-laws, and intelligence-chat reasoning now follow a **civil + criminal dual-track** legal lens by default when both branches are live.
- Prompt and socket behavior updates improve consistency of event typing and cross-module legal framing.

## Features Delivered

- Added `app/langgraph/procedural_timeline_agent.py` with extraction, consolidation, validation, and merge-safe timeline output behavior.
- Added procedural timeline generation and emission flow in `app/socket/events.py` including scheduling after list-of-dates and applicable-laws.
- Added prompt support (`app/prompts/applicable_acts.py`) and legal reasoning instructions that reinforce dual-branch Indian-law coverage.

## Quality and Stability Improvements

- Strengthened legal output coherence so criminal and civil implications are handled together where applicable, without forced artificial balancing where only one branch is relevant.
- Improved downstream feature consistency via event type normalization and structured timeline payload handling.

## Compatibility Notes

- This is a **major release** because legal reasoning behavior is expanded across strategy/report/chat surfaces and a new `procedural_timeline` feature type is now part of emitted capabilities.
- Ensure backend/frontend consumers that maintain feature allow-lists include `procedural_timeline` in accepted feature types.

---

# Release v2.0.1 - 2026-04-23

This is a hotfix release (`v2.0.1`) on top of `v2.0.0`, addressing two targeted bugs reported after the `v2.0.0` rollout: incorrect quality score explanation rendering and noisy/invalid entries in the list-of-dates extraction flow.

## Highlights

- Quality score explanation no longer leaks internal rejection tracking notes to clients — the explanation is now fully regenerated from current workspace state on each score update.
- List-of-dates extraction is significantly more precise: garbage UI-scraped content, placeholder dates, and irrelevant citation years are now filtered out before results are emitted.

## Bug Fixes

- Fixed quality score explanation returning raw JSON or markdown code fences from the AI model instead of plain readable text.
- Fixed quality score explanation incorrectly appending internal rejection/score-delta notes that were visible to end clients; explanation is now cleanly regenerated on item rejection.
- Fixed list-of-dates agent emitting entries with placeholder or invalid dates (e.g. `1111-11-11`, `9999-99-99`) and UI-scraped garbage content (e.g. "View PDF", "Feedback", "Copyright Policy").
- Fixed list-of-dates prompt to exclude listing-history table rows, statute citation years, and background biographical dates unrelated to the case.
- Improved list-of-dates evidence snippet quality — raw document fragments and PDF link lists are no longer accepted as evidence snippets.

## Compatibility Notes

- Drop-in replacement for `v2.0.0`. No schema or contract changes; only behavioral fixes in quality score explanation generation and list-of-dates filtering.

---

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
