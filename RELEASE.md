# Release v5.1.0 - 2026-07-31

This release (`v5.1.0`) completes the workspace bill and repairs the document manifest. Paid non-LLM Google Cloud work — Vision OCR, Speech-to-Text, Vertex AI Search, Google Search grounding and embeddings — is now metered and priced alongside LLM tokens, and the cached document inventory that every feature prompt treats as ground truth is now kept accurate in both directions: deleted evidence is reported as a gap instead of vanishing, and previously-uploaded documents are no longer flagged as missing after an incremental upload. One new inbound event, `workspace_version_restore`, is added for workspace version rollback. **One semantically breaking change for consumers:** `metadata.price` / `totalPrice` now include paid service cost, not just token cost. It also carries the workspace-ownership handover event, curated-panel-state reconciliation, a role-resolution correction that affects every downstream panel, and a batch of ingestion and drafting fixes.

## Highlights

- **The bill is now the whole bill.** Every paid Google Cloud call the pipeline makes was previously billed to nobody. `metadata` on every emit now carries per-response and cumulative `ocrUnits`/`ocrPrice`, `sttSeconds`/`sttPrice`, `searchQueries`/`searchPrice`, `groundingRequests`/`groundingPrice`, `embedTokens`/`embedPrice` and their sum `servicePrice` — with `price == inputPrice + outputPrice + servicePrice`.
- **Deleted documents are reported as missing information.** Deleting a matter's invoice used to make it disappear from the intelligence entirely; it now surfaces as a gap and stops being cited as evidence, guaranteed by a deterministic append rather than by prompt compliance alone.
- **A live manifest bug is fixed.** Incremental uploads were overwriting the cached document manifest with only the newly-processed files, so after a second upload the AI forgot the earlier documents and began flagging *already-uploaded* evidence as missing. Every workspace with more than one upload should see missing-information quality visibly improve.
- **New inbound event `workspace_version_restore`** reconciles a restored workspace against reality — diffing the live vector-store document set against `evidenceFiles` — instead of trusting the backend's delta, so a restore is neither silently skipped nor re-billed.
- **A workspace's cumulative bill survives a service restart** instead of restarting from zero on the first `workspace_sync` after a cold start.
- **New inbound event `workspace_transferred`** — when a matter changes owner, per-workspace owner state is re-keyed and the Drafting Assistant is rebuilt against the new lawyer's template library, without reprocessing a single document.
- **Curated panel state is no longer churned.** `workspace_sync` now accepts the previously-emitted, user-curated panel state as `currentState`, so regeneration tags each item as carried-over vs new instead of re-presenting work the lawyer had already reviewed.
- **A role-resolution bug that silently mis-framed whole workspaces is fixed.** A document-upload sync carries no `workspaceRole`, and a cold-memory context fabricated an ADJUDICATOR default that shadowed the user's real selection — so panels could be generated from the wrong perspective.
- **Answered missing-information items now resolve on upload.** Uploading a document that answers a gap clears it, instead of leaving it standing.
- **Panels arrive in product order and the event loop no longer stalls** during OCR-heavy ingestion.

## Features Delivered

- Paid-service metering and pricing (`app/ai/usage.py`, `app/ai/pricing.py`): `UsageAccumulator` carries `ocr_units` / `stt_seconds` / `search_queries` / `grounding_requests` / `embed_tokens`, reported by matching `record_*` helpers at the single lowest paid call site and priced by the new `service_price_for()`. Rates are verified against Google's July 2026 published pricing and each is environment-overridable; the Google Search grounding default is model-aware ($35/1k grounded prompts on Gemini 2.5, $14/1k on Gemini 3).
- Removed-evidence ledger (`CacheKinds.REMOVED_DOCUMENTS`): each pruned `document_assessments` entry is snapshotted before it is dropped, feeding both a REMOVED EVIDENCE block in the analysis-report prompt and `_apply_removed_evidence_missing_info`, which deterministically appends one `missingInfo` entry per removed document type the agent did not raise itself and re-clamps `caseResolutionScore` to 95. Re-ingesting the same `documentId` clears the ledger entry, so restores are self-healing.
- `workspace_version_restore` handler (`WorkspaceVersionRestorePayload`, required `versionId` + advisory `restoredDocumentIds`): a separate handler rather than a branch of `on_workspace_sync`, whose `skip_reprocess` path would have swallowed the restore. It deletes, prunes, re-ingests under its own usage scope and regenerates the full feature suite, with idempotency keyed on `versionId` in its own 180 s window.
- `workspace_transferred` handler (`WorkspaceTransferredPayload` / `TransferredUserInfo`): `workspaceId`, `previousUserId`, `newUserId`, optional `newUser`, `reEmitFeatures` and an informational `evidenceFiles`. Ownership moves without reprocessing — the handler re-keys context, role caches, drafting provenance and every cached wrapper embedding `userId`, then regenerates and re-emits only the features named in `reEmitFeatures` (currently `drafting_assistant`, force-synced against the new owner's templates first).
- `currentState` on the workspace-sync payload (`WorkspaceCurrentState` plus `CurrentStateTextItem` / `CurrentStatePartyItem` / `CurrentStateDateItem` / `CurrentStateLawItem` groups): the frontend returns the curated panel state on a re-run trigger (upload / transcript / remove / objective change, omitted on plain rename or reset) and regeneration reconciles against it, tagging carried-over items `isMatched`. Matching is by text/content only — the `status` field (approved/pending/rejected) is parsed and deliberately ignored.
- Objective-aware Case Laws and Drafting: an objective change now propagates into both panels instead of leaving them derived from the previous objective.

## Quality and Stability Improvements

- `document_assessments` envelope construction is now one shared helper (`_build_document_assessment_envelope`) across the created / sync / restore paths, replacing three byte-identical copies of the same 60-line transformation.
- Token spend recorded with no usage scope open is no longer silently dropped — it warns once per kind, matching the paid-service recorders, and the three handlers that tripped it (`probing_prompts_request`, template ingestion, blueprint proposals) are instrumented.
- `document_feature_response` emits now warn when a hand-built `metadata` key has no counterpart field in `AIResponseMetadata`, so a typo'd key can no longer ship silently and go unrecorded by the backend cost service.
- Blocking OCR text extraction is offloaded to a worker thread (`asyncio.to_thread`), so a scan-heavy upload no longer stalls the event loop and starves chat and heartbeats.
- The per-workspace feature pipeline is serialized behind a lock, so panels emit in product order rather than in whichever order their background tasks happened to finish; cached-feature re-emission (`_reemit_cached_features`) runs under the same lock.
- PDF ingestion strips publisher boilerplate from legal-database exports (SCC Online TruePrint, Manupatra, Indian Kanoon), which previously dominated the extracted text — image-only judgments were skipping OCR and being rejected as “repetitive headers”.

## Bug Fixes

- The document inventory no longer lies about which documents are present: `_prune_document_assessments` runs on delete before any regeneration builds a prompt, and `_merge_document_assessments` keys by `documentId` on upload so the cache stays a full manifest. Reconnect replay now re-sends the complete set.
- Document-ingestion spend is no longer lost — `process_batch` runs inside a usage scope on both `workspace_created` and `workspace_sync`, so per-document summary, evidentiary-strength and classification calls plus all ingestion OCR, transcription and embedding spend are billed.
- A workspace's cumulative `token_usage` is hydrated before anything can upsert it, and a payload with no `token_usage` can no longer clobber a stored one.
- JSON-repair retries are billed: up to two extra `generate_content` calls per malformed JSON generation were previously recorded nowhere.
- Partial spend is reported when a document *sync* fails mid-ingestion, matching the existing `workspace_created` behaviour.
- A feature's spend is billed even when the socket is down — the metadata block is built (and the cumulative incremented) before the emit is skipped.
- Audio re-diarization Gemini tokens are counted; the three raw-SDK helpers bypassed the metering hook.
- Workspace role resolution now treats the persisted role cache as authoritative over the in-memory context, and the role is resolved *before* the context is upserted — a role-less sync can no longer fabricate an ADJUDICATOR default that shadows the user's real selection. Related role→party advocacy resolution fixes land with it.
- Missing-information items answered by a newly uploaded document are resolved instead of persisting.
- Drafted documents keep continuous paragraph numbering across sections.
- The workspace no longer fails to refresh on document upload.

## Compatibility Notes

- **Semantically breaking for cost consumers:** `price` and `totalPrice` now include `servicePrice`. Read `price` directly; any consumer recomputing it from `inputPrice + outputPrice` will understate the bill. All new fields are optional and additive.
- **Behavioural, not breaking:** the reconnect replay of `document_assessments` now carries the full manifest rather than the last batch — consumers must **upsert by `documentId`**, not replace their list.
- `analysis_report.missingInfo` keeps its shape (`[{id, text, isMatched}]`) but will contain more entries; `caseResolutionScore` caps at 95 while any gap exists.
- `probing_prompts_response` gains the standard `metadata` block, but only when chips were actually generated.
- `workspace_version_restore` is inert until the backend emits it, so the AI service can be deployed first and rolled back safely. The backend contract for it is specified in `docs/BACKEND_VERSION_RESTORE_SPEC.md`, which must be sent to the backend team directly: the repository ignores `*.md` apart from `README.md`, `SYSTEM_ARCHITECTURE.md`, `RELEASE.md` and `CHANGELOG.md`, so that spec is not present in a clone.
- No new **required** environment variables, no new dependencies, and no Qdrant, Docker or schema migration. Five optional pricing overrides exist (`OCR_PRICE_PER_1K_UNITS`, `STT_PRICE_PER_MINUTE`, `SEARCH_PRICE_PER_1K_QUERIES`, `GROUNDING_PRICE_PER_1K_REQUESTS`, `EMBED_PRICE_PER_MILLION_TOKENS`); all have sane defaults.
- Free tiers and volume discounts are not modelled, so reported cost is an upper bound on the true invoice.
- **New inbound event `workspace_transferred`.** Inert until the backend emits it, so it does not gate this deployment. `reEmitFeatures` currently expects `drafting_assistant`.
- **New optional inbound field `currentState`** on the workspace-sync payload. Omitting it preserves the previous behaviour (everything regenerates untagged); sending it is what enables carried-over vs new tagging, so the frontend must return the curated panel state to get the benefit.
- Panels now emit in a fixed product order rather than completion order; a consumer that assumed arrival order was arbitrary is unaffected, one that raced on it will now see deterministic ordering.

---

# Release v5.0.1 - 2026-07-09

This release (`v5.0.1`) is a single user-visible fix to the chat acknowledgement for surgical panel edits. It is backward compatible with `v5.0.0` — no socket event or payload-shape changes.

## Highlights

- **Panel edits from chat now reply with a real confirmation.** When a lawyer's message is routed to the free-form panel-edit path (e.g. *"remove 1. … from possible next steps"*), the acknowledgement was a bare `"Updated: possible next steps."`, which read as the AI not responding.

## Bug Fixes

- The pre-dispatcher records each applied per-panel edit instruction, and a new helper `_build_panel_edit_ack` composes a 2–4 sentence plain-text confirmation stating **what** changed and the resulting panel state (how many next steps remain and which is now first; renames confirm the replacement across affected panels). On any generation failure it falls back to a templated acknowledgement — never worse than before.

## Compatibility Notes

- Drop-in replacement for `v5.0.0`. The `ai_response` shape, the surgical-edit routing, the skip of the full chat graph, and the no-cascade rule for panel edits are all unchanged.

---

# Release v5.0.0 - 2026-07-07

This release (`v5.0.0`) adds two new intelligence panels — **Case Laws** and **Relevant Parties** — plus manual case-law upload, multi-draft template matching in the Drafting Assistant, and a large no-behaviour-change decomposition of the socket layer. **Major version bump: several socket payload contracts changed shape** (party role vocabulary and field names, drafting-suggestion match fields, case-law item ids) — the backend and frontend must be released in step.

## Highlights

- **Case Laws panel** — relevant prior judgments, each with a real, clickable source URL, a proper Indian headnote (issues, material facts, ratio, holding, relief) and a High/Medium/Low relevance badge. Selection is strict source-verified: the model ranks and summarizes only retrieved judgments and may not invent citations.
- **Relevant Parties panel** — the people and organisations actually party to the matter, each with a normalized role badge and a one-line description, with per-document provenance so a deletion purges exactly that document's parties.
- **Manual case-law upload** — a lawyer uploads a judgment file; the AI classifies whether it really is a case law, extracts it into the panel, and rejects anything else with a reason naming what the document actually is.
- **Every matching library draft is now offered, each with its own % match score**, instead of the AI silently binding a suggestion to one "winner" template.
- **`app/socket/events.py` (20,987 lines) decomposed into 21 per-feature mixins** and all 51 inline prompts centralised into `app/prompts/`, both verified byte-identical — no behaviour change.

## Features Delivered

- `document_feature_response` with `featureType:"case_laws"`: `payload.caseLaws[]` (capped at 8, stable `caselaw_N` ids) carrying citation, court, bench, date, binding strength/stars, legal issues, material facts, ratio, holding, relief, the case-vs-matter comparison fields, and `referenceUrl`/`referenceName` for the source judgment. Sources are aggregated across Indian Kanoon, Vertex AI Search and Gemini `google_search` grounding; a two-pass pipeline selects and compares, then fetches the real judgment text and extracts the judgment-intrinsic fields strictly from it. `legalStatus` defaults to "Not independently verified".
- `document_feature_response` with `featureType:"relevant_parties"`: `payload.parties[]` (capped at 30) of `{ id, name, role, details, contactNumber, email, officialIdUrl }`, with new inbound CRUD events `add_relevant_party` / `edit_relevant_party` / `delete_relevant_party`. Contact fields are PII and are always emitted empty on auto-generation — they are only ever populated from user input.
- New inbound events `add_case_law` (resolve and verify a citation, URL or text) and `case_law_document_request` → `case_law_document_response` (manual judgment upload, always terminal so the frontend never waits forever).
- New chat scopes `chat_type:"CASE_LAWS"` and `chat_type:"RELEVANT_PARTIES"`, each injecting only its cached panel into the chat context.
- `DraftingSuggestionItem` gains `matches[]` of `{ templateId, templateName, category, matchPercent }`, sorted by score; the minimum-match-score gate is removed so weak fits are shown with an honest score instead of being hidden.
- New `gemini_client.generate_grounded()` returning text plus `google_search` grounding sources, with grounding redirect URLs resolved to their real destinations.

## Quality and Stability Improvements

- Objective / case-perspective change now **preserves** the document-derived panels (Relevant Parties, Case Laws, Upcoming Dates, Drafting suggestions) and regenerates only what the legal objective actually affects.
- Document deletion **replaces** the derived panels (analysis report, case laws, applicable laws) instead of union-merging them, so they reflect the reduced document set; uploads keep the accumulate behaviour.
- A case-law quality gate drops cards that would render half-empty or off-topic, and the Indian Kanoon scraper parses real judgment rows and retries with a trimmed query when a long query returns nothing.
- Socket-layer decomposition verified by AST method-body equality (218/218 nodes identical) and a runtime diff (222 methods, 21 handler keys, same singleton); prompt extraction verified 51/51 byte-identical including interpolated expressions. See `docs/SOCKET_EVENTS_ARCHITECTURE.md`.

## Bug Fixes

- Dateless source dates ("25 May", "26 tareekh") now resolve to the current year across every panel, via an explicit date-interpretation directive plus a deterministic backstop that rewrites ungrounded years in free text. Statute years, currency amounts, bare years and any year present in the source are never touched.
- List of Dates / Applicable Laws / Upcoming Dates loaders no longer stick after a chat panel edit that had nothing to change — a terminal-emit guard re-emits the cached payload.
- Surgical chat edits now handle TERM / DATE / YEAR corrections, not just names, including the ISO `date` field of dated entries.
- Duplicate Relevant Parties after a re-extraction are absorbed by canonical party identity plus a guarded fuzzy match; user-deleted parties are re-checked through the same logic so they stay deleted.

## Compatibility Notes

- **Breaking.** Relevant-party roles are Title-Case `Petitioner | Respondent | Witness | Victim | Accused | Other` (previously an UPPERCASE nine-role enum); the one-line text moved from `description` to `details`; `rawDesignation` / `sourceDoc` were dropped; PII contact fields were added. Legacy-cached entries are coerced to the new shape on merge.
- **Breaking.** The drafting-suggestion top-level `templateId` / `templateName` / `matchMode` / `matchScore` / `matchReason` fields are replaced at the emit boundary by `matches[]`.
- Backend and frontend must be released in step with this version.
- Public Python surface is unchanged despite the decomposition: `event_handler` remains the only symbol other modules import from `app/socket/events.py`.

---

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
