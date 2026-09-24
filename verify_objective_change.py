#!/usr/bin/env python3
"""
verify_objective_change.py — Offline checks for what must be updated when the
strategic objective changes.

Two things now move with the objective that did not before: the **case
summary** (re-framed, groups 1-10) and the **quality score explanation**
(group 11).

When the lawyer declares a NEW strategic objective, the case summary is
re-framed to lead with the facts that objective makes decisive — it is no
longer locked. The rule under test is the boundary of that licence:

    re-framing is EMPHASIS and ORDERING, never FACTS.

The case summary is the fact-of-record narrative the analysis report, list of
dates and applicable laws are all derived from, so a re-frame that loses a
figure, invents an entity, or drops the mandated opening is discarded and the
previous text stands — while the new objective still lands, which is the part
the lawyer actually asked for.

Covers:
  1. figures      — the dropped-figure detector itself
  2. accepted     — a clean re-frame updates both summaries and the objective
  3. no-summary   — a response without summaries keeps the previous text
  4. dropped fact — a re-frame that loses a figure is vetoed
  5. invented     — a re-frame that introduces a new entity is vetoed
  6. opening      — a re-frame that drops "This case is about..." is vetoed
  7. half         — only one summary surviving keeps BOTH previous texts
  8. reassignment — the role-reassignment caller still locks the summaries
  9. surgical     — a one-line objective edit still preserves the summary
 10. ack          — the chat reply names the case summary only if it moved
 11. score expl   — the quality score explanation is objective-aware and is
                    always refreshed by an objective change
 12. interruption — a change interrupted by a disconnect is re-delivered or
                    re-run, and never leaves a panel loader spinning

USAGE
    python verify_objective_change.py            # run everything
    python verify_objective_change.py --list     # show the check names

REQUIREMENTS
    Run inside the project venv so `app` is importable. Nothing else.
"""

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.prompts.strategy_prompts import \
    regenerate_strategy_for_new_objective_prompt  # noqa: E402
from app.prompts.analysis_report_prompts import \
    ensure_quality_score_explanation_prompt  # noqa: E402
from app.socket.event_utils import find_dropped_case_figures  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402

_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []
PROMPTS_SEEN = []

UID, CHAT = "verify_user", "verify_chat"

#: Run-unique workspace ids. The service persists per-workspace state (strategy
#: payload, workspace meta, role cache) to Qdrant, so a fixed id would let one
#: run's writes become the next run's starting state whenever a real Qdrant is
#: reachable. `_cleanup_workspaces()` deletes them again at the end.
RUN_ID = uuid.uuid4().hex[:10]
TOUCHED_WORKSPACES: set = set()


def _ws(name: str) -> str:
    ws = f"{name}_{RUN_ID}"
    TOUCHED_WORKSPACES.add(ws)
    return ws


async def _cleanup_workspaces() -> None:
    try:
        from app.vectorstore.qdrant import vector_store
    except Exception:
        return
    for ws in sorted(TOUCHED_WORKSPACES):
        try:
            await vector_store.delete_workspace(ws)
        except Exception:
            pass

CASE_SUMMARY = (
    "This case is about Ramesh Sharma, the vendee, and Sunil Verma, the vendor, "
    "in a dispute over Arazi No. 262 measuring 0.263 Hectares at Mauza Bhagwanpur. "
    "A sale consideration of Rs. 11,64,000 was agreed on 12/03/2019 and Rs. 10,00,000 "
    "was paid. The suit is registered as O.S. No. 827/2019."
)
REPORT_CASE_SUMMARY = (
    "This matter is about a land transaction between Ramesh Sharma and Sunil Verma "
    "concerning Arazi No. 262 measuring 0.263 Hectares at Mauza Bhagwanpur. "
    "A sale consideration of Rs. 11,64,000 was agreed on 12/03/2019 and Rs. 10,00,000 "
    "was paid. The proceeding is registered as O.S. No. 827/2019."
)
# A legitimate re-frame: limitation-first ordering, every figure intact.
REFRAMED_CASE_SUMMARY = (
    "This case is about the limitation position in O.S. No. 827/2019, filed over "
    "Arazi No. 262 measuring 0.263 Hectares at Mauza Bhagwanpur. The agreement of "
    "12/03/2019 fixed a sale consideration of Rs. 11,64,000, of which Rs. 10,00,000 "
    "was paid, and it is from that date that time runs between Ramesh Sharma, the "
    "vendee, and Sunil Verma, the vendor."
)
REFRAMED_REPORT_SUMMARY = (
    "This matter is about the limitation position in O.S. No. 827/2019, concerning "
    "Arazi No. 262 measuring 0.263 Hectares at Mauza Bhagwanpur. The agreement of "
    "12/03/2019 fixed a sale consideration of Rs. 11,64,000, of which Rs. 10,00,000 "
    "was paid, and time runs from that date between Ramesh Sharma and Sunil Verma."
)
NEW_OBJECTIVE = (
    "Establish that the suit is within limitation and secure specific performance "
    "of the agreement dated 12/03/2019."
)
INSTRUCTION = "focus the case on the limitation point"


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


class _DownSocket:
    """A disconnected socket: SocketClient.emit returns False without raising
    when the connection is gone, so the payload is silently dropped."""

    async def emit(self, event, payload):
        return False

    async def emit_ai_response(self, payload):
        return False


H._socket_client = _FakeSocket()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _seed(ws):
    H._workspace_strategy_payload[ws] = {
        "workspaceId": ws, "userId": UID, "role": "PETITIONER",
        "caseSummary": CASE_SUMMARY,
        "reportCaseSummary": REPORT_CASE_SUMMARY,
        "strategicObjective": "Secure refund of the paid consideration.",
        "possibleNextSteps": ["Issue a demand notice."],
        "suggestionChips": ["chip"] * 12,
    }
    return dict(H._workspace_strategy_payload[ws])


def _stub(summaries):
    """Gemini stub for the objective regeneration. `summaries` is merged into
    the returned JSON, so a test can omit or corrupt the summary fields."""
    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        PROMPTS_SEEN.append(p)
        if "legal strategy analyst" in p.lower() and '"suggestionChips"' in p:
            return json.dumps({
                "strategicObjective": NEW_OBJECTIVE,
                "possibleNextSteps": ["Plead the limitation position.",
                                      "File the agreement as Annexure 1."],
                "suggestionChips": ["chip"] * 16,
                **summaries,
            })
        return "{}"
    _gc.gemini_client.generate = _gen


async def _regen(ws, summaries, **kwargs):
    EMITS.clear()
    PROMPTS_SEEN.clear()
    _stub(summaries)
    ok = await H._regenerate_strategy_for_new_objective(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_objective_instruction=INSTRUCTION,
        current_strategy=dict(H._workspace_strategy_payload[ws]),
        **kwargs,
    )
    return ok, H._workspace_strategy_payload[ws]


BOTH = {"caseSummary": REFRAMED_CASE_SUMMARY,
        "reportCaseSummary": REFRAMED_REPORT_SUMMARY}


# ------------------------------------------------------------------ 1: figures

def check_figures():
    print("\n[1] The dropped-figure detector")
    f = find_dropped_case_figures
    check("identical text drops nothing", f(CASE_SUMMARY, CASE_SUMMARY) == [])
    check("a genuine re-frame drops nothing",
          f(CASE_SUMMARY, REFRAMED_CASE_SUMMARY) == [],
          str(f(CASE_SUMMARY, REFRAMED_CASE_SUMMARY)))
    check("re-notation is not a loss (Rs. 11,64,000 -> 1164000)",
          f("Rs. 11,64,000 paid", "1164000 paid") == [])
    check("a lost amount is caught",
          f(CASE_SUMMARY, CASE_SUMMARY.replace("Rs. 11,64,000", "the price")) ==
          ["11,64,000"])
    check("a lost suit number is caught",
          "827" in "".join(f(CASE_SUMMARY, CASE_SUMMARY.replace("827/2019", "the suit"))))
    check("a lost magnitude figure is caught (6 lakh)",
          f("withheld 6 lakh USD", "withheld the sum") == ["6"])
    check("date re-wording is not a loss (03/04/2019 -> 3 April 2019)",
          f("paid on 03/04/2019", "paid on 3 April 2019") == [])
    check("small counts are ignored",
          f("there are 3 defendants", "there are three defendants") == [])
    check("empty previous text drops nothing", f("", "anything") == [])


# ----------------------------------------------------------------- 2: accepted

async def check_accepted():
    print("\n[2] A clean re-frame updates both summaries and the objective")
    ws = _ws("verify_ocs_ok")
    before = _seed(ws)
    ok, strat = await _regen(ws, BOTH)

    check("regeneration reported success", ok is True, f"got {ok}")
    check("case summary re-framed", strat["caseSummary"] == REFRAMED_CASE_SUMMARY,
          strat["caseSummary"][:90])
    check("report case summary re-framed",
          strat["reportCaseSummary"] == REFRAMED_REPORT_SUMMARY)
    check("objective updated too",
          strat["strategicObjective"] != before["strategicObjective"])
    check("next steps refreshed",
          strat["possibleNextSteps"] != before["possibleNextSteps"])
    check("every figure survived the re-frame",
          find_dropped_case_figures(before["caseSummary"], strat["caseSummary"]) == [])
    check("strategy_objective emitted once",
          [e for e, _ in EMITS].count("document_feature_response") == 1,
          str([e for e, _ in EMITS]))
    emitted = next(p for e, p in EMITS if e == "document_feature_response")
    check("the emitted payload carries the new summary",
          emitted["payload"]["caseSummary"] == REFRAMED_CASE_SUMMARY)
    check("prompt asked for a re-frame, not a rewrite",
          any("RE-FRAME RULES" in p for p in PROMPTS_SEEN))
    check("prompt did not lock the summary",
          not any("LOCKED caseSummary" in p for p in PROMPTS_SEEN))


# --------------------------------------------------------------- 3: no summary

async def check_no_summary_returned():
    print("\n[3] A response without summaries keeps the previous text")
    ws = _ws("verify_ocs_none")
    before = _seed(ws)
    ok, strat = await _regen(ws, {})

    check("regeneration still succeeds", ok is True, f"got {ok}")
    check("case summary preserved", strat["caseSummary"] == before["caseSummary"])
    check("report case summary preserved",
          strat["reportCaseSummary"] == before["reportCaseSummary"])
    check("objective still updated",
          strat["strategicObjective"] != before["strategicObjective"])


# ------------------------------------------------------------- 4: dropped fact

async def check_dropped_fact_vetoed():
    print("\n[4] A re-frame that loses a figure is discarded")
    ws = _ws("verify_ocs_drop")
    before = _seed(ws)
    lossy = REFRAMED_CASE_SUMMARY.replace("Rs. 11,64,000", "the agreed price")
    ok, strat = await _regen(ws, {
        "caseSummary": lossy,
        "reportCaseSummary": REFRAMED_REPORT_SUMMARY,
    })

    check("regeneration still succeeds", ok is True, f"got {ok}")
    check("lossy case summary rejected", strat["caseSummary"] == before["caseSummary"],
          strat["caseSummary"][:90])
    check("its counterpart is rejected with it (summaries stay consistent)",
          strat["reportCaseSummary"] == before["reportCaseSummary"])
    check("the objective still landed",
          strat["strategicObjective"] != before["strategicObjective"])


# ----------------------------------------------------------------- 5: invented

async def check_invented_entity_vetoed():
    print("\n[5] A re-frame that invents an entity is discarded")
    ws = _ws("verify_ocs_invent")
    before = _seed(ws)
    invented = REFRAMED_CASE_SUMMARY + (
        " The matter was referred to the Bihar Cricket Association for conciliation."
    )
    ok, strat = await _regen(ws, {
        "caseSummary": invented,
        "reportCaseSummary": REFRAMED_REPORT_SUMMARY,
    })

    check("regeneration still succeeds", ok is True, f"got {ok}")
    check("fabricated case summary rejected",
          strat["caseSummary"] == before["caseSummary"], strat["caseSummary"][:90])
    check("the objective still landed",
          strat["strategicObjective"] != before["strategicObjective"])


# ------------------------------------------------------------------ 6: opening

async def check_opening_enforced():
    print("\n[6] A re-frame that drops the mandated opening is discarded")
    ws = _ws("verify_ocs_open")
    before = _seed(ws)
    ok, strat = await _regen(ws, {
        "caseSummary": REFRAMED_CASE_SUMMARY.replace(
            "This case is about", "The dispute concerns"),
        "reportCaseSummary": REFRAMED_REPORT_SUMMARY,
    })
    check("case summary without its opening rejected",
          strat["caseSummary"] == before["caseSummary"], strat["caseSummary"][:90])

    _seed(ws)
    ok, strat = await _regen(ws, {
        "caseSummary": REFRAMED_CASE_SUMMARY,
        "reportCaseSummary": REFRAMED_REPORT_SUMMARY.replace(
            "This matter is about", "The proceeding concerns"),
    })
    check("report summary without its opening rejected",
          strat["reportCaseSummary"] == REPORT_CASE_SUMMARY)
    check("and the case summary is held back with it",
          strat["caseSummary"] == CASE_SUMMARY)


# --------------------------------------------------------------------- 7: half

async def check_half_reframe():
    print("\n[7] Only one summary surviving keeps BOTH previous texts")
    ws = _ws("verify_ocs_half")
    before = _seed(ws)
    ok, strat = await _regen(ws, {"caseSummary": REFRAMED_CASE_SUMMARY})

    check("a lone caseSummary is not applied",
          strat["caseSummary"] == before["caseSummary"], strat["caseSummary"][:90])
    check("report case summary untouched",
          strat["reportCaseSummary"] == before["reportCaseSummary"])
    check("the objective still landed",
          strat["strategicObjective"] != before["strategicObjective"])


# -------------------------------------------------------------- 8: reassignment

async def check_role_reassignment_locks():
    print("\n[8] The role-reassignment caller still locks the summaries")
    ws = _ws("verify_ocs_lock")
    before = _seed(ws)
    ok, strat = await _regen(ws, BOTH, refresh_case_summary=False)

    check("regeneration succeeds", ok is True, f"got {ok}")
    check("case summary locked", strat["caseSummary"] == before["caseSummary"])
    check("report case summary locked",
          strat["reportCaseSummary"] == before["reportCaseSummary"])
    check("objective still regenerated",
          strat["strategicObjective"] != before["strategicObjective"])
    check("prompt locked the summary",
          any("LOCKED caseSummary" in p for p in PROMPTS_SEEN))
    check("prompt carried no re-frame licence",
          not any("RE-FRAME RULES" in p for p in PROMPTS_SEEN))

    # The prompt builder itself must be byte-stable in locked mode.
    kw = dict(SPEAKER_LABEL_GUARDRAIL="SPK", case_summary="CS",
              combined_document_content="DOCS",
              current_strategy={"strategicObjective": "o", "possibleNextSteps": ["a"]},
              report_case_summary="RCS", role="PETITIONER",
              user_objective_instruction="x")
    locked = regenerate_strategy_for_new_objective_prompt(
        **kw, refresh_case_summary=False)
    reframe = regenerate_strategy_for_new_objective_prompt(**kw)
    check("locked prompt forbids restating the summaries",
          "Do not restate or replace caseSummary" in locked)
    check("re-frame prompt asks for both summary keys",
          '"caseSummary"' in reframe and '"reportCaseSummary"' in reframe)
    check("locked prompt asks for neither", '"caseSummary"' not in locked)


# ----------------------------------------------------------------- 9: surgical

async def check_surgical_edit_unchanged():
    print("\n[9] Regression: a surgical objective edit still preserves the summary")
    ws = _ws("verify_ocs_surgical")
    before = _seed(ws)

    async def _gen(*a, prompt="", **k):
        return json.dumps({"strategicObjective": "Secure specific performance only."})
    _gc.gemini_client.generate = _gen
    EMITS.clear()
    ok = await H._regenerate_strategic_objective_with_feedback(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_feedback="rephrase the last line", current_strategy=dict(before))
    strat = H._workspace_strategy_payload[ws]

    check("surgical edit applied", ok is True, f"got {ok}")
    check("case summary untouched", strat["caseSummary"] == before["caseSummary"])
    check("report case summary untouched",
          strat["reportCaseSummary"] == before["reportCaseSummary"])


# --------------------------------------------------------------------- 10: ack

#: Panel-edit classifier verdict: not a surgical edit, a DECLARED objective.
#: This is the route an objective change takes through the chat handler.
_DECLARATION_CLS = {k: {"shouldEdit": False, "instruction": ""} for k in (
    "caseSummary", "reportCaseSummary", "strategicObjective",
    "possibleNextSteps", "analysisReport", "listOfDates")}
_DECLARATION_CLS.update({
    "isRename": False, "isRoleReassignment": False, "reassignRole": "",
    "reassignParty": "", "isFactCorrection": False, "factCorrections": [],
    "isObjectiveDeclaration": True, "objectiveDeclarationText": NEW_OBJECTIVE,
    "isQuestionLike": False,
})


def _stub_chat(summaries):
    """Like `_stub`, but also answers the chat handler's intent classifier so
    the message actually reaches the objective-change path."""
    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        PROMPTS_SEEN.append(p)
        if "legal-strategy workspace chat" in p:
            return json.dumps(_DECLARATION_CLS)
        if "legal strategy analyst" in p.lower() and '"suggestionChips"' in p:
            return json.dumps({
                "strategicObjective": NEW_OBJECTIVE,
                "possibleNextSteps": ["Plead the limitation position.",
                                      "File the agreement as Annexure 1."],
                "suggestionChips": ["chip"] * 16,
                **summaries,
            })
        return "{}"
    _gc.gemini_client.generate = _gen


async def check_ack_is_honest():
    print("\n[10] The chat reply names the case summary only when it moved")
    ws = _ws("verify_ocs_ack")

    async def _send(ws_id):
        EMITS.clear()
        await H.on_user_message_request({
            "content": "change the strategic objective to this - " + NEW_OBJECTIVE,
            "chat_type": "ANALYSIS_REPORT",
            "conversation_history": [],
            "workspaceId": ws_id, "userId": UID, "chatId": CHAT,
            "timestamp": "2026-01-01T00:00:00Z",
        })
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(e == "ai_response" for e, _ in EMITS):
                break
        return next((p.get("content", "") for e, p in EMITS if e == "ai_response"), "")

    _seed(ws)
    _stub_chat(BOTH)
    reply = await _send(ws)
    check("case summary actually re-framed",
          H._workspace_strategy_payload[ws]["caseSummary"] == REFRAMED_CASE_SUMMARY,
          H._workspace_strategy_payload[ws]["caseSummary"][:90])
    check("reply says the case summary changed", "case summary" in reply.lower(), reply)

    ws2 = _ws("verify_ocs_ack2")
    _seed(ws2)
    _stub_chat({})      # model returns no summaries -> previous text kept
    reply2 = await _send(ws2)
    check("case summary unchanged",
          H._workspace_strategy_payload[ws2]["caseSummary"] == CASE_SUMMARY)
    check("reply does NOT claim the case summary changed",
          "case summary" not in reply2.lower(), reply2)
    check("reply still reports the objective change",
          "strategic objective" in reply2.lower(), reply2)


# --------------------------------------------------- 11: quality score explanation

def check_score_explanation_units():
    print("\n[11a] The quality score explanation is objective-aware")
    # Pure pieces first: the clause, the deterministic text, the prompt.
    clause = H._objective_clause_for_explanation(NEW_OBJECTIVE)
    check("objective clause built", "readiness for the current objective" in clause.lower(),
          clause)
    two_sentences = "Establish limitation. Then seek specific performance."
    check("clause quotes only the first sentence",
          "specific performance" not in
          H._objective_clause_for_explanation(two_sentences).lower(),
          H._objective_clause_for_explanation(two_sentences))
    check("clause is capped", len(clause) <= 230, str(len(clause)))
    check("no objective yields no clause",
          H._objective_clause_for_explanation("") == "")
    check("long objective is truncated",
          H._objective_clause_for_explanation("x" * 400).endswith("..."))

    args = dict(case_resolution_score=55, missing_info=["a gap"], weaknesses=[],
                risks=[], strengths=[])
    plain = H._derive_quality_score_explanation(**args)
    with_obj = H._derive_quality_score_explanation(**args,
                                                   strategic_objective=NEW_OBJECTIVE)
    check("deterministic text changes with the objective", plain != with_obj)
    check("deterministic text keeps its score lead",
          with_obj.startswith("The current case resolution score is 55%."), with_obj[:60])
    check("deterministic text names the objective",
          "limitation" in with_obj.lower(), with_obj)

    clean = H._derive_quality_score_explanation(
        case_resolution_score=100, missing_info=[], weaknesses=[], risks=[],
        strengths=[{"point": "p"}], strategic_objective=NEW_OBJECTIVE)
    check("100%% text also carries the objective", "limitation" in clean.lower(), clean)

    p_no = ensure_quality_score_explanation_prompt(case_resolution_score=55, summary="{}")
    p_yes = ensure_quality_score_explanation_prompt(
        case_resolution_score=55, summary="{}", strategic_objective=NEW_OBJECTIVE)
    check("prompt unchanged without an objective", "OBJECTIVE RULE" not in p_no)
    check("prompt carries the objective rule", "OBJECTIVE RULE" in p_yes)
    check("prompt carries the objective text", "limitation" in p_yes.lower())


async def check_score_explanation_helper():
    print("\n[11b] The explanation helper passes the objective to the model")
    seen = {"prompt": ""}

    async def _gen(*a, prompt="", **k):
        seen["prompt"] = prompt or (a[0] if a else "")
        return "The current score is 55% because the limitation point is now decisive."
    _gc.gemini_client.generate = _gen

    out = await H._ensure_quality_score_explanation(
        workspace_role="PETITIONER", case_resolution_score=55,
        missing_info=["a gap"], weaknesses=[], risks=[], strengths=[],
        existing="", strategic_objective=NEW_OBJECTIVE)
    check("explanation produced", out.startswith("The current score is 55%"), out)
    check("objective reached the model", "limitation" in seen["prompt"].lower())

    seen["prompt"] = ""
    kept = await H._ensure_quality_score_explanation(
        workspace_role="PETITIONER", case_resolution_score=55,
        missing_info=[], weaknesses=[], risks=[], strengths=[],
        existing="Model already wrote this.", strategic_objective=NEW_OBJECTIVE)
    check("a model-supplied explanation is still respected",
          kept == "Model already wrote this.", kept)
    check("and costs no extra call", seen["prompt"] == "")


async def check_score_explanation_refreshed_on_objective_change():
    print("\n[11c] An objective change always refreshes the explanation")
    ws = _ws("verify_ocs_score")
    _seed(ws)
    stale = "The current score is 40% because OLD framing."
    H._workspace_analysis_payload[ws] = {
        "missingInfo": ["an old gap"], "strengths": [], "weaknesses": [], "risks": [],
        "relevantJurisdiction": [], "caseResolutionScore": 40,
        "qualityScoreExplanation": stale,
        "reportQualityScoreExplanation": stale,
        "pendingActions": [], "nextSteps": [], "suggestedDrafts": [],
    }
    H._workspace_strategy_payload[ws]["strategicObjective"] = NEW_OBJECTIVE

    # The cascade's later steps reach the network (Kanoon / Vertex); this check
    # is about step 1, so stub them out.
    async def _noop(*a, **k):
        return None
    stubbed = (
        "_generate_and_emit_applicable_laws",
        "_reeval_list_of_dates_relevance",
        "_emit_relevant_parties",
        "_generate_and_emit_case_laws",
        "_reemit_case_laws_unchanged",
        "_generate_and_emit_drafting_suggestions",
        "_reemit_drafting_unchanged",
    )
    originals = {name: getattr(H.__class__, name) for name in stubbed}
    for name in stubbed:
        setattr(H.__class__, name, _noop)
    orig_gen = H.__class__._generate_analysis_report_from_documents

    # The regenerated report repeats the STALE explanation — the case the
    # forced refresh exists for (truncated JSON refilled objective-blind).
    async def _fake_report(self, **k):
        return {
            "missingInfo": ["a new objective-driven gap"], "strengths": [],
            "weaknesses": [], "risks": [], "relevantJurisdiction": [],
            "caseResolutionScore": 55, "qualityScoreExplanation": stale,
            "reportQualityScoreExplanation": stale, "pendingActions": [],
            "nextSteps": [], "suggestedDrafts": [],
        }
    H.__class__._generate_analysis_report_from_documents = _fake_report

    saw = {"objective_in_prompt": False}

    async def _gen(*a, prompt="", **k):
        p = prompt or (a[0] if a else "")
        if "OBJECTIVE RULE" in p:
            saw["objective_in_prompt"] = True
            return ("The current score is 55% because the agreement of 12/03/2019 "
                    "fixes when time began to run, which is decisive for the "
                    "limitation objective.")
        return "{}"
    _gc.gemini_client.generate = _gen

    EMITS.clear()
    try:
        await H._run_objective_change_incremental_refresh(
            workspace_id=ws, user_id=UID, workspace_role="PETITIONER")
    finally:
        for name, fn in originals.items():
            setattr(H.__class__, name, fn)
        H.__class__._generate_analysis_report_from_documents = orig_gen

    report = H._workspace_analysis_payload[ws]
    check("stale explanation replaced",
          report.get("qualityScoreExplanation") != stale,
          str(report.get("qualityScoreExplanation"))[:100])
    check("regenerated with the objective in hand", saw["objective_in_prompt"])
    check("new explanation reflects the new objective",
          "limitation" in (report.get("qualityScoreExplanation") or "").lower(),
          str(report.get("qualityScoreExplanation"))[:100])
    check("score itself updated", report.get("caseResolutionScore") == 55,
          str(report.get("caseResolutionScore")))
    check("report-facing copy refreshed too",
          report.get("reportQualityScoreExplanation") not in (stale, "", None),
          str(report.get("reportQualityScoreExplanation"))[:100])
    check("report-facing copy stays word-safe",
          "legal" not in (report.get("reportQualityScoreExplanation") or "").lower()
          and "respondent" not in
          (report.get("reportQualityScoreExplanation") or "").lower())
    check("analysis_report emitted",
          any(e == "document_feature_response"
              and p.get("featureType") == "analysis_report" for e, p in EMITS))


# --------------------------------------------- 12: interruption never strands a loader

async def check_interruption_recovery():
    """The 2026-09-17 failure: an objective change armed six panel loaders,
    the socket dropped 25s into the ~2-minute regeneration, and the work died
    with no log line and no emit — so the panels span for the rest of the
    session. A loader is cleared ONLY by a document_feature_response for the
    same featureType, so every way this path can stop must either deliver the
    real payload later or emit a terminal state now."""
    print("\n[12] An interrupted objective change never strands a loader")
    from app.socket.event_utils import OBJECTIVE_CHANGE_REFRESH_FEATURES

    # --- 12a: a dropped emit is buffered, not lost, and reported as failure
    ws = _ws("verify_ocs_drop")
    _seed(ws)
    H._pending_feature_deliveries.pop(ws, None)
    _stub(BOTH)
    EMITS.clear()
    H._socket_client = _DownSocket()
    try:
        ok = await H._regenerate_strategy_for_new_objective(
            workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
            user_objective_instruction=INSTRUCTION,
            current_strategy=dict(H._workspace_strategy_payload[ws]),
        )
    finally:
        H._socket_client = _FakeSocket()
    check("a dropped emit is NOT reported as success", ok is False)
    buffered = H._pending_feature_deliveries.get(ws) or {}
    check("the generated payload is buffered for re-delivery",
          "strategy_objective" in buffered, str(list(buffered)))
    check("the buffered payload carries the NEW objective",
          (buffered.get("strategy_objective", {})
           .get("payload", {}).get("strategicObjective")) == NEW_OBJECTIVE)

    # --- 12b: reconnect re-delivers the real payload (not a cached snapshot)
    EMITS.clear()
    delivered = await H._flush_pending_feature_deliveries(ws)
    check("reconnect re-delivers it",
          (delivered or {}).get(ws) == ["strategy_objective"], str(delivered))
    check("and it is the real generated objective, not the old one",
          any(e == "document_feature_response"
              and p.get("featureType") == "strategy_objective"
              and p.get("payload", {}).get("strategicObjective") == NEW_OBJECTIVE
              for e, p in EMITS))
    check("the buffer is emptied once delivered",
          not (H._pending_feature_deliveries.get(ws) or {}))

    # --- 12c: a cancelled regeneration is logged, marked, and re-raises
    ws = _ws("verify_ocs_cancel")
    _seed(ws)
    H._pending_objective_changes.pop(ws, None)

    async def _cancelled_gen(*a, **k):
        raise asyncio.CancelledError()

    _gc.gemini_client.generate = _cancelled_gen
    cancelled = False
    try:
        await H._regenerate_strategy_for_new_objective(
            workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
            user_objective_instruction=INSTRUCTION,
            current_strategy=dict(H._workspace_strategy_payload[ws]),
        )
    except asyncio.CancelledError:
        cancelled = True
    check("cancellation propagates (not swallowed as success)", cancelled)
    marker = H._pending_objective_changes.get(ws) or {}
    check("the interrupted instruction is marked for resume",
          marker.get("instruction") == INSTRUCTION, str(marker)[:120])
    check("the marker records the loaders that were armed",
          set(marker.get("featureTypes") or [])
          == set(OBJECTIVE_CHANGE_REFRESH_FEATURES))

    # --- 12d: the resume completes the work the cancellation lost
    _stub(BOTH)
    EMITS.clear()
    resumed = await H._resume_pending_objective_changes(only_workspace_id=ws)
    check("the interrupted change is re-run on reconnect", resumed == 1,
          str(resumed))
    check("and the new objective finally lands",
          H._workspace_strategy_payload[ws].get("strategicObjective")
          == NEW_OBJECTIVE)
    check("the resume marker is cleared once it lands",
          not (H._pending_objective_changes.get(ws) or {}).get("instruction"))

    # --- 12e: a resume is attempted once, then the loaders are released
    ws = _ws("verify_ocs_giveup")
    _seed(ws)
    H._pending_objective_changes[ws] = {
        "userId": UID, "workspaceRole": "PETITIONER",
        "instruction": INSTRUCTION,
        "featureTypes": list(OBJECTIVE_CHANGE_REFRESH_FEATURES),
        "attempts": 1, "startedAt": time.time(),
    }
    EMITS.clear()
    resumed = await H._resume_pending_objective_changes(only_workspace_id=ws)
    check("a second cancellation is not retried forever", resumed == 0)
    released = {p.get("featureType") for e, p in EMITS
                if e == "document_feature_response"}
    check("every armed loader is released when the work is abandoned",
          set(OBJECTIVE_CHANGE_REFRESH_FEATURES) <= released,
          str(sorted(released)))

    # --- 12f: an LLM failure is named, retried once, and reported
    ws = _ws("verify_ocs_llmerr")
    _seed(ws)
    calls = {"n": 0}

    async def _err_gen(*a, **k):
        calls["n"] += 1
        return "Error generating response: 429 Too Many Requests"

    _gc.gemini_client.generate = _err_gen
    EMITS.clear()
    ok = await H._regenerate_strategy_for_new_objective(
        workspace_id=ws, user_id=UID, workspace_role="PETITIONER",
        user_objective_instruction=INSTRUCTION,
        current_strategy=dict(H._workspace_strategy_payload[ws]),
    )
    check("an LLM error sentinel is not mistaken for content", ok is False)
    check("and it is retried once before giving up", calls["n"] == 2,
          str(calls["n"]))
    check("nothing was emitted for a failed regeneration",
          not any(e == "document_feature_response" for e, p in EMITS))

    # --- 12g: the release helper clears exactly the loaders still owed
    EMITS.clear()
    released = await H._release_refresh_loaders(
        ws, UID, list(OBJECTIVE_CHANGE_REFRESH_FEATURES),
        skip={"strategy_objective"}, reason="verify",
        workspace_role="PETITIONER",
    )
    check("a feature that already emitted is not re-emitted",
          "strategy_objective" not in released, str(released))
    check("every other armed loader gets a terminal state",
          set(released) == set(OBJECTIVE_CHANGE_REFRESH_FEATURES)
          - {"strategy_objective"}, str(sorted(released)))

    # --- 12h: a vague "revise as per the documents" is not used AS the objective
    grounded = H._document_grounded_objective_instruction(
        "just revise the strategic objective as per proper documents")
    check("a revision request is reframed as a document re-derivation",
          "Re-derive the strategic objective strictly from the uploaded documents"
          in grounded)
    check("and it is explicitly NOT presented as the new objective text",
          "NOT the new objective text" in grounded)


CHECKS = [
    ("figures", check_figures),
    ("accepted", check_accepted),
    ("no-summary", check_no_summary_returned),
    ("dropped", check_dropped_fact_vetoed),
    ("invented", check_invented_entity_vetoed),
    ("opening", check_opening_enforced),
    ("half", check_half_reframe),
    ("reassignment", check_role_reassignment_locks),
    ("surgical", check_surgical_edit_unchanged),
    ("ack", check_ack_is_honest),
    ("score-expl-units", check_score_explanation_units),
    ("score-expl-helper", check_score_explanation_helper),
    ("score-expl-refresh", check_score_explanation_refreshed_on_objective_change),
    ("interruption", check_interruption_recovery),
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
