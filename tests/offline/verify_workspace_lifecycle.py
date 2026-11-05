#!/usr/bin/env python3
"""
verify_workspace_lifecycle.py — Offline checks for the workspace lifecycle:
compliance purge, ownership transfer, version restore, reconnect delivery, the
default role and the log safety of invalid-payload paths.

Covers the fixes in a6f1e6b and 13ee4f9:

  * purge: ``purgeAfter`` is absolute; a due purge runs at once; a duplicate
    ``requestId`` is swallowed; live traffic revokes it; persistence is blocked
    FIRST; the memory sweep takes per-workspace and ``"<ws>:"`` keys but never
    ``_pending_purges`` mid-run nor ``_run_epochs``; the case's running work is
    cancelled and fenced (epoch raised); failures retry and, when every attempt
    fails, escalate (critical log + ``workspace_purge_failed`` audit); buffered
    chat replies of the case are dropped;
  * transfer: invalid payload logs no values; the re-emit task is tracked and
    registered as a run task (a stop/purge can cancel it); unknown features are
    skipped; the reconnect replay is armed for the NEW owner;
  * version restore: ``evidenceFiles`` is authoritative; a repeated
    ``versionId`` is swallowed;
  * reconnect: undelivered chat replies are buffered per chat and all re-sent
    on connect (even when a panel replay is pending); a reply that fails again
    stays buffered; every armed workspace inside the window is replayed, an
    older one is skipped;
  * role: nothing known → PETITIONER; workspace_created with an empty or an
    invalid role stores PETITIONER;
  * logs: ``validation_fields`` never carries an input value; the case-law,
    law-relevance and global template sync invalid-payload paths log none either.

Real handlers with Qdrant, the document processor, the generators, audit and
the socket stubbed. No API key, no backend, no Qdrant, no GCS.

USAGE
    python tests/offline/verify_workspace_lifecycle.py            # run everything
    python tests/offline/verify_workspace_lifecycle.py --list     # show the groups
    python tests/offline/verify_workspace_lifecycle.py --only purge transfer
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

# Record (rather than perform) the write-through persistence BEFORE the handler
# modules import it by name, so nothing reaches Qdrant.
from app.workspace import persistence as _persistence  # noqa: E402

PERSISTED = []
STORE = {}


def _record_persist(workspace_id, kind, data, **_kw):
    PERSISTED.append((workspace_id, kind))
    STORE[(workspace_id, kind)] = copy.deepcopy(data)


async def _restore(workspace_id, kind, **_kw):
    return copy.deepcopy(STORE.get((workspace_id, kind)))


_persistence.persist_state = _record_persist
_persistence.restore_state = _restore

from loguru import logger  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.langgraph.integration import workspace_role_integration  # noqa: E402
from app.observability.audit import audit_logger  # noqa: E402
from app.processing.document_processor import document_processor  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers import run_control as RC  # noqa: E402
from app.socket.handlers import workspace_events as WE  # noqa: E402
from app.socket.handlers import workspace_purge as WP  # noqa: E402
from app.socket.schemas import CaseLawDocumentRequest  # noqa: E402
from app.utils.redact import validation_fields  # noqa: E402
from app.vectorstore.qdrant import vector_store  # noqa: E402
from app.workspace import participants as P  # noqa: E402
from app.workspace.context import context_manager  # noqa: E402
from app.workspace.manager import workspace_manager  # noqa: E402

RC.restore_state = _restore

RESULTS = []
EMITS = []
CALLS = []
AUDIT = []
LOGS = []  # (level, message) from every log line, captured by a sink
RUN_ID = uuid.uuid4().hex[:8]

# Fictional ids only.
OWNER = "6a00e1f8d3a9c4b2e1f00001"
NEW_OWNER = "6a00e1f8d3a9c4b2e1f00002"
DOC_A = "6a00c1f0a9e4b0d2a1c30001"
DOC_B = "6a00c1f0a9e4b0d2a1c30002"
DOC_C = "6a00c1f0a9e4b0d2a1c30003"

#: Marker planted in payload fields that must never reach a log line.
SECRET = "ZQX-SENTINEL-" + RUN_ID


def _ws(name):
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _logs_since(mark):
    return LOGS[mark:]


def _leaked(mark, needle=SECRET):
    return [m for _, m in _logs_since(mark) if needle in m]


async def _until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# ----------------------------------------------------------------------- socket

class _FakeSocket:
    """Returns True: SocketClient.emit reports DELIVERY (see CLAUDE.md)."""

    def __init__(self):
        self.fail_chats = set()  # chatIds whose ai_response is "dropped"

    async def emit(self, event, payload):
        EMITS.append((event, copy.deepcopy(payload)))
        return True

    async def emit_ai_response(self, payload):
        if str(payload.get("chatId")) in self.fail_chats:
            return False
        EMITS.append(("ai_response", copy.deepcopy(payload)))
        return True


SOCKET = _FakeSocket()


def _features_emitted(ws):
    return [p.get("featureType") for e, p in EMITS
            if e == "document_feature_response" and p.get("workspaceId") == ws]


# ------------------------------------------------------------------------ stubs

LIVE_DOCS = {}


def _evidence(doc_id, name):
    return {
        "_id": doc_id, "originalName": name, "storeFileName": f"6a00_{name}",
        "filename": name.rsplit(".", 1)[0], "extension": name.rsplit(".", 1)[-1],
        "fileHash": uuid.uuid4().hex, "url": f"https://storage.example.invalid/{name}?sig=x",
        "path": f"workspaces/ws/evidence/{name}", "uploadedAt": "2026-09-29T09:10:00.000Z",
    }


def _batch(ids):
    return {
        "total_files": len(ids), "successful": len(ids), "failed": 0,
        "average_readability_score": 90,
        "results": [{"document_id": i, "filename": i, "success": True,
                     "readability_score": 90, "readability_rating": "Excellent",
                     "readable": True, "extracted_text_length": 100,
                     "stored_in_vector_db": True, "summary": "s",
                     "evidentiary_strength": "High", "document_type": "fir",
                     "extracted_fields": {}, "sensitive_fields": {}}
                    for i in ids],
    }


class _DeleteControl:
    fail_times = 0  # how many delete_workspace calls raise before succeeding
    calls = 0


DELETE = _DeleteControl()
ORDER = []  # purge stage order


def _install_stubs():
    def _audit(event_type, data, *a, **kw):
        AUDIT.append((event_type, copy.deepcopy(data)))
    audit_logger.log_event = _audit

    async def _process_batch(files, workspace_id, user_id):
        ids = [f["_id"] for f in files]
        CALLS.append(("process_batch", workspace_id, sorted(ids)))
        LIVE_DOCS.setdefault(workspace_id, [])
        LIVE_DOCS[workspace_id] += [i for i in ids if i not in LIVE_DOCS[workspace_id]]
        return _batch(ids)

    async def _get_all_documents(workspace_id, limit_per_batch=100):
        return [{"document_id": i, "content": "x"} for i in LIVE_DOCS.get(workspace_id, [])]

    async def _delete_documents(workspace_id, document_ids):
        CALLS.append(("delete_documents", workspace_id, sorted(document_ids)))
        LIVE_DOCS[workspace_id] = [
            i for i in LIVE_DOCS.get(workspace_id, []) if i not in document_ids]
        return True

    async def _vs_delete_workspace(workspace_id):
        ORDER.append(("vector_delete", workspace_id))
        return True

    async def _wm_delete_workspace(workspace_id):
        DELETE.calls += 1
        ORDER.append(("manager_delete", workspace_id,
                      workspace_id in H._pending_purges))
        if DELETE.fail_times:
            DELETE.fail_times -= 1
            raise RuntimeError("simulated storage outage")
        return True

    async def _get_role(workspace_id, user_id, chat_id=None, **_kw):
        return None

    async def _upsert_role(workspace_id, user_id, role, chat_id=None, **_kw):
        CALLS.append(("upsert_role", workspace_id, role))
        return True

    document_processor.process_batch = _process_batch
    vector_store.get_all_documents = _get_all_documents
    vector_store.delete_documents = _delete_documents
    vector_store.delete_workspace = _vs_delete_workspace
    vector_store.get_workspace_role = _get_role
    vector_store.upsert_workspace_role = _upsert_role
    workspace_manager.delete_workspace = _wm_delete_workspace

    real_block = WP.block_workspace_persistence

    def _block(workspace_id):
        ORDER.append(("block", workspace_id))
        real_block(workspace_id)
    WP.block_workspace_persistence = _block

    real_sweep = H._clear_workspace_memory

    def _sweep(workspace_id):
        ORDER.append(("sweep", workspace_id))
        return real_sweep(workspace_id)
    H._clear_workspace_memory = _sweep

    async def _init_role(workspace_id, user_id, initial_role, **_kw):
        CALLS.append(("init_role", workspace_id, initial_role))
        return {"ok": True}
    workspace_role_integration.initialize_workspace_with_role = _init_role

    async def _hydrate(workspace_id, *, user_id=None, recover=True):
        return None

    async def _remember(workspace_id, user_id, role, **_kw):
        CALLS.append(("remember_role", workspace_id, role))

    async def _resolve(workspace_id, user_id, preferred_role=None, **_kw):
        return (preferred_role or "PETITIONER").strip().upper()

    async def _tag(workspace_id, report, *, log_context=""):
        return report

    async def _meta(workspace_id, user_id):
        return {"caseCategory": "Criminal", "subcategories": [], "description": "d"}

    async def _strategy(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("strategy", workspace_id))
        return {"caseSummary": "A fictional FIR.", "strategicObjective": "Bail.",
                "possibleNextSteps": [], "suggestionChips": []}

    async def _analysis(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("analysis", workspace_id))
        return {"missingInfo": [], "caseResolutionScore": 60}

    def _schedule_tail(**kw):
        CALLS.append(("schedule_tail", kw.get("workspace_id"),
                      sorted(kw.get("deleted_doc_ids") or [])))

    async def _recover():
        return None

    H._hydrate_workspace_caches = _hydrate
    H._remember_workspace_role = _remember
    H._resolve_workspace_role = _resolve
    H._tag_analysis_report_against_current_state = _tag
    H._get_workspace_category_and_subcategory = _meta
    H._generate_strategies_from_documents = _strategy
    H._generate_analysis_report_from_documents = _analysis
    H._schedule_list_of_dates_and_applicable_laws = _schedule_tail
    H._recover_interrupted_work = _recover
    H._socket_client = SOCKET


def _reset(*workspaces):
    EMITS.clear()
    CALLS.clear()
    AUDIT.clear()
    ORDER.clear()
    DELETE.fail_times = 0
    DELETE.calls = 0
    SOCKET.fail_chats.clear()
    for ws in workspaces:
        WP.allow_workspace_persistence(ws)
        for name, value in list(vars(H).items()):
            if isinstance(value, dict):
                for k in [k for k in value if k == ws
                          or (isinstance(k, str) and k.startswith(ws + ":"))]:
                    value.pop(k, None)
            elif isinstance(value, set):
                value.discard(ws)
        LIVE_DOCS.pop(ws, None)
        P.clear_participants(ws)


def _deleted(ws, *, purge_after, request_id=None, days=7):
    return {
        "workspaceId": ws, "userId": OWNER, "deletedAt": _iso(datetime.now(timezone.utc)),
        "purgeAfter": purge_after, "purgeAfterDays": days, "reason": "user_deleted",
        "requestId": request_id or f"wd_{ws[:8]}_{uuid.uuid4().hex[:6]}",
        "event": "workspace_deleted",
    }


def _audits(kind, ws=None):
    return [d for e, d in AUDIT if e == kind and (ws is None or d.get("workspace_id") == ws)]


async def _drain_purge(ws):
    pending = H._pending_purges.get(ws) or {}
    task = pending.get("task")
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except Exception:
            pass


# -------------------------------------------------------------------- 1: purge

async def check_purge():
    # 1a. A future purgeAfter is obeyed as sent, never recomputed from days.
    ws = _ws("purge_future")
    _reset(ws)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await H.handle("workspace_deleted", _deleted(ws, purge_after=_iso(future), days=0))
    await asyncio.sleep(0.05)
    pending = H._pending_purges.get(ws) or {}
    armed_for = WP._parse_iso_utc(pending.get("purgeAfter"))
    check("a future purgeAfter arms a timer and does not purge yet",
          pending.get("task") is not None and not pending["task"].done()
          and ws not in H._purged_workspaces and DELETE.calls == 0)
    check("the timer is armed for purgeAfter exactly (absolute, not purgeAfterDays)",
          armed_for is not None and abs((armed_for - future).total_seconds()) < 1,
          detail=f"armed={pending.get('purgeAfter')} sent={_iso(future)}")

    # 1b. Duplicate requestId while pending: swallowed, same timer.
    rid = pending.get("requestId")
    first_task = pending.get("task")
    await H.handle("workspace_deleted",
                   _deleted(ws, purge_after=_iso(future), request_id=rid))
    check("a re-send under the same requestId is swallowed (same timer, one schedule)",
          (H._pending_purges.get(ws) or {}).get("task") is first_task
          and len(_audits("workspace_purge_scheduled", ws)) == 1
          and not first_task.done())

    # 1c. Live traffic revokes the pending purge (via a real workspace_sync).
    updated_at = "2026-10-01T10:00:00.000Z"
    fp = json.dumps({"workspaceId": ws, "updatedAt": updated_at, "deletedDocumentIds": [],
                     "files": [], "workspaceRole": "", "title": "", "status": "",
                     "currentState": ""}, sort_keys=True, default=str)
    # Seed the replay guard so the sync stops right after the purge revocation.
    H._last_workspace_sync_fingerprint[ws] = {"fingerprint": fp, "ts": time.time()}
    try:
        await H.handle("workspace_sync", {"workspaceId": ws, "userId": OWNER,
                                          "updatedAt": updated_at})
    except Exception as e:  # the check below is what matters
        print(f"        (workspace_sync raised {type(e).__name__})")
    await asyncio.sleep(0)
    check("a workspace_sync revokes the pending purge (timer cancelled, entry gone)",
          ws not in H._pending_purges and first_task.cancelled()
          and len(_audits("workspace_purge_cancelled", ws)) == 1)
    await asyncio.sleep(0.05)
    check("a revoked purge never erases", DELETE.calls == 0
          and ws not in H._purged_workspaces)

    # 1d. A past purgeAfter purges immediately (even with purgeAfterDays=7).
    ws = _ws("purge_due")
    _reset(ws)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    await H.handle("workspace_deleted", _deleted(ws, purge_after=_iso(past), days=7))
    await _until(lambda: ws in H._purged_workspaces)
    check("a past purgeAfter purges at once", ws in H._purged_workspaces
          and len(_audits("workspace_purged", ws)) == 1)
    check("and the pending entry is cleared once the erase succeeded",
          ws not in H._pending_purges)

    # 1e. Ordering, sweep scope, halt of running work, chat replies.
    ws, other = _ws("purge_full"), _ws("purge_full_other")
    _reset(ws, other)
    H._workspace_strategy_payload[ws] = {"caseSummary": "x"}
    H._workspace_strategy_payload[other] = {"caseSummary": "y"}
    H._role_cache_by_user_workspace[f"{ws}:{OWNER}"] = "RESPONDENT"
    H._role_cache_by_user_workspace[f"{other}:{OWNER}"] = "RESPONDENT"
    H._recovered_workspaces.add(ws)
    H._recovered_workspaces.add(other)
    H._pending_ai_responses["chat_purged"] = {"chatId": "chat_purged", "workspaceId": ws}
    H._pending_ai_responses["chat_kept"] = {"chatId": "chat_kept", "workspaceId": other}
    H._arm_reconnect_reemit(ws, OWNER)
    H._arm_reconnect_reemit(other, OWNER)
    H._pending_purges[ws] = {"requestId": "wd_x", "task": None}  # mid-run
    epoch_before = H._run_epoch(ws)
    H._run_epochs[ws] = epoch_before

    seen = {"blocked_at_cancel": None, "late_emit": None}

    async def _running_panel():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["blocked_at_cancel"] = _persistence.is_workspace_persistence_blocked(ws)
            # What a real pipeline does on its way out: try to emit. Fenced.
            seen["late_emit"] = await H._emit_document_feature(
                feature_type="case_laws", workspace_id=ws, user_id=OWNER,
                payload={"caseLaws": []})
            raise

    token = RC._RUN_SCOPE.set((ws, epoch_before))
    try:
        running = asyncio.create_task(_running_panel())
    finally:
        RC._RUN_SCOPE.reset(token)
    H._register_run_task(ws, running)
    await asyncio.sleep(0)

    await H._purge_workspace(workspace_id=ws, request_id="wd_x", user_id=OWNER)

    stages = [s[0] for s in ORDER if s[1] == ws]
    check("persistence is blocked FIRST, before the sweep and the deletes",
          stages[:1] == ["block"] and stages.index("sweep") < stages.index("manager_delete")
          < stages.index("vector_delete"), detail=str(stages))
    check("persistence was already blocked when the running work was cancelled",
          seen["blocked_at_cancel"] is True)
    check("the case's running registered task is cancelled (bug 2)", running.cancelled())
    check("the stop fence is raised: epoch incremented and NOT swept (bug 2)",
          H._run_epochs.get(ws) == epoch_before + 1,
          detail=f"before={epoch_before} after={H._run_epochs.get(ws)}")
    check("the cancelled task's late emit is fenced (not delivered, not sent)",
          seen["late_emit"] is False and "case_laws" not in _features_emitted(ws))
    check("sweep drops the per-workspace dict entry",
          ws not in H._workspace_strategy_payload)
    check("sweep drops '<ws>:'-prefixed composite keys",
          f"{ws}:{OWNER}" not in H._role_cache_by_user_workspace)
    check("sweep drops the workspace from per-workspace sets",
          ws not in H._recovered_workspaces)
    check("another workspace's entries survive the sweep",
          other in H._workspace_strategy_payload
          and f"{other}:{OWNER}" in H._role_cache_by_user_workspace
          and other in H._recovered_workspaces and other in H._pending_reemits)
    mgr = [s for s in ORDER if s[0] == "manager_delete" and s[1] == ws]
    check("_pending_purges is NOT swept mid-run (still present at the durable delete)",
          bool(mgr) and mgr[0][2] is True)
    check("the reconnect replay is disarmed for the purged case",
          ws not in H._pending_reemits)
    check("buffered chat replies of the purged case are dropped, others kept",
          "chat_purged" not in H._pending_ai_responses
          and "chat_kept" in H._pending_ai_responses)
    H._pending_ai_responses.pop("chat_kept", None)
    _reset(other)

    # 1f. A failing delete retries, then succeeds.
    old_delays = WP._PURGE_RETRY_DELAYS
    WP._PURGE_RETRY_DELAYS = (0, 0.01, 0.01)
    try:
        ws = _ws("purge_retry")
        _reset(ws)
        DELETE.fail_times = 1
        await H.handle("workspace_deleted", _deleted(
            ws, purge_after=_iso(datetime.now(timezone.utc) - timedelta(seconds=1))))
        await _until(lambda: ws in H._purged_workspaces)
        check("a failed purge attempt is retried and then completes",
              ws in H._purged_workspaces and DELETE.calls == 2,
              detail=f"calls={DELETE.calls}")

        # 1g. Every attempt fails: critical + workspace_purge_failed.
        ws = _ws("purge_fail")
        _reset(ws)
        DELETE.fail_times = 99
        mark = len(LOGS)
        await H.handle("workspace_deleted", _deleted(
            ws, purge_after=_iso(datetime.now(timezone.utc) - timedelta(seconds=1))))
        await _until(lambda: bool(_audits("workspace_purge_failed", ws)))
        check("every attempt is made before giving up",
              DELETE.calls == len(WP._PURGE_RETRY_DELAYS), detail=f"calls={DELETE.calls}")
        check("an exhausted purge logs CRITICAL",
              any(lvl == "CRITICAL" and ws in m for lvl, m in _logs_since(mark)))
        failed = _audits("workspace_purge_failed", ws)
        check("and writes a workspace_purge_failed audit event",
              len(failed) == 1 and failed[0].get("attempts") == len(WP._PURGE_RETRY_DELAYS))
        check("an exhausted purge is not reported as done",
              ws not in H._purged_workspaces)
        H._cancel_purge_task(ws, reason="test cleanup")
    finally:
        WP._PURGE_RETRY_DELAYS = old_delays
        DELETE.fail_times = 0


# ----------------------------------------------------------------- 2: transfer

def _transfer(ws, *, features, at):
    return {
        "workspaceId": ws, "previousUserId": OWNER, "newUserId": NEW_OWNER,
        "title": "New Client 101", "workspaceRole": "RESPONDENT", "status": "active",
        "evidenceFiles": [_evidence(DOC_A, "FIR.pdf")],
        "reEmitFeatures": features, "transferredAt": at,
    }


async def check_transfer():
    ws = _ws("transfer")
    _reset(ws)

    # 2a. Invalid payload: returns cleanly, no values in the log.
    mark = len(LOGS)
    raised = None
    try:
        await H.handle("workspace_transferred", {
            "workspaceId": ws, "previousUserId": OWNER,  # newUserId missing
            "title": SECRET, "reEmitFeatures": [SECRET]})
    except Exception as e:
        raised = e
    check("an invalid transfer payload returns without raising", raised is None,
          detail=repr(raised))
    check("and its log lines carry no payload values", not _leaked(mark),
          detail=str(_leaked(mark))[:300])
    check("and nothing is armed or emitted for it",
          ws not in H._pending_reemits and not _features_emitted(ws))

    # 2b. Valid transfer: the re-emit task is tracked and a run task.
    gate = asyncio.Event()
    drafted = []
    synced = []

    async def _ensure_synced(*, timeout=None, force=False):
        synced.append(force)
        await gate.wait()

    async def _draft(workspace_id, user_id, workspace_role=None, **_kw):
        drafted.append((workspace_id, user_id, workspace_role))

    H.ensure_templates_synced = _ensure_synced
    H._generate_and_emit_drafting_suggestions = _draft
    try:
        before = set(H._background_tasks)
        await H.handle("workspace_transferred", _transfer(
            ws, features=["drafting_assistant"], at="2026-10-02T10:00:00.000Z"))
        await _until(lambda: bool(synced))
        new_tasks = [t for t in H._background_tasks - before if not t.done()]
        check("the transfer re-emit task is held in _background_tasks (tracked)",
              len(new_tasks) == 1, detail=f"{len(new_tasks)} new task(s)")
        task = new_tasks[0] if new_tasks else None
        check("and registered as a run task of the workspace",
              task is not None and task in (H._workspace_run_tasks.get(ws) or {}))
        check("the reconnect replay is armed for the NEW owner",
              (H._pending_reemits.get(ws) or ("", 0))[0] == NEW_OWNER)
        cancelled = H._cancel_workspace_run_tasks(ws)
        await asyncio.sleep(0)
        check("_cancel_workspace_run_tasks cancels the transfer re-emit",
              task is not None and task in cancelled and task.cancelled()
              and not drafted)

        # 2c. Unknown features are skipped; known ones still run.
        gate.set()
        mark = len(LOGS)
        before = set(H._background_tasks)
        await H.handle("workspace_transferred", _transfer(
            ws, features=["bogus_panel", "drafting_assistant"],
            at="2026-10-02T11:00:00.000Z"))
        new_tasks = list(H._background_tasks - before)
        if new_tasks:
            await asyncio.wait(new_tasks, timeout=2)
        check("an unknown reEmitFeatures entry is skipped with a warning",
              any(lvl == "WARNING" and "unknown reEmitFeatures" in m
                  for lvl, m in _logs_since(mark)))
        check("the known feature after it still runs, for the new owner",
              drafted == [(ws, NEW_OWNER, "RESPONDENT")], detail=str(drafted))
        check("nothing is emitted for the unknown feature",
              "bogus_panel" not in _features_emitted(ws))
    finally:
        gate.set()
        del H.ensure_templates_synced
        del H._generate_and_emit_drafting_suggestions
        H._pending_reemits.pop(ws, None)


# ----------------------------------------------------------- 3: version restore

def _restore_payload(ws, *, version, evidence):
    return {
        "workspaceId": ws, "userId": OWNER, "title": "New Client 101",
        "status": "active", "workspaceRole": "PETITIONER",
        "evidenceFiles": evidence, "deletedDocumentIds": [],
        "restoredDocumentIds": [], "versionId": version,
        "updatedAt": "2026-10-03T09:00:00.000Z",
    }


async def check_version_restore():
    ws = _ws("restore")
    _reset(ws)
    LIVE_DOCS[ws] = [DOC_A, DOC_B]
    H._workspace_document_assessments_payload[ws] = {
        "featureType": "document_assessments", "workspaceId": ws, "userId": OWNER,
        "payload": {"documentAssessments": [
            {"documentId": DOC_A, "fileName": "FIR.pdf", "success": True},
            {"documentId": DOC_B, "fileName": "Notice.pdf", "success": True}]}}
    # Backend delta deliberately wrong: evidenceFiles must win.
    payload = _restore_payload(ws, version="v_3", evidence=[
        _evidence(DOC_A, "FIR.pdf"), _evidence(DOC_C, "Reply.pdf")])
    payload["deletedDocumentIds"] = [DOC_A]
    await H.handle("workspace_version_restore", payload)

    deletes = [c for c in CALLS if c[0] == "delete_documents" and c[1] == ws]
    ingests = [c for c in CALLS if c[0] == "process_batch" and c[1] == ws]
    check("a live document absent from evidenceFiles is deleted (delta ignored)",
          deletes == [("delete_documents", ws, [DOC_B])], detail=str(deletes))
    check("a document in evidenceFiles but not live is ingested, only that one",
          ingests == [("process_batch", ws, [DOC_C])], detail=str(ingests))
    check("the live store now matches evidenceFiles",
          sorted(LIVE_DOCS.get(ws, [])) == sorted([DOC_A, DOC_C]))
    manifest = json.dumps(H._workspace_document_assessments_payload.get(ws) or {})
    check("the assessments manifest drops the deleted and adds the ingested",
          DOC_B not in manifest and DOC_A in manifest and DOC_C in manifest)
    check("the deleted document is recorded as a removed-evidence gap",
          DOC_B in json.dumps(H._workspace_removed_documents.get(ws) or {}, default=str))
    check("the full suite is regenerated",
          ("strategy", ws) in CALLS and ("analysis", ws) in CALLS
          and any(c[0] == "schedule_tail" and c[1] == ws for c in CALLS))

    calls_before = len(CALLS)
    emits_before = len(EMITS)
    await H.handle("workspace_version_restore", payload)
    check("a repeated versionId inside the window is swallowed",
          len(CALLS) == calls_before and len(EMITS) == emits_before,
          detail=str(CALLS[calls_before:]))


# -------------------------------------------------------- 4: reconnect delivery

async def check_reconnect():
    ws1, ws2, ws_old = _ws("rc_one"), _ws("rc_two"), _ws("rc_old")
    _reset(ws1, ws2, ws_old)
    H._pending_ai_responses.clear()
    H._pending_reemits.clear()

    replays = []
    original_replay = H._reemit_cached_features

    async def _replay(workspace_id, user_id):
        replays.append({
            "ws": workspace_id, "user": user_id, "scope": RC._RUN_SCOPE.get(),
            "registered": asyncio.current_task() in (
                H._workspace_run_tasks.get(workspace_id) or {}),
        })
    H._reemit_cached_features = _replay
    try:
        # Two chats, two undelivered replies: both buffered (was one slot).
        SOCKET.fail_chats.update({"chat_one", "chat_two"})
        for chat, ws in (("chat_one", ws1), ("chat_two", ws2)):
            payload = {"chatId": chat, "workspaceId": ws, "content": "fictional reply"}
            delivered = await H._socket_client.emit_ai_response(payload)
            if not delivered:  # the chat handler's buffering rule
                H._pending_ai_responses[chat] = payload
        check("two chats' undelivered replies are both buffered (bug 5)",
              set(H._pending_ai_responses) == {"chat_one", "chat_two"})

        # chat_two keeps failing; chat_one goes through on reconnect.
        SOCKET.fail_chats.discard("chat_one")
        H._arm_reconnect_reemit(ws1, OWNER)
        H._arm_reconnect_reemit(ws2, NEW_OWNER)
        H._pending_reemits[ws_old] = (OWNER, time.time() - WE._REEMIT_WINDOW_SECONDS - 60)
        await H.handle("disconnect", {})
        await H.handle("connected", {"clientType": "AI_Maverick"})
        await _until(lambda: len(replays) >= 2)
        await asyncio.sleep(0.05)

        sent = [p.get("chatId") for e, p in EMITS if e == "ai_response"]
        check("on connect the reply is re-sent even though a panel replay was pending",
              sent == ["chat_one"], detail=str(sent))
        check("a reply that fails again stays buffered for the next connect",
              set(H._pending_ai_responses) == {"chat_two"})
        replayed = sorted((r["ws"], r["user"]) for r in replays)
        check("the replay covers every workspace armed inside the window (bug 5)",
              replayed == sorted([(ws1, OWNER), (ws2, NEW_OWNER)]), detail=str(replayed))
        check("a workspace armed longer ago than the window is skipped",
              all(r["ws"] != ws_old for r in replays))
        check("each replay runs in its own workspace's run scope (bug 3)",
              all(r["scope"] and r["scope"][0] == r["ws"] for r in replays))
        check("and is registered as a run task, so a stop can cancel it (bug 3)",
              all(r["registered"] for r in replays))
        check("the replay is armed once per disconnect (cleared after)",
              not H._pending_reemits)

        # Next connect: the still-buffered reply is retried and now delivered.
        SOCKET.fail_chats.clear()
        await H.handle("connected", {"clientType": "AI_Maverick"})
        sent = [p.get("chatId") for e, p in EMITS if e == "ai_response"]
        check("the still-buffered reply is delivered on the following connect",
              sent == ["chat_one", "chat_two"] and not H._pending_ai_responses,
              detail=str(sent))
    finally:
        H._reemit_cached_features = original_replay
        H._pending_ai_responses.clear()
        H._pending_reemits.clear()
        H._had_disconnect = False


# ------------------------------------------------------------------- 5: role

async def check_role_default():
    cls = type(H)
    ws = _ws("role_unknown")
    _reset(ws)
    mark = len(LOGS)
    role = await cls._resolve_workspace_role(H, ws, OWNER)
    check("nothing known about the role → PETITIONER (bug 1)", role == "PETITIONER",
          detail=role)
    check("the fallback is logged as a WARNING",
          any(lvl == "WARNING" and "defaulting to PETITIONER" in m
              for lvl, m in _logs_since(mark)))
    role = await cls._resolve_workspace_role(H, ws, OWNER, preferred_role="respondent")
    check("an explicit valid role still wins", role == "RESPONDENT")

    # workspace_created with an empty / invalid role: the REAL role memory.
    stub_remember = H._remember_workspace_role
    del H._remember_workspace_role
    try:
        for label, sent in (("an empty", ""), ("an invalid", "WIZARD")):
            ws = _ws(f"role_created_{label.split()[-1]}")
            _reset(ws)
            await H.handle("workspace_created", {
                "workspaceId": ws, "userId": OWNER, "title": "New Client 101",
                "workspaceRole": sent, "evidenceFiles": [],
                "createdAt": "2026-10-04T09:00:00.000Z"})
            stored = H._role_cache_by_user_workspace.get(f"{ws}:{OWNER}")
            ctx = context_manager.get_context(ws)
            check(f"workspace_created with {label} role stores PETITIONER (bug 1)",
                  stored == "PETITIONER"
                  and ("upsert_role", ws, "PETITIONER") in CALLS,
                  detail=f"stored={stored}")
            check(f"and the context and role agent get PETITIONER ({label} role)",
                  ctx is not None and ctx.workspace_role == "PETITIONER"
                  and ("init_role", ws, "PETITIONER") in CALLS)
    finally:
        H._remember_workspace_role = stub_remember


# -------------------------------------------------------------- 6: log safety

async def check_log_safety():
    try:
        CaseLawDocumentRequest(workspaceId=["not", "a", "string", SECRET],
                               userId=OWNER, documentUrl=SECRET)
        err = None
    except ValidationError as e:
        err = e
    check("the planted value really is in pydantic's own message (control)",
          err is not None and SECRET in str(err))
    fields = validation_fields(err) if err else ""
    check("validation_fields names the field and error type",
          "workspaceId" in fields and "ValidationError" in fields, detail=fields)
    check("validation_fields never contains the input value", SECRET not in fields,
          detail=fields)
    check("a non-pydantic error is reduced to its type name",
          validation_fields(RuntimeError(SECRET)) == "RuntimeError")

    ws = _ws("logs")
    _reset(ws)

    # case_law_document_request: invalid payload.
    mark = len(LOGS)
    await H.handle("case_law_document_request", {
        "requestId": "cl_1", "workspaceId": ws, "userId": OWNER,
        "documentName": SECRET})  # documentUrl missing
    check("case_law_document_request: invalid payload logs no input values",
          not _leaked(mark), detail=str(_leaked(mark))[:300])
    check("and still answers the request (accepted=false)",
          any(e == "case_law_document_response" and p.get("accepted") is False
              for e, p in EMITS))

    # case_law_document_request: valid, the file name is logged as a size only.
    mark = len(LOGS)
    await H.handle("case_law_document_request", {
        "requestId": "cl_2", "workspaceId": ws, "userId": OWNER,
        "documentUrl": "", "documentName": SECRET + ".pdf"})
    check("case_law_document_request logs the file name as chars(), not the name",
          not _leaked(mark) and any("case_law_document_request: workspace=" in m
                                    and "chars>" in m for _, m in _logs_since(mark)),
          detail=str(_leaked(mark))[:300])

    # applicable_law_relevance_request: invalid payload (law missing fields).
    mark = len(LOGS)
    await H.handle("applicable_law_relevance_request", {
        "workspaceId": ws, "userId": OWNER, "requestId": "lr_1",
        "law": {"title": SECRET, "description": SECRET, "section": ["bad", SECRET]}})
    check("applicable_law_relevance_request: invalid payload logs no input values",
          not _leaked(mark), detail=str(_leaked(mark))[:300])

    # global_template_sync / global_template_sync_response: invalid payloads.
    # The planted values sit in fields the validator rejects (template entries,
    # a non-int count, an unknown action).
    def _secret_lines(mark, prefix):
        return [m for _, m in _logs_since(mark)
                if m.startswith(prefix) and SECRET in m]

    mark = len(LOGS)
    await H.handle("global_template_sync", {
        "action": SECRET,
        "templates": [{"name": SECRET, "fileUrl": SECRET}]})  # id, version missing
    check("global_template_sync: the received and invalid-payload lines log no "
          "input values",
          not _secret_lines(mark, "global_template_sync"),
          detail=str(_secret_lines(mark, "global_template_sync"))[:300])
    mark = len(LOGS)
    await H.handle("global_template_sync_response", {
        "requestId": "gts-fictional", "templates": [{"name": SECRET}],
        "count": SECRET})
    check("global_template_sync_response: the received and invalid-payload lines "
          "log no input values",
          not _secret_lines(mark, "global_template_sync_response"),
          detail=str(_secret_lines(mark, "global_template_sync_response"))[:300])


# ------------------------------------------------------------------- runner

CHECKS = [
    ("purge", check_purge),
    ("transfer", check_transfer),
    ("version-restore", check_version_restore),
    ("reconnect", check_reconnect),
    ("role-default", check_role_default),
    ("log-safety", check_log_safety),
]


def _capture(message):
    record = message.record
    text = str(record["message"])
    if record["exception"] is not None:
        text += " " + str(record["exception"].value)
    LOGS.append((record["level"].name, text))


async def _run(selected):
    _install_stubs()
    for name, fn in CHECKS:
        if selected and name not in selected:
            continue
        print(f"\n[{name}]")
        before = len(RESULTS)
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        GROUP_COUNTS[name] = len(RESULTS) - before


GROUP_COUNTS = {}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show the check groups")
    ap.add_argument("--only", nargs="*", default=None, help="run only these groups")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    # Keep the console readable (VERIFY_LOG_LEVEL=DEBUG to see everything);
    # the capture sink sees every line regardless.
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("VERIFY_LOG_LEVEL", "CRITICAL"))
    logger.add(_capture, level="DEBUG")
    asyncio.run(_run(set(args.only) if args.only else None))
    failed = [n for n, ok in RESULTS if not ok]
    print("\n" + ", ".join(f"{k}={v}" for k, v in GROUP_COUNTS.items()))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
