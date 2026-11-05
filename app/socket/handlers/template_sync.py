"""Drafting template library: the Global Template Library feed and blueprint proposals.

One platform-wide, versioned template library (SIM-58 / SIM-173) that admins
publish and every lawyer uses. The per-lawyer ``template_sync`` /
``template_sync_response`` / ``request_template_sync`` events are no longer
handled: every template now comes from ``global_template_sync`` and the
connect-time ``global_template_sync_response`` snapshot.
"""

import asyncio
import uuid
from typing import Any, Dict, Optional

from loguru import logger

from app.core.config import settings
from app.utils.redact import chars, validation_fields

from app.langgraph.template_store import (
    GLOBAL_DRAFT_OWNER,
    GLOBAL_OWNER,
    GLOBAL_SCOPE,
    STATUS_PUBLISHED,
    STATUS_RETIRED,
    template_store,
)
from app.socket.schemas import (
    GlobalTemplateItem,
    GlobalTemplateSyncPayload,
    GlobalTemplateSyncResponsePayload,
)


#: The actions GlobalTemplateSyncPayload accepts.
_KNOWN_GLOBAL_ACTIONS = frozenset({"published", "retired", "deleted"})

#: requestId prefix of our request_global_template_sync, kept in
#: ``_pending_template_sync``.
_GLOBAL_SYNC_REQUEST_PREFIX = "gts-"


def _extension_of(*names: Optional[str]) -> str:
    """The first file extension found in ``names`` (URLs are cut at ``?``)."""
    for name in names:
        base = (name or "").split("?", 1)[0].rsplit("/", 1)[-1]
        if "." in base:
            return base.rsplit(".", 1)[-1].lower()
    return ""


def _global_formatting(item: GlobalTemplateItem) -> Optional[Dict[str, Any]]:
    """An item's ``formatting`` for ingest, without an empty blueprint.

    The feed sends ``"blueprint": {}`` for a template whose blueprint is not
    approved yet; passed on, it would overwrite the AI profile with blanks.
    """
    if item.formatting is None:
        return None
    formatting = item.formatting.model_dump(exclude_none=True)
    blueprint = formatting.get("blueprint")
    if isinstance(blueprint, dict) and not any(blueprint.values()):
        formatting.pop("blueprint", None)
    return formatting or None


def _safe_count(value: Any) -> Any:
    """An inbound ``count`` for a log line: an int as-is, anything else its size."""
    return value if isinstance(value, int) else chars(value)


class TemplateSyncMixin:
    """The Global Template Library feed and blueprint proposals."""

    async def on_request_blueprint_proposal(self, data: Dict[str, Any]) -> None:
        """Handle request_blueprint_proposal (Server → AI): profile an admin's
        global template draft (on upload, or "Re-profile") and reply with a
        ``template_blueprint_proposal`` (see :meth:`_handle_global_blueprint_proposal`).

        Wrapped in a usage scope: the ingest OCRs the template and the profile
        calls the model. The event names no workspace, so the spend cannot be
        added to a case — it is priced and logged instead, which also keeps the
        "consumed outside any capture_usage scope" alarm meaningful."""
        from app.ai.usage import capture_usage, log_unbilled_usage

        with capture_usage() as tpl_usage:
            try:
                payload_in = data if isinstance(data, dict) else {}
                await self._handle_global_blueprint_proposal(payload_in)
            finally:
                log_unbilled_usage("request_blueprint_proposal", tpl_usage)

    async def ensure_templates_synced(
        self, *, timeout: Optional[float] = None, force: bool = False
    ) -> None:
        """Make sure the global template library has been loaded this connection.

        A no-op once a snapshot has been applied (pushes keep the store fresh
        after that), unless ``force``. Otherwise it requests the snapshot, or
        joins the one already in flight, and waits for it to finish ingesting.

        The wait is always bounded: ``timeout=None`` means
        ``settings.template_sync_timeout_seconds``. A dropped emit or a backend
        that never answers must not hold the workspace pipeline lock; an
        undelivered request returns at once. On expiry the caller proceeds with
        the current store, and a snapshot that arrives later still refreshes the
        Drafting Assistant (``_reemit_drafting_on_global_change``).
        """
        if not self._socket_client:
            return
        if self._global_templates_synced and not force:
            return
        await self.request_global_template_sync(timeout=timeout)

    def _prewarm_templates(
        self, *, timeout: Optional[float] = None
    ) -> Optional["asyncio.Task"]:
        """Start the bounded template sync as a tracked task.

        Pipeline runners call this BEFORE taking the workspace pipeline lock, so
        the backend round trip never runs while the lock is held; drafting then
        waits on the task only briefly (``templates_ready``). ``timeout`` is the
        run's audio-extended cap, when it has one. Returns ``None`` when there
        is nothing to warm.
        """
        if not self._socket_client or self._global_templates_synced:
            return None
        try:
            return self._track_background_task(
                asyncio.create_task(self.ensure_templates_synced(timeout=timeout)),
                label="template prewarm",
            )
        except RuntimeError as rt_err:
            logger.warning("Could not start template prewarm: {}", rt_err)
            return None

    # ------------------------------------------------------------------
    # Global Template Library (SIM-173): one platform-wide feed, no userId
    # ------------------------------------------------------------------

    async def request_global_template_sync(
        self, *, timeout: Optional[float] = None
    ) -> None:
        """Ask the backend for every published global template and wait for it.

        Sent on every (re)connect: pushes are fire-and-forget, so a publish made
        while this service was down is only caught by this snapshot. One
        request at a time: a second caller waits on the one in flight. The wait
        is bounded (see :meth:`ensure_templates_synced`).
        """
        if not self._socket_client:
            return
        bound = (
            float(timeout)
            if timeout is not None
            else float(settings.template_sync_timeout_seconds)
        )
        if any(
            key.startswith(_GLOBAL_SYNC_REQUEST_PREFIX)
            for key in self._pending_template_sync
        ):
            await self._await_global_template_snapshot(bound)
            return
        request_id = f"{_GLOBAL_SYNC_REQUEST_PREFIX}{uuid.uuid4().hex[:12]}"
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._pending_template_sync[request_id] = fut
        try:
            delivered = await self._socket_client.emit(
                "request_global_template_sync", {"requestId": request_id}
            )
            if not delivered:
                # SocketClient.emit returns False for a dropped emit: no reply
                # can come, so waiting would only hold the caller up.
                logger.warning(
                    "request_global_template_sync not delivered (requestId={}) — "
                    "proceeding with current store",
                    request_id,
                )
                return
            logger.info(
                "request_global_template_sync emitted (requestId={}, bound={}s)",
                request_id,
                round(bound),
            )
            await asyncio.wait_for(fut, timeout=bound)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "request_global_template_sync timed out after {}s (requestId={}) — "
                "proceeding with current store",
                round(bound),
                request_id,
            )
        except Exception as e:
            logger.warning("request_global_template_sync failed: {}", e)
        finally:
            self._pending_template_sync.pop(request_id, None)

    def _schedule_global_template_sync(self) -> None:
        """Start :meth:`request_global_template_sync` as a tracked task (on connect)."""
        if not self._socket_client:
            return
        try:
            self._track_background_task(
                asyncio.create_task(self.request_global_template_sync()),
                label="global template sync",
            )
        except RuntimeError as rt_err:
            logger.warning("Could not start global template sync: {}", rt_err)

    async def _await_global_template_snapshot(
        self, timeout: Optional[float] = None
    ) -> None:
        """Wait, bounded, for an in-flight global snapshot to finish ingesting."""
        pending = [
            fut
            for key, fut in list(self._pending_template_sync.items())
            if key.startswith(_GLOBAL_SYNC_REQUEST_PREFIX) and not fut.done()
        ]
        if not pending:
            return
        bound = (
            float(timeout)
            if timeout is not None
            else float(settings.template_sync_timeout_seconds)
        )
        try:
            # shield: our timeout must not cancel the request's own future.
            await asyncio.wait_for(asyncio.shield(pending[0]), timeout=bound)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "global template snapshot not in after {}s — using the current store",
                round(bound),
            )

    async def on_global_template_sync_response(self, data: Dict[str, Any]) -> None:
        """Handle global_template_sync_response (Server → AI): the full published set.

        A full replace, never a delta: every listed template is applied as
        ``published``, and anything held that is absent was retired or deleted
        while we were away, so it is retired (never suggested again; drafts
        already started from it still resolve). ``templates=[]`` is valid.
        """
        payload_in = data if isinstance(data, dict) else {}
        logger.info(
            "global_template_sync_response received: requestId={} count={} keys={}",
            (payload_in or {}).get("requestId"),
            _safe_count((payload_in or {}).get("count")),
            list((payload_in or {}).keys()),
        )
        raw_request_id = payload_in.get("requestId")
        try:
            try:
                payload = GlobalTemplateSyncResponsePayload(**payload_in)
            except Exception as e:
                logger.warning(
                    "global_template_sync_response: invalid payload (keys={}): {}",
                    list(payload_in.keys()),
                    validation_fields(e),
                )
                return
            templates = list(payload.templates or [])
            from app.ai.usage import capture_usage, log_unbilled_usage

            with capture_usage() as tpl_usage:
                try:
                    applied = 0
                    for item in templates:
                        if await self._apply_global_published(item):
                            applied += 1
                finally:
                    log_unbilled_usage("global template ingest", tpl_usage)
            listed = {item.id for item in templates}
            retired = 0
            for template_id in await template_store.global_ids():
                if template_id not in listed and await template_store.set_global_status(
                    template_id=template_id, status=STATUS_RETIRED
                ):
                    retired += 1
            self._global_templates_synced = True
            logger.success(
                "global template snapshot applied: listed={} applied={} retired={}",
                len(templates),
                applied,
                retired,
            )
        finally:
            fut = (
                self._pending_template_sync.get(raw_request_id)
                if isinstance(raw_request_id, str) and raw_request_id
                else None
            )
            if fut is not None and not fut.done():
                fut.set_result(True)
        await self._reemit_drafting_on_global_change()

    async def on_global_template_sync(self, data: Dict[str, Any]) -> None:
        """Handle global_template_sync (Server → AI): an admin published,
        retired or deleted a global template. Sent once for the platform.

        - ``published``: add or replace the current version. A new file hash is
          downloaded and profiled; the same hash keeps the extracted text. The
          same version with the same hash is a no-op (the backend may re-send).
        - ``retired``: never suggested again; drafts started from it still work.
        - ``deleted``: a never-published draft was removed; erase what we hold.
        """
        payload_in = data if isinstance(data, dict) else {}
        action_in = payload_in.get("action")
        logger.info(
            "global_template_sync received: action={} templates={} keys={}",
            action_in if action_in in _KNOWN_GLOBAL_ACTIONS else chars(action_in),
            len(payload_in.get("templates") or [])
            if isinstance(payload_in.get("templates"), list)
            else "(not a list)",
            list(payload_in.keys()),
        )
        try:
            payload = GlobalTemplateSyncPayload(**payload_in)
        except Exception as e:
            logger.warning(
                "global_template_sync: invalid payload (keys={}): {}",
                list(payload_in.keys()),
                validation_fields(e),
            )
            return

        templates = list(payload.templates or [])
        if payload.action == "published":
            from app.ai.usage import capture_usage, log_unbilled_usage

            with capture_usage() as tpl_usage:
                try:
                    for item in templates:
                        await self._apply_global_published(item)
                finally:
                    log_unbilled_usage("global template ingest", tpl_usage)
        elif payload.action == "retired":
            for item in templates:
                if not await template_store.set_global_status(
                    template_id=item.id, status=STATUS_RETIRED
                ):
                    logger.info("global template {} retired but not held", item.id)
        else:  # deleted
            for item in templates:
                removed = await template_store.forget_global(template_id=item.id)
                logger.info(
                    "global template {} deleted: {} cached entr(ies) erased",
                    item.id,
                    removed,
                )
        await self._reemit_drafting_on_global_change()

    async def _apply_global_published(self, item: GlobalTemplateItem) -> bool:
        """Make ``item`` the current, published version of its global template.

        Returns True when the template is held and published afterwards.
        Per-item failures are logged and isolated.
        """
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("global template: drafting agent unavailable: {}", e)
            return False

        current = await template_store.get(user_id=GLOBAL_OWNER, template_id=item.id)
        same_version = bool(current) and current.get("version") == item.version
        same_file = bool(current) and (current.get("file_hash") or "") == (
            item.fileHash or ""
        )
        if same_version and same_file:
            # A re-send: nothing changes, except a retired template coming back.
            await template_store.set_global_status(
                template_id=item.id, status=STATUS_PUBLISHED
            )
            return True

        formatting = _global_formatting(item)
        if item.kind == "preset" or not item.fileUrl:
            if item.kind != "preset":
                logger.warning(
                    "global template {} v{} published without fileUrl — skipped",
                    item.id,
                    item.version,
                )
                return False
            stored = await drafting_agent.store_preset(
                user_id=GLOBAL_OWNER,
                template_id=item.id,
                name=item.name or item.id,
                category=item.category or "Custom",
                formatting=formatting,
            )
            if not stored:
                return False
            preset = await template_store.get(user_id=GLOBAL_OWNER, template_id=item.id)
            if preset is None:
                return False
            preset.update({"scope": GLOBAL_SCOPE, "version": item.version})
            await template_store.put(
                user_id=GLOBAL_OWNER, template_id=item.id, payload=preset
            )
        else:
            # Reuse an extraction of this exact file: the current version (a
            # metadata-only republish) or the admin draft profiled before publish.
            previous = current if same_file else None
            if previous is None and item.fileHash:
                draft = await template_store.get(
                    user_id=GLOBAL_DRAFT_OWNER, template_id=item.id
                )
                if draft and draft.get("file_hash") == item.fileHash:
                    previous = draft
            try:
                ingested = await drafting_agent.ingest_template(
                    user_id=GLOBAL_OWNER,
                    template_id=item.id,
                    name=item.name or item.originalName or item.id,
                    category=item.category or "Document Draft",
                    extension=item.extension
                    or _extension_of(item.originalName, item.fileUrl),
                    mime_type=item.mimeType,
                    file_url=item.fileUrl,
                    path=None,
                    file_hash=item.fileHash,
                    previous_payload=previous,
                    kind=item.kind,
                    formatting=formatting,
                    scope=GLOBAL_SCOPE,
                    version=item.version,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "global template ingest failed (template={} v{}): {}",
                    item.id,
                    item.version,
                    e,
                )
                return False
            if not ingested:
                logger.warning(
                    "global template {} v{} not ingested — previous version kept",
                    item.id,
                    item.version,
                )
                return False
        await template_store.set_global_status(
            template_id=item.id, status=STATUS_PUBLISHED
        )
        logger.info(
            "global template {} published at v{} (kind={})",
            item.id,
            item.version,
            item.kind or "file",
        )
        return True

    async def _reemit_drafting_on_global_change(self) -> None:
        """Refresh the Drafting Assistant for every lawyer with a panel open.

        The global library is every lawyer's library, so a change reaches each
        workspace whose drafting panel was built; the per-user count check in
        ``_maybe_reemit_drafting_on_templates`` skips the ones it leaves alone.
        """
        user_ids = {
            (meta or {}).get("user_id")
            for meta in list(self._drafting_meta_by_workspace.values())
        }
        for user_id in sorted(uid for uid in user_ids if uid):
            await self._maybe_reemit_drafting_on_templates(user_id)

    async def _handle_global_blueprint_proposal(self, payload_in: Dict[str, Any]) -> None:
        """Profile an admin's global template and reply with ``scope="global"``.

        Every template is global now, so ``scope`` is not required on the
        request. The template is still a draft, so it is profiled under
        ``GLOBAL_DRAFT_OWNER`` and never suggested. The reply carries no
        ``userId`` and echoes ``fileHash``: the backend drops a proposal whose
        hash no longer matches (the admin replaced the file meanwhile). A
        failure is answered ``status="failed"`` so the admin panel is not left
        waiting.
        """
        template_id = str(payload_in.get("templateId") or "").strip()
        file_url = payload_in.get("fileUrl") or ""
        file_hash = str(payload_in.get("fileHash") or "").strip()
        original_name = str(payload_in.get("originalName") or "").strip()
        logger.info(
            "request_blueprint_proposal RECEIVED: scope=global templateId={} "
            "hasFileUrl={} hasFileHash={}",
            template_id,
            bool(file_url),
            bool(file_hash),
        )
        if not template_id or not file_hash:
            logger.warning(
                "request_blueprint_proposal (global): missing templateId/fileHash — dropping"
            )
            return
        try:
            from app.langgraph.drafting_agent import drafting_agent
        except Exception as e:
            logger.error("request_blueprint_proposal (global): drafting agent unavailable: {}", e)
            return

        draft = await template_store.get(user_id=GLOBAL_DRAFT_OWNER, template_id=template_id)
        held = (
            draft
            if draft
            and draft.get("file_hash") == file_hash
            and (draft.get("extracted_text") or "").strip()
            else None
        )
        just_ingested = False
        if held is None and file_url:
            current = await template_store.get(user_id=GLOBAL_OWNER, template_id=template_id)
            previous = next(
                (
                    p
                    for p in (draft, current)
                    if p and p.get("file_hash") == file_hash
                ),
                None,
            )
            try:
                held = await drafting_agent.ingest_template(
                    user_id=GLOBAL_DRAFT_OWNER,
                    template_id=template_id,
                    name=original_name or template_id,
                    category="Document Draft",
                    extension=_extension_of(original_name, file_url),
                    mime_type=payload_in.get("mimeType"),
                    file_url=file_url,
                    path=None,
                    file_hash=file_hash,
                    previous_payload=previous,
                )
                just_ingested = held is not None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "request_blueprint_proposal (global): ingest failed (template={}): {}",
                    template_id,
                    e,
                )

        proposal: Optional[Dict[str, Any]] = None
        if held is not None:
            try:
                # Just profiled at ingest: build from that rather than profiling
                # twice. Already held: this is a "Re-profile" click.
                proposal = await drafting_agent.build_blueprint_proposal(
                    user_id=GLOBAL_DRAFT_OWNER,
                    template_id=template_id,
                    force_reprofile=not just_ingested,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "request_blueprint_proposal (global): build failed (template={}): {}",
                    template_id,
                    e,
                )
        if proposal:
            proposal.pop("userId", None)
            proposal["fileHash"] = file_hash
        else:
            proposal = {
                "templateId": template_id,
                "fileHash": file_hash,
                "status": "failed",
                "error": "template could not be read or profiled",
            }
        proposal["scope"] = GLOBAL_SCOPE
        if not self._socket_client:
            logger.warning("request_blueprint_proposal (global): no socket client")
            return
        delivered = await self._socket_client.emit("template_blueprint_proposal", proposal)
        logger.info(
            "template_blueprint_proposal (global) delivered={} template={} status={} vars={}",
            "yes" if delivered else "no",
            template_id,
            proposal.get("status"),
            len((proposal.get("blueprint") or {}).get("variables") or []),
        )
