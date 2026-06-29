"""
Socket.IO event handlers with Intelligence Chat Agent integration.
Updated to match backend Socket.IO specification.
"""
import asyncio
import json
import re
import time
import uuid
from datetime import date as date_type
from datetime import datetime
from html import unescape
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus

import httpx
from loguru import logger

from app.agents.role_models import RoleEnum
from app.core.config import settings
from app.langgraph.integration import workspace_role_integration
from app.observability.audit import audit_logger
from app.processing.audio_transcription import (
    _has_devanagari as _audio_has_devanagari,
    transliterate_formatted_transcript_to_hinglish,
)
from app.processing.document_processor import document_processor
from app.search.vertex_ai_search import search_indiankanoon as vertex_search_indiankanoon
from app.search.case_law_finder import (
    fetch_judgment_text,
    find_case_law_sources,
    scrape_indiankanoon,
)
from app.prompts.applicable_acts import (
    TARGETED_INDIAN_ACTS,
    format_targeted_acts_for_prompt,
)
from app.prompts.legal_intelligence_system import (
    ANNEXURE_UPLOAD_GATE_MESSAGE,
    APP_BRAND_EXCLUSION_DIRECTIVE,
    ENGLISH_ONLY_OUTPUT_DIRECTIVE,
    NAME_CAPITALISATION_DIRECTIVE,
    get_legal_intelligence_system_instruction,
    get_structured_document_feature_system_instruction,
)
from app.langgraph.chat_nodes import (
    _last_ai_message_from_history,
    is_user_affirmation_to_continue,
    merge_intelligence_chat_history,
)
from app.langgraph.list_of_dates_agent import (
    list_of_dates_temporal_agent,
    snap_hallucinated_year,
)
from app.socket.schemas import (
    AIResponseMetadata,
    AIResponsePayload,
    AnalysisDocxGenerateRequestPayload,
    ApplicableLawRelevanceRequestPayload,
    TemplateSyncPayload,
    TemplateSyncResponsePayload,
    UserMessageRequestPayload,
    WorkspaceCreatedPayload,
    WorkspaceSyncPayload,
)
from app.utils.exceptions import SocketEventError
from app.workspace.custom_instructions import (build_custom_instruction_suffix,
                                               hydrate_custom_instructions_from_cache,
                                               set_custom_instructions)
from app.workspace.manager import workspace_manager
from app.workspace.persistence import (CacheKinds, PersistentWorkspaceDict,
                                       hydrate_workspace_state, persist_state,
                                       restore_state)
from agent import process_analysis_docx_request

from app.socket.event_utils import (
    EventHandler,
    _STRATEGY_ROLE_LABELS_RE,
    OBJECTIVE_UPDATE_OFFER_QUESTION,
    CHAT_SMALL_WORKSPACE_DOC_THRESHOLD,
    CHAT_SEARCH_CHUNK_LIMIT,
    CHAT_MAX_DOCS_INCLUDED,
    CHAT_MAX_CHARS_PER_DOC,
    CHAT_MAX_TOTAL_CHARS,
    CHAT_MIN_SCORE_THRESHOLD,
    _ANNEXURE_REF_RE,
    _ROMAN_VALUES,
    SPEAKER_LABEL_GUARDRAIL,
    _ENUM_PREFIX_RE,
    _UI_TRUNCATION_RE,
    _APP_BRAND_RE,
    _STRENGTH_POINT_MIN_LEN,
    _ANNEXURE_SINGLE_RE,
    _ANNEXURE_PLURAL_RE,
    _OVERVIEW_CONTACTED_DATE_RE,
    _OVERVIEW_TO_PHONE_RE,
    _roman_or_int,
    _derive_report_case_summary,
    _coerce_analysis_text_item,
    _strip_enum_prefix,
    _strip_ui_truncated_tokens,
    scrub_app_brand,
    _coerce_analysis_text_list,
    _strip_strength_point_statute_boilerplate,
    _annexure_wording_for_next_steps,
    _clamp_annexure_references,
    _format_document_for_prompt,
    _normalize_report_mobile_from_overview,
    _overview_conversation_date_to_iso,
    extract_overview_conversation_report_contact_meta,
)
from app.socket.handlers import (
    JsonHelpersMixin,
    DocumentsMixin,
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
    DocxExportMixin,
    TemplateSyncMixin,
    WorkspaceMetadataMixin,
    WorkspaceEventsMixin,
    ChatMixin,
    SuggestionChipsMixin,
)


class SocketEventHandler(
    JsonHelpersMixin,
    DocumentsMixin,
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
    DocxExportMixin,
    TemplateSyncMixin,
    WorkspaceMetadataMixin,
    WorkspaceEventsMixin,
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
        # Procedural timeline payload per workspace (upcoming filing/hearing/compliance/evidence/adjournment dates)
        self._workspace_procedural_timeline_payload: Dict[
            str, List[Dict[str, Any]]
        ] = PersistentWorkspaceDict(CacheKinds.PROCEDURAL_TIMELINE)
        # Drafting Assistant — proactive suggested drafts panel per workspace.
        # Shape: {"suggestedDrafts": [{"title","relevance","description"}, ...]}
        self._workspace_drafting_payload: Dict[str, Dict[str, Any]] = (
            PersistentWorkspaceDict(CacheKinds.DRAFTING_SUGGESTIONS)
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
        # Re-emit after reconnect when socket dropped right after analysis report (so backend gets the update)
        self._had_disconnect: bool = False
        self._pending_reemit_workspace_id: Optional[str] = None
        self._pending_reemit_user_id: Optional[str] = None
        # Per-workspace coalescing for analysis_report_feedback. Bulk-reject sends
        # N events in ~2ms; processing them sequentially under a lock is correct
        # but takes ~19s × N (Gemini scoring + explanation regeneration). We
        # instead debounce: events accumulate in a queue, and a single batch
        # flush kicks off ~500ms after the last event, removing every rejected
        # item at once and emitting ONE updated report. The lock still guards
        # the flush itself so a re-burst can't race a flush in flight.
        self._analysis_feedback_locks: Dict[str, asyncio.Lock] = {}
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
        # Last ai_response payload; re-emit after reconnect when socket dropped right after emit so backend receives it
        self._pending_ai_response: Optional[Dict[str, Any]] = None
        # De-dup guard for replayed workspace_sync events (e.g., reconnect replay).
        # workspace_id -> {"fingerprint": str, "ts": float}
        self._last_workspace_sync_fingerprint: Dict[str, Dict[str, Any]] = {}
        # Guard against overlapping analysis-report refresh tasks from chat marker misses.
        self._analysis_refresh_inflight: Set[str] = set()
        # During objective-change regeneration, suppress stale background analysis_report emits
        # from older pipelines so FE receives only the fresh objective-aligned report.
        self._suppress_analysis_emit_until_ts: Dict[str, float] = {}
        self._register_default_handlers()

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
        self.register("user_message_request", self.on_user_message_request)
        self.register("probing_prompts_request", self.on_probing_prompts_request)
        self.register("analysis_report_feedback", self.on_analysis_report_feedback)
        self.register(
            "added_missing_information",
            self.on_added_missing_information,
        )
        self.register(
            "applicable_law_relevance_request",
            self.on_applicable_law_relevance_request,
        )
        # Case Laws — user manually adds a precedent (citation / url / text).
        self.register("add_case_law", self.on_add_case_law)
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

        logger.info("✅ Event handlers registered")

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

        if handler:
            try:
                logger.debug("Handling event: {} with data: {}", event_name, data)
                await handler(data)
            except Exception as e:
                logger.error("Error handling event {}: {}", event_name, str(e))
                logger.exception(e)
                raise SocketEventError(
                    f"Failed to handle event: {event_name}",
                    details={"event": event_name, "error": str(e)},
                )
        else:
            logger.warning("No handler registered for event: {}", event_name)

    _EMPTY_CASE_ENTITIES: Dict[str, List[Any]] = {
        "parties": [],
        "dates": [],
        "properties": [],
        "amounts": [],
        "suit_numbers": [],
        "statutes": [],
        "annexures": [],
    }

    _PARTY_ROLES = {
        "PETITIONER",
        "RESPONDENT",
        "COMPLAINANT",
        "ACCUSED",
        "VICTIM",
        "WITNESS",
        "ADVOCATE",
        "AUTHORITY",
        "OTHER",
    }

    _ANALYSIS_FEEDBACK_SCORE_BUMP_DEFAULT = 4

    _ADDED_MISSING_INFO_SCORE_DROP_DEFAULT = 3

    _ANALYSIS_REPORT_MAX_SCORE_WITH_PENDING_ITEMS = 95

    _ANALYSIS_REPORT_DERIVED_SCORE_FLOOR = 30


event_handler = SocketEventHandler()
