"""Word file from existing draft text (`draft_file_build_request`, SIM-216).

The backend asks for a .docx built from a draft it already stores: the
lawyer's "Rebuild Word file" (the copy failed, or ``docxStatus`` was
``failed``), and the first Word file of a draft saved before the AI produced
one. The answer, ``draft_file_build_response``, is ALWAYS sent, success or
failure, whenever the request carries a readable ``requestId``.

How it works
------------
- ``format: "markdown"`` runs the SIM-202 normaliser; ``format: "html"`` (the
  in-app editor) is first converted to the same Markdown structure
  (``app.processing.draft_html``). Both then go through the drafting agent's
  own renderer (``DraftingAgent.build_docx_from_text``): the template's
  formatting when ``templateId`` is given and held, else the SIM-208 standard
  layout inferred from the draft's first heading (the contract carries no
  title). The request's ``formatLayout`` (SIM-217) is then applied exactly as
  on a drafting turn; a letterhead image that cannot be fetched is skipped.
  The file is uploaded through ``app.storage.draft_files.upload_draft_docx``
  (``drafts/<workspaceId>/``, short-lived link), so the workspace purge
  reaches it.
- The answer is ``status: ok`` with ``downloadUrl``, or ``status: error``
  with a short ``errorCode`` (never text or a URL).
- The work runs as a tracked background task, inside the event's logging
  context, so any model cost (template route only) is billed to the
  workspace. It is bounded by ``DRAFT_DOCX_BUILD_TIMEOUT_SECONDS``.
- Idempotent on ``requestId``: a re-send while the build is in flight is
  swallowed; a re-send within ``DOCX_BUILD_REPLAY_SECONDS`` of the answer gets
  the same answer again (the backend re-sends after a reconnect when it never
  saw the first one). The ledger is keyed by workspace id, so the purge sweep
  erases it, cached links included.

Purge and stop
--------------
- A request for a workspace that is purged, pending purge, or being purged is
  refused as ``invalid_request``. It does NOT call ``cancel_pending_purge``:
  a rebuild is the backend serving a stored draft, not the lawyer working on
  the case, and must never be what keeps a deleted case's data alive.
- A build that finishes after the purge started deletes the workspace's draft
  prefix again and answers ``invalid_request``, so no file outlives the purge.
- The event runs unscoped (``_UNSCOPED_EVENTS`` in ``run_control``): a
  ``workspace_stop_request`` stops a running UPDATE and must not eat the
  backend's answer here, as it never eats a chat answer.
"""

import asyncio
import re
import time
from typing import Any, Dict, Optional

from loguru import logger

from app.ai.usage import capture_usage
from app.core.config import settings
from app.processing.draft_format import select_format_layout
from app.processing.draft_html import html_to_markdown
from app.processing.draft_markdown import count_placeholders, normalize_if_enabled
from app.socket.backend_events import DRAFT_DOCX_BUILD_RESPONSE
from app.socket.schemas import (
    DraftDocxBuildRequestPayload,
    DraftDocxBuildResponsePayload,
)
from app.utils.redact import chars, validation_fields
from app.workspace.persistence import is_workspace_persistence_blocked

#: How long a completed build's answer is replayed to a re-send of its
#: requestId (contract: 10 minutes).
DOCX_BUILD_REPLAY_SECONDS = 600.0

#: Slack on top of the build timeout before an ``in_flight`` entry stops
#: swallowing re-sends. Only a hung task can outlive the timeout; this keeps
#: such a request from being swallowed forever.
_INFLIGHT_SLACK_SECONDS = 60.0


def _build_timeout_seconds() -> float:
    return float(getattr(settings, "draft_docx_build_timeout_seconds", 120) or 120)


def _id(value: Any) -> Optional[str]:
    """An echo-safe id: a short string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return None


#: ``purpose`` values the backend sends; anything else is logged as ``other``.
_PURPOSES = frozenset({"rebuild", "first_file"})

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _draft_title(title: Optional[str], markdown: str) -> str:
    """The title the standard layout is inferred from (SIM-208).

    The backend's contract carries no title, so it is read from the draft
    itself: its first heading, else its first few non-empty lines (the title
    block of a court filing or notice). Never logged.
    """
    if title and title.strip():
        return title.strip()
    lines = [ln.strip() for ln in (markdown or "").splitlines() if ln.strip()]
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            return m.group(1).replace("**", "").strip()[:120]
    head = " ".join(ln.lstrip("#").replace("**", "").strip() for ln in lines[:5])
    return head[:200] or "Draft"


def _prepare_markdown(content: str, fmt: str) -> str:
    """Draft text → normalised Markdown (blocking; run in a thread)."""
    markdown = html_to_markdown(content) if fmt == "html" else content
    return normalize_if_enabled(markdown or "")


class DraftDocxBuildMixin:
    """Handles ``draft_file_build_request``; emits ``draft_file_build_response``."""

    # ------------------------------------------------------------------ ledger

    def _docx_build_ledger(self, workspace_id: str) -> Dict[str, Dict[str, Any]]:
        """``requestId -> {status, ts, response}`` for one workspace.

        Top-level key is the workspace id so the purge sweep erases it.
        """
        return self._draft_docx_build_requests.setdefault(workspace_id, {})

    def _docx_build_prune(self, workspace_id: str) -> None:
        ledger = self._draft_docx_build_requests.get(workspace_id)
        if not ledger:
            return
        horizon = 2 * max(
            DOCX_BUILD_REPLAY_SECONDS, _build_timeout_seconds() + _INFLIGHT_SLACK_SECONDS
        )
        now = time.time()
        for rid in [r for r, e in ledger.items() if now - float(e.get("ts") or 0) > horizon]:
            ledger.pop(rid, None)

    def _docx_build_refused(self, workspace_id: str) -> bool:
        """True for a workspace that is purged, pending purge or being purged."""
        return bool(
            self.is_workspace_purged(workspace_id)
            or workspace_id in self._pending_purges
            or is_workspace_persistence_blocked(workspace_id)
        )

    # --------------------------------------------------------------- responses

    @staticmethod
    def _docx_build_response(
        echo: Dict[str, Any],
        *,
        reason: Optional[str] = None,
        url: Optional[str] = None,
        placeholders: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ok = bool(url) and reason is None
        body = DraftDocxBuildResponsePayload(
            **echo,
            status="ok" if ok else "error",
            errorCode=None if ok else (reason or "render_failed"),
            downloadUrl=url if ok else None,
            placeholderCount=placeholders,
            metadata=metadata or None,
        )
        return body.model_dump(exclude_none=True)

    async def _emit_docx_build_response(self, payload: Dict[str, Any]) -> bool:
        client = self._socket_client
        if client is None:
            logger.warning("draft_file_build_response not sent: no socket client")
            return False
        return bool(await client.emit(DRAFT_DOCX_BUILD_RESPONSE, payload))

    # ----------------------------------------------------------------- handler

    async def on_draft_docx_build_request(self, data: Dict[str, Any]) -> None:
        """Handle draft_file_build_request (Server -> AI_Maverick).

        Validates, de-dupes on ``requestId``, refuses purged workspaces, then
        schedules the build. Every exit that has a ``requestId`` answers.
        """
        raw = data if isinstance(data, dict) else {}
        echo: Dict[str, Any] = {
            "requestId": _id(raw.get("requestId")),
            "workspaceId": _id(raw.get("workspaceId")),
            "draftId": _id(raw.get("draftId")),
        }
        try:
            req = DraftDocxBuildRequestPayload(**raw)
        except Exception as exc:
            logger.warning(
                "draft_file_build status=error error=invalid_request fields={}",
                validation_fields(exc),
            )
            if echo["requestId"]:
                await self._emit_docx_build_response(
                    self._docx_build_response(echo, reason="invalid_request")
                )
            else:
                logger.warning("draft_file_build_request has no requestId; cannot answer")
            return

        echo = {
            "requestId": req.requestId,
            "workspaceId": req.workspaceId,
            "draftId": req.draftId,
        }
        workspace_id = req.workspaceId
        request_id = req.requestId

        entry = (self._draft_docx_build_requests.get(workspace_id) or {}).get(request_id)
        if entry:
            age = time.time() - float(entry.get("ts") or 0.0)
            status = entry.get("status")
            if status == "in_flight" and age < (
                _build_timeout_seconds() + _INFLIGHT_SLACK_SECONDS
            ):
                logger.info(
                    "draft_file_build re-send swallowed (in flight {:.0f}s)", age
                )
                return
            if status == "done" and age < DOCX_BUILD_REPLAY_SECONDS:
                delivered = await self._emit_docx_build_response(entry["response"])
                logger.info(
                    "draft_file_build re-send answered from cache (age {:.0f}s) "
                    "status={} delivered={}",
                    age,
                    entry["response"].get("status"),
                    "yes" if delivered else "no",
                )
                return

        if self._docx_build_refused(workspace_id):
            logger.warning(
                "draft_file_build status=error error=invalid_request "
                "(workspace purged or pending purge)"
            )
            await self._emit_docx_build_response(
                self._docx_build_response(echo, reason="invalid_request")
            )
            return

        self._docx_build_prune(workspace_id)
        self._docx_build_ledger(workspace_id)[request_id] = {
            "status": "in_flight",
            "ts": time.time(),
        }
        self._track_background_task(
            asyncio.create_task(self._run_draft_docx_build(req, echo)),
            label="draft_docx_build",
        )

    async def _run_draft_docx_build(
        self, req: DraftDocxBuildRequestPayload, echo: Dict[str, Any]
    ) -> None:
        """Build, upload, answer, log one summary line."""
        from app.langgraph.drafting_agent import drafting_agent

        workspace_id = req.workspaceId
        request_id = req.requestId
        started = time.monotonic()
        reason: Optional[str] = None
        url: Optional[str] = None
        placeholders: Optional[int] = None
        route = "none"
        template_state = "requested" if req.templateId else "none"
        metadata: Optional[Dict[str, Any]] = None
        model_called = False
        layout = select_format_layout(req.formatLayout)
        purpose = req.purpose if req.purpose in _PURPOSES else "other"

        with capture_usage() as usage:
            try:
                async with asyncio.timeout(_build_timeout_seconds()):
                    markdown = await asyncio.to_thread(
                        _prepare_markdown, req.content, req.format
                    )
                    if not markdown.strip():
                        reason = "empty_content"
                    else:
                        placeholders = count_placeholders(markdown)
                        role = None
                        if req.templateId:
                            role = await self._resolve_workspace_role(
                                workspace_id, req.userId
                            )
                        result = await drafting_agent.build_docx_from_text(
                            markdown=markdown,
                            title=_draft_title(req.title, markdown),
                            user_id=req.userId,
                            workspace_id=workspace_id,
                            template_id=req.templateId,
                            template_version=req.templateVersion,
                            template_scope=req.templateScope,
                            role=role,
                            format_layout=layout,
                        )
                        route, template_state = result.route, result.template
                        url, reason = result.url, result.reason
            except asyncio.CancelledError:
                # Shutdown (the event is unscoped, so a stop never lands here).
                # Forget the request so the backend's re-send runs it again.
                logger.warning(
                    "draft_file_build cancelled format={} route={}", req.format, route
                )
                ledger = self._draft_docx_build_requests.get(workspace_id)
                if ledger is not None:
                    ledger.pop(request_id, None)
                raise
            except TimeoutError:
                reason, url = "timeout", None
            except Exception as exc:
                logger.warning("draft_file_build failed ({})", type(exc).__name__)
                reason, url = "render_failed", None
            model_called = bool(
                getattr(usage, "input_tokens", 0) or getattr(usage, "output_tokens", 0)
            )
            if model_called:
                metadata = self._ingest_usage_metadata(workspace_id, usage)

        refused = self._docx_build_refused(workspace_id)
        if refused:
            # The purge started while this ran: the file must not outlive it.
            if url:
                from app.storage.draft_files import delete_workspace_draft_files

                try:
                    await asyncio.to_thread(delete_workspace_draft_files, workspace_id)
                except Exception as exc:
                    logger.opt(exception=exc).critical(
                        "draft_file_build: draft file uploaded during a purge could "
                        "not be deleted; the purge retry must clear it"
                    )
            reason, url = "invalid_request", None

        payload = self._docx_build_response(
            echo, reason=reason, url=url, placeholders=placeholders, metadata=metadata
        )
        if refused:
            ledger = self._draft_docx_build_requests.get(workspace_id)
            if ledger is not None:
                ledger.pop(request_id, None)
        else:
            self._docx_build_ledger(workspace_id)[request_id] = {
                "status": "done",
                "ts": time.time(),
                "response": payload,
            }
        delivered = await self._emit_docx_build_response(payload)
        logger.info(
            "draft_file_build status={} error={} purpose={} format={} layout={} "
            "route={} template={} chars={} placeholders={} model_called={} "
            "delivered={} duration={:.1f}s",
            payload["status"],
            payload.get("errorCode", "-"),
            purpose,
            req.format,
            "yes" if layout else "no",
            route,
            template_state,
            chars(req.content),
            placeholders if placeholders is not None else "-",
            "yes" if model_called else "no",
            "yes" if delivered else "no",
            time.monotonic() - started,
        )
