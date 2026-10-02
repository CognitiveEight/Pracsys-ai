#!/usr/bin/env python3
"""
verify_missing_info_edit.py — Offline checks for `missing_information_edited`.

A lawyer re-words one Missing Information item. Only the text changes: the item
keeps its id, the score and every other list stay as they are, and no model is
called. Then the edit has to SURVIVE: the next regeneration derives missingInfo
from the documents, which still carry the old wording, so the edit ledger
(`_workspace_edited_missing_info`) re-applies it during currentState tagging.

Exercises the real handler (`on_missing_information_edited`) and the real
tagging path with only Gemini and the socket stubbed. No API key, no backend.
A reachable Qdrant is used for write-through if present, and cleaned up.

Covers:
  1. schema        — the exact backend payload parses
  2. apply         — text replaced by id; id, isMatched, score untouched; one emit
  3. redelivery    — same requestId swallowed; older editedAt dropped as stale
  4. viewer        — a `view` collaborator's edit is not applied
  5. regeneration  — tagging re-applies the edit and keeps the item's id
  6. legacy        — an untagged (plain-string) report is matched by wording
  7. cache behind  — item unknown to the cache: recorded, nothing emitted
  8. purge         — the ledger is erased by the workspace-purge sweep

USAGE
    python verify_missing_info_edit.py            # run everything
    python verify_missing_info_edit.py --list     # show the check names
"""

import argparse
import asyncio
import importlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.socket.events import event_handler as H  # noqa: E402
from app.socket.schemas import MissingInformationEditedPayload  # noqa: E402
from app.workspace import participants as P  # noqa: E402
from app.workspace.persistence import (CacheKinds,  # noqa: E402
                                       WORKSPACE_HYDRATION_KINDS)

_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []
GEMINI_CALLS = []

RUN_ID = uuid.uuid4().hex[:10]
TOUCHED_WORKSPACES: set = set()

OWNER = "68b0e1f8d3a9c4b2e1f03456"
EDITOR = "68b0e1f8d3a9c4b2e1f09999"
VIEWER = "68b0e1f8d3a9c4b2e1f07777"
ITEM = "missingInfo_9f2c1a8b4de0"
OTHER = "missingInfo_aaaaaaaaaaaa"

OLD = ("Certified copies of the disputed Bone Density Test and Tanner-Whitehouse 3 "
       "radiological assessment reports are missing.")
NEW = ("Certified copies of the disputed Bone Density Test and Tanner-Whitehouse 3 "
       "radiological assessment reports from the treating hospital are missing.")
UNRELATED = "Birth certificate of the accused is not on record."


def _ws(name: str) -> str:
    ws = f"{name}_{RUN_ID}"
    TOUCHED_WORKSPACES.add(ws)
    return ws


class _FakeSocket:
    # Must return True: SocketClient.emit reports DELIVERY.
    async def emit(self, event, payload):
        EMITS.append((event, payload))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", payload))
        return True


H._socket_client = _FakeSocket()


def _stub_gemini():
    """The edit path must not call the model; the tagging pass may (semantic
    match) — answer 'no matches' so only the deterministic logic is tested."""
    async def _gen(*a, prompt="", **k):
        GEMINI_CALLS.append(prompt or (a[0] if a else ""))
        return json.dumps({"matches": []})
    _gc.gemini_client.generate = _gen


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _analysis_emits():
    return [p["payload"] for e, p in EMITS
            if e == "document_feature_response" and p.get("featureType") == "analysis_report"]


def _seed(ws, *, tagged=True):
    if tagged:
        missing = [
            {"id": ITEM, "text": OLD, "isMatched": True},
            {"id": OTHER, "text": UNRELATED, "isMatched": False},
        ]
    else:
        missing = [OLD, UNRELATED]
    H._workspace_analysis_payload[ws] = {
        "missingInfo": missing, "strengths": [], "weaknesses": [], "risks": [],
        "relevantJurisdiction": [], "caseResolutionScore": 62,
        "qualityScoreExplanation": "Explanation.", "reportQualityScoreExplanation": "Explanation.",
        "pendingActions": [], "nextSteps": [], "suggestedDrafts": [],
    }
    H._workspace_current_state_payload[ws] = {
        "missingInfo": [{"id": ITEM, "text": OLD}, {"id": OTHER, "text": UNRELATED}],
    }
    H._workspace_edited_missing_info.pop(ws, None)
    P.set_participants(ws, [
        {"userId": EDITOR, "name": "Editor", "permission": "edit"},
        {"userId": VIEWER, "name": "Viewer", "permission": "view"},
    ])


def _event(ws, *, request_id=None, edited_by=EDITOR, new=NEW, prev=OLD,
           edited_at="2026-10-02T09:14:22.118Z", item=ITEM):
    return {
        "requestId": request_id or f"mie_{uuid.uuid4().hex[:8]}",
        "workspaceId": ws,
        "userId": OWNER,
        "editedBy": {"userId": edited_by},
        "itemId": item,
        "previousText": prev,
        "newText": new,
        "editedAt": edited_at,
        "timestamp": edited_at,
    }


def _item(report, item_id):
    return next((x for x in report.get("missingInfo") or []
                 if isinstance(x, dict) and x.get("id") == item_id), None)


# ------------------------------------------------------------------ checks

def check_schema():
    print("\n[1] Schema")
    p = MissingInformationEditedPayload.model_validate({
        "requestId": "mie_68c1f0a9_1759395262118",
        "workspaceId": "68c1f0a9e4b0d2a1c3f45678",
        "userId": OWNER,
        "editedBy": {"userId": EDITOR},
        "itemId": ITEM,
        "previousText": OLD,
        "newText": NEW,
        "editedAt": "2026-10-02T09:14:22.118Z",
        "timestamp": "2026-10-02T09:14:22.118Z",
    })
    check("payload parses", p.itemId == ITEM and p.newText == NEW)
    check("editedBy parsed", p.editedBy is not None and p.editedBy.userId == EDITOR)
    check("hydration reads the edit ledger",
          CacheKinds.EDITED_MISSING_INFO in WORKSPACE_HYDRATION_KINDS)


async def check_apply():
    print("\n[2] Apply")
    ws = _ws("mie_apply")
    _seed(ws)
    EMITS.clear()
    GEMINI_CALLS.clear()
    await H.on_missing_information_edited(_event(ws))
    out = _analysis_emits()
    check("exactly one analysis_report emitted", len(out) == 1, f"got {len(out)}")
    report = out[-1] if out else {}
    item = _item(report, ITEM) or {}
    check("text replaced", item.get("text") == NEW)
    check("id kept", bool(item))
    check("isMatched kept", item.get("isMatched") is True)
    check("other item untouched", (_item(report, OTHER) or {}).get("text") == UNRELATED)
    check("score untouched", report.get("caseResolutionScore") == 62)
    check("explanation untouched", report.get("qualityScoreExplanation") == "Explanation.")
    check("no model call", not GEMINI_CALLS, f"{len(GEMINI_CALLS)} call(s)")
    check("cache updated", (_item(H._workspace_analysis_payload[ws], ITEM) or {}).get("text") == NEW)
    cs = H._workspace_current_state_payload[ws]["missingInfo"]
    check("currentState follows the edit",
          next(x for x in cs if x["id"] == ITEM)["text"] == NEW)
    ledger = H._workspace_edited_missing_info.get(ws) or {}
    check("ledger recorded with prior wording",
          ledger.get(ITEM, {}).get("text") == NEW and OLD in ledger[ITEM]["priorTexts"])


async def check_redelivery():
    print("\n[3] Redelivery")
    ws = _ws("mie_redeliver")
    _seed(ws)
    first = _event(ws, request_id="mie_dup", edited_at="2026-10-02T09:14:22.118Z")
    await H.on_missing_information_edited(first)
    EMITS.clear()
    await H.on_missing_information_edited(first)
    check("same requestId swallowed", not _analysis_emits())

    later_text = NEW + " Request them under Section 94 BNSS."
    await H.on_missing_information_edited(
        _event(ws, new=later_text, prev=NEW, edited_at="2026-10-02T09:20:00.000Z"))
    EMITS.clear()
    # A late re-send of an OLDER edit under a different requestId.
    await H.on_missing_information_edited(
        _event(ws, new="stale wording", prev=OLD, edited_at="2026-10-02T09:15:00.000Z"))
    check("older editedAt dropped as stale", not _analysis_emits())
    check("newer edit stands",
          (_item(H._workspace_analysis_payload[ws], ITEM) or {}).get("text") == later_text)


async def check_viewer():
    print("\n[4] Viewer")
    ws = _ws("mie_viewer")
    _seed(ws)
    EMITS.clear()
    await H.on_missing_information_edited(_event(ws, edited_by=VIEWER))
    out = _analysis_emits()
    check("unchanged report re-emitted", len(out) == 1
          and (_item(out[0], ITEM) or {}).get("text") == OLD)
    check("cache untouched", (_item(H._workspace_analysis_payload[ws], ITEM) or {}).get("text") == OLD)
    check("nothing recorded", not H._workspace_edited_missing_info.get(ws))


async def check_regeneration():
    print("\n[5] Regeneration")
    ws = _ws("mie_regen")
    _seed(ws)
    await H.on_missing_information_edited(_event(ws))

    # A sync regenerates from the documents: the model's ORIGINAL wording again.
    fresh = {"missingInfo": [OLD, UNRELATED], "weaknesses": [], "risks": [],
             "relevantJurisdiction": []}
    tagged = await H._tag_analysis_report_against_current_state(ws, fresh)
    item = _item(tagged, ITEM)
    check("edited item keeps its id", item is not None,
          json.dumps(tagged.get("missingInfo"))[:300])
    check("lawyer's wording wins", (item or {}).get("text") == NEW)
    check("not duplicated", sum(1 for x in tagged["missingInfo"] if x["text"] == NEW) == 1)

    # The frontend's next currentState carries the edited text; a regeneration
    # that drops the gap entirely must not resurrect it.
    resolved = {"missingInfo": [UNRELATED], "weaknesses": [], "risks": [],
                "relevantJurisdiction": []}
    tagged = await H._tag_analysis_report_against_current_state(ws, resolved)
    check("a resolved gap is not re-introduced",
          [x["text"] for x in tagged["missingInfo"]] == [UNRELATED])


async def check_legacy():
    print("\n[6] Legacy untagged report")
    ws = _ws("mie_legacy")
    _seed(ws, tagged=False)
    EMITS.clear()
    await H.on_missing_information_edited(_event(ws))
    out = _analysis_emits()
    report = out[-1] if out else {}
    item = _item(report, ITEM)
    check("matched by wording and tagged with itemId", item is not None,
          json.dumps(report.get("missingInfo"))[:300])
    check("text replaced", (item or {}).get("text") == NEW)


async def check_cache_behind():
    print("\n[7] Cache behind the frontend")
    ws = _ws("mie_behind")
    _seed(ws)
    EMITS.clear()
    await H.on_missing_information_edited(
        _event(ws, item="missingInfo_ffffffffffff", prev="Some item we never saw."))
    check("nothing emitted", not _analysis_emits())
    check("edit recorded for the next regeneration",
          "missingInfo_ffffffffffff" in (H._workspace_edited_missing_info.get(ws) or {}))


async def check_purge_sweep():
    print("\n[8] Purge sweep")
    ws = _ws("mie_purge")
    _seed(ws)
    await H.on_missing_information_edited(_event(ws))
    H._clear_workspace_memory(ws)
    check("ledger erased", ws not in H._workspace_edited_missing_info)


async def _cleanup():
    try:
        from app.vectorstore.qdrant import vector_store
    except Exception:
        vector_store = None
    for ws in sorted(TOUCHED_WORKSPACES):
        if vector_store is not None:
            try:
                await vector_store.delete_workspace(ws)
            except Exception:
                pass
        P.clear_participants(ws)
        H._clear_workspace_memory(ws)


CHECKS = [
    ("schema", check_schema),
    ("apply", check_apply),
    ("redelivery", check_redelivery),
    ("viewer", check_viewer),
    ("regeneration", check_regeneration),
    ("legacy", check_legacy),
    ("cache_behind", check_cache_behind),
    ("purge", check_purge_sweep),
]


async def main():
    _stub_gemini()
    for _, fn in CHECKS:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
    await _cleanup()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list the check groups and exit")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        sys.exit(0)
    sys.exit(asyncio.run(main()))
