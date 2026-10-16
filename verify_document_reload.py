#!/usr/bin/env python3
"""
verify_document_reload.py — Offline checks for `document_reload_request`.

The lawyer hits "reload" on one document in Supporting Material (or the backend
does, because the document scored badly). The AI must purge everything it holds
for that file, read it again from a freshly-signed URL, and re-run the
workspace feature suite over the FULL evidence set — without losing the
document's identity and without wiping the items the lawyer already curated.

The rules under test, all of them load-bearing:

  * the OLD copy goes before the NEW one arrives — vectors deleted and the
    assessments manifest pruned BEFORE re-ingestion, or the inventory every
    feature prompt embeds asserts an unreadable document as ground truth;
  * the document keeps its backend id — resolved from targetFile / evidenceFiles
    / the cached manifest, never invented, because a fabricated id creates a
    second copy no later delete can reach;
  * a SUCCESSFUL reload is not a removal — the removed-evidence ledger is
    written only when the re-read fails, and cleared when it succeeds;
  * ``currentState`` survives — approved items keep their ids;
  * re-sends under the same ``requestId`` are expected: swallowed while in
    flight or just completed, but a retry after a FAILURE must run.

Exercises the real handler (``on_document_reload_request``) with Qdrant, the
document processor, the feature generators and the socket stubbed. No API key,
no backend, no Qdrant.

Covers:
  1. schema        — the backend's payload (the spec sample) parses
  2. id resolution — _id / fileHash / storeFileName / manifest / ambiguous
  3. happy path    — purge-then-read ordering, emits, manifest merge, ledger
  4. curation      — currentState is cached and approved items keep their ids
  5. failure       — a dead re-read is reported as a gap, loaders still settle
  6. dedupe        — in-flight and completed re-sends skipped, failures retried
  7. lifecycle     — pending purge revoked, roster not clobbered, role resolved

USAGE
    python verify_document_reload.py            # run everything
    python verify_document_reload.py --list     # show the check names

REQUIREMENTS
    Run inside the project venv so `app` is importable. Nothing else.
"""

import argparse
import asyncio
import copy
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Silence the write-through persistence before any handler state is touched:
# every cache assignment would otherwise fire a background Qdrant upsert.
from app.workspace import persistence as _persistence  # noqa: E402

_persistence.persist_state = lambda *a, **k: None

from app.processing.document_processor import document_processor  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.schemas import DocumentReloadRequestPayload  # noqa: E402
from app.vectorstore.qdrant import vector_store  # noqa: E402
from app.workspace import participants as P  # noqa: E402

RESULTS = []
#: Ordered trace of everything the handler did to the outside world, so the
#: "purge before re-read" ordering can be asserted rather than assumed.
CALLS = []
EMITS = []

RUN_ID = uuid.uuid4().hex[:10]

OWNER = "68b0e1f8d3a9c4b2e1f03456"
DOC_ID = "68c1f0a9e4b0d2a1c3f45671"      # the document being reloaded
OTHER_ID = "68c1f0a9e4b0d2a1c3f45672"    # a second, untouched document
TARGET_HASH = "9f2e7c1a5b8d43e6a0c9f71b2d84e5a3"

NINA = "66f1a2b3c4d5e6f708091a02"
ROSTER = [{"userId": NINA, "name": "Nina Mehta", "permission": "edit"}]


def _ws(name: str) -> str:
    return f"{name}_{RUN_ID}"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


# --------------------------------------------------------------------- payload

def _evidence(doc_id, name, file_hash, *, with_id=True):
    f = {
        "originalName": name,
        "storeFileName": f"68c1f0a9_{name}",
        "filename": name.rsplit(".", 1)[0],
        "extension": name.rsplit(".", 1)[-1],
        "fileHash": file_hash,
        "url": f"https://storage.googleapis.com/{name}?X-Goog-Signature=sig",
        "path": f"workspaces/ws/evidence/{name}",
        "uploadedAt": "2026-09-14T08:12:44.000Z",
    }
    if with_id:
        f["_id"] = doc_id
    return f


def _payload(ws, *, request_id=None, with_ids=True, current_state=True,
             participants=None, target_id=None):
    """The backend's document_reload_request, as specified."""
    target = _evidence(DOC_ID, "FIR_CR-2026-45871.pdf", TARGET_HASH,
                       with_id=bool(target_id))
    if target_id:
        target["_id"] = target_id
    target["previousQualityScore"] = 27
    data = {
        "requestId": request_id or f"dlr_{DOC_ID[:8]}_1758067200000",
        "workspaceId": ws,
        "userId": OWNER,
        "title": "New Client 101",
        "status": "active",
        "workspaceRole": "PETITIONER",
        "reason": "user_requested",
        "targetFile": target,
        "evidenceFiles": [
            _evidence(DOC_ID, "FIR_CR-2026-45871.pdf", TARGET_HASH,
                      with_id=with_ids),
            _evidence(OTHER_ID, "Inventory_Annexure_B.pdf", "aa11bb22cc33",
                      with_id=with_ids),
        ],
        "timestamp": "2026-09-17T10:31:07.412Z",
    }
    if current_state:
        data["currentState"] = {
            "missingInfo": [{"id": "mi_01",
                             "text": "Inventory list with serial numbers",
                             "status": "approved"}],
            "risks": [{"id": "rk_01", "text": "Limitation may have expired",
                       "status": "pending"}],
            "weaknesses": [{"id": "wk_01", "text": "No eyewitness statement",
                            "status": "rejected"}],
            "relevantJurisdiction": [{"id": "rj_01",
                                      "text": "Sessions Court, Pune",
                                      "status": "approved"}],
            "relevantParties": {"approved": [{"id": "rp_01",
                                              "name": "Michael Turner"}],
                                "pending": []},
            "listOfDates": {"approved": [{"id": "ld_01", "date": "2026-05-14",
                                          "title": "Alleged incident"}],
                            "pending": []},
            "applicableLaws": [{"act": "Bharatiya Nyaya Sanhita, 2023",
                                "section": "306"}],
        }
    if participants is not None:
        data["participants"] = participants
    return data


# ----------------------------------------------------------------------- stubs

class _FakeSocket:
    # Both return True: SocketClient.emit reports DELIVERY, and a double that
    # returns None reads as "the socket dropped it", which makes the handlers
    # buffer for re-delivery and report failure.
    async def emit(self, event, payload):
        EMITS.append((event, payload))
        CALLS.append(("emit", event, (payload or {}).get("featureType")))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", payload))
        return True


def _batch_result(score=91, success=True):
    return {
        "total_files": 1,
        "successful": 1 if success else 0,
        "failed": 0 if success else 1,
        "average_readability_score": score if success else 0,
        "results": [{
            "document_id": DOC_ID,
            "filename": "FIR_CR-2026-45871",
            "success": success,
            "readability_score": score if success else 0,
            "readability_rating": "Excellent" if success else "Unreadable",
            "readable": success,
            "extracted_text_length": 8214 if success else 0,
            "stored_in_vector_db": success,
            "summary": "First information report naming the accused.",
            "evidentiary_strength": "High",
            "document_type": "fir",
            "extracted_fields": {},
            "sensitive_fields": {},
        }],
    }


#: What process_batch does on the next call: a result, or an exception to raise.
NEXT_INGEST = {"result": _batch_result(), "raise": None}
SCHEDULED_TAIL = []


def _install_stubs():
    """Replace every outward call the handler makes. Recorded in CALLS."""

    async def _process_batch(files, workspace_id, user_id):
        CALLS.append(("process_batch", [f.get("_id") for f in files]))
        if NEXT_INGEST["raise"]:
            raise NEXT_INGEST["raise"]
        return copy.deepcopy(NEXT_INGEST["result"])

    async def _delete_documents(workspace_id, document_ids):
        CALLS.append(("delete_documents", list(document_ids)))
        return True

    async def _get_all_documents(workspace_id, limit_per_batch=100):
        return [{"document_id": DOC_ID, "content": "x" * 40, "uploaded_at": ""},
                {"document_id": OTHER_ID, "content": "y" * 40, "uploaded_at": ""}]

    document_processor.process_batch = _process_batch
    vector_store.delete_documents = _delete_documents
    vector_store.get_all_documents = _get_all_documents

    async def _hydrate(workspace_id, *, user_id=None):
        return None

    async def _remember(workspace_id, user_id, role):
        return None

    async def _resolve(workspace_id, user_id, preferred_role=None):
        CALLS.append(("resolve_role", preferred_role))
        return (preferred_role or "ADJUDICATOR").strip().upper()

    async def _strategy(workspace_id, user_id, workspace_role, batch_result,
                        is_update=False):
        CALLS.append(("generate_strategy", workspace_role,
                      bool(batch_result), is_update))
        return {
            "workspaceId": workspace_id, "userId": user_id,
            "role": workspace_role,
            "caseSummary": "An FIR alleges abetment of suicide.",
            "reportCaseSummary": "An FIR alleges abetment of suicide.",
            "strategicObjective": "Secure discharge for the accused.",
            "possibleNextSteps": ["File a discharge application."],
            "suggestionChips": ["chip"] * 12,
        }

    async def _analysis(workspace_id, user_id, workspace_role, batch_result,
                        is_update=False):
        CALLS.append(("generate_analysis", workspace_role, bool(batch_result)))
        return {
            "missingInfo": ["Inventory list with serial numbers",
                            "Post-mortem report"],
            "strengths": [], "weaknesses": [], "risks": [],
            "relevantJurisdiction": [], "caseResolutionScore": 61,
            "qualityScoreExplanation": "", "reportQualityScoreExplanation": "",
            "pendingActions": [], "nextSteps": [], "suggestedDrafts": [],
        }

    async def _meta(workspace_id, user_id):
        CALLS.append(("workspace_metadata_generated",))
        return {"caseCategory": "Criminal", "subcategories": ["FIR"],
                "description": "Abetment of suicide."}

    def _schedule(workspace_id, user_id, workspace_role, batch_result, *,
                  log_context="", deleted_doc_ids=None):
        CALLS.append(("schedule_tail", bool(batch_result), deleted_doc_ids))
        SCHEDULED_TAIL.append({"batch_result": batch_result,
                               "deleted_doc_ids": deleted_doc_ids})

    H._hydrate_workspace_caches = _hydrate
    H._remember_workspace_role = _remember
    H._resolve_workspace_role = _resolve
    H._generate_strategies_from_documents = _strategy
    H._generate_analysis_report_from_documents = _analysis
    H._get_workspace_category_and_subcategory = _meta
    H._schedule_list_of_dates_and_applicable_laws = _schedule
    H._socket_client = _FakeSocket()


def _seed_manifest(ws, target_score=27):
    """Cached document_assessments: the manifest the reload must not shrink."""
    H._workspace_document_assessments_payload[ws] = {
        "featureType": "document_assessments",
        "workspaceId": ws, "userId": OWNER,
        "payload": {
            "documentAssessments": [
                {"documentId": DOC_ID, "filename": "FIR_CR-2026-45871",
                 "success": True, "readabilityScore": target_score,
                 "documentType": "fir", "summary": "Barely legible scan.",
                 "extractedFields": {}, "sensitiveFields": {}},
                {"documentId": OTHER_ID, "filename": "Inventory_Annexure_B",
                 "success": True, "readabilityScore": 88,
                 "documentType": "inventory", "summary": "Inventory annexure.",
                 "extractedFields": {}, "sensitiveFields": {}},
            ],
            "summary": {"totalFiles": 2, "successfulFiles": 2, "failedFiles": 0,
                        "averageReadabilityScore": (target_score + 88) / 2},
        },
    }


def _reset(ws):
    CALLS.clear()
    EMITS.clear()
    SCHEDULED_TAIL.clear()
    NEXT_INGEST["result"] = _batch_result()
    NEXT_INGEST["raise"] = None
    for cache in (H._workspace_document_assessments_payload,
                  H._workspace_analysis_payload, H._workspace_strategy_payload,
                  H._workspace_metadata_payload, H._workspace_removed_documents,
                  H._workspace_current_state_payload,
                  H._workspace_rejected_analysis_items):
        cache.pop(ws, None)
    H._document_reload_requests.pop(ws, None)
    P.clear_participants(ws)
    _seed_manifest(ws)


def _manifest(ws):
    cached = H._workspace_document_assessments_payload.get(ws) or {}
    return (cached.get("payload") or {}).get("documentAssessments") or []


def _emitted(feature):
    """The inner ``payload`` block of every emit for one featureType."""
    return [p.get("payload") or {} for e, p in EMITS
            if e == "document_feature_response" and p.get("featureType") == feature]


def _call_index(name):
    for i, c in enumerate(CALLS):
        if c[0] == name:
            return i
    return -1


# ------------------------------------------------------------------- 1: schema

def check_schema():
    print("\n[1] Schema: the backend's document_reload_request parses")
    ws = _ws("schema")
    p = DocumentReloadRequestPayload(**_payload(ws))
    check("requestId parsed", p.requestId.startswith("dlr_"))
    check("userId is the case owner", p.userId == OWNER)
    check("targetFile fileHash parsed", p.targetFile.fileHash == TARGET_HASH)
    check("previousQualityScore parsed", p.targetFile.previousQualityScore == 27)
    check("targetFile carries no id in the backend contract",
          p.targetFile.id is None)
    check("evidenceFiles parsed with ids",
          [f.id for f in p.evidenceFiles] == [DOC_ID, OTHER_ID])
    check("currentState approved item parsed",
          p.currentState.missingInfo[0].id == "mi_01")
    check("reason parsed", p.reason == "user_requested")

    bare = DocumentReloadRequestPayload(
        requestId="dlr_x_1", workspaceId=ws, userId=OWNER,
        targetFile=_evidence(DOC_ID, "a.pdf", TARGET_HASH, with_id=False))
    check("participants absent is None, not []", bare.participants is None)
    check("currentState is optional at the schema level",
          bare.currentState is None)
    check("evidenceFiles is optional at the schema level",
          bare.evidenceFiles is None)

    shared = DocumentReloadRequestPayload(**_payload(ws, participants=ROSTER))
    check("participants parsed on a shared case",
          shared.participants[0].permission == "edit")

    extra = DocumentReloadRequestPayload(
        **{**_payload(ws), "somethingNewFromBackend": True})
    check("an unknown field does not reject the reload", extra.requestId != "")


# ------------------------------------------------------------ 2: id resolution

def check_id_resolution():
    print("\n[2] Target id: resolved from the payload, never invented")
    ws = _ws("idres")
    _reset(ws)

    by_hash = DocumentReloadRequestPayload(**_payload(ws))
    check("resolved from the evidenceFiles entry with the same fileHash",
          H._resolve_reload_document_id(ws, by_hash) == DOC_ID)

    direct = DocumentReloadRequestPayload(**_payload(ws, target_id="explicit_id"))
    check("an _id on targetFile wins",
          H._resolve_reload_document_id(ws, direct) == "explicit_id")

    # fileHash changed on re-signing: storeFileName / path still identify it.
    rehashed = _payload(ws)
    rehashed["targetFile"]["fileHash"] = "0000different0000"
    check("falls back to storeFileName when the hash no longer matches",
          H._resolve_reload_document_id(
              ws, DocumentReloadRequestPayload(**rehashed)) == DOC_ID)

    # No ids anywhere in the payload: the cached manifest is the last resort.
    no_ids = DocumentReloadRequestPayload(**_payload(ws, with_ids=False))
    check("falls back to a unique filename match in the cached manifest",
          H._resolve_reload_document_id(ws, no_ids) == DOC_ID)

    # Two cached documents sharing the display name — guessing would purge the
    # wrong one, so it must refuse.
    dupes = copy.deepcopy(H._workspace_document_assessments_payload[ws])
    dupes["payload"]["documentAssessments"][1]["filename"] = "FIR_CR-2026-45871"
    H._workspace_document_assessments_payload[ws] = dupes
    check("an ambiguous filename match resolves to nothing",
          H._resolve_reload_document_id(ws, no_ids) is None)

    H._workspace_document_assessments_payload.pop(ws, None)
    check("no ids and no manifest resolves to nothing",
          H._resolve_reload_document_id(ws, no_ids) is None)


async def check_unresolvable_aborts():
    print("\n[2b] An unresolvable target aborts before anything is touched")
    ws = _ws("idabort")
    _reset(ws)
    H._workspace_document_assessments_payload.pop(ws, None)
    await H.on_document_reload_request(_payload(ws, with_ids=False))
    check("nothing was deleted from the vector store",
          _call_index("delete_documents") == -1)
    check("nothing was re-ingested", _call_index("process_batch") == -1)
    check("no panel loader was lit",
          not [e for e, _ in EMITS if e == "workspace:refresh"])
    check("the request is marked failed so a re-send can retry",
          H._document_reload_requests.get(ws, {}).get(
              _payload(ws)["requestId"], {}).get("status") == "failed")


# --------------------------------------------------------------- 3: happy path

async def check_happy_path():
    print("\n[3] Happy path: purge, re-read, re-run")
    ws = _ws("happy")
    _reset(ws)
    await H.on_document_reload_request(_payload(ws))

    check("workspace:refresh is the first thing emitted",
          EMITS and EMITS[0][0] == "workspace:refresh")
    refresh = EMITS[0][1] if EMITS else {}
    check("the document_assessments loader is lit (an emit is promised)",
          "document_assessments" in (refresh.get("featureTypes") or []))
    check("every rebuilt panel's loader is lit",
          {"strategy_objective", "analysis_report", "list_of_dates",
           "applicable_laws", "procedural_timeline"}
          <= set(refresh.get("featureTypes") or []))

    check("the old copy was deleted, and only the target",
          ("delete_documents", [DOC_ID]) in CALLS)
    check("exactly one document was re-ingested",
          ("process_batch", [DOC_ID]) in CALLS)
    check("the purge happens BEFORE the re-read",
          -1 < _call_index("delete_documents") < _call_index("process_batch"))
    check("the loaders are lit before the purge, not after it",
          -1 < _call_index("emit") < _call_index("delete_documents"))

    emitted_assessments = (_emitted("document_assessments") or [{}])[0]
    check("the emit describes the reloaded file",
          [e.get("documentId")
           for e in emitted_assessments.get("documentAssessments", [])] == [DOC_ID])
    entries = {e["documentId"]: e for e in _manifest(ws)}
    check("the manifest still holds both documents (merge, not replace)",
          set(entries) == {DOC_ID, OTHER_ID}, f"got {sorted(entries)}")
    check("the reloaded document carries its NEW score",
          entries.get(DOC_ID, {}).get("readabilityScore") == 91)
    check("the untouched document is unchanged",
          entries.get(OTHER_ID, {}).get("readabilityScore") == 88)

    check("document_assessments emitted", len(_emitted("document_assessments")) == 1)
    check("workspace_metadata emitted", len(_emitted("workspace_metadata")) == 1)
    meta = (_emitted("workspace_metadata") or [{}])[0]
    check("a reload never renames the workspace",
          meta.get("updateTitle") is False and meta.get("workspaceTitle") == "")
    check("strategy_objective emitted", len(_emitted("strategy_objective")) == 1)
    check("analysis_report emitted", len(_emitted("analysis_report")) == 1)

    check("the feature tail was scheduled",
          len(SCHEDULED_TAIL) == 1)
    check("the tail is told a document was re-read (not a pure deletion)",
          SCHEDULED_TAIL and SCHEDULED_TAIL[0]["batch_result"] is not None)
    check("a successful reload passes no deleted ids",
          SCHEDULED_TAIL and SCHEDULED_TAIL[0]["deleted_doc_ids"] is None)

    check("a successful reload is NOT recorded as removed evidence",
          not [e for e in (H._workspace_removed_documents.get(ws) or [])
               if e.get("documentId") == DOC_ID])
    check("the request is marked done",
          H._document_reload_requests[ws][_payload(ws)["requestId"]]["status"]
          == "done")


async def check_stale_ledger_cleared():
    print("\n[3b] A reload heals a document that was previously reported missing")
    ws = _ws("heal")
    _reset(ws)
    H._workspace_removed_documents[ws] = [{
        "documentId": DOC_ID, "fileName": "FIR_CR-2026-45871",
        "documentType": "fir", "keyFields": {}, "removedAt": "2026-09-16T00:00:00Z"}]
    await H.on_document_reload_request(_payload(ws))
    check("the removed-evidence entry is cleared once the document is back",
          not [e for e in (H._workspace_removed_documents.get(ws) or [])
               if e.get("documentId") == DOC_ID])


# ------------------------------------------------------------- 4: the curation

async def check_curation_survives():
    print("\n[4] currentState: the lawyer's curation survives the re-run")
    ws = _ws("curate")
    _reset(ws)
    await H.on_document_reload_request(_payload(ws))

    cached = H._workspace_current_state_payload.get(ws) or {}
    check("currentState is cached for every regeneration path",
          [i["id"] for i in (cached.get("missingInfo") or [])] == ["mi_01"])

    report = (_emitted("analysis_report") or [{}])[0]
    items = report.get("missingInfo") or []
    matched = [i for i in items if isinstance(i, dict) and i.get("isMatched")]
    check("the approved item keeps its id",
          [i["id"] for i in matched] == ["mi_01"],
          f"got {[(i.get('id'), i.get('isMatched')) for i in items]}")
    check("a genuinely new finding is tagged new",
          any(isinstance(i, dict) and i.get("isMatched") is False
              and i.get("text") == "Post-mortem report" for i in items))

    _reset(ws)
    H._workspace_current_state_payload[ws] = {
        "missingInfo": [{"id": "mi_99", "text": "Inventory list with serial numbers"}]}
    await H.on_document_reload_request(_payload(ws, current_state=False))
    check("an omitted currentState does not clobber the cached one",
          (H._workspace_current_state_payload.get(ws) or {})
          .get("missingInfo", [{}])[0].get("id") == "mi_99")


# ------------------------------------------------------------------ 5: failure

async def check_failed_reload():
    print("\n[5] A dead re-read is reported honestly, and settles the loaders")
    ws = _ws("fail")
    _reset(ws)
    NEXT_INGEST["raise"] = RuntimeError("GCS 403: signature expired")
    await H.on_document_reload_request(_payload(ws))

    check("the document is recorded as removed evidence",
          [e for e in (H._workspace_removed_documents.get(ws) or [])
           if e.get("documentId") == DOC_ID])
    check("the removed entry keeps the document's type for the gap text",
          (H._workspace_removed_documents.get(ws) or [{}])[0]
          .get("documentType") == "fir")
    check("the promised document_assessments emit still happens (loader settles)",
          len(_emitted("document_assessments")) == 1)
    check("the settling emit is the cached manifest minus the dead document",
          [e.get("documentId")
           for e in (_emitted("document_assessments") or [{}])[0]
           .get("documentAssessments", [])] == [OTHER_ID])
    check("the panels are still regenerated against what survives",
          len(_emitted("strategy_objective")) == 1
          and len(_emitted("analysis_report")) == 1)
    check("workspace_metadata is NOT re-derived from a failed read",
          not _emitted("workspace_metadata"))
    check("the tail is told the document is gone",
          SCHEDULED_TAIL and SCHEDULED_TAIL[0]["deleted_doc_ids"] == [DOC_ID])
    check("the request is marked failed so the backend's re-send retries",
          H._document_reload_requests[ws][_payload(ws)["requestId"]]["status"]
          == "failed")

    # A re-read that returns a result in which nothing succeeded is the same
    # failure wearing a different hat.
    _reset(ws)
    NEXT_INGEST["result"] = _batch_result(success=False)
    await H.on_document_reload_request(_payload(ws))
    check("a zero-success batch counts as a failed reload",
          bool([e for e in (H._workspace_removed_documents.get(ws) or [])
                if e.get("documentId") == DOC_ID]))


# ------------------------------------------------------------------- 6: dedupe

async def check_dedupe():
    print("\n[6] requestId: re-sends are expected, failures still retry")
    ws = _ws("dedupe")
    _reset(ws)
    data = _payload(ws)
    await H.on_document_reload_request(data)
    first_reads = len([c for c in CALLS if c[0] == "process_batch"])
    await H.on_document_reload_request(dict(data))
    check("the same requestId is not re-read",
          len([c for c in CALLS if c[0] == "process_batch"]) == first_reads)
    check("the duplicate emits nothing at all",
          len(_emitted("analysis_report")) == 1)

    await H.on_document_reload_request(_payload(ws, request_id="dlr_other_2"))
    check("a different requestId runs",
          len([c for c in CALLS if c[0] == "process_batch"]) == first_reads + 1)

    # A retry of a FAILED request must run — nothing else would ever fix it.
    _reset(ws)
    NEXT_INGEST["raise"] = RuntimeError("transient GCS error")
    await H.on_document_reload_request(data)
    NEXT_INGEST["raise"] = None
    NEXT_INGEST["result"] = _batch_result()
    await H.on_document_reload_request(dict(data))
    check("a re-send after a failure re-runs the reload",
          len([c for c in CALLS if c[0] == "process_batch"]) == 2)
    check("and the retry heals the ledger",
          not [e for e in (H._workspace_removed_documents.get(ws) or [])
               if e.get("documentId") == DOC_ID])

    # An in-flight request is skipped rather than run twice concurrently.
    _reset(ws)
    H._mark_reload_request(ws, data["requestId"], "in_flight")
    await H.on_document_reload_request(data)
    check("an in-flight request is not started a second time",
          not [c for c in CALLS if c[0] == "process_batch"])


# ---------------------------------------------------------------- 7: lifecycle

async def check_lifecycle():
    print("\n[7] Workspace lifecycle: purge revoked, roster kept, role resolved")
    ws = _ws("life")
    _reset(ws)

    H._pending_purges[ws] = {"requestId": "wd_1", "purgeAfter": "2026-09-24",
                             "userId": OWNER, "reason": "user_deleted",
                             "task": None}
    P.set_participants(ws, ROSTER)
    await H.on_document_reload_request(_payload(ws))
    check("reload traffic revokes a pending compliance purge",
          ws not in H._pending_purges)
    roster = P.get_participants(ws) or []
    check("a reload without participants does NOT clear the roster",
          len(roster) == 1,
          "a reload is a document operation and says nothing about sharing")

    check("the payload role is what the generators run under",
          ("generate_strategy", "PETITIONER", True, True) in CALLS
          and ("generate_analysis", "PETITIONER", True) in CALLS)
    check("the role gate is consulted with the payload's preferred role",
          ("resolve_role", "PETITIONER") in CALLS)

    _reset(ws)
    P.set_participants(ws, ROSTER)
    await H.on_document_reload_request(
        _payload(ws, request_id="dlr_share_9", participants=[]))
    check("an explicit empty roster is applied",
          (P.get_participants(ws) or []) == [])


# -------------------------------------------------------------------- runner

CHECKS = [
    ("schema", check_schema),
    ("id-resolution", check_id_resolution),
    ("id-abort", check_unresolvable_aborts),
    ("happy-path", check_happy_path),
    ("ledger-heal", check_stale_ledger_cleared),
    ("curation", check_curation_survives),
    ("failure", check_failed_reload),
    ("dedupe", check_dedupe),
    ("lifecycle", check_lifecycle),
]


async def _run(selected):
    _install_stubs()
    for name, fn in CHECKS:
        if selected and name not in selected:
            continue
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show the check names")
    ap.add_argument("--only", nargs="*", default=None,
                    help="run only these checks")
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
