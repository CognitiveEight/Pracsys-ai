"""
AI Socket.IO Service - Main orchestration layer.

This module provides the main AI service that:
1. Connects to Socket.IO server as AI_Maverick client
2. Listens for workspace_created, workspace_sync, user_message_request
3. Processes messages with Intelligence Chat Agent
4. Emits ai_response back to server

Usage:
    from app.socket.ai_service import ai_socket_service
    
    # Start the service
    await ai_socket_service.start()
    
    # Service runs in background, processing events
    # ...
    
    # Stop the service
    await ai_socket_service.stop()
"""

import asyncio
from typing import Optional

from loguru import logger

from app.core.config import settings
from app.socket.client import SocketClient


class AISocketService:
    """
    AI Socket.IO Service - Main orchestration layer.

    This service manages the Socket.IO connection and coordinates
    event handling with the Intelligence Chat Agent.
    """

    def __init__(self):
        """Initialize the AI Socket Service."""
        self.client: Optional[SocketClient] = None
        self._running = False
        self._background_task: Optional[asyncio.Task] = None
        logger.info("AI Socket Service initialized")

    async def start(self) -> None:
        """
        Start the AI Socket Service.

        This will:
        1. Create Socket.IO client
        2. Connect to server with AI_Maverick authentication
        3. Start listening for events

        Raises:
            SocketConnectionError: If connection fails
        """
        if self._running:
            logger.warning("AI Socket Service is already running")
            return

        try:
            logger.info("Starting AI Socket Service...")

            # Delegate to the shared application lifecycle rather than only
            # opening the socket. This method used to create a SocketClient and
            # connect, and nothing else — so the vector store, the document
            # processor and the Gemini client were never initialised. The
            # service came up green and then failed on the first document,
            # which reads like an ingestion bug rather than a service that was
            # never booted. Delegating also keeps this entry point and
            # ``python -m app.main`` on one code path, so they cannot drift.
            from app.core.lifecycle import lifecycle

            await lifecycle.startup()

            # Reuse the lifecycle's client; creating a second one here would
            # open a second connection to the same ai_service room and every
            # event would be handled twice.
            self.client = lifecycle.socket_client
            self._running = True

            logger.success("AI Socket Service started successfully")

        except Exception as e:
            logger.error("Failed to start AI Socket Service: {}", e)
            self._running = False
            raise

    async def stop(self) -> None:
        """
        Stop the AI Socket Service.

        This will:
        1. Disconnect from Socket.IO server
        2. Clean up resources
        """
        if not self._running:
            logger.warning("AI Socket Service is not running")
            return

        try:
            logger.info("Stopping AI Socket Service...")

            # Mirror start(): tear the whole stack down, not just the socket.
            # Disconnecting alone left the document processor's temp files and
            # the Qdrant connection open.
            from app.core.lifecycle import lifecycle

            await lifecycle.shutdown()
            self.client = None
            self._running = False

            logger.success("AI Socket Service stopped")

        except Exception as e:
            logger.error("Error stopping AI Socket Service: {}", e)
            raise

    async def run_forever(self) -> None:
        """
        Run the AI Socket Service forever.

        This is a blocking call that keeps the service running
        until interrupted or an error occurs.
        """
        await self.start()

        try:
            logger.info("AI Socket Service running... (Press Ctrl+C to stop)")

            # Keep the service running
            while self._running:
                await asyncio.sleep(1)

                # Check connection health
                if self.client and not self.client.is_connected:
                    logger.warning(
                        "Socket disconnected, reconnection will be attempted automatically"
                    )
                    # Socket.IO client will handle reconnection automatically

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error("Error in AI Socket Service: {}", e)
            raise
        finally:
            await self.stop()

    @property
    def is_running(self) -> bool:
        """Check if the service is running."""
        return self._running

    @property
    def is_connected(self) -> bool:
        """Check if the socket is connected."""
        return self.client.is_connected if self.client else False

    def get_status(self) -> dict:
        """
        Get the current status of the AI Socket Service.

        Returns:
            dict: Status information
        """
        return {
            "running": self._running,
            "connected": self.is_connected,
            "socket_url": settings.socket_url,
            "client_type": "AI_Maverick",
            "events_listening": [
                "workspace_created",
                "workspace_sync",
                "user_message_request",
                "analysis_docx_generate_request",
            ],
            "events_emitting": ["ai_response"],
        }


# Global AI Socket Service instance
ai_socket_service = AISocketService()


# Convenience functions for direct usage
async def start_ai_service() -> None:
    """Start the AI Socket Service."""
    await ai_socket_service.start()


async def stop_ai_service() -> None:
    """Stop the AI Socket Service."""
    await ai_socket_service.stop()


async def run_ai_service_forever() -> None:
    """Run the AI Socket Service forever (blocking)."""
    await ai_socket_service.run_forever()


# CLI entry point
async def main():
    """Run the AI service standalone.

    Delegates to the same ``Application`` that ``python -m app.main`` runs, so
    the two commands are equivalent and cannot drift. That matters: this entry
    point previously only opened the socket, leaving the vector store, document
    processor and Gemini client uninitialised — the service reported a healthy
    connection and then failed on the first document.

    ``Application.run()`` performs the full startup, installs SIGTERM/SIGINT
    handlers, blocks until a shutdown signal, and tears the stack down.
    """
    from app.main import Application

    logger.info("AI Socket.IO service (client type AI_Maverick, HMAC-SHA256 handshake)")
    await Application().run()


if __name__ == "__main__":
    """Run the AI Socket Service standalone.

    Equivalent to ``python -m app.main``.
    """
    from app.observability.logger import setup_logging

    # Configure logging BEFORE anything else, exactly as app.main does —
    # without this the service ran on Loguru's defaults and the file sinks,
    # rotation and audit log were never set up.
    setup_logging()

    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover - top-level crash reporting
        logger.critical("Fatal error: {}", str(exc))
        raise
