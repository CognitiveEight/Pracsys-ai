#!/usr/bin/env python3
"""
verify_fact_corrections.py — Offline checks for chat-driven workspace edits.

Exercises the real code path a lawyer's chat correction takes
(``on_user_message_request`` → panel-edit classifier → per-panel editors →
``document_feature_response``) with only two things stubbed: the Gemini calls
and the socket. No API key, no backend, no Qdrant.

Covers:
  1. no-op detection      — an editor that changes nothing must report so
  2. applicable laws      — index-merge keeps ``isMatched``; a bad edit is rejected
  3. fact ledger          — supersession, prompt block, field allowlist
  4. precheck gate        — corrections reach the classifier, questions do not
  5. end-to-end           — "the disputed amount is 6 lakh, not 16 lakh"
  6. honest ack           — when nothing matched, the reply must not claim success
  7. rename regression    — the pre-existing rename path is unchanged

USAGE
    python verify_fact_corrections.py            # run everything
    python verify_fact_corrections.py --list     # show the check names

REQUIREMENTS
    Run inside the project venv so `app` is importable. Nothing else.
"""

import argparse
import asyncio
import importlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.config import settings  # noqa: E402

settings.enable_fact_correction_fanout = True

from app.socket.events import event_handler as H  # noqa: E402

_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []


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

UID, CHAT = "verify_user", "verify_chat"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"\n        {detail}" if detail and not ok else ""))


def _reply(payload_key="content"):
    return next((p.get(payload_key, "") for e, p in EMITS if e == "ai_response"), "")


def _features():
    out = {}
    for e, p in EMITS:
        if e == "document_feature_response":
            out.setdefault(p["featureType"], []).append(p["payload"])
    return out


async def _send(ws, message, chat_type="ANALYSIS_REPORT"):
    EMITS.clear()
    await H.on_user_message_request({
        "content": message,
        "chat_type": chat_type,
        "conversation_history": [],
        "workspaceId": ws,
        "userId": UID,
        "chatId": CHAT,
        "timestamp": "2026-01-01T00:00:00Z",
    })


def _seed(ws, amount="16 lakh"):
    H._workspace_strategy_payload[ws] = {
        "workspaceId": ws, "userId": UID, "role": "PETITIONER",
        "caseSummary": f"A pending amount of {amount} USD is being withheld.",
        "reportCaseSummary": f"A total of {amount} USD is withheld.",
        "strategicObjective": f"Secure release of the outstanding {amount} USD.",
        "possibleNextSteps": [f"Issue a demand notice for {amount} USD.",
                              "Engage a cyber-forensic expert."],
        "suggestionChips": ["chip"] * 12,
    }
    H._workspace_analysis_payload[ws] = {
        "missingInfo": [f"Proof of the {amount} USD claim."], "strengths": [],
        "weaknesses": [], "risks": [], "relevantJurisdiction": [],
        "caseResolutionScore": 50, "qualityScoreExplanation": "",
        "reportQualityScoreExplanation": "", "pendingActions": [],
        "nextSteps": [f"Recover {amount} USD."], "suggestedDrafts": [],
    }
    H._workspace_list_of_dates_payload[ws] = [{
        "date": "2026-02-28", "title": f"Glitch recorded, {amount} disputed",
        "description": "Sales worth 16,00,000 were recorded from single clicks.",
        "evidenceSnippet": f"multiple sales were recorded totalling {amount}",
    }]
    H._workspace_applicable_laws_payload[ws] = {"laws": [{
        "id": "law_1", "act": "Indian Contract Act, 1872", "section": "70",
        "description": f"Recovery of {amount} USD for non-gratuitous acts.",
        "relevance": "High", "isMatched": True,
    }]}
    H._workspace_procedural_timeline_payload[ws] = [
        {"date": "2026-04-28", "title": f"Campaign terminated over the {amount} dispute"}
    ]
    H._workspace_user_facts.pop(ws, None)


def _stub(classification, editors):
    """Install a Gemini stub: `classification` for the classifier, `editors`
    (a prompt-substring -> JSON string mapping) for everything else."""
    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        if "STRICT intent classifier" in p:
            return json.dumps(classification)
        if "WRITE THE CONFIRMATION MESSAGE" in p:
            return ""           # force our templated fallback, so we test our text
        for needle, reply in editors.items():
            if needle in p:
                return reply
        return "{}"
    _gc.gemini_client.generate = _gen


# --------------------------------------------------------------- 1 + 2: editors

async def check_editors():
    print("\n[1] Editor no-op detection and applicable-laws merge")
    ws = "verify_editors"
    _seed(ws)
    base = dict(H._workspace_strategy_payload[ws])

    async def _echo_obj(*a, prompt="", **k):
        return json.dumps({"strategicObjective": base["strategicObjective"]})
    _gc.gemini_client.generate = _echo_obj
    ok = await H._regenerate_strategic_objective_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_feedback="change 16 lakh to 6 lakh", current_strategy=dict(base))
    check("objective no-op returns False", ok is False, f"got {ok}")

    async def _echo_ws(*a, prompt="", **k):
        return json.dumps({"strategicObjective":
                           "  " + base["strategicObjective"].replace(" ", "  ") + "\n"})
    _gc.gemini_client.generate = _echo_ws
    ok = await H._regenerate_strategic_objective_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_feedback="x", current_strategy=dict(base))
    check("whitespace-only difference counts as no-op", ok is False, f"got {ok}")

    async def _real(*a, prompt="", **k):
        return json.dumps({"strategicObjective": "Secure release of 6 lakh USD."})
    _gc.gemini_client.generate = _real
    ok = await H._regenerate_strategic_objective_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_feedback="x", current_strategy=dict(base))
    check("real change returns True", ok is True, f"got {ok}")
    check("real change preserves case summary",
          H._workspace_strategy_payload[ws]["caseSummary"] == base["caseSummary"])

    laws = json.loads(json.dumps(H._workspace_applicable_laws_payload[ws]))
    async def _echo_laws(*a, prompt="", **k):
        return json.dumps(laws)
    _gc.gemini_client.generate = _echo_laws
    ok = await H._regenerate_applicable_laws_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER", user_feedback="x")
    check("applicable-laws no-op returns False", ok is False, f"got {ok}")

    changed = json.loads(json.dumps(laws))
    changed["laws"][0]["description"] = "Recovery of 6 lakh USD."
    async def _chg(*a, prompt="", **k):
        return json.dumps(changed)
    _gc.gemini_client.generate = _chg
    ok = await H._regenerate_applicable_laws_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER", user_feedback="x")
    cached = H._workspace_applicable_laws_payload[ws]
    check("applicable-laws real change returns True", ok is True)
    check("applicable-laws keeps isMatched", cached["laws"][0].get("isMatched") is True,
          str(cached["laws"][0]))

    broken = json.loads(json.dumps(laws))
    broken["laws"][0]["description"] = "now charged under the IPC instead"
    async def _bad(*a, prompt="", **k):
        return json.dumps(broken)
    _gc.gemini_client.generate = _bad
    before = len(H._workspace_applicable_laws_payload[ws]["laws"])
    ok = await H._regenerate_applicable_laws_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER", user_feedback="x")
    check("applicable-laws IPC-leak edit rejected", ok is False)
    check("applicable-laws cache intact after rejection",
          len(H._workspace_applicable_laws_payload[ws]["laws"]) == before)


# ------------------------------------------------------------------ 3: ledger

async def check_ledger():
    print("\n[2] Fact ledger: supersession, prompt block, field allowlist")
    ws = "verify_ledger"
    H._workspace_user_facts.pop(ws, None)

    H.add_user_asserted_fact(ws, was="16 lakh", now="6 lakh USD",
                             description="the disputed amount")
    H.add_user_asserted_fact(ws, was="6 lakh USD", now="8 lakh USD",
                             description="the disputed amount")
    facts = H.get_user_asserted_facts(ws)
    check("supersession keeps exactly one entry", len(facts) == 1, str(facts))
    check("current value is the latest", facts[0]["now"] == "8 lakh USD", str(facts[0]))
    check("original wrong value retained", facts[0]["was"] == "16 lakh", str(facts[0]))
    check("intermediate value marked superseded",
          "6 lakh USD" in facts[0]["superseded"], str(facts[0]))

    block = H._build_user_facts_block(ws)
    check("prompt block states the corrected value", "8 lakh USD" in block)
    check("prompt block forbids superseded values", "NOT 16 lakh" in block, block[:200])
    check("prompt block keeps anti-invention scope",
          "NOT authorise inventing" in block, block[:400])
    check("empty ledger renders no block", H._build_user_facts_block("nobody") == "")

    H._workspace_user_facts.pop(ws, None)
    H.add_user_asserted_fact(ws, was="16 lakh", now="6 lakh USD",
                             description="the disputed amount")
    payload = {"listOfDates": [{
        "date": "2026-02-28", "title": "Glitch of 16 lakh recorded",
        "description": "A sum of 16,00,000 was disputed.",
        "evidenceSnippet": "the parties agree the sum of 16 lakh is withheld",
    }]}
    n = H._apply_fact_corrections_to_payload(ws, payload, label="list_of_dates")
    row = payload["listOfDates"][0]
    check("substitution ran", n >= 2, f"n={n}")
    check("title corrected", "6 lakh USD" in row["title"], row["title"])
    check("Indian-grouped 16,00,000 corrected", "16,00,000" not in row["description"],
          row["description"])
    check("ISO date untouched", row["date"] == "2026-02-28", row["date"])
    check("evidenceSnippet NOT falsified", "16 lakh" in row["evidenceSnippet"],
          row["evidenceSnippet"])


# -------------------------------------------------------------- 4: precheck

def check_gate():
    print("\n[3] Precheck gate: corrections reach the classifier, questions do not")
    from app.socket.handlers.chat import _PANEL_EDIT_VERB_RE, _CORRECTION_SHAPE_RE
    panel_words = ("case summary", "objective", "strategy", "next step", "summary",
                   "analysis", "report", "risk", "name", "workspace", "everywhere")

    def gate(msg, chat_type=""):
        low = msg.lower()
        corr = (len(msg.strip()) >= 12 and bool(_CORRECTION_SHAPE_RE.search(msg))
                and not H._is_pure_informational_query(msg))
        return corr or (bool(_PANEL_EDIT_VERB_RE.search(msg))
                        and (any(w in low for w in panel_words)
                             or chat_type == "STRATEGY_OBJECTIVE"))

    for msg, ct, want in [
        ("the disputed amount is 6 lakh USD, not 16 lakh", "", True),
        ("it's showing 16 lakh, it should be 6 lakh", "", True),
        ("the hearing is on 22.09.2026, not 02.09.2026", "", True),
        ("remove the 3rd line from case summary", "", True),
        ("correct the name Rambur with Rambir", "", True),
        ("change the wordings", "STRATEGY_OBJECTIVE", True),
        ("What is the total amount credited to the account?", "", False),
        ("what does section 138 say about the 15 day notice period?", "", False),
        ("Can you explain the risks in this matter?", "", False),
        ("hi there", "", False),
    ]:
        check(f"[{'fire' if want else 'skip'}] {msg[:56]}", gate(msg, ct) == want)


# ------------------------------------------------------------ 5: end-to-end

FACT_CLS = {k: {"shouldEdit": False, "instruction": ""} for k in (
    "caseSummary", "reportCaseSummary", "strategicObjective",
    "possibleNextSteps", "analysisReport", "listOfDates")}
FACT_CLS.update({
    "isRename": False, "isRoleReassignment": False, "reassignRole": "",
    "reassignParty": "", "isFactCorrection": True,
    "factCorrections": [{"was": "16 lakh", "now": "6 lakh USD",
                         "description": "the disputed amount"}],
    "isQuestionLike": False,
})


async def check_end_to_end():
    print("\n[4] End-to-end: 'the disputed amount is 6 lakh USD, not 16 lakh'")
    ws = "verify_e2e"
    _seed(ws)
    _stub(FACT_CLS, {
        "edit the existing Strategic Objective": json.dumps({
            "strategicObjective": "Secure release of the outstanding 6 lakh USD."}),
        '"caseSummary"': json.dumps({
            "caseSummary": "A pending amount of 6 lakh USD is being withheld.",
            "reportCaseSummary": "A total of 6 lakh USD is withheld."}),
        '"possibleNextSteps"': json.dumps({
            "possibleNextSteps": ["Issue a demand notice for 6 lakh USD.",
                                  "Engage a cyber-forensic expert."],
            "suggestionChips": ["chip"] * 12}),
        '"missingInfo"': json.dumps({
            "missingInfo": ["Proof of the 6 lakh USD claim."], "strengths": [],
            "weaknesses": [], "risks": [], "relevantJurisdiction": [],
            "caseResolutionScore": 55, "qualityScoreExplanation": "",
            "reportQualityScoreExplanation": "", "pendingActions": [],
            "nextSteps": [], "suggestedDrafts": []}),
    })
    await _send(ws, "update the entire intelligence panel, strategy and objective and "
                    "the possible next steps to reflect that the disputed amount is "
                    "only 6 lakh USD, not 16 lakh")
    feats = _features()
    strat = H._workspace_strategy_payload[ws]

    for panel in ("strategy_objective", "analysis_report", "list_of_dates",
                  "applicable_laws", "procedural_timeline"):
        check(f"{panel} emitted (loader cleared)", panel in feats, str(sorted(feats)))
    check("one coalesced strategy emit", len(feats.get("strategy_objective", [])) == 1)
    check("case summary corrected", "16 lakh" not in strat["caseSummary"], strat["caseSummary"])
    check("strategic objective corrected", "16 lakh" not in strat["strategicObjective"],
          strat["strategicObjective"])
    check("next steps corrected", not any("16 lakh" in s for s in strat["possibleNextSteps"]),
          str(strat["possibleNextSteps"]))
    check("analysis report corrected",
          "16 lakh" not in json.dumps(H._workspace_analysis_payload[ws]))
    check("analysis report kept nextSteps (merge-back)",
          "nextSteps" in H._workspace_analysis_payload[ws])
    lod = feats["list_of_dates"][0]["listOfDates"][0]
    check("dates corrected deterministically", "16 lakh" not in lod["title"], lod["title"])
    check("dates ISO date untouched", lod["date"] == "2026-02-28")
    check("dates evidenceSnippet NOT falsified", "16 lakh" in lod["evidenceSnippet"])
    check("laws corrected", "16 lakh" not in
          feats["applicable_laws"][0]["laws"][0]["description"])
    check("laws isMatched preserved",
          feats["applicable_laws"][0]["laws"][0].get("isMatched") is True)
    facts = H.get_user_asserted_facts(ws)
    check("correction pinned for future regenerations",
          len(facts) == 1 and facts[0]["now"] == "6 lakh USD", str(facts))
    check("pinned fact will reach later prompts",
          "6 lakh USD" in H._build_user_facts_block(ws))


# ------------------------------------------------------------- 6: honest ack

async def check_honest_ack():
    print("\n[5] Honest ack: nothing matched must not be reported as success")
    ws = "verify_ack"
    _seed(ws)
    base = dict(H._workspace_strategy_payload[ws])
    cls = json.loads(json.dumps(FACT_CLS))
    cls["factCorrections"] = [{"was": "99 lakh", "now": "7 lakh USD",
                               "description": "the retainer fee"}]
    _stub(cls, {
        "edit the existing Strategic Objective":
            json.dumps({"strategicObjective": base["strategicObjective"]}),
        '"caseSummary"': json.dumps({"caseSummary": base["caseSummary"],
                                     "reportCaseSummary": base["reportCaseSummary"]}),
        '"possibleNextSteps"': json.dumps({"possibleNextSteps": base["possibleNextSteps"],
                                           "suggestionChips": ["chip"] * 12}),
    })
    await _send(ws, "the retainer fee is 7 lakh USD, not 99 lakh")
    reply = _reply().lower()
    print(f"        reply: {_reply()[:150]}")
    check("reply does not claim an update",
          "i've updated" not in reply and "i have updated" not in reply, reply)
    check("reply says the content was not found",
          "did not contain" in reply or "could not" in reply or "nothing" in reply, reply)
    check("panels left untouched",
          H._workspace_strategy_payload[ws]["strategicObjective"] == base["strategicObjective"])
    check("loaders still cleared", "strategy_objective" in _features(), str(sorted(_features())))


# -------------------------------------------------------- 7: rename regression

async def check_rename_regression():
    print("\n[6] Regression: the pre-existing rename path is unchanged")
    ws = "verify_rename"
    _seed(ws)
    H._workspace_strategy_payload[ws] = {
        "workspaceId": ws, "userId": UID, "role": "PETITIONER",
        "caseSummary": "Rambur filed the suit.",
        "reportCaseSummary": "Rambur filed.",
        "strategicObjective": "Advance Rambur's claim.",
        "possibleNextSteps": ["Serve notice on Rambur.", "File the reply for Rambur."],
        "suggestionChips": ["chip"] * 12,
    }
    instr = ("Replace every occurrence of 'Rambur' with 'Rambir' wherever it "
             "appears; change nothing else.")
    cls = {k: {"shouldEdit": True, "instruction": instr} for k in (
        "caseSummary", "reportCaseSummary", "strategicObjective",
        "possibleNextSteps", "analysisReport", "listOfDates")}
    cls.update({"isRename": True, "isRoleReassignment": False, "reassignRole": "",
                "reassignParty": "", "isFactCorrection": False,
                "factCorrections": [], "isQuestionLike": False})
    _stub(cls, {
        "edit the existing Strategic Objective":
            json.dumps({"strategicObjective": "Advance Rambir's claim."}),
        '"caseSummary"': json.dumps({"caseSummary": "Rambir filed the suit.",
                                     "reportCaseSummary": "Rambir filed."}),
        '"possibleNextSteps"': json.dumps({
            "possibleNextSteps": ["Serve notice on Rambir.", "File the reply for Rambir."],
            "suggestionChips": ["chip"] * 12}),
    })
    await _send(ws, "correct the name Rambur with Rambir")
    strat = H._workspace_strategy_payload[ws]
    feats = _features()
    check("rename applied to case summary", "Rambir" in strat["caseSummary"])
    check("rename applied to objective", "Rambir" in strat["strategicObjective"])
    check("rename applied to next steps", "Rambir" in strat["possibleNextSteps"][0])
    check("still one coalesced strategy emit",
          len(feats.get("strategy_objective", [])) == 1, str({k: len(v) for k, v in feats.items()}))
    check("no fact pinned for a rename", H.get_user_asserted_facts(ws) == [])


async def check_objective_declaration():
    print("\n[7] A drafted objective works in ANY chat, and is used verbatim")
    ws = "verify_declaration"
    _seed(ws)
    drafted = (
        "The strategic objective of S. E. Investments Ltd., the Petitioner in the "
        "ongoing enforcement proceedings, is to secure full recovery of the "
        "principal debt of Rs. 4,02,13,129/- with interest at 18% through "
        "enforcement of the arbitral award. The Petitioner should move for "
        "appointment of a Court Commissioner for B-214, Chittaranjan Park."
    )
    before = dict(H._workspace_strategy_payload[ws])
    cls = {k: {"shouldEdit": False, "instruction": ""} for k in (
        "caseSummary", "reportCaseSummary", "strategicObjective",
        "possibleNextSteps", "analysisReport", "listOfDates")}
    cls.update({"isRename": False, "isRoleReassignment": False, "reassignRole": "",
                "reassignParty": "", "isFactCorrection": False, "factCorrections": [],
                "isObjectiveDeclaration": True, "objectiveDeclarationText": drafted,
                "isQuestionLike": False})
    seen = {"verbatim_rule": False}

    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        if "legal-strategy workspace chat" in p:
            return json.dumps(cls)
        if "legal strategy analyst" in p.lower() and '"suggestionChips"' in p:
            seen["verbatim_rule"] = "VERBATIM RULE" in p
            return json.dumps({
                "strategicObjective": drafted,
                "possibleNextSteps": ["File the original award before the Joint Registrar.",
                                      "Move for sale of the attached property."],
                "suggestionChips": ["chip"] * 16})
        return "{}"
    _gc.gemini_client.generate = _gen

    # chat_type is ANALYSIS_REPORT — previously a dead end for a declaration.
    await _send(ws, "change the strategic objective to this - " + drafted,
                chat_type="ANALYSIS_REPORT")
    for _ in range(40):
        await asyncio.sleep(0.05)
        if "strategy_objective" in _features():
            break
    strat = H._workspace_strategy_payload[ws]
    check("declaration handled outside the Strategy & Objective chat",
          strat["strategicObjective"] != before["strategicObjective"],
          strat["strategicObjective"][:80])
    check("the lawyer's own drafting is reproduced verbatim",
          strat["strategicObjective"] == drafted, strat["strategicObjective"][:120])
    check("VERBATIM RULE reached the prompt", seen["verbatim_rule"])
    check("case summary still locked", strat["caseSummary"] == before["caseSummary"])
    check("next steps refreshed", strat["possibleNextSteps"] != before["possibleNextSteps"])


CHECKS = [
    ("editors", check_editors),
    ("ledger", check_ledger),
    ("gate", check_gate),
    ("end-to-end", check_end_to_end),
    ("honest-ack", check_honest_ack),
    ("rename", check_rename_regression),
    ("objective-declaration", check_objective_declaration),
]


async def main():
    for _, fn in CHECKS:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list the check groups and exit")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        sys.exit(0)
    sys.exit(asyncio.run(main()))
