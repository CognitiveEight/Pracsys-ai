"""
Pydantic schemas for socket event data validation.
"""
from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl


class EvidenceFile(BaseModel):
    """Schema for evidence file data."""
    
    filename: str
    url: str
    path: str
    uploadedAt: datetime
    id: str = Field(alias="_id")  # Changed from _id to id
    
    class Config:
        populate_by_name = True


class WorkspaceData(BaseModel):
    """Schema for workspace data payload."""
    
    id: str = Field(alias="_id")  # Changed from _id to id
    userId: str
    title: str
    workspaceRole: str
    status: str
    isDeleted: bool = False
    deletedAt: Optional[datetime] = None
    evidenceFiles: List[EvidenceFile] = []
    createdAt: datetime
    updatedAt: datetime
    v: int = Field(default=0, alias="__v")  # Changed from __v to v
    
    class Config:
        populate_by_name = True


class SocketResponse(BaseModel):
    """Schema for socket response data."""
    
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

