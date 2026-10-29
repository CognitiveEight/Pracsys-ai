"""
Pydantic schemas for socket event data validation.
Updated to match backend Socket.IO specification.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# ============================================================================
# Backend Event Payload Schemas (Server → AI)
# ============================================================================


class EvidenceFilePayload(BaseModel):
    """
    Schema for evidence file in workspace payloads.
    Matches backend workspace_created and workspace_sync event structure.
    When backend sends isDeleted: true, the document should be removed from vector store.
    """

    originalName: str  # Original filename
    storeFileName: str  # GCS stored filename with UUID
    filename: str  # Display filename with annexure prefix
    extension: str  # File extension
    fileHash: str  # SHA-256 hash
    url: str  # GCS pre-signed URL
    path: str  # GCS storage path
    uploadedAt: str  # ISO date string
    id: Optional[str] = Field(
        None, alias="_id"
    )  # MongoDB ObjectId (maps to backend's _id)
    isDeleted: Optional[bool] = Field(
        False, alias="isDeleted"
    )  # True when user deleted this document

    class Config:
        populate_by_name = True


class WorkspaceCreatedPayload(BaseModel):
    """
    Schema for workspace_created event (Server → AI_Maverick).
    Emitted to ai_service room when new workspace is created.
    Optional customInstructions: structure (Default|Bullet Points|Paragraphs),
    tone (Default(Empathetic)|Formal|Assertive), description (free text).
    """

    workspaceId: str
    userId: str
    title: str
    workspaceRole: str
    evidenceFiles: List[EvidenceFilePayload] = []
    createdAt: str  # ISO timestamp
    structure: Optional[str] = None  # Default | Bullet Points | Paragraphs
    tone: Optional[str] = None  # Default(Empathetic) | Formal | Assertive
    description: Optional[str] = (
        None  # Optional custom description, e.g. "Use Indian legal terms only"
    )

    class Config:
        populate_by_name = True


# ============================================================================
# currentState — the previously-emitted, user-curated panel state the frontend
# sends back inside workspace_sync so regeneration can tag each item as matched
# (carried over from before) vs new. Present only on a re-run trigger
# (upload/transcript/remove/objective change); omitted on plain rename/reset.
# Each sub-block is omitted when that feature is empty. The ``status`` field
# (approved/pending/rejected) is parsed but intentionally ignored — matching is
# by text/content only.
# ============================================================================


class CurrentStateTextItem(BaseModel):
    """One curated text item (missingInfo/risks/weaknesses/relevantJurisdiction)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    text: str
    status: Optional[str] = None  # approved | pending | rejected (ignored on match)


class CurrentStatePartyItem(BaseModel):
    """One curated relevant-party (matched by name)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str


class CurrentStatePartiesGroup(BaseModel):
    """Relevant-parties grouped by curation status; flattened on match."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    approved: List[CurrentStatePartyItem] = []
    pending: List[CurrentStatePartyItem] = []


class CurrentStateDateItem(BaseModel):
    """One curated list-of-dates entry (matched by date + title)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    date: str
    title: str


class CurrentStateDatesGroup(BaseModel):
    """List-of-dates grouped by curation status; flattened on match."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    approved: List[CurrentStateDateItem] = []
    pending: List[CurrentStateDateItem] = []


class CurrentStateLawItem(BaseModel):
    """One curated applicable-law (matched by act + section; no prior id)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    act: str
    section: Optional[str] = None


class WorkspaceCurrentState(BaseModel):
    """
    Previously-emitted, user-curated panel state echoed back by the frontend.
    Every sub-block is optional (omitted when that feature is empty).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    missingInfo: Optional[List[CurrentStateTextItem]] = None
    risks: Optional[List[CurrentStateTextItem]] = None
    weaknesses: Optional[List[CurrentStateTextItem]] = None
    relevantJurisdiction: Optional[List[CurrentStateTextItem]] = None
    relevantParties: Optional[CurrentStatePartiesGroup] = None
    listOfDates: Optional[CurrentStateDatesGroup] = None
    applicableLaws: Optional[List[CurrentStateLawItem]] = None


class WorkspaceParticipant(BaseModel):
    """
    One collaborator on a SHARED workspace (workspace sharing).

    Arrives inside ``workspace_sync.participants``. ``userId`` identifies the
    collaborator; the workspace's own ``userId`` stays the case OWNER.
    ``permission`` is ``edit`` or ``view`` — it is kept as a free string rather
    than a Literal so an unrecognised value degrades to read-only (see
    ``app/workspace/participants.py``) instead of rejecting the whole sync.
    """

    userId: str
    name: Optional[str] = None
    permission: Optional[str] = None  # "edit" | "view"

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class WorkspaceSyncPayload(BaseModel):
    """
    Schema for workspace_sync event (Server → AI_Maverick).
    Emitted to ai_service room when workspace is updated.
    When user deletes documents from Supporting Material, backend may send
    deletedDocumentIds so AI can remove them from vector store and regenerate strategy.
    workspaceRole is sent when user changes role (e.g. Edit Workspace → Mediator).
    Optional custom instructions: structure, tone, description (same as workspace_created).
    ``currentState`` carries the previously-emitted panel state so regeneration
    can tag items as matched vs new (see WorkspaceCurrentState).
    """

    workspaceId: str
    userId: str
    title: Optional[str] = None
    status: Optional[str] = None
    workspaceRole: Optional[str] = (
        None  # PETITIONER | RESPONDENT | MEDIATOR | ADJUDICATOR (from Edit Workspace)
    )
    evidenceFiles: Optional[List[EvidenceFilePayload]] = None
    deletedDocumentIds: Optional[List[str]] = (
        None  # Document IDs removed from Supporting Material
    )
    updatedAt: str  # ISO timestamp
    structure: Optional[str] = None
    tone: Optional[str] = None
    description: Optional[str] = None
    currentState: Optional[WorkspaceCurrentState] = (
        None  # Previously-curated panel state (present only on re-run triggers)
    )
    # Collaborator roster. ABSENT ENTIRELY (not an empty array) when the case
    # is not shared, which is how an un-share reaches this service — see
    # `set_participants(..., authoritative=True)` in app/workspace/participants.py.
    participants: Optional[List[WorkspaceParticipant]] = None

    class Config:
        populate_by_name = True


class WorkspaceVersionRestorePayload(WorkspaceSyncPayload):
    """
    Schema for workspace_version_restore event (Server → AI_Maverick).
    Emitted when the user restores the workspace to an earlier document version.

    Deliberately extends WorkspaceSyncPayload so both sides reuse the same
    evidence-file serialisation. Two additions:

    - ``versionId`` — the version being restored TO. Required: it keys this
      handler's own idempotency guard, which is separate from the
      workspace_sync fingerprint window because restoring back to a version
      seen minutes ago is a legitimate user action, not a replay.
    - ``restoredDocumentIds`` — documents coming back into evidence (a subset of
      ``evidenceFiles``). Advisory only.

    ``evidenceFiles`` is AUTHORITATIVE: it must carry the FULL surviving
    document set at the restored version. ``deletedDocumentIds`` and
    ``restoredDocumentIds`` are cross-checks — the handler diffs
    ``evidenceFiles`` against the live vector store and reconciles to that, so a
    partially-correct payload still converges on the right state.
    """

    versionId: str  # Version being restored to
    restoredDocumentIds: List[str] = []  # Documents returning to evidence (advisory)

    class Config:
        populate_by_name = True


class DocumentReloadTargetFile(EvidenceFilePayload):
    """The one document a ``document_reload_request`` re-reads.

    Same shape as any other evidence file — deliberately, because the reload is
    a RE-READ of a document that is already in evidence, not a new upload: the
    ``fileHash`` is unchanged and the same file appears again inside
    ``evidenceFiles``. Only ``url`` differs (the backend re-signs it) plus:

    - ``previousQualityScore`` — the readability score this service reported
      last time, or ``None`` if the document was never scored. Advisory: it is
      logged and audited so a reload can be judged against its predecessor, and
      never used to decide whether to re-ingest.
    """

    previousQualityScore: Optional[float] = None

    class Config:
        populate_by_name = True


class DocumentReloadRequestPayload(BaseModel):
    """
    Schema for document_reload_request event (Server → AI_Maverick).

    The user hit "reload" on one document in Supporting Material (or the
    backend did, because the document scored badly). The AI purges everything
    it holds for that document, re-fetches and re-extracts it, and re-runs the
    workspace feature suite over the FULL evidence set.

    - ``requestId`` de-dupes retries. The backend records the request in its AI
      ledger and re-sends on a stall or reconnect, so a re-send under the same
      id is expected rather than an error.
    - ``targetFile`` is the document being reloaded. It carries no document id
      of its own in the backend contract, so the handler resolves one (see
      ``_resolve_reload_document_id``) — an id-less reload cannot be ingested.
    - ``evidenceFiles`` is every ACTIVE file, so the feature re-run has full
      context. Nothing is being removed here, so no soft-deleted entries.
    - ``currentState`` is MANDATORY on this event: without it the re-run would
      wipe the items the lawyer already approved or rejected.
    - ``participants`` is context only (present on shared cases); access is
      enforced by the backend, and a reload says nothing about sharing, so the
      handler applies it non-authoritatively.
    """

    requestId: str  # 'dlr_<docId first 8>_<epochMs>' — de-dupes retries
    workspaceId: str
    userId: str  # always the case OWNER, shared or not
    title: Optional[str] = None
    status: Optional[str] = None
    workspaceRole: Optional[str] = None
    reason: Optional[str] = None  # 'user_requested' | 'poor_quality'
    targetFile: DocumentReloadTargetFile
    evidenceFiles: Optional[List[EvidenceFilePayload]] = None
    currentState: Optional[WorkspaceCurrentState] = None
    participants: Optional[List[WorkspaceParticipant]] = None
    timestamp: Optional[str] = None  # ISO timestamp

    class Config:
        populate_by_name = True


class StoppedByInfo(BaseModel):
    """Who actually clicked Stop — may be a collaborator, never re-keys state.

    ``name`` is personal data: it is accepted so the payload validates, and is
    never logged or written anywhere.
    """

    userId: str
    name: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class WorkspaceStopRequestPayload(BaseModel):
    """
    Schema for workspace_stop_request event (Server → AI_Maverick).

    The lawyer clicked Stop on a running update. FIRE-AND-FORGET: the backend
    never waits for a reply, stamps its own run stopped, and discards every
    feature response that arrives afterwards. This service's job is to stop
    paying for work nobody will see — cancel in-flight work, drop buffered
    deliveries (including the persisted buffer, so a restart cannot resurface
    them), and disarm recovery markers.

    - ``requestId`` — 'stp_<first 8 of workspaceId>_<epochMs>'. Re-sends under
      the same id are EXPECTED and swallowed.
    - ``userId`` — always the case OWNER, as on every other event.
    - ``stoppedBy`` — who clicked (may be a collaborator). Informational.
    - ``receivedFeatures`` — delivered and saved; settled. Informational.
    - ``pendingFeatures`` — abandoned. Decides whether an interrupted objective
      change is kept for a later ``workspace_resume_request``.
    - ``stoppedAt`` — ISO instant of the click; used to recognise a stale
      re-send that arrives after a newer run has already started.

    Every field except ``workspaceId`` is optional on purpose: a malformed stop
    must still cancel, because the moment a lawyer most wants Stop is when
    something is already wrong.
    """

    requestId: Optional[str] = None
    workspaceId: str
    userId: Optional[str] = None
    stoppedBy: Optional[StoppedByInfo] = None
    receivedFeatures: List[str] = []
    pendingFeatures: Optional[List[str]] = None
    stoppedAt: Optional[str] = None
    timestamp: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class WorkspaceResumeRequestPayload(BaseModel):
    """
    Schema for workspace_resume_request event (Server → AI_Maverick).

    The lawyer clicked "Finish update" on a stopped run. Produce EXACTLY the
    features in ``reEmitFeatures`` — no more — because the frontend arms one
    spinner per featureType declared in ``workspace:refresh`` and only a
    ``document_feature_response`` of that type clears it. The other features
    completed and are settled; re-sending them would overwrite good content.

    - ``requestId`` — 'rsm_<first 8 of workspaceId>_<epochMs>'. De-duped like
      ``document_reload_request``: a re-send while in flight or just completed
      is swallowed, a re-send after a failed attempt re-runs.
    - ``evidenceFiles`` — the FULL active document set (same shape as
      workspace_sync); authoritative, reconciled against the vector store.
    - ``currentState`` — required so the resumed features honour isMatched.
    - ``participants`` — same semantics as workspace_sync (absent = not shared).
    """

    requestId: Optional[str] = None
    workspaceId: str
    userId: str  # always the case OWNER
    title: Optional[str] = None
    status: Optional[str] = None
    workspaceRole: Optional[str] = None
    reEmitFeatures: List[str] = []
    evidenceFiles: Optional[List[EvidenceFilePayload]] = None
    currentState: Optional[WorkspaceCurrentState] = None
    participants: Optional[List[WorkspaceParticipant]] = None
    timestamp: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class TransferredUserInfo(BaseModel):
    """New owner details embedded in workspace_transferred."""

    id: str
    name: Optional[str] = None
    email: Optional[str] = None

    class Config:
        populate_by_name = True


class WorkspaceDeletedPayload(BaseModel):
    """
    Schema for workspace_deleted event (Server → AI_Maverick).

    The backend's erasure cascade does not reach this service: when a lawyer
    deletes a case, the backend drops its own records and files, but the copy
    embedded in our vector store is outside that cascade and used to survive
    indefinitely. This event is the instruction to erase it, so that "your case
    is deleted" is true of the AI's copy too.

    Three properties of this event drive the handler:

    - ``purgeAfter`` is ABSOLUTE and authoritative. Delete at or after that
      instant; never recompute it from ``purgeAfterDays`` (which is
      informational) or from a local clock offset. A timestamp rather than a
      duration means there is nothing to configure here and no clock-skew
      argument between the two services.
    - DUPLICATES ARE EXPECTED. The backend emit is fire-and-forget, so an event
      sent while this service is mid-deploy evaporates with no error. The
      backend therefore persists every pending deletion and re-sends it on each
      reconnect, with the SAME ``requestId``, until ``purgeAfter`` passes. The
      handler dedupes on ``requestId`` and is idempotent per workspace.
    - The deletion is REVOCABLE until it runs. A restore inside the grace
      window arrives as ordinary workspace traffic (created / sync / restore /
      transfer), which cancels the pending purge — without that, a workspace
      restored on day 2 would be silently erased on day 7.
    """

    workspaceId: str  # 24-hex MongoDB ObjectId; the only key needed to purge
    userId: str  # Case OWNER, always — never whoever pressed delete
    deletedAt: str  # ISO 8601 UTC
    purgeAfter: str  # ISO 8601 UTC — absolute; delete at or after this instant
    purgeAfterDays: Optional[int] = None  # Informational; purgeAfter wins
    reason: Optional[str] = None  # "user_deleted" | "admin_deleted"
    requestId: Optional[str] = None  # wd_<first8-of-workspaceId>_<epoch_ms>
    event: Optional[str] = None  # Always "workspace_deleted"

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class WorkspaceTransferredPayload(BaseModel):
    """
    Schema for workspace_transferred event (Server → AI_Maverick).
    Workspace ownership moved from previousUserId to newUserId. Backend keeps
    all workspace data; AI only re-keys owner state and re-emits the features
    named in reEmitFeatures (currently ["drafting_assistant"], which must be
    rebuilt against the new owner's template library).
    evidenceFiles is the full active file set — informational only, the
    documents are already processed/vectorized.
    """

    workspaceId: str
    previousUserId: str
    newUserId: str
    newUser: Optional[TransferredUserInfo] = None
    title: Optional[str] = None
    workspaceRole: Optional[str] = None
    status: Optional[str] = None  # active | archived
    evidenceFiles: List[EvidenceFilePayload] = []
    reEmitFeatures: List[str] = []
    transferredAt: Optional[str] = None  # ISO timestamp

    class Config:
        populate_by_name = True


class AnalysisReportFeedbackPayload(BaseModel):
    """
    Payload for analysis_report_feedback event (App → Backend → AI service).
    User rejects an analysis report item (missingInfo or riskOrWeakness) as not relevant.
    """

    workspaceId: str
    userId: str
    workspaceRole: str
    section: Literal["missingInfo", "riskOrWeakness"]
    rejectedValue: str
    isRejected: bool = True
    timestamp: str  # ISO timestamp

    class Config:
        populate_by_name = True


class RejectionFeedbackItem(BaseModel):
    """Why one reject did or did not move the readiness score (AI → backend)."""

    section: Literal["missingInfo", "riskOrWeakness"]
    rejectedValue: str  # echoes AnalysisReportFeedbackPayload.rejectedValue
    isValidRejection: bool  # True when the reject removed an item; False if already removed
    scoreIncrease: int  # section weight (missingInfo 5, weakness 5, risk 3); 0 when stale
    reason: str  # one sentence for the lawyer; empty when none was produced


class RejectionFeedback(BaseModel):
    """``rejectionFeedback`` on the ``analysis_report`` payload, present only on
    the emit that answers ``analysis_report_feedback``. Additive and optional;
    never cached or replayed."""

    scoreChange: int  # applied; may be below the sum of scoreIncrease (caps)
    items: List[RejectionFeedbackItem]


class AddedMissingInformationPayload(BaseModel):
    """
    Payload for added_missing_information event (App → Backend → AI service).
    User manually supplies additional information to address previously-flagged
    missingInfo items. Free-form title + description (no specific item id).
    AI assesses relevance against the case; if related, the quality score is
    decreased and qualityScoreExplanation is regenerated.
    """

    workspaceId: str
    userId: str
    title: str
    description: str
    workspaceRole: Optional[str] = None
    timestamp: str  # ISO timestamp

    class Config:
        populate_by_name = True


class MissingInformationEditedBy(BaseModel):
    """Who edited the item (``missing_information_edited.editedBy``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    userId: Optional[str] = Field(
        None, validation_alias=AliasChoices("userId", "user_id")
    )


class MissingInformationEditedPayload(BaseModel):
    """
    Payload for missing_information_edited (Server → AI service).
    A lawyer rewrote the wording of one Missing Information item. Only the text
    changes: no status change, no new fields. The backend emits nothing when the
    trimmed text is unchanged. ``userId`` is the case OWNER; ``editedBy`` is
    whoever typed the edit (a collaborator on a shared case).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requestId: Optional[str] = Field(
        None, validation_alias=AliasChoices("requestId", "request_id")
    )
    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: Optional[str] = Field(
        None, validation_alias=AliasChoices("userId", "user_id")
    )
    editedBy: Optional[MissingInformationEditedBy] = Field(
        None, validation_alias=AliasChoices("editedBy", "edited_by")
    )
    itemId: str = Field(..., validation_alias=AliasChoices("itemId", "item_id"))
    previousText: Optional[str] = Field(
        None, validation_alias=AliasChoices("previousText", "previous_text")
    )
    newText: str = Field(..., validation_alias=AliasChoices("newText", "new_text"))
    editedAt: Optional[str] = Field(
        None, validation_alias=AliasChoices("editedAt", "edited_at")
    )
    timestamp: Optional[str] = None


class ApplicableLawItemInRequest(BaseModel):
    """Law object inside applicable_law_relevance_request (Server → AI)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[str] = None
    act: str = Field(..., validation_alias=AliasChoices("act", "Act"))
    section: str = Field(..., validation_alias=AliasChoices("section", "Section"))
    title: Optional[str] = Field(None, validation_alias=AliasChoices("title", "Title"))
    description: Optional[str] = Field(
        None, validation_alias=AliasChoices("description", "Description")
    )
    userDescription: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("userDescription", "user_description"),
    )


class ApplicableLawRelevanceRequestPayload(BaseModel):
    """
    Payload for applicable_law_relevance_request (Server → AI).
    Lawyer-selected law; AI returns applicability via applicable_law_relevance_response.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    requestId: Optional[str] = Field(
        None, validation_alias=AliasChoices("requestId", "request_id")
    )
    timestamp: Optional[str] = None
    law: ApplicableLawItemInRequest


class AnalysisDocxGenerateRequestPayload(BaseModel):
    """
    Payload for analysis_docx_generate_request (Server -> AI).
    AI performs text normalization and returns analysis_docx_generate_response.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requestId: str
    workspaceId: str
    userId: Optional[str] = None
    trigger: Optional[str] = None
    contentHash: Optional[str] = None
    documentSource: Dict[str, Any]
    timestamp: Optional[str] = None


class ConversationHistoryItem(BaseModel):
    """Schema for conversation history item."""

    role: Literal["USER", "AI"]
    content: str


class ChatAttachmentPayload(BaseModel):
    """Schema for document attached to a chat message (intelligence chat upload)."""

    url: str
    fileName: Optional[str] = None
    mimeType: Optional[str] = None

    class Config:
        populate_by_name = True
        extra = "ignore"


class ChatAskedBy(BaseModel):
    """
    Who typed a chat message, when that is not the case owner.

    Present on ``user_message_request`` ONLY for a shared workspace where a
    collaborator asked the question. The payload's ``userId`` is unchanged (it
    remains the OWNER), so this is purely additive: it drives attribution in
    the reply and the read-only gate for ``view`` collaborators.
    """

    userId: str
    name: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class UserMessageRequestPayload(BaseModel):
    """
    Schema for user_message_request event (Server → AI_Maverick).
    User message forwarded to AI service in ai_service room.

    Note: Backend sends 'content' field, not 'message'.
    When user uploads a document in chat, attachment has url, fileName, mimeType.

    For DRAFTING_ASSISTANT chats, additional optional fields identify the draft
    session and (on the kick-off turn) the template/suggestion the lawyer picked.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    content: str  # User message content (backend sends 'content', not 'message')
    chat_type: str = Field(validation_alias=AliasChoices("chat_type", "chatType"))
    conversation_history: List[ConversationHistoryItem] = []
    workspaceId: Optional[str] = None
    userId: Optional[str] = None
    chatId: Optional[str] = None
    timestamp: str  # ISO timestamp
    attachment: Optional[ChatAttachmentPayload] = (
        None  # Document uploaded with this message (e.g. ANALYSIS_REPORT chat)
    )
    askedBy: Optional[ChatAskedBy] = Field(
        default=None,
        validation_alias=AliasChoices("askedBy", "asked_by"),
    )  # Set only when a shared-workspace collaborator (not the owner) asked.

    # Drafting Assistant fields. Optional; present on the kick-off turn when
    # the lawyer starts a draft from a template / suggestion / blank.
    actionableId: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("actionableId", "actionable_id"),
    )  # One per draft session; AI caches state keyed by this.
    source: Optional[str] = None  # "template" | "suggestedDraft" | "blank"
    suggestedDraftId: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("suggestedDraftId", "suggested_draft_id"),
    )
    suggestionTitle: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("suggestionTitle", "suggestion_title"),
    )  # Echo of the clicked suggestion's title — drives template matching.
    suggestionDescription: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "suggestionDescription", "suggestion_description"
        ),
    )  # Echo of the clicked suggestion's description — extra signal for matching.
    templateId: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateId", "template_id"),
    )
    templateUrl: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateUrl", "template_url"),
    )  # Lazy-fetch fallback when AI's template cache is cold.
    templateOriginalName: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateOriginalName", "template_original_name"),
    )
    templateMimeType: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateMimeType", "template_mime_type"),
    )
    # Global Template Library (SIM-173): "global" for a global template (absent
    # on drafts from a lawyer's own template), and the version the draft
    # started from. A draft started on v3 keeps getting v3.
    templateScope: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateScope", "template_scope"),
    )
    templateVersion: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("templateVersion", "template_version"),
    )
    draftMode: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("draftMode", "draft_mode"),
    )
    # Optional layout hint sent by the frontend (reserved). Parsed and threaded
    # through to the drafting agent for future layout-aware rendering; not yet
    # consumed. Shape is frontend-defined — kept opaque here.
    formatLayout: Optional[Any] = Field(
        default=None,
        validation_alias=AliasChoices("formatLayout", "format_layout"),
    )


# ============================================================================
# Drafting Assistant — Global Template Library (Server → AI)
# ============================================================================


class TemplateBlueprintVariable(BaseModel):
    """One interactive-variable inside a template's drafting blueprint.

    May arrive fully specified (AI-proposed or echoed back after approval) or
    as a near-empty row the lawyer typed in the dashboard (only `label` set) —
    the drafting agent enriches name-only rows on the AI side.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: Optional[str] = None
    label: Optional[str] = None
    question: Optional[str] = None
    kind: Optional[str] = None
    fillFromCase: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("fillFromCase", "fill_from_case"),
    )
    required: Optional[bool] = None


class TemplateBlueprint(BaseModel):
    """The lawyer-approved (or AI-proposed) drafting blueprint for a template.

    Mirrors the dashboard's *AI Drafting Blueprint & Variables Approval*
    screen: persona/filing context, tone, structure style, draft depth, the
    legal-guardrails level, and the interactive-variable registry.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    context: Optional[str] = None
    tone: Optional[str] = None
    structure: Optional[str] = None
    depth: Optional[str] = None
    legalGuardrails: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("legalGuardrails", "legal_guardrails"),
    )
    variables: List[TemplateBlueprintVariable] = []

    @field_validator("variables", mode="before")
    @classmethod
    def _coerce_string_variables(cls, v: Any) -> Any:
        """Accept variables sent as plain strings (old backend format).

        Converts each bare string into {'label': <string>} so downstream
        code always gets TemplateBlueprintVariable objects regardless of
        which backend version emitted the payload."""
        if not isinstance(v, list):
            return v
        coerced = []
        for item in v:
            if isinstance(item, str):
                coerced.append({"label": item.strip()})
            else:
                coerced.append(item)
        return coerced


class TemplateFormatting(BaseModel):
    """The `formatting` blob attached to a global template item.

    `page`, `letterhead`, and `court` are free-form layout blobs used at DOCX
    export (kept as opaque dicts). `blueprint` is the drafting config the AI
    consumes for kickoff + render.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    page: Optional[Dict[str, Any]] = None
    letterhead: Optional[Dict[str, Any]] = None
    court: Optional[Dict[str, Any]] = None
    blueprint: Optional[TemplateBlueprint] = None


class GlobalTemplateItem(BaseModel):
    """
    One Global Template Library entry (SIM-58 / SIM-173), as carried by
    ``global_template_sync`` and ``global_template_sync_response``.

    A ``retired`` item may carry only id, version, name, category and kind.
    Presets (``kind="preset"``) have no file fields.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    version: int
    name: Optional[str] = None
    originalName: Optional[str] = None  # file name with its extension
    category: Optional[str] = None
    kind: Optional[str] = None  # "file" | "preset"
    extension: Optional[str] = None
    mimeType: Optional[str] = None
    size: Optional[int] = None
    fileHash: Optional[str] = None
    fileUrl: Optional[str] = None  # signed, ~1 hour; cache content, never the URL
    formatting: Optional[TemplateFormatting] = None
    draftCategory: Optional[str] = None


class GlobalTemplateSyncPayload(BaseModel):
    """
    Schema for the global_template_sync event (Server → AI_Maverick).

    Sent once for the whole platform (no ``userId``) when an admin publishes,
    retires or deletes a global template.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action: Literal["published", "retired", "deleted"]
    templates: List[GlobalTemplateItem] = []
    timestamp: Optional[str] = None


class GlobalTemplateSyncResponsePayload(BaseModel):
    """
    Schema for the global_template_sync_response event (Server → AI_Maverick).

    The reply to ``request_global_template_sync``: every published global
    template. A full replace, never a delta; ``templates=[]`` means nothing is
    published yet. ``requestId`` is always echoed, even on an empty list.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requestId: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("requestId", "request_id"),
    )
    count: Optional[int] = None  # === len(templates)
    templates: List[GlobalTemplateItem] = []
    timestamp: Optional[str] = None


# ============================================================================
# AI Response Schemas (AI → Server)
# ============================================================================


class AIResponseMetadata(BaseModel):
    """
    Metadata for AI response.
    Per backend documentation for ai_response event.
    """

    messageId: Optional[str] = None  # Optional message identifier
    aiModel: Optional[str] = None  # AI model name (e.g., "gemini-pro")
    tokensUsed: Optional[int] = None  # Total tokens for THIS response (input+output)
    responseTime: Optional[float] = None  # Response time in milliseconds
    error: Optional[str] = None  # Error message if any

    # --- Per-response token usage & cost (this response only) ---
    # Thinking tokens are folded into outputTokens (Gemini bills them as output).
    # Prices are USD, computed from the active model's rate (app/ai/pricing.py).
    inputTokens: Optional[int] = None
    outputTokens: Optional[int] = None
    totalTokens: Optional[int] = None
    inputPrice: Optional[float] = None
    outputPrice: Optional[float] = None
    # FULL cost of this response: inputPrice + outputPrice + servicePrice.
    price: Optional[float] = None

    # --- Per-response paid NON-LLM usage & cost (this response only) ---
    # The pipeline spends money outside Gemini generation. Each source reports its
    # billable quantity and its USD cost; ``servicePrice`` is their sum and is
    # already included in ``price`` above. Rates: app/ai/pricing.py (env-overridable).
    # Free tiers are monthly + account-wide so they are NOT modelled — these are
    # upper bounds. ``embedTokens`` is an estimate (~4 chars/token).
    ocrUnits: Optional[int] = None  # Vision units: 1 per image, 1 per OCR'd PDF page
    ocrPrice: Optional[float] = None
    sttSeconds: Optional[float] = None  # Speech-to-Text billed audio seconds
    sttPrice: Optional[float] = None
    searchQueries: Optional[int] = None  # Vertex AI Search queries
    searchPrice: Optional[float] = None
    groundingRequests: Optional[int] = None  # Google Search grounding requests
    groundingPrice: Optional[float] = None
    embedTokens: Optional[int] = None  # embedding input tokens (estimated)
    embedPrice: Optional[float] = None
    servicePrice: Optional[float] = None  # sum of the five prices above

    # --- Cumulative per-workspace totals (incremented on every response) ---
    totalInputTokens: Optional[int] = None
    totalOutputTokens: Optional[int] = None
    totalInputPrice: Optional[float] = None
    totalOutputPrice: Optional[float] = None
    # Running total cost for the workspace: LLM tokens AND paid non-LLM APIs
    # (== totalInputPrice + totalOutputPrice + totalServicePrice).
    totalPrice: Optional[float] = None

    # --- Cumulative per-workspace paid NON-LLM totals ---
    totalOcrUnits: Optional[int] = None
    totalOcrPrice: Optional[float] = None
    totalSttSeconds: Optional[float] = None
    totalSttPrice: Optional[float] = None
    totalSearchQueries: Optional[int] = None
    totalSearchPrice: Optional[float] = None
    totalGroundingRequests: Optional[int] = None
    totalGroundingPrice: Optional[float] = None
    totalEmbedTokens: Optional[int] = None
    totalEmbedPrice: Optional[float] = None
    totalServicePrice: Optional[float] = None  # included in totalPrice above

    # Additional fields for internal use (not in backend spec)
    confidence: Optional[str] = None
    detectedIntent: Optional[str] = None

    class Config:
        populate_by_name = True


class DraftOpenQuestion(BaseModel):
    """One open question the drafting agent still needs answered."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    label: str


class DraftDocumentPayload(BaseModel):
    """
    Final (or revised) draft delivered by the drafting agent.
    `content` is the full draft as a Markdown string — frontend renders it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str
    format: Literal["markdown"] = "markdown"
    content: str
    version: int = 1
    sourceTemplateId: Optional[str] = None
    sourceTemplateName: Optional[str] = None
    sourceType: Optional[str] = None  # "template" | "suggestedDraft" | "blank"
    wordCount: Optional[int] = None
    # Public HTTPS URL to the generated .docx blob uploaded to GCS.
    # Absent when DOCX generation or upload failed — markdown still ships.
    downloadUrl: Optional[str] = None


class Citation(BaseModel):
    """
    One source behind a chat answer, matching an inline ``[index]`` marker.

    ``type`` is ``document`` (an uploaded annexure: ``documentId``,
    ``annexure``, ``fileName``, ``page``/``pageEnd``, ``timestamp`` for audio,
    and a verbatim ``quote``), ``case_law`` or ``statute`` (``title`` and
    ``url``). Every citation has been checked against the evidence that was
    actually in the prompt — see app/rag/citations.py.

    Spreadsheet sources add ``sheet`` and ``rowStart``/``rowEnd`` (the exact
    row of the quote). ``computed: true`` marks a figure the system calculated
    over every row of that sheet (app/rag/table_query.py); its ``quote`` states
    the calculation. All four are optional and additive.
    """

    index: int
    type: Literal["document", "case_law", "statute", "web"]
    documentId: Optional[str] = None
    annexure: Optional[int] = None
    fileName: Optional[str] = None
    page: Optional[int] = None
    pageEnd: Optional[int] = None
    timestamp: Optional[str] = None
    heading: Optional[str] = None
    quote: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    sheet: Optional[str] = None
    rowStart: Optional[int] = None
    rowEnd: Optional[int] = None
    computed: Optional[bool] = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class AIResponsePayload(BaseModel):
    """
    Schema for ai_response event (AI_Maverick → Server).
    AI service sends response back to server.
    suggestionChips: optional follow-up prompts (clickable chips) - when user clicks, that text is sent as next message.

    For DRAFTING_ASSISTANT chats, additional fields carry draft-flow state:
      - draftStatus: analyzing | gathering_info | ready | revising
      - openQuestions: slots the AI still needs the lawyer to fill
      - draftDocument: final Markdown draft (populated only when status="ready")
      - actionableId: echoes the draft session key from the request
    """

    chatId: str
    workspaceId: str
    userId: str
    content: str
    metadata: Optional[AIResponseMetadata] = None
    suggestionChips: Optional[List[str]] = (
        None  # 3-4 contextual, role-based prompts for next turn
    )
    # Sources behind the answer, one per inline [n] marker in `content`.
    # Additive: absent/empty when the answer cites nothing.
    citations: Optional[List[Citation]] = None

    # Drafting Assistant fields (omitted for other chat types).
    actionableId: Optional[str] = None
    chat_type: Optional[str] = None
    isDraft: Optional[bool] = (
        None  # True only on the turn that just delivered a draft body inline.
    )
    draftStatus: Optional[
        Literal["analyzing", "gathering_info", "ready", "revising"]
    ] = None
    openQuestions: Optional[List[DraftOpenQuestion]] = None
    draftDocument: Optional[DraftDocumentPayload] = None

    class Config:
        populate_by_name = True


# ============================================================================
# Drafting Assistant — suggestions payload (AI → Server via document_feature_response)
# ============================================================================


class MatchItem(BaseModel):
    """
    One library draft matched to a suggested draft.

    A suggestion can match MULTIPLE library drafts of the same document type
    (e.g. two uploaded Legal Notices). Each match carries its own
    ``matchPercent`` so the frontend can render a "N DRAFTS FOUND" badge and a
    selection modal listing every matching draft with a "% Match" badge, and
    pass the chosen ``templateId`` straight through on click.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    templateId: str
    templateName: Optional[str] = None
    category: Optional[str] = None  # library category, e.g. "Document Draft"
    matchPercent: Optional[int] = None  # 0-100, this draft's fit to the case


class DraftingSuggestionItem(BaseModel):
    """
    One suggested draft in the proactive Drafting Assistant panel.

    Emitted inside document_feature_response payload.suggestedDrafts[].

    ``matches`` lists every library draft that matches this suggestion (same
    document type), sorted by ``matchPercent`` desc. It is ``[]`` for AI-only
    suggestions with no library match. The frontend renders a "DRAFT AVAILABLE"
    badge for a single match and "N DRAFTS FOUND" + a pick-one modal when
    len > 1, and passes the chosen ``templateId`` straight through on click.

    Note: the drafting agent works with a richer internal card shape
    (``matchedDrafts`` / ``matchScore`` plus mirrored top-level template
    fields); it is serialized to this wire shape at the emit boundary via
    ``event_utils.drafting_payload_to_wire``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str
    relevance: Literal["High", "Medium", "Low"] = "Medium"
    description: Optional[str] = None  # one-line summary, <= 160 chars
    matches: List["MatchItem"] = []


class DraftingSuggestionsPayload(BaseModel):
    """Inner `payload` shape for featureType=drafting_assistant."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    suggestedDrafts: List[DraftingSuggestionItem] = []


class CaseLawItem(BaseModel):
    """
    One relevant case-law precedent in the Case Laws panel.

    Emitted inside document_feature_response payload.caseLaws[] under
    featureType="case_laws". Every item is source-backed: ``sourceUrl`` always
    points to the real judgment/source the citation was drawn from (Indian
    Kanoon, Vertex AI Search, or a Google-Search-grounded web result). Items
    without a verifiable source are dropped before emit, so ``verified`` is
    True for everything that reaches the client.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # --- Identity (from the judgment / source) ---
    id: str
    title: str  # e.g. "Arnesh Kumar v. State of Bihar"
    citation: Optional[str] = None  # primary citation, e.g. "(2014) 8 SCC 273"
    equivalentCitation: Optional[str] = None  # e.g. "AIR 2014 SC 2756"
    court: Optional[str] = None  # e.g. "Supreme Court of India"
    bench: List[str] = []  # judges, when stated in the judgment
    date: Optional[str] = None  # decision date (ISO or "18 July 2006")

    # --- Precedential weight (derived deterministically from the court) ---
    bindingStrength: Optional[str] = (
        None  # "Binding" | "Highly Persuasive" | "Persuasive"
    )
    bindingStars: Optional[int] = None  # 1-5, for a quick visual weight badge

    # --- Substance of the judgment (extracted from the real judgment text) ---
    areaOfLaw: List[str] = []  # e.g. ["Consumer Protection", "Warranty"]
    legalIssues: List[str] = []  # the questions the judgment answered
    materialFacts: List[str] = []  # 3-5 legally relevant facts
    ratio: Optional[str] = None  # ratio decidendi — the binding legal principle
    holding: Optional[str] = None  # the outcome / disposition
    relief: List[str] = []  # e.g. ["Refund", "Replacement", "Compensation"]

    # --- AI value-add: how this precedent maps to the user's matter ---
    summary: str  # one-line holding + relevance (kept for back-compat)
    relevance: Literal["High", "Medium", "Low"] = "Medium"
    matchedFacts: List[str] = []  # facts shared with the user's case
    applicabilityReason: Optional[str] = None  # why it applies to THIS matter
    distinguishing: List[str] = []  # ways the precedent can be distinguished
    adverseImpact: Optional[str] = None  # how it may weaken the user's position
    confidence: Optional[int] = None  # 0-100, AI confidence in the match

    # --- Provenance & status ---
    sourceUrl: str  # real URL the case was retrieved from / verified against
    sourceName: Optional[str] = None  # "Indian Kanoon" | "Web (Google Search)" | ...
    # Backend/FE contract fields for the clickable source link; mirror
    # sourceUrl/sourceName. Kept alongside them for back-compat.
    referenceUrl: Optional[str] = None  # e.g. "https://indiankanoon.org/doc/123456/"
    referenceName: Optional[str] = None  # e.g. "Full judgment (Indian Kanoon)"
    # Good-law status is NOT asserted unless there is real evidence; default is an
    # honest "not verified" so a lawyer confirms on SCC Online / Manupatra.
    legalStatus: str = "Not independently verified"
    verified: bool = True


class CaseLawsPayload(BaseModel):
    """Inner `payload` shape for featureType=case_laws."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    caseLaws: List[CaseLawItem] = []


class AddCaseLawRequest(BaseModel):
    """
    Payload for add_case_law event (App → Backend → AI service).

    User manually adds a precedent via the "Add Case Law" button by supplying
    ANY one of: a citation, a source URL, or free-text description. The AI
    resolves/verifies a real source, assesses relevance to the case, and
    appends the case (re-emitting featureType=case_laws). If no real source can
    be resolved, the cached panel is re-emitted unchanged.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    workspaceRole: Optional[str] = Field(
        None, validation_alias=AliasChoices("workspaceRole", "workspace_role")
    )
    citation: Optional[str] = None
    url: Optional[str] = None
    text: Optional[str] = None
    timestamp: Optional[str] = None


class CaseLawDocumentRequest(BaseModel):
    """
    Payload for case_law_document_request (Server → AI).

    Emitted to the ai_service room right after a lawyer's file is stored in GCS.
    The AI fetches ``documentUrl``, decides whether the document is a case law,
    and replies with case_law_document_response echoing the same ``requestId``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requestId: Optional[str] = Field(
        None, validation_alias=AliasChoices("requestId", "request_id")
    )
    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    documentUrl: str = Field(
        ..., validation_alias=AliasChoices("documentUrl", "document_url")
    )
    documentName: Optional[str] = Field(
        None, validation_alias=AliasChoices("documentName", "document_name")
    )
    mimeType: Optional[str] = Field(
        None, validation_alias=AliasChoices("mimeType", "mime_type")
    )
    workspaceRole: Optional[str] = Field(
        None, validation_alias=AliasChoices("workspaceRole", "workspace_role")
    )
    timestamp: Optional[str] = None


class UploadedCaseLaw(BaseModel):
    """Minimal case-law item returned in case_law_document_response.caseLaw."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    citation: Optional[str] = None
    court: Optional[str] = None
    summary: Optional[str] = None
    relevance: Literal["High", "Medium", "Low"] = "Medium"
    referenceUrl: Optional[str] = None  # e.g. "https://indiankanoon.org/doc/123456/"
    referenceName: Optional[str] = None  # e.g. "Full judgment (Indian Kanoon)"


class CaseLawDocumentResponse(BaseModel):
    """
    Payload for case_law_document_response (AI → Server).

    Echoes ``requestId``/``workspaceId``/``userId``. On acceptance ``caseLaw`` is
    populated; on rejection ``rejectionReason`` explains why the uploaded
    document is not a case law.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requestId: Optional[str] = None
    workspaceId: str
    userId: str
    accepted: bool
    caseLaw: Optional[UploadedCaseLaw] = None
    rejectionReason: Optional[str] = None


# Normalized party-role vocabulary the frontend colour-codes into badges.
# Anything outside this set is mapped to "Other" by ``_normalize_party_role``.
PartyRole = Literal[
    "Petitioner",
    "Respondent",
    "Witness",
    "Victim",
    "Accused",
    "Other",
]


class RelevantPartyItem(BaseModel):
    """
    One party in the Relevant Parties panel.

    Emitted inside document_feature_response payload.parties[] under
    featureType="relevant_parties". Names are taken verbatim from the uploaded
    documents (never invented); ``role`` is a normalized enum the frontend
    colour-codes. Contact fields are PII: they are emitted empty on
    document-driven auto-generation and only populated when the user supplies
    them via the add/edit_relevant_party panel-edit events.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str  # exact name/title as written, e.g. "Rajesh Kumar", "President, BCCI"
    role: PartyRole = "Other"
    details: str  # one neutral sentence on this party's role in the matter
    contactNumber: str = ""  # user-supplied (panel edit); "" on auto-gen
    email: str = ""  # user-supplied (panel edit); "" on auto-gen
    officialIdUrl: str = ""  # user-supplied (panel edit); "" on auto-gen
    isMatched: bool = False  # True when this party carried over from currentState


class RelevantPartiesPayload(BaseModel):
    """Inner `payload` shape for featureType=relevant_parties."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    parties: List[RelevantPartyItem] = []


class AddRelevantPartyRequest(BaseModel):
    """Payload for add_relevant_party (App → Backend → AI). User adds a party."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    workspaceRole: Optional[str] = Field(
        None, validation_alias=AliasChoices("workspaceRole", "workspace_role")
    )
    name: str
    role: Optional[str] = None  # coerced to PartyRole; AI infers when omitted
    details: Optional[str] = None  # contract field; `description` kept for back-compat
    description: Optional[str] = None
    contactNumber: Optional[str] = None
    email: Optional[str] = None
    officialIdUrl: Optional[str] = None
    timestamp: Optional[str] = None


class EditRelevantPartyRequest(BaseModel):
    """Payload for edit_relevant_party (App → Backend → AI). Edit a party by id."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    partyId: str = Field(
        ..., validation_alias=AliasChoices("partyId", "party_id", "id")
    )
    name: Optional[str] = None
    role: Optional[str] = None
    details: Optional[str] = None  # contract field; `description` kept for back-compat
    description: Optional[str] = None
    contactNumber: Optional[str] = None
    email: Optional[str] = None
    officialIdUrl: Optional[str] = None
    timestamp: Optional[str] = None


class DeleteRelevantPartyRequest(BaseModel):
    """Payload for delete_relevant_party (App → Backend → AI). Delete by id."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    workspaceId: str = Field(
        ..., validation_alias=AliasChoices("workspaceId", "workspace_id")
    )
    userId: str = Field(..., validation_alias=AliasChoices("userId", "user_id"))
    partyId: str = Field(
        ..., validation_alias=AliasChoices("partyId", "party_id", "id")
    )
    timestamp: Optional[str] = None


# ============================================================================
# Strategy & Objective Schemas (AI → Server)
# ============================================================================


class StrategyPayload(BaseModel):
    """
    Schema for a single strategy option.
    """

    strategyId: str
    strategyType: str
    title: str
    description: str
    rationale: str
    preconditions: List[str] = []
    risks: List[str] = []
    downstreamImpact: Dict[str, str] = {}
    relevanceScore: float = 0.0
    compatibleRoles: List[str] = []
    requiresEvidenceTypes: List[str] = []
    estimatedComplexity: str = "MEDIUM"
    status: str = "SUGGESTED"
    createdAt: str  # ISO timestamp

    class Config:
        populate_by_name = True


class StrategiesGeneratedPayload(BaseModel):
    """
    Schema for strategies_generated event (AI_Maverick → Server).
    Emitted after documents are processed and strategies are generated.
    """

    workspaceId: str
    userId: str
    role: str
    isUpdate: bool = False
    strategies: List[StrategyPayload] = []
    strategyCount: int = 0
    basedOnDocuments: int = 0
    averageReadability: float = 0.0
    timestamp: str  # ISO timestamp

    class Config:
        populate_by_name = True


class StrategiesUpdatedPayload(BaseModel):
    """
    Schema for strategies_updated event (AI_Maverick → Server).
    Emitted when new documents are added and strategies are regenerated.
    """

    workspaceId: str
    userId: str
    role: str
    isUpdate: bool = True
    strategies: List[StrategyPayload] = []
    strategyCount: int = 0
    basedOnDocuments: int = 0
    averageReadability: float = 0.0
    timestamp: str  # ISO timestamp

    class Config:
        populate_by_name = True


# ============================================================================
# Legacy Schemas (for backward compatibility)
# ============================================================================


class EvidenceFile(BaseModel):
    """Legacy schema for evidence file data."""

    filename: str
    url: str
    path: str
    uploadedAt: datetime
    id: str = Field(alias="_id")

    class Config:
        populate_by_name = True


class WorkspaceData(BaseModel):
    """Legacy schema for workspace data payload."""

    id: str = Field(alias="_id")
    userId: str
    title: str
    workspaceRole: str
    status: str
    isDeleted: bool = False
    deletedAt: Optional[datetime] = None
    evidenceFiles: List[EvidenceFile] = []
    createdAt: datetime
    updatedAt: datetime
    v: int = Field(default=0, alias="__v")

    class Config:
        populate_by_name = True


class SocketResponse(BaseModel):
    """Legacy schema for socket response data."""

    status: bool
    statusCode: int
    message: str
    data: WorkspaceData
    error: List[Any] = []


class WorkspaceUpdateEvent(BaseModel):
    """Schema for workspace update events."""

    workspace_id: str
    event_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict


class ChatMessage(BaseModel):
    """Schema for chat messages."""

    workspace_id: str
    user_id: str
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Optional[dict] = None


class AgentResponse(BaseModel):
    """Schema for agent responses."""

    workspace_id: str
    agent_type: str
    response: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Optional[dict] = None
