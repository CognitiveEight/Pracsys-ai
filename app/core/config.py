"""
Application configuration management.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # Application Configuration
    app_name: str = "simplertoday"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    
    # Socket.IO Configuration
    socket_url: str
    socket_path: str = "/socket.io"
    socket_namespace: str = "/"
    socket_reconnection: bool = True
    socket_reconnection_attempts: int = 0  # 0 means infinite attempts
    socket_reconnection_delay: int = 2000  # ms before first reconnect (avoid tight loop)
    socket_reconnection_delay_max: int = 60000  # ms max delay between reconnects (cap backoff)
    socket_secret_key: Optional[str] = None  # Secret key for HMAC signature (SOCKET_SECRET_KEY env var)
    
    # AI Service Authentication
    # Payload for HMAC signature - JSON string of AI user object
    socket_ai_payload: Optional[str] = None  # AI user payload for signature (from backend)
    
    # Socket.IO Event Names (per backend specification)
    # Events AI listens to: workspace_created, workspace_sync, user_message_request
    # Events AI emits: ai_response
    
    # Signature encoding (per backend spec: urlsafe base64)
    socket_signature_encoding: str = "urlsafe"  # Options: "urlsafe" or "standard"
    
    # API Keys - Google Services
    google_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.5-flash"
    
    # Google Cloud Platform
    google_application_credentials: Optional[str] = None
    gcp_project_id: Optional[str] = None
    google_cse_id: Optional[str] = None
    
    # Vector Store (Qdrant)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: Optional[str] = None
    qdrant_collection_name: str = "simplertoday"
    qdrant_timeout_seconds: float = 60.0  # Request timeout for Qdrant (delete/index can be slow)
    
    # Redis Configuration
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    
    # Security
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    
    # Workspace Configuration
    workspace_data_dir: str = "./data/workspaces"
    max_file_size_mb: int = 100
    
    # Observability
    enable_audit_log: bool = True
    enable_trace: bool = True
    prometheus_port: int = 9090
    
    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.app_env == "development"
    
    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env == "production"
    
    @property
    def redis_url(self) -> str:
        """Get Redis URL."""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


# Global settings instance
settings = Settings()

