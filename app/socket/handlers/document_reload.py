"""Single-document re-read (`document_reload_request`).

Why this exists
---------------
A document can land in evidence badly: a scanned PDF whose OCR produced
nothing, an upload that raced a truncated GCS write, an audio file whose
transcription failed. The lawyer sees a quality score of 0 and a panel built
on a document the AI cannot actually read. Before this event the only recovery
was to delete the document and upload it again, which threw away its id and
every curated item tied to it.

``document_reload_request`` is the targeted repair: the backend re-signs that
one file's URL and asks this service to read it again. The document keeps its
identity — same ``fileHash``, same document id, still present in
``evidenceFiles`` — so this is a RE-READ, not a delete plus an upload.

Four properties of the event shape the implementation:

1. **Purge before re-fetch.** The old chunks are deleted from Qdrant and the
   document's entry is pruned from the cached ``document_assessments`` manifest
   *before* anything is re-ingested or any prompt is built. Skipping the prune
   would leave ``_build_document_inventory`` asserting the old (unreadable)
   copy as ground truth while the new text is being written.
2. **The document is expected back.** Unlike a deletion, a reload is not a
   removal, so the removed-evidence ledger is written ONLY if the re-ingestion
   fails. When it succeeds the fresh assessment is merged back and the ledger
   entry cleared (:meth:`_forget_removed_documents`) — otherwise a successful
   reload would report its own document as a missing-evidence gap.
3. **``currentState`` is mandatory.** The feature re-run replaces every panel,
   so without tagging against the echoed state the lawyer's approved and
   rejected items are wiped — the bug already fixed once for uploads. Cached
   into ``_workspace_current_state_payload`` exactly as ``workspace_sync``
   does, which is what every regeneration path reads.
4. **Re-sends are expected.** The backend records the request in its AI ledger
   and re-sends on a stall or reconnect under the same ``requestId``. A re-send
   while the first attempt is in flight (or shortly after it succeeded) is
   swallowed; a re-send after a FAILED attempt must re-run, because nothing
   else will trigger a second one.

Everything downstream of the re-ingestion is the ordinary re-run: the same
``workspace:refresh`` and ``document_feature_response`` emits a
``workspace_sync`` produces, in the same product order.
"""

import time
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from app.ai.usage import capture_usage
from app.observability.audit import audit_logger
from app.processing.document_processor import document_processor
from app.socket.schemas import DocumentReloadRequestPayload
from app.workspace.participants import set_participants
from app.core.roles import DEFAULT_WORKSPACE_ROLE

#: How long a COMPLETED reload request stays de-duplicated. A re-send inside
#: this window is the backend's reconnect cover for a request it already got an
#: answer to; outside it, a repeat of the same requestId is treated as a fresh
#: instruction. Matches the workspace_sync / version-restore replay windows.
_RELOAD_DEDUPE_SECONDS = 180.0

#: Ceiling on how long a request may sit in the ``in_flight`` state before a
#: re-send is allowed to run it again. A process killed mid-reload leaves no
#: entry at all (the ledger is in memory), so this only covers a reload that
#: hung inside the pipeline — 30 minutes is far longer than the slowest
#: OCR + full feature suite, so it cannot double-run live work.
_RELOAD_INFLIGHT_TIMEOUT_SECONDS = 1800.0

#: Panels whose loaders the frontend lights up for the duration of a reload.
#: ``document_assessments`` is included unconditionally because this handler
#: always emits one — a fresh envelope on success, the cached envelope when the
#: re-ingestion fails — so the loader can never be left spinning.
_RELOAD_REFRESH_FEATURES = [
    "document_assessments",
    "strategy_objective",
    "analysis_report",
    "list_of_dates",
    "applicable_laws",
    "procedural_timeline",
    "relevant_parties",
    "case_laws",
    "drafting_assistant",
]


class DocumentReloadMixin:
    """Handles ``document_reload_request``: purge one document, re-read it,
    re-run the workspace feature suite over the full evidence set."""

    # ------------------------------------------------------------------ dedupe

    def _reload_request_state(self, workspace_id: str) -> Dict[str, Dict[str, Any]]:
        """Per-workspace ``requestId -> {status, ts}`` ledger.

        Keyed by workspace_id at the top level on purpose: that is what the
        compliance purge's ``self.__dict__`` sweep matches on, so a deleted
        workspace's reload history is erased with everything else.
        """
        return self._document_reload_requests.setdefault(workspace_id, {})

    def _should_skip_reload_request(
        self, workspace_id: str, request_id: str
    ) -> bool:
        """True when this ``requestId`` is a duplicate that must not re-run.

        Skips a request that is still in flight, and one that completed inside
        the de-dupe window. Deliberately does NOT skip a request whose previous
        attempt failed: the backend stops re-sending once its own retry budget
        is spent, so swallowing the retry of a failure would leave the document
        unreadable with nothing left to fix it.
        """
        if not request_id:
            return False
        ledger = self._reload_request_state(workspace_id)
        entry = ledger.get(request_id)
        if not entry:
            return False
        status = str(entry.get("status") or "")
        age = time.time() - float(entry.get("ts") or 0.0)
        if status == "in_flight" and age < _RELOAD_INFLIGHT_TIMEOUT_SECONDS:
            logger.info(
                "Skipping document_reload_request {} for workspace {}: already in "
                "flight ({:.0f}s ago)",
                request_id,
                str(workspace_id)[:12],
                age,
            )
            return True
        if status == "done" and age < _RELOAD_DEDUPE_SECONDS:
            logger.info(
                "Skipping duplicate document_reload_request {} for workspace {} "
                "(completed {:.0f}s ago)",
                request_id,
                str(workspace_id)[:12],
                age,
            )
            return True
        return False

    def _mark_reload_request(
        self, workspace_id: str, request_id: str, status: str
    ) -> None:
        """Record a request's state (``in_flight`` / ``done`` / ``failed``) and
        drop entries too old to be anyone's retry."""
        if not request_id:
            return
        ledger = self._reload_request_state(workspace_id)
        now = time.time()
        ledger[request_id] = {"status": status, "ts": now}
        stale = [
            rid
            for rid, e in ledger.items()
            if now - float(e.get("ts") or 0.0) > _RELOAD_INFLIGHT_TIMEOUT_SECONDS * 2
        ]
        for rid in stale:
            ledger.pop(rid, None)

    # ------------------------------------------------------------- id resolve

    def _resolve_reload_document_id(
        self,
        workspace_id: str,
        payload: DocumentReloadRequestPayload,
    ) -> Optional[str]:
        """Find the backend document id for the file being reloaded.

        ``process_batch`` indexes ``file_info["_id"]`` unconditionally, and the
        id must be the one the backend already uses or the re-ingested copy
        lands under a second identity that no future delete can reach. The
        backend's ``targetFile`` block carries no id of its own, so it is
        resolved, most reliable source first:

        1. an ``_id`` on ``targetFile`` itself, if the backend ever adds one;
        2. the ``evidenceFiles`` entry with the same ``fileHash`` (the contract
           guarantees the reloaded file appears there too, unchanged);
        3. the ``evidenceFiles`` entry with the same ``storeFileName``/``path``
           — covers a backend that re-hashes on re-signing;
        4. a UNIQUE match on filename in the cached ``document_assessments``
           manifest, which is this service's own record of what it ingested.

        Returns ``None`` when every route fails, which the caller treats as a
        hard stop rather than inventing an id.
        """
        target = payload.targetFile

        direct = str(getattr(target, "id", "") or "").strip()
        if direct:
            return direct

        target_hash = str(target.fileHash or "").strip().lower()
        store_name = str(target.storeFileName or "").strip().lower()
        target_path = str(target.path or "").strip().lower()

        for f in payload.evidenceFiles or []:
            fid = str(getattr(f, "id", "") or "").strip()
            if not fid:
                continue
            if target_hash and str(f.fileHash or "").strip().lower() == target_hash:
                return fid
        for f in payload.evidenceFiles or []:
            fid = str(getattr(f, "id", "") or "").strip()
            if not fid:
                continue
            if store_name and str(f.storeFileName or "").strip().lower() == store_name:
                return fid
            if target_path and str(f.path or "").strip().lower() == target_path:
                return fid

        # Last resort: this service's own manifest. Only accepted on a unique
        # name match — two documents sharing a display name would otherwise
        # make the reload purge the wrong one.
        names = {
            str(target.filename or "").strip().lower(),
            str(target.originalName or "").strip().lower(),
        }
        names.discard("")
        if names:
            cached = self._workspace_document_assessments_payload.get(workspace_id)
            entries = []
            if isinstance(cached, dict):
                entries = (cached.get("payload") or {}).get(
                    "documentAssessments"
                ) or []
            matches = {
                str(e.get("documentId") or "").strip()
                for e in entries
                if isinstance(e, dict)
                and e.get("documentId")
                and self._assessment_entry_name(e).strip().lower() in names
            }
            matches.discard("")
            if len(matches) == 1:
                resolved = matches.pop()
                logger.info(
                    "Reload target resolved from the cached assessments manifest "
                    "by filename: {}",
                    resolved,
                )
                return resolved
            if len(matches) > 1:
                logger.warning(
                    "Reload target filename {} matches {} cached documents; refusing "
                    "to guess which one to purge",
                    sorted(names),
                    len(matches),
                )
        return None

    # ----------------------------------------------------------------- handler

    async def on_document_reload_request(self, data: Dict[str, Any]) -> None:
        """
        Handle document_reload_request event (Server → AI_Maverick).

        Flow:
        1. Parse + de-dupe on ``requestId``; cache ``currentState``; roster
        2. Hydrate caches, resolve role, upsert context from ``evidenceFiles``
        3. Resolve the target document id (hard stop if it cannot be resolved)
        4. ``workspace:refresh`` → every panel's loader on
        5. Purge the document: vectors, then the assessments manifest entry
        6. Re-fetch + re-extract it (metered), emit fresh assessments + metadata
        7. Regenerate strategy + analysis inline, then schedule the tail
        8. Audit
        """
        request_id = str((data or {}).get("requestId") or "")
        workspace_id = str((data or {}).get("workspaceId") or "")
        try:
            payload = DocumentReloadRequestPayload(**data)
            workspace_id = str(payload.workspaceId)
            user_id = str(payload.userId)
            request_id = str(payload.requestId or "")
            reason = str(payload.reason or "user_requested")

            # Reload traffic is a live workspace: revoke any armed compliance
            # purge (see handlers/workspace_purge.py).
            self.cancel_pending_purge(workspace_id, trigger="document_reload_request")

            # Roster is context only here. NOT authoritative: a reload is a
            # document operation and says nothing about sharing, so an absent
            # `participants` must not be read as an un-share the way it is on
            # workspace_sync.
            try:
                set_participants(
                    workspace_id, payload.participants, authoritative=False
                )
            except Exception as participants_err:
                logger.warning(
                    "Participant roster update on document reload failed "
                    "(non-fatal): {}",
                    participants_err,
                )

            if self._should_skip_reload_request(workspace_id, request_id):
                return
            self._mark_reload_request(workspace_id, request_id, "in_flight")

            logger.info(
                "Document reload: {} in workspace {} (user {}, reason={}, "
                "previous score={})",
                payload.targetFile.originalName,
                workspace_id,
                user_id,
                reason,
                payload.targetFile.previousQualityScore,
            )

            # Curated panel state — mandatory on this event, but treated with
            # the same "only overwrite when present" rule as sync so a payload
            # that omits it cannot clobber the last known curation.
            if payload.currentState is not None:
                try:
                    self._workspace_current_state_payload[workspace_id] = (
                        payload.currentState.model_dump()
                    )
                except Exception as cs_err:
                    logger.debug(
                        "Failed to cache currentState on document reload: {}", cs_err
                    )

            # Hydrate BEFORE any upsert: on cold memory an upsert creates an
            # empty-metadata context and persists it over the WORKSPACE_META
            # row, wiping the workspace's cumulative token usage. Same ordering
            # as on_workspace_sync / on_workspace_version_restore.
            try:
                await self._hydrate_workspace_caches(workspace_id, user_id=user_id)
            except Exception as hydrate_err:
                logger.debug(
                    "Workspace cache hydration before document reload failed "
                    "({}): {}",
                    workspace_id[:12],
                    hydrate_err,
                )

            if payload.workspaceRole:
                await self._remember_workspace_role(
                    workspace_id, user_id, str(payload.workspaceRole)
                )
            workspace_role = await self._resolve_workspace_role(
                workspace_id=workspace_id,
                user_id=user_id,
                preferred_role=payload.workspaceRole,
            )
            logger.info("Resolved workspace role for document reload: {}", workspace_role)

            from datetime import datetime as dt

            from app.workspace.context import context_manager

            active_files = [
                f
                for f in (payload.evidenceFiles or [])
                if not (
                    getattr(f, "isDeleted", False) or getattr(f, "is_deleted", False)
                )
            ]
            updated_at = None
            try:
                if payload.timestamp:
                    updated_at = dt.fromisoformat(
                        payload.timestamp.replace("Z", "+00:00")
                    )
            except Exception:
                updated_at = dt.utcnow()
            context_manager.upsert_workspace(
                workspace_id,
                user_id,
                title=payload.title,
                status=payload.status,
                workspace_role=payload.workspaceRole or workspace_role,
                evidence_files=active_files,
                updated_at=updated_at,
            )

            document_id = self._resolve_reload_document_id(workspace_id, payload)
            if not document_id:
                # Without the backend's own document id the re-ingested copy
                # would land under a fabricated identity that no later delete or
                # sync could reach — worse than not reloading at all. Nothing has
                # been emitted or deleted yet (this runs before the refresh), so
                # stopping here leaves the workspace exactly as it was and no
                # loader spinning; the backend's own stall cover reports it.
                logger.error(
                    "document_reload_request {}: could not resolve a document id for "
                    "{} (fileHash {}); evidenceFiles carried no matching _id and the "
                    "cached manifest gave no unique match. Reload abandoned.",
                    request_id or "<none>",
                    payload.targetFile.originalName,
                    payload.targetFile.fileHash,
                )
                audit_logger.log_event(
                    "document_reload_unresolved",
                    {
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "request_id": request_id,
                        "file_hash": payload.targetFile.fileHash,
                        "file_name": payload.targetFile.originalName,
                        "reason": reason,
                    },
                )
                self._mark_reload_request(workspace_id, request_id, "failed")
                return

            # Drift is informational only: the reload is scoped to one document,
            # and silently re-ingesting everything else would spend the user's
            # money on work they did not ask for. A real divergence is a
            # workspace_sync's job.
            from app.vectorstore.qdrant import vector_store

            live_ids: Set[str] = set()
            try:
                live_docs = await vector_store.get_all_documents(
                    workspace_id=workspace_id
                )
                live_ids = {
                    str(d.get("document_id")).strip()
                    for d in (live_docs or [])
                    if d.get("document_id")
                }
                declared_ids = {
                    str(getattr(f, "id", "") or "").strip()
                    for f in active_files
                    if str(getattr(f, "id", "") or "").strip()
                }
                if declared_ids:
                    missing = sorted(declared_ids - live_ids)
                    orphaned = sorted(live_ids - declared_ids)
                    if missing or orphaned:
                        logger.warning(
                            "Reload drift for workspace {}: {} declared file(s) not in "
                            "the vector store, {} indexed doc(s) not declared. Only "
                            "the reload target is touched.",
                            workspace_id[:12],
                            len(missing),
                            len(orphaned),
                        )
            except Exception as live_err:
                logger.debug(
                    "Could not read live documents before reload (non-fatal): {}",
                    live_err,
                )

            await self._emit_workspace_refresh(
                workspace_id=workspace_id,
                user_id=user_id,
                reason="ai_update_started",
                feature_types=list(_RELOAD_REFRESH_FEATURES),
            )

            # ------------------------------ purge ------------------------------
            # Everything this service holds for the document goes first: its
            # chunks and embeddings in Qdrant, then its entry in the cached
            # assessments manifest. The manifest prune must happen before any
            # prompt is built (_build_document_inventory calls that cache
            # "GROUND TRUTH for which documents are present"), and its return
            # value is the ONLY surviving record of the old entry — held here
            # so it can be written to the removed-evidence ledger if the
            # re-ingestion below fails.
            try:
                await vector_store.delete_documents(
                    workspace_id=workspace_id, document_ids=[document_id]
                )
                logger.info(
                    "Reload purged document {} from the vector store", document_id
                )
            except Exception as del_err:
                logger.error(
                    "Vector store purge for reload of document {} failed; aborting so "
                    "the workspace is not left with both the stale and the fresh copy "
                    "of the same document: {}",
                    document_id,
                    del_err,
                )
                self._mark_reload_request(workspace_id, request_id, "failed")
                raise

            pruned_entries: List[Dict[str, Any]] = []
            try:
                pruned_entries = self._prune_document_assessments(
                    workspace_id, [document_id]
                )
            except Exception as prune_err:
                logger.warning(
                    "Assessment prune during document reload failed: {}", prune_err
                )

            # ----------------------------- re-read -----------------------------
            target = payload.targetFile
            files_to_process = [
                {
                    "url": target.url,
                    "filename": target.filename,
                    "originalName": target.originalName,
                    "storeFileName": target.storeFileName,
                    "extension": target.extension,
                    "fileHash": target.fileHash,
                    "path": target.path,
                    "uploadedAt": target.uploadedAt,
                    "_id": document_id,
                }
            ]
            batch_result: Optional[Dict[str, Any]] = None
            ingest_usage = None
            try:
                # Own capture_usage scope: without one the OCR units, STT
                # seconds, embedding and per-document Gemini tokens this
                # re-extraction spends are billed to nobody.
                with capture_usage() as ingest_usage:
                    batch_result = await document_processor.process_batch(
                        files=files_to_process,
                        workspace_id=workspace_id,
                        user_id=user_id,
                    )
            except Exception as ingest_err:
                logger.opt(exception=ingest_err).error(
                    "Reload re-ingestion of document {} failed: {}",
                    document_id,
                    ingest_err,
                )
                batch_result = None

            reload_succeeded = bool(batch_result) and bool(
                batch_result.get("successful", 0)
            )
            new_score = None
            if batch_result:
                for r in batch_result.get("results", []):
                    if str(r.get("document_id") or "") == document_id:
                        new_score = r.get("readability_score")
            if reload_succeeded:
                logger.success(
                    "Document {} re-read: quality score {}  {}",
                    payload.targetFile.originalName,
                    payload.targetFile.previousQualityScore,
                    new_score,
                )
            else:
                logger.error(
                    "Document {} could not be re-read; it is no longer in the "
                    "vector store and is now reported as a missing-evidence gap",
                    payload.targetFile.originalName,
                )

            if self._socket_client:
                if batch_result:
                    try:
                        document_assessment_payload = (
                            self._build_document_assessment_envelope(
                                workspace_id, user_id, batch_result
                            )
                        )
                        document_assessment_payload["metadata"] = (
                            self._ingest_usage_metadata(workspace_id, ingest_usage)
                        )
                        await self._socket_client.emit(
                            "document_feature_response", document_assessment_payload
                        )
                        # MERGE, never replace: this envelope describes the one
                        # reloaded file, and the cache has to stay a manifest of
                        # every live document. The merge is keyed on documentId,
                        # so the reloaded document's entry is replaced in place
                        # and keeps its position in the inventory.
                        self._workspace_document_assessments_payload[workspace_id] = (
                            self._merge_document_assessments(
                                workspace_id, document_assessment_payload
                            )
                        )
                        logger.success(
                            "Document assessments (reload) sent to backend"
                        )
                    except Exception as assess_err:
                        logger.error(
                            "Failed to emit document_assessments on reload: {}",
                            assess_err,
                        )
                else:
                    # The refresh above lit the document_assessments loader on
                    # the promise of an emit. Nothing was produced, so re-emit
                    # the cached envelope to settle it instead of leaving the
                    # panel spinning. Mirrors the version-restore failure path.
                    try:
                        cached_assess = (
                            self._workspace_document_assessments_payload.get(
                                workspace_id
                            )
                        )
                        if isinstance(cached_assess, dict):
                            # Bill the partial spend, not zeros: a re-read that
                            # died after OCR still burned OCR units, and this is
                            # the only emit that can carry them.
                            # _ingest_usage_metadata degrades to the
                            # cumulative-only block when the scope never opened
                            # or was already counted.
                            cached_assess["metadata"] = self._ingest_usage_metadata(
                                workspace_id, ingest_usage
                            )
                            await self._socket_client.emit(
                                "document_feature_response", cached_assess
                            )
                            logger.info(
                                "Re-emitted cached document_assessments to clear the "
                                "loader after a failed reload re-ingestion"
                            )
                    except Exception as reemit_err:
                        logger.warning(
                            "Could not re-emit cached document_assessments after a "
                            "failed reload: {}",
                            reemit_err,
                        )

            # Ledger reconciliation. A reload is NOT a removal, so the ledger is
            # only written when the document genuinely failed to come back —
            # otherwise a successful reload would make the analysis report
            # announce its own document as a missing-evidence gap. Conversely a
            # failed reload must write it, because the document really is out of
            # evidence now and the gap is the honest report.
            try:
                if reload_succeeded:
                    self._forget_removed_documents(workspace_id, [document_id])
                elif pruned_entries:
                    self._record_removed_documents(workspace_id, pruned_entries)
            except Exception as ledger_err:
                logger.warning(
                    "Removed-document ledger update after reload failed: {}",
                    ledger_err,
                )

            # workspace_metadata: empty title + updateTitle=False so the backend
            # keeps the name the user has — a reload must never rename the case.
            # Category/description are re-derived because a document that was
            # unreadable before can change both once it is finally read.
            if reload_succeeded and self._socket_client:
                try:
                    with capture_usage() as meta_usage:
                        meta = await self._get_workspace_category_and_subcategory(
                            workspace_id=workspace_id,
                            user_id=user_id,
                        )
                    workspace_metadata_payload = {
                        "featureType": "workspace_metadata",
                        "workspaceId": workspace_id,
                        "userId": user_id,
                        "payload": {
                            "workspaceTitle": "",
                            "updateTitle": False,
                            "caseCategory": meta.get("caseCategory", "General"),
                            "subcategories": meta.get("subcategories", []),
                            "description": meta.get("description", ""),
                        },
                    }
                    workspace_metadata_payload["metadata"] = (
                        self._ingest_usage_metadata(workspace_id, meta_usage)
                    )
                    await self._socket_client.emit(
                        "document_feature_response", workspace_metadata_payload
                    )
                    self._workspace_metadata_payload[workspace_id] = (
                        workspace_metadata_payload
                    )
                    logger.success(
                        "Workspace metadata (reload) sent: title=unchanged, "
                        "category={}",
                        meta.get("caseCategory"),
                    )
                except Exception as meta_err:
                    logger.warning(
                        "workspace_metadata emit on reload failed: {}", meta_err
                    )

            # -------------------------- regenerate all --------------------------
            # Identical to the version-restore tail: strategy + analysis inline
            # (so a failure still leaves a valid payload via the fallbacks), then
            # the scheduled tail covers list_of_dates → applicable_laws →
            # procedural_timeline → relevant_parties → case_laws → drafting.
            # Bound before the try so the emits can always read them even if a
            # scope never opened.
            _u_strategy = None
            _u_analysis = None
            strategies_result = None
            try:
                with capture_usage() as _u_strategy:
                    strategies_result = await self._generate_strategies_from_documents(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        workspace_role=workspace_role,
                        batch_result=batch_result,
                        is_update=True,
                    )
            except Exception as strategy_err:
                logger.warning(
                    "Strategy generation after document reload failed: {}; using "
                    "fallback",
                    strategy_err,
                )
            if not strategies_result:
                strategies_result = self._strategy_objective_fallback(
                    workspace_id, user_id, workspace_role, is_update=True
                )
            try:
                if self._socket_client:
                    await self._emit_document_feature(
                        feature_type="strategy_objective",
                        workspace_id=workspace_id,
                        user_id=user_id,
                        payload=strategies_result,
                        usage=_u_strategy,
                    )
                    self._workspace_strategy_payload[workspace_id] = strategies_result
                    logger.success("Strategy & Objectives sent after document reload")
            except Exception as strat_emit_err:
                logger.warning(
                    "Strategy emit after document reload failed: {}", strat_emit_err
                )

            analysis_report = None
            try:
                with capture_usage() as _u_analysis:
                    analysis_report = (
                        await self._generate_analysis_report_from_documents(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            workspace_role=workspace_role,
                            batch_result=batch_result,
                            is_update=True,
                        )
                    )
            except Exception as ar_err:
                logger.warning(
                    "Analysis report after document reload failed: {}; using fallback",
                    ar_err,
                )
            if not analysis_report or not isinstance(analysis_report, dict):
                analysis_report = self._analysis_report_fallback(
                    (workspace_role or DEFAULT_WORKSPACE_ROLE).strip().upper()
                )
            # Tag against the echoed currentState: items the lawyer approved or
            # rejected keep their ids (isMatched=True) and only genuinely new
            # findings arrive as new. This is what stops a reload wiping the
            # curation — the reason currentState is mandatory on this event.
            try:
                analysis_report = await self._tag_analysis_report_against_current_state(
                    workspace_id, analysis_report, log_context=" (document reload)"
                )
            except Exception as tag_err:
                logger.warning(
                    "Analysis report currentState tagging after document reload "
                    "failed: {}; emitting untagged",
                    tag_err,
                )
            try:
                if self._socket_client:
                    await self._emit_document_feature(
                        feature_type="analysis_report",
                        workspace_id=workspace_id,
                        user_id=user_id,
                        payload=analysis_report,
                        usage=_u_analysis,
                    )
                    self._workspace_analysis_payload[workspace_id] = analysis_report
                    logger.success("Analysis report sent after document reload")
            except Exception as ar_emit_err:
                logger.warning(
                    "Analysis report emit after document reload failed: {}",
                    ar_emit_err,
                )

            # batch_result is non-None whenever the re-read produced anything, which
            # keeps list_of_dates / procedural_timeline OFF their pure-deletion fast
            # path so the re-read document is actually re-examined. deleted_doc_ids
            # is passed ONLY when the document failed to come back — then it really
            # was removed, and the dated panels must drop entries sourced from it.
            self._schedule_list_of_dates_and_applicable_laws(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_role=workspace_role,
                batch_result=batch_result,
                log_context=" (after document reload)",
                deleted_doc_ids=None if reload_succeeded else [document_id],
            )

            self._mark_reload_request(
                workspace_id, request_id, "done" if reload_succeeded else "failed"
            )
            audit_logger.log_event(
                "document_reload_ai",
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "request_id": request_id,
                    "document_id": document_id,
                    "file_name": payload.targetFile.originalName,
                    "reason": reason,
                    "previous_quality_score": payload.targetFile.previousQualityScore,
                    "new_quality_score": new_score,
                    "succeeded": reload_succeeded,
                    "evidence_files_count": len(active_files),
                },
            )
            logger.success(
                "Document reload completed for workspace {} (document {}, "
                "succeeded={})",
                workspace_id,
                document_id,
                reload_succeeded,
            )

        except Exception as e:
            # Leave the request marked failed so the backend's re-send is allowed
            # to run it again — a swallowed retry would strand the document.
            try:
                self._mark_reload_request(workspace_id, request_id, "failed")
            except Exception:
                pass
            logger.opt(exception=e).error("Error processing document_reload_request event: {}", str(e))
            raise
