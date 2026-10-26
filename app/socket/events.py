"""
Socket.IO event handlers with Intelligence Chat Agent integration.
Updated to match backend Socket.IO specification.
"""
import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger

from app.prompts.legal_intelligence_system import (
    get_legal_intelligence_system_instruction,
)
from app.utils.exceptions import SocketEventError
from app.workspace.persistence import (CacheKinds, PersistentWorkspaceDict)

from app.socket.event_utils import (
    EventHandler,
)
from app.socket.handlers import (
    JsonHelpersMixin,
    DocumentsMixin,
    DocumentReloadMixin,
    StateRolesMixin,
    ExtractionMixin,
    StrategyMixin,
    AnalysisReportMixin,
    ListOfDatesMixin,
    ApplicableLawsMixin,
    CaseLawsMixin,
    RelevantPartiesMixin,
    ProceduralTimelineMixin,
    DraftingMixin,
    ObjectiveChangeMixin,
    LawRelevanceMixin,
    PanelEditsMixin,
    UserFactsMixin,
    DocxExportMixin,
    TemplateSyncMixin,
    WorkspaceMetadataMixin,
    WorkspaceEventsMixin,
    WorkspacePurgeMixin,
    RunControlMixin,
    ChatMixin,
    SuggestionChipsMixin,
)


#: Inbound events the frontend/backend polls on a timer.
_POLLING_EVENTS = frozenset({"probing_prompts_request", "ai_heartbeat_ping"})


class SocketEventHandler(
    JsonHelpersMixin,
    DocumentsMixin,
    DocumentReloadMixin,
    StateRolesMixin,
    ExtractionMixin,
    StrategyMixin,
    AnalysisReportMixin,
    ListOfDatesMixin,
    ApplicableLawsMixin,
    CaseLawsMixin,
    RelevantPartiesMixin,
    ProceduralTimelineMixin,
    DraftingMixin,
    ObjectiveChangeMixin,
    LawRelevanceMixin,
    PanelEditsMixin,
    UserFactsMixin,
    DocxExportMixin,
    TemplateSyncMixin,
    WorkspaceMetadataMixin,
    WorkspaceEventsMixin,
    WorkspacePurgeMixin,
    RunControlMixin,
    ChatMixin,
    SuggestionChipsMixin,
):
    """Socket.IO event handler.

    Composition root: owns all per-workspace state (``__init__``) and the
    event dispatch (register/handle); every feature's behaviour lives in the
    mixins in ``app/socket/handlers/``. The public surface (the
    ``event_handler`` singleton, all ``on_*`` handlers, ``handle``/``register``/
    ``set_socket_client``) is unchanged.
    """

    PROBING_THROTTLE_SECONDS = 1.0

    def __init__(self):
        self._handlers: Dict[str, EventHandler] = {}
        self._socket_client = None  # Will be set by main service
        # Legal knowledge graph (app/graph/store.py); None when NEO4J_URL is unset.
        self._graph_store = None
        # NOTE: every per-workspace cache below is a PersistentWorkspaceDict so
        # that writes (`self._workspace_X[workspace_id] = ...`) are mirrored to
        # Qdrant in the background. After an AI service restart we hydrate the
        # cache by reading these same kinds back via `restore_state`.
        self._workspace_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("strategy_suggestion_chips")
        )  # workspace_id -> 15-16 strategy chips
        self._workspace_analysis_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("analysis_suggestion_chips")
        )  # workspace_id -> 15-16 analysis-report chips
        self._workspace_chat_suggestion_chips: Dict[str, List[str]] = (
            PersistentWorkspaceDict("chat_suggestion_chips")
        )  # workspace_id -> 10-12 contextual chips (refreshed per AI reply; we return random 4 on each request)
        # Drafting Assistant — separate chip cache so the Drafting panel
        # never serves the intelligence-chat chips. Keyed by either
        # actionable_id (per draft session) or workspace_id (panel-level
        # fallback when the polling request doesn't carry actionableId).
        # In-memory only; chips are cheap to regenerate.
        self._workspace_drafting_suggestion_chips: Dict[str, List[str]] = {}
        self._last_workspace_id_with_chips: Optional[str] = (
            None  # fallback when request workspace has no chips
        )
        self._last_probing_emit_time: Dict[str, float] = (
            {}
        )  # workspace_id -> time of last probing_prompts_response emit
        # Cache last document_feature_response payloads per workspace so we can re-emit reliably after reconnect
        # (backend sometimes disconnects the socket during long AI work)
        # - document_assessments: initial assessment for all documents
        # - workspace_metadata: derived case title/category/description
        # - strategy_objective: detailed strategy/opinion
        # - analysis_report: score + strengths/weaknesses
        # - list_of_dates: timeline extracted from documents
        self._workspace_document_assessments_payload: Dict[str, Any] = (
            PersistentWorkspaceDict(CacheKinds.DOCUMENT_ASSESSMENTS)
        )
        self._workspace_metadata_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.WORKSPACE_METADATA_PAYLOAD
        )
        self._workspace_strategy_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.STRATEGY_PAYLOAD
        )
        self._workspace_analysis_payload: Dict[str, Any] = PersistentWorkspaceDict(
            CacheKinds.ANALYSIS_REPORT
        )
        # Per-workspace cache of the extracted case_entities dict so a single
        # generation cycle that runs strategy → analysis_report extracts the
        # entities ONCE. Keyed by workspace_id, value is (content_sha1, entities).
        # The content hash automatically busts the cache when documents change.
        self._workspace_case_entities_cache: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        # Per-workspace memory of rejected analysis-report items so that an
        # objective-change regeneration does not silently re-introduce a
        # missingInfo / weakness / risk the user already dismissed via
        # analysis_report_feedback. Shape per workspace_id:
        #   {"missingInfo": [str, ...], "riskOrWeakness": [str, ...]}
        self._workspace_rejected_analysis_items: Dict[str, Dict[str, List[str]]] = (
            PersistentWorkspaceDict(CacheKinds.REJECTED_ANALYSIS_ITEMS)
        )
        # Per-workspace memory of missingInfo entries the user added via
        # added_missing_information — protected from the answered-by-upload
        # resolution pass (only analysis_report_feedback removes them).
        self._workspace_user_added_missing_info: Dict[str, List[str]] = (
            PersistentWorkspaceDict(CacheKinds.USER_ADDED_MISSING_INFO)
        )
        # Per-workspace ledger of missingInfo items the lawyer re-worded via
        # missing_information_edited ({itemId: {text, priorTexts, editedAt,
        # requestId}}). Re-applied by _tag_analysis_report_against_current_state
        # so a regeneration from the documents cannot revert the lawyer's text.
        # Instance attribute so the workspace-purge sweep erases it.
        self._workspace_edited_missing_info: Dict[str, Dict[str, Dict[str, Any]]] = (
            PersistentWorkspaceDict(CacheKinds.EDITED_MISSING_INFO)
        )
        # Facts the lawyer corrected in chat ("the disputed amount is 6 lakh USD,
        # not 16 lakh"). Mirrors _workspace_removed_documents: a user decision
        # that outlives the panel it was made on. Every generator prompt carries
        # it as an AUTHORITATIVE USER CORRECTIONS block, and
        # _apply_fact_corrections_to_payload enforces it deterministically on the
        # way out — without both, the next document upload regenerates from
        # documents that still say 16 lakh and reverts the correction.
        # Deliberately an instance attribute (not a module-level dict like
        # app/workspace/custom_instructions.py) so the workspace-purge sweep over
        # self.__dict__ erases it: these entries hold verbatim case facts.
        # Shape: {workspace_id: [{id, slot, description, was, now, superseded,
        #                         assertedAt, source}, ...]}
        self._workspace_user_facts: Dict[str, List[Dict[str, Any]]] = (
            PersistentWorkspaceDict(CacheKinds.USER_ASSERTED_FACTS)
        )
        # List of Dates timeline per workspace (chronological events from case documents)
        self._workspace_list_of_dates_payload: Dict[str, List[Dict[str, Any]]] = (
            PersistentWorkspaceDict(CacheKinds.LIST_OF_DATES)
        )
        # Applicable laws payload per workspace (acts/sections inferred from docs + case law search)
        self._workspace_applicable_laws_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.APPLICABLE_LAWS)
        )
        # Case laws payload per workspace (relevant precedents matched from docs,
        # each backed by a real source URL). Shape: {"caseLaws": [{...}, ...]}
        self._workspace_case_laws_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.CASE_LAWS)
        )
        # Relevant parties payload per workspace (people/orgs extracted from docs
        # and classified into a role enum). Shape: {"parties": [{...}, ...]}
        self._workspace_relevant_parties_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.RELEVANT_PARTIES)
        )
        # Names of parties the user has deleted, so regeneration won't re-add
        # them. Shape: {workspace_id: [normalized_name, ...]}
        self._workspace_deleted_parties: Dict[str, List[str]] = (
            PersistentWorkspaceDict(CacheKinds.DELETED_PARTIES)
        )
        # Ledger of documents the user uploaded and later deleted (mirrors
        # _workspace_deleted_parties: memory of a user removal that survives
        # regeneration). Snapshotted from the document_assessments entry by
        # _prune_document_assessments before that entry is dropped, so the
        # documentType and key fields outlive the document. The analysis report
        # reports each material entry as a missingInfo gap; an entry is removed
        # when the same documentId is re-ingested (upload or version restore).
        # Shape: {workspace_id: [{documentId, fileName, documentType,
        #                         keyFields, removedAt}, ...]}
        self._workspace_removed_documents: Dict[str, List[Dict[str, Any]]] = (
            PersistentWorkspaceDict(CacheKinds.REMOVED_DOCUMENTS)
        )
        # Procedural timeline payload per workspace (upcoming filing/hearing/compliance/evidence/adjournment dates)
        self._workspace_procedural_timeline_payload: Dict[
            str, List[Dict[str, Any]]
        ] = PersistentWorkspaceDict(CacheKinds.PROCEDURAL_TIMELINE)
        # Drafting Assistant — proactive suggested drafts panel per workspace.
        # Shape: {"suggestedDrafts": [{"title","relevance","description"}, ...]}
        self._workspace_drafting_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.DRAFTING_SUGGESTIONS)
        )
        # Latest currentState echoed back by the frontend on workspace_sync — the
        # previously-emitted, user-curated panel state used to tag regenerated
        # items as matched (carried over from before) vs new. Read by every
        # regeneration path (sync upload/delete AND the chat-triggered
        # objective-change path). Shape mirrors schemas.WorkspaceCurrentState.
        self._workspace_current_state_payload: Dict[str, Any] = (
            PersistentWorkspaceDict(CacheKinds.CURRENT_STATE)
        )
        # In-flight request_template_sync correlations: requestId -> Future,
        # resolved by on_template_sync_response once the snapshot is ingested
        # into the in-memory template store. Lets the suggestion/kickoff path
        # await a cold-cache fill before matching.
        self._pending_template_sync: Dict[str, "asyncio.Future"] = {}
        # Per-workspace drafting-suggestion provenance, used to self-heal the
        # panel when a template finishes ingesting *after* suggestions were
        # built (e.g. a slow scanned-PDF OCR that misses the template-sync
        # window). Shape: workspace_id -> {"user_id", "role", "lib_count"},
        # where lib_count is the ready-template count the suggestions were
        # pre-matched against. When the library later grows, the panel is
        # regenerated. See _maybe_reemit_drafting_on_templates.
        self._drafting_meta_by_workspace: Dict[str, Dict[str, Any]] = {}
        # Feature payloads that were generated but NOT delivered (the socket was
        # down when the emit ran — SocketClient.emit skips silently). Keyed
        # workspace_id -> {featureType: envelope}, and persisted, because a panel
        # loader is cleared only by the arrival of its document_feature_response:
        # an undelivered payload is a stuck panel until it is re-sent. Flushed by
        # _flush_pending_feature_deliveries on (re)connect.
        self._pending_feature_deliveries: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.PENDING_FEATURE_DELIVERY)
        )
        # An objective change that was accepted but never produced a payload —
        # cancelled with its socket turn, or timed out. Nothing to re-deliver, so
        # the instruction itself is kept and re-run once on reconnect. Shape:
        # {"userId","workspaceRole","instruction","featureTypes","attempts","startedAt"}
        self._pending_objective_changes: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.PENDING_OBJECTIVE_CHANGE)
        )
        # Workspaces whose persisted recovery markers have already been read in
        # this process. A restart loses the in-memory markers, so they are
        # re-read on first contact with the workspace — but exactly once, or
        # every chat turn would re-query Qdrant for them.
        self._recovered_workspaces: Set[str] = set()
        # Strong references to detached background tasks. asyncio keeps only a
        # weak reference to a task, so a fire-and-forget create_task() can be
        # garbage-collected mid-flight; and a task whose exception is never
        # retrieved reports nothing. See _track_background_task.
        self._background_tasks: Set[asyncio.Task] = set()
        # Re-emit after reconnect when socket dropped right after analysis report (so backend gets the update)
        self._had_disconnect: bool = False
        # workspace_id -> (user_id, armed_at): every case whose cached panels
        # are replayed after a reconnect (was a single slot: only the most
        # recently active case was ever replayed).
        self._pending_reemits: Dict[str, Tuple[str, float]] = {}
        # Per-workspace coalescing for analysis_report_feedback. Bulk-reject sends
        # N events in ~2ms; processing them sequentially under a lock is correct
        # but takes ~19s × N (Gemini scoring + explanation regeneration). We
        # instead debounce: events accumulate in a queue, and a single batch
        # flush kicks off ~500ms after the last event, removing every rejected
        # item at once and emitting ONE updated report. The lock still guards
        # the flush itself so a re-burst can't race a flush in flight.
        self._analysis_feedback_locks: Dict[str, asyncio.Lock] = {}
        # Serializes the feature generation/emit pipeline per workspace so the
        # intelligence panels always arrive in product order (assessments →
        # metadata → strategy → analysis → list_of_dates → applicable_laws →
        # procedural_timeline → relevant_parties → case_laws → drafting).
        # New triggers QUEUE behind the in-flight chain (FIFO); cross-workspace
        # runs stay concurrent because locks are keyed by workspace_id.
        self._workspace_pipeline_locks: Dict[str, asyncio.Lock] = {}
        # Who holds each workspace's pipeline lock, in which stage, since when:
        # workspace_id -> {"holder", "stage", "acquiredAt", "cap", "warnedAt"}.
        # Read by the lock watchdog and the run cap (handlers/_shared.py).
        self._workspace_pipeline_lock_holder: Dict[str, Dict[str, Any]] = {}
        self._pending_feedback_queue: Dict[str, List[Dict[str, Any]]] = {}
        self._pending_feedback_task: Dict[str, asyncio.Task] = {}
        # Window for collecting bulk-reject bursts before flushing.
        self._feedback_debounce_seconds: float = 0.5
        # Max total score bump from a single batch (each valid rejection
        # contributes 1–5; capping the sum prevents huge scores when the user
        # rejects many items at once).
        self._feedback_batch_max_score_delta: int = 15
        # Chat attachment cache: chat_id -> list of {fileName, content} so follow-up questions in same chat see the document
        self._chat_attachment_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        # Intelligence chat: last assistant text per chat_id (client may omit conversation_history)
        self._intelligence_last_ai_by_chat: Dict[str, str] = {}
        # Persistent role cache keys:
        # - per chat: f"{workspace_id}:{user_id}:{chat_id}" -> role
        # - per user/workspace: f"{workspace_id}:{user_id}" -> role
        self._role_cache_by_chat: Dict[str, str] = {}
        self._role_cache_by_user_workspace: Dict[str, str] = {}
        # chat_id -> ai_response payload whose delivery failed; all re-sent on the
        # next connect (was a single slot shared by every chat, so a second
        # undelivered reply silently replaced the first).
        self._pending_ai_responses: Dict[str, Dict[str, Any]] = {}
        # De-dup guard for replayed workspace_sync events (e.g., reconnect replay).
        # workspace_id -> {"fingerprint": str, "ts": float}
        self._last_workspace_sync_fingerprint: Dict[str, Dict[str, Any]] = {}
        # De-dup guard for replayed workspace_version_restore events. Kept
        # SEPARATE from the workspace_sync guard on purpose: restoring back to a
        # version that was live minutes ago produces a document set the sync
        # fingerprint has already seen, and sharing the window would silently
        # swallow the restore. Keyed on versionId instead.
        # workspace_id -> {"fingerprint": str, "ts": float}
        self._last_version_restore_fingerprint: Dict[str, Dict[str, Any]] = {}
        # De-dup guard for replayed workspace_transferred events (same shape).
        # workspace_id -> {"fingerprint": str, "ts": float}
        self._last_workspace_transfer_fingerprint: Dict[str, Dict[str, Any]] = {}
        # Guard against overlapping analysis-report refresh tasks from chat marker misses.
        self._analysis_refresh_inflight: Set[str] = set()
        # --- workspace_deleted / compliance purge (see handlers/workspace_purge.py)
        # Deletions awaiting their purgeAfter deadline.
        #   workspace_id -> {requestId, purgeAfter, deletedAt, userId, reason,
        #                    armedAt, task}
        # Held in memory only, by design: a restart loses the timer, and the
        # backend re-sends every pending deletion on reconnect under the same
        # requestId until purgeAfter passes, which re-arms it.
        self._pending_purges: Dict[str, Dict[str, Any]] = {}
        # requestId -> first-seen time. The backend's de-duplication key.
        self._seen_purge_requests: Dict[str, float] = {}
        # Workspaces already erased. Keeps a re-send that races the purgeAfter
        # boundary from re-arming a purge for data that is already gone, and
        # marks the workspace as needing a rebuild-from-scratch on the next sync.
        #   workspace_id -> {purgedAt, requestId, userId, reason}
        self._purged_workspaces: Dict[str, Dict[str, Any]] = {}
        # chat_id -> workspace_id. Chat-scoped caches hold extracted document
        # text but are keyed by chat_id alone; without this index the purge
        # cannot reach them. Populated at the chat entry point.
        self._chat_workspace_index: Dict[str, str] = {}
        # --- document_reload_request (see handlers/document_reload.py)
        # Per-workspace ledger of reload requests: workspace_id -> {requestId ->
        # {status, ts}}. The backend re-sends a reload on a stall or reconnect
        # under the SAME requestId, so a re-send while the first attempt is in
        # flight (or shortly after it succeeded) must be swallowed — but a
        # re-send after a FAILED attempt must run, because nothing else would
        # ever retry it. Keyed by workspace_id at the top level so the
        # compliance purge's self.__dict__ sweep erases it with the workspace.
        self._document_reload_requests: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # --- workspace_stop_request / workspace_resume_request
        # (see handlers/run_control.py). Every dict below is keyed by
        # workspace_id at the top level so the compliance purge's sweep erases it.
        # Run epoch per workspace. A stop bumps it; anything running under an
        # older epoch is fenced (its panel emits and buffer writes are dropped).
        self._run_epochs: Dict[str, int] = {}
        # Tasks that make up a workspace's running update, so a stop can cancel
        # them: workspace_id -> {task: started_at}.
        self._workspace_run_tasks: Dict[str, Dict[asyncio.Task, float]] = {}
        # Local receive time of the latest workspace-scoped event. A stop whose
        # stoppedAt predates it is a stale re-send and must not fence new work.
        self._last_workspace_event_at: Dict[str, float] = {}
        # workspace_id -> {requestId: first-seen}. Stop re-sends are expected.
        self._stop_requests: Dict[str, Dict[str, float]] = {}
        # workspace_id -> {requestId: {status, ts}}, as _document_reload_requests.
        self._resume_requests: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._register_default_handlers()

    def set_graph_store(self, store) -> None:
        """Register the legal knowledge graph (``GraphStore`` or None)."""
        self._graph_store = store

    def set_socket_client(self, client):
        """Set the socket client for emitting responses."""
        self._socket_client = client
        logger.debug("Socket client registered with event handler")

    def _get_generation_system_instruction(self, workspace_role: Optional[str]) -> str:
        """Use one consistent legal system instruction across intelligence generations."""
        return get_legal_intelligence_system_instruction(
            current_role=(workspace_role or "").strip().upper() or None,
            annexures_uploaded=True,
            role_confirmed=True,
        )

    def _register_default_handlers(self) -> None:
        """Register default event handlers."""
        # Connection events
        self.register("connect", self.on_connect)
        self.register("connected", self.on_connected)
        self.register("disconnect", self.on_disconnect)
        self.register("error", self.on_error)

        # Backend-specified events (AI_Maverick listens to these)
        self.register("workspace_created", self.on_workspace_created)
        self.register("workspace_sync", self.on_workspace_sync)
        self.register(
            "workspace_version_restore", self.on_workspace_version_restore
        )
        # Workspace ownership moved to another lawyer — re-key owner state and
        # re-emit the features listed in reEmitFeatures (drafting_assistant).
        self.register("workspace_transferred", self.on_workspace_transferred)
        self.register("user_message_request", self.on_user_message_request)
        # Supporting Material "reload" on one document: purge everything held
        # for it, re-read it, re-run the workspace features over all evidence.
        self.register("document_reload_request", self.on_document_reload_request)
        # Stop / "Finish update" on a running workspace update.
        self.register("workspace_stop_request", self.on_workspace_stop_request)
        self.register("workspace_resume_request", self.on_workspace_resume_request)
        self.register("probing_prompts_request", self.on_probing_prompts_request)
        self.register("analysis_report_feedback", self.on_analysis_report_feedback)
        self.register(
            "added_missing_information",
            self.on_added_missing_information,
        )
        # Missing Information — lawyer re-worded one item (text only).
        self.register(
            "missing_information_edited",
            self.on_missing_information_edited,
        )
        self.register(
            "applicable_law_relevance_request",
            self.on_applicable_law_relevance_request,
        )
        # Case Laws — user manually adds a precedent (citation / url / text).
        self.register("add_case_law", self.on_add_case_law)
        # Case Laws — lawyer uploads a document; AI classifies + extracts it.
        self.register(
            "case_law_document_request", self.on_case_law_document_request
        )
        # Relevant Parties — user add / edit / delete of panel items.
        self.register("add_relevant_party", self.on_add_relevant_party)
        self.register("edit_relevant_party", self.on_edit_relevant_party)
        self.register("delete_relevant_party", self.on_delete_relevant_party)
        self.register(
            "analysis_docx_generate_request",
            self.on_analysis_docx_generate_request,
        )
        # Drafting Assistant — template library sync from BE.
        self.register("template_sync", self.on_template_sync)
        # Reply to our AI-initiated request_template_sync (full snapshot).
        self.register("template_sync_response", self.on_template_sync_response)
        # "Regenerate Extra Info" — backend asks the AI to re-profile a template
        # and emit a fresh blueprint proposal for the approval screen.
        self.register(
            "request_blueprint_proposal", self.on_request_blueprint_proposal
        )

        # Legacy event handlers
        self.register("workspace_updated", self.on_workspace_updated)
        self.register("workspace_deleted", self.on_workspace_deleted)

        logger.info("Event handlers registered")

    def register(self, event_name: str, handler: EventHandler) -> None:
        """
        Register an event handler.

        Args:
            event_name: Name of the event
            handler: Async function to handle the event
        """
        self._handlers[event_name] = handler
        logger.debug("Registered handler for event: {}", event_name)

    async def handle(self, event_name: str, data: Any) -> None:
        """
        Handle an incoming event.

        Args:
            event_name: Name of the event
            data: Event data
        """
        handler = self._handlers.get(event_name)
        if not handler:
            logger.warning("No handler registered for event: {}", event_name)
            return

        # Correlation context for every line this event produces, including
        # lines from tasks it spawns (contextvars are copied by create_task).
        # Ids only — never the payload: it carries signed URLs, names and
        # case text.
        body = data if isinstance(data, dict) else {}
        context = {
            "request_id": str(body.get("requestId") or uuid.uuid4().hex[:12]),
            "event": event_name,
            "workspace_id": str(body.get("workspaceId") or "-"),
        }
        if body.get("chatId"):
            context["chat_id"] = str(body["chatId"])
        # Polled every few seconds by the frontend: their start/finish lines
        # would bury everything else, so they log only at DEBUG (failures
        # still log at ERROR).
        level = "DEBUG" if event_name in _POLLING_EVENTS else "INFO"
        with logger.contextualize(**context):
            logger.log(level, "event {} received (keys: {})", event_name, sorted(body.keys()))
            started = time.monotonic()
            status = "ok"
            model_token = self._apply_model_selection(event_name, body)
            try:
                # Every workspace-scoped event runs inside its run scope, which
                # is what lets workspace_stop_request cancel and fence it.
                await self._dispatch_in_run_scope(event_name, data, handler)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as e:
                status = "error"
                logger.opt(exception=e).error("Event handler failed: {}", e)
                raise SocketEventError(
                    f"Failed to handle event: {event_name}",
                    details={"event": event_name, "error": str(e)},
                )
            finally:
                from app.workspace.model_selection import reset_message_model

                reset_message_model(model_token)
                # Long panel work runs in background tasks after this returns;
                # each of those reports its own "result" line when it emits.
                logger.log(
                    "WARNING" if status != "ok" else level,
                    "event {} finished in {:.1f}s status={}",
                    event_name, time.monotonic() - started, status,
                )

    #: Events whose ``aiModel`` sets the CASE's model (see model_selection.py).
    _CASE_MODEL_EVENTS = frozenset(
        {"workspace_created", "workspace_sync", "workspace_version_restore"}
    )

    def _apply_model_selection(self, event_name: str, body: Dict[str, Any]) -> Any:
        """Apply ``aiModel`` from an inbound event. Workspace events set the
        case's model (absent = unchanged, null/"" = default); a chat message
        overrides it for that turn only. Returns the turn-override token for
        ``handle`` to reset. Never raises."""
        try:
            from app.workspace.model_selection import set_message_model, set_workspace_model

            if event_name in self._CASE_MODEL_EVENTS and "aiModel" in body:
                set_workspace_model(str(body.get("workspaceId") or ""), body.get("aiModel"))
            elif event_name == "user_message_request" and body.get("aiModel"):
                return set_message_model(body.get("aiModel"))
        except Exception as exc:  # a bad selection must never block the event
            logger.warning("aiModel not applied: {}", exc)
        return None

    _EMPTY_CASE_ENTITIES: Dict[str, List[Any]] = {
        "parties": [],
        "dates": [],
        "properties": [],
        "amounts": [],
        "suit_numbers": [],
        "statutes": [],
        "annexures": [],
    }

    # Canonical Title-Case party roles the backend/frontend expect.
    _PARTY_ROLES = {
        "Petitioner",
        "Respondent",
        "Witness",
        "Victim",
        "Accused",
        "Other",
    }

    # Maps any (legacy/synonym) role wording → a canonical role above. Keyed by
    # UPPERCASE input; anything not present resolves to "Other".
    _PARTY_ROLE_MAP = {
        "PETITIONER": "Petitioner",
        "APPELLANT": "Petitioner",
        "COMPLAINANT": "Petitioner",
        "PLAINTIFF": "Petitioner",
        "RESPONDENT": "Respondent",
        "DEFENDANT": "Respondent",
        "WITNESS": "Witness",
        "VICTIM": "Victim",
        "ACCUSED": "Accused",
        "ADVOCATE": "Other",
        "AUTHORITY": "Other",
        "OTHER": "Other",
    }

    @classmethod
    def _normalize_party_role(cls, raw: Any) -> str:
        """Map any role wording to a canonical Title-Case role; default 'Other'."""
        return cls._PARTY_ROLE_MAP.get(str(raw or "").strip().upper(), "Other")

    _ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT = 4

    _ADDED_MISSING_INFO_SCORE_DROP_DEFAULT = 3

    _ANALYSIS_REPORT_MAX_SCORE_WITH_PENDING_ITEMS = 95

    _ANALYSIS_REPORT_DERIVED_SCORE_FLOOR = 30


event_handler = SocketEventHandler()
