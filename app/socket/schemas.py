"""
Pydantic schemas for socket event data validation.
Updated to match backend Socket.IO specification.
"""
from typing import Optional, List, Any, Dict, Literal
from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl


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
    id: Optional[str] = Field(None, alias="_id")  # MongoDB ObjectId (maps to backend's _id)
    isDeleted: Optional[bool] = Field(False, alias="isDeleted")  # True when user deleted this document
    
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
    description: Optional[str] = None  # Optional custom description, e.g. "Use Indian legal terms only"
    
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
    workspaceRole: Optional[str] = None  # PETITIONER | RESPONDENT | MEDIATOR | ADJUDICATOR (from Edit Workspace)
    evidenceFiles: Optional[List[EvidenceFilePayload]] = None
    deletedDocumentIds: Optional[List[str]] = None  # Document IDs removed from Supporting Material
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


class ConversationHistoryItem(BaseModel):
    """Schema for conversation history item."""
    
    role: Literal["USER", "AI"]
    content: str


class UserMessageRequestPayload(BaseModel):
    """
    Schema for user_message_request event (Server → AI_Maverick).
    User message forwarded to AI service in ai_service room.
    
    Note: Backend sends 'content' field, not 'message'.
    """
    
    content: str  # User message content (backend sends 'content', not 'message')
    chat_type: str  # ChatType from backend
    conversation_history: List[ConversationHistoryItem] = []
    workspaceId: Optional[str] = None
    userId: Optional[str] = None
    chatId: Optional[str] = None
    timestamp: str  # ISO timestamp
    
    class Config:
        populate_by_name = True


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


class AIResponsePayload(BaseModel):
    """
    Schema for ai_response event (AI_Maverick → Server).
    AI service sends response back to server.
    suggestionChips: optional follow-up prompts (clickable chips) - when user clicks, that text is sent as next message.
    """
    
    chatId: str
    workspaceId: str
    userId: str
    content: str
    metadata: Optional[AIResponseMetadata] = None
    suggestionChips: Optional[List[str]] = None  # 3-4 contextual, role-based prompts for next turn
    
    class Config:
        populate_by_name = True


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

