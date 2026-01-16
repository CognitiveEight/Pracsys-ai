"""
Socket.IO client for connecting to the workspace server.
"""
import asyncio
from typing import Optional, Dict, Any
import socketio
from loguru import logger

from app.core.config import settings
from app.socket.events import event_handler
from app.utils.exceptions import SocketConnectionError


class SocketClient:
    """Socket.IO client wrapper for managing connections and events."""
    
    def __init__(self):
        self.sio: Optional[socketio.AsyncClient] = None
        self._connected = False
        self._reconnect_task: Optional[asyncio.Task] = None
    
    async def connect(self) -> None:
        """
        Establish connection to the Socket.IO server.
        
        Raises:
            SocketConnectionError: If connection fails
        """
        try:
            # Create Socket.IO client
            self.sio = socketio.AsyncClient(
                reconnection=settings.socket_reconnection,
                reconnection_attempts=settings.socket_reconnection_attempts,
                reconnection_delay=settings.socket_reconnection_delay / 1000,
                logger=False,
                engineio_logger=False
            )
            
            # Register event handlers
            self._register_handlers()
            
            # Connect to server
            logger.info("Connecting to socket server: {}", settings.socket_url)
            await self.sio.connect(
                settings.socket_url,
                socketio_path=settings.socket_path,
                namespaces=[settings.socket_namespace]
            )
            
            self._connected = True
            logger.success("Successfully connected to socket server")
            
        except Exception as e:
            logger.error("Failed to connect to socket server: {}", str(e))
            raise SocketConnectionError(
                "Failed to connect to socket server",
                details={"error": str(e), "url": settings.socket_url}
            )
    
    async def disconnect(self) -> None:
        """Disconnect from the Socket.IO server."""
        if self.sio and self._connected:
            try:
                await self.sio.disconnect()
                self._connected = False
                logger.info("Disconnected from socket server")
            except Exception as e:
                logger.error("Error during disconnect: {}", str(e))
    
    async def emit(self, event: str, data: Dict[str, Any]) -> None:
        """
        Emit an event to the server.
        
        Args:
            event: Event name
            data: Event data
        """
        if not self.sio or not self._connected:
            raise SocketConnectionError("Socket not connected")
        
        try:
            await self.sio.emit(event, data, namespace=settings.socket_namespace)
            logger.debug("Emitted event: {} with data: {}", event, data)
        except Exception as e:
            logger.error("Failed to emit event {}: {}", event, str(e))
            raise
    
    async def subscribe(self, workspace_id: str) -> None:
        """
        Subscribe to workspace updates.
        
        Args:
            workspace_id: Workspace identifier
        """
        await self.emit("subscribe", {"workspace_id": workspace_id})
        logger.info("Subscribed to workspace: {}", workspace_id)
    
    async def unsubscribe(self, workspace_id: str) -> None:
        """
        Unsubscribe from workspace updates.
        
        Args:
            workspace_id: Workspace identifier
        """
        await self.emit("unsubscribe", {"workspace_id": workspace_id})
        logger.info("Unsubscribed from workspace: {}", workspace_id)
    
    def _register_handlers(self) -> None:
        """Register Socket.IO event handlers."""
        
        @self.sio.event
        async def connect():
            """Handle connection event."""
            logger.info("Socket.IO connected")
            await event_handler.handle("connect", None)
        
        @self.sio.event
        async def disconnect():
            """Handle disconnection event."""
            logger.info("Socket.IO disconnected")
            self._connected = False
            await event_handler.handle("disconnect", None)
        
        @self.sio.event
        async def connect_error(data):
            """Handle connection error."""
            logger.error("Socket.IO connection error: {}", data)
            await event_handler.handle("error", data)
        
        @self.sio.event
        async def workspace_created(data):
            """Handle workspace created event."""
            await event_handler.handle("workspace_created", data)
        
        @self.sio.event
        async def workspace_updated(data):
            """Handle workspace updated event."""
            await event_handler.handle("workspace_updated", data)
        
        @self.sio.event
        async def workspace_deleted(data):
            """Handle workspace deleted event."""
            await event_handler.handle("workspace_deleted", data)
        
        @self.sio.event
        async def chat_message(data):
            """Handle chat message event."""
            await event_handler.handle("chat_message", data)
        
        @self.sio.on("*")
        async def catch_all(event, data):
            """Catch all other events."""
            logger.debug("Received event: {} with data: {}", event, data)
    
    @property
    def is_connected(self) -> bool:
        """Check if socket is connected."""
        return self._connected

