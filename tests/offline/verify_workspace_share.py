#!/usr/bin/env python3
"""
verify_workspace_share.py — Offline checks for workspace sharing.

Sharing reaches this service through two additive fields:

  * ``workspace_sync.participants`` — the collaborator roster (``edit`` /
    ``view``). ABSENT ENTIRELY, not an empty array, when the case is not
    shared.
  * ``user_message_request.askedBy`` — present only when someone other than
    the case owner typed the message. ``userId`` on that payload stays the
    OWNER, so no owner-scoped cache is re-keyed.

The rule under test: a collaborator with ``view`` permission may ask anything,
but must not be able to change the case — no panel edits, no objective
rewrite, no role flip — and the reply must say so instead of implying the
change landed.

Exercises the real chat path (``on_user_message_request`` → panel-edit
classifier → per-panel editors) with only Gemini and the socket stubbed. No
API key, no backend, no Qdrant.

Covers:
  1. schemas       — the exact backend payloads parse, and stay optional
  2. roster        — set / absent-means-unshared / restore / hydrate / purge
  3. resolve_asker — owner, editor, viewer, stranger, cold-cache
  4. viewer e2e    — an edit instruction changes nothing and is disclosed
  5. editor e2e    — the same instruction from an `edit` collaborator applies
  6. owner e2e     — no askedBy behaves exactly as before sharing existed
  7. role gate     — a viewer cannot flip the workspace role
  8. prompt        — the asker reaches the chat prompt as attribution only

USAGE
    python tests/offline/verify_workspace_share.py            # run everything
    python tests/offline/verify_workspace_share.py --list     # show the check names

REQUIREMENTS
    Run inside the project venv so `app` is importable. Nothing else.
"""

import argparse
import asyncio
import importlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

from app.socket.events import event_handler as H  # noqa: E402
from app.socket.schemas import (UserMessageRequestPayload,  # noqa: E402
                                WorkspaceSyncPayload,
                                WorkspaceVersionRestorePayload)
from app.workspace import participants as P  # noqa: E402

_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []

#: Every workspace id this run touches carries this suffix. The service
#: PERSISTS per-workspace state (role cache, workspace meta, the user-fact
#: ledger, panel payloads) to Qdrant, so with a real Qdrant reachable a fixed
#: id would let one run's writes become the next run's starting state — which
#: fails checks for reasons that have nothing to do with the code under test.
#: `_cleanup_workspaces()` deletes them again at the end.
RUN_ID = uuid.uuid4().hex[:10]
TOUCHED_WORKSPACES: set = set()


def _ws(name: str) -> str:
    """Run-unique workspace id, registered for cleanup."""
    ws = f"{name}_{RUN_ID}"
    TOUCHED_WORKSPACES.add(ws)
    return ws


async def _cleanup_workspaces() -> None:
    """Delete everything this run wrote, in Qdrant and in memory."""
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


OWNER = "66f1a2b3c4d5e6f708091a01"
NINA = "66f1a2b3c4d5e6f708091a02"   # edit
RAJ = "66f1a2b3c4d5e6f708091a03"    # view
CHAT = "66f1a2b3c4d5e6f708091a99"

ROSTER = [
    {"userId": NINA, "name": "Nina Mehta", "permission": "edit"},
    {"userId": RAJ, "name": "Raj Kapoor", "permission": "view"},
]


class _FakeSocket:
    # Both return True: SocketClient.emit reports DELIVERY, and a double that
    # returns None reads as "the socket dropped it", which makes the handlers
    # buffer for re-delivery and report failure.
    async def emit(self, event, payload):
        EMITS.append((event, payload))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", payload))
        return True


H._socket_client = _FakeSocket()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _reply():
    return next((p.get("content", "") for e, p in EMITS if e == "ai_response"), "")


def _features():
    out = {}
    for e, p in EMITS:
        if e == "document_feature_response":
            out.setdefault(p["featureType"], []).append(p["payload"])
    return out


def _seed(ws, amount="16 lakh"):
    """Minimal panel state — enough for the panel-edit path to be reachable."""
    H._workspace_strategy_payload[ws] = {
        "workspaceId": ws, "userId": OWNER, "role": "PETITIONER",
        "caseSummary": f"A pending amount of {amount} USD is being withheld.",
        "reportCaseSummary": f"A total of {amount} USD is withheld.",
        "strategicObjective": f"Secure release of the outstanding {amount} USD.",
        "possibleNextSteps": [f"Issue a demand notice for {amount} USD."],
        "suggestionChips": ["chip"] * 12,
    }
    H._workspace_analysis_payload[ws] = {
        "missingInfo": [f"Proof of the {amount} USD claim."], "strengths": [],
        "weaknesses": [], "risks": [], "relevantJurisdiction": [],
        "caseResolutionScore": 50, "qualityScoreExplanation": "",
        "reportQualityScoreExplanation": "", "pendingActions": [],
        "nextSteps": [], "suggestedDrafts": [],
    }
    H._workspace_user_facts.pop(ws, None)
    P.set_participants(ws, ROSTER)


EDIT_CLS = {k: {"shouldEdit": False, "instruction": ""} for k in (
    "caseSummary", "reportCaseSummary", "strategicObjective",
    "possibleNextSteps", "analysisReport", "listOfDates")}
EDIT_CLS.update({
    "isRename": False, "isRoleReassignment": False, "reassignRole": "",
    "reassignParty": "", "isFactCorrection": True,
    "factCorrections": [{"was": "16 lakh", "now": "6 lakh USD",
                         "description": "the disputed amount"}],
    "isQuestionLike": False,
})

EDITORS = {
    "edit the existing Strategic Objective": json.dumps({
        "strategicObjective": "Secure release of the outstanding 6 lakh USD."}),
    '"caseSummary"': json.dumps({
        "caseSummary": "A pending amount of 6 lakh USD is being withheld.",
        "reportCaseSummary": "A total of 6 lakh USD is withheld."}),
    '"possibleNextSteps"': json.dumps({
        "possibleNextSteps": ["Issue a demand notice for 6 lakh USD."],
        "suggestionChips": ["chip"] * 12}),
    '"missingInfo"': json.dumps({
        "missingInfo": ["Proof of the 6 lakh USD claim."], "strengths": [],
        "weaknesses": [], "risks": [], "relevantJurisdiction": [],
        "caseResolutionScore": 55, "qualityScoreExplanation": "",
        "reportQualityScoreExplanation": "", "pendingActions": [],
        "nextSteps": [], "suggestedDrafts": []}),
}

PROMPTS_SEEN = []
FALLBACK_ANSWER = "The limitation point turns on when the cause of action arose."


def _stub():
    """Gemini stub. Records every prompt so the attribution check can read it."""
    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        PROMPTS_SEEN.append(p)
        if "STRICT intent classifier" in p:
            return json.dumps(EDIT_CLS)
        if "WRITE THE CONFIRMATION MESSAGE" in p:
            return ""           # force the templated fallback
        for needle, reply in EDITORS.items():
            if needle in p:
                return reply
        return FALLBACK_ANSWER
    _gc.gemini_client.generate = _gen


CORRECTION = ("update the case summary and the strategic objective to reflect "
              "that the disputed amount is only 6 lakh USD, not 16 lakh")


async def _send(ws, message, asked_by=None, chat_type="ANALYSIS_REPORT"):
    EMITS.clear()
    PROMPTS_SEEN.clear()
    data = {
        "content": message,
        "chat_type": chat_type,
        "conversation_history": [],
        "workspaceId": ws,
        "userId": OWNER,          # always the OWNER, shared or not
        "chatId": CHAT,
        "timestamp": "2026-09-09T10:04:11.000Z",
    }
    if asked_by:
        data["askedBy"] = asked_by
    await H.on_user_message_request(data)


# ------------------------------------------------------------------ 1: schemas

def check_schemas():
    print("\n[1] Schemas: the backend's payloads parse, and stay optional")

    sync = WorkspaceSyncPayload(**{
        "workspaceId": "ws1", "userId": OWNER,
        "title": "Sharma v State of Maharashtra", "status": "active",
        "workspaceRole": "petitioner", "evidenceFiles": [],
        "participants": ROSTER,
        "updatedAt": "2026-09-09T10:04:11.000Z",
    })
    check("workspace_sync participants parsed", len(sync.participants or []) == 2)
    check("participant permission preserved",
          [p.permission for p in sync.participants] == ["edit", "view"])
    check("participant name preserved", sync.participants[0].name == "Nina Mehta")
    check("workspace userId is still the owner", sync.userId == OWNER)

    absent = WorkspaceSyncPayload(workspaceId="ws1", userId=OWNER,
                                  updatedAt="2026-09-09T10:04:11.000Z")
    check("participants absent is None, not []", absent.participants is None)

    odd = WorkspaceSyncPayload(workspaceId="ws1", userId=OWNER,
                               updatedAt="t",
                               participants=[{"userId": NINA,
                                              "permission": "commenter"}])
    check("unknown permission does not reject the sync",
          odd.participants[0].permission == "commenter")

    msg = UserMessageRequestPayload(**{
        "content": "What is our exposure on the limitation point?",
        "chat_type": "ANALYSIS_REPORT", "workspaceId": "ws1", "userId": OWNER,
        "chatId": CHAT,
        "askedBy": {"userId": NINA, "name": "Nina Mehta"},
        "timestamp": "2026-09-09T10:04:11.000Z",
    })
    check("askedBy parsed", msg.askedBy and msg.askedBy.userId == NINA)
    check("askedBy name parsed", msg.askedBy.name == "Nina Mehta")
    check("message userId is still the owner", msg.userId == OWNER)

    plain = UserMessageRequestPayload(content="hi", chat_type="ANALYSIS_REPORT",
                                      workspaceId="ws1", userId=OWNER,
                                      chatId=CHAT, timestamp="t")
    check("askedBy absent is None (unshared case)", plain.askedBy is None)

    restore = WorkspaceVersionRestorePayload(
        workspaceId="ws1", userId=OWNER, updatedAt="t", versionId="v1")
    check("version restore inherits participants (absent here)",
          restore.participants is None)


# ------------------------------------------------------------------- 2: roster

def check_roster():
    print("\n[2] Roster: set / absent-means-unshared / restore / hydrate / purge")
    ws = _ws('verify_roster')
    P.clear_participants(ws)

    P.set_participants(ws, ROSTER, authoritative=True)
    check("roster stored", len(P.get_participants(ws)) == 2)
    check("lookup by userId", (P.get_participant(ws, RAJ) or {}).get("permission")
          == "view")
    check("unknown userId is not on the roster", P.get_participant(ws, "nobody") is None)

    # A version restore carries no sharing state — it must not wipe the roster.
    P.set_participants(ws, None, authoritative=False)
    check("version restore keeps the roster", len(P.get_participants(ws)) == 2)

    # workspace_sync omitting the field means the case is no longer shared.
    P.set_participants(ws, None, authoritative=True)
    check("sync without participants un-shares", P.get_participants(ws) == [])

    # Hydration only fills a cold workspace.
    P.hydrate_participants_from_cache(ws, ROSTER)
    check("cold hydration restores the roster", len(P.get_participants(ws)) == 2)
    P.hydrate_participants_from_cache(ws, [{"userId": "zzz", "permission": "edit"}])
    check("hydration never overwrites live state",
          P.get_participant(ws, "zzz") is None)

    P.clear_participants(ws)
    check("purge forgets the roster (names are personal data)",
          P.get_participants(ws) == [] and not P.has_roster(ws))


# ------------------------------------------------------------ 3: resolve_asker

def check_resolve():
    print("\n[3] resolve_asker: owner, editor, viewer, stranger, cold cache")
    ws = _ws('verify_resolve')
    P.set_participants(ws, ROSTER)

    owner = P.resolve_asker(ws, OWNER, None)
    check("no askedBy is the owner", owner.is_owner and owner.can_edit)

    self_owner = P.resolve_asker(ws, OWNER, {"userId": OWNER, "name": "Owner"})
    check("askedBy == owner is the owner", self_owner.is_owner and self_owner.can_edit)

    nina = P.resolve_asker(ws, OWNER, {"userId": NINA, "name": "Nina Mehta"})
    check("edit collaborator may write",
          nina.can_edit and not nina.is_owner and nina.permission == "edit")
    check("collaborator name resolved", nina.name == "Nina Mehta")

    raj = P.resolve_asker(ws, OWNER, {"userId": RAJ, "name": "Raj Kapoor"})
    check("view collaborator is read-only", raj.is_read_only and raj.known)
    check("read-only asker carries no owner rights", not raj.is_owner)

    stranger = P.resolve_asker(ws, OWNER, {"userId": "someone-else"})
    check("stranger against a known roster is read-only",
          not stranger.can_edit and not stranger.known)

    # Name falls back to the roster when askedBy omits it.
    check("name falls back to the roster",
          P.resolve_asker(ws, OWNER, {"userId": RAJ}).name == "Raj Kapoor")

    P.clear_participants(ws)
    cold = P.resolve_asker(ws, OWNER, {"userId": NINA})
    check("no roster cached fails open (roster not synced yet)",
          cold.can_edit and not cold.known)


# ---------------------------------------------------------------- 4: viewer e2e

async def check_viewer_blocked():
    print("\n[4] End-to-end: a view-only collaborator cannot change the case")
    ws = _ws('verify_view')
    _seed(ws)
    _stub()
    before = json.dumps(H._workspace_strategy_payload[ws], sort_keys=True)

    await _send(ws, CORRECTION, asked_by={"userId": RAJ, "name": "Raj Kapoor"})

    after = json.dumps(H._workspace_strategy_payload[ws], sort_keys=True)
    check("strategy panel untouched", before == after)
    check("still says 16 lakh",
          "16 lakh" in H._workspace_strategy_payload[ws]["caseSummary"])
    check("analysis report untouched",
          "16 lakh" in json.dumps(H._workspace_analysis_payload[ws]))
    check("no fact pinned to the ledger", not H.get_user_asserted_facts(ws))
    check("no panel re-emitted", not _features(), str(sorted(_features())))

    reply = _reply()
    check("a reply was still sent", bool(reply.strip()), repr(reply))
    check("reply discloses the view-only block", "view-only access" in reply, reply)
    check("reply does not claim the edit landed",
          "I've updated" not in reply and "I have updated" not in reply, reply)


# ---------------------------------------------------------------- 5: editor e2e

async def check_editor_allowed():
    print("\n[5] End-to-end: an `edit` collaborator applies the same change")
    ws = _ws('verify_edit')
    _seed(ws)
    _stub()

    await _send(ws, CORRECTION, asked_by={"userId": NINA, "name": "Nina Mehta"})

    strat = H._workspace_strategy_payload[ws]
    check("case summary corrected", "16 lakh" not in strat["caseSummary"],
          strat["caseSummary"])
    check("strategic objective corrected", "16 lakh" not in strat["strategicObjective"],
          strat["strategicObjective"])
    check("panels re-emitted", "strategy_objective" in _features(),
          str(sorted(_features())))
    check("no view-only notice on an allowed edit",
          "view-only access" not in _reply(), _reply())


# ----------------------------------------------------------------- 6: owner e2e

async def check_owner_unchanged():
    print("\n[6] Regression: no askedBy behaves exactly as before sharing existed")
    ws = _ws('verify_owner')
    _seed(ws)
    P.clear_participants(ws)      # unshared case: no roster at all
    _stub()

    await _send(ws, CORRECTION)

    strat = H._workspace_strategy_payload[ws]
    check("owner edit still applies", "16 lakh" not in strat["caseSummary"],
          strat["caseSummary"])
    check("owner reply carries no sharing notice",
          "view-only access" not in _reply(), _reply())
    check("no asker section in the prompt for an unshared case",
          not any("WHO IS ASKING" in p for p in PROMPTS_SEEN))


# ---------------------------------------------------------------- 7: role gate

async def check_role_gate():
    print("\n[7] A view-only collaborator cannot flip the workspace role")
    ws = _ws("verify_role")
    _seed(ws)
    _stub()
    H._role_cache_by_user_workspace[f"{ws}:{OWNER}"] = "PETITIONER"

    await _send(ws, "MEDIATOR", asked_by={"userId": RAJ, "name": "Raj Kapoor"})

    check("role cache unchanged",
          H._role_cache_by_user_workspace.get(f"{ws}:{OWNER}") == "PETITIONER",
          str(H._role_cache_by_user_workspace.get(f"{ws}:{OWNER}")))
    check("reply explains why the role did not change",
          "view-only access" in _reply(), _reply())

    # The owner sending the same word still selects the role.
    EMITS.clear()
    await _send(ws, "MEDIATOR")
    check("owner can still select a role",
          H._role_cache_by_user_workspace.get(f"{ws}:{OWNER}") == "MEDIATOR",
          str(H._role_cache_by_user_workspace.get(f"{ws}:{OWNER}")))



# ------------------------------------------------------------------- 8: prompt

async def check_prompt_attribution():
    print("\n[8] The asker reaches the prompt as attribution only")
    ws = _ws('verify_prompt')
    _seed(ws)
    _stub()

    await _send(ws, "What is our exposure on the limitation point?",
                asked_by={"userId": RAJ, "name": "Raj Kapoor"})

    asker_prompts = [p for p in PROMPTS_SEEN if "WHO IS ASKING" in p]
    check("asker section built", bool(asker_prompts))
    blob = "\n".join(asker_prompts)
    check("collaborator named in the prompt", "Raj Kapoor" in blob)
    check("access level stated", "view-only" in blob)
    check("analysis explicitly unaffected by access level",
          "answer exactly as you would for the owner" in blob.lower())
    check("collaborator is not presented as a party to the dispute",
          "do not treat them as a party to the dispute" in blob.lower())
    check("a question from a viewer is not treated as an edit",
          "view-only access" not in _reply(), _reply())


CHECKS = [
    ("schemas", check_schemas),
    ("roster", check_roster),
    ("resolve", check_resolve),
    ("viewer", check_viewer_blocked),
    ("editor", check_editor_allowed),
    ("owner", check_owner_unchanged),
    ("role", check_role_gate),
    ("prompt", check_prompt_attribution),
]


async def main():
    for _, fn in CHECKS:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
    await _cleanup_workspaces()
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
    ap.add_argument("--list", action="store_true",
                    help="list the check groups and exit")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        sys.exit(0)
    sys.exit(asyncio.run(main()))
