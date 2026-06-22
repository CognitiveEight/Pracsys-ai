"""
Pydantic schemas for socket event data validation.
Updated to match backend Socket.IO specification.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl

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


class WorkspaceSyncPayload(BaseModel):
    """
    Schema for workspace_sync event (Server → AI_Maverick).
    Emitted to ai_service room when workspace is updated.
    When user deletes documents from Supporting Material, backend may send
    deletedDocumentIds so AI can remove them from vector store and regenerate strategy.
    workspaceRole is sent when user changes role (e.g. Edit Workspace → Mediator).
    Optional custom instructions: structure, tone, description (same as workspace_created).
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
    title: Optional[str] = Field(
        None, validation_alias=AliasChoices("title", "Title")
    )
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
        validation_alias=AliasChoices("suggestionDescription", "suggestion_description"),
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
        validation_alias=AliasChoices(
            "templateOriginalName", "template_original_name"
        ),
    )
    templateMimeType: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("templateMimeType", "template_mime_type"),
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
    tokensUsed: Optional[int] = None  # Number of tokens used
    responseTime: Optional[float] = None  # Response time in milliseconds
    error: Optional[str] = None  # Error message if any

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
    isDraft: Optional[bool] = None  # True only on the turn that just delivered a draft body inline.
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


class DraftingSuggestionItem(BaseModel):
    """
    One suggested draft in the proactive Drafting Assistant panel.

    Emitted inside document_feature_response payload.suggestedDrafts[].

    Template pre-match fields (``templateId`` / ``templateName`` /
    ``matchMode``) are populated by ``DraftingAgent.suggest()`` when a
    template in the lawyer's library matches the suggestion. When set,
    the frontend can render a "Uses <template name>" badge and pass
    ``templateId`` straight through on click so the kickoff handler
    skips a second matcher round-trip. When unset (None), the click
    flow falls back to a blank-source kickoff and the AI infers a
    standard Indian-advocate skeleton from the case context alone.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str
    relevance: Literal["High", "Medium", "Low"] = "Medium"
    description: Optional[str] = None  # one-line summary, <= 160 chars
    templateId: Optional[str] = None
    templateName: Optional[str] = None
    # Debug / UX hint — "type-match", "anchor-match", "tiebreak" when
    # a template was matched; "no-match" when the library had no
    # suitable template; None when matching was not attempted (e.g.
    # legacy callers or empty library).
    matchMode: Optional[str] = None


class DraftingSuggestionsPayload(BaseModel):
    """Inner `payload` shape for featureType=drafting_assistant."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    suggestedDrafts: List[DraftingSuggestionItem] = []


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
