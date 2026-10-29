"""
Application configuration management.
"""

from typing import List, Optional

from pydantic import SecretStr
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
    # Include local variable values in tracebacks. Off by default: in this
    # service those values are socket payloads, document text and credentials.
    log_diagnose: bool = False

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

    # Handshake signature scheme (see app/socket/auth.py).
    #   "v1"     -> replay-resistant: HMAC over "v1:AI_Maverick:<ts>:<nonce>",
    #               sent as x-signature / x-signature-timestamp / x-signature-nonce.
    #   "legacy" -> pre-migration constant signature. Replayable forever; only for
    #               rollback while the backend's AI_SIGNATURE_ALLOW_LEGACY window is open.
    socket_auth_scheme: str = "v1"

    # Engine.IO transports for the handshake, comma-separated (e.g. "websocket" or
    # "polling,websocket"). Empty/unset keeps the python-socketio default
    # (polling first, then upgrade). Pin to "websocket" if the backend verifies the
    # nonce on the upgrade request as well as the initial handshake.
    socket_transports: Optional[str] = None

    # API Keys - Google Services
    google_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.8-flash"
    # Gemini 3.8 Flash accepts thinking levels low / medium / high only — a
    # MINIMAL request is a hard API error. None = infer from the model name;
    # set explicitly to override for a model not covered by the inference.
    gemini_minimal_thinking_supported: Optional[bool] = None

    # Embedding model for the vector store. Queries and documents are embedded
    # with different task types (RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT), and the
    # dimensionality is requested explicitly rather than truncated after the
    # fact. Changing either value means a new vector space: re-index before
    # serving from it.
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    # Attempts per embedding call before the chunk is stored as
    # embedding_failed (kept for full-context reads, excluded from search).
    embedding_max_attempts: int = 3

    # ── Model portability (app/llm/) ──────────────────────────────────────
    # Every model call goes gemini_client -> app.llm router -> a provider
    # adapter. A "profile" is "<provider>:<model>" or, for the providers that
    # hold several named accounts, "<provider>:<account>:<model>":
    #   gemini:<model>                     Google AI Studio (GOOGLE_API_KEY)
    #   openai:<model>                     api.openai.com (OPENAI_API_KEY)
    #   azure_openai:<model>               Azure OpenAI; <model> is looked up in
    #                                      AZURE_OPENAI_DEPLOYMENTS, else used as
    #                                      the deployment name
    #   azure_foundry:<model>              Azure AI Foundry (OpenAI-compatible)
    #   bedrock:<model-id>                 AWS Bedrock Converse API
    #   sagemaker:<endpoint-name>          SageMaker endpoint (SAGEMAKER_ENDPOINTS)
    #   openai_compat:<account>:<model>    any OpenAI-compatible server — vLLM,
    #                                      Ollama, TGI, DeepSeek, Qwen/DashScope,
    #                                      Together, Groq (OPENAI_COMPAT_PROFILES)
    # Unset LLM_DEFAULT means "gemini:<GEMINI_MODEL>" — today's behaviour.
    llm_default: Optional[str] = None
    # JSON {task: [profile, fallback, ...]}; task = the @meter_usage feature
    # label (see docs/MODEL_PORTABILITY.md). Unlisted tasks use the default.
    llm_routes: Optional[str] = None
    # JSON [profile, ...] tried after a task's own chain on error/timeout.
    llm_fallbacks: Optional[str] = None
    # Models the user may choose per case (the `aiModel` field on workspace /
    # chat events). JSON {"<id>": "<profile>"} — the id is what the frontend
    # sends, e.g. {"gemini-3.8-flash": "gate0:gemini-3.8-flash",
    # "gpt-4o": "gate0:gpt-4o"} — or a JSON list of profiles (id = model part).
    # The LLM_DEFAULT model is always selectable. An unknown id is ignored and
    # the case keeps its model. Tasks listed in LLM_ROUTES are pinned to their
    # route and ignore the selection; grounding and embeddings never follow it.
    llm_selectable_models: Optional[str] = None
    # Profiles allowed to serve Google-Search-grounded calls (only Gemini can).
    llm_grounding_profiles: Optional[str] = None
    # Extra attempts on the SAME profile for a transient error (429/5xx/network)
    # before moving down the chain. Timeouts are never retried.
    llm_transient_retries: int = 1
    # JSON {model: [input_usd_per_million, output_usd_per_million]}.
    llm_pricing_overrides: Optional[str] = None
    # JSON {model: {"max_output_tokens": 8192, "supports_images": false,
    #   "json_mode": true}} — per-model limits the adapters cannot know (output
    # ceilings differ widely across Bedrock / open-weight models, and a request
    # above the ceiling is a hard 400). Applies to every provider.
    llm_model_options: Optional[str] = None

    # OpenAI (api.openai.com, or OPENAI_BASE_URL for a gateway)
    openai_api_key: Optional[SecretStr] = None
    openai_base_url: Optional[str] = None
    openai_organization: Optional[str] = None
    # Azure OpenAI. With no API key, Entra ID (DefaultAzureCredential) is used.
    azure_openai_endpoint: Optional[str] = None
    azure_openai_api_key: Optional[SecretStr] = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployments: Optional[str] = None  # JSON {model: deployment}
    # Azure AI Foundry, OpenAI-compatible endpoint (…/openai/v1). With no API
    # key, Entra ID is used.
    azure_foundry_endpoint: Optional[str] = None
    azure_foundry_api_key: Optional[SecretStr] = None
    # AWS. Credentials come ONLY from the standard boto3 chain (env, profile,
    # instance/task role) — never from this file.
    aws_region: Optional[str] = None
    bedrock_region: Optional[str] = None
    bedrock_guardrail_id: Optional[str] = None
    bedrock_guardrail_version: Optional[str] = None
    # JSON {name: {"endpoint": "...", "payload_format": "openai_messages"|"tgi_generate",
    #              "region": "...", "max_output_tokens": 4096}}
    sagemaker_endpoints: Optional[str] = None
    # JSON {account: {"base_url": "...", "api_key_env": "DEEPSEEK_API_KEY",
    #   "json_mode": true, "supports_images": false, "max_output_tokens": 8192,
    #   "query_prefix": "..."}}
    openai_compat_profiles: Optional[str] = None
    # Gate0 — ViitorCloud's governed AI gateway (LiteLLM proxy). Profiles are
    # "gate0:<model>" where <model> is whatever name gate0 serves (a model
    # group such as "gemini-3.8-flash", or a task class such as "gate0/lite").
    gate0_base_url: Optional[str] = None  # e.g. https://gate0.example.internal/v1
    gate0_api_key: Optional[SecretStr] = None  # a gate0 virtual key for this service
    # Governance mode: every model call (generation, grounding, embeddings) MUST
    # go through gate0. Startup refuses any direct-provider profile, and the
    # router never calls one, so nothing can bypass the gateway's keys,
    # budgets and audit trail.
    gate0_enforce: bool = False
    # Tag every request with app / feature / workspace / request ids (sent as
    # LiteLLM request metadata + tags) for per-workspace spend in gate0.
    gate0_tag_requests: bool = True
    gate0_app_name: str = "simplertoday"
    # Send Gemini safety_settings BLOCK_NONE for Gemini-backed gate0 models
    # (legal evidence trips the default filters; same policy as direct Gemini).
    gate0_gemini_block_none: bool = True
    # Gate0 usage meter: report the non-model paid units (Vision OCR pages,
    # Speech-to-Text seconds, Vertex AI Search queries, Google Search grounding
    # requests gate0 did not price) to gate0, so gate0 holds the whole bill of
    # every case. Needs gate0's usage-record endpoint (usage-record schema
    # 1.1.0); off until that is deployed. Records carry ids and quantities only.
    gate0_meter_enabled: bool = False
    gate0_meter_path: str = "/usage-records"  # relative to GATE0_BASE_URL
    gate0_meter_flush_seconds: float = 10.0
    # Records gate0 could not take are kept here and re-sent on the next start
    # (gate0 dedupes by call id). Billing records only — no case content.
    gate0_meter_outbox_dir: str = "data/gate0_meter_outbox"
    # Embeddings: EMBEDDING_MODEL / EMBEDDING_DIMENSIONS above name the model.
    # gemini | openai | azure_openai | azure_foundry | bedrock | openai_compat:<account>
    embedding_provider: str = "gemini"
    # Prepended to queries for embedding models without task types
    # (e.g. Qwen3-Embedding expects an instruction prefix on queries).
    embedding_query_prefix: str = ""

    # Hard ceiling on a single Gemini generation. Without it a stalled request
    # blocks its caller forever: the long feature generations are awaited inline
    # by socket handlers, and a strategy/objective regeneration legitimately runs
    # ~2 minutes, so the bound must sit comfortably above that while still being
    # finite. On expiry the call raises asyncio.TimeoutError, which the feature
    # handlers turn into a reported failure instead of a permanent loader.
    gemini_request_timeout_seconds: float = 300.0

    # Stuck-loader bounds (SIM-163 Phase -1). A panel loader clears only when its
    # panel arrives, so nothing that holds the per-workspace pipeline lock may
    # wait without bound: while it is held, every later upload, objective change,
    # reconnect replay and resume for the case queues behind it.
    # Bound on any request_global_template_sync wait. An undelivered request
    # returns at once; a late snapshot still refreshes the Drafting Assistant.
    template_sync_timeout_seconds: float = 900.0
    # How long drafting waits on the template prewarm while holding the pipeline
    # lock. The prewarm itself keeps waiting (up to the bound above) outside it.
    template_sync_in_lock_wait_seconds: float = 5.0
    # Hard cap on one upload pipeline / dates-laws run / objective-change cascade
    # holding the lock, measured from acquisition. At the cap the run is
    # cancelled, logged CRITICAL with its stage, and its armed panels settle from
    # cache.
    pipeline_run_timeout_seconds: float = 900.0
    # Seconds of cap added per second of audio in the run's batch, and the upper
    # bound of that extension (also used when an audio duration is unknown).
    audio_timeout_factor: float = 1.0
    pipeline_run_timeout_audio_max_seconds: float = 3600.0
    # Informational WARNING when a case's pipeline lock has been held this long.
    pipeline_lock_warn_seconds: float = 300.0
    # Upload pipeline: generate dates, laws, timeline, parties and case laws
    # while strategy -> analysis -> drafting run, holding their emits until
    # those three are sent. false restores the sequential run.
    pipeline_concurrent_panels: bool = True
    # List of Dates: per-document temporal calls and per-date dedupe calls
    # running at once. 1 restores the sequential behaviour.
    list_of_dates_concurrency: int = 4

    # Token pricing overrides (USD per 1,000,000 tokens). When BOTH are set they
    # take precedence over the built-in per-model table in app/ai/pricing.py, so
    # ops can retune costs without a code change. Leave unset to price by model.
    token_price_input_per_million: Optional[float] = None
    token_price_output_per_million: Optional[float] = None

    # Paid non-LLM API rate overrides. Each takes precedence over the matching
    # default in app/ai/pricing.py, so an upstream price change can be corrected
    # without a deploy. Leave unset to use the built-in defaults — and note those
    # defaults must be verified against Google's published pricing pages (cited
    # in pricing.py). Free tiers are not modelled; cost is an upper bound.
    ocr_price_per_1k_units: Optional[float] = None  # Vision text_detection
    stt_price_per_minute: Optional[float] = None  # Speech-to-Text V2 / Chirp 3
    search_price_per_1k_queries: Optional[float] = None  # Vertex AI Search
    grounding_price_per_1k_requests: Optional[float] = None  # Google Search grounding
    embed_price_per_million_tokens: Optional[float] = None  # Gemini embeddings

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
    # Evidence chunks (dense + sparse vectors) live in their own collection; the
    # collection above keeps the state/role caches. None → "<name>_evidence_v1".
    qdrant_chunks_collection: Optional[str] = None
    # Spreadsheet rows (no vectors; app/rag/table_query.py computes over them).
    # None → "<name>_tables_v1".
    qdrant_tables_collection: Optional[str] = None
    # Spreadsheet calculations in chat (app/rag/table_query.py). Off → no
    # [T#] results; the prompt then forbids totals over partial rows.
    table_query_enabled: bool = True
    # Characters of document text one panel prompt may carry (~4 chars/token).
    # Documents over their share are excerpted by relevance, not cut from the
    # front (app/rag/packing.py). Raising it improves recall at token cost.
    panel_context_chars: int = 200_000
    # Chat retrieval returns passages (hybrid search + rewrite + rerank) and
    # answers carry checked [n] citations. Off → the previous whole-document
    # context and uncited answers.
    # --- Legal knowledge graph (ontology agentic RAG; app/ontology, app/graph,
    # app/retrieval). Unset NEO4J_URL leaves the graph off: every layer still
    # imports and runs, and retrieval falls back to hybrid search alone.
    neo4j_url: Optional[str] = None
    neo4j_username: str = "neo4j"
    neo4j_password: Optional[str] = None
    neo4j_database: str = "neo4j"
    # "community" or "enterprise" — decides which constraints the generated
    # schema may create.
    neo4j_edition: str = "community"
    # Apply the generated ontology schema on startup (idempotent: IF NOT EXISTS).
    graph_apply_schema_on_startup: bool = True
    # Extract ontology facts into the graph after document ingestion. Default
    # OFF: one LLM call per evidence chunk, opted into deliberately. Runs as a
    # tracked background task, never delaying the panels.
    ontology_ingest_enabled: bool = False
    # Panels using agentic (graph + hybrid) retrieval, comma-separated, e.g.
    # "list_of_dates,relevant_parties". Empty = none. Enable a panel only when
    # scripts/eval_rag.py shows it beats the baseline.
    retrieval_panels: Optional[str] = None
    # "augment" (evidence added alongside the existing context). "replace" is
    # not enabled in this release.
    retrieval_mode: str = "augment"

    @property
    def graph_enabled(self) -> bool:
        """True when Neo4j is configured. The graph is optional."""
        return bool(self.neo4j_url and self.neo4j_password)

    rag_v2_enabled: bool = True
    citations_enabled: bool = True
    # One regeneration when an answer states a figure no cited passage holds.
    citation_repair_enabled: bool = True
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
    # Chirp 3 BatchRecognize rejects files over 20 minutes. Longer recordings
    # are cut at pauses into windows of at most this many seconds (overlap
    # included), transcribed in parallel and stitched back together.
    audio_batch_window_seconds: float = 1140.0
    # Audio shared by neighbouring windows; used to match speakers across them.
    audio_window_overlap_seconds: float = 15.0
    # BatchRecognize jobs in flight at once for one recording.
    audio_batch_concurrency: int = 4
    # Recordings longer than this are rejected before any paid call. 0 = no cap.
    audio_max_duration_minutes: float = 1440.0
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

    # Feature flags
    # Fan a chat fact correction ("the disputed amount is 6 lakh, not 16 lakh")
    # out across every panel that can hold the value, and pin it so a later
    # document-driven regeneration cannot revert it. Set
    # ENABLE_FACT_CORRECTION_FANOUT=false to disable if the classifier's
    # isFactCorrection precision proves too low on real traffic — grep
    # "Panel-edit classifier:" for isFactCorrection=True to measure it.
    enable_fact_correction_fanout: bool = True

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
    def socket_transports_list(self) -> Optional[List[str]]:
        """
        Parsed SOCKET_TRANSPORTS, or None to keep the python-socketio default.

        Returns:
            Ordered transport names (e.g. ``["websocket"]``), or None when unset.
        """
        if not self.socket_transports:
            return None
        transports = [t.strip() for t in self.socket_transports.split(",") if t.strip()]
        return transports or None

    @property
    def redis_url(self) -> str:
        """Get Redis URL."""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


# Global settings instance
settings = Settings()
