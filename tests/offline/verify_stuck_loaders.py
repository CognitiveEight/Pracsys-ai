#!/usr/bin/env python3
"""
verify_stuck_loaders.py — Offline checks for SIM-163 Phase -1 (stuck loaders).

A panel loader armed by ``workspace:refresh`` clears only when a
``document_feature_response`` for the same featureType arrives. These checks
pin the rules that keep every armed loader paired with a terminal emit, and
keep the per-workspace pipeline lock from being held forever:

  * template sync is bounded (TEMPLATE_SYNC_TIMEOUT_SECONDS), an undelivered
    request returns at once, an invalid reply releases its waiter, and the
    round trip runs OUTSIDE the pipeline lock (drafting waits on it only
    TEMPLATE_SYNC_IN_LOCK_WAIT_SECONDS);
  * a lock-hold watchdog reports a hold past PIPELINE_LOCK_WARN_SECONDS with
    its holder and stage, rate-limited, and releases nothing;
  * every failed tail step, a cancelled runner, a strategy that returns
    nothing, a process_batch crash and an all-files-failed upload settle their
    armed panels from cache, exactly once;
  * one run's lock hold is capped (PIPELINE_RUN_TIMEOUT_SECONDS, extended for
    audio): at the cap it is cut, logged CRITICAL with its stage, audited,
    its unemitted panels settled, and queued work then runs.

Group names match the AC table in
specs/acceptance-criteria/SIM-163-incremental-update.md (AC-100..AC-120,
except AC-113/AC-114, which need `make e2e`).

Runs on an event loop with a FAKE CLOCK: ``_advance(s)`` moves loop.time()
forward, so asyncio.timeout / wait_for / sleep expire without waiting.
Exercises the real runners, tail, sync handler and template sync with the
socket, Qdrant, the document processor and the generators stubbed. No API
key, no backend, no Qdrant. Fictional data only.

USAGE
    python tests/offline/verify_stuck_loaders.py            # run everything
    python tests/offline/verify_stuck_loaders.py --list     # show the group names
    python tests/offline/verify_stuck_loaders.py --only stuck_run_cap
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

# Record (rather than perform) the write-through persistence.
from app.workspace import persistence as _persistence  # noqa: E402

STORE = {}


def _record_persist(workspace_id, kind, data, **_kw):
    STORE[(workspace_id, kind)] = copy.deepcopy(data)


async def _restore(workspace_id, kind, **_kw):
    return copy.deepcopy(STORE.get((workspace_id, kind)))


_persistence.persist_state = _record_persist
_persistence.restore_state = _restore

from loguru import logger  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.observability.audit import audit_logger  # noqa: E402
from app.processing.document_processor import document_processor  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers import run_control as RC  # noqa: E402
from app.socket.handlers._shared import UPLOAD_REFRESH_FEATURES  # noqa: E402
from app.socket.schemas import UserMessageRequestPayload  # noqa: E402
from app.vectorstore.qdrant import vector_store  # noqa: E402
from app.workspace import participants as P  # noqa: E402

RC.restore_state = _restore

RESULTS = []
EMITS = []          # (event, payload, pipeline-lock-held-for-its-workspace)
CALLS = []
LOGS = []           # (level, message)
AUDITS = []         # (event_type, data)
RUN_ID = uuid.uuid4().hex[:8]
OWNER = "68b0e1f8d3a9c4b2e1f03456"
NEW_OWNER = "68b0e1f8d3a9c4b2e1f09999"
DOC_A = "68c1f0a9e4b0d2a1c3f45671"
DOC_C = "68c1f0a9e4b0d2a1c3f45673"
#: Must never reach the wire or a log line.
LEAK_SENTINEL = "ZQX-SENTINEL-" + RUN_ID

CACHED = "ORIGIN_CACHED"
FRESH = "ORIGIN_FRESH"
ARMED = list(UPLOAD_REFRESH_FEATURES)
RELEASABLE = [f for f in ARMED if f != "document_assessments"]
TAIL = ["list_of_dates", "applicable_laws", "procedural_timeline",
        "relevant_parties", "case_laws", "drafting_assistant"]


def _ws(name):
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ fake clock

class _FakeClockLoop(asyncio.SelectorEventLoop):
    """loop.time() = real monotonic + an offset the checks move forward."""

    def __init__(self):
        super().__init__()
        self.offset = 0.0

    def time(self):
        return super().time() + self.offset


def _advance(seconds):
    asyncio.get_running_loop().offset += float(seconds)


async def _settle(n=60):
    for _ in range(n):
        await asyncio.sleep(0)


async def _until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# ------------------------------------------------------------------ socket

DROP = set()        # events the fake socket reports as NOT delivered


class _FakeSocket:
    # Returns True unless scripted: SocketClient.emit reports DELIVERY.
    async def emit(self, event, payload):
        ws = str((payload or {}).get("workspaceId") or "")
        locked = bool(ws) and H._workspace_pipeline_lock(ws).locked()
        EMITS.append((event, copy.deepcopy(payload), locked))
        return event not in DROP

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", copy.deepcopy(payload), False))
        return True


def _emits(ws, feature=None, since=0):
    return [p for e, p, _l in EMITS[since:]
            if e == "document_feature_response" and p.get("workspaceId") == ws
            and (feature is None or p.get("featureType") == feature)]


def _count(ws, feature, since=0, until=None):
    return len([p for e, p, _l in EMITS[since:until]
                if e == "document_feature_response" and p.get("workspaceId") == ws
                and p.get("featureType") == feature])


def _origin(envelope):
    text = json.dumps(envelope.get("payload"), default=str)
    if CACHED in text:
        return "cached"
    if FRESH in text:
        return "fresh"
    return "other"


def _template_requests():
    return [p for e, p, _l in EMITS if e == "request_template_sync"]


def _logged(fragment, since=0, level=None):
    return [m for lv, m in LOGS[since:] if fragment in m and (level is None or lv == level)]


# ------------------------------------------------------------------ stubs

LIVE_DOCS = {}
FAIL = set()            # generator raises before emitting
FAIL_AFTER = set()      # generator emits, then raises
BLOCK = {}              # feature -> asyncio.Event the generator waits on
SLOW = {}               # feature -> seconds the generator sleeps (fake clock)
STRATEGY = {"mode": "ok"}
INGEST = {"mode": "ok"}
TEMPLATES = {"count": 1}
HANDLE_TURN = []
REEMITS = []
CANCELLED = []          # features whose generator was cancelled mid-call
VIA_EMIT = []           # (workspace, featureType) passed to _emit_document_feature


def _seed(ws):
    H._workspace_strategy_payload[ws] = {
        "caseSummary": "A fictional cheque dispute.", "strategicObjective": CACHED,
        "possibleNextSteps": [], "suggestionChips": []}
    H._workspace_analysis_payload[ws] = {
        "missingInfo": [], "caseResolutionScore": 41, "qualityScoreExplanation": CACHED}
    H._workspace_list_of_dates_payload[ws] = [
        {"date": "2025-01-02", "event": "Notice issued", "note": CACHED}]
    H._workspace_applicable_laws_payload[ws] = {"applicableLaws": [], "note": CACHED}
    H._workspace_procedural_timeline_payload[ws] = [
        {"date": "2026-02-03", "event": "Hearing", "note": CACHED}]
    H._workspace_relevant_parties_payload[ws] = {"parties": [], "note": CACHED}
    H._workspace_case_laws_payload[ws] = {"caseLaws": [], "note": CACHED}
    H._workspace_drafting_payload[ws] = {"suggestedDrafts": [
        {"title": "Reply to notice", "description": CACHED}]}


def _batch(ids, *, success=True, extra=None):
    results = []
    for i in ids:
        r = {"document_id": i, "filename": i, "success": success,
             "readability_score": 90, "readability_rating": "Excellent",
             "readable": True, "extracted_text_length": 100,
             "stored_in_vector_db": success, "summary": "s",
             "evidentiary_strength": "High", "document_type": "notice",
             "extracted_fields": {}, "sensitive_fields": {}}
        if not success:
            r["error"] = "unreadable"
        r.update(extra or {})
        results.append(r)
    ok = len(ids) if success else 0
    return {"total_files": len(ids), "successful": ok, "failed": len(ids) - ok,
            "average_readability_score": 90, "results": results}


def _evidence(doc_id, name):
    return {
        "_id": doc_id, "originalName": name, "storeFileName": f"68c1f0a9_{name}",
        "filename": name.rsplit(".", 1)[0], "extension": name.rsplit(".", 1)[-1],
        "fileHash": uuid.uuid4().hex, "url": f"https://storage.example/{name}?sig=x",
        "path": f"workspaces/ws/evidence/{name}",
        "uploadedAt": "2026-10-20T09:10:00.000Z",
    }


def _install_stubs():
    async def _process_batch(files, workspace_id, user_id, **_kw):
        ids = [f["_id"] for f in files]
        CALLS.append(("process_batch", sorted(ids)))
        if INGEST["mode"] == "crash":
            raise RuntimeError("ingest crashed")
        if INGEST["mode"] == "fail":
            return _batch(ids, success=False)
        LIVE_DOCS.setdefault(workspace_id, [])
        LIVE_DOCS[workspace_id] += [i for i in ids if i not in LIVE_DOCS[workspace_id]]
        return _batch(ids)

    async def _get_all_documents(workspace_id, limit_per_batch=100):
        return [{"document_id": i, "content": "x"} for i in LIVE_DOCS.get(workspace_id, [])]

    async def _delete_documents(workspace_id, document_ids):
        return True

    document_processor.process_batch = _process_batch
    vector_store.get_all_documents = _get_all_documents
    vector_store.delete_documents = _delete_documents

    async def _hydrate(workspace_id, *, user_id=None, recover=True):
        return None

    async def _remember(workspace_id, user_id, role, **_kw):
        return None

    async def _resolve(workspace_id, user_id, preferred_role=None, **_kw):
        return (preferred_role or "PETITIONER").strip().upper()

    async def _tag(workspace_id, report, *, log_context=""):
        return report

    async def _meta(workspace_id, user_id):
        return {"caseCategory": "Civil", "subcategories": [], "description": "d"}

    async def _chips(**_kw):
        return []

    async def _explanation(**_kw):
        return "A fictional explanation."

    async def _reeval(**_kw):
        return False

    def _gen(feature, payload_fn):
        async def _generate(workspace_id, user_id, workspace_role=None, **kw):
            CALLS.append(("generate", feature))
            try:
                if feature in BLOCK:
                    await BLOCK[feature].wait()
                if feature in SLOW:
                    await asyncio.sleep(SLOW[feature])
            except asyncio.CancelledError:
                CANCELLED.append(feature)
                raise
            if feature in FAIL:
                raise RuntimeError(f"{feature} generator failed")
            await H._emit_document_feature(
                feature_type=feature, workspace_id=workspace_id,
                user_id=user_id, payload=payload_fn(workspace_id), usage=None)
            if feature in FAIL_AFTER:
                raise RuntimeError(f"{feature} failed after emitting")
        return _generate

    async def _strategy(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("generate", "strategy_objective"))
        if STRATEGY["mode"] == "none":
            return None
        if STRATEGY["mode"] == "raise":
            raise RuntimeError("strategy failed: " + LEAK_SENTINEL)
        return {"caseSummary": "A fictional cheque dispute.", "strategicObjective": FRESH,
                "possibleNextSteps": [], "suggestionChips": []}

    async def _analysis(workspace_id, user_id, workspace_role, batch_result, is_update=False):
        CALLS.append(("generate", "analysis_report"))
        if "analysis_report" in BLOCK:
            await BLOCK["analysis_report"].wait()
        return {"missingInfo": [], "caseResolutionScore": 60,
                "qualityScoreExplanation": FRESH}

    H._hydrate_workspace_caches = _hydrate
    H._remember_workspace_role = _remember
    H._resolve_workspace_role = _resolve
    H._tag_analysis_report_against_current_state = _tag
    H._get_workspace_category_and_subcategory = _meta
    H._generate_analysis_report_suggestion_chips = _chips
    H._ensure_quality_score_explanation = _explanation
    H._reeval_list_of_dates_relevance = _reeval
    H._generate_strategies_from_documents = _strategy
    H._generate_analysis_report_from_documents = _analysis
    H._generate_and_emit_list_of_dates = _gen(
        "list_of_dates", lambda ws: {"listOfDates": [{"date": "2025-03-04", "note": FRESH}]})
    H._generate_and_emit_applicable_laws = _gen(
        "applicable_laws", lambda ws: {"applicableLaws": [], "note": FRESH})
    H._generate_and_emit_procedural_timeline = _gen(
        "procedural_timeline", lambda ws: {"proceduralTimeline": [{"note": FRESH}]})
    H._generate_and_emit_relevant_parties = _gen(
        "relevant_parties", lambda ws: {"parties": [], "note": FRESH})
    H._generate_and_emit_case_laws = _gen(
        "case_laws", lambda ws: {"caseLaws": [], "note": FRESH})
    H._generate_and_emit_drafting_suggestions = _gen(
        "drafting_assistant", lambda ws: {"suggestedDrafts": [{"title": FRESH}]})
    H._socket_client = _FakeSocket()

    # Record which featureTypes go through the one emit chokepoint.
    real_emit = H._emit_document_feature

    async def _emit_via(*a, **kw):
        VIA_EMIT.append((kw.get("workspace_id"), kw.get("feature_type")))
        return await real_emit(*a, **kw)

    H._emit_document_feature = _emit_via

    # Template library and drafting agent: no store, no model.
    from app.langgraph.drafting_agent import drafting_agent
    from app.langgraph.template_store import template_store

    async def _count_for_user(user_id):
        return TEMPLATES["count"]

    async def _list_for_user(user_id):
        return [{"template_id": "tpl_1"}]

    async def _suggest(**_kw):
        CALLS.append(("drafting_agent.suggest",))
        return [{"title": "Reply to notice", "description": FRESH}]

    async def _rematch(**kw):
        CALLS.append(("drafting_agent.rematch",))
        return list(kw.get("suggestions") or [])

    async def _handle_turn(**_kw):
        HANDLE_TURN.append(asyncio.get_running_loop().time())
        raise RuntimeError("drafting turn stub")

    async def _ingest(user_id, templates):
        TEMPLATES["count"] += len(templates)
        return len(templates)

    template_store.count_for_user = _count_for_user
    template_store.list_for_user = _list_for_user
    drafting_agent.suggest = _suggest
    drafting_agent.rematch_unmatched_suggestions = _rematch
    drafting_agent.handle_turn = _handle_turn
    H._ingest_template_items = _ingest

    def _audit(event_type, data, *a, **kw):
        AUDITS.append((event_type, copy.deepcopy(data)))

    audit_logger.log_event = _audit

    # Pin what the checks depend on, whatever the local .env says.
    settings.citations_enabled = False
    settings.template_sync_timeout_seconds = 900.0
    settings.template_sync_in_lock_wait_seconds = 5.0
    settings.pipeline_run_timeout_seconds = 900.0
    settings.audio_timeout_factor = 1.0
    settings.pipeline_run_timeout_audio_max_seconds = 3600.0
    settings.pipeline_lock_warn_seconds = 300.0
    settings.pipeline_concurrent_panels = True


def _sequential(group):
    """Run a group with PIPELINE_CONCURRENT_PANELS=false (the rollback mode).

    The group names one stuck step as the run's stage and expects the steps
    after it, drafting included, to be unsent. Both hold only when the steps
    run one at a time; the concurrent pipeline is covered by
    tests/test_parallel_panels.py."""
    async def run():
        settings.pipeline_concurrent_panels = False
        try:
            await group()
        finally:
            settings.pipeline_concurrent_panels = True
    return run


def _reset(ws, *, seed=True):
    CALLS.clear()
    FAIL.clear()
    FAIL_AFTER.clear()
    BLOCK.clear()
    SLOW.clear()
    DROP.clear()
    HANDLE_TURN.clear()
    REEMITS.clear()
    CANCELLED.clear()
    STRATEGY["mode"] = "ok"
    INGEST["mode"] = "ok"
    TEMPLATES["count"] = 1
    for d in (H._workspace_strategy_payload, H._workspace_analysis_payload,
              H._workspace_list_of_dates_payload, H._workspace_applicable_laws_payload,
              H._workspace_procedural_timeline_payload,
              H._workspace_relevant_parties_payload, H._workspace_case_laws_payload,
              H._workspace_drafting_payload, H._pending_feature_deliveries,
              H._workspace_document_assessments_payload, H._drafting_meta_by_workspace,
              H._workspace_pipeline_lock_holder):
        d.pop(ws, None)
    LIVE_DOCS[ws] = [DOC_A]
    P.clear_participants(ws)
    if seed:
        _seed(ws)


def _schedule_upload(ws, *, armed=True, batch=None):
    before = set(H._background_tasks)
    H._schedule_strategy_analysis_pipeline(
        workspace_id=ws, user_id=OWNER, workspace_role="PETITIONER",
        batch_result=batch if batch is not None else _batch([DOC_C]),
        is_update=True, analysis_log_context=" (sync)",
        armed_features=list(ARMED) if armed else None)
    new = [t for t in H._background_tasks - before if not t.done()]
    return new[0] if new else None


def _schedule_dates(ws):
    before = set(H._background_tasks)
    H._schedule_list_of_dates_and_applicable_laws(ws, OWNER, "PETITIONER", None)
    new = [t for t in H._background_tasks - before if not t.done()]
    return new[0] if new else None


def _schedule_cascade(ws):
    before = set(H._background_tasks)
    H._schedule_objective_change_reanalysis(ws, OWNER)
    new = [t for t in H._background_tasks - before if not t.done()]
    return new[0] if new else None


def _called(feature):
    return ("generate", feature) in CALLS


def _no_secret_on_wire():
    return not any(LEAK_SENTINEL in json.dumps(p, default=str) for _e, p, _l in EMITS)


def _cache_dicts():
    return {
        "strategy_objective": H._workspace_strategy_payload,
        "analysis_report": H._workspace_analysis_payload,
        "list_of_dates": H._workspace_list_of_dates_payload,
        "applicable_laws": H._workspace_applicable_laws_payload,
        "procedural_timeline": H._workspace_procedural_timeline_payload,
        "relevant_parties": H._workspace_relevant_parties_payload,
        "case_laws": H._workspace_case_laws_payload,
        "drafting_assistant": H._workspace_drafting_payload,
    }


def _caches(ws):
    """A deep copy of every panel cache for ``ws`` (compared before/after)."""
    return {f: copy.deepcopy(d.get(ws)) for f, d in _cache_dicts().items()}


_LIST_KEYS = ("listOfDates", "applicableLaws", "proceduralTimeline", "parties",
              "caseLaws", "suggestedDrafts")


def _is_empty_terminal(envelope):
    """No cached or fresh content, and every list the panel carries is empty."""
    body = envelope.get("payload")
    if not isinstance(body, dict) or _origin(envelope) != "other":
        return False
    return all(body.get(k) in (None, []) for k in _LIST_KEYS)


def _no_error_field(ws):
    return not any("error" in p for p in _emits(ws, "strategy_objective"))


# ============================================================ template sync

async def stuck_template_sync_no_answer():
    """AC-100."""
    ws = _ws("tmpl_noanswer")
    _reset(ws)
    TEMPLATES["count"] = 0
    real_drafting = H.__dict__.pop("_generate_and_emit_drafting_suggestions")
    mark = len(EMITS)
    lmark = len(LOGS)
    try:
        task = _schedule_upload(ws)
        # Drafting starts its in-lock wait right after the analysis stage ends
        # (the tail panels are sent as they finish, so they are no cue).
        await _until(lambda: _template_requests()
                     and _logged("stage analysis_report took", lmark)
                     and _logged("stage case_laws took", lmark))
        await _settle()
        check("the template request went out before the lock was needed",
              bool(_template_requests()))
        check("drafting is waiting (in-lock bound), not emitted yet",
              _count(ws, "drafting_assistant", mark) == 0 and not task.done())
        _advance(5.1)
        await _until(lambda: task.done())
        check("drafting proceeds after TEMPLATE_SYNC_IN_LOCK_WAIT_SECONDS and emits",
              _count(ws, "drafting_assistant", mark) == 1)
        check("the pipeline lock is released",
              not H._workspace_pipeline_lock(ws).locked()
              and ws not in H._workspace_pipeline_lock_holder)
        check("the prewarm keeps waiting outside the lock",
              len(H._pending_template_sync) >= 1)

        mark2 = len(EMITS)
        lmark2 = len(LOGS)
        task2 = _schedule_upload(ws)
        await _until(lambda: _logged("stage analysis_report took", lmark2) and
                     _count(ws, "case_laws", mark2) == 1)
        await _settle()
        _advance(5.1)
        await _until(lambda: task2.done())
        check("a second upload then runs and emits every panel",
              all(_count(ws, f, mark2) == 1 for f in RELEASABLE),
              detail=str([(f, _count(ws, f, mark2)) for f in RELEASABLE]))

        original = H._reemit_cached_features

        async def _reemit(w, u):
            REEMITS.append(w)
            await original(w, u)

        H._reemit_cached_features = _reemit
        try:
            mark3 = len(EMITS)
            H._had_disconnect = True
            H._pending_reemits[ws] = (OWNER, time.time())
            await H._do_reemit_after_reconnect()
            await _until(lambda: _count(ws, "drafting_assistant", mark3) >= 1)
            check("a reconnect replay (_locked_reemit) then runs and emits",
                  ws in REEMITS and _count(ws, "strategy_objective", mark3) == 1)
        finally:
            H.__dict__.pop("_reemit_cached_features", None)
        # ~10.2 s of fake time has passed since the first prewarm was sent.
        _advance(880)
        await _settle()
        check("the prewarm is still waiting outside the lock before 900 s",
              len(H._pending_template_sync) >= 1)
        _advance(21)
        await _until(lambda: not H._pending_template_sync)
        check("every prewarm ends at its bound; no future left pending",
              not H._pending_template_sync)
    finally:
        H._generate_and_emit_drafting_suggestions = real_drafting


async def stuck_template_sync_undelivered():
    """AC-101."""
    _reset(_ws("tmpl_undelivered"))
    TEMPLATES["count"] = 0
    DROP.add("request_template_sync")
    mark = len(LOGS)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(H.ensure_templates_synced(OWNER), timeout=2.0)
    check("an undelivered request returns at once", loop.time() - started < 1.0)
    check("it logs 'request_template_sync not delivered'",
          bool(_logged("request_template_sync not delivered", mark)))
    check("no future is left pending", not H._pending_template_sync)


async def stuck_template_sync_invalid_reply():
    """AC-102."""
    _reset(_ws("tmpl_invalid"))
    TEMPLATES["count"] = 0
    mark = len(EMITS)
    task = asyncio.create_task(H.ensure_templates_synced(OWNER))
    await _until(lambda: any(e == "request_template_sync" for e, _p, _l in EMITS[mark:]))
    rid = [p for e, p, _l in EMITS[mark:] if e == "request_template_sync"][0]["requestId"]
    check("the waiter is pending on its request", rid in H._pending_template_sync)
    await H.on_template_sync_response({"requestId": rid, "userId": None, "templates": "x"})
    await _until(lambda: task.done(), timeout=1.0)
    check("an invalid reply resolves the pending future; the waiter proceeds",
          task.done() and task.exception() is None)
    check("no future is left pending", not H._pending_template_sync)


async def stuck_template_sync_late():
    """AC-103."""
    ws = _ws("tmpl_late")
    _reset(ws)
    TEMPLATES["count"] = 0
    H._drafting_meta_by_workspace[ws] = {"user_id": OWNER, "role": "PETITIONER",
                                         "lib_count": 0}
    mark = len(EMITS)
    task = asyncio.create_task(H.ensure_templates_synced(OWNER))
    await _until(lambda: any(e == "request_template_sync" for e, _p, _l in EMITS[mark:]))
    rid = [p for e, p, _l in EMITS[mark:] if e == "request_template_sync"][0]["requestId"]
    _advance(901)
    await _until(lambda: task.done())
    check("the bound expired first", task.done() and not H._pending_template_sync)
    mark2 = len(EMITS)
    await H.on_template_sync_response({
        "requestId": rid, "userId": OWNER, "count": 1,
        "templates": [{"id": "tpl_1", "name": "Notice.docx", "category": "Notice",
                       "extension": "docx", "fileUrl": "https://storage.example/t?sig=x"}]})
    await _until(lambda: _count(ws, "drafting_assistant", mark2) == 1)
    late = [(p, locked) for e, p, locked in EMITS[mark2:]
            if e == "document_feature_response" and p.get("workspaceId") == ws
            and p.get("featureType") == "drafting_assistant"]
    check("drafting is re-emitted via _maybe_reemit_drafting_on_templates",
          len(late) == 1 and ("drafting_agent.rematch",) in CALLS)
    check("outside the pipeline lock", bool(late) and late[0][1] is False)


async def stuck_template_sync_other_callers():
    """AC-104: the chat drafting turn and workspace_transferred."""
    ws = _ws("tmpl_callers")
    _reset(ws)
    TEMPLATES["count"] = 0
    mark = len(EMITS)
    payload = UserMessageRequestPayload(
        content="Draft a reply to the notice", chat_type="DRAFTING_ASSISTANT",
        workspaceId=ws, userId=OWNER, chatId=f"chat_{RUN_ID}",
        timestamp="2026-10-26T10:00:00.000Z", source="template", templateId="tpl_1")
    turn = asyncio.create_task(H._handle_drafting_assistant_message(
        payload=payload, workspace_id=ws, user_id=OWNER, chat_id=f"chat_{RUN_ID}"))
    await _until(lambda: any(e == "request_template_sync" for e, _p, _l in EMITS[mark:]))
    _advance(899)
    await _settle()
    check("the chat drafting turn is still waiting at 899 s", not HANDLE_TURN)
    _advance(2)
    await _until(lambda: bool(HANDLE_TURN))
    check("the chat drafting turn continues after 900 s", len(HANDLE_TURN) == 1)
    await asyncio.gather(turn, return_exceptions=True)

    ws2 = _ws("tmpl_transfer")
    _reset(ws2)
    TEMPLATES["count"] = 0
    mark = len(EMITS)
    await H.handle("workspace_transferred", {
        "workspaceId": ws2, "previousUserId": OWNER, "newUserId": NEW_OWNER,
        "title": "New Client 101", "workspaceRole": "RESPONDENT", "status": "active",
        "evidenceFiles": [_evidence(DOC_A, "Notice.pdf")],
        "reEmitFeatures": ["drafting_assistant"],
        "transferredAt": "2026-10-26T10:00:00.000Z"})
    sent = await _until(
        lambda: any(e == "request_template_sync" for e, _p, _l in EMITS[mark:]))
    check("workspace_transferred requests a forced template sync", sent)
    _advance(899)
    await _settle()
    check("the transfer is still waiting at 899 s", not _called("drafting_assistant"))
    _advance(2)
    await _until(lambda: _called("drafting_assistant"))
    check("workspace_transferred continues after 900 s", _called("drafting_assistant"))


async def stuck_template_wait_900():
    """AC-120."""
    _reset(_ws("tmpl_900"))
    TEMPLATES["count"] = 0
    mark = len(LOGS)
    task = asyncio.create_task(H.ensure_templates_synced(OWNER))
    await _until(lambda: bool(H._pending_template_sync))
    _advance(899)
    await _settle()
    check("not returned before 900 s", not task.done())
    _advance(1.01)
    await _until(lambda: task.done())
    check("returns after TEMPLATE_SYNC_TIMEOUT_SECONDS with no explicit timeout",
          task.done() and task.exception() is None)
    check("logs a timeout warning",
          bool(_logged("request_template_sync timed out after 900s", mark, "WARNING")))
    check("the pending future is removed", not H._pending_template_sync)


# ============================================================ watchdog

async def stuck_lock_watchdog():
    """AC-105."""
    ws = _ws("watchdog")
    _reset(ws)
    BLOCK["case_laws"] = asyncio.Event()
    H.start_pipeline_lock_watchdog()
    try:
        task = _schedule_upload(ws)
        await _until(lambda: _called("case_laws"))
        await _settle()
        before = len(EMITS)
        mark = len(LOGS)
        _advance(301)
        await _settle()
        warned = _logged("pipeline lock held", mark, "WARNING")
        check("one WARNING once the hold passes 300 s", len(warned) == 1,
              detail=str(warned))
        check("it names holder, stage and cap",
              bool(warned) and "holder=upload_pipeline stage=case_laws cap=900s" in warned[0])
        check("it carries no content",
              bool(warned) and CACHED not in warned[0] and FRESH not in warned[0])
        _advance(60)
        await _settle()
        _advance(60)
        await _settle()
        check("rate-limited: no repeat within 5 minutes",
              len(_logged("pipeline lock held", mark, "WARNING")) == 1)
        _advance(185)
        await _settle()
        check("reported again after 5 minutes",
              len(_logged("pipeline lock held", mark, "WARNING")) == 2)
        check("the watchdog releases nothing",
              len(EMITS) == before and not task.done()
              and H._workspace_pipeline_lock(ws).locked())
        BLOCK["case_laws"].set()
        await _until(lambda: task.done())
    finally:
        await H.stop_pipeline_lock_watchdog()


# ============================================================ tail failures

async def stuck_tail_step_failure():
    """AC-106."""
    for feature in TAIL:
        ws = _ws(f"tail_{feature}")
        _reset(ws)
        FAIL.add(feature)
        mark = len(EMITS)
        task = _schedule_upload(ws)
        await _until(lambda: task.done())
        got = _emits(ws, feature, mark)
        check(f"{feature} raising gets exactly one terminal emit, from cache",
              len(got) == 1 and _origin(got[0]) == "cached",
              detail=f"{len(got)} emit(s) {[_origin(g) for g in got]}")
        later = TAIL[TAIL.index(feature) + 1:]
        check(f"{feature} raising: later steps still run and emit",
              all(_count(ws, f, mark) == 1 and _origin(_emits(ws, f, mark)[0]) == "fresh"
                  for f in later),
              detail=str([(f, _count(ws, f, mark)) for f in later]))

    # No cache for the failing feature: the terminal emit is the empty state.
    for feature in TAIL:
        ws = _ws(f"tail_empty_{feature}")
        _reset(ws)
        _cache_dicts()[feature].pop(ws, None)
        FAIL.add(feature)
        mark = len(EMITS)
        task = _schedule_upload(ws)
        await _until(lambda: task.done())
        got = _emits(ws, feature, mark)
        check(f"{feature} raising with no cache gets exactly one empty terminal emit",
              len(got) == 1 and _is_empty_terminal(got[0]),
              detail=f"{len(got)} emit(s) {[g.get('payload') for g in got]}")


async def stuck_no_double_emit():
    """AC-107."""
    ws = _ws("nodouble")
    _reset(ws)
    FAIL_AFTER.add("applicable_laws")
    mark = len(EMITS)
    task = _schedule_upload(ws)
    await _until(lambda: task.done())
    got = _emits(ws, "applicable_laws", mark)
    check("a step that emitted then raised is not emitted again",
          len(got) == 1 and _origin(got[0]) == "fresh", detail=str(len(got)))


# ============================================================ cancellation

async def stuck_runner_cancelled():
    """AC-108."""
    ws = _ws("runner_cancel")
    _reset(ws)
    BLOCK["list_of_dates"] = asyncio.Event()
    mark = len(EMITS)
    lmark = len(LOGS)
    task = _schedule_upload(ws)
    await _until(lambda: _called("list_of_dates"))
    check("strategy emitted before the cancel", _count(ws, "strategy_objective", mark) == 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check("the cancellation is re-raised", task.cancelled())
    check("it is logged", bool(_logged("pipeline CANCELLED", lmark)))
    counts = {f: _count(ws, f, mark) for f in RELEASABLE}
    check("every armed feature gets exactly one terminal emit",
          all(c == 1 for c in counts.values()), detail=str(counts))
    # Panels finished before the cancel were sent fresh (nothing holds them);
    # the blocked one and the lane's unsent drafting are settled from cache.
    check("the unemitted ones come from cache",
          all(_origin(_emits(ws, f, mark)[0]) == "cached"
              for f in ("list_of_dates", "drafting_assistant"))
          and all(_origin(_emits(ws, f, mark)[0]) in ("cached", "fresh")
                  for f in RELEASABLE[2:]))
    check("the blocked step was itself cancelled", "list_of_dates" in CANCELLED)
    check("the lock is free", not H._workspace_pipeline_lock(ws).locked())


async def stuck_dates_runner_cancelled():
    """AC-109."""
    ws = _ws("dates_cancel")
    _reset(ws)
    BLOCK["procedural_timeline"] = asyncio.Event()
    mark = len(EMITS)
    task = _schedule_dates(ws)
    await _until(lambda: _called("procedural_timeline"))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    check("the cancellation is re-raised", task.cancelled())
    counts = {f: _count(ws, f, mark) for f in TAIL}
    check("each tail feature gets exactly one terminal emit",
          all(c == 1 for c in counts.values()), detail=str(counts))
    check("the unemitted ones are released from cache",
          all(_origin(_emits(ws, f, mark)[0]) == "cached" for f in TAIL[2:]))


# ============================================================ strategy

async def stuck_strategy_failure():
    """AC-110."""
    for mode in ("none", "raise"):
        ws = _ws(f"strategy_{mode}")
        _reset(ws)
        STRATEGY["mode"] = mode
        mark = len(EMITS)
        vmark = len(VIA_EMIT)
        task = _schedule_upload(ws)
        await _until(lambda: task.done())
        got = _emits(ws, "strategy_objective", mark)
        check(f"strategy {mode}: emitted through _emit_document_feature",
              VIA_EMIT[vmark:].count((ws, "strategy_objective")) == 1)
        check(f"strategy {mode}: emitted once with the cached payload",
              len(got) == 1 and got[0]["payload"].get("strategicObjective") == CACHED,
              detail=str(len(got)))
        check(f"strategy {mode}: no error field, no exception text on the wire",
              _no_error_field(ws) and _no_secret_on_wire())
        check(f"strategy {mode}: later panels emit",
              all(_count(ws, f, mark) == 1 for f in RELEASABLE[1:]))

    ws = _ws("strategy_first_build")
    _reset(ws, seed=False)
    STRATEGY["mode"] = "none"
    mark = len(EMITS)
    task = _schedule_upload(ws, armed=False)
    await _until(lambda: task.done())
    got = _emits(ws, "strategy_objective", mark)
    body = got[0].get("payload") if got else None
    check("first build: strategy settles with the fallback payload",
          len(got) == 1 and isinstance(body, dict) and _no_error_field(ws)
          and body.get("isEmpty") is True and body.get("strategicObjective") == ""
          and _origin(got[0]) == "other",
          detail=str(len(got)))
    check("first build: no exception text on the wire", _no_secret_on_wire())
    check("first build: later panels emit",
          all(_count(ws, f, mark) == 1 for f in RELEASABLE[1:]))


# ============================================================ sync branches

def _sync(ws, files):
    return {"workspaceId": ws, "userId": OWNER, "workspaceRole": "PETITIONER",
            "updatedAt": "2026-10-26T10:00:00.000Z",
            "evidenceFiles": files}


async def stuck_ingest_crash():
    """AC-111."""
    ws = _ws("ingest_crash")
    _reset(ws)
    INGEST["mode"] = "crash"
    caches = _caches(ws)
    mark = len(EMITS)
    await H.handle("workspace_sync", _sync(
        ws, [_evidence(DOC_A, "Notice.pdf"), _evidence(DOC_C, "Reply.pdf")]))
    await _settle()
    refresh = [p for e, p, _l in EMITS[mark:] if e == "workspace:refresh"
               and p.get("workspaceId") == ws]
    check("9 loaders were armed", bool(refresh) and len(refresh[0]["featureTypes"]) == 9)
    errors = [p for p in _emits(ws, "document_assessments", mark) if "error" in p]
    check("the document_assessments error emit is kept", len(errors) == 1)
    counts = {f: _count(ws, f, mark) for f in RELEASABLE}
    check("the other 8 features each get one terminal emit",
          all(c == 1 for c in counts.values()), detail=str(counts))
    check("they come from cache",
          all(_origin(_emits(ws, f, mark)[0]) == "cached" for f in RELEASABLE))
    check("caches unchanged (all eight panel caches)", _caches(ws) == caches)
    check("no panel generator ran", not any(c[0] == "generate" for c in CALLS))


async def stuck_all_files_failed():
    """AC-112."""
    ws = _ws("all_failed")
    _reset(ws)
    INGEST["mode"] = "fail"
    caches = _caches(ws)
    tasks_before = set(H._background_tasks)
    mark = len(EMITS)
    await H.handle("workspace_sync", _sync(
        ws, [_evidence(DOC_A, "Notice.pdf"), _evidence(DOC_C, "Reply.pdf")]))
    await _settle()
    counts = {f: _count(ws, f, mark) for f in RELEASABLE}
    check("every armed feature (strategy included) is re-emitted once",
          all(c == 1 for c in counts.values()), detail=str(counts))
    check("from cache, unchanged",
          all(_origin(_emits(ws, f, mark)[0]) == "cached" for f in RELEASABLE)
          and _emits(ws, "strategy_objective", mark)[0]["payload"].get(
              "strategicObjective") == CACHED)
    check("analysis and dates caches unchanged (no fallback, no wipe)",
          _caches(ws)["analysis_report"] == caches["analysis_report"]
          and _caches(ws)["list_of_dates"] == caches["list_of_dates"])
    check("every other panel cache unchanged too", _caches(ws) == caches)
    new_tasks = [t for t in H._background_tasks - tasks_before if not t.done()]
    check("no full re-extract is scheduled",
          not new_tasks and not any(c[0] == "generate" for c in CALLS))

    ws = _ws("all_failed_first")
    _reset(ws, seed=False)
    INGEST["mode"] = "fail"
    LIVE_DOCS[ws] = []
    mark = len(EMITS)
    await H.handle("workspace_sync", _sync(ws, [_evidence(DOC_C, "Reply.pdf")]))
    await _until(lambda: _count(ws, "drafting_assistant", mark) == 1)
    check("first build: nothing armed",
          not [p for e, p, _l in EMITS[mark:] if e == "workspace:refresh"
               and p.get("workspaceId") == ws])
    dates = _emits(ws, "list_of_dates", mark)
    check("first build: today's fallback (analysis fallback, empty dates, tail)",
          _count(ws, "analysis_report", mark) == 1
          and dates and dates[0]["payload"].get("listOfDates") == []
          and _called("list_of_dates"))


# ============================================================ slow step

async def stuck_slow_step_visible():
    """AC-115."""
    ws = _ws("slow")
    _reset(ws)
    SLOW["case_laws"] = 300.0
    mark = len(EMITS)
    lmark = len(LOGS)
    task = _schedule_upload(ws)
    await _until(lambda: _called("case_laws"))
    await _settle()
    _advance(301)
    reported = H._check_pipeline_lock_holds()
    await _until(lambda: task.done())
    took = _logged("stage case_laws took", lmark)
    check("'stage case_laws took' shows the delay",
          bool(took) and float(took[0].split("took ")[1].split("s")[0]) >= 300.0,
          detail=str(took))
    warned = _logged("pipeline lock held", lmark, "WARNING")
    check("the watchdog reports stage=case_laws past the threshold",
          reported == 1 and bool(warned) and "stage=case_laws" in warned[0])
    check("later panels still settle",
          _count(ws, "case_laws", mark) == 1 and _count(ws, "drafting_assistant", mark) == 1)


# ============================================================ caps

def _resume(ws, features):
    return {
        "requestId": f"rsm_{ws[:8]}_{uuid.uuid4().hex[:6]}",
        "workspaceId": ws, "userId": OWNER, "title": "New Client 101",
        "status": "active", "workspaceRole": "PETITIONER",
        "reEmitFeatures": list(features),
        "evidenceFiles": [_evidence(DOC_A, "Notice.pdf")],
        "currentState": {"missingInfo": []},
        "timestamp": "2026-10-26T10:15:03.442Z",
    }


async def stuck_run_cap():
    """AC-116."""
    ws = _ws("run_cap")
    _reset(ws)
    BLOCK["case_laws"] = asyncio.Event()
    mark = len(EMITS)
    lmark = len(LOGS)
    amark = len(AUDITS)
    task = _schedule_upload(ws)
    await _until(lambda: _called("case_laws"))

    # Queue a cascade, a reconnect replay and a resume behind the lock.
    original = H._reemit_cached_features

    async def _reemit(w, u):
        REEMITS.append(w)
        await original(w, u)

    H._reemit_cached_features = _reemit
    try:
        cascade = _schedule_cascade(ws)
        H._had_disconnect = True
        H._pending_reemits[ws] = (OWNER, time.time())
        await H._do_reemit_after_reconnect()
        resume_payload = _resume(ws, ["case_laws"])
        resume = asyncio.create_task(H.handle("workspace_resume_request", resume_payload))
        await _settle()
        _advance(899)
        await _settle()
        check("not cut before 900 s", not task.done() and not _logged(
            "pipeline run exceeded", lmark))
        # The settlement is the capped run's last act before the lock is
        # released, so emits up to its return are the run's own.
        upload_end = {}
        settle = H._settle_pipeline_run_cap

        async def _settle_and_mark(*a, **kw):
            await settle(*a, **kw)
            upload_end.setdefault("i", len(EMITS))

        H._settle_pipeline_run_cap = _settle_and_mark

        def _mark_end():
            return task.done()

        # Unblock later callers only: the stuck call itself must be cut by
        # the cap, not released by the event.
        BLOCK.pop("case_laws")
        _advance(2)
        await _until(_mark_end)
        crit = _logged("pipeline run exceeded", lmark, "CRITICAL")
        check("one CRITICAL 'pipeline run exceeded cap=900s ... stage=case_laws'",
              len(crit) == 1 and "cap=900s" in crit[0] and "holder=upload_pipeline" in crit[0]
              and "stage=case_laws" in crit[0], detail=str(crit))
        audits = [d for t, d in AUDITS[amark:] if t == "pipeline_run_timeout"]
        check("a pipeline_run_timeout audit event with no content",
              len(audits) == 1 and audits[0].get("stage") == "case_laws"
              and CACHED not in json.dumps(audits[0]))
        end = upload_end.get("i")
        check("the cap settlement ran", end is not None)
        counts = {f: _count(ws, f, mark, end) for f in RELEASABLE}
        check("every armed feature has exactly one terminal emit from the run",
              all(c == 1 for c in counts.values()), detail=str(counts))
        check("the unemitted ones came from cache",
              _origin(_emits(ws, "case_laws", mark)[0]) == "cached"
              and _origin(_emits(ws, "drafting_assistant", mark)[0]) == "cached")
        check("the run was cancelled: the stuck case_laws call was cut",
              "case_laws" in CANCELLED)
        check("the lock was freed", task.done() and not task.cancelled())
        await _until(lambda: cascade.done() and resume.done() and ws in REEMITS)
        check("the queued cascade then ran",
              cascade.done() and bool(_logged("stage objective_cascade_total", lmark)))
        check("the queued reconnect replay then ran", ws in REEMITS)
        check("the queued resume then ran",
              resume.done() and (H._resume_requests.get(ws) or {}).get(
                  resume_payload["requestId"], {}).get("status") == "done")
    finally:
        H.__dict__.pop("_reemit_cached_features", None)
        H.__dict__.pop("_settle_pipeline_run_cap", None)
        _advance(3600)
        await _settle()


async def stuck_cascade_cap():
    """AC-117."""
    ws = _ws("cascade_cap")
    _reset(ws)
    BLOCK["drafting_assistant"] = asyncio.Event()
    mark = len(EMITS)
    lmark = len(LOGS)
    amark = len(AUDITS)
    task = _schedule_cascade(ws)
    await _until(lambda: _called("drafting_assistant"))
    _advance(901)
    await _until(lambda: task.done())
    BLOCK.pop("drafting_assistant").set()
    crit = _logged("pipeline run exceeded", lmark, "CRITICAL")
    check("one CRITICAL for the cascade at its stage",
          len(crit) == 1 and "holder=cascade" in crit[0]
          and "stage=cascade_drafting" in crit[0] and "cap=900s" in crit[0],
          detail=str(crit))
    cascade_features = ["analysis_report", "list_of_dates", "applicable_laws",
                        "procedural_timeline", "relevant_parties", "case_laws",
                        "drafting_assistant"]
    counts = {f: _count(ws, f, mark) for f in cascade_features}
    check("every cascade feature has exactly one terminal emit (no double release)",
          all(c == 1 for c in counts.values()), detail=str(counts))
    check("drafting settled from cache",
          _origin(_emits(ws, "drafting_assistant", mark)[0]) == "cached")
    audits = [d for t, d in AUDITS[amark:] if t == "pipeline_run_timeout"]
    check("a pipeline_run_timeout audit event for the cascade, with no content",
          len(audits) == 1 and audits[0].get("holder") == "cascade"
          and audits[0].get("stage") == "cascade_drafting"
          and CACHED not in json.dumps(audits[0]) and FRESH not in json.dumps(audits[0]),
          detail=str(audits))
    check("the run was cancelled: the stuck drafting call was cut",
          "drafting_assistant" in CANCELLED)
    check("the lock is free", not H._workspace_pipeline_lock(ws).locked())
    _advance(3600)
    await _settle()


async def stuck_audio_cap():
    """AC-118."""
    ws = _ws("audio_cap")
    _reset(ws)
    TEMPLATES["count"] = 0
    batch = _batch(["68c1f0a9e4b0d2a1c3f4567a", "68c1f0a9e4b0d2a1c3f4567b"])
    batch["results"][0].update(is_audio=True, audio_duration_seconds=1200)
    batch["results"][1].update(is_audio=True, audio_duration_seconds=600)
    cap = H._pipeline_run_cap(batch)
    check("cap = min(900 + 1.0 x 1,800, 3,600) = 2,700 s", cap == 2700.0, detail=str(cap))
    BLOCK["case_laws"] = asyncio.Event()
    lmark = len(LOGS)
    task = _schedule_upload(ws, batch=batch)
    await _until(lambda: _called("case_laws"))
    check("the template prewarm for the run uses the same cap",
          bool(_logged("bound=2700s", lmark)))
    _advance(901)
    await _settle()
    check("not cut at 900 s", not task.done() and not _logged("pipeline run exceeded", lmark))
    _advance(1798)
    await _settle()
    check("still running before 2,700 s", not task.done())
    _advance(2)
    await _until(lambda: task.done())
    crit = _logged("pipeline run exceeded", lmark, "CRITICAL")
    check("cut after 2,700 s", len(crit) == 1 and "cap=2700s" in crit[0], detail=str(crit))
    BLOCK.pop("case_laws").set()
    _advance(3600)
    await _settle()


async def stuck_audio_cap_unknown():
    """AC-119."""
    batch = _batch(["68c1f0a9e4b0d2a1c3f4567c"])
    batch["results"][0].update(is_audio=True, audio_duration_seconds=None)
    check("an unknown duration takes PIPELINE_RUN_TIMEOUT_AUDIO_MAX_SECONDS",
          H._pipeline_run_cap(batch) == 3600.0)
    missing = _batch(["68c1f0a9e4b0d2a1c3f4567d"])
    missing["results"][0].update(is_audio=True)
    check("a failed transcription (no duration key) takes the maximum",
          H._pipeline_run_cap(missing) == 3600.0)
    worst = 0.0
    for durations in ([0], [1], [2699], [2700], [2701], [3600], [10 ** 6],
                      [1200, 600, 900], [10 ** 9, 5]):
        b = _batch([f"68c1f0a9e4b0d2a1c3f45{i:03d}" for i in range(len(durations))])
        for r, d in zip(b["results"], durations):
            r.update(is_audio=True, audio_duration_seconds=d)
        worst = max(worst, H._pipeline_run_cap(b))
    check("the cap never exceeds the maximum for any durations", worst == 3600.0,
          detail=str(worst))
    check("no audio: the base cap", H._pipeline_run_cap(_batch([DOC_C])) == 900.0)


# ------------------------------------------------------------------- runner

CHECKS = [
    ("stuck_template_sync_no_answer", stuck_template_sync_no_answer),       # AC-100
    ("stuck_template_sync_undelivered", stuck_template_sync_undelivered),   # AC-101
    ("stuck_template_sync_invalid_reply", stuck_template_sync_invalid_reply),  # AC-102
    ("stuck_template_sync_late", stuck_template_sync_late),                 # AC-103
    ("stuck_template_sync_other_callers", stuck_template_sync_other_callers),  # AC-104
    ("stuck_lock_watchdog", _sequential(stuck_lock_watchdog)),                           # AC-105
    ("stuck_tail_step_failure", stuck_tail_step_failure),                   # AC-106
    ("stuck_no_double_emit", stuck_no_double_emit),                         # AC-107
    ("stuck_runner_cancelled", stuck_runner_cancelled),                     # AC-108
    ("stuck_dates_runner_cancelled", stuck_dates_runner_cancelled),         # AC-109
    ("stuck_strategy_failure", stuck_strategy_failure),                     # AC-110
    ("stuck_ingest_crash", stuck_ingest_crash),                             # AC-111
    ("stuck_all_files_failed", stuck_all_files_failed),                     # AC-112
    ("stuck_slow_step_visible", stuck_slow_step_visible),                   # AC-115
    ("stuck_run_cap", _sequential(stuck_run_cap)),                                       # AC-116
    ("stuck_cascade_cap", stuck_cascade_cap),                               # AC-117
    ("stuck_audio_cap", stuck_audio_cap),                                   # AC-118
    ("stuck_audio_cap_unknown", stuck_audio_cap_unknown),                   # AC-119
    ("stuck_template_wait_900", stuck_template_wait_900),                   # AC-120
]


async def _run(selected):
    _install_stubs()
    for name, fn in CHECKS:
        if selected and name not in selected:
            continue
        print(f"\n[{name}]")
        # A hung or crashed group is a failure, not a hang. The guard is on the
        # REAL clock: asyncio.wait_for would expire as soon as a group advances
        # the fake one.
        group = asyncio.create_task(fn())
        deadline = time.monotonic() + 30.0
        while not group.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not group.done():
            group.cancel()
            await asyncio.gather(group, return_exceptions=True)
            check(f"{name} completed", False, detail="timed out (real 30 s)")
        elif group.exception() is not None:
            check(f"{name} completed", False, detail=repr(group.exception()))
    # Leave no stray work behind.
    for t in list(H._background_tasks):
        t.cancel()
    await asyncio.gather(*H._background_tasks, return_exceptions=True)


def _sink(message):
    record = message.record
    LOGS.append((record["level"].name, record["message"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the group names")
    ap.add_argument("--only", nargs="*", default=None, help="run only these groups")
    ap.add_argument("--verbose", action="store_true", help="also print service logs")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    if not args.verbose:
        logger.remove()
    logger.add(_sink, level="DEBUG")
    loop = _FakeClockLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run(set(args.only) if args.only else None))
    finally:
        loop.close()
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
