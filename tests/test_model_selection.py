"""Per-case model selection (``aiModel``): routing, overrides, pins, safety."""

from __future__ import annotations

import json

import pytest
from loguru import logger

from app.core.config import settings
from app.llm.config import clear_caches
from app.llm.router import ModelRouter
from app.workspace import model_selection as ms

_SETTINGS = ("llm_default", "llm_routes", "llm_fallbacks", "llm_grounding_profiles",
             "llm_selectable_models", "embedding_provider", "embedding_model",
             "embedding_dimensions", "gate0_enforce")


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    saved = {k: getattr(settings, k) for k in _SETTINGS}
    settings.llm_default = "gate0:gemini-3.8-flash"
    settings.llm_routes = json.dumps({"audio_transliteration": ["gate0:gemini-2.5-flash"]})
    settings.llm_fallbacks = None
    settings.llm_grounding_profiles = json.dumps(["gate0:gemini-3.8-flash"])
    settings.llm_selectable_models = json.dumps(
        {"gemini-3.8-flash": "gate0:gemini-3.8-flash", "gpt-4o": "gate0:gpt-4o",
         "gemini-2.5-pro": "gate0:gemini-2.5-pro"})
    settings.gate0_enforce = False
    monkeypatch.setattr(ms, "_persist", lambda ws: None)
    ms._selected.clear()
    clear_caches()
    yield
    for k, v in saved.items():
        setattr(settings, k, v)
    ms._selected.clear()
    clear_caches()


def _head(router, task="list_of_dates"):
    return str(router.chain_for(task)[0])


def test_default_without_a_choice():
    with logger.contextualize(workspace_id="ws-1"):
        assert _head(ModelRouter()) == "gate0:gemini-3.8-flash"


def test_case_choice_routes_every_task_and_keeps_default_as_fallback():
    ms.set_workspace_model("ws-1", "gpt-4o")
    with logger.contextualize(workspace_id="ws-1"):
        chain = [str(p) for p in ModelRouter().chain_for("list_of_dates")]
    assert chain == ["gate0:gpt-4o", "gate0:gemini-3.8-flash"]
    with logger.contextualize(workspace_id="ws-2"):  # other cases unaffected
        assert _head(ModelRouter()) == "gate0:gemini-3.8-flash"


def test_chat_message_override_wins_for_that_turn_only():
    ms.set_workspace_model("ws-1", "gpt-4o")
    with logger.contextualize(workspace_id="ws-1"):
        token = ms.set_message_model("gemini-2.5-pro")
        assert _head(ModelRouter(), "explain_why_node") == "gate0:gemini-2.5-pro"
        ms.reset_message_model(token)
        assert _head(ModelRouter(), "explain_why_node") == "gate0:gpt-4o"
    assert ms.get_workspace_model("ws-1") == "gpt-4o"


def test_unknown_model_is_ignored_and_case_keeps_its_model():
    ms.set_workspace_model("ws-1", "gpt-4o")
    assert ms.set_workspace_model("ws-1", "some-unvetted-model") == "gpt-4o"
    assert ms.set_message_model("some-unvetted-model") is None


def test_absent_means_unchanged_and_empty_resets_to_default():
    ms.set_workspace_model("ws-1", "gpt-4o")
    assert ms.set_workspace_model("ws-1", None) is None
    assert ms.get_workspace_model("ws-1") is None


def test_pinned_tasks_grounding_and_embeddings_ignore_the_choice():
    ms.set_workspace_model("ws-1", "gpt-4o")
    router = ModelRouter()
    with logger.contextualize(workspace_id="ws-1"):
        assert _head(router, "audio_transliteration") == "gate0:gemini-2.5-flash"
        assert [str(p) for p in router.routing.grounding] == ["gate0:gemini-3.8-flash"]


def test_selectable_models_are_validated_at_startup():
    settings.gate0_enforce = True
    settings.llm_selectable_models = json.dumps({"gpt-4o": "openai:gpt-4o"})
    clear_caches()
    router = ModelRouter()
    problems = dict(router.problems())
    assert "openai:gpt-4o" in problems and "GATE0_ENFORCE" in problems["openai:gpt-4o"]


def test_hydration_restores_but_never_overrides_a_newer_choice():
    ms.hydrate_from_cache("ws-1", {"model": "gpt-4o"})
    assert ms.get_workspace_model("ws-1") == "gpt-4o"
    ms.set_workspace_model("ws-1", "gemini-2.5-pro")
    ms.hydrate_from_cache("ws-1", {"model": "gpt-4o"})
    assert ms.get_workspace_model("ws-1") == "gemini-2.5-pro"


def test_purge_clears_the_choice():
    ms.set_workspace_model("ws-1", "gpt-4o")
    ms.clear_model_selection("ws-1")
    assert ms.get_workspace_model("ws-1") is None


async def test_inbound_events_apply_ai_model(monkeypatch):
    from app.socket.events import SocketEventHandler

    seen = {}

    async def fake_dispatch(self, event_name, data, handler):
        seen[event_name] = _head(ModelRouter(), "explain_why_node")

    monkeypatch.setattr(SocketEventHandler, "_dispatch_in_run_scope", fake_dispatch)
    h = SocketEventHandler.__new__(SocketEventHandler)
    h._handlers = {"workspace_sync": object(), "user_message_request": object()}
    await h.handle("workspace_sync", {"workspaceId": "ws-1", "aiModel": "gpt-4o"})
    await h.handle("user_message_request", {"workspaceId": "ws-1", "aiModel": "gemini-2.5-pro"})
    assert seen == {"workspace_sync": "gate0:gpt-4o", "user_message_request": "gate0:gemini-2.5-pro"}
    assert ms.get_workspace_model("ws-1") == "gpt-4o"  # the chat override did not stick


def test_gate0_gemini_embeddings_keep_the_existing_collection():
    from app.llm.config import embedding_space_suffix

    settings.embedding_model, settings.embedding_dimensions = "gemini-embedding-001", 768
    for provider in ("gemini", "gate0"):
        settings.embedding_provider = provider
        assert embedding_space_suffix() == ""
    settings.embedding_provider = "openai"
    assert embedding_space_suffix() != ""
