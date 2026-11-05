#!/usr/bin/env python3
"""
verify_workspace_flow.py — Offline checks for the workspace feature suite:
``workspace_created`` / ``workspace_sync`` → document_assessments →
workspace_metadata → strategy → analysis → list_of_dates → applicable_laws →
procedural_timeline → relevant_parties → case_laws → drafting_assistant, plus
the reconnect replay and the role resolution that feeds every generator.

The rules under test:

  * workspace_created drives the whole suite, every panel once (drafting
    twice: a first build re-runs it once laws and dates exist), and never arms
    a ``workspace:refresh`` it does not pay off;
  * workspace_sync with a new upload arms its loaders and every armed
    featureType gets a terminal ``document_feature_response``; the cached
    document_assessments manifest is MERGED (never shrunk to the new files),
    while the emit itself stays incremental; a replayed sync is swallowed;
  * a deletion sync arms eight loaders and pays every one of them off, and
    prunes the deleted document from the manifest;
  * a generator that fails still leaves its panel with a terminal state
    (strategy settled from cache, analysis fallback, case laws fallback,
    cache-or-empty for the rest) and never skips the panels after it;
  * an undelivered emit (socket returns False) is buffered — in memory AND
    persisted — and re-sent on reconnect BEFORE the cached replay;
  * every generator receives a validated role: a lower-case role is
    normalised, an invalid one falls back to DEFAULT_WORKSPACE_ROLE, and a role-less sync
    uses the persisted role rather than a fabricated default.

NOT COVERED HERE (and why)
  * "No validated role → no downstream generator runs" (CLAUDE.md, role gate):
    the socket flow does not implement a hard gate. workspace_created replaces
    an invalid role with DEFAULT_WORKSPACE_ROLE and carries on when
    ``initialize_workspace_with_role`` raises. The checks below pin what the
    code guarantees (every generator gets a valid role); the gap is reported
    rather than pinned.

Drives the real handlers through ``event_handler.handle`` (run scopes, logging
context and the stop fence apply) with Qdrant, persistence, the document
processor, the panel generators and the socket stubbed. No API key, no
backend, no Qdrant, no billed call. The template library prewarm is stubbed
off: the fake socket reports every emit delivered, so a real prewarm would
wait for a template_sync_response that never comes.

USAGE
    python tests/offline/verify_workspace_flow.py            # run everything
    python tests/offline/verify_workspace_flow.py --list     # show the check names
"""

import argparse
import asyncio
import copy
import importlib
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

# Record (rather than perform) the write-through persistence BEFORE anything
# from-imports persist_state, so the checks can assert what was persisted.
from app.workspace import persistence as _persistence  # noqa: E402

PERSISTED = []
STORE = {}  # (workspace_id, kind) -> last persisted value


def _record_persist(workspace_id, kind, data, **_kw):
    PERSISTED.append((workspace_id, kind, copy.deepcopy(data)))
    STORE[(workspace_id, kind)] = copy.deepcopy(data)


async def _restore(workspace_id, kind, **_kw):
    return copy.deepcopy(STORE.get((workspace_id, kind)))


_persistence.persist_state = _record_persist
_persistence.restore_state = _restore

from app.core.config import settings  # noqa: E402
from app.core.roles import DEFAULT_WORKSPACE_ROLE  # noqa: E402
from app.langgraph.integration import workspace_role_integration  # noqa: E402
from app.processing.document_processor import document_processor  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers import run_control as RC  # noqa: E402
from app.vectorstore.qdrant import vector_store  # noqa: E402
from app.workspace import participants as P  # noqa: E402
from app.workspace.persistence import CacheKinds  # noqa: E402

RC.restore_state = _restore
_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []
DROPPED = []
CALLS = []
MODEL_CALLS = []
RUN_ID = uuid.uuid4().hex[:8]
OWNER = "68b0e1f8d3a9c4b2e1f0a001"
DOC_A = "68c1f0a9e4b0d2a1c3f4a001"
DOC_B = "68c1f0a9e4b0d2a1c3f4a002"
DOC_C = "68c1f0a9e4b0d2a1c3f4a003"
VALID_ROLES = {"ADJUDICATOR", "PETITIONER", "RESPONDENT", "MEDIATOR"}

#: Product order of the full suite a successful ingestion produces.
SUITE_IN_ORDER = [
    "document_assessments", "workspace_metadata", "strategy_objective",
    "analysis_report", "list_of_dates", "applicable_laws", "procedural_timeline",
    "relevant_parties", "case_laws", "drafting_assistant",
]
#: Panels produced by the stubbed generators (everything after ingestion).
GENERATED = SUITE_IN_ORDER[2:]
TAIL = SUITE_IN_ORDER[4:]
#: A first build runs drafting once more at the end, when laws and dates exist.
FIRST_BUILD = SUITE_IN_ORDER + ["drafting_assistant"]


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
    # ``drop`` names featureTypes whose emit the "socket" swallows (False).
    def __init__(self):
        self.drop = set()

    async def emit(self, event, payload):
        if (event == "document_feature_response" and isinstance(payload, dict)
                and payload.get("featureType") in self.drop):
            DROPPED.append((event, copy.deepcopy(payload)))
            return False
        EMITS.append((event, copy.deepcopy(payload)))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", copy.deepcopy(payload)))
        return True


SOCKET = _FakeSocket()


def _features_emitted(ws):
    return [p.get("featureType") for e, p in EMITS
            if e == "document_feature_response" and p.get("workspaceId") == ws]


def _tail_once(features):
    """The TAIL panels emitted, each counted once and kept in TAIL order.

    Panels after analysis generate concurrently and a first build re-runs
    drafting, so emit order and count are not a contract; that each one
    emitted is.
    """
    return [f for f in TAIL if f in features]


def _feature_envelopes(ws, feature):
    return [p for e, p in EMITS if e == "document_feature_response"
            and p.get("workspaceId") == ws and p.get("featureType") == feature]


def _refreshes(ws):
    return [(i, p) for i, (e, p) in enumerate(EMITS)
            if e == "workspace:refresh" and p.get("workspaceId") == ws]


def _unpaid_loaders(ws):
    """Armed featureTypes with no document_feature_response after their refresh."""
    unpaid = set()
    for idx, refresh in _refreshes(ws):
        later = {p.get("featureType") for e, p in EMITS[idx + 1:]
                 if e == "document_feature_response" and p.get("workspaceId") == ws}
        unpaid |= set(refresh.get("featureTypes") or []) - later
    return unpaid


def _generated_roles(ws):
    return {c[2] for c in CALLS if c[0] == "generate" and c[3] == ws}


# ------------------------------------------------------------------------ stubs

LIVE_DOCS = {}     # workspace_id -> [doc ids in the "vector store"]
ROLE_STORE = {}    # "ws:user" -> role persisted by upsert_workspace_role
RAISE = {}         # feature -> exception instance the generator raises


def _evidence(doc_id, name):
    return {
        "_id": doc_id, "originalName": name, "storeFileName": f"68c1f0a9_{name}",
        "filename": name.rsplit(".", 1)[0], "extension": name.rsplit(".", 1)[-1],
        "fileHash": uuid.uuid4().hex, "url": f"https://storage.example.invalid/{name}",
        "path": f"workspaces/ws/evidence/{name}",
        "uploadedAt": "2026-10-01T09:10:00.000Z",
    }


def _batch(ids):
    return {
        "total_files": len(ids), "successful": len(ids), "failed": 0,
        "average_readability_score": 90,
        "results": [{"document_id": i, "filename": f"File {i[-3:]}", "success": True,
                     "readability_score": 90, "readability_rating": "Excellent",
                     "readable": True, "extracted_text_length": 100,
                     "stored_in_vector_db": True, "summary": "A fictional exhibit.",
                     "evidentiary_strength": "High", "document_type": "invoice",
                     "extracted_fields": {}, "sensitive_fields": {}}
                    for i in ids],
    }


def _install_stubs():
    async def _process_batch(files, workspace_id, user_id):
        ids = [f["_id"] for f in files]
        CALLS.append(("process_batch", sorted(ids), None, workspace_id))
        LIVE_DOCS.setdefault(workspace_id, [])
        LIVE_DOCS[workspace_id] += [i for i in ids if i not in LIVE_DOCS[workspace_id]]
        return _batch(ids)

    async def _get_all_documents(workspace_id, limit_per_batch=100):
        return [{"document_id": i, "content": "x"} for i in LIVE_DOCS.get(workspace_id, [])]

    async def _delete_documents(workspace_id, document_ids):
        CALLS.append(("delete_documents", sorted(document_ids), None, workspace_id))
        LIVE_DOCS[workspace_id] = [
            i for i in LIVE_DOCS.get(workspace_id, []) if i not in document_ids]
        return True

    async def _upsert_role(workspace_id, user_id, role, chat_id=None):
        ROLE_STORE[f"{workspace_id}:{user_id}"] = role

    async def _get_role(workspace_id, user_id, chat_id=None):
        return ROLE_STORE.get(f"{workspace_id}:{user_id}")

    document_processor.process_batch = _process_batch
    vector_store.get_all_documents = _get_all_documents
    vector_store.delete_documents = _delete_documents
    vector_store.upsert_workspace_role = _upsert_role
    vector_store.get_workspace_role = _get_role

    async def _role_init(workspace_id, user_id, initial_role=None):
        CALLS.append(("role_init", None, initial_role, workspace_id))
        return {}

    workspace_role_integration.initialize_workspace_with_role = _role_init

    # Anything that slips past the stubs to a real model fails fast and is
    # recorded, rather than making a billed call.
    async def _guard_generate(*_a, **_kw):
        MODEL_CALLS.append("generate")
        return "Error generating response: offline suite"

    _gc.gemini_client.generate = _guard_generate

    async def _hydrate(workspace_id, *, user_id=None, recover=True):
        return None

    async def _tag(workspace_id, report, *, log_context=""):
        return report

    async def _meta(workspace_id, user_id):
        CALLS.append(("metadata", None, None, workspace_id))
        return {"caseCategory": "Civil", "subcategories": [], "description": "d",
                "caseTitle": "Fictional Matter"}

    async def _chips(workspace_id, report, role):
        return []

    async def _heal(workspace_id):
        return None

    def _maybe_raise(feature):
        exc = RAISE.get(feature)
        if exc is not None:
            raise exc

    async def _strategy(workspace_id, user_id, workspace_role, batch_result,
                        is_update=False):
        CALLS.append(("generate", "strategy_objective", workspace_role, workspace_id,
                      {"is_update": is_update}))
        _maybe_raise("strategy_objective")
        return {"caseSummary": "A fictional supply dispute.",
                "strategicObjective": "Recover the unpaid invoice amount.",
                "possibleNextSteps": [], "suggestionChips": []}

    async def _analysis(workspace_id, user_id, workspace_role, batch_result,
                        is_update=False):
        CALLS.append(("generate", "analysis_report", workspace_role, workspace_id,
                      {"is_update": is_update}))
        _maybe_raise("analysis_report")
        return {"missingInfo": [], "caseResolutionScore": 61, "generated": True}

    # Each tail stub caches like its real generator, so the reconnect replay
    # has something to replay.
    caches = {
        "list_of_dates": (lambda ws: H._workspace_list_of_dates_payload, lambda ws: [],
                          lambda ws, c: {"workspaceId": ws, "userId": OWNER,
                                         "listOfDates": c}),
        "applicable_laws": (lambda ws: H._workspace_applicable_laws_payload,
                            lambda ws: {"laws": []}, lambda ws, c: c),
        "procedural_timeline": (lambda ws: H._workspace_procedural_timeline_payload,
                                lambda ws: [], lambda ws, c: {"proceduralTimeline": c}),
        "relevant_parties": (lambda ws: H._workspace_relevant_parties_payload,
                             lambda ws: {"parties": []}, lambda ws, c: c),
        "case_laws": (lambda ws: H._workspace_case_laws_payload,
                      lambda ws: {"caseLaws": []}, lambda ws, c: c),
        "drafting_assistant": (None, None, lambda ws, c: {"suggestedDrafts": []}),
    }

    def _gen(feature):
        cache_of, value_of, wire_of = caches[feature]

        async def _generate(workspace_id, user_id, workspace_role=None, **kw):
            CALLS.append(("generate", feature, workspace_role, workspace_id, kw))
            _maybe_raise(feature)
            value = value_of(workspace_id) if value_of else None
            if cache_of:
                cache_of(workspace_id)[workspace_id] = value
            await H._emit_document_feature(
                feature_type=feature, workspace_id=workspace_id, user_id=user_id,
                payload=wire_of(workspace_id, value), usage=None)
        return _generate

    H._hydrate_workspace_caches = _hydrate
    H._tag_analysis_report_against_current_state = _tag
    H._get_workspace_category_and_subcategory = _meta
    H._generate_analysis_report_suggestion_chips = _chips
    H._heal_devanagari_in_document_assessments = _heal
    H._generate_strategies_from_documents = _strategy
    H._generate_analysis_report_from_documents = _analysis
    H._generate_and_emit_list_of_dates = _gen("list_of_dates")
    H._generate_and_emit_applicable_laws = _gen("applicable_laws")
    H._generate_and_emit_procedural_timeline = _gen("procedural_timeline")
    H._generate_and_emit_relevant_parties = _gen("relevant_parties")
    H._generate_and_emit_case_laws = _gen("case_laws")
    H._generate_and_emit_drafting_suggestions = _gen("drafting_assistant")
    async def _templates_synced(*, timeout=None, force=False):
        return None

    H._prewarm_templates = lambda *, timeout=None: None
    H.ensure_templates_synced = _templates_synced
    H._socket_client = SOCKET


def _reset(ws, live=None):
    EMITS.clear()
    DROPPED.clear()
    CALLS.clear()
    PERSISTED.clear()
    RAISE.clear()
    SOCKET.drop = set()
    for d in (H._run_epochs, H._workspace_run_tasks, H._last_workspace_event_at,
              H._pending_feature_deliveries, H._pending_objective_changes,
              H._workspace_strategy_payload, H._workspace_analysis_payload,
              H._workspace_metadata_payload, H._workspace_current_state_payload,
              H._workspace_document_assessments_payload, H._workspace_removed_documents,
              H._workspace_list_of_dates_payload, H._workspace_applicable_laws_payload,
              H._workspace_procedural_timeline_payload,
              H._workspace_relevant_parties_payload, H._workspace_case_laws_payload,
              H._workspace_drafting_payload, H._last_workspace_sync_fingerprint):
        d.pop(ws, None)
    for key in [k for k in ROLE_STORE if k.startswith(f"{ws}:")]:
        ROLE_STORE.pop(key, None)
    for key in [k for k in list(H._role_cache_by_user_workspace) if k.startswith(f"{ws}:")]:
        H._role_cache_by_user_workspace.pop(key, None)
    for key in [k for k in STORE if k[0] == ws]:
        STORE.pop(key, None)
    LIVE_DOCS[ws] = list(live or [])
    P.clear_participants(ws)


def _created(ws, *, role="PETITIONER", evidence=None):
    return {
        "workspaceId": ws, "userId": OWNER, "title": "Fictional Matter 7",
        "workspaceRole": role,
        "evidenceFiles": evidence if evidence is not None else [
            _evidence(DOC_A, "Invoice.pdf"), _evidence(DOC_B, "Notice.pdf")],
        "createdAt": "2026-10-01T09:10:00.000Z",
    }


def _sync(ws, *, evidence, role=None, updated_at=None, deleted=None):
    data = {
        "workspaceId": ws, "userId": OWNER, "title": "Fictional Matter 7",
        "status": "active", "evidenceFiles": evidence,
        "updatedAt": updated_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    if role is not None:
        data["workspaceRole"] = role
    if deleted is not None:
        data["deletedDocumentIds"] = deleted
    return data


async def _settle(timeout=5.0):
    """Wait for every tracked background task (the pipelines) to finish."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pending = [t for t in list(H._background_tasks) if not t.done()]
        if not pending:
            return True
        await asyncio.wait(pending, timeout=max(0.01, end - time.monotonic()))
    return not [t for t in H._background_tasks if not t.done()]


async def _until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def _create_settled(ws, **kw):
    await H.handle("workspace_created", _created(ws, **kw))
    return await _settle()


def _manifest_ids(ws):
    env = H._workspace_document_assessments_payload.get(ws) or {}
    return [e.get("documentId") for e in
            ((env.get("payload") or {}).get("documentAssessments") or [])]


# ---------------------------------------------------------- 1: created suite

async def check_created_suite():
    ws = _ws("created")
    _reset(ws)
    settled = await _create_settled(ws)
    check("the background pipeline finishes", settled)
    emitted = _features_emitted(ws)
    check("workspace_created emits the full suite once each (drafting twice)",
          sorted(emitted) == sorted(FIRST_BUILD)
          and emitted[:2] == SUITE_IN_ORDER[:2], detail=str(emitted))
    check("every generator ran exactly once (drafting twice)",
          sorted(c[1] for c in CALLS if c[0] == "generate")
          == sorted(GENERATED + ["drafting_assistant"]),
          detail=str([c[1] for c in CALLS if c[0] == "generate"]))
    check("evidence is ingested once, with every file",
          [c[1] for c in CALLS if c[0] == "process_batch"] == [sorted([DOC_A, DOC_B])])
    check("the created run is not an update (is_update=False)",
          all(c[4].get("is_update") is False for c in CALLS
              if c[0] == "generate" and c[1] in ("strategy_objective", "analysis_report")))
    check("no workspace:refresh is armed on a first ingestion", not _refreshes(ws))
    check("the manifest cache holds every ingested document",
          sorted(_manifest_ids(ws)) == sorted([DOC_A, DOC_B]))
    check("strategy and analysis payloads are cached for reconnect",
          H._workspace_strategy_payload.get(ws, {}).get("strategicObjective")
          and H._workspace_analysis_payload.get(ws, {}).get("generated") is True)
    check("nothing was left buffered as undelivered",
          not H._pending_feature_deliveries.get(ws))
    check("no call reached a real model", not MODEL_CALLS, detail=str(MODEL_CALLS))

    ws2 = _ws("created_empty")
    _reset(ws2)
    await _create_settled(ws2, evidence=[])
    emitted = _features_emitted(ws2)
    check("a workspace with no evidence still gets analysis + dates terminal emits",
          emitted[:2] == ["analysis_report", "list_of_dates"], detail=str(emitted))
    check("and nothing is ingested or generated for strategy",
          not any(c[0] == "process_batch" for c in CALLS)
          and not any(c[1] == "strategy_objective" for c in CALLS if c[0] == "generate"))


# ------------------------------------------------------- 2: sync merge + rerun

async def check_sync_merge():
    ws = _ws("sync_merge")
    _reset(ws)
    await _create_settled(ws)
    EMITS.clear()
    CALLS.clear()
    evidence = [_evidence(DOC_A, "Invoice.pdf"), _evidence(DOC_B, "Notice.pdf"),
                _evidence(DOC_C, "Reply.pdf")]
    data = _sync(ws, evidence=evidence)
    await H.handle("workspace_sync", data)
    await _settle()
    check("only the new document is ingested on sync",
          [c[1] for c in CALLS if c[0] == "process_batch"] == [[DOC_C]],
          detail=str([c for c in CALLS if c[0] == "process_batch"]))
    wire = _feature_envelopes(ws, "document_assessments")
    wire_ids = [e.get("documentId") for e in
                ((wire[0].get("payload") or {}).get("documentAssessments") or [])] if wire else []
    check("the document_assessments emit stays incremental (new file only)",
          wire_ids == [DOC_C], detail=str(wire_ids))
    check("the cached manifest is MERGED, not overwritten",
          sorted(_manifest_ids(ws)) == sorted([DOC_A, DOC_B, DOC_C]),
          detail=str(_manifest_ids(ws)))
    summary = ((H._workspace_document_assessments_payload.get(ws) or {})
               .get("payload") or {}).get("summary") or {}
    check("the merged manifest's summary counts every document",
          summary.get("totalFiles") == 3 and summary.get("successfulFiles") == 3,
          detail=str(summary))
    check("the sync re-runs strategy and analysis as an update (is_update=True)",
          {c[1] for c in CALLS if c[0] == "generate" and c[4].get("is_update") is True}
          == {"strategy_objective", "analysis_report"})
    emitted = _features_emitted(ws)
    check("the sync re-emits the generated suite in product order",
          [f for f in emitted if f in GENERATED] == GENERATED, detail=str(emitted))
    refreshes = _refreshes(ws)
    armed = set((refreshes[0][1].get("featureTypes") or [])) if refreshes else set()
    check("an upload over existing panels arms its loaders first",
          len(refreshes) == 1 and refreshes[0][0] == 0
          and armed == set(SUITE_IN_ORDER) - {"workspace_metadata"}, detail=str(armed))
    check("every armed loader gets a terminal document_feature_response",
          not _unpaid_loaders(ws), detail=str(_unpaid_loaders(ws)))

    # A replay of the same sync inside the window must not regenerate.
    EMITS.clear()
    CALLS.clear()
    await H.handle("workspace_sync", data)
    await _settle()
    check("a replayed identical workspace_sync is swallowed (no work, no emit)",
          not CALLS and not EMITS, detail=str(CALLS[:3]))

    # A re-ingested document REPLACES its manifest entry rather than duplicating.
    ws2 = _ws("sync_replace")
    _reset(ws2)
    await _create_settled(ws2)
    LIVE_DOCS[ws2] = [DOC_A]  # B's vectors are gone: the sync re-reads it
    CALLS.clear()
    await H.handle("workspace_sync", _sync(ws2, evidence=[
        _evidence(DOC_A, "Invoice.pdf"), _evidence(DOC_B, "Notice.pdf")]))
    await _settle()
    check("a document missing from the store is re-ingested",
          [c[1] for c in CALLS if c[0] == "process_batch"] == [[DOC_B]])
    check("its manifest entry is replaced, not duplicated",
          sorted(_manifest_ids(ws2)) == sorted([DOC_A, DOC_B]),
          detail=str(_manifest_ids(ws2)))


# ------------------------------------------------------ 3: deletion sync

async def check_sync_delete():
    ws = _ws("sync_delete")
    _reset(ws)
    await _create_settled(ws)
    EMITS.clear()
    CALLS.clear()
    await H.handle("workspace_sync", _sync(
        ws, evidence=[_evidence(DOC_A, "Invoice.pdf")], deleted=[DOC_B]))
    await _settle()
    check("the deleted document is removed from the store",
          ("delete_documents", [DOC_B], None, ws) in CALLS)
    check("and pruned from the manifest", _manifest_ids(ws) == [DOC_A],
          detail=str(_manifest_ids(ws)))
    check("and recorded as a removed-evidence gap",
          any(e.get("documentId") == DOC_B
              for e in H._workspace_removed_documents.get(ws) or []))
    refreshes = _refreshes(ws)
    check("a deletion arms the eight regenerated panels",
          len(refreshes) == 1
          and set(refreshes[0][1].get("featureTypes") or []) == set(GENERATED))
    check("every armed loader gets a terminal document_feature_response",
          not _unpaid_loaders(ws), detail=str(_unpaid_loaders(ws)))
    check("nothing is re-ingested on a pure deletion",
          not any(c[0] == "process_batch" for c in CALLS))


# ----------------------------------------------- 4: failing generators

async def _sync_upload_with(ws, raising):
    """Run an upload sync over existing panels with ``raising`` generators."""
    _reset(ws)
    await _create_settled(ws)
    EMITS.clear()
    CALLS.clear()
    RAISE.update(raising)
    await H.handle("workspace_sync", _sync(ws, evidence=[
        _evidence(DOC_A, "Invoice.pdf"), _evidence(DOC_B, "Notice.pdf"),
        _evidence(DOC_C, "Reply.pdf")]))
    await _settle()


async def check_failing_generators():
    ws = _ws("fail_strategy")
    await _sync_upload_with(ws, {"strategy_objective": RuntimeError("model down"),
                                 "analysis_report": RuntimeError("model down")})
    strat = _feature_envelopes(ws, "strategy_objective")
    check("a raising strategy generator re-sends the cached strategy, not an error",
          len(strat) == 1 and not strat[0].get("error")
          and (strat[0].get("payload") or {}).get("strategicObjective")
          == "Recover the unpaid invoice amount.", detail=str(strat)[:200])
    analysis = _feature_envelopes(ws, "analysis_report")
    check("a raising analysis generator is replaced by the fallback report",
          len(analysis) == 1 and not (analysis[0].get("payload") or {}).get("generated"),
          detail=str(analysis)[:200])
    check("the panels after a failure still run and emit",
          _tail_once(_features_emitted(ws)) == TAIL,
          detail=str(_features_emitted(ws)))
    check("no armed loader is left without a terminal emit",
          not _unpaid_loaders(ws), detail=str(_unpaid_loaders(ws)))

    ws2 = _ws("fail_case_laws")
    await _sync_upload_with(ws2, {"case_laws": RuntimeError("search down")})
    cached = {"caseLaws": []}
    laws = _feature_envelopes(ws2, "case_laws")
    check("a raising case_laws generator still ends its loader (cached-or-empty)",
          len(laws) == 1 and laws[0].get("payload") == cached, detail=str(laws)[:200])
    check("and drafting still runs after it",
          "drafting_assistant" in _features_emitted(ws2))

    ws3 = _ws("fail_list_of_dates")
    await _sync_upload_with(ws3, {"list_of_dates": RuntimeError("model down")})
    check("a raising list_of_dates generator does not skip the panels after it",
          _tail_once(_features_emitted(ws3))[1:] == TAIL[1:],
          detail=str(_features_emitted(ws3)))
    check("a raising list_of_dates generator still gets a terminal emit",
        not _unpaid_loaders(ws3), detail=f"unpaid loaders: {sorted(_unpaid_loaders(ws3))}")

    ws4 = _ws("fail_cancelled")
    await _sync_upload_with(ws4, {"applicable_laws": asyncio.CancelledError()})
    check("a CancelledError in the sync pipeline releases its armed loaders",
        not _unpaid_loaders(ws4), detail=f"unpaid loaders: {sorted(_unpaid_loaders(ws4))}")


# ------------------------------------------------- 5: reconnect re-delivery

async def check_reconnect_redelivery():
    ws = _ws("reconnect")
    _reset(ws)
    SOCKET.drop = {"analysis_report"}
    await _create_settled(ws)
    check("a dropped emit is not delivered", any(
        p.get("featureType") == "analysis_report" for _e, p in DROPPED))
    buffered = (H._pending_feature_deliveries.get(ws) or {}).get("analysis_report")
    check("the undelivered envelope is buffered in memory",
          isinstance(buffered, dict) and buffered.get("featureType") == "analysis_report")
    check("and persisted (survives a restart)",
          "analysis_report" in (STORE.get((ws, CacheKinds.PENDING_FEATURE_DELIVERY)) or {}))
    check("the panels after the drop still emit",
          _tail_once(_features_emitted(ws)) == TAIL)

    # The socket comes back.
    SOCKET.drop = set()
    EMITS.clear()
    # The buffered envelope carries its own messageId; the cached replay mints
    # a fresh one. That is how the re-delivered (real) payload is told apart
    # from the replayed snapshot of the same featureType.
    buffered_id = (buffered.get("metadata") or {}).get("messageId")
    await H.handle("disconnect", {})
    await H.handle("connect", {})
    await _until(lambda: len(_feature_envelopes(ws, "analysis_report")) >= 2)
    await _until(lambda: "drafting_assistant" in _features_emitted(ws)
                 or "case_laws" in _features_emitted(ws))
    first = next((p for e, p in EMITS if e == "document_feature_response"), {})
    check("the buffered envelope is re-sent first on reconnect",
          first.get("featureType") == "analysis_report"
          and (first.get("metadata") or {}).get("messageId") == buffered_id,
          detail=str([p.get("featureType") for e, p in EMITS][:4]))
    replay = _features_emitted(ws)[1:]
    check("followed by the cached replay in product order",
          replay[:2] == ["document_assessments", "workspace_metadata"]
          and "analysis_report" in replay, detail=str(replay))
    check("the in-memory buffer is emptied once delivered",
          not (H._pending_feature_deliveries.get(ws) or {}))
    check("and so is the persisted buffer",
          not (STORE.get((ws, CacheKinds.PENDING_FEATURE_DELIVERY)) or {}))

    # Still down at reconnect → kept for the next one, nothing lost.
    ws2 = _ws("reconnect_down")
    _reset(ws2)
    SOCKET.drop = {"list_of_dates"}
    await _create_settled(ws2)
    await H._flush_pending_feature_deliveries(ws2)
    check("a re-delivery that fails again stays buffered",
          "list_of_dates" in (H._pending_feature_deliveries.get(ws2) or {}))
    SOCKET.drop = set()
    H._had_disconnect = False


# ---------------------------------------------------------------- 6: role

async def check_role_resolution():
    ws = _ws("role_lower")
    _reset(ws)
    # Not the default role, so a fallback cannot pass for a normalisation.
    await _create_settled(ws, role="respondent")
    check("a lower-case role is normalised before any generator runs",
          _generated_roles(ws) == {"RESPONDENT"}, detail=str(_generated_roles(ws)))
    check("the role agent is initialised with the validated role",
          [c[2] for c in CALLS if c[0] == "role_init"] == ["RESPONDENT"])
    order = [c[0] for c in CALLS]
    check("role initialisation happens before ingestion and generation",
          order and order[0] == "role_init", detail=str(order[:3]))
    check("the role is persisted for later events",
          ROLE_STORE.get(f"{ws}:{OWNER}") == "RESPONDENT")

    ws2 = _ws("role_invalid")
    _reset(ws2)
    await _create_settled(ws2, role="PLAINTIFF")
    roles = _generated_roles(ws2)
    check("an invalid role never reaches a generator",
          roles and roles <= VALID_ROLES and "PLAINTIFF" not in roles, detail=str(roles))
    check("it falls back to DEFAULT_WORKSPACE_ROLE",
          roles == {DEFAULT_WORKSPACE_ROLE}, detail=str(roles))
    check("and the role agent never sees the invalid role",
          [c[2] for c in CALLS if c[0] == "role_init"] == [DEFAULT_WORKSPACE_ROLE])

    ws3 = _ws("role_sync")
    _reset(ws3)
    await _create_settled(ws3, role="RESPONDENT")
    # Cold memory: only the persisted role survives (as after a restart).
    H._role_cache_by_user_workspace.pop(f"{ws3}:{OWNER}", None)
    CALLS.clear()
    await H.handle("workspace_sync", _sync(ws3, evidence=[
        _evidence(DOC_A, "Invoice.pdf"), _evidence(DOC_B, "Notice.pdf"),
        _evidence(DOC_C, "Reply.pdf")]))
    await _settle()
    roles = _generated_roles(ws3)
    check("a role-less sync uses the persisted role, not a fabricated default",
          roles == {"RESPONDENT"}, detail=str(roles))


# ------------------------------------------------------------------- runner

CHECKS = [
    ("created-suite", check_created_suite),
    ("sync-merge", check_sync_merge),
    ("sync-delete", check_sync_delete),
    ("failing-generators", check_failing_generators),
    ("reconnect", check_reconnect_redelivery),
    ("role", check_role_resolution),
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
    await _settle()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the check names")
    ap.add_argument("--only", nargs="*", default=None, help="run only these checks")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        return 0
    # Pinned: panel citations read the evidence store (Qdrant) at the emit
    # chokepoint; a developer's .env must not turn that on here.
    old_citations = settings.citations_enabled
    settings.citations_enabled = False
    try:
        asyncio.run(_run(set(args.only) if args.only else None))
    finally:
        settings.citations_enabled = old_citations
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
