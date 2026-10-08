"""
OpenAI Chat Completions, and everything that speaks it.

One adapter family covers:
- ``openai``        — api.openai.com (or OPENAI_BASE_URL for a gateway);
- ``openai_compat`` — any OpenAI-compatible server, one named account each in
  OPENAI_COMPAT_PROFILES: self-hosted vLLM / Ollama / TGI / LM Studio, and the
  hosted DeepSeek, Qwen (DashScope compatible mode), Together, Groq, Fireworks
  APIs. This is how DeepSeek, Qwen, Llama, Mistral etc. are reached;
- ``azure_foundry`` — Azure AI Foundry's OpenAI-compatible ``/openai/v1``
  endpoint (OSS and partner models deployed in Foundry).
Azure OpenAI proper lives in azure_openai.py (deployment routing + api-version).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Dict, List, Optional

from app.ai.usage import record_embed_tokens
from app.core.config import settings
from app.llm.base import LoopLocal, Provider, missing_module
from app.llm.config import compat_accounts, env_secret, secret
from app.llm.errors import ProviderError, is_transient_status, request_timeout_seconds
from app.llm.json_utils import JSON_ONLY_INSTRUCTION, strip_reasoning
from app.llm.types import (
    AudioPart,
    Capabilities,
    DocumentPart,
    EmbedTask,
    ImagePart,
    LLMRequest,
    LLMResult,
    TextPart,
)

_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_AUDIO_FORMATS = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def is_reasoning_model(model: str) -> bool:
    """OpenAI reasoning models reject ``temperature`` and take ``reasoning_effort``."""
    name = (model or "").lower().rsplit("/", 1)[-1]
    return name.startswith(_REASONING_PREFIXES)


class OpenAIChatProvider(Provider):
    """Shared Chat Completions mapping. Subclasses supply the client and options."""

    name = "openai"
    embed_batch_size = 100
    #: Parameter carrying the output budget. Newer OpenAI models reject
    #: ``max_tokens``; most OpenAI-compatible servers only know ``max_tokens``.
    max_tokens_param = "max_completion_tokens"
    #: Whether this endpoint understands ``response_format: json_schema``.
    supports_json_schema = True

    def __init__(self, account: str = "") -> None:
        super().__init__(account)
        self._client = LoopLocal(self._make_client)

    # -- subclass hooks -------------------------------------------------------
    def _make_client(self) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=secret(settings.openai_api_key),
            base_url=settings.openai_base_url or None,
            organization=settings.openai_organization or None,
            **self._client_limits(),
        )

    def unavailable_reason(self) -> Optional[str]:
        if missing_module("openai"):
            return missing_module("openai")
        if not secret(settings.openai_api_key):
            return "OPENAI_API_KEY is not set"
        return None

    def options(self) -> Dict[str, Any]:
        return {}

    def deployment(self, model: str) -> str:
        """The ``model`` value sent on the wire."""
        return model

    @staticmethod
    def _client_limits() -> Dict[str, Any]:
        # The router owns retries and the timeout; the SDK's own would multiply
        # them (2 SDK retries x router retry x fallback chain).
        timeout = request_timeout_seconds()
        return {"max_retries": 0, "timeout": (timeout + 5.0) if timeout else None}

    # -- capabilities ---------------------------------------------------------
    def capabilities(self, model: str) -> Capabilities:
        opts = self.options()
        return Capabilities(
            json_mode=bool(opts.get("json_mode", True)),
            json_schema=self.supports_json_schema and bool(opts.get("json_mode", True)),
            images=bool(opts.get("supports_images", True)),
            documents=bool(opts.get("supports_documents", self.name in ("openai", "azure_openai"))),
            audio=bool(opts.get("supports_audio", "audio" in (model or "").lower())),
            grounding=False,
            thinking=is_reasoning_model(model),
            max_output_tokens=opts.get("max_output_tokens"),
        )

    # -- request mapping ------------------------------------------------------
    @staticmethod
    def _content(parts: List[Any]) -> Any:
        if all(isinstance(p, TextPart) for p in parts):
            # A plain string is the most widely supported shape (many
            # OpenAI-compatible servers reject content arrays for text models).
            return "\n".join(p.text for p in parts)
        out: List[Dict[str, Any]] = []
        for part in parts:
            if isinstance(part, TextPart):
                out.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                out.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{part.mime_type};base64,{_b64(part.data)}"},
                    }
                )
            elif isinstance(part, DocumentPart):
                out.append(
                    {
                        "type": "file",
                        "file": {
                            "filename": part.name,
                            "file_data": f"data:{part.mime_type};base64,{_b64(part.data)}",
                        },
                    }
                )
            elif isinstance(part, AudioPart):
                out.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": _b64(part.data),
                            "format": _AUDIO_FORMATS.get(part.mime_type, "wav"),
                        },
                    }
                )
        return out

    def build_params(self, request: LLMRequest, model: str) -> Dict[str, Any]:
        caps = self.capabilities(model)
        system = request.system or ""
        if request.json_mode:
            # OpenAI requires the word "JSON" in the messages for json_object,
            # and models without a JSON mode need the instruction anyway.
            system = f"{system}\n\n{JSON_ONLY_INSTRUCTION}".strip()
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for msg in request.messages:
            messages.append({"role": msg.role, "content": self._content(msg.parts)})

        params: Dict[str, Any] = {
            "model": self.deployment(model),
            "messages": messages,
            self.max_tokens_param: int(request.max_tokens),
        }
        if is_reasoning_model(model):
            level = (request.thinking or "LOW").lower()
            params["reasoning_effort"] = "low" if level == "minimal" else level
        else:
            params["temperature"] = request.temperature
        if request.json_mode and caps.json_mode:
            if request.json_schema and caps.json_schema:
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": request.json_schema},
                }
            else:
                params["response_format"] = {"type": "json_object"}
        extra_body = self.options().get("extra_body")
        if extra_body:
            params["extra_body"] = dict(extra_body)
        return params

    @staticmethod
    def _to_error(exc: Exception) -> ProviderError:
        import openai

        status = getattr(exc, "status_code", None)
        transient = is_transient_status(status) or isinstance(
            exc, (openai.APIConnectionError, openai.APITimeoutError)
        )
        return ProviderError(f"{type(exc).__name__}: {str(exc)[:300]}", transient=transient)

    @staticmethod
    def _unsupported_param(exc: Exception, params: Dict[str, Any]) -> Optional[str]:
        """The request parameter a 400 rejected, when it is one we can drop or
        rename (reasoning models vs temperature; old servers vs
        max_completion_tokens / response_format)."""
        message = str(exc).lower()
        if "unsupported" not in message and "not support" not in message and "unrecognized" not in message and "extra" not in message:
            return None
        for name in ("temperature", "max_completion_tokens", "response_format", "reasoning_effort"):
            if name in message and name in params:
                return name
        return None

    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        import openai

        client = self._client.get()
        params = self.build_params(request, model)
        response = None
        headers = None
        for _ in range(3):
            try:
                response, headers = await self._create(client, params)
                break
            except asyncio.CancelledError:
                raise
            except openai.BadRequestError as exc:
                if getattr(exc, "code", None) == "content_filter":
                    # Azure rejects a filtered PROMPT with a 400; that is a
                    # block (policy), not a failure to retry or fall back from.
                    return LLMResult(
                        text="", finish="blocked", provider=self.name, model=model,
                        raw_finish="content_filter(prompt)",
                    )
                bad = self._unsupported_param(exc, params)
                if bad is None:
                    raise self._to_error(exc) from exc
                value = params.pop(bad)
                if bad == "max_completion_tokens":
                    params["max_tokens"] = value
            except Exception as exc:
                raise self._to_error(exc) from exc
        if response is None:
            raise ProviderError("request rejected after dropping unsupported parameters")
        return self._result(response, model, headers)

    async def _create(self, client: Any, params: Dict[str, Any]) -> Any:
        """One Chat Completions call: ``(response, response_headers or None)``."""
        return await client.chat.completions.create(**params), None

    def _result(self, response: Any, model: str, headers: Any = None) -> LLMResult:
        choices = list(getattr(response, "choices", None) or [])
        choice = choices[0] if choices else None
        raw_finish = str(getattr(choice, "finish_reason", None) or "-")
        text = strip_reasoning(str(getattr(getattr(choice, "message", None), "content", None) or ""))
        finish = {
            "stop": "ok",
            "tool_calls": "ok",
            "function_call": "ok",
            "length": "truncated",
            "content_filter": "blocked",
        }.get(raw_finish, "ok" if text else "error")
        if finish == "blocked":
            text = ""
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        return LLMResult(
            text=text,
            finish=finish,  # type: ignore[arg-type]
            provider=self.name,
            model=model,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            # completion_tokens already includes reasoning tokens.
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            thinking_tokens=int(getattr(details, "reasoning_tokens", 0) or 0),
            raw_finish=raw_finish,
        )

    # -- embeddings -------------------------------------------------------------
    def _sends_dimensions(self, model: str) -> bool:
        opts = self.options()
        if "send_dimensions" in opts:
            return bool(opts["send_dimensions"])
        return "text-embedding-3" in (model or "")

    def _embed_extra_body(self, model: str, task: EmbedTask) -> Dict[str, Any]:
        return {}

    async def embed(
        self, texts: List[str], model: str, task: EmbedTask, dimensions: int
    ) -> List[List[float]]:
        prefix = self.options().get("query_prefix", settings.embedding_query_prefix or "")
        inputs = [f"{prefix}{t}" if task == "query" and prefix else t for t in texts]
        kwargs: Dict[str, Any] = {
            "model": self.deployment(model),
            "input": inputs,
            "encoding_format": "float",
        }
        if self._sends_dimensions(model):
            kwargs["dimensions"] = dimensions
        extra_body = self._embed_extra_body(model, task)
        if extra_body:
            kwargs["extra_body"] = extra_body
        try:
            response = await self._client.get().embeddings.create(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._to_error(exc) from exc
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        record_embed_tokens(tokens or sum(len(t or "") for t in texts) // 4)
        data = sorted(response.data or [], key=lambda d: getattr(d, "index", 0))
        return [list(d.embedding or []) for d in data]


class OpenAICompatProvider(OpenAIChatProvider):
    """A named OpenAI-compatible account from OPENAI_COMPAT_PROFILES, e.g.::

        {"deepseek": {"base_url": "https://api.deepseek.com",
                      "api_key_env": "DEEPSEEK_API_KEY", "max_output_tokens": 8192},
         "local":    {"base_url": "http://vllm:8000/v1", "json_mode": true}}
    """

    name = "openai_compat"
    max_tokens_param = "max_tokens"
    supports_json_schema = False

    def options(self) -> Dict[str, Any]:
        return dict(compat_accounts().get(self.account) or {})

    def capabilities(self, model: str) -> Capabilities:
        opts = self.options()
        return Capabilities(
            json_mode=bool(opts.get("json_mode", True)),
            json_schema=False,
            images=bool(opts.get("supports_images", False)),
            documents=False,
            audio=bool(opts.get("supports_audio", False)),
            grounding=False,
            thinking=False,
            max_output_tokens=opts.get("max_output_tokens"),
        )

    def unavailable_reason(self) -> Optional[str]:
        if missing_module("openai"):
            return missing_module("openai")
        opts = self.options()
        if not opts:
            return f"OPENAI_COMPAT_PROFILES has no account '{self.account}'"
        if not opts.get("base_url"):
            return f"OPENAI_COMPAT_PROFILES['{self.account}'] has no base_url"
        if opts.get("api_key_env") and not env_secret(opts["api_key_env"]):
            return f"{opts['api_key_env']} (api key for '{self.account}') is not set"
        return None

    def _make_client(self) -> Any:
        from openai import AsyncOpenAI

        opts = self.options()
        return AsyncOpenAI(
            # Local servers (vLLM / Ollama) ignore the key but the SDK needs one.
            api_key=env_secret(opts.get("api_key_env")) or "not-needed",
            base_url=opts.get("base_url"),
            default_headers=dict(opts.get("headers") or {}) or None,
            **self._client_limits(),
        )


class AzureFoundryProvider(OpenAIChatProvider):
    """Azure AI Foundry models through the OpenAI-compatible v1 endpoint
    (``https://<resource>.services.ai.azure.com/openai/v1``). Entra ID is used
    when no API key is configured."""

    name = "azure_foundry"
    max_tokens_param = "max_tokens"

    def unavailable_reason(self) -> Optional[str]:
        if missing_module("openai"):
            return missing_module("openai")
        if not settings.azure_foundry_endpoint:
            return "AZURE_FOUNDRY_ENDPOINT is not set"
        if not secret(settings.azure_foundry_api_key) and missing_module("azure.identity"):
            return "AZURE_FOUNDRY_API_KEY is not set and azure-identity is not installed"
        return None

    def _make_client(self) -> Any:
        from openai import AsyncOpenAI

        api_key: Any = secret(settings.azure_foundry_api_key)
        if not api_key:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            api_key = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
        return AsyncOpenAI(
            api_key=api_key,
            base_url=settings.azure_foundry_endpoint,
            **self._client_limits(),
        )
