"""Request/response mapping of each provider adapter, against fake SDK clients.

Offline: no network, no credentials. Live checks are scripts/smoke_llm_providers.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from app.core.config import settings
from app.llm.config import clear_caches
from app.llm.errors import ProviderError
from app.llm.json_utils import strip_reasoning
from app.llm.providers.aws import BedrockProvider, SageMakerProvider
from app.llm.providers.azure_openai import AzureOpenAIProvider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai_compat import OpenAIChatProvider, OpenAICompatProvider
from app.llm.types import DocumentPart, ImagePart, LLMRequest, Message, TextPart


def _completion(content, finish="stop", prompt=10, completion=5, reasoning=0):
    return NS(
        choices=[NS(finish_reason=finish, message=NS(content=content))],
        usage=NS(prompt_tokens=prompt, completion_tokens=completion,
                 completion_tokens_details=NS(reasoning_tokens=reasoning)),
    )


class _FakeChat:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.params = []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **params):
        self.params.append(dict(params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _bad_request(message, code=None):
    body = {"message": message, "code": code} if code else None
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
    return openai.BadRequestError(message, response=response, body=body)


@pytest.fixture(autouse=True)
def _reset():
    saved = (settings.openai_compat_profiles, settings.azure_openai_deployments,
             settings.sagemaker_endpoints, settings.bedrock_guardrail_id,
             settings.bedrock_guardrail_version)
    clear_caches()
    yield
    (settings.openai_compat_profiles, settings.azure_openai_deployments,
     settings.sagemaker_endpoints, settings.bedrock_guardrail_id,
     settings.bedrock_guardrail_version) = saved
    clear_caches()


# -- OpenAI family -------------------------------------------------------------------


def test_openai_json_mode_sets_response_format_and_mentions_json():
    p = OpenAIChatProvider()
    params = p.build_params(LLMRequest.from_prompt("q", system="sys", json_mode=True), "gpt-4.1")
    assert params["response_format"] == {"type": "json_object"}
    assert "JSON" in params["messages"][0]["content"] and params["messages"][0]["role"] == "system"
    assert params["max_completion_tokens"] == 12000 and params["temperature"] == 0.7
    assert params["messages"][1] == {"role": "user", "content": "q"}


def test_openai_reasoning_models_get_effort_not_temperature():
    params = OpenAIChatProvider().build_params(
        LLMRequest.from_prompt("q", thinking="MINIMAL"), "o4-mini")
    assert "temperature" not in params and params["reasoning_effort"] == "low"


def test_openai_images_and_pdfs_become_content_parts():
    req = LLMRequest(messages=[Message("user", [
        TextPart("look"), ImagePart(b"img", "image/png"), DocumentPart(b"%PDF", "application/pdf", "a.pdf")])])
    content = OpenAIChatProvider().build_params(req, "gpt-4.1")["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["file"]["file_data"].startswith("data:application/pdf;base64,")


async def test_openai_result_mapping_and_reasoning_strip():
    p = OpenAIChatProvider()
    fake = _FakeChat([_completion("<think>plan</think>answer", "length", reasoning=3)])
    p._client.set(fake)
    result = await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert (result.text, result.finish, result.input_tokens, result.output_tokens,
            result.thinking_tokens) == ("answer", "truncated", 10, 5, 3)


async def test_openai_content_filter_is_blocked_with_no_text():
    p = OpenAIChatProvider()
    p._client.set(_FakeChat([_completion("partial", "content_filter")]))
    result = await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert result.finish == "blocked" and result.text == ""


async def test_unsupported_parameter_is_dropped_and_retried():
    p = OpenAIChatProvider()
    fake = _FakeChat([_bad_request("Unsupported parameter: 'temperature'"), _completion("ok")])
    p._client.set(fake)
    result = await p.generate(LLMRequest.from_prompt("q"), "custom-deployment")
    assert result.text == "ok" and "temperature" not in fake.params[1]


async def test_other_400s_raise_non_transient_errors():
    p = OpenAIChatProvider()
    p._client.set(_FakeChat([_bad_request("context length exceeded")]))
    with pytest.raises(ProviderError) as err:
        await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert err.value.transient is False


async def test_azure_prompt_filter_400_is_blocked_and_deployment_is_mapped():
    settings.azure_openai_deployments = '{"gpt-4.1": "prod-41"}'
    clear_caches()
    p = AzureOpenAIProvider()
    assert p.deployment("gpt-4.1") == "prod-41" and p.deployment("other") == "other"
    p._client.set(_FakeChat([_bad_request("filtered", code="content_filter")]))
    result = await p.generate(LLMRequest.from_prompt("q"), "gpt-4.1")
    assert result.finish == "blocked"


def test_compat_accounts_use_max_tokens_and_their_own_limits():
    settings.openai_compat_profiles = json.dumps({
        "deepseek": {"base_url": "https://api.deepseek.example", "api_key_env": "NOPE_UNSET_KEY",
                     "max_output_tokens": 8192},
        "local": {"base_url": "http://vllm:8000/v1", "json_mode": False}})
    clear_caches()
    ds = OpenAICompatProvider("deepseek")
    assert "NOPE_UNSET_KEY" in ds.unavailable_reason()
    assert ds.capabilities("deepseek-chat").max_output_tokens == 8192
    local = OpenAICompatProvider("local")
    assert local.unavailable_reason() is None
    params = local.build_params(LLMRequest.from_prompt("q", json_mode=True), "qwen3")
    assert "max_tokens" in params and "response_format" not in params
    assert "JSON" in params["messages"][0]["content"]
    assert OpenAICompatProvider("missing").unavailable_reason()


def test_strip_reasoning_handles_unterminated_blocks():
    assert strip_reasoning("<think>a</think> b") == "b"
    assert strip_reasoning("x <think>never closed") == "x"
    assert strip_reasoning("plain") == "plain"


# -- Bedrock / SageMaker ------------------------------------------------------------------


def test_bedrock_converse_request_shape():
    settings.bedrock_guardrail_id, settings.bedrock_guardrail_version = "gr-1", "2"
    req = LLMRequest(
        messages=[Message("user", [TextPart("q"), DocumentPart(b"%PDF", "application/pdf", "Lease_v2.pdf")])],
        system="sys", max_tokens=4000, temperature=0.1, json_mode=True)
    params = BedrockProvider().build_params(req, "meta.llama3-3-70b-instruct-v1:0")
    assert params["system"][0]["text"].startswith("sys") and "JSON" in params["system"][0]["text"]
    assert params["inferenceConfig"] == {"maxTokens": 4000, "temperature": 0.1}
    doc = params["messages"][0]["content"][1]["document"]
    assert doc["format"] == "pdf" and doc["name"] == "Lease v2 pdf"
    assert params["guardrailConfig"]["guardrailIdentifier"] == "gr-1"
    assert "toolConfig" not in params


def test_bedrock_schema_is_enforced_with_a_forced_tool_on_capable_models():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    req = LLMRequest.from_prompt("q", json_mode=True, json_schema=schema)
    params = BedrockProvider().build_params(req, "us.anthropic.claude-x-v1:0")
    assert params["toolConfig"]["toolChoice"] == {"tool": {"name": "respond"}}
    result = BedrockProvider().parse(
        {"output": {"message": {"content": [{"toolUse": {"input": {"a": "b"}}}]}},
         "stopReason": "tool_use", "usage": {"inputTokens": 7, "outputTokens": 3}},
        "us.anthropic.claude-x-v1:0")
    assert json.loads(result.text) == {"a": "b"} and result.finish == "ok"


@pytest.mark.parametrize("stop,finish", [
    ("end_turn", "ok"), ("max_tokens", "truncated"),
    ("guardrail_intervened", "blocked"), ("content_filtered", "blocked")])
def test_bedrock_stop_reasons(stop, finish):
    result = BedrockProvider().parse(
        {"output": {"message": {"content": [{"reasoningContent": {}}, {"text": "hi"}]}},
         "stopReason": stop, "usage": {}}, "m")
    assert result.finish == finish
    assert result.text == ("" if finish == "blocked" else "hi")


def test_sagemaker_payload_formats():
    settings.sagemaker_endpoints = json.dumps({
        "tgi": {"endpoint": "ep-tgi", "payload_format": "tgi_generate"},
        "vllm": {"endpoint": "ep-vllm", "json_mode": True, "max_output_tokens": 4096}})
    clear_caches()
    sm = SageMakerProvider()
    body = sm.build_body(LLMRequest.from_prompt("q", system="s", temperature=0.0), "tgi")
    assert body["inputs"] == "s\n\nq" and body["parameters"]["do_sample"] is False
    assert "temperature" not in body["parameters"]
    parsed = sm.parse([{"generated_text": "out", "details": {"finish_reason": "length",
                                                             "generated_tokens": 9}}], body, "tgi")
    assert (parsed.text, parsed.finish, parsed.output_tokens) == ("out", "truncated", 9)
    body = sm.build_body(LLMRequest.from_prompt("q", json_mode=True), "vllm")
    assert body["response_format"] == {"type": "json_object"}
    assert sm.capabilities("vllm").max_output_tokens == 4096
    parsed = sm.parse({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
                       "usage": {"prompt_tokens": 2, "completion_tokens": 1}}, body, "vllm")
    assert (parsed.text, parsed.finish, parsed.input_tokens) == ("x", "ok", 2)


# -- Gemini ---------------------------------------------------------------------------------


class _GeminiModels:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _gemini_response(text, finish):
    return NS(
        text=text,
        candidates=[NS(finish_reason=NS(name=finish), content=NS(parts=[NS(text=text)]))],
        usage_metadata=NS(prompt_token_count=4, candidates_token_count=2, thoughts_token_count=3),
    )


@pytest.mark.parametrize("finish,expected", [
    ("STOP", "ok"), ("MAX_TOKENS", "truncated"), ("SAFETY", "blocked")])
async def test_gemini_finish_mapping_and_thinking_tokens(finish, expected):
    p = GeminiProvider()
    models = _GeminiModels(_gemini_response("hello", finish))
    p._client.set(NS(aio=NS(models=models)))
    result = await p.generate(LLMRequest.from_prompt("q", system="s", json_mode=True), "gemini-x")
    assert result.finish == expected
    assert result.output_tokens == 5 and result.thinking_tokens == 3
    assert models.kwargs["contents"] == "q"  # plain prompt goes as a string, as before
    assert models.kwargs["config"].response_mime_type == "application/json"
    assert models.kwargs["config"].max_output_tokens == 16000  # thinking floor kept
