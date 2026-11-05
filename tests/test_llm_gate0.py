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
             "llm_model_options", "embedding_provider", "embedding_model", "embedding_dimensions",
             "llm_selectable_models", "gate0_meter_enabled")


@pytest.fixture(autouse=True)
def _gate0_settings():
    saved = {k: getattr(settings, k) for k in _SETTINGS}
    settings.gate0_base_url = "https://gate0.example.invalid/v1"
    settings.gate0_api_key = "test-virtual-key"
    settings.gate0_tag_requests = True
    settings.llm_selectable_models = None  # a developer's .env must not change results
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
    assert meta["case_id"] == "ws-9" and meta["request_id"] == "req-1"
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


# -- SIM-29: gateway cost and call id on embeddings, llm_call join -------------------------


class _FakeEmbedGateway:
    """Embeddings + model list, as gate0 (LiteLLM) serves them."""

    def __init__(self, headers=None, dims=3, tokens=12, models=("gemini-embedding-001",), models_error=None):
        self.kwargs = None

        async def create(**kwargs):
            self.kwargs = kwargs
            data = [NS(index=i, embedding=[0.1 * (i + 1)] * dims) for i in range(len(kwargs["input"]))]
            return _Raw(NS(data=data, usage=NS(prompt_tokens=tokens)), httpx.Headers(headers or {}))

        async def list_models():
            if models_error is not None:
                raise models_error
            return NS(data=[NS(id=m) for m in models])

        self.embeddings = NS(with_raw_response=NS(create=create))
        self.models = NS(list=list_models)


_GW_HEADERS = {"x-litellm-response-cost": "0.0007", "x-litellm-call-id": "gw-call-1"}


async def test_embedding_bills_gateway_cost():
    p = Gate0Provider()
    p._client.set(_FakeEmbedGateway(_GW_HEADERS))
    with capture_usage() as usage:
        batch = await p.embed(["a", "b"], "gemini-embedding-001", "document", 3)
    assert len(batch) == 2 and batch.gateway_call_id == "gw-call-1"
    assert batch.cost_usd == pytest.approx(0.0007)
    assert usage.embed_tokens == 12 and usage.embed_gateway_tokens == 12
    assert usage.embed_gateway_cost == pytest.approx(0.0007)


async def test_embedding_without_cost_header_falls_back_to_estimate():
    p = Gate0Provider()
    p._client.set(_FakeEmbedGateway({}))
    with capture_usage() as usage:
        batch = await p.embed(["a"], "gemini-embedding-001", "query", 3)
    assert batch.cost_usd is None and batch.gateway_call_id is None
    assert usage.embed_tokens == 12 and usage.embed_gateway_tokens == 0
    assert usage.embed_gateway_cost == 0.0


def _capture_llm_lines():
    lines: list = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG",
                      filter=lambda r: r["message"].startswith("llm_call"))
    return lines, sink


async def test_llm_call_line_carries_gateway_id_and_cost():
    settings.llm_default = "gate0:gpt-4.1"
    settings.llm_fallbacks = None
    c = ModelClient()
    c.router.reload()
    provider = c.router.provider_for(parse_profile("gate0:gpt-4.1"))
    provider._client.set(_FakeGateway(_response("hi"), {
        "x-litellm-response-cost": "0.0123", "x-litellm-call-id": "gw-call-7"}))
    lines, sink = _capture_llm_lines()
    try:
        assert await c.generate("q", task="t") == "hi"
    finally:
        logger.remove(sink)
    assert len(lines) == 1
    assert "cost=0.0123" in lines[0] and "gateway_call_id=gw-call-7" in lines[0]


async def test_embed_logs_one_llm_call_line():
    settings.embedding_provider = "gate0"
    settings.embedding_model = "gemini-embedding-001"
    settings.embedding_dimensions = 3
    c = ModelClient()
    c.router.reload()
    c.router.provider_for(c.router.embedding_profile())._client.set(_FakeEmbedGateway(_GW_HEADERS))
    lines, sink = _capture_llm_lines()
    try:
        vectors = await c.router.embed(["a", "b"], "document")
    finally:
        logger.remove(sink)
    assert len(vectors) == 2
    assert len(lines) == 1 and lines[0].startswith("llm_call embed feature=embeddings")
    assert "gateway_call_id=gw-call-1" in lines[0] and "cost=0.0007" in lines[0]


# -- SIM-29: model features from configuration, not model names ---------------------------


def _params(model, **req):
    return Gate0Provider().build_params(LLMRequest.from_prompt("q", **req), model)


def test_anthropic_family_gets_reasoning_without_gemini_parameters():
    settings.llm_model_options = json.dumps(
        {"claude-sonnet-x": {"family": "anthropic", "thinking": True, "max_output_tokens": 20000}})
    clear_caches()
    params = _params("claude-sonnet-x", max_tokens=1000)
    assert params["reasoning_effort"] == "low" and "temperature" not in params
    assert params["max_tokens"] == 8192
    assert "safety_settings" not in params.get("extra_body", {})
    assert "web_search_options" not in params
    # JSON on Claude is a forced tool call: thinking must be off.
    params = _params("claude-sonnet-x", json_mode=True)
    assert "reasoning_effort" not in params and "temperature" in params


def test_unconfigured_model_gets_a_plain_request():
    settings.llm_model_options = None
    clear_caches()
    for model in ("qwen3-32b", "claude-sonnet-x", "gpt-4o", "llama-3.3-70b"):
        params = _params(model)
        assert "reasoning_effort" not in params, model
        assert "temperature" in params, model
        assert "safety_settings" not in params.get("extra_body", {}), model
        caps = Gate0Provider().capabilities(model)
        assert caps.documents is False and caps.grounding is False and caps.thinking is False, model


def test_open_source_model_is_configured_entirely_by_options():
    settings.llm_model_options = json.dumps({"qwen3-32b": {
        "supports_documents": True, "thinking": True, "max_output_tokens": 8000,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}, "metadata": "ignored"}}})
    clear_caches()
    c = ModelClient()
    c.router.reload()
    provider = c.router.provider_for(parse_profile("gate0:qwen3-32b"))
    caps = c.router.capabilities(provider, "qwen3-32b")
    assert caps.documents is True and caps.thinking is True and caps.max_output_tokens == 8000
    params = _params("qwen3-32b")
    assert params["reasoning_effort"] == "low" and "temperature" not in params
    extra = params["extra_body"]
    assert extra["chat_template_kwargs"] == {"enable_thinking": True}
    assert isinstance(extra["metadata"], dict)  # the adapter's own attribution wins


def test_thinking_false_overrides_the_reasoning_model_name_rule():
    settings.llm_model_options = json.dumps({"o3-mini": {"thinking": False}})
    clear_caches()
    params = _params("o3-mini")
    assert "reasoning_effort" not in params and "temperature" in params


def test_grounding_only_where_configured():
    settings.llm_model_options = None
    clear_caches()
    assert Gate0Provider().capabilities("gpt-4o").grounding is False
    assert "web_search_options" not in _params("gpt-4o", grounding=True)
    settings.llm_model_options = json.dumps({"gpt-4o-search": {"grounding": True}})
    clear_caches()
    assert Gate0Provider().capabilities("gpt-4o-search").grounding is True
    params = _params("gpt-4o-search", grounding=True, json_mode=True)
    assert params["web_search_options"] == {} and "response_format" not in params


def test_cohere_input_type_through_gate0():
    settings.llm_model_options = json.dumps({"bedrock-embed": {"cohere_input_type": True}})
    clear_caches()
    p = Gate0Provider()
    assert p._embed_extra_body("cohere.embed-multilingual-v3", "query")["input_type"] == "search_query"
    assert p._embed_extra_body("bedrock-embed", "document")["input_type"] == "search_document"
    assert "input_type" not in p._embed_extra_body("titan-embed-v2", "query")
    assert "input_type" not in p._embed_extra_body("gemini-embedding-001", "query")


# -- SIM-29: startup check that gate0 serves every configured model ----------------------


def _enforced_full_config(monkeypatch, gateway):
    settings.gate0_enforce = True
    settings.llm_default = "gate0:gemini-3.8-flash"
    settings.llm_fallbacks = '["gate0:gpt-4.1"]'
    settings.embedding_provider = "gate0"
    settings.embedding_model = "gemini-embedding-001"
    c = ModelClient()
    c.router.reload()
    c.router.provider_for(parse_profile("gate0:gemini-3.8-flash"))._client.set(gateway)
    import app.core.lifecycle as lifecycle_mod

    monkeypatch.setattr(lifecycle_mod, "gemini_client", c)
    return c


async def test_startup_reports_models_gate0_does_not_serve(monkeypatch):
    from app.core.lifecycle import ApplicationLifecycle

    c = _enforced_full_config(monkeypatch, _FakeEmbedGateway(
        models=("gemini-3.8-flash", "gemini-embedding-001")))
    assert await c.router.gate0_unserved_models() == ["gpt-4.1"]
    lines: list = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG")
    try:
        await ApplicationLifecycle._check_gate0_models()
    finally:
        logger.remove(sink)
    assert ("ERROR", "gate0 does not serve configured model gpt-4.1") in lines


async def test_served_model_probe_failure_does_not_block_startup(monkeypatch):
    from app.core.lifecycle import ApplicationLifecycle

    _enforced_full_config(monkeypatch, _FakeEmbedGateway(
        models_error=openai.APIConnectionError(request=httpx.Request("GET", "https://gate0.example.invalid"))))
    lines: list = []
    sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG")
    try:
        await ApplicationLifecycle._check_gate0_models()  # must not raise
    finally:
        logger.remove(sink)
    assert any(level == "WARNING" and "Could not list gate0 models" in msg for level, msg in lines)


# LiteLLM (1.105.1) rejects with 401 any request whose body or metadata carries one
# of these keys (client-side credential / endpoint selectors). A subset that a
# well-meaning attribution field could collide with.
_LITELLM_BANNED_KEYS = {"workspace_id", "aws_workspace_id", "anthropic_workspace_id",
                        "api_base", "base_url", "deployment_url", "model_list"}


def test_requests_never_carry_a_key_litellm_rejects():
    p = Gate0Provider()
    with logger.contextualize(request_id="req-1", workspace_id="ws-9", event="user_message_request"):
        params = p.build_params(LLMRequest.from_prompt("q", tags={"feature": "chat"}), "gpt-4o")
        embed_extra = p._embed_extra_body("gemini-embedding-001", "query")
    for body in (params, params["extra_body"], params["extra_body"]["metadata"],
                 embed_extra, embed_extra["metadata"]):
        assert not (_LITELLM_BANNED_KEYS & set(body)), sorted(_LITELLM_BANNED_KEYS & set(body))
    # The case is still attributed, under a key gate0 accepts and the reconcile tag.
    assert params["extra_body"]["metadata"]["case_id"] == "ws-9"
    assert "workspace_id:ws-9" in params["extra_body"]["metadata"]["tags"]
