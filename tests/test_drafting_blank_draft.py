"""SIM-191: a new blank draft (no template, no saved session) must not crash.

``_handle_turn`` read ``session_state.get(...)`` while pinning the template
version, and ``_load_session_state`` returns None for a draft never saved, so
the first message of every blank draft — and every retry — failed with
"'NoneType' object has no attribute 'get'". Fake session store and model; no
billed calls. Fictional data only.
"""

import json

import app.langgraph.drafting_agent as drafting_module
from app.ai.gemini_client import gemini_client
from app.langgraph.drafting_agent import drafting_agent

ERROR_REPLY = "I ran into an issue while preparing the draft."

BLANK_DRAFT_REQUEST = {
    "actionableId": "user_draft_1",
    "workspaceId": "ws_fictional",
    "userId": "user_fictional",
    "templateId": None,
    "suggestedDraftId": None,
    "source": None,
}


def _install_fakes(monkeypatch, session):
    saved = []
    pins = []

    async def _load(**_kw):
        return session

    async def _save(*_a, **_kw):
        saved.append(True)

    async def _model(prompt="", *_a, **_kw):
        return json.dumps({"reply": "A fictional reply."})

    monkeypatch.setattr(drafting_agent, "_load_session_state", _load)
    monkeypatch.setattr(drafting_agent, "_save_session_state", _save)
    monkeypatch.setattr(gemini_client, "generate", _model)
    monkeypatch.setattr(gemini_client, "generate_longform", _model)
    monkeypatch.setattr(
        drafting_module, "pin_template_version", lambda tid, ver: pins.append((tid, ver))
    )
    return saved, pins


async def _turn():
    return await drafting_agent.handle_turn(
        user_message="Draft a generic vakalatnama for a cheque bounce case",
        case_context={},
        request_fields=dict(BLANK_DRAFT_REQUEST),
        conversation_history=[],
    )


async def test_blank_draft_without_session_does_not_crash(monkeypatch):
    saved, pins = _install_fakes(monkeypatch, session=None)
    turn = await _turn()
    assert ERROR_REPLY not in (turn.get("content") or "")
    assert saved, "the new session is saved, so a retry is not stuck on the same crash"
    assert pins == [(None, None)]


async def test_blank_draft_with_session_pins_the_session_template(monkeypatch):
    session = {"template_id": "tpl_fictional", "template_version": 3}
    _, pins = _install_fakes(monkeypatch, session=session)
    await _turn()
    assert pins == [("tpl_fictional", 3)]
