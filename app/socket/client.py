"""
Socket.IO client for connecting to the workspace server.

Backend note: To avoid "Connection lost" during long AI processing (e.g. chat response
taking 10–20s), the Socket.IO server should set a high pingTimeout (e.g. 60000 ms) and
sensible pingInterval so the connection is not closed while the AI service is generating.
"""

import asyncio
import base64
import hashlib
import json
import hmac
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import socketio
from loguru import logger

from app import __version__ as app_version
from app.core.config import settings
from app.socket.backend_events import AI_HEARTBEAT_PING, AI_HEARTBEAT_PONG
from app.socket.events import event_handler
from app.utils.exceptions import SocketConnectionError

_AI_PROCESS_START_MONO = time.monotonic()


def _utc_iso_ms_z() -> str:
    """UTC ISO-8601 with millisecond precision and Z suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}" + "Z"


class SocketClient:
    """Socket.IO client wrapper for managing connections and events."""

    def __init__(self):
        self.sio: Optional[socketio.AsyncClient] = None
        self._connected = False
        self._reconnect_task: Optional[asyncio.Task] = None
        # Used to coordinate short waits during transient disconnects.
        self._connected_event: asyncio.Event = asyncio.Event()

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
            logger.warning(
                "Socket signature authentication not configured (SOCKET_SECRET_KEY missing)"
            )
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
                settings.socket_secret_key.encode("utf-8"),  # key
                payload.encode("utf-8"),  # message
                hashlib.sha256,  # digestmod
            ).digest()

            # Encode signature in Base64 URL-safe (per backend spec)
            signature_b64 = base64.urlsafe_b64encode(signature).decode("utf-8")
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
        headers = {"x-client-type": "AI_Maverick"}

        # Add signature if configured
        signature = self._generate_signature()
        if signature:
            headers["x-signature"] = signature
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
            reconnection_delay_max = getattr(
                settings, "socket_reconnection_delay_max", 60000
            )

            # Convert delay to seconds (0 means no delay)
            delay_seconds = reconnection_delay / 1000 if reconnection_delay > 0 else 0
            delay_max_seconds = (
                reconnection_delay_max / 1000 if reconnection_delay_max > 0 else 60.0
            )

            self.sio = socketio.AsyncClient(
                reconnection=settings.socket_reconnection,
                reconnection_attempts=reconnection_attempts,  # 0 = infinite
                reconnection_delay=delay_seconds,
                reconnection_delay_max=delay_max_seconds,  # cap backoff so we don't wait too long
                logger=False,
                engineio_logger=False,
            )

            # Register event handlers
            self._register_handlers()

            # Get custom headers with authentication
            custom_headers = self._get_custom_headers()

            # Connect to server
            logger.info("Connecting to socket server: {}", settings.socket_url)
            logger.info("Socket path: {}", settings.socket_path)
            logger.info("Namespace: {}", settings.socket_namespace)
            logger.info(
                "Custom headers: {}",
                {
                    k: "***" if k == "x-signature" else v
                    for k, v in custom_headers.items()
                },
            )

            if custom_headers.get("x-signature"):
                logger.success("✅ Authentication signature generated successfully")
            else:
                logger.warning(
                    "⚠️ No authentication signature (SECRET_KEY or PAYLOAD_JSON not configured)"
                )

            await self.sio.connect(
                settings.socket_url,
                socketio_path=settings.socket_path,
                headers=custom_headers,
                namespaces=[settings.socket_namespace],
                wait=True,
                wait_timeout=60,
            )

            # Give server a moment to process connection and send any error messages
            await asyncio.sleep(0.5)

            if self._connected:
                logger.success("Successfully connected to socket server as AI_Maverick")
            else:
                logger.error(
                    "Connection established but immediately lost - check server logs"
                )

        except Exception as e:
            logger.error("Failed to connect to socket server: {}", str(e))
            raise SocketConnectionError(
                "Failed to connect to socket server",
                details={"error": str(e), "url": settings.socket_url},
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
        Emit an event to the server. If socket is disconnected or emit fails (e.g. namespace
        closed), logs and returns without raising so long-running handlers can cache and re-emit
        after reconnect.
        """
        if not self.sio:
            logger.warning(
                "Socket client not initialized; skipping emit for event: {}",
                event,
            )
            return

        # If temporarily disconnected, wait briefly for reconnection.
        if not self._connected:
            wait_timeout = (
                getattr(settings, "socket_emit_wait_timeout_seconds", 15.0) or 15.0
            )
            logger.warning(
                "Socket not connected; waiting up to {:.1f}s before skipping event: {}",
                wait_timeout,
                event,
            )
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=wait_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Socket still not connected after {:.1f}s; skipping emit for event: {}",
                    wait_timeout,
                    event,
                )
                return

        # Now we consider ourselves connected for emitting.
        if not self._connected:
            logger.warning(
                "Socket connection state not ready; skipping emit for event: {}", event
            )
            return
        try:
            logger.info(f"📤 Emitting event: {event}")
            await self.sio.emit(event, data, namespace=settings.socket_namespace)
            logger.success(f"✅ Event emitted successfully: {event}")
            logger.debug("Event data: {}", data)
        except Exception as e:
            err_msg = str(e)
            logger.error("Failed to emit event {}: {}", event, err_msg)
            # Treat namespace/connection errors as disconnected so callers don't burst-retry
            if (
                "not a connected namespace" in err_msg
                or "connection" in err_msg.lower()
            ):
                self._connected = False
                # So re-emit runs after reconnect even if disconnect event was not fired
                try:
                    event_handler._had_disconnect = True
                except Exception:
                    pass
            # Do not raise: allow caller to cache payload and re-emit after reconnect

    async def emit_ai_response(self, data: Dict[str, Any]) -> None:
        """
        Emit AI response to the server using ai_response event.
        If socket is disconnected, logs and returns without raising so chat handler does not crash.
        After emit, briefly yields so the socket can flush the payload (reduces disconnect-before-delivery).
        """
        if not self.sio:
            logger.warning(
                "Socket client not initialized; skipping ai_response for chat: {}",
                data.get("chatId"),
            )
            return

        if not self._connected:
            wait_timeout = (
                getattr(settings, "socket_emit_wait_timeout_seconds", 15.0) or 15.0
            )
            logger.warning(
                "Socket not connected; waiting up to {:.1f}s before skipping ai_response for chat: {}",
                wait_timeout,
                data.get("chatId"),
            )
            try:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=wait_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Socket still not connected after {:.1f}s; skipping ai_response for chat: {}",
                    wait_timeout,
                    data.get("chatId"),
                )
                return

        if not self._connected:
            # Safety check
            return
        try:
            await self.sio.emit(
                "ai_response", data, namespace=settings.socket_namespace
            )
            logger.success("✅ Emitted ai_response for chat: {}", data.get("chatId"))
            logger.debug("AI response data: {}", data)
            await asyncio.sleep(0.25)
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

    def _heartbeat_pong_payload(self, payload_in: Dict[str, Any]) -> Dict[str, Any]:
        """Build pong body; echoes requestId/serverSentAt from Node when present."""
        return {
            "requestId": payload_in.get("requestId"),
            "serverSentAt": payload_in.get("serverSentAt"),
            "aiTimestamp": _utc_iso_ms_z(),
            "status": "ok",
            "version": app_version,
            "uptimeMs": int((time.monotonic() - _AI_PROCESS_START_MONO) * 1000),
        }

    async def _emit_heartbeat_pong(self, payload: Dict[str, Any]) -> None:
        """
        Emit pong on every backend ping. Does not use emit-wait logic.
        Always attempts if the client exists so idle sockets keep application-level traffic.
        """
        if not self.sio:
            return
        try:
            await self.sio.emit(
                AI_HEARTBEAT_PONG,
                payload,
                namespace=settings.socket_namespace,
            )
        except Exception as e:
            logger.warning("Heartbeat pong emit failed: {}", e)

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
            logger.info("   - probing_prompts_request")
            logger.info("   - analysis_report_feedback")
            logger.info("   - added_missing_information")
            logger.info("   - applicable_law_relevance_request")
            logger.info("   - add_case_law")
            logger.info("   - add_relevant_party")
            logger.info("   - edit_relevant_party")
            logger.info("   - delete_relevant_party")
            logger.info("   - analysis_docx_generate_request")
            logger.info("   - template_sync")
            logger.info("   - template_sync_response")
            logger.info("   - ai_heartbeat_ping")
            logger.info(
                "📤 Ready to emit: ai_response, probing_prompts_response, document_feature_response, applicable_law_relevance_response, analysis_docx_generate_response, ai_heartbeat_pong"
            )
            self._connected = True
            self._connected_event.set()
            await event_handler.handle("connect", None)

        @self.sio.event
        async def disconnect(reason=None):
            """Handle disconnection event with reason."""
            self._connected = False
            self._connected_event.clear()
            logger.warning("Socket.IO disconnected")
            if reason:
                logger.warning("🔴 Disconnect reason: {}", reason)
                if reason == "transport close":
                    logger.info(
                        "Server closed the connection (check server pingTimeout if during long AI work)"
                    )
                elif reason == "ping timeout":
                    logger.info(
                        "Ping timeout – backend should increase pingTimeout (e.g. 60000) for long-running AI"
                    )
                elif reason == "transport error":
                    logger.warning("Transport error (network/proxy)")
                elif reason == "server namespace disconnect":
                    logger.warning("Server disconnected (check auth)")
            else:
                logger.warning("🔴 Connection lost (no reason provided)")
            logger.info("Reconnecting automatically (delay from config)...")
            await event_handler.handle("disconnect", {"reason": reason})

        @self.sio.event
        async def connect_error(data):
            """Handle connection error."""
            logger.error("Socket.IO connection error: {}", data)
            logger.error(
                "❌ Connection failed - check authentication and server status"
            )
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
                logger.info(
                    "📥 workspace_sync RAW payload: {}",
                    raw[:2000] + ("..." if len(raw) > 2000 else ""),
                )
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

        @self.sio.event
        async def probing_prompts_request(data):
            """
            Handle probing_prompts_request (Server → AI_Maverick).
            For STRATEGY_OBJECTIVE chatType, respond with random 4 suggestion chips as prompts.
            """
            logger.info("📋 Received probing_prompts_request event")
            await event_handler.handle("probing_prompts_request", data)

        @self.sio.event
        async def analysis_report_feedback(data):
            """
            Handle analysis_report_feedback (App → Backend → AI_Maverick).
            User rejected an analysis report item (missingInfo or riskOrWeakness); recalc score and re-emit report.
            """
            logger.info("📋 Received analysis_report_feedback event")
            await event_handler.handle("analysis_report_feedback", data)

        @self.sio.event
        async def added_missing_information(data):
            """
            Handle added_missing_information (App → Backend → AI_Maverick).
            User manually supplied a title+description to address previously-flagged
            missingInfo. AI assesses case relevance; if related, the analysis score
            is decreased and qualityScoreExplanation is regenerated, then re-emitted.
            """
            logger.info("📝 Received added_missing_information event")
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "added_missing_information payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info("added_missing_information payload (repr): {}", repr(data))
            await event_handler.handle("added_missing_information", data)

        @self.sio.event
        async def applicable_law_relevance_request(data):
            """
            Handle applicable_law_relevance_request (Server → AI_Maverick).
            Lawyer-selected law; AI returns applicability as applicable_law_relevance_response.
            """
            logger.info(
                "⚖️ applicable_law_relevance_request ← backend (socket received)"
            )
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "applicable_law_relevance_request payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info(
                    "applicable_law_relevance_request payload (repr): {}", repr(data)
                )
            await event_handler.handle("applicable_law_relevance_request", data)

        @self.sio.event
        async def add_case_law(data):
            """
            Handle add_case_law (App → Backend → AI_Maverick).
            User manually adds a case-law precedent via the "Add Case Law" button
            by supplying a citation, a source URL, or free text. AI resolves and
            verifies a real source, assesses relevance, appends to the Case Laws
            panel, and re-emits featureType=case_laws.
            """
            logger.info("📚 Received add_case_law event")
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "add_case_law payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info("add_case_law payload (repr): {}", repr(data))
            await event_handler.handle("add_case_law", data)

        @self.sio.event
        async def add_relevant_party(data):
            """Handle add_relevant_party (App → Backend → AI). User adds a party."""
            logger.info("👥 Received add_relevant_party event")
            await event_handler.handle("add_relevant_party", data)

        @self.sio.event
        async def edit_relevant_party(data):
            """Handle edit_relevant_party (App → Backend → AI). Edit a party by id."""
            logger.info("👥 Received edit_relevant_party event")
            await event_handler.handle("edit_relevant_party", data)

        @self.sio.event
        async def delete_relevant_party(data):
            """Handle delete_relevant_party (App → Backend → AI). Delete a party by id."""
            logger.info("👥 Received delete_relevant_party event")
            await event_handler.handle("delete_relevant_party", data)

        @self.sio.event
        async def analysis_docx_generate_request(data):
            """
            Handle analysis_docx_generate_request (Server -> AI_Maverick).
            AI returns processed document text via analysis_docx_generate_response.
            """
            logger.info(
                "📝 analysis_docx_generate_request <- backend (socket received)"
            )
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "analysis_docx_generate_request payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info(
                    "analysis_docx_generate_request payload (repr): {}", repr(data)
                )
            await event_handler.handle("analysis_docx_generate_request", data)

        @self.sio.event
        async def template_sync(data):
            """
            Handle template_sync event (Server → AI_Maverick).
            Lawyer-scoped Template Library sync — action ∈ uploaded|deleted|renamed.
            Without this binding, python-socketio falls through to the catch-all
            and the event is silently dropped (only logged at DEBUG level), so
            deletions/renames/uploads never reach the drafting agent.
            """
            logger.info("📚 template_sync ← backend (socket received)")
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "template_sync payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info("template_sync payload (repr): {}", repr(data))
            await event_handler.handle("template_sync", data)

        @self.sio.event
        async def template_sync_response(data):
            """
            Server → AI_Maverick: full snapshot of a user's active templates
            in reply to our request_template_sync emit. Populates the
            in-memory template store on demand (e.g. after an AI restart).
            """
            logger.info("📚 template_sync_response ← backend (socket received)")
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "template_sync_response payload (preview): {}",
                    raw[:1800] + ("..." if len(raw) > 1800 else ""),
                )
            except Exception:
                logger.info("template_sync_response payload (repr): {}", repr(data))
            await event_handler.handle("template_sync_response", data)

        @self.sio.event
        async def request_blueprint_proposal(data):
            """
            Server → AI: the "Regenerate Extra Info" action on the dashboard's
            blueprint-approval screen. The AI re-profiles the template and emits
            a fresh template_blueprint_proposal. Without this explicit binding
            the event falls through the catch-all and is silently dropped.
            """
            logger.info("📚 request_blueprint_proposal ← backend (socket received)")
            try:
                raw = json.dumps(data, default=str) if data is not None else "null"
                logger.info(
                    "request_blueprint_proposal payload (preview): {}",
                    raw[:1200] + ("..." if len(raw) > 1200 else ""),
                )
            except Exception:
                logger.info("request_blueprint_proposal payload (repr): {}", repr(data))
            await event_handler.handle("request_blueprint_proposal", data)

        @self.sio.on(AI_HEARTBEAT_PING)
        async def ai_heartbeat_ping(data):
            """
            Node backend → AI: reply with ai_heartbeat_pong on every ping so the link stays active.
            """
            payload_in = data if isinstance(data, dict) else {}
            try:
                out = self._heartbeat_pong_payload(payload_in)
            except Exception as e:
                logger.warning(
                    "heartbeat ping payload build failed, sending minimal pong: {}", e
                )
                out = {
                    "aiTimestamp": _utc_iso_ms_z(),
                    "status": "ok",
                    "version": app_version,
                    "uptimeMs": int((time.monotonic() - _AI_PROCESS_START_MONO) * 1000),
                }
            await self._emit_heartbeat_pong(out)

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
