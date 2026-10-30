"""Drafting template library sync and blueprint proposals.

Feature mixin extracted from events.py — methods moved verbatim.
"""

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.utils.redact import chars, validation_fields

from app.socket.schemas import (
    TemplateSyncPayload,
    TemplateSyncResponsePayload,
)



#: The actions TemplateSyncPayload accepts; anything else is logged as its size.
_KNOWN_ACTIONS = frozenset(
    {"uploaded", "deleted", "renamed", "preset_created", "formatting_updated"}
)


def _safe_action(value: Any) -> str:
    """An inbound ``action`` for a log line: the value if known, else its size."""
    return value if value in _KNOWN_ACTIONS else chars(value)


def _safe_count(value: Any) -> Any:
    """An inbound ``count`` for a log line: an int as-is, anything else its size."""
    return value if isinstance(value, int) else chars(value)


class TemplateSyncMixin:
    """Drafting template library sync and blueprint proposals."""

    async def on_template_sync(self, data: Dict[str, Any]) -> None:
        """
        Handle template_sync event (Server → AI_Maverick).

        Payload:
        {
            action: "uploaded" | "renamed" | "deleted",
            userId: str,
            templates: [ {id, name, previousName?, category, extension,
                          mimeType?, size?, fileHash?, fileUrl?, path?} ],
            timestamp: str (ISO)
        }

        For "uploaded" the AI downloads each template, extracts its text via
        `file_handlers.process_file_by_extension`, and stores the result in a
        lawyer-scoped Qdrant cache keyed by (userId, template_id). "renamed"
        only patches cached metadata (content unchanged) — keyed on
        template_id, so no stale name-keyed entry is left behind. "deleted"
        removes the cache entry (supports bulk: multiple templates per event).
        """
        payload_in = data if isinstance(data, dict) else {}
        # Log the RAW inbound event exactly as the backend pushed it
        # (top-level keys + action + template count) before validation,
        # so a malformed/unexpected payload is still visible in the logs.
        logger.info(
            "template_sync received: action={} userId={} templates={} keys={}",
            _safe_action((payload_in or {}).get("action")),
            str((payload_in or {}).get("userId") or "")[:12],
            len((payload_in or {}).get("templates") or [])
            if isinstance((payload_in or {}).get("templates"), list)
            else "(not a list)",
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncPayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync: invalid payload (action={}, keys={}): {}",
                _safe_action((payload_in or {}).get("action")),
                list((payload_in or {}).keys()),
                validation_fields(e),
            )
            return

        action = (payload.action or "").strip().lower()
        user_id = (payload.userId or "").strip()
        if not user_id:
            logger.warning("template_sync: missing userId — dropping event")
            return

        templates = list(payload.templates or [])
        logger.info(
            "template_sync action={} user={} count={}",
            action,
            user_id[:12],
            len(templates),
        )

        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("template_sync: drafting agent unavailable: {}", e)
            return

        if action == "uploaded":
            await self._ingest_template_items(user_id, templates)
            # A newly-ingested template may match suggestions that were built
            # before it was ready — regenerate the panel if the library grew.
            await self._maybe_reemit_drafting_on_templates(user_id)
            # Emit the AI's blueprint proposal for the dashboard approval screen,
            # for each freshly-uploaded file-template that did NOT already carry
            # an approved blueprint (scenario B re-uploads skip this).
            await self._emit_blueprint_proposals(user_id, templates)
        elif action == "formatting_updated":
            # The lawyer approved / edited a template's AI blueprint in the
            # dashboard. File content is unchanged — store the approved
            # blueprint as authoritative (no re-download / re-extract).
            for tpl in templates:
                if not tpl.id:
                    continue
                blueprint = None
                if getattr(tpl, "formatting", None) is not None and tpl.formatting.blueprint is not None:
                    blueprint = tpl.formatting.blueprint.model_dump()
                if blueprint is None:
                    logger.info(
                        "formatting_updated: no blueprint on item {} — skipping",
                        tpl.id,
                    )
                    continue
                await drafting_agent.apply_approved_blueprint(
                    user_id=user_id, template_id=tpl.id, blueprint=blueprint
                )
        elif action == "preset_created":
            # A court / letterhead preset (no file body) — cache its layout for
            # use at DOCX export.
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.store_preset(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Custom",
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
        elif action == "renamed":
            for tpl in templates:
                if not tpl.id or not tpl.name:
                    continue
                await drafting_agent.rename_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    new_name=tpl.name,
                    previous_name=tpl.previousName,
                )
        elif action == "deleted":
            for tpl in templates:
                if not tpl.id:
                    continue
                await drafting_agent.delete_template(
                    user_id=user_id, template_id=tpl.id
                )
            # Self-heal the drafting panel: demote any suggestion card that was
            # matched to a now-deleted template and re-match it against the
            # current library, so a deleted template can never drive a kickoff.
            await self._maybe_reemit_drafting_on_templates(user_id)
        else:
            logger.warning("template_sync: unknown action {!r}", action)

    async def _emit_blueprint_proposals(
        self, user_id: str, templates: List[Any]
    ) -> None:
        """Emit a ``template_blueprint_proposal`` for each freshly-uploaded
        file-template that did NOT already carry an approved blueprint.

        The AI profiles the template at ingest; here we package that into the
        proposal the dashboard renders on its approval screen. Per-item failures
        are isolated and non-fatal."""
        if not self._socket_client:
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_emit_blueprint_proposals: drafting agent unavailable: {}", e)
            return
        # Profiling calls Gemini; user-scoped like template ingest, so priced and
        # logged rather than billed to a workspace (see _ingest_template_items).
        from app.ai.usage import capture_usage, log_unbilled_usage

        with capture_usage() as proposal_usage:
            try:
                await self._emit_blueprint_proposals_inner(user_id, templates, drafting_agent)
            finally:
                log_unbilled_usage("blueprint proposals", proposal_usage)

    async def _emit_blueprint_proposals_inner(
        self, user_id: str, templates: List[Any], drafting_agent: Any
    ) -> None:
        """Body of :meth:`_emit_blueprint_proposals` (see its docstring)."""
        for tpl in templates:
            # Skip presets (no draft body) and re-uploads that already shipped an
            # approved blueprint (those are stored authoritative at ingest).
            if getattr(tpl, "kind", None) == "preset":
                continue
            if (
                getattr(tpl, "formatting", None) is not None
                and tpl.formatting.blueprint is not None
            ):
                continue
            if not tpl.id:
                continue
            try:
                proposal = await drafting_agent.build_blueprint_proposal(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal build failed (template={}): {}", tpl.id, e
                )
                continue
            if not proposal:
                continue
            try:
                await self._socket_client.emit(
                    "template_blueprint_proposal", proposal
                )
                logger.success(
                    "template_blueprint_proposal emitted (template={}, status={}, vars={})",
                    tpl.id,
                    proposal.get("status"),
                    len((proposal.get("blueprint") or {}).get("variables") or []),
                )
            except Exception as e:
                logger.warning(
                    "blueprint proposal emit failed (template={}): {}", tpl.id, e
                )

    async def _ingest_template_items(
        self, user_id: str, templates: List[Any]
    ) -> int:
        """Download + extract + cache each template into the in-memory store.

        Shared by the ``template_sync`` ``uploaded`` action and the
        ``template_sync_response`` snapshot. Returns the count ingested.
        Per-item failures are isolated and non-fatal. Re-uses any existing
        cached payload so an unchanged file hash skips re-extraction.

        Template ingestion is paid work (Vision OCR on scanned templates, Gemini
        profiling), but it is user-scoped — templates belong to a library, not a
        workspace — so a scope is opened here purely to price and log the spend
        via ``log_unbilled_usage``. Wrapping the shared helper covers both
        callers at once.
        """
        from app.ai.usage import capture_usage, log_unbilled_usage

        with capture_usage() as tpl_usage:
            try:
                return await self._ingest_template_items_inner(user_id, templates)
            finally:
                log_unbilled_usage("template ingest", tpl_usage)

    async def _ingest_template_items_inner(
        self, user_id: str, templates: List[Any]
    ) -> int:
        """Body of :meth:`_ingest_template_items` (see its docstring)."""
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("_ingest_template_items: drafting agent unavailable: {}", e)
            return 0

        ingested = 0
        for tpl in templates:
            if not tpl.id or not tpl.fileUrl:
                logger.warning(
                    "template ingest: skipping item with missing id/fileUrl ({!r})",
                    tpl.id,
                )
                continue
            try:
                previous = await drafting_agent.get_template(
                    user_id=user_id, template_id=tpl.id
                )
            except Exception:
                previous = None
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=tpl.id,
                    name=tpl.name or tpl.id,
                    category=tpl.category or "Document Draft",
                    extension=tpl.extension or "",
                    mime_type=tpl.mimeType,
                    file_url=tpl.fileUrl,
                    path=tpl.path,
                    file_hash=tpl.fileHash,
                    previous_payload=previous if isinstance(previous, dict) else None,
                    kind=getattr(tpl, "kind", None),
                    formatting=(
                        tpl.formatting.model_dump()
                        if getattr(tpl, "formatting", None) is not None
                        else None
                    ),
                )
                ingested += 1
            except Exception as e:
                logger.warning(
                    "template ingest failed (template={}): {}", tpl.id, e
                )
        return ingested

    async def on_template_sync_response(self, data: Dict[str, Any]) -> None:
        """Handle template_sync_response (Server → AI), the reply to our
        ``request_template_sync``. Ingests the full snapshot into the
        in-memory store and resolves the awaiting Future (by requestId)."""
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "template_sync_response received: userId={} requestId={} count={} keys={}",
            str((payload_in or {}).get("userId") or "")[:12],
            (payload_in or {}).get("requestId"),
            _safe_count((payload_in or {}).get("count")),
            list((payload_in or {}).keys()),
        )
        try:
            payload = TemplateSyncResponsePayload(**payload_in)
        except Exception as e:
            logger.warning(
                "template_sync_response: invalid payload (keys={}): {}",
                list((payload_in or {}).keys()),
                validation_fields(e),
            )
            # Still a reply to our request: release its waiter, which would
            # otherwise sit out the whole bound (it used to wait forever).
            raw_request_id = (payload_in or {}).get("requestId")
            fut = (
                self._pending_template_sync.get(raw_request_id)
                if isinstance(raw_request_id, str) and raw_request_id
                else None
            )
            if fut is not None and not fut.done():
                fut.set_result(False)
            return

        user_id = (payload.userId or "").strip()
        request_id = payload.requestId
        templates = list(payload.templates or [])


        try:
            if not user_id:
                logger.warning(
                    "template_sync_response: missing userId — dropping event"
                )
            else:
                ingested = await self._ingest_template_items(user_id, templates)
                logger.success(
                    "template_sync_response ingested {}/{} template(s) for user={}",
                    ingested,
                    len(templates),
                    user_id[:12],
                )
                # A requested snapshot is the user's FULL library — reconcile
                # deletions so cached templates the backend no longer reports
                # (e.g. deleted while a `deleted` delta event was missed) are
                # dropped. Ingest merges only, so without this a forced sync
                # would leave the store a stale superset. Only replies to our
                # own request_template_sync (requestId set) are authoritative.
                if request_id:
                    try:
                        from app.langgraph.template_store import template_store

                        snapshot_ids = {
                            str(tpl.id) for tpl in templates if tpl.id
                        }
                        stale_ids = [
                            str(cached.get("template_id"))
                            for cached in await template_store.list_for_user(
                                user_id=user_id
                            )
                            if cached.get("template_id")
                            and str(cached.get("template_id")) not in snapshot_ids
                        ]
                        for stale_id in stale_ids:
                            await template_store.delete(
                                user_id=user_id, template_id=stale_id
                            )
                        if stale_ids:
                            logger.info(
                                "template_sync_response reconciled {} stale "
                                "template(s) for user={} (absent from snapshot)",
                                len(stale_ids),
                                user_id[:12],
                            )
                    except Exception as e:
                        logger.warning(
                            "template_sync_response: stale-template reconcile "
                            "failed (user={}): {}",
                            user_id[:12],
                            e,
                        )
        finally:
            # Always resolve the awaiting future — even if ingestion raised — so
            # ensure_templates_synced does not sit out its whole bound on a
            # failed ingest.
            fut = (
                self._pending_template_sync.get(request_id) if request_id else None
            )
            if fut is not None and not fut.done():
                fut.set_result(True)

        # If this snapshot arrived late (after the ensure_templates_synced
        # timeout) and grew the library, regenerate any drafting panels that
        # were built against the smaller/empty library.
        if user_id:
            await self._maybe_reemit_drafting_on_templates(user_id)

    async def on_request_blueprint_proposal(self, data: Dict[str, Any]) -> None:
        """Handle request_blueprint_proposal (Server → AI), the "Regenerate
        Extra Info" action on the dashboard's blueprint-approval screen.

        Re-profiles the template (forcing a fresh profile even if one exists)
        and emits a fresh ``template_blueprint_proposal`` back. Ingests the
        template from ``fileUrl`` first when the in-memory store is cold.

        Wrapped in a usage scope: the cold ingest OCRs the template and the
        proposal build calls Gemini. This event is user-scoped (no workspaceId in
        the payload and none in the reply), so the spend cannot be added to a
        workspace cumulative — it is priced and logged instead, which also keeps
        the "consumed outside any capture_usage scope" alarm meaningful."""
        from app.ai.usage import capture_usage, log_unbilled_usage

        with capture_usage() as tpl_usage:
            try:
                await self._handle_request_blueprint_proposal(data)
            finally:
                log_unbilled_usage("request_blueprint_proposal", tpl_usage)

    async def _handle_request_blueprint_proposal(self, data: Dict[str, Any]) -> None:
        """Body of :meth:`on_request_blueprint_proposal` (see its docstring)."""
        payload_in = data if isinstance(data, dict) else {}
        user_id = str(payload_in.get("userId") or "").strip()
        template_id = str(payload_in.get("templateId") or "").strip()
        file_url = payload_in.get("fileUrl") or ""
        file_hash = payload_in.get("fileHash") or ""
        logger.info(
            "request_blueprint_proposal RECEIVED: userId={} templateId={} hasFileUrl={}",
            user_id[:12],
            template_id,
            bool(file_url),
        )
        if not user_id or not template_id:
            logger.warning(
                "request_blueprint_proposal: missing userId/templateId — dropping event"
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error(
                "request_blueprint_proposal: drafting agent unavailable: {}", e
            )
            return

        # Ensure the template is in the store with extracted text. On a cold
        # cache (AI restart) the template may be absent — ingest it from the
        # supplied fileUrl, deriving name/extension from the object path.
        try:
            tpl = await drafting_agent.get_template(
                user_id=user_id, template_id=template_id
            )
        except Exception:
            tpl = None
        if (not tpl or not (tpl.get("extracted_text") or "").strip()) and file_url:
            url_path = file_url.split("?", 1)[0]
            fname = url_path.rsplit("/", 1)[-1] if url_path else ""
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            try:
                await drafting_agent.ingest_template(
                    user_id=user_id,
                    template_id=template_id,
                    name=fname or template_id,
                    category=(tpl.get("category") if isinstance(tpl, dict) else None)
                    or "Document Draft",
                    extension=ext,
                    mime_type=None,
                    file_url=file_url,
                    path=None,
                    file_hash=file_hash,
                    previous_payload=tpl if isinstance(tpl, dict) else None,
                )
            except Exception as e:
                logger.warning(
                    "request_blueprint_proposal: cold ingest failed (template={}): {}",
                    template_id,
                    e,
                )

        try:
            proposal = await drafting_agent.build_blueprint_proposal(
                user_id=user_id, template_id=template_id, force_reprofile=True
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: proposal build failed (template={}): {}",
                template_id,
                e,
            )
            proposal = None
        if not proposal:
            logger.warning(
                "request_blueprint_proposal: no proposal produced (template={})",
                template_id,
            )
            return
        if not self._socket_client:
            logger.warning(
                "request_blueprint_proposal: no socket client — cannot emit proposal"
            )
            return
        try:
            await self._socket_client.emit(
                "template_blueprint_proposal", proposal
            )
            logger.success(
                "template_blueprint_proposal (regenerated) emitted "
                "(template={}, status={}, vars={})",
                template_id,
                proposal.get("status"),
                len((proposal.get("blueprint") or {}).get("variables") or []),
            )
        except Exception as e:
            logger.warning(
                "request_blueprint_proposal: emit failed (template={}): {}",
                template_id,
                e,
            )

    async def ensure_templates_synced(
        self, user_id: str, *, timeout: Optional[float] = None, force: bool = False
    ) -> None:
        """Make sure the in-memory template store is warm for ``user_id``.

        If the store already has templates for the user (delta events keep it
        fresh within a session), this is a no-op. Otherwise it emits
        ``request_template_sync`` and awaits the ``template_sync_response``
        snapshot (resolved in :meth:`on_template_sync_response` only AFTER the
        templates finish ingesting), so a cold cache (e.g. right after an AI
        restart) is fully filled before template matching runs.

        ``force=True`` skips the warm-cache short-circuit and always requests a
        fresh snapshot (used by workspace_transferred so the new owner's
        library is guaranteed current before suggestions are re-matched). The
        snapshot reply also reconciles deletions, so a forced sync makes the
        store authoritative, not just a superset.

        The wait is always bounded: ``timeout=None`` means
        ``settings.template_sync_timeout_seconds``. It used to be unbounded on
        the belief that the reply always comes; a dropped emit or a backend that
        never answers then held the workspace pipeline lock forever and every
        later panel of the case span. An undelivered request returns at once.
        On expiry the caller proceeds with the current store, and a snapshot
        that arrives later still refreshes the Drafting Assistant
        (``_maybe_reemit_drafting_on_templates``).
        """
        if not user_id or not self._socket_client:
            return
        if not force:
            try:
                from app.langgraph.template_store import template_store

                if await template_store.count_for_user(user_id=user_id) > 0:
                    return  # already warm this session
            except Exception:
                pass

        bound = (
            float(timeout)
            if timeout is not None
            else float(settings.template_sync_timeout_seconds)
        )
        request_id = f"tmpl-{(user_id or '')[:8]}-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future" = loop.create_future()
        self._pending_template_sync[request_id] = fut
        try:
            delivered = await self._socket_client.emit(
                "request_template_sync",
                {"userId": user_id, "requestId": request_id},
            )
            if not delivered:
                # SocketClient.emit returns False for a dropped emit: no reply
                # can come, so waiting would only hold the caller up.
                logger.warning(
                    "request_template_sync not delivered (user={}, requestId={}) — "
                    "proceeding with current store",
                    user_id[:12],
                    request_id,
                )
                return
            logger.info(
                "request_template_sync emitted (user={}, requestId={}, bound={}s)",
                user_id[:12],
                request_id,
                round(bound),
            )
            await asyncio.wait_for(fut, timeout=bound)
            logger.info(
                "template snapshot received for user={} (requestId={})",
                user_id[:12],
                request_id,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "request_template_sync timed out after {}s (user={}) — "
                "proceeding with current store",
                round(bound),
                user_id[:12],
            )
        except Exception as e:
            logger.warning(
                "request_template_sync failed (user={}): {}", user_id[:12], e
            )
        finally:
            self._pending_template_sync.pop(request_id, None)

    def _prewarm_templates(
        self, user_id: str, *, timeout: Optional[float] = None
    ) -> Optional["asyncio.Task"]:
        """Start the bounded template sync as a tracked task.

        Pipeline runners call this BEFORE taking the workspace pipeline lock, so
        the backend round trip never runs while the lock is held; drafting then
        waits on the task only briefly (``templates_ready``). ``timeout`` is the
        run's audio-extended cap, when it has one. Returns ``None`` when there
        is nothing to warm.
        """
        if not user_id or not self._socket_client:
            return None
        try:
            return self._track_background_task(
                asyncio.create_task(
                    self.ensure_templates_synced(user_id, timeout=timeout)
                ),
                label="template prewarm",
            )
        except RuntimeError as rt_err:
            logger.warning("Could not start template prewarm: {}", rt_err)
            return None
