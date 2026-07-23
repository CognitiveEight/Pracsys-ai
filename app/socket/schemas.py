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
    HttpUrl,
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

    class Config:
        populate_by_name = True


class TransferredUserInfo(BaseModel):
    """New owner details embedded in workspace_transferred."""

    id: str
    name: Optional[str] = None
    email: Optional[str] = None

    class Config:
        populate_by_name = True


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
# Drafting Assistant — template_sync (Server → AI)
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
    """The `formatting` blob attached to a template_sync item.

    `page`, `letterhead`, and `court` are free-form layout blobs used at DOCX
    export (kept as opaque dicts). `blueprint` is the drafting config the AI
    consumes for kickoff + render.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    page: Optional[Dict[str, Any]] = None
    letterhead: Optional[Dict[str, Any]] = None
    court: Optional[Dict[str, Any]] = None
    blueprint: Optional[TemplateBlueprint] = None


class TemplateSyncItem(BaseModel):
    """
    One template row inside a template_sync payload.

    Fields are populated based on the parent payload's `action`:
      - uploaded: id, name, category, extension, mimeType, size, fileHash,
        fileUrl, path all present; `formatting` optional.
      - renamed: id, name, previousName, category, extension, mimeType,
        fileHash, path present; size / fileUrl omitted (content unchanged).
      - deleted: id, name, category, extension, mimeType, fileHash, path
        present; size / fileUrl omitted.
      - preset_created: kind="preset", no file fields; `formatting` carries
        page / court / letterhead layout.
      - formatting_updated: file unchanged (same fileHash); `formatting`
        carries the approved `blueprint`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str
    previousName: Optional[str] = None
    category: str  # "Letterhead" | "Folder" | "Document Draft" | court types
    extension: str  # pdf | doc | docx | txt | "" (presets)
    mimeType: Optional[str] = None
    size: Optional[int] = None
    fileHash: Optional[str] = None
    fileUrl: Optional[str] = None
    path: Optional[str] = None
    kind: Optional[str] = None  # "file" | "preset"
    formatting: Optional[TemplateFormatting] = None


class TemplateSyncPayload(BaseModel):
    """
    Schema for the template_sync event (Server → AI_Maverick).
    Emitted when the lawyer uploads, renames, deletes, creates a preset, or
    updates a template's formatting/blueprint. Supports bulk operations.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action: Literal[
        "uploaded",
        "deleted",
        "renamed",
        "preset_created",
        "formatting_updated",
    ]
    userId: str = Field(validation_alias=AliasChoices("userId", "user_id"))
    templates: List[TemplateSyncItem] = []
    timestamp: Optional[str] = None


class TemplateSyncResponsePayload(BaseModel):
    """
    Schema for the template_sync_response event (Server → AI_Maverick).

    The server's reply to an AI-initiated ``request_template_sync`` — a full
    snapshot of the user's active (non-deleted) templates. Treated exactly
    like a ``template_sync`` with ``action="uploaded"`` for the whole list,
    so the in-memory template store is (re)populated on demand (e.g. after
    an AI restart, where the backend has not pushed any delta this session).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    userId: str = Field(validation_alias=AliasChoices("userId", "user_id"))
    requestId: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("requestId", "request_id"),
    )  # Echoed from the request so the AI can correlate reply → request.
    count: Optional[int] = None  # === len(templates)
    templates: List[TemplateSyncItem] = []
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
    price: Optional[float] = None  # inputPrice + outputPrice for this response

    # --- Cumulative per-workspace totals (incremented on every response) ---
    totalInputTokens: Optional[int] = None
    totalOutputTokens: Optional[int] = None
    totalInputPrice: Optional[float] = None
    totalOutputPrice: Optional[float] = None
    totalPrice: Optional[float] = None  # running total cost for the workspace

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
