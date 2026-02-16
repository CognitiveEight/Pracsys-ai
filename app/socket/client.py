"""
Socket.IO client for connecting to the workspace server.
"""
import asyncio
import hmac
import hashlib
import base64
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
    
    def _generate_signature(self) -> Optional[str]:
        """
        Generate HMAC-SHA256 signature for authentication.
        
        Per backend specification:
        - Signature = HMAC_SHA256(SOCKET_SECRET_KEY, ai_chat_user)
        - Encoded as Base64 URL-safe
        
        Returns:
            Base64-encoded signature or None if credentials not configured
        """
        if not settings.socket_secret_key:
            logger.warning("Socket signature authentication not configured (SOCKET_SECRET_KEY missing)")
            return None
        
        try:
            # Get payload from configuration
            payload = settings.socket_ai_payload
            
            if not payload:
                logger.error("SOCKET_AI_PAYLOAD not configured in .env file")
                return None
            
            # Generate HMAC-SHA256 signature using SOCKET_SECRET_KEY + payload
            # HMAC requires: key, message, digestmod
            signature = hmac.new(
                settings.socket_secret_key.encode('utf-8'),  # key
                payload.encode('utf-8'),                      # message
                hashlib.sha256                                # digestmod
            ).digest()
            
            # Encode signature in Base64 URL-safe (per backend spec)
            signature_b64 = base64.urlsafe_b64encode(signature).decode('utf-8')
            logger.debug("Generated socket signature for AI_Maverick client")
            logger.debug(f"Payload: {payload}")
            logger.debug(f"Signature: {signature_b64}")
            
            return signature_b64
        except Exception as e:
            logger.error("Failed to generate signature: {}", str(e))
            return None
    
    def _get_custom_headers(self) -> Dict[str, str]:
        """
        Get custom headers for socket connection including authentication.
        
        Returns:
            Dictionary of custom headers
        """
        headers = {
            'x-client-type': 'AI_Maverick'
        }
        
        # Add signature if configured
        signature = self._generate_signature()
        if signature:
            headers['x-signature'] = signature
            logger.debug("Added authentication signature to headers")
        
        return headers
    
    async def connect(self) -> None:
        """
        Establish connection to the Socket.IO server.
        
        Raises:
            SocketConnectionError: If connection fails
        """
        try:
            # Create Socket.IO client with reconnection parameters
            reconnection_attempts = settings.socket_reconnection_attempts
            reconnection_delay = settings.socket_reconnection_delay
            
            # Convert delay to seconds (0 means no delay)
            delay_seconds = reconnection_delay / 1000 if reconnection_delay > 0 else 0
            
            self.sio = socketio.AsyncClient(
                reconnection=settings.socket_reconnection,
                reconnection_attempts=reconnection_attempts,  # 0 = infinite
                reconnection_delay=delay_seconds,
                logger=False,
                engineio_logger=False
            )
            
            # Register event handlers
            self._register_handlers()
            
            # Get custom headers with authentication
            custom_headers = self._get_custom_headers()
            
            # Connect to server
            logger.info("Connecting to socket server: {}", settings.socket_url)
            logger.info("Socket path: {}", settings.socket_path)
            logger.info("Namespace: {}", settings.socket_namespace)
            logger.info("Custom headers: {}", {k: "***" if k == "x-signature" else v for k, v in custom_headers.items()})
            
            if custom_headers.get('x-signature'):
                logger.success("✅ Authentication signature generated successfully")
            else:
                logger.warning("⚠️ No authentication signature (SECRET_KEY or PAYLOAD_JSON not configured)")
            
            await self.sio.connect(
                settings.socket_url,
                socketio_path=settings.socket_path,
                headers=custom_headers,
                namespaces=[settings.socket_namespace],
                wait=True,
                wait_timeout=10
            )
            
            # Give server a moment to process connection and send any error messages
            await asyncio.sleep(0.5)
            
            if self._connected:
                logger.success("Successfully connected to socket server as AI_Maverick")
            else:
                logger.error("Connection established but immediately lost - check server logs")
            
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
            logger.info(f"📤 Emitting event: {event}")
            await self.sio.emit(event, data, namespace=settings.socket_namespace)
            logger.success(f"✅ Event emitted successfully: {event}")
            logger.debug("Event data: {}", data)
        except Exception as e:
            logger.error("Failed to emit event {}: {}", event, str(e))
            raise
    
    async def emit_ai_response(self, data: Dict[str, Any]) -> None:
        """
        Emit AI response to the server using ai_response event.
        
        Per backend specification:
        AI_Maverick → Server: ai_response event with payload:
        {
            chatId: str,
            workspaceId: str,
            userId: str,
            content: str,
            metadata: {...}
        }
        
        Args:
            data: AI response payload
        """
        if not self.sio or not self._connected:
            raise SocketConnectionError("Socket not connected")
        
        try:
            await self.sio.emit(
                "ai_response",
                data,
                namespace=settings.socket_namespace
            )
            logger.success("✅ Emitted ai_response for chat: {}", data.get("chatId"))
            logger.debug("AI response data: {}", data)
        except Exception as e:
            logger.error("Failed to emit ai_response: {}", str(e))
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
        """
        Register Socket.IO event handlers.
        
        Per backend specification, AI service listens for:
        - workspace_created: New workspace created
        - workspace_sync: Workspace updated
        - user_message_request: User sent chat message
        """
        
        @self.sio.event
        async def connect():
            """Handle connection event."""
            logger.info("Socket.IO connected")
            logger.success("🟢 Connection established with server as AI_Maverick")
            logger.info("📡 Listening for events:")
            logger.info("   - workspace_created")
            logger.info("   - workspace_sync")
            logger.info("   - user_message_request")
            logger.info("📤 Ready to emit: ai_response")
            self._connected = True
            await event_handler.handle("connect", None)
        
        @self.sio.event
        async def disconnect(reason=None):
            """Handle disconnection event with reason."""
            self._connected = False
            logger.warning("Socket.IO disconnected")
            if reason:
                logger.warning(f"🔴 Disconnect reason: {reason}")
                
                # Interpret common disconnect reasons
                if reason == "transport close":
                    logger.error("❌ Server closed the connection (possibly authentication failure)")
                elif reason == "ping timeout":
                    logger.error("❌ Connection timed out (no ping response)")
                elif reason == "transport error":
                    logger.error("❌ Transport error (network issue)")
                elif reason == "server namespace disconnect":
                    logger.error("❌ Server forcefully disconnected (check authentication)")
                else:
                    logger.warning(f"Disconnect reason: {reason}")
            else:
                logger.warning("🔴 Connection lost (no reason provided)")
            
            logger.info("Reconnection will be attempted automatically...")
            await event_handler.handle("disconnect", {"reason": reason})
        
        @self.sio.event
        async def connect_error(data):
            """Handle connection error."""
            logger.error("Socket.IO connection error: {}", data)
            logger.error("❌ Connection failed - check authentication and server status")
            await event_handler.handle("error", data)
        
        @self.sio.event
        async def error(data):
            """Handle general error event from server."""
            logger.error("Socket.IO error from server: {}", data)
            await event_handler.handle("error", data)
        
        @self.sio.event
        async def unauthorized(data):
            """Handle unauthorized/authentication failure."""
            logger.error("🚫 Authentication failed - check SOCKET_SECRET_KEY")
            logger.error("Server response: {}", data)
        
        # ========================================================================
        # Backend-specified events (AI_Maverick listens to these)
        # ========================================================================
        
        @self.sio.event
        async def connected(data):
            """
            Handle 'connected' event from server.
            Confirms successful connection with userId and clientType.
            """
            logger.success("✅ Server confirmed connection")
            logger.info(f"Connection data: {data}")
            await event_handler.handle("connected", data)
        
        @self.sio.event
        async def workspace_created(data):
            """
            Handle workspace_created event (Server → AI_Maverick).
            New workspace created, emitted to ai_service room.
            """
            logger.info("📦 Received workspace_created event")
            await event_handler.handle("workspace_created", data)
        
        @self.sio.event
        async def workspace_sync(data):
            """
            Handle workspace_sync event (Server → AI_Maverick).
            Workspace updated (title/status/files), emitted to ai_service room.
            """
            logger.info("🔄 Received workspace_sync event")
            # Log raw websocket input for debugging (e.g. delete flags, evidenceFiles)
            try:
                import json
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info("📥 workspace_sync RAW payload: {}", raw[:2000] + ("..." if len(raw) > 2000 else ""))
            except Exception:
                logger.info("📥 workspace_sync RAW payload (repr): {}", repr(data))
            await event_handler.handle("workspace_sync", data)
        
        @self.sio.event
        async def user_message_request(data):
            """
            Handle user_message_request event (Server → AI_Maverick).
            User message forwarded to AI service, emitted to ai_service room.
            """
            logger.info("💬 Received user_message_request event")
            await event_handler.handle("user_message_request", data)
        
        # ========================================================================
        # Legacy event handlers (for backward compatibility)
        # ========================================================================
        
        @self.sio.event
        async def workspace_updated(data):
            """Handle workspace updated event (legacy)."""
            logger.debug("Received legacy workspace_updated event")
            await event_handler.handle("workspace_updated", data)
        
        @self.sio.event
        async def workspace_deleted(data):
            """Handle workspace deleted event (legacy)."""
            logger.debug("Received legacy workspace_deleted event")
            await event_handler.handle("workspace_deleted", data)
        
        @self.sio.on("*")
        async def catch_all(event, *args):
            """Catch all other events from server."""
            logger.debug(f"📩 Received unhandled event: {event}")
            if args:
                logger.debug(f"   Event data: {args}")
            
            # Handle common server events
            if event == "auth_error" or event == "authentication_error":
                logger.error("🚫 Authentication error from server!")
                logger.error(f"   Details: {args}")
            elif event == "error":
                logger.error(f"❌ Error from server: {args}")
            else:
                logger.debug(f"Event: {event}, Data: {args}")
    
    @property
    def is_connected(self) -> bool:
        """Check if socket is connected."""
        return self._connected

