"""The drafting assistant must not re-ask the same questions forever.

A lawyer who answered every scoping question with "to be confirmed" got the
identical list back on every reply, and the draft was never produced: the
answer mapper dropped deferred answers, and only a bare "ok" / "proceed"
broke the loop. Fake model and renderer; no billed calls. Fictional data only.
"""

import json

from app.ai.gemini_client import gemini_client
from app.langgraph.drafting_agent import DraftingAgent, drafting_agent

QUESTIONS = [
    {"id": "amount", "label": "Outstanding Amount: What is the amount claimed?",
     "classification": "Critical"},
    {"id": "notice_date", "label": "Notice Date: When will the notice issue?",
     "classification": "Critical"},
    {"id": "address", "label": "Recipient Address: What is the registered address?",
     "classification": "Important"},
]


def _session():
    return {
        "draft_status": "gathering_info",
        "draft_title": "Fictional Demand Notice",
        "open_questions": [dict(q) for q in QUESTIONS],
        "gathered_facts": {},
    }


def _install(monkeypatch, mapping):
    """Fake the answer mapper's model call and record render calls."""
    rendered = []

    async def _model(prompt="", *_a, **_kw):
        return json.dumps(mapping)

    async def _render(**kw):
        rendered.append(dict(kw["session_state"]))
        kw["session_state"]["draft_status"] = "draft_ready"
        return kw["session_state"]

    monkeypatch.setattr(gemini_client, "generate", _model)
    monkeypatch.setattr(drafting_agent, "_render_and_attach", _render)
    return rendered


async def _reply(session, text):
    return await drafting_agent._handle_followup(
        user_message=text,
        case_context={},
        session_state=session,
        request_fields={},
        conversation_history=[],
    )


async def test_deferred_answers_render_with_placeholders(monkeypatch):
    rendered = _install(monkeypatch, {
        "amount": DraftingAgent.DEFERRED_ANSWER,
        "notice_date": "1 January 2030",
        "address": DraftingAgent.DEFERRED_ANSWER,
    })
    await _reply(_session(), "1. pending  2. 1 January 2030  3. to be verified")
    assert len(rendered) == 1
    facts = rendered[0]["gathered_facts"]
    assert facts["amount"] == "<Outstanding Amount>"
    assert facts["address"] == "<Recipient Address>"
    assert facts["notice_date"] == "1 January 2030"


async def test_unmapped_deferral_reply_renders(monkeypatch):
    rendered = _install(monkeypatch, {})
    await _reply(_session(), "All of these are to be confirmed by the client.")
    assert len(rendered) == 1


async def test_second_unproductive_reply_renders_instead_of_repeating(monkeypatch):
    rendered = _install(monkeypatch, {})
    session = await _reply(_session(), "Let me check with the accounts team.")
    assert not rendered
    assert session["draft_status"] == "gathering_info"
    assert "proceed" in session["last_assistant_message"]
    await _reply(session, "Still checking with them.")
    assert len(rendered) == 1


async def test_clarifying_question_is_never_treated_as_proceed(monkeypatch):
    rendered = _install(monkeypatch, {})
    session = await _reply(_session(), "Let me check with the accounts team.")
    session = await _reply(session, "What do you mean by registered address?")
    assert not rendered
    assert session["draft_status"] == "gathering_info"


async def test_partial_real_answers_still_ask_the_rest(monkeypatch):
    rendered = _install(monkeypatch, {"notice_date": "1 January 2030"})
    session = await _reply(_session(), "Notice date is 1 January 2030.")
    assert not rendered
    assert [q["id"] for q in session["open_questions"]] == ["amount", "address"]
    assert session["unproductive_question_turns"] == 0


async def test_model_error_is_not_read_as_answers(monkeypatch):
    rendered = _install(monkeypatch, {})

    async def _error(*_a, **_kw):
        return "Error: 429 quota exceeded"

    monkeypatch.setattr(gemini_client, "generate", _error)
    monkeypatch.setattr("app.llm.errors.is_generation_error", lambda raw: True)
    session = await _reply(_session(), "Let me check with the accounts team.")
    assert not rendered
    assert session["gathered_facts"] == {}


def test_deferral_wording():
    yes = [
        "[AMOUNT TO BE CONFIRMED]", "Pending confirmation from statements",
        "TBC", "will share the address later", "we don't have these yet",
        "leave them blank", "use placeholders",
    ]
    no = ["Notice date is 1 January 2030", "The amount is 5,00,000"]
    assert all(DraftingAgent._is_deferral_reply(t) for t in yes)
    assert not any(DraftingAgent._is_deferral_reply(t) for t in no)
