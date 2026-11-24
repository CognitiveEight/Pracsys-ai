"""Picking an option after an incompatible-template flag must not restart the session.

The frontend re-sends the thread's templateId on every drafting message. Once
the lawyer chose (a) "draft from a fresh structure", the session's template_id
was cleared, so the next reply looked like a different template pick: the
kickoff ran again, re-flagged the same template and re-asked a/b/c on every
answer. Fake model and handlers; no billed calls. Fictional data only.
"""

from app.langgraph.drafting_agent import drafting_agent

TEMPLATE = "tmpl-fictional-0001"


def _incompatible_session():
    return {
        "source": "template",
        "template_id": TEMPLATE,
        "template_name": "Fictional Form",
        "draft_title": "Fictional Authority Letter",
        "suggestion_title": "Fictional Authority Letter",
        "incompatible": True,
        "draft_status": "analyzing",
        "actionable_id": "user_draft_1",
    }


async def test_option_a_remembers_the_set_aside_template(monkeypatch):
    async def _scope(**_kw):
        return {"template_id": None, "draft_status": "gathering_info", "open_questions": []}

    monkeypatch.setattr(drafting_agent, "_scope_blank_draft", _scope)
    state = await drafting_agent._handle_incompatible_reply(
        user_message="a",
        case_context={},
        session_state=_incompatible_session(),
        request_fields={"templateId": TEMPLATE},
        conversation_history=[],
    )
    assert state["template_id"] is None
    assert state["set_aside_template_id"] == TEMPLATE
    assert state["actionable_id"] == "user_draft_1"


async def _turn(monkeypatch, session, template_id):
    calls = []

    async def _load(**_kw):
        return session

    async def _kickoff(**_kw):
        calls.append("kickoff")
        return {"draft_status": "analyzing"}

    async def _followup(**kw):
        calls.append("followup")
        return kw["session_state"]

    monkeypatch.setattr(drafting_agent, "_load_session_state", _load)
    monkeypatch.setattr(drafting_agent, "_handle_kickoff", _kickoff)
    monkeypatch.setattr(drafting_agent, "_handle_followup", _followup)
    try:
        await drafting_agent._handle_turn(
            user_message="1. Advocate: Fictional Name  2. Forum: Fictional Court",
            case_context={},
            request_fields={"templateId": template_id, "actionableId": "user_draft_1"},
            conversation_history=[],
        )
    except Exception:
        # Response shaping after routing is out of scope here; the route taken is.
        pass
    return calls


def _gathering_after_option_a():
    return {
        "template_id": None,
        "set_aside_template_id": TEMPLATE,
        "draft_status": "gathering_info",
        "draft_title": "Fictional Authority Letter",
        "open_questions": [{"id": "forum", "label": "Forum: Which court?"}],
        "gathered_facts": {},
    }


async def test_resent_set_aside_template_answers_the_questions(monkeypatch):
    calls = await _turn(monkeypatch, _gathering_after_option_a(), TEMPLATE)
    assert calls[:1] == ["followup"]
    assert "kickoff" not in calls


async def test_a_different_template_still_starts_a_new_session(monkeypatch):
    calls = await _turn(monkeypatch, _gathering_after_option_a(), "tmpl-fictional-0002")
    assert calls[:1] == ["kickoff"]
