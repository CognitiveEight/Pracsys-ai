# Socket Events Architecture (`app/socket/`)

This document is the knowledge-transfer (KT) map for the socket event layer. It
was produced when the former monolithic `app/socket/events.py` (~21,000 lines,
one class) was decomposed into per-feature **mixin** modules.

## The big picture

`SocketEventHandler` is a single class assembled from many **mixins**, one per
feature. There is exactly one instance — the module-level singleton
`event_handler` — created at the bottom of `app/socket/events.py` and imported
everywhere it is needed (`client.py`, `core/lifecycle.py`, `socket/ai_service.py`,
`socket/__init__.py`, and the `scripts/verify_case_laws.py` dev tool).

```
inbound socket event (client.py @self.sio.event)
        │  event_handler.handle("<name>", data)
        ▼
SocketEventHandler.handle  ──► self._handlers["<name>"]  (registered in __init__)
        ▼
on_<event> handler (lives in a mixin)
        ▼
feature generation (mixin methods) ──► self._socket_client.emit("document_feature_response", {...})
        ▼                                   └─ cached in a PersistentWorkspaceDict (write-through to Qdrant)
re-emitted on reconnect via _do_reemit_after_reconnect
```

### Why mixins
Every method operates on shared per-workspace state (the `self._workspace_*`
caches). Mixins keep that single-object, shared-`self` model intact: behaviour
is grouped into readable files, but at runtime Python's MRO composes them back
into one class — so `self._extract_case_entities(...)`, `self._prepare_json_candidate(...)`,
etc. resolve across mixins exactly as before. **No behaviour changed in the
decomposition** (verified by an AST method-body equality check against the
pre-split file plus a runtime method/handler-key diff).

## File map

| File | Class | Responsibility |
|------|-------|----------------|
| `events.py` | `SocketEventHandler` | **Composition root**: `__init__` (ALL per-workspace state + cache dicts), `register`/`handle`/`set_socket_client`, `_register_default_handlers`, retained class constants, and the `event_handler` singleton. |
| `event_utils.py` | — | Pure, stdlib-only module helpers, constants, and regexes (`scrub_app_brand`, `_format_document_for_prompt`, `_clamp_annexure_references`, `EventHandler` type, `CHAT_*` thresholds, …). A dependency leaf. |
| `handlers/_shared.py` | `JsonHelpersMixin` | JSON extraction/repair helpers used across features. |
| `handlers/documents.py` | `DocumentsMixin` | Shared document-content assembly, inventory, and case-signal extraction. |
| `handlers/document_reload.py` | `DocumentReloadMixin` | `on_document_reload_request` — one document re-read: purge its vectors and manifest entry, re-fetch and re-extract it, then re-run the feature suite. Owns the `requestId` de-dupe ledger and the target-id resolution. |
| `handlers/state_roles.py` | `StateRolesMixin` | Cache hydration from Qdrant + workspace-role memory/restore/resolve. |
| `handlers/extraction.py` | `ExtractionMixin` | Case-entity "deep agents" (parties/timeline/assets) + `_extract_case_entities` (content-hash cached). |
| `handlers/strategy.py` | `StrategyMixin` | Strategy & objective generation, edits, and objective-change intent classification. |
| `handlers/analysis_report.py` | `AnalysisReportMixin` | Analysis report generation, scoring, feedback (`on_analysis_report_feedback`, `on_added_missing_information`, `on_missing_information_edited`), rejection memory, panel-update parsing. |
| `handlers/list_of_dates.py` | `ListOfDatesMixin` | List of Dates (chronology) generation, emit, feedback. |
| `handlers/applicable_laws.py` | `ApplicableLawsMixin` | Applicable laws (acts/sections) generation + Indian Kanoon search + feedback. |
| `handlers/case_laws.py` | `CaseLawsMixin` | Case-law precedent retrieval, ranking, judgment enrichment, digest. |
| `handlers/relevant_parties.py` | `RelevantPartiesMixin` | Relevant Parties extraction + role classification. |
| `handlers/procedural_timeline.py` | `ProceduralTimelineMixin` | Upcoming/procedural dates generation + feedback. |
| `handlers/drafting.py` | `DraftingMixin` | Drafting Assistant suggestions + template-driven refresh. |
| `handlers/objective_change.py` | `ObjectiveChangeMixin` | Background schedulers, the strategy→analysis→timeline pipeline, and all objective-change merge/dedupe helpers. |
| `handlers/law_relevance.py` | `LawRelevanceMixin` | Lawyer-selected applicable-law relevance assessment (`on_applicable_law_relevance_request`). |
| `handlers/panel_edits.py` | `PanelEditsMixin` | User add/edit/delete for Case Laws and Relevant Parties panels. |
| `handlers/docx_export.py` | `DocxExportMixin` | `on_analysis_docx_generate_request`. |
| `handlers/template_sync.py` | `TemplateSyncMixin` | Global Template Library feed (`global_template_sync`, connect-time snapshot) + global blueprint proposals + `ensure_templates_synced`. |
| `handlers/workspace_metadata.py` | `WorkspaceMetadataMixin` | Workspace title/summary/category/description generation + fallbacks. |
| `handlers/workspace_events.py` | `WorkspaceEventsMixin` | Connection lifecycle, `_do_reemit_after_reconnect`, and the big `on_workspace_created` / `on_workspace_sync` entrypoints. |
| `handlers/workspace_purge.py` | `WorkspacePurgeMixin` | `on_workspace_deleted` — the compliance purge: honours the backend's absolute `purgeAfter`, dedupes the expected re-sends, and is revoked by any live workspace traffic via `cancel_pending_purge`. |
| `handlers/run_control.py` | `RunControlMixin` | `on_workspace_stop_request` / `on_workspace_resume_request` — Stop and "Finish update". Owns the run fence: every workspace-scoped event runs in a `(workspace_id, epoch)` run scope (`_dispatch_in_run_scope`, called from `handle`); a stop bumps the epoch, cancels the registered run tasks, and from then on the `_socket_client` proxy suppresses the stopped run's `document_feature_response` / `workspace:refresh`. Resume declares exactly `reEmitFeatures` and produces them in product order. |
| `handlers/chat.py` | `ChatMixin` | `on_user_message_request` (intelligence chat), drafting-assistant chat, `on_probing_prompts_request`. |
| `handlers/suggestion_chips.py` | `SuggestionChipsMixin` | Suggestion-chip generation for panels and chat. |

## Conventions when working here

- **State lives only in `events.py` `__init__`.** Mixins are stateless behaviour; they read/write `self._workspace_*`. To add a new cache, add it to `__init__` (as a `PersistentWorkspaceDict(CacheKinds.X)`), add the `CacheKind` in `app/workspace/persistence.py`, and re-emit it in `_do_reemit_after_reconnect`.
- **Adding a new inbound event:** write `on_<event>` in the relevant mixin, register it in `_register_default_handlers` (events.py), and add the `@self.sio.event` shim in `app/socket/client.py`.
- **Adding a new feature panel:** create a `XxxMixin` in `handlers/`, add it to the `SocketEventHandler(...)` base list and the `handlers/__init__.py` re-export, schedule generation in `objective_change._schedule_*` runner, cache + re-emit. (The Case Laws / Relevant Parties mixins are the reference implementations.)
- **Mixins must never import `events.py`** (avoids a cycle). They import shared helpers from `app.socket.event_utils` and call sibling methods via `self.`.
- **Prompts live in `app/prompts/<feature>_prompts.py`.** Each large prompt is a keyword-only builder function (e.g. `case_laws_prompts.select_case_laws_prompt(...)`) whose f-string is the verbatim prompt text; handler methods build the interpolated values and pass them as kwargs. To change wording, edit the builder. The prompt packages depend only on stdlib + `app.prompts.legal_intelligence_system` (never on `app.socket`), so they stay a clean leaf. (Globally-stable values — `json` and the output directives — are imported inside the prompt module; everything else, including `self` for the two prompts that call back into document-inventory helpers, is passed in.)
- **Big files remain** (`analysis_report.py`, `strategy.py`, `objective_change.py`, `workspace_events.py`, `chat.py`) because individual methods (e.g. `on_user_message_request`, `on_workspace_sync`) are themselves very large. Breaking those methods into smaller pieces is deliberate future work — it was out of scope for the no-behaviour-change decomposition.
