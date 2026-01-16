"""
Application lifecycle management.
"""
import asyncio
from typing import Optional
from loguru import logger

from app.core.config import settings
from app.socket.client import SocketClient
from app.vectorstore.qdrant import QdrantStore
from app.processing.document_processor import document_processor
from app.ai.gemini_client import gemini_client
from app.utils.exceptions import ConfigurationError


class ApplicationLifecycle:
    """Manages application startup and shutdown lifecycle."""
    
    def __init__(self):
        self.socket_client: Optional[SocketClient] = None
        self.vector_store: Optional[QdrantStore] = None
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
            
            # Initialize Gemini AI client
            logger.info("Initializing Google Gemini AI client...")
            gemini_client.initialize()
            
            # Note: GCP Vision OCR is now integrated into file_processor
            # and initializes automatically when used
            
            # Initialize socket client
            logger.info("Initializing socket client...")
            self.socket_client = SocketClient()
            
            # Connect to socket server
            logger.info("Connecting to socket server: {}", settings.socket_url)
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
        
        if not settings.secret_key or settings.secret_key == "your-secret-key-here-change-in-production":
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

