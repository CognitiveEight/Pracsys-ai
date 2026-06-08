"""
Application configuration management.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
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
    socket_reconnection_delay: int = (
        2000  # ms before first reconnect (avoid tight loop)
    )
    socket_reconnection_delay_max: int = (
        60000  # ms max delay between reconnects (cap backoff)
    )
    socket_secret_key: Optional[str] = (
        None  # Secret key for HMAC signature (SOCKET_SECRET_KEY env var)
    )
    # When emitting events, if the socket is temporarily disconnected, wait briefly
    # for reconnection instead of skipping payloads.
    socket_emit_wait_timeout_seconds: float = 15.0

    # AI Service Authentication
    # Payload for HMAC signature - JSON string of AI user object
    socket_ai_payload: Optional[str] = (
        None  # AI user payload for signature (from backend)
    )

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
    # Separate service-account for Speech-to-Text V2 (Chirp 3) + audio GCS bucket.
    # The OCR/Vision SA does not have Speech API permissions, so audio uses its own.
    # Falls back to ``google_application_credentials`` when unset.
    speech_google_application_credentials: Optional[str] = None
    gcp_project_id: Optional[str] = None
    # Optional: use an API key for Vision OCR instead of service-account credentials.
    # Environment variable supported: GOOGLE_CLOUD_VISION_API_KEY
    google_cloud_vision_api_key: Optional[str] = None

    # Vertex AI Search (replaces the deprecated Google Custom Search / CSE flow).
    # Used as the Indian Kanoon fallback search source. ``gcp_project_id``
    # above is reused for the project field.
    vertex_search_data_store_id: Optional[str] = None
    vertex_search_location_id: str = "global"  # global | us | eu
    # 0 = unstructured, 1 = structured, 2 = website, 3 = blended
    vertex_search_engine_data_type: int = 2
    vertex_search_max_documents: int = 8

    # Vector Store (Qdrant)
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: Optional[str] = None
    qdrant_collection_name: str = "simplertoday"
    qdrant_timeout_seconds: float = (
        60.0  # Request timeout for Qdrant (delete/index can be slow)
    )

    # Audio transcription (Chirp 3 / Speech-to-Text V2)
    # GCS bucket is required for native Chirp 3 speaker diarization via
    # BatchRecognize. Without it, the pipeline falls back to chunked Recognize
    # (transcription works, but speaker tags may be absent).
    gcs_bucket_name: Optional[str] = None

    # Drafting Assistant — separate bucket for generated draft .docx files
    # and retained template skeletons. Kept distinct from the audio bucket
    # so the two pipelines can have independent retention / IAM policies
    # (drafts are public-read; audio uploads are private + ephemeral).
    gcs_docx_bucket_name: Optional[str] = None
    chirp_region: str = "us"  # Speech-to-Text V2 regional endpoint (us / eu / asia)
    audio_languages: str = (
        "en-IN,hi-IN"  # Comma-separated language codes (Chirp 3 diarization-supported)
    )
    audio_min_speakers: int = 2
    audio_max_speakers: int = 6
    huggingface_token: Optional[str] = None  # Optional, for pyannote fallback
    pyannote_model: str = "pyannote/speaker-diarization-3.1"

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
