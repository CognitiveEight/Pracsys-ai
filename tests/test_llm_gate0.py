"""Gate0 (governed LiteLLM gateway) adapter and GATE0_ENFORCE governance mode.

Offline: a fake OpenAI client stands in for the gateway.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest
from loguru import logger

from app.ai.gemini_client import ModelClient, is_generation_error
from app.ai.usage import UsageAccumulator, capture_usage, llm_price
from app.core.config import settings
from app.llm.config import clear_caches, parse_profile
from app.llm.errors import ProviderError
from app.llm.providers.gate0 import Gate0Provider
from app.llm.types import LLMRequest

_SETTINGS = ("gate0_base_url", "gate0_api_key", "gate0_enforce", "gate0_tag_requests",
             "llm_default", "llm_routes", "llm_fallbacks", "llm_grounding_profiles",
             "llm_model_options", "embedding_provider", "embedding_model", "embedding_dimensions")


@pytest.fixture(autouse=True)
def _gate0_settings():
    saved = {k: getattr(settings, k) for k in _SETTINGS}
    settings.gate0_base_url = "https://gate0.example.invalid/v1"
    settings.gate0_api_key = "test-virtual-key"
    settings.gate0_tag_requests = True
    clear_caches()
    yield
    for k, v in saved.items():
        setattr(settings, k, v)
    clear_caches()


class _Raw:
    def __init__(self, response, headers):
        self._response, self.headers = response, headers

    def parse(self):
        return self._response


class _FakeGateway:
    def __init__(self, response, headers=None, error=None):
        self.params = None

        async def create(**params):
            self.params = params
            if error is not None:
                raise error
            return _Raw(response, httpx.Headers(headers or {}))

        self.chat = NS(completions=NS(with_raw_response=NS(create=create)))


def _response(content="ok", finish="stop", annotations=None):
    return NS(
        choices=[NS(finish_reason=finish, message=NS(content=content, annotations=annotations))],
        usage=NS(prompt_tokens=100, completion_tokens=50, completion_tokens_details=None),
    )


def _status_error(status, message):
    response = httpx.Response(status, request=httpx.Request("POST", "https://gate0.example.invalid"))
    return openai.APIStatusError(message, response=response, body=None)


# -- request mapping ------------------------------------------------------------------


def test_gemini_requests_carry_thinking_safety_and_attribution():
    p = Gate0Provider()
    req = LLMRequest.from_prompt("q", json_mode=True, thinking="MINIMAL", max_tokens=12000,
                                 tags={"feature": "list_of_dates", "kind": "generate"})
    with logger.contextualize(request_id="req-1", workspace_id="ws-9", event="workspace_sync"):
        params = p.build_params(req, "gemini-3.8-flash")
    assert params["max_tokens"] == 16000 and params["reasoning_effort"] == "low"
    assert params["temperature"] == 0.7
    assert params["response_format"] == {"type": "json_object"}
    extra = params["extra_body"]
    assert {s["threshold"] for s in extra["safety_settings"]} == {"BLOCK_NONE"}
    meta = extra["metadata"]
    assert meta["workspace_id"] == "ws-9" and meta["request_id"] == "req-1"
    assert meta["feature"] == "list_of_dates" and meta["app"] == "simplertoday"
    assert "workspace_id:ws-9" in meta["tags"] and "feature:list_of_dates" in meta["tags"]


def test_non_gemini_models_get_no_gemini_parameters():
    params = Gate0Provider().build_params(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert "reasoning_effort" not in params
    assert "safety_settings" not in params["extra_body"]
    assert params["max_tokens"] == 12000


def test_task_class_can_be_declared_gemini_and_capped():
    settings.llm_model_options = json.dumps({"gate0/lite": {"gemini": True, "max_output_tokens": 8192}})
    clear_caches()
    p = Gate0Provider()
    assert p.capabilities("gate0/lite").grounding is True
    params = p.build_params(LLMRequest.from_prompt("q"), "gate0/lite")
    assert params["max_tokens"] == 8192 and "safety_settings" in params["extra_body"]


def test_grounding_uses_web_search_and_drops_json_mode():
    req = LLMRequest.from_prompt("latest", grounding=True, json_mode=True)
    params = Gate0Provider().build_params(req, "gemini-3.8-flash")
    assert params["web_search_options"] == {} and "response_format" not in params


def test_tagging_can_be_switched_off():
    settings.gate0_tag_requests = False
    params = Gate0Provider().build_params(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert "extra_body" not in params


# -- response -------------------------------------------------------------------------


async def test_gateway_cost_header_and_grounding_sources():
    text = "Section 318 BNS was applied."
    ann = [{"type": "url_citation", "url_citation": {
        "url": "https://indiankanoon.example/doc/1", "title": "Judgment", "start_index": 0, "end_index": 15}}]
    p = Gate0Provider()
    p._client.set(_FakeGateway(_response(text, annotations=ann), {"x-litellm-response-cost": "0.0042"}))
    result = await p.generate(LLMRequest.from_prompt("q", grounding=True), "gemini-3.8-flash")
    assert result.cost_usd == pytest.approx(0.0042)
    assert result.sources == [{"title": "Judgment", "url": "https://indiankanoon.example/doc/1",
                               "supports": ["Section 318 BNS"]}]


async def test_budget_block_is_final_but_rate_limit_is_transient():
    p = Gate0Provider()
    p._client.set(_FakeGateway(None, error=_status_error(429, "Budget has been exceeded for team")))
    with pytest.raises(ProviderError) as err:
        await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert err.value.transient is False and "budget" in str(err.value)
    p._client.set(_FakeGateway(None, error=_status_error(429, "Rate limit reached")))
    with pytest.raises(ProviderError) as err:
        await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert err.value.transient is True


def test_embedding_task_type_and_dimensions_for_gemini_models():
    p = Gate0Provider()
    assert p._embed_extra_body("gemini-embedding-001", "query")["task_type"] == "RETRIEVAL_QUERY"
    assert "task_type" not in p._embed_extra_body("text-embedding-3-large", "document")
    assert p._sends_dimensions("gemini-embedding-001") is True


# -- billing ----------------------------------------------------------------------------


def test_gateway_cost_replaces_the_table_price():
    acc = UsageAccumulator()
    acc.add(1000, 500, model="gemini-3.8-flash", cost=0.01)
    assert llm_price(acc)["price"] == pytest.approx(0.01)
    assert acc.input_price + acc.output_price == pytest.approx(0.01)


async def test_router_bills_the_gateway_cost():
    settings.llm_default = "gate0:gpt-4.1"
    settings.llm_fallbacks = None
    c = ModelClient()
    c.router.reload()
    provider = c.router.provider_for(parse_profile("gate0:gpt-4.1"))
    provider._client.set(_FakeGateway(_response("hi"), {"x-litellm-response-cost": "0.5"}))
    with capture_usage() as usage:
        assert await c.generate("q", task="t") == "hi"
    assert llm_price(usage)["price"] == pytest.approx(0.5)


# -- governance -------------------------------------------------------------------------


def test_enforce_rejects_direct_profiles_at_startup():
    settings.gate0_enforce = True
    settings.llm_default = "gate0:gemini-3.8-flash"
    settings.llm_fallbacks = '["gemini:gemini-3.8-flash"]'
    settings.embedding_provider = "gemini"
    c = ModelClient()
    c.router.reload()
    problems = dict(c.router.problems())
    assert "gemini:gemini-3.8-flash" in problems and "GATE0_ENFORCE" in problems["gemini:gemini-3.8-flash"]
    assert "gate0:gemini-3.8-flash" not in problems
    with pytest.raises(RuntimeError, match="misconfigured"):
        c.initialize()


async def test_enforce_never_calls_a_direct_provider_at_runtime(monkeypatch):
    settings.gate0_enforce = True
    settings.llm_default = "gemini:gemini-3.8-flash"
    settings.llm_fallbacks = None
    c = ModelClient()
    c.router.reload()
    called = []

    async def _direct(*a, **k):
        called.append(1)

    monkeypatch.setattr(c.router.provider_for(parse_profile("gemini:gemini-3.8-flash")), "generate", _direct)
    out = await c.generate("q", task="t")
    assert is_generation_error(out) and called == []


def test_enforce_without_gemini_gate0_profile_disables_grounding():
    settings.gate0_enforce = True
    settings.llm_default = "gate0:gpt-4.1"
    settings.llm_fallbacks = None
    settings.llm_grounding_profiles = None
    c = ModelClient()
    c.router.reload()
    assert c.router.routing.grounding == []
    settings.llm_default = "gate0:gemini-3.8-flash"
    c.router.reload()
    assert [str(p) for p in c.router.routing.grounding] == ["gate0:gemini-3.8-flash"]


def test_full_gate0_configuration_is_clean():
    settings.gate0_enforce = True
    settings.llm_default = "gate0:gemini-3.8-flash"
    settings.llm_fallbacks = '["gate0:gpt-4.1"]'
    settings.embedding_provider = "gate0"
    settings.embedding_model = "gemini-embedding-001"
    c = ModelClient()
    c.router.reload()
    assert c.router.problems() == []
    assert c.is_available()
