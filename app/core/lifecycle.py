"""
Application lifecycle management.
"""

import asyncio
from typing import Optional

from loguru import logger

from app.utils.redact import safe_url

from app.ai.gemini_client import gemini_client
from app.core.config import settings
from app.processing.document_processor import document_processor
from app.socket.auth import SCHEME_LEGACY, SCHEME_V1
from app.socket.auth import self_check as socket_auth_self_check
from app.socket.client import SocketClient
from app.socket.events import event_handler
from app.utils.exceptions import ConfigurationError
from app.vectorstore.qdrant import QdrantStore


class ApplicationLifecycle:
    """Manages application startup and shutdown lifecycle."""

    def __init__(self):
        self.socket_client: Optional[SocketClient] = None
        self.vector_store: Optional[QdrantStore] = None
        self.graph_store = None
        self.document_processor = document_processor
        self._running = False

    async def startup(self) -> None:
        """Initialize application components on startup."""
        logger.info("Starting application: {}", settings.app_name)

        try:
            # Validate required configuration
            self._validate_config()

            # Initialize vector store
            logger.info("Initializing vector store...")
            self.vector_store = QdrantStore()
            await self.vector_store.initialize()

            # Initialize document processor
            logger.info("Initializing document processor...")
            await self.document_processor.initialize()

            # Initialize the model client (validates LLM_* routing; an
            # explicitly configured profile that cannot be served stops startup)
            logger.info("Initializing model client...")
            gemini_client.initialize()

            # Legal knowledge graph. Non-strict: an unreachable graph is
            # logged and the service keeps serving on hybrid search alone.
            # Registering it also hooks graph-fact withdrawal into every
            # vector-store deletion (app/core/knowledge_plane.py).
            from app.core.knowledge_plane import (
                register_knowledge_plane,
                setup_knowledge_plane,
            )

            self.graph_store = await setup_knowledge_plane(strict=False)
            register_knowledge_plane(self.graph_store)

            # Initialize socket client
            logger.info("Initializing socket client...")
            self.socket_client = SocketClient()

            # Register socket client with event handler
            logger.info("Registering socket client with event handler...")
            event_handler.set_socket_client(self.socket_client)

            # Connect to socket server
            logger.info("Connecting to socket server: {}", safe_url(settings.socket_url))
            await self.socket_client.connect()

            self._running = True
            logger.success("Application started successfully")

        except Exception as e:
            logger.error("Failed to start application: {}", str(e))
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        """Cleanup application components on shutdown."""
        logger.info("Shutting down application...")
        self._running = False

        # Disconnect socket client
        if self.socket_client:
            try:
                await self.socket_client.disconnect()
                logger.info("Socket client disconnected")
            except Exception as e:
                logger.error("Error disconnecting socket client: {}", str(e))

        if self.graph_store is not None:
            try:
                await self.graph_store.close()
            except Exception as e:
                logger.error("Error closing the knowledge graph: {}", e)

        # Close document processor
        if self.document_processor:
            try:
                await self.document_processor.cleanup()
                logger.info("Document processor cleaned up")
            except Exception as e:
                logger.error("Error cleaning up document processor: {}", str(e))

        # Close vector store connection
        if self.vector_store:
            try:
                await self.vector_store.close()
                logger.info("Vector store connection closed")
            except Exception as e:
                logger.error("Error closing vector store: {}", str(e))

        logger.success("Application shutdown complete")

    def _validate_config(self) -> None:
        """Validate required configuration."""
        if not settings.socket_url:
            raise ConfigurationError("SOCKET_URL is required")

        # The handshake is signed; without the shared secret every connection is
        # refused by the backend, so fail fast with a clear message instead of
        # looping on an opaque refusal.
        if not settings.socket_secret_key:
            raise ConfigurationError(
                "SOCKET_SECRET_KEY is required to sign the Socket.IO handshake"
            )

        scheme = (settings.socket_auth_scheme or "").strip().lower()
        if scheme not in (SCHEME_V1, SCHEME_LEGACY):
            raise ConfigurationError(
                f"SOCKET_AUTH_SCHEME must be '{SCHEME_V1}' or '{SCHEME_LEGACY}', "
                f"got {settings.socket_auth_scheme!r}"
            )
        if scheme == SCHEME_LEGACY:
            if not settings.socket_ai_payload:
                raise ConfigurationError(
                    "SOCKET_AI_PAYLOAD is required when SOCKET_AUTH_SCHEME=legacy"
                )
            logger.warning(
                "SOCKET_AUTH_SCHEME=legacy - the handshake signature is a "
                "constant and is replayable forever. This is a rollback-only "
                "setting and is accepted only while the backend's "
                "AI_SIGNATURE_ALLOW_LEGACY window is open. Return to 'v1'."
            )

        # Fails loudly here rather than as an unexplained bad_signature at connect.
        socket_auth_self_check()

        if (
            not settings.secret_key
            or settings.secret_key == "your-secret-key-here-change-in-production"
        ):
            if settings.is_production:
                raise ConfigurationError("SECRET_KEY must be set in production")
            logger.warning("Using default SECRET_KEY - not suitable for production")

        logger.debug("Configuration validated successfully")

    @property
    def is_running(self) -> bool:
        """Check if application is running."""
        return self._running


# Global lifecycle instance
lifecycle = ApplicationLifecycle()
