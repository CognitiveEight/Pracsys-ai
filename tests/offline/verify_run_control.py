#!/usr/bin/env python3
"""
verify_run_control.py — Offline checks for `workspace_stop_request` and
`workspace_resume_request`.

The lawyer clicks Stop on a running update; later, "Finish update". The rules
under test, all load-bearing:

  * STOP is a fence, not a hope — after it, nothing the stopped run does can
    reach the socket (no document_feature_response, no workspace:refresh that
    would re-arm a spinner), the pending-delivery buffer, or a resume marker;
    a NEW run started after the stop is untouched;
  * stop cancels the running update's tasks, but never a chat turn;
  * the buffered deliveries are dropped in memory AND in the persisted copy,
    and a restart does not resurface them or auto-resume the run;
  * an objective change the stop interrupted is HELD for the resume (the
    resume payload does not carry the new objective), never auto-resumed;
  * stop re-sends are expected (requestId), and a stale re-send that arrives
    after newer work must not fence the new run;
  * RESUME declares EXACTLY reEmitFeatures in workspace:refresh, produces one
    document_feature_response per feature in product order, and nothing else;
  * unknown features are dropped from both; every declared loader ends;
  * resume is idempotent on requestId (failed → re-runs), reconciles
    evidenceFiles, revokes a pending purge and applies the roster.

Exercises the real handlers through ``event_handler.handle`` (so run scopes
apply) with Qdrant, the document processor, the generators and the socket
stubbed. No API key, no backend, no Qdrant.

USAGE
    python tests/offline/verify_run_control.py            # run everything
    python tests/offline/verify_run_control.py --list     # show the check names
"""

import argparse
import asyncio
import copy
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

# Record (rather than perform) the write-through persistence, so the checks can
# assert that a stop overwrote the PERSISTED buffer, not just the memory copy.
from app.workspace import persistence as _persistence  # noqa: E402

PERSISTED = []
STORE = {}  # (workspace_id, kind) -> last persisted value, read by restore_state


def _record_persist(workspace_id, kind, data, **_kw):
    PERSISTED.append((workspace_id, kind, copy.deepcopy(data)))
    STORE[(workspace_id, kind)] = copy.deepcopy(data)


async def _restore(workspace_id, kind, **_kw):
    return copy.deepcopy(STORE.get((workspace_id, kind)))


_persistence.persist_state = _record_persist
_persistence.restore_state = _restore

from app.processing.document_processor import document_processor  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers import run_control as RC  # noqa: E402
from app.socket.schemas import (  # noqa: E402
    WorkspaceResumeRequestPayload,
    WorkspaceStopRequestPayload,
)
from app.vectorstore.qdrant import vector_store  # noqa: E402
from app.workspace import participants as P  # noqa: E402
from app.workspace.persistence import CacheKinds  # noqa: E402

RC.restore_state = _restore

RESULTS = []
EMITS = []
CALLS = []
RUN_ID = uuid.uuid4().hex[:8]
OWNER = "68b0e1f8d3a9c4b2e1f03456"
COLLAB = "68d2a7c4f1e8b3a6d0c25789"
DOC_A = "68c1f0a9e4b0d2a1c3f45671"
DOC_B = "68c1f0a9e4b0d2a1c3f45672"

PENDING = ["relevant_parties", "procedural_timeline", "case_laws", "drafting_assistant"]
#: The same four in product order — the order they are produced and emitted in
#: (each panel reads the caches of the ones before it).
PENDING_IN_ORDER = ["procedural_timeline", "relevant_parties", "case_laws",
                    "drafting_assistant"]
RECEIVED = ["workspace_metadata", "document_assessments", "strategy_objective",
            "analysis_report", "list_of_dates", "applicable_laws"]


def _ws(name):
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


# ----------------------------------------------------------------------- socket

class _FakeSocket:
    # Returns True: SocketClient.emit reports DELIVERY; a double returning None
    # reads as a dropped emit and makes handlers buffer for re-delivery.
    async def emit(self, event, payload):
        EMITS.append((event, copy.deepcopy(payload)))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", copy.deepcopy(payload)))
        return True


def _features_emitted(ws=None):
    return [p.get("featureType") for e, p in EMITS
            if e == "document_feature_response"
            and (ws is None or p.get("workspaceId") == ws)]


def _refreshes(ws=None):
    return [p for e, p in EMITS if e == "workspace:refresh"
            and (ws is None or p.get("workspaceId") == ws)]


# ------------------------------------------------------------------------ stubs

LIVE_DOCS = {}          # workspace_id -> [doc ids in the vector store]
FAIL_FEATURE = set()    # generators that raise
BLOCK = {}              # feature -> asyncio.Event the generator waits on


def _evidence(doc_id, name, *, with_id=True):
    f = {
        "originalName": name, "storeFileName": f"68c1f0a9_{name}",
        "filename": name.rsplit(".", 1)[0], "extension": name.rsplit(".", 1)[-1],
        "fileHash": uuid.uuid4().hex, "url": f"https://storage.example/{name}?sig=x",
        "path": f"workspaces/ws/evidence/{name}",
        "uploadedAt": "2026-09-29T09:10:00.000Z",
    }
    if with_id:
        f["_id"] = doc_id
    return f


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


def _install_stubs():
    async def _process_batch(files, workspace_id, user_id):
        ids = [f["_id"] for f in files]
        CALLS.append(("process_batch", sorted(ids)))
        LIVE_DOCS.setdefault(workspace_id, [])
        LIVE_DOCS[workspace_id] += [i for i in ids if i not in LIVE_DOCS[workspace_id]]
        return _batch(ids)

    async def _get_all_documents(workspace_id, limit_per_batch=100):
        return [{"document_id": i, "content": "x"} for i in LIVE_DOCS.get(workspace_id, [])]

    async def _delete_documents(workspace_id, document_ids):
        CALLS.append(("delete_documents", sorted(document_ids)))
        LIVE_DOCS[workspace_id] = [
            i for i in LIVE_DOCS.get(workspace_id, []) if i not in document_ids
        ]
        return True

    document_processor.process_batch = _process_batch
    vector_store.get_all_documents = _get_all_documents
    vector_store.delete_documents = _delete_documents

    async def _hydrate(workspace_id, *, user_id=None, recover=True):
        return None

    async def _remember(workspace_id, user_id, role, **_kw):
        return None

    async def _resolve(workspace_id, user_id, preferred_role=None, **_kw):
        return (preferred_role or "ADJUDICATOR").strip().upper()

    async def _tag(workspace_id, report, *, log_context=""):
        return report

    async def _meta(workspace_id, user_id):
        return {"caseCategory": "Criminal", "subcategories": [], "description": "d"}

    def _gen(feature, payload_fn):
        async def _generate(workspace_id, user_id, workspace_role=None, **kw):
            CALLS.append(("generate", feature, kw))
            if feature in BLOCK:
                await BLOCK[feature].wait()
            if feature in FAIL_FEATURE:
                raise RuntimeError(f"{feature} generator failed")
            await H._emit_document_feature(
                feature_type=feature, workspace_id=workspace_id,
                user_id=user_id, payload=payload_fn(workspace_id), usage=None)
        return _generate

    async def _strategy(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("generate", "strategy_objective", {}))
        return {"caseSummary": "An FIR.", "strategicObjective": "Old objective.",
                "possibleNextSteps": [], "suggestionChips": []}

    async def _analysis(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("generate", "analysis_report", {}))
        return {"missingInfo": [], "caseResolutionScore": 60}

    async def _objective(workspace_id, user_id, workspace_role,
                         user_objective_instruction, current_strategy, **_kw):
        CALLS.append(("objective_replay", user_objective_instruction))
        merged = dict(current_strategy, strategicObjective=user_objective_instruction)
        H._workspace_strategy_payload[workspace_id] = merged
        return await H._emit_document_feature(
            feature_type="strategy_objective", workspace_id=workspace_id,
            user_id=user_id, payload=merged, usage=None)

    H._hydrate_workspace_caches = _hydrate
    H._remember_workspace_role = _remember
    H._resolve_workspace_role = _resolve
    H._tag_analysis_report_against_current_state = _tag
    H._get_workspace_category_and_subcategory = _meta
    H._generate_strategies_from_documents = _strategy
    H._generate_analysis_report_from_documents = _analysis
    H._regenerate_strategy_for_new_objective = _objective
    H._generate_and_emit_list_of_dates = _gen(
        "list_of_dates", lambda ws: {"listOfDates": []})
    H._generate_and_emit_applicable_laws = _gen(
        "applicable_laws", lambda ws: {"applicableLaws": []})
    H._generate_and_emit_procedural_timeline = _gen(
        "procedural_timeline", lambda ws: {"proceduralTimeline": []})
    H._generate_and_emit_relevant_parties = _gen(
        "relevant_parties", lambda ws: {"parties": []})
    H._generate_and_emit_case_laws = _gen("case_laws", lambda ws: {"caseLaws": []})
    H._generate_and_emit_drafting_suggestions = _gen(
        "drafting_assistant", lambda ws: {"suggestedDrafts": []})
    H._socket_client = _FakeSocket()


def _reset(ws):
    EMITS.clear()
    CALLS.clear()
    PERSISTED.clear()
    FAIL_FEATURE.clear()
    BLOCK.clear()
    for d in (H._run_epochs, H._workspace_run_tasks, H._last_workspace_event_at,
              H._stop_requests, H._resume_requests, H._pending_feature_deliveries,
              H._pending_objective_changes, H._workspace_strategy_payload,
              H._workspace_current_state_payload, H._document_reload_requests,
              H._workspace_document_assessments_payload,
              H._workspace_removed_documents, H._workspace_relevant_parties_payload,
              H._workspace_case_laws_payload, H._workspace_drafting_payload,
              H._workspace_procedural_timeline_payload):
        d.pop(ws, None)
    H._recovered_workspaces.discard(ws)
    for key in [k for k in STORE if k[0] == ws]:
        STORE.pop(key, None)
    LIVE_DOCS[ws] = [DOC_A, DOC_B]
    P.clear_participants(ws)


def _stop(ws, *, request_id=None, pending=None, received=None, stopped_at=None):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "requestId": request_id or f"stp_{ws[:8]}_{uuid.uuid4().hex[:6]}",
        "workspaceId": ws, "userId": OWNER,
        "stoppedBy": {"userId": COLLAB, "name": "A Collaborator"},
        "receivedFeatures": RECEIVED if received is None else received,
        "pendingFeatures": PENDING if pending is None else pending,
        "stoppedAt": stopped_at or now_iso, "timestamp": stopped_at or now_iso,
    }


def _resume(ws, *, request_id=None, features=None, evidence=None,
            participants="absent"):
    data = {
        "requestId": request_id or f"rsm_{ws[:8]}_{uuid.uuid4().hex[:6]}",
        "workspaceId": ws, "userId": OWNER, "title": "New Client 101",
        "status": "active", "workspaceRole": "PETITIONER",
        "reEmitFeatures": list(PENDING if features is None else features),
        "evidenceFiles": evidence if evidence is not None else [
            _evidence(DOC_A, "FIR.pdf"), _evidence(DOC_B, "Notice.pdf")],
        "currentState": {"missingInfo": [{"id": "mi_1", "text": "Post-mortem report",
                                          "status": "approved"}]},
        "timestamp": "2026-09-29T10:15:03.442Z",
    }
    if participants != "absent":
        data["participants"] = participants
    return data


async def _until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# -------------------------------------------------------------------- 1: schema

def check_schema():
    stop = WorkspaceStopRequestPayload(**_stop("68c1f0a9e4b0d2a1c3f45678"))
    check("the spec's workspace_stop_request parses",
          stop.pendingFeatures == PENDING and stop.stoppedBy.userId == COLLAB)
    res = WorkspaceResumeRequestPayload(**_resume("68c1f0a9e4b0d2a1c3f45678"))
    check("the spec's workspace_resume_request parses",
          res.reEmitFeatures == PENDING and len(res.evidenceFiles) == 2
          and res.currentState is not None)
    check("both events are registered",
          "workspace_stop_request" in H._handlers
          and "workspace_resume_request" in H._handlers)


# --------------------------------------------------------------------- 2: fence

async def check_fence():
    ws, other = _ws("fence"), _ws("fence_other")
    _reset(ws)
    _reset(other)
    token = RC._RUN_SCOPE.set((ws, 0))
    try:
        H._run_epochs[ws] = 1  # a stop happened while this "run" was going
        ok_panel = await H._emit_document_feature(
            feature_type="relevant_parties", workspace_id=ws, user_id=OWNER,
            payload={"parties": []})
        await H._emit_workspace_refresh(ws, OWNER, feature_types=["case_laws"])
        check("a stopped run's panel emit is suppressed and reported undelivered",
              ok_panel is False and "relevant_parties" not in _features_emitted())
        check("a stopped run's workspace:refresh is never sent (no spinner re-arm)",
              not _refreshes(ws))
        check("nor is its output buffered for re-delivery",
              not H._pending_feature_deliveries.get(ws))
        await H._socket_client.emit_ai_response({"chatId": "c1", "workspaceId": ws})
        check("a chat answer is never fenced",
              any(e == "ai_response" for e, _ in EMITS))
        await H._emit_document_feature(
            feature_type="case_laws", workspace_id=other, user_id=OWNER,
            payload={"caseLaws": []})
        check("another workspace's emit is not fenced by this stop",
              "case_laws" in _features_emitted(other))
    finally:
        RC._RUN_SCOPE.reset(token)
    token = RC._RUN_SCOPE.set((ws, 1))  # a run that started AFTER the stop
    try:
        await H._emit_workspace_refresh(ws, OWNER, feature_types=["case_laws"])
        check("a run started after the stop arms its loaders normally",
              len(_refreshes(ws)) == 1)
    finally:
        RC._RUN_SCOPE.reset(token)


# ----------------------------------------------------------- 3: stop cancels

async def check_stop_cancels():
    ws = _ws("cancel")
    _reset(ws)
    state = {"child": None, "handler_cancelled": False}
    original = H._handlers["workspace_sync"]

    async def _fake_sync(data):
        async def _child():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # What a real pipeline does on cancel: release its loaders and
                # buffer — both must be refused by the fence.
                await H._release_refresh_loaders(
                    ws, OWNER, ["case_laws"], reason="cancelled")
                H._note_pending_feature_delivery(ws, "case_laws", {"x": 1})
                raise
        state["child"] = H._track_background_task(asyncio.create_task(_child()))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["handler_cancelled"] = True
            await H._emit_document_feature(
                feature_type="relevant_parties", workspace_id=ws, user_id=OWNER,
                payload={"parties": []})
            raise

    chat_state = {"done": False}
    original_chat = H._handlers["user_message_request"]

    async def _fake_chat(data):
        await asyncio.sleep(0.3)
        chat_state["done"] = True

    H._handlers["workspace_sync"] = _fake_sync
    H._handlers["user_message_request"] = _fake_chat
    try:
        run = asyncio.create_task(H.handle("workspace_sync", {"workspaceId": ws}))
        chat = asyncio.create_task(
            H.handle("user_message_request", {"workspaceId": ws}))
        await _until(lambda: state["child"] is not None)
        check("the update's handler and its spawned task are registered",
              len(H._workspace_run_tasks.get(ws) or {}) == 2)
        await H.handle("workspace_stop_request", _stop(ws))
        await asyncio.sleep(0)
        check("stop cancels the running update's handler",
              run.done() and state["handler_cancelled"])
        check("stop cancels the tasks it spawned", state["child"].cancelled())
        check("nothing the cancelled run emitted on its way out got through",
              not _features_emitted(ws))
        check("nothing it tried to buffer was kept",
              not H._pending_feature_deliveries.get(ws))
        await chat
        check("a chat turn is NOT cancelled by stop", chat_state["done"])
        check("the task registry is empty afterwards",
              not H._workspace_run_tasks.get(ws))

        born_stale = {}
        token = RC._RUN_SCOPE.set((ws, 0))
        try:
            born_stale["t"] = H._track_background_task(
                asyncio.create_task(asyncio.sleep(3600)))
        finally:
            RC._RUN_SCOPE.reset(token)
        await asyncio.sleep(0)
        check("a task spawned by the stopped run after the stop is cancelled at birth",
              born_stale["t"].cancelled())
    finally:
        H._handlers["workspace_sync"] = original
        H._handlers["user_message_request"] = original_chat


# ----------------------------------------------------- 4: buffers and markers

async def check_buffers_and_markers():
    ws = _ws("buffers")
    _reset(ws)
    H._pending_feature_deliveries[ws] = {
        "case_laws": {"featureType": "case_laws", "workspaceId": ws, "payload": {}}}
    H._pending_objective_changes[ws] = {
        "userId": OWNER, "workspaceRole": "PETITIONER",
        "instruction": "Seek anticipatory bail instead of quashing",
        "attempts": 0, "startedAt": time.time()}
    H._recovered_workspaces.add(ws)
    PERSISTED.clear()
    await H.handle("workspace_stop_request",
                   _stop(ws, pending=["strategy_objective"] + PENDING, received=[]))
    check("buffered deliveries are dropped in memory",
          H._pending_feature_deliveries.get(ws) == {})
    check("and the PERSISTED buffer is overwritten, so a restart finds nothing",
          any(w == ws and k == CacheKinds.PENDING_FEATURE_DELIVERY and d == {}
              for w, k, d in PERSISTED))
    marker = H._pending_objective_changes.get(ws) or {}
    check("an interrupted objective change is HELD, not dropped",
          marker.get("heldByStop") is True
          and marker.get("instruction") == "Seek anticipatory bail instead of quashing")
    check("the stop sends no reply", not EMITS)

    # Strategy already received → the objective landed; nothing to hold.
    ws2 = _ws("buffers_landed")
    _reset(ws2)
    H._pending_objective_changes[ws2] = {
        "userId": OWNER, "instruction": "x", "attempts": 0, "startedAt": time.time()}
    H._recovered_workspaces.add(ws2)
    await H.handle("workspace_stop_request", _stop(ws2))  # strategy in received
    check("an objective change whose strategy was received is cleared, not held",
          not (H._pending_objective_changes.get(ws2) or {}).get("instruction"))

    # A fenced objective regeneration (the chat turn is not cancelled) holds too.
    ws3 = _ws("buffers_fenced")
    _reset(ws3)
    token = RC._RUN_SCOPE.set((ws3, 0))
    try:
        H._run_epochs[ws3] = 1
        H._note_pending_objective_change(
            workspace_id=ws3, user_id=OWNER, workspace_role="PETITIONER",
            instruction="Settle through mediation")
    finally:
        RC._RUN_SCOPE.reset(token)
    check("a marker written by a stopped run is written HELD",
          (H._pending_objective_changes.get(ws3) or {}).get("heldByStop") is True)


# ------------------------------------------------------------------ 5: restart

async def check_restart():
    ws = _ws("restart")
    _reset(ws)
    held = {"userId": OWNER, "workspaceRole": "PETITIONER",
            "instruction": "Seek anticipatory bail", "attempts": 0,
            "startedAt": time.time(), "heldByStop": True, "heldAt": time.time()}
    STORE[(ws, CacheKinds.PENDING_FEATURE_DELIVERY)] = {}
    STORE[(ws, CacheKinds.PENDING_OBJECTIVE_CHANGE)] = held
    H._recovered_workspaces.discard(ws)
    await H._recover_workspace_after_hydrate(ws, OWNER)
    await asyncio.sleep(0.05)
    check("after a restart the held objective is restored into memory",
          (H._pending_objective_changes.get(ws) or {}).get("heldByStop") is True)
    check("but NOT auto-resumed (no refresh, no regeneration)",
          not _refreshes(ws) and not any(c[0] == "objective_replay" for c in CALLS))
    resumed = await H._resume_pending_objective_changes()
    check("the reconnect sweep skips a held objective change",
          resumed == 0 and not _refreshes(ws))

    # Cold stop: the pre-restart marker lives only in Qdrant.
    ws2 = _ws("restart_cold")
    _reset(ws2)
    STORE[(ws2, CacheKinds.PENDING_OBJECTIVE_CHANGE)] = {
        "userId": OWNER, "instruction": "Seek bail", "attempts": 0,
        "startedAt": time.time()}
    STORE[(ws2, CacheKinds.PENDING_FEATURE_DELIVERY)] = {
        "case_laws": {"featureType": "case_laws"}}
    await H.handle("workspace_stop_request",
                   _stop(ws2, pending=["strategy_objective"], received=[]))
    check("a stop on a cold process holds the persisted objective marker",
          (STORE.get((ws2, CacheKinds.PENDING_OBJECTIVE_CHANGE)) or {}).get("heldByStop")
          is True)
    check("and empties the persisted buffer it never had in memory",
          STORE.get((ws2, CacheKinds.PENDING_FEATURE_DELIVERY)) == {})
    check("and marks the workspace recovered, so hydration cannot flush it",
          ws2 in H._recovered_workspaces)


# ------------------------------------------------------------------- 6: dedupe

async def check_stop_dedupe():
    ws = _ws("stop_dedupe")
    _reset(ws)
    data = _stop(ws, request_id="stp_dupe_1")
    await H.handle("workspace_stop_request", data)
    epoch = H._run_epoch(ws)
    await H.handle("workspace_stop_request", data)
    check("a duplicate stop (same requestId) is a no-op", H._run_epoch(ws) == epoch)

    ws2 = _ws("stop_stale")
    _reset(ws2)
    H._last_workspace_event_at[ws2] = time.time()  # the lawyer uploaded since
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 600))
    await H.handle("workspace_stop_request", _stop(ws2, stopped_at=old))
    check("a stale stop re-sent after newer work does not fence the new run",
          H._run_epoch(ws2) == 0)

    ws4 = _ws("stop_polling")
    _reset(ws4)
    original = H._handlers["probing_prompts_request"]

    async def _noop(data):
        return None

    H._handlers["probing_prompts_request"] = _noop
    try:
        await H.handle("probing_prompts_request", {"workspaceId": ws4})
    finally:
        H._handlers["probing_prompts_request"] = original
    await H.handle("workspace_stop_request", _stop(ws4, stopped_at=old))
    check("polling traffic is not newer work: a late stop still applies",
          H._run_epoch(ws4) == 1)

    ws3 = _ws("stop_malformed")
    _reset(ws3)
    await H.handle("workspace_stop_request",
                   {"workspaceId": ws3, "pendingFeatures": "not-a-list"})
    check("a malformed stop still stops", H._run_epoch(ws3) == 1)


# ------------------------------------------------------------------- 7: resume

async def check_resume_contract():
    ws = _ws("resume")
    _reset(ws)
    H._workspace_strategy_payload[ws] = {"caseSummary": "An FIR."}
    await H.handle("workspace_resume_request", _resume(ws))
    refreshes = _refreshes(ws)
    check("exactly one workspace:refresh is emitted", len(refreshes) == 1)
    declared = (refreshes or [{}])[0].get("featureTypes") or []
    check("it declares EXACTLY the reEmitFeatures set — no more, no fewer",
          sorted(declared) == sorted(PENDING) and len(declared) == len(PENDING),
          detail=str(declared))
    check("the refresh comes before any feature response",
          EMITS and EMITS[0][0] == "workspace:refresh")
    check("one document_feature_response per resumed feature, in product order",
          _features_emitted(ws) == PENDING_IN_ORDER, detail=str(_features_emitted(ws)))
    check("none of the six settled features is re-emitted",
          not set(_features_emitted(ws)) & set(RECEIVED))
    check("currentState is cached for isMatched tagging",
          (H._workspace_current_state_payload.get(ws) or {}).get("missingInfo"))

    _reset(ws)
    await H.handle("workspace_resume_request", _resume(
        ws, features=["drafting_assistant", "not_a_feature", "relevant_parties"]))
    check("unknown features are dropped and the rest run in product order",
          (_refreshes(ws) or [{}])[0].get("featureTypes")
          == ["relevant_parties", "drafting_assistant"]
          and _features_emitted(ws) == ["relevant_parties", "drafting_assistant"])

    _reset(ws)
    await H.handle("workspace_resume_request", _resume(ws, features=["not_a_feature"]))
    check("nothing resumable → no refresh and no work", not EMITS)


async def check_resume_failures_and_dedupe():
    ws = _ws("resume_fail")
    _reset(ws)
    H._workspace_case_laws_payload[ws] = {"caseLaws": [{"id": "cl_1"}]}
    FAIL_FEATURE.add("case_laws")
    await H.handle("workspace_resume_request", _resume(ws, request_id="rsm_f1"))
    emitted = _features_emitted(ws)
    check("a failing generator still ends its loader (released from cache)",
          emitted.count("case_laws") == 1 and set(emitted) == set(PENDING),
          detail=str(emitted))

    EMITS.clear()
    await H.handle("workspace_resume_request", _resume(ws, request_id="rsm_f1"))
    check("a re-send of a completed resume is swallowed", not EMITS)

    H._resume_requests[ws]["rsm_f1"]["status"] = "failed"
    FAIL_FEATURE.clear()
    await H.handle("workspace_resume_request", _resume(ws, request_id="rsm_f1"))
    check("a re-send after a FAILED resume runs again",
          _features_emitted(ws) == PENDING_IN_ORDER, detail=str(_features_emitted(ws)))


async def check_resume_objective():
    ws = _ws("resume_objective")
    _reset(ws)
    H._workspace_strategy_payload[ws] = {"caseSummary": "An FIR.",
                                         "strategicObjective": "Old objective."}
    H._pending_objective_changes[ws] = {
        "userId": OWNER, "workspaceRole": "PETITIONER",
        "instruction": "Seek anticipatory bail", "attempts": 0,
        "startedAt": time.time(), "heldByStop": True, "heldAt": time.time()}
    await H.handle("workspace_resume_request", _resume(
        ws, features=["strategy_objective", "analysis_report"]))
    check("resume replays the objective change the stop held",
          ("objective_replay", "Seek anticipatory bail") in CALLS)
    check("so the new objective lands (not the old one)",
          H._workspace_strategy_payload[ws].get("strategicObjective")
          == "Seek anticipatory bail")
    check("and the held marker is consumed",
          not (H._pending_objective_changes.get(ws) or {}).get("instruction"))

    _reset(ws)
    H._workspace_strategy_payload[ws] = {"caseSummary": "An FIR."}
    H._pending_objective_changes[ws] = {
        "instruction": "Seek bail", "heldByStop": True, "heldAt": time.time()}
    real_replay = H._regenerate_strategy_for_new_objective

    async def _failing_replay(**_kw):
        return False

    H._regenerate_strategy_for_new_objective = _failing_replay
    try:
        await H.handle("workspace_resume_request",
                       _resume(ws, features=["strategy_objective"]))
    finally:
        H._regenerate_strategy_for_new_objective = real_replay
    check("a replay that does not land keeps the objective held for a retry",
          (H._pending_objective_changes.get(ws) or {}).get("heldByStop") is True)
    check("and its loader still ends", _features_emitted(ws) == ["strategy_objective"])

    _reset(ws)
    H._pending_objective_changes[ws] = {
        "instruction": "Seek bail", "heldByStop": True, "heldAt": time.time()}
    original = H._handlers["workspace_sync"]

    async def _noop(data):
        return None

    H._handlers["workspace_sync"] = _noop
    try:
        await H.handle("workspace_sync", {"workspaceId": ws})
    finally:
        H._handlers["workspace_sync"] = original
    check("a new full run (workspace_sync) drops a held objective change",
          not (H._pending_objective_changes.get(ws) or {}).get("instruction"))


async def check_resume_reconcile_and_lifecycle():
    ws = _ws("resume_reconcile")
    _reset(ws)
    LIVE_DOCS[ws] = [DOC_A, "orphan_doc"]
    H._workspace_document_assessments_payload[ws] = {
        "featureType": "document_assessments", "workspaceId": ws, "userId": OWNER,
        "payload": {"documentAssessments": [
            {"documentId": DOC_A, "filename": "FIR", "success": True},
            {"documentId": "orphan_doc", "filename": "Old", "success": True,
             "documentType": "notice"}]}}
    H._pending_purges[ws] = {"requestId": "wd_1", "task": None}
    await H.handle("workspace_resume_request", _resume(
        ws, participants=[{"userId": COLLAB, "name": "A Collaborator",
                           "permission": "view"}]))
    check("a document missing from the store is ingested",
          ("process_batch", [DOC_B]) in CALLS)
    check("an indexed document no longer in evidenceFiles is deleted",
          ("delete_documents", ["orphan_doc"]) in CALLS)
    check("and recorded as a removed-evidence gap",
          any(e.get("documentId") == "orphan_doc"
              for e in H._workspace_removed_documents.get(ws) or []))
    check("a pending compliance purge is revoked by resume",
          ws not in H._pending_purges)
    check("the roster is applied", (P.get_participants(ws) or [{}])[0].get("userId")
          == COLLAB)

    _reset(ws)
    LIVE_DOCS[ws] = [DOC_A, "orphan_doc"]
    await H.handle("workspace_resume_request", _resume(
        ws, evidence=[_evidence(DOC_A, "FIR.pdf"),
                      _evidence(None, "NoId.pdf", with_id=False)]))
    check("an id-less evidence file blocks deletions (nothing guessed away)",
          not any(c[0] == "delete_documents" for c in CALLS))

    _reset(ws)
    LIVE_DOCS[ws] = [DOC_A, DOC_B]
    H._workspace_document_assessments_payload[ws] = {
        "featureType": "document_assessments", "workspaceId": ws, "userId": OWNER,
        "payload": {"documentAssessments": [{"documentId": DOC_A, "success": True}]}}
    await H.handle("workspace_resume_request", _resume(
        ws, features=["document_assessments"] + PENDING))
    check("a stopped ingestion is re-read: indexed but unassessed → re-ingested",
          ("delete_documents", [DOC_B]) in CALLS and ("process_batch", [DOC_B]) in CALLS)
    check("and document_assessments is emitted first, once",
          _features_emitted(ws)[:1] == ["document_assessments"]
          and _features_emitted(ws).count("document_assessments") == 1)


# ----------------------------------------------------------- 8: stop a resume

async def check_stop_during_resume():
    ws = _ws("stop_resume")
    _reset(ws)
    BLOCK["procedural_timeline"] = asyncio.Event()
    resume = asyncio.create_task(H.handle(
        "workspace_resume_request", _resume(ws, request_id="rsm_s1")))
    await _until(lambda: any(c[:2] == ("generate", "procedural_timeline") for c in CALLS))
    before = list(_features_emitted(ws))
    await H.handle("workspace_stop_request", _stop(ws))
    await asyncio.sleep(0.05)
    check("a stop cancels a running resume", resume.done())
    check("no loader release is emitted after the stop",
          _features_emitted(ws) == before, detail=f"{before} → {_features_emitted(ws)}")
    check("the resume is marked stopped, so a re-send can run it again",
          (H._resume_requests.get(ws) or {}).get("rsm_s1", {}).get("status") == "stopped")


# ------------------------------------------------------------------- runner

CHECKS = [
    ("schema", check_schema),
    ("fence", check_fence),
    ("stop-cancels", check_stop_cancels),
    ("buffers-markers", check_buffers_and_markers),
    ("restart", check_restart),
    ("stop-dedupe", check_stop_dedupe),
    ("resume-contract", check_resume_contract),
    ("resume-failures", check_resume_failures_and_dedupe),
    ("resume-objective", check_resume_objective),
    ("resume-reconcile", check_resume_reconcile_and_lifecycle),
    ("stop-during-resume", check_stop_during_resume),
]


async def _run(selected):
    _install_stubs()
    for name, fn in CHECKS:
        if selected and name not in selected:
            continue
        print(f"\n[{name}]")
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the check names")
    ap.add_argument("--only", nargs="*", default=None, help="run only these checks")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    asyncio.run(_run(set(args.only) if args.only else None))
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
