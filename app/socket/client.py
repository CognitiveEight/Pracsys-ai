"""
Socket.IO client for connecting to the workspace server.

Backend note: To avoid "Connection lost" during long AI processing (e.g. chat response
taking 10–20s), the Socket.IO server should set a high pingTimeout (e.g. 60000 ms) and
sensible pingInterval so the connection is not closed while the AI service is generating.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import socketio
from loguru import logger

from app import __version__ as app_version
from app.core.config import settings
from app.socket.auth import (
    CLIENT_ID,
    HEADER_CLIENT_TYPE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SCHEME_V1,
    SocketAuthError,
    build_auth_headers,
    redact_auth_headers,
)
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
        # Handshake bookkeeping. Every connection attempt (including python-socketio's
        # automatic reconnects) signs a fresh nonce, so this records which one is in
        # flight: it is what turns an opaque "connection refused" into a diagnosable
        # event, and what proves at runtime which scheme the backend actually accepted.
        self._handshake_attempts: int = 0
        self._last_handshake: Optional[Dict[str, Any]] = None
        # Reset on every successful connect; see _log_handshake_refusal.
        self._refusal_hint_logged = False

    def _get_custom_headers(self) -> Dict[str, str]:
        """
        Build a FRESH set of handshake headers for one connection attempt.

        This is handed to python-socketio as a callable rather than a dict:
        the library re-resolves callable headers on every connect *and* on every
        automatic reconnect, which is exactly what the v1 scheme requires --
        the nonce is single-use and the timestamp expires after 300s, so reusing
        one set of headers makes the first connection succeed and every
        reconnect fail with ``replayed_nonce``.

        Never raises: it runs inside python-socketio's reconnect loop, which
        only catches ConnectionError/ValueError, so an exception here would kill
        the reconnect task. A misconfiguration degrades to the unsigned
        ``x-client-type`` header, the backend refuses the handshake, and the
        normal reconnect backoff applies. Startup validation in
        ``app/core/lifecycle.py`` catches the missing secret far earlier.

        Returns:
            Handshake headers, lowercase-keyed.
        """
        self._handshake_attempts += 1
        attempt = self._handshake_attempts
        scheme = (settings.socket_auth_scheme or "").strip().lower()

        try:
            headers = build_auth_headers(
                settings.socket_secret_key,
                scheme=settings.socket_auth_scheme,
                legacy_payload=settings.socket_ai_payload,
            )
        except SocketAuthError as e:
            self._last_handshake = {
                "attempt": attempt,
                "scheme": scheme,
                "signed": False,
                "error": str(e),
                "builtAt": _utc_iso_ms_z(),
            }
            logger.error(
                "Handshake #{} NOT signed ({}); connecting unsigned - the backend "
                "will refuse this with missing_header",
                attempt,
                str(e),
            )
            return {HEADER_CLIENT_TYPE: CLIENT_ID}

        signature = headers.get(HEADER_SIGNATURE, "")
        self._last_handshake = {
            "attempt": attempt,
            "scheme": scheme,
            "signed": True,
            "timestamp": headers.get(HEADER_TIMESTAMP),
            "nonce": headers.get(HEADER_NONCE),
            "signatureLength": len(signature),
            "builtAt": _utc_iso_ms_z(),
        }

        # Timestamp and nonce are logged deliberately: neither is a credential, and
        # they are exactly what the backend asks for when diagnosing a refused
        # handshake. The signature and the secret are never logged - only the
        # signature's length, which is the one thing that catches a padding or
        # encoding mistake (v1 must always be 43 unpadded base64url characters).
        if scheme == SCHEME_V1:
            logger.debug(
                "Handshake #{} signed | scheme={} ts={} nonce={} sigLen={}",
                attempt,
                scheme,
                headers.get(HEADER_TIMESTAMP),
                headers.get(HEADER_NONCE),
                len(signature),
            )
            if len(signature) != 43 or "=" in signature:
                logger.error(
                    "Signature is {} chars (padded={}) but v1 requires 43 unpadded "
                    "base64url characters - the backend will reject this as "
                    "bad_signature",
                    len(signature),
                    "=" in signature,
                )
        else:
            logger.warning(
                "Handshake #{} signed with the LEGACY constant signature "
                "(replayable; accepted only while the backend's "
                "AI_SIGNATURE_ALLOW_LEGACY window is open)",
                attempt,
            )

        logger.debug("Handshake headers: {}", redact_auth_headers(headers))
        return headers

    def _handshake_summary(self) -> Dict[str, Any]:
        """
        Return the last handshake's non-secret details for logging.

        Empty until the first connection attempt has built its headers.
        """
        return dict(self._last_handshake or {})

    def _log_handshake_refusal(self, reason: Any) -> None:
        """
        Log everything needed to diagnose a refused handshake without the
        backend's logs, and everything the backend asks for when we raise an
        issue with them -- except the signature and the secret, which must
        never be logged.

        Called from both ``connect_error`` and the failure path of
        :meth:`connect`: the backend disconnects on a bad handshake without a
        descriptive body, and depending on the transport the rejection can
        surface as a namespace error or simply as a connect timeout, so neither
        call site alone is guaranteed to fire.

        Args:
            reason: Whatever the transport reported (may be None).
        """
        hs = self._handshake_summary()
        if not hs:
            logger.error("Handshake refused before any headers were built | reason={}", reason)
            return

        logger.error(
            "Handshake #{} refused | scheme={} ts={} nonce={} sigLen={} builtAt={} reason={}",
            hs.get("attempt"),
            hs.get("scheme"),
            hs.get("timestamp"),
            hs.get("nonce"),
            hs.get("signatureLength"),
            hs.get("builtAt"),
            reason,
        )
        # The diagnosis hint once per outage, not once per retry: a reconnect
        # loop used to write ~8 ERROR lines per attempt (40% of app.log).
        if not self._refusal_hint_logged:
            self._refusal_hint_logged = True
            logger.error(
                "Signed string was 'v1:AI_Maverick:<ts>:<nonce>'. Ask the backend for "
                "the refusal code. EVERY attempt refused => clock drift >300s (check "
                "NTP; local UTC now is {}, sent ts was {}) or a SOCKET_SECRET_KEY "
                "mismatch. Only RECONNECTS refused => the nonce was reused instead of "
                "re-signed. Refused on the legacy scheme => the backend's "
                "AI_SIGNATURE_ALLOW_LEGACY window is closed. Report the values above "
                "- never the signature or the secret.",
                _utc_iso_ms_z(),
                hs.get("timestamp"),
            )

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

            # Connect to server
            transports = settings.socket_transports_list
            from urllib.parse import urlsplit

            logger.info(
                "Connecting to socket server host={} path={} namespace={} auth={} transports={}",
                urlsplit(settings.socket_url).netloc,
                settings.socket_path,
                settings.socket_namespace,
                settings.socket_auth_scheme,
                transports or "default",
            )

            if not settings.socket_secret_key:
                logger.error(
                    "SOCKET_SECRET_KEY is not configured - the backend will refuse "
                    "the handshake"
                )

            # NOTE: the builder is passed as a CALLABLE, not as a pre-built dict.
            # python-socketio resolves callable headers on every connect attempt,
            # including its automatic reconnects, so each attempt gets a fresh
            # nonce and timestamp. Passing a dict here would make the first
            # connection succeed and every reconnect fail with replayed_nonce.
            await self.sio.connect(
                settings.socket_url,
                socketio_path=settings.socket_path,
                headers=self._get_custom_headers,
                transports=transports,
                namespaces=[settings.socket_namespace],
                wait=True,
                wait_timeout=60,
            )

            # Give server a moment to process connection and send any error messages
            await asyncio.sleep(0.5)

            if self._connected:
                logger.info("Connected to socket server as AI_Maverick")
            else:
                logger.error(
                    "Connection established but immediately lost - check server logs"
                )

        except Exception as e:
            logger.error("Failed to connect to socket server: {}", str(e))
            # A refused handshake can surface here as a namespace failure or as a
            # plain wait_timeout, with nothing in the error naming the cause.
            self._log_handshake_refusal(str(e))
            raise SocketConnectionError(
                "Failed to connect to socket server",
                details={
                    "error": str(e),
                    "url": settings.socket_url,
                    "authScheme": settings.socket_auth_scheme,
                    "handshake": self._handshake_summary(),
                },
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

    async def emit(self, event: str, data: Dict[str, Any]) -> bool:
        """
        Emit an event to the server. If socket is disconnected or emit fails (e.g. namespace
        closed), logs and returns without raising so long-running handlers can cache and re-emit
        after reconnect.

        Returns ``True`` only when the payload was handed to the socket, ``False`` on every
        skip/failure path. Callers that must not lose a payload (feature panels, chat
        responses) use this to buffer it for re-delivery after reconnect — without it a
        disconnect mid-generation silently discards the result and the frontend loader,
        which is only ever cleared by the arrival of that payload, spins forever.
        """
        if not self.sio:
            logger.warning(
                "Socket client not initialized; skipping emit for event: {}",
                event,
            )
            return False

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
                return False

        # Now we consider ourselves connected for emitting.
        if not self._connected:
            logger.warning(
                "Socket connection state not ready; skipping emit for event: {}", event
            )
            return False
        try:

            await self.sio.emit(event, data, namespace=settings.socket_namespace)
            logger.debug(
                "Emitted {}{}",
                event,
                f" featureType={data.get('featureType')}" if isinstance(data, dict) and data.get("featureType") else "",
            )
            return True
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
            return False

    async def emit_ai_response(self, data: Dict[str, Any]) -> bool:
        """
        Emit AI response to the server using ai_response event.
        If socket is disconnected, logs and returns without raising so chat handler does not crash.
        After emit, briefly yields so the socket can flush the payload (reduces disconnect-before-delivery).

        Returns ``True`` only on a confirmed hand-off to the socket, so callers can buffer
        the payload for re-delivery instead of inferring delivery from the absence of an
        exception (the silent-skip paths below raise nothing).
        """
        if not self.sio:
            logger.warning(
                "result ai_response delivered=no — socket client not initialized (chat {})",
                data.get("chatId"),
            )
            return False

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
                    "result ai_response delivered=no — socket still not connected after {:.1f}s (chat {})",
                    wait_timeout,
                    data.get("chatId"),
                )
                return False

        if not self._connected:
            # Safety check
            return False
        try:
            await self.sio.emit(
                "ai_response", data, namespace=settings.socket_namespace
            )
            # The chat counterpart of the panel "result" line: one per reply.
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            logger.info(
                "result ai_response delivered=yes chars={} citations={} confidence={} price={}",
                len(str(data.get("content") or data.get("message") or "")),
                len(data.get("citations") or []),
                data.get("confidence") or "-",
                meta.get("price"),
            )
            await asyncio.sleep(0.25)
            return True
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
        - workspace_version_restore: Workspace restored to an earlier document version
        - document_reload_request: One document re-read on user request
        - user_message_request: User sent chat message
        """

        @self.sio.event
        async def connect():
            """Handle connection event."""
            self._refusal_hint_logged = False
            # Which handshake was accepted. During the migration this is the line
            # that proves the service is genuinely on v1 and not silently riding
            # the backend's AI_SIGNATURE_ALLOW_LEGACY window (rollout step 7).
            hs = self._handshake_summary()
            if hs and hs.get("scheme") != SCHEME_V1:
                logger.warning(
                    "Connected on the LEGACY handshake scheme (handshake #{}) - the "
                    "backend still has AI_SIGNATURE_ALLOW_LEGACY open. Move to "
                    "SOCKET_AUTH_SCHEME=v1 before that window closes.",
                    hs.get("attempt"),
                )
            else:
                logger.info(
                    "Socket connected (handshake #{}, scheme={}, {} event handlers)",
                    hs.get("attempt"),
                    hs.get("scheme"),
                    len(event_handler._handlers),
                )
            self._connected = True
            self._connected_event.set()
            await event_handler.handle("connect", None)

        @self.sio.event
        async def disconnect(reason=None):
            """Handle disconnection event with reason."""
            self._connected = False
            self._connected_event.clear()
            hints = {
                "transport close": "server closed the connection (check the server pingTimeout during long AI work)",
                "ping timeout": "ping timeout (the backend pingTimeout should allow long AI work, e.g. 60000)",
                "transport error": "network or proxy error",
                "server namespace disconnect": "server disconnected the namespace (check auth)",
            }
            logger.warning(
                "Socket disconnected: {}; reconnecting with a freshly signed handshake",
                hints.get(reason, reason or "no reason given"),
            )
            await event_handler.handle("disconnect", {"reason": reason})

        @self.sio.event
        async def connect_error(data):
            """
            Handle connection error.

            The backend disconnects on a failed handshake without a descriptive
            body, so the hints below mirror the failure table in
            docs/AI_SERVICE_AUTH_MIGRATION.md section 8. Ask the backend team
            for the refusal code in their logs; when reporting, include the UTC
            time, the x-signature-timestamp and x-signature-nonce sent (both are
            logged at DEBUG) - never the signature or the secret.
            """
            self._log_handshake_refusal(data)
            await event_handler.handle("error", data)

        @self.sio.event
        async def error(data):
            """Handle general error event from server."""
            await event_handler.handle("error", data)

        @self.sio.event
        async def unauthorized(data):
            """Handle unauthorized/authentication failure."""
            logger.error("Socket authentication failed - check SOCKET_SECRET_KEY")

        # ========================================================================
        # Backend-specified events (AI_Maverick listens to these)
        # ========================================================================

        @self.sio.event
        async def connected(data):
            """
            Handle 'connected' event from server.
            Confirms successful connection with userId and clientType.
            """
            payload = data if isinstance(data, dict) else {}
            hs = self._handshake_summary()
            logger.info(
                "Server confirmed connection | clientType={} userId={} scheme={} "
                "handshake=#{}",
                payload.get("clientType"),
                payload.get("userId"),
                hs.get("scheme"),
                hs.get("attempt"),
            )
            if payload.get("message"):
                logger.info("Server message: {}", payload.get("message"))
            if payload.get("clientType") and payload.get("clientType") != CLIENT_ID:
                logger.error(
                    "Backend identified this client as {} - expected {}. Check the "
                    "x-client-type header.",
                    payload.get("clientType"),
                    CLIENT_ID,
                )
            await event_handler.handle("connected", data)

        @self.sio.event
        async def workspace_created(data):
            """
            Handle workspace_created event (Server → AI_Maverick).
            New workspace created, emitted to ai_service room.
            """
            await event_handler.handle("workspace_created", data)

        @self.sio.event
        async def workspace_sync(data):
            """
            Handle workspace_sync event (Server → AI_Maverick).
            Workspace updated (title/status/files), emitted to ai_service room.
            """
            await event_handler.handle("workspace_sync", data)

        @self.sio.event
        async def workspace_version_restore(data):
            """
            Handle workspace_version_restore event (Server → AI_Maverick).
            User restored the workspace to an earlier document version:
            evidenceFiles carries the FULL surviving set at that version, so the
            AI reconciles the vector store to it and regenerates every panel.
            """
            await event_handler.handle("workspace_version_restore", data)

        @self.sio.event
        async def workspace_transferred(data):
            """
            Handle workspace_transferred event (Server → AI_Maverick).
            Workspace ownership moved to another lawyer; AI re-keys owner state
            and re-emits the features listed in reEmitFeatures against the new
            owner's template library (drafting_assistant).
            """
            await event_handler.handle("workspace_transferred", data)

        @self.sio.event
        async def document_reload_request(data):
            """
            Handle document_reload_request event (Server → AI_Maverick).
            The user (or the backend, on a poor-quality document) asked for ONE
            document to be read again: the AI purges everything it holds for
            that file, re-fetches the freshly-signed URL, and re-runs the
            workspace features over the full evidenceFiles set.
            """
            await event_handler.handle("document_reload_request", data)

        @self.sio.event
        async def workspace_stop_request(data):
            """
            Handle workspace_stop_request event (Server → AI_Maverick).
            The lawyer clicked Stop on a running update: cancel it, drop its
            buffered deliveries, disarm its recovery markers. Fire-and-forget —
            never replied to. Re-sends under the same requestId are expected.
            """
            await event_handler.handle("workspace_stop_request", data)

        @self.sio.event
        async def workspace_resume_request(data):
            """
            Handle workspace_resume_request event (Server → AI_Maverick).
            The lawyer clicked "Finish update": produce ONLY reEmitFeatures,
            announced by a workspace:refresh naming exactly that set.
            """
            await event_handler.handle("workspace_resume_request", data)

        @self.sio.event
        async def user_message_request(data):
            """
            Handle user_message_request event (Server → AI_Maverick).
            User message forwarded to AI service, emitted to ai_service room.
            """
            await event_handler.handle("user_message_request", data)

        @self.sio.event
        async def probing_prompts_request(data):
            """
            Handle probing_prompts_request (Server → AI_Maverick).
            For STRATEGY_OBJECTIVE chatType, respond with random 4 suggestion chips as prompts.
            """
            await event_handler.handle("probing_prompts_request", data)

        @self.sio.event
        async def analysis_report_feedback(data):
            """
            Handle analysis_report_feedback (App → Backend → AI_Maverick).
            User rejected an analysis report item (missingInfo or riskOrWeakness); recalc score and re-emit report.
            """
            await event_handler.handle("analysis_report_feedback", data)

        @self.sio.event
        async def added_missing_information(data):
            """
            Handle added_missing_information (App → Backend → AI_Maverick).
            User manually supplied a title+description to address previously-flagged
            missingInfo. AI assesses case relevance; if related, the analysis score
            is decreased and qualityScoreExplanation is regenerated, then re-emitted.
            """
            await event_handler.handle("added_missing_information", data)

        @self.sio.event
        async def applicable_law_relevance_request(data):
            """
            Handle applicable_law_relevance_request (Server → AI_Maverick).
            Lawyer-selected law; AI returns applicability as applicable_law_relevance_response.
            """
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
            await event_handler.handle("add_case_law", data)

        @self.sio.event
        async def case_law_document_request(data):
            """
            Handle case_law_document_request (Server → AI_Maverick).
            A lawyer uploaded a document (stored in GCS). AI fetches documentUrl,
            decides whether it is a case law, and replies with
            case_law_document_response echoing requestId (accepted + caseLaw, or
            rejected + rejectionReason). Accepted cases also update the Case Laws
            panel via document_feature_response (featureType=case_laws).
            """
            await event_handler.handle("case_law_document_request", data)

        @self.sio.event
        async def add_relevant_party(data):
            """Handle add_relevant_party (App → Backend → AI). User adds a party."""
            await event_handler.handle("add_relevant_party", data)

        @self.sio.event
        async def edit_relevant_party(data):
            """Handle edit_relevant_party (App → Backend → AI). Edit a party by id."""
            await event_handler.handle("edit_relevant_party", data)

        @self.sio.event
        async def delete_relevant_party(data):
            """Handle delete_relevant_party (App → Backend → AI). Delete a party by id."""
            await event_handler.handle("delete_relevant_party", data)

        @self.sio.event
        async def analysis_docx_generate_request(data):
            """
            Handle analysis_docx_generate_request (Server -> AI_Maverick).
            AI returns processed document text via analysis_docx_generate_response.
            """
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
            await event_handler.handle("template_sync", data)

        @self.sio.event
        async def template_sync_response(data):
            """
            Server → AI_Maverick: full snapshot of a user's active templates
            in reply to our request_template_sync emit. Populates the
            in-memory template store on demand (e.g. after an AI restart).
            """
            await event_handler.handle("template_sync_response", data)

        @self.sio.event
        async def request_blueprint_proposal(data):
            """
            Server → AI: the "Regenerate Extra Info" action on the dashboard's
            blueprint-approval screen. The AI re-profiles the template and emits
            a fresh template_blueprint_proposal. Without this explicit binding
            the event falls through the catch-all and is silently dropped.
            """
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
            await event_handler.handle("workspace_updated", data)

        @self.sio.event
        async def workspace_deleted(data):
            """
            Handle workspace_deleted event (Server → AI_Maverick).

            The backend's erasure cascade does not reach this service, so this
            event is the instruction to erase the AI-held copy of the case. It
            is logged at INFO rather than DEBUG: a compliance deletion should
            leave a trace at the default log level.

            Re-sends carrying the same requestId are EXPECTED — the backend
            emit is fire-and-forget, so it re-sends every pending deletion on
            reconnect until purgeAfter passes. The handler dedupes.
            """
            await event_handler.handle("workspace_deleted", data)

        @self.sio.on("*")
        async def catch_all(event, *args):
            """Catch all other events from server. Event names only: the
            arguments are server payloads and are never logged."""
            if event in ("auth_error", "authentication_error"):
                logger.error("Authentication error event from server: {}", event)
            elif event == "error":
                logger.error("Error event from server")
            else:
                logger.debug("Unhandled server event: {} ({} arg(s))", event, len(args))

    @property
    def is_connected(self) -> bool:
        """Check if socket is connected."""
        return self._connected
