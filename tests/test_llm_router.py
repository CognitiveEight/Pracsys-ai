"""Model routing, fallback, capability checks, cost and the facade contract.

Offline: every provider is a fake registered in place of the real adapter.
"""

from __future__ import annotations

import asyncio
import ast
import pathlib
from typing import List

import pytest

from app.ai.gemini_client import ModelClient, is_generation_error
from app.ai.usage import capture_usage, llm_price, primary_model
from app.core.config import settings
from app.llm import router as router_mod
from app.llm.base import Provider
from app.llm.config import RoutingConfigError, parse_profile
from app.llm.errors import EmbeddingError, ProviderError
from app.llm.types import Capabilities, ImagePart, LLMRequest, LLMResult, Message, TextPart

_LLM_SETTINGS = (
    "llm_default",
    "llm_routes",
    "llm_fallbacks",
    "llm_grounding_profiles",
    "llm_pricing_overrides",
    "llm_model_options",
    "llm_transient_retries",
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "embedding_max_attempts",
    "gemini_request_timeout_seconds",
)


class FakeProvider(Provider):
    """Scripted provider: each call pops the next outcome for its model."""

    script: dict = {}
    calls: List[tuple] = []
    caps = Capabilities()

    def __init__(self, account: str = "") -> None:
        super().__init__(account)

    def capabilities(self, model: str) -> Capabilities:
        return type(self).caps

    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        type(self).calls.append((self.name, model, request))
        outcome = type(self).script[model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(60)
        text, finish = outcome
        return LLMResult(text=text, finish=finish, provider=self.name, model=model,
                         input_tokens=1000, output_tokens=500)

    async def embed(self, texts, model, task, dimensions):
        type(self).calls.append((self.name, model, task))
        return type(self).script["embed"].pop(0)


class FakeA(FakeProvider):
    name = "openai"


class FakeB(FakeProvider):
    name = "bedrock"
    caps = Capabilities(images=True)


class FakeGrounding(FakeProvider):
    name = "gemini"
    caps = Capabilities(grounding=True)

    async def generate(self, request, model):
        result = await super().generate(request, model)
        result.sources = [{"title": "t", "url": "https://example.invalid/x"}]
        return result


@pytest.fixture
def client(monkeypatch):
    saved = {k: getattr(settings, k) for k in _LLM_SETTINGS}
    FakeProvider.script = {}
    FakeProvider.calls = []
    monkeypatch.setattr(
        router_mod, "provider_classes",
        lambda: {"openai": FakeA, "bedrock": FakeB, "gemini": FakeGrounding},
    )
    settings.llm_default = "openai:model-a"
    settings.llm_routes = None
    settings.llm_fallbacks = '["bedrock:model-b"]'
    settings.llm_grounding_profiles = '["gemini:model-g"]'
    settings.llm_pricing_overrides = '{"model-a": [1.0, 2.0], "model-b": [10.0, 20.0]}'
    settings.llm_model_options = None
    settings.llm_transient_retries = 1
    settings.gemini_request_timeout_seconds = 300.0
    c = ModelClient()
    c.router.reload()
    yield c
    for k, v in saved.items():
        setattr(settings, k, v)
    c.router.reload()


# -- profiles -------------------------------------------------------------------


def test_profile_parsing_keeps_colons_in_model_ids():
    p = parse_profile("bedrock:us.anthropic.claude-x-v1:0")
    assert (p.provider, p.model) == ("bedrock", "us.anthropic.claude-x-v1:0")
    p = parse_profile("openai_compat:ollama:qwen3:32b")
    assert (p.provider, p.account, p.model) == ("openai_compat", "ollama", "qwen3:32b")
    assert str(p) == "openai_compat:ollama:qwen3:32b"


@pytest.mark.parametrize("bad", ["", "gemini", "nope:model", "openai_compat:onlyaccount"])
def test_bad_profiles_are_rejected(bad):
    with pytest.raises(RoutingConfigError):
        parse_profile(bad)


def test_routes_and_fallbacks_build_the_chain(client):
    settings.llm_routes = '{"analysis_report": ["bedrock:model-b", "openai:model-a"]}'
    client.router.reload()
    chain = [str(p) for p in client.router.routing.chain("analysis_report")]
    assert chain == ["bedrock:model-b", "openai:model-a"]  # fallback de-duplicated
    assert [str(p) for p in client.router.routing.chain("other")] == [
        "openai:model-a", "bedrock:model-b"]


def test_malformed_routes_fail_loudly(client):
    settings.llm_routes = "{not json"
    client.router.reload()
    with pytest.raises(RoutingConfigError):
        client.router.routing


# -- fallback / retry ---------------------------------------------------------------


async def test_ok_result_is_returned_from_the_first_profile(client):
    FakeProvider.script = {"model-a": [("hello", "ok")]}
    assert await client.generate("hi", task="t") == "hello"
    assert [c[1] for c in FakeProvider.calls] == ["model-a"]


async def test_error_falls_back_to_the_next_profile(client):
    FakeProvider.script = {"model-a": [ProviderError("400 bad")], "model-b": [("from b", "ok")]}
    assert await client.generate("hi", task="t") == "from b"


async def test_transient_error_is_retried_on_the_same_profile_first(client, monkeypatch):
    monkeypatch.setattr(router_mod.asyncio, "sleep", _no_sleep)
    FakeProvider.script = {"model-a": [ProviderError("429", transient=True), ("again", "ok")]}
    assert await client.generate("hi", task="t") == "again"
    assert [c[1] for c in FakeProvider.calls] == ["model-a", "model-a"]


async def test_blocked_is_final_and_never_routed_around(client):
    FakeProvider.script = {"model-a": [("", "blocked")], "model-b": [("leak", "ok")]}
    assert await client.generate("hi", task="t") == ""
    assert [c[1] for c in FakeProvider.calls] == ["model-a"]


async def test_timeout_moves_down_the_chain(client):
    settings.gemini_request_timeout_seconds = 0.05
    FakeProvider.script = {"model-a": ["hang"], "model-b": [("late", "ok")]}
    assert await client.generate("hi", task="t") == "late"


async def test_all_profiles_failing_returns_a_sentinel(client):
    FakeProvider.script = {"model-a": [ProviderError("boom")], "model-b": [ProviderError("boom")]}
    out = await client.generate("hi", task="t")
    assert is_generation_error(out) and "boom" in out


async def test_timeout_sentinel_names_the_timeout(client):
    settings.gemini_request_timeout_seconds = 0.05
    settings.llm_fallbacks = None
    client.router.reload()
    FakeProvider.script = {"model-a": ["hang"]}
    out = await client.generate("hi", task="t")
    assert is_generation_error(out) and "timed out" in out


async def test_cancellation_propagates(client):
    FakeProvider.script = {"model-a": [asyncio.CancelledError()]}
    with pytest.raises(asyncio.CancelledError):
        await client.generate("hi", task="t")


async def test_unconfigured_provider_gives_not_available_sentinel(client, monkeypatch):
    monkeypatch.setattr(FakeA, "unavailable_reason", lambda self: "OPENAI_API_KEY is not set")
    monkeypatch.setattr(FakeB, "unavailable_reason", lambda self: "no creds")
    out = await client.generate("hi", task="t")
    assert is_generation_error(out) and out.startswith("Error: model not available")
    assert not client.is_available()


# -- capabilities -------------------------------------------------------------------


async def test_request_needing_images_skips_a_text_only_profile(client):
    FakeProvider.script = {"model-b": [("saw it", "ok")]}
    req = LLMRequest(messages=[Message("user", [TextPart("x"), ImagePart(b"\x89PNG", "image/png")])])
    result = await client.router.complete(req, task="t", kind="generate")
    assert result.text == "saw it" and [c[1] for c in FakeProvider.calls] == ["model-b"]


async def test_output_budget_is_clamped_to_the_model_ceiling(client):
    settings.llm_model_options = '{"model-a": {"max_output_tokens": 8192}}'
    client.router.reload()
    FakeProvider.script = {"model-a": [("ok", "ok")]}
    await client.generate("hi", max_tokens=12000, task="t")
    assert FakeProvider.calls[0][2].max_tokens == 8192


async def test_grounded_calls_use_only_grounding_profiles(client):
    FakeProvider.script = {"model-g": [("answer", "ok")]}
    text, sources = await client.generate_grounded("latest case law")
    assert text == "answer" and sources[0]["url"].startswith("https://")
    assert [c[1] for c in FakeProvider.calls] == ["model-g"]


async def test_grounding_without_a_capable_profile_degrades_to_empty(client):
    settings.llm_grounding_profiles = '["openai:model-a"]'
    client.router.reload()
    assert await client.generate_grounded("q") == ("", [])
    assert FakeProvider.calls == []


# -- JSON, longform, chat --------------------------------------------------------------


async def test_json_mode_strips_fences_and_reasoning(client):
    FakeProvider.script = {"model-a": [('<think>hmm</think>```json\n{"a": 1,}\n```', "ok")]}
    out = await client.generate("hi", task="t", response_mime_type="application/json")
    assert out == '{"a": 1}'
    assert FakeProvider.calls[0][2].json_mode is True


async def test_malformed_json_gets_a_repair_round(client):
    FakeProvider.script = {"model-a": [("not json at all", "ok"), ('{"fixed": true}', "ok")]}
    out = await client.generate("hi", task="t", response_mime_type="application/json")
    assert out == '{"fixed": true}'
    assert FakeProvider.calls[1][2].temperature == 0.0


async def test_repair_can_be_disabled_for_callers_that_recover_themselves(client):
    FakeProvider.script = {"model-a": [('[{"a": 1}, {"b"', "ok")]}
    out = await client.generate(
        "hi", task="t", response_mime_type="application/json", repair_json=False)
    assert out == '[{"a": 1}, {"b"' and len(FakeProvider.calls) == 1


async def test_longform_continues_until_the_document_finishes(client):
    FakeProvider.script = {"model-a": [("part one ", "truncated"), ("part two", "ok")]}
    out = await client.generate_longform("draft it")
    assert out == "part one part two"
    assert FakeProvider.calls[1][2].thinking == "MINIMAL"


async def test_chat_folds_system_messages(client):
    FakeProvider.script = {"model-a": [("reply", "ok")]}
    out = await client.chat([{"role": "system", "content": "be brief"},
                             {"role": "user", "content": "q"},
                             {"role": "assistant", "content": "a"},
                             {"role": "user", "content": "q2"}])
    req = FakeProvider.calls[0][2]
    assert out == "reply" and req.system == "be brief" and len(req.messages) == 3


async def test_generate_sync_runs_on_the_main_loop_from_a_worker_thread(client):
    FakeProvider.script = {"model-a": [("from thread", "ok")]}
    client.initialize()  # captures the running loop
    with capture_usage() as usage:
        out = await asyncio.to_thread(client.generate_sync, "hi", task="t")
    assert out == "from thread"
    assert usage.input_tokens == 1000  # billed to the scope that started the work


async def test_generate_sync_refuses_the_loop_thread(client):
    assert is_generation_error(client.generate_sync("hi"))


# -- cost -------------------------------------------------------------------------------


async def test_mixed_models_in_one_scope_are_priced_per_model(client):
    settings.llm_routes = '{"cheap": ["openai:model-a"], "dear": ["bedrock:model-b"]}'
    client.router.reload()
    FakeProvider.script = {"model-a": [("a", "ok")], "model-b": [("b", "ok")]}
    with capture_usage() as usage:
        await client.generate("x", task="cheap")
        await client.generate("y", task="dear")
    price = llm_price(usage)
    # 1000 in / 500 out on each: a = 0.001 + 0.001; b = 0.01 + 0.01
    assert price["price"] == pytest.approx(0.022)
    assert primary_model(usage) in ("model-a", "model-b")
    assert set(usage.models) == {"model-a", "model-b"}


# -- embeddings ------------------------------------------------------------------------


async def test_embeddings_are_validated_and_normalised(client):
    settings.embedding_provider = "openai"
    settings.embedding_model = "emb"
    settings.embedding_dimensions = 2
    settings.embedding_max_attempts = 1
    client.router.reload()
    FakeProvider.script = {"embed": [[[3.0, 4.0]]]}
    assert await client.embed_query("q") == pytest.approx([0.6, 0.8])
    assert FakeProvider.calls[-1][2] == "query"
    for bad in ([[1.0, 2.0, 3.0]], [[0.0, 0.0]], []):
        FakeProvider.script = {"embed": [bad]}
        with pytest.raises(EmbeddingError):
            await client.embed_text("d")


async def _no_sleep(*_a, **_k):
    return None


# -- boundaries --------------------------------------------------------------------------

_SDK_MODULES = ("google.generativeai", "google.genai", "openai", "anthropic", "boto3",
                "aioboto3", "botocore", "azure.identity")


def test_no_provider_sdk_is_imported_outside_the_adapters():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "app").rglob("*.py")) + [root / "agent.py"]:
        if "app/llm/providers" in path.as_posix():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            if any(n == m or n.startswith(m + ".") for n in names for m in _SDK_MODULES):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []


def test_default_routing_is_unchanged_gemini():
    for k in ("llm_default", "llm_routes", "llm_fallbacks", "llm_grounding_profiles"):
        assert getattr(settings, k) in (None, ""), f"{k} set in the test environment"
    c = ModelClient()
    assert [str(p) for p in c.router.routing.chain("anything")] == [f"gemini:{settings.gemini_model}"]
    assert str(c.router.embedding_profile()).startswith("gemini:")
