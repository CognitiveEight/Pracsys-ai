"""
Complete Socket.IO Event Schemas - Backend Specification
Based on official backend Socket.IO events documentation (2026-01-21)

This module contains schemas for ALL events documented by the backend team.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# ============================================================================
# Enums
# ============================================================================


class ChatType:
    """ChatType enum values from backend."""

    STRATEGY_OBJECTIVE = "STRATEGY_OBJECTIVE"
    ANALYSIS_REPORT = "ANALYSIS_REPORT"
    APPLICABLE_LAWS = "APPLICABLE_LAWS"
    DRAFTING_ASSISTANT = "DRAFTING_ASSISTANT"
    TEMPLATES_CASE_LAWS = "TEMPLATES_CASE_LAWS"


class WorkspaceRole:
    """WorkspaceRole enum values from backend."""

    PETITIONER = "PETITIONER"
    RESPONDENT = "RESPONDENT"
    MEDIATOR = "MEDIATOR"
    ADJUDICATOR = "ADJUDICATOR"


class MessageRole:
    """Message role enum."""

    USER = "USER"
    AI = "AI"


class ClientType:
    """Client type enum."""

    WEB_MAVERICK = "Web_Maverick"
    AI_MAVERICK = "AI_Maverick"


# ============================================================================
# Client → Server Events (Web_Maverick)
# ============================================================================


class JoinWorkspacePayload(BaseModel):
    """Payload for join_workspace event."""

    workspaceId: str  # MongoDB ObjectId


class LeaveWorkspacePayload(BaseModel):
    """Payload for leave_workspace event."""

    workspaceId: str  # MongoDB ObjectId


class SendMessagePayload(BaseModel):
    """Payload for send_message event."""

    content: str  # Message text (required)
    chatType: str  # ChatType enum (required)
    workspaceId: str  # MongoDB ObjectId (required)
    chatId: Optional[str] = (
        None  # MongoDB ObjectId (optional, for continuing existing chat)
    )


class TypingPayload(BaseModel):
    """Payload for typing event."""

    isTyping: bool  # Typing indicator (required)
    chatId: Optional[str] = None  # MongoDB ObjectId (optional)


# ============================================================================
# Server → Client Events (Web_Maverick)
# ============================================================================


class ConnectedPayload(BaseModel):
    """
    Payload for connected event.
    Emitted immediately after successful connection and authentication.
    """

    message: str  # Connection confirmation message
    userId: str  # MongoDB ObjectId
    clientType: str  # Web_Maverick or AI_Maverick


class ErrorPayload(BaseModel):
    """Payload for error event."""

    message: str  # Error message
    timestamp: Optional[str] = None  # ISO date string


class MessageReceivedPayload(BaseModel):
    """Payload for message_received event (acknowledgment)."""

    message: str  # Acknowledgment message
    timestamp: str  # ISO date string


class MessageMetadata(BaseModel):
    """Metadata for message objects."""

    messageId: Optional[str] = None
    aiModel: Optional[str] = None
    tokensUsed: Optional[int] = None
    responseTime: Optional[float] = None
    error: Optional[str] = None


class MessageObject(BaseModel):
    """Message object structure used in user_message and ai_message events."""

    role: str  # USER or AI
    content: str  # Message text
    timestamp: str  # ISO date string
    metadata: Optional[MessageMetadata] = Field(default_factory=dict)


class UserMessagePayload(BaseModel):
    """
    Payload for user_message event.
    User's own message echoed back after being saved to database.
    """

    chatId: str  # MongoDB ObjectId
    message: MessageObject


class AIMessagePayload(BaseModel):
    """
    Payload for ai_message event.
    AI response message emitted to user and workspace room.
    """

    chatId: str  # MongoDB ObjectId
    message: MessageObject


class ChatUpdatedPayload(BaseModel):
    """Payload for chat_updated event."""

    chatId: str  # MongoDB ObjectId
    title: Optional[str] = None  # Chat title
    messageCount: Optional[int] = None  # Total messages in chat


class WorkspaceJoinedPayload(BaseModel):
    """Payload for workspace_joined event."""

    workspaceId: str  # MongoDB ObjectId


class WorkspaceLeftPayload(BaseModel):
    """Payload for workspace_left event."""

    workspaceId: str  # MongoDB ObjectId


class UserTypingPayload(BaseModel):
    """Payload for user_typing event (broadcast to chat room)."""

    userId: str  # MongoDB ObjectId
    isTyping: bool


# ============================================================================
# Server → AI Service Events (room: ai_service)
# ============================================================================


class EvidenceFile(BaseModel):
    """Evidence file structure in workspace events."""

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

    class Config:
        populate_by_name = True


class WorkspaceCreatedPayload(BaseModel):
    """
    Payload for workspace_created event (Server → AI_Maverick).
    Emitted to ai_service room when new workspace is created.
    """

    workspaceId: str  # MongoDB ObjectId
    userId: str  # MongoDB ObjectId
    title: str  # Workspace title
    workspaceRole: str  # PETITIONER | RESPONDENT | MEDIATOR | ADJUDICATOR
    evidenceFiles: List[EvidenceFile] = []
    createdAt: str  # ISO date string


class WorkspaceDeletedNotice(BaseModel):
    """
    Payload for workspace_deleted event (Server → AI_Maverick).

    The backend's erasure cascade does not reach this service, so this event is
    the explicit instruction to erase the AI-held copy of a deleted case.
    ``purgeAfter`` is absolute and authoritative; ``purgeAfterDays`` is
    informational. Re-sends under the same ``requestId`` are expected until
    ``purgeAfter`` passes, because the backend emit is fire-and-forget.

    Handled by ``WorkspacePurgeMixin``; the validated schema used at runtime is
    ``app.socket.schemas.WorkspaceDeletedPayload``.
    """

    event: Optional[str] = None
    workspaceId: str
    userId: str
    deletedAt: str
    purgeAfter: str
    purgeAfterDays: Optional[int] = None
    reason: Optional[str] = None
    requestId: Optional[str] = None


class WorkspaceSyncPayload(BaseModel):
    """
    Payload for workspace_sync event (Server → AI_Maverick).
    Emitted to ai_service room when workspace is updated.
    """

    workspaceId: str  # MongoDB ObjectId
    userId: str  # MongoDB ObjectId
    title: Optional[str] = None  # Workspace title
    status: Optional[str] = None  # ACTIVE | ARCHIVED
    evidenceFiles: Optional[List[EvidenceFile]] = None
    updatedAt: str  # ISO date string


# ============================================================================
# AI Service → Server Events (AI_Maverick)
# ============================================================================


class AIResponsePayload(BaseModel):
    """
    Payload for ai_response event (AI_Maverick → Server).
    AI service sends response to a user's message.
    Only AI_Maverick clients can emit this event.
    """

    chatId: str  # MongoDB ObjectId (required)
    workspaceId: str  # MongoDB ObjectId (required)
    userId: str  # MongoDB ObjectId (required)
    content: str  # AI response text (required)
    metadata: Optional[MessageMetadata] = None  # Optional metadata


# ============================================================================
# Heartbeat (Backend → AI ping, AI → Backend pong)
# ============================================================================

AI_HEARTBEAT_PING = "ai_heartbeat_ping"
AI_HEARTBEAT_PONG = "ai_heartbeat_pong"

# ============================================================================
# Drafting Assistant — Template Library sync (Server → AI)
# ============================================================================

TEMPLATE_SYNC = "template_sync"

# AI → Server: pull a user's full template library on demand (e.g. on
# connect / before generating drafting suggestions, when the in-memory
# store is cold after an AI restart). Server replies with TEMPLATE_SYNC_RESPONSE.
REQUEST_TEMPLATE_SYNC = "request_template_sync"

# Server → AI: full snapshot of the user's active templates, in reply to
# REQUEST_TEMPLATE_SYNC. Same template shape as TEMPLATE_SYNC.
TEMPLATE_SYNC_RESPONSE = "template_sync_response"

# AI → Server: the AI's profiled "drafting blueprint" + interactive-variable
# registry for a freshly-uploaded template, surfaced in the dashboard's
# "AI Drafting Blueprint & Variables Approval" screen. The lawyer reviews /
# edits it and the approved result returns via TEMPLATE_SYNC action
# "formatting_updated".
TEMPLATE_BLUEPRINT_PROPOSAL = "template_blueprint_proposal"

# Server → AI: the dashboard's "Regenerate Extra Info" action — re-profile the
# template and emit a fresh TEMPLATE_BLUEPRINT_PROPOSAL. Payload carries
# {userId, templateId, fileHash, fileUrl, timestamp}.
REQUEST_BLUEPRINT_PROPOSAL = "request_blueprint_proposal"

# ============================================================================
# Knowledge base — the shared reference shelf (Server → AI only)
# ============================================================================

# An admin added a document: fetch, read and index it (same id = replace).
KB_DOCUMENT_ADDED = "kb_document_added"
# An admin deleted a document: erase every copy on receipt.
KB_DOCUMENT_DELETED = "kb_document_deleted"
# The complete shelf, on every connect: a full replace, never a delta.
KB_SYNC = "kb_sync"


class TemplateAction:
    """Action values for the template_sync event."""

    UPLOADED = "uploaded"
    DELETED = "deleted"
    RENAMED = "renamed"
    # New: a court/letterhead preset was created (no file body — carries a
    # `formatting` layout blob), and the lawyer approved/edited a template's
    # AI Blueprint (carries `formatting.blueprint`).
    PRESET_CREATED = "preset_created"
    FORMATTING_UPDATED = "formatting_updated"


class TemplateKind:
    """`kind` values for a template_sync item."""

    FILE = "file"      # a file-backed template (has fileUrl / extracted text)
    PRESET = "preset"  # a layout-only preset (court header / letterhead / page)


class TemplateCategory:
    """Category values for templates in the lawyer's library."""

    LETTERHEAD = "Letterhead"
    FOLDER = "Folder"
    DOCUMENT_DRAFT = "Document Draft"


class AIHeartbeatPingPayload(BaseModel):
    """
    Backend → AI_Maverick. Emitted periodically or from healthCheck().
    """

    requestId: Optional[str] = None
    serverSentAt: Optional[str] = None
    source: Optional[str] = None  # "backend" | "health_check"


class AIHeartbeatPongPayload(BaseModel):
    """AI_Maverick → Backend. Echoes ping correlation fields when present."""

    requestId: Optional[str] = None
    serverSentAt: Optional[str] = None
    aiTimestamp: str
    status: Literal["ok"] = "ok"
    version: str
    uptimeMs: int


# ============================================================================
# Additional Events (Not in official docs but used internally)
# ============================================================================


class UserMessageRequestPayload(BaseModel):
    """
    Internal event for forwarding user messages to AI service.
    This event is used to notify AI service of new user messages.

    Note: Backend sends 'content' field, not 'message'.
    """

    content: str  # User message content
    chat_type: str  # ChatType enum (note: uses snake_case)
    conversation_history: List[dict] = Field(default_factory=list)  # Previous messages
    workspaceId: Optional[str] = None  # MongoDB ObjectId
    userId: Optional[str] = None  # MongoDB ObjectId
    chatId: Optional[str] = None  # MongoDB ObjectId
    timestamp: str  # ISO date string


# ============================================================================
# Helper Functions
# ============================================================================


def validate_chat_type(chat_type: str) -> bool:
    """Validate if chat_type is a valid ChatType enum value."""
    valid_types = [
        ChatType.STRATEGY_OBJECTIVE,
        ChatType.ANALYSIS_REPORT,
        ChatType.APPLICABLE_LAWS,
        ChatType.DRAFTING_ASSISTANT,
        ChatType.TEMPLATES_CASE_LAWS,
    ]
    return chat_type in valid_types


def validate_workspace_role(role: str) -> bool:
    """Validate if role is a valid WorkspaceRole enum value."""
    valid_roles = [
        WorkspaceRole.PETITIONER,
        WorkspaceRole.RESPONDENT,
        WorkspaceRole.MEDIATOR,
        WorkspaceRole.ADJUDICATOR,
    ]
    return role in valid_roles


# ============================================================================
# Documentation References
# ============================================================================

"""
Backend Socket.IO Events Documentation (2026-01-21)

Connection Requirements:
- Web_Maverick: JWT token required
  - auth.token (Socket.IO auth object)
  - Query parameter: token
  - Header: Authorization: Bearer <token>

- AI_Maverick: Replay-resistant signature authentication (scheme v1)
  - Header: x-signature (HMAC-SHA256 over "v1:AI_Maverick:<ts>:<nonce>",
    base64url, unpadded, 43 chars)
  - Header: x-signature-timestamp (UTC Unix seconds, +/-300s window)
  - Header: x-signature-nonce (32 lowercase hex chars, single-use)
  - Fresh headers per connection attempt, reconnects included.
    See app/socket/auth.py and docs/AI_SERVICE_AUTH_MIGRATION.md.

Client Type:
- Must be specified via query parameter or header: x-client-type
- Allowed values: Web_Maverick, AI_Maverick

Socket Path:
- Default: /socket.io

Rooms:
- User personal: user:{userId}
- Workspace: workspace:{workspaceId}
- Chat: chat:{chatId}
- AI service: ai_service

Notes:
1. All MongoDB ObjectIds are 24-character hex strings
2. All dates are ISO 8601 strings (e.g., 2026-01-21T08:35:21.943Z)
3. File URLs are Google Cloud Storage pre-signed URLs with expiration
4. File hashes are SHA-256 hex strings
5. Typing indicators are broadcast to chat rooms, not workspace rooms
6. AI responses are emitted to both user personal room and workspace room (if workspaceId provided)
"""
