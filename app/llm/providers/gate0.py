"""
Gate0 — ViitorCloud's governed AI gateway (a LiteLLM proxy).

Gate0 speaks the OpenAI API, so this adapter is the OpenAI mapping plus what a
governed gateway adds:

- **Attribution.** Every request carries LiteLLM ``metadata`` and ``tags``
  (app, feature, workspace, request id) so gate0 can attribute spend per
  workspace. Ids only — never names or prompt content.
- **Billing.** The cost gate0 computed is read from the ``x-litellm-response-cost``
  header and billed as-is, so SimplerToday and gate0 report the same figure.
- **Log join.** gate0's ``x-litellm-call-id`` is kept on every result (chat and
  embeddings), so each ``llm_call`` line matches one gate0 spend record.
- **Model features come from configuration, not model names.** Any model
  gate0 serves (Gemini, Claude, GPT, Azure deployments, or open-source models
  such as Llama, Qwen, DeepSeek or Mistral on Bedrock, vLLM or Ollama) is
  described in ``LLM_MODEL_OPTIONS[model]``:
  - ``supports_documents`` / ``supports_images`` / ``max_output_tokens``;
  - ``thinking: true`` sends ``reasoning_effort`` (LiteLLM maps it to each
    backend's reasoning control);
  - ``grounding: true`` sends ``web_search_options`` (LiteLLM's provider-neutral
    web search);
  - ``extra_body`` passes backend-specific parameters through unchanged;
  - ``family`` names a vendor whose protocol needs special handling.
  Two families have it. ``gemini`` (also inferred from a name containing
  "gemini", so existing Gemini setups are unchanged) gets the thinking floor,
  Google Search grounding, ``safety_settings`` BLOCK_NONE and the embedding
  ``task_type``. ``anthropic`` drops ``temperature`` with extended thinking and
  skips thinking in JSON mode (LiteLLM forces a tool call for JSON on Claude,
  which thinking does not allow). Everything else is sent as a plain OpenAI
  request.
- **Budgets.** A 429 that is a budget block is final (non-transient): retrying
  a spent budget only adds load. A plain rate limit stays transient.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from app.ai.usage import record_grounding_requests
from app.core.config import settings
from app.llm.base import missing_module
from app.llm.config import model_family, model_options, secret
from app.llm.errors import ProviderError, is_transient_status
from app.llm.providers.openai_compat import OpenAIChatProvider, is_reasoning_model
from app.llm.types import Capabilities, EmbedTask, LLMRequest, LLMResult

_GEMINI_OUTPUT_FLOOR = 16000  # thinking tokens share the output budget
# Claude's thinking budget comes out of max_tokens (LiteLLM maps
# reasoning_effort to a budget of up to 4096), so leave room for the answer.
_ANTHROPIC_THINKING_FLOOR = 8192
_SERVED_MODELS_TIMEOUT = 10.0
_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)


def _log_context() -> Dict[str, Any]:
    """The correlation ids bound by ``logger.contextualize`` for the running
    event (request_id, workspace_id, …). Empty outside an event."""
    try:
        import loguru._logger as _loguru

        return dict(_loguru.context.get() or {})
    except Exception:  # internal API; attribution is best-effort
        return {}


def is_gemini_model(model: str) -> bool:
    return model_family(model) == "gemini"


def _thinking_enabled(model: str) -> bool:
    """Whether a non-Gemini model gets ``reasoning_effort``: the model's
    ``thinking`` option, else the OpenAI reasoning-model rule the base adapter
    already applies."""
    opt = model_options(model).get("thinking")
    return bool(opt) if opt is not None else is_reasoning_model(model)


def _reasoning_level(request: LLMRequest) -> str:
    level = (request.thinking or "LOW").lower()
    # Gemini 3.8 rejects MINIMAL; LOW is the safe floor everywhere.
    return "low" if level == "minimal" else level


class Gate0Provider(OpenAIChatProvider):
    name = "gate0"
    # LiteLLM accepts max_tokens for every backend it routes to.
    max_tokens_param = "max_tokens"

    def unavailable_reason(self) -> Optional[str]:
        if missing_module("openai"):
            return missing_module("openai")
        if not settings.gate0_base_url:
            return "GATE0_BASE_URL is not set"
        if not secret(settings.gate0_api_key):
            return "GATE0_API_KEY is not set"
        return None

    def options(self) -> Dict[str, Any]:
        return {}

    def _make_client(self) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=secret(settings.gate0_api_key),
            base_url=settings.gate0_base_url,
            **self._client_limits(),
        )

    def capabilities(self, model: str) -> Capabilities:
        opts = model_options(model)
        gemini = model_family(model) == "gemini"
        return Capabilities(
            json_mode=True,
            json_schema=True,
            images=True,
            # Anything else declares supports_documents in LLM_MODEL_OPTIONS
            # (the router applies it over this default).
            documents=gemini,
            audio=False,
            grounding=bool(opts.get("grounding", gemini)),
            thinking=gemini or _thinking_enabled(model),
            max_output_tokens=None,  # set per model in LLM_MODEL_OPTIONS
        )

    # -- request ------------------------------------------------------------------
    def _metadata(self, request_tags: Dict[str, str]) -> Dict[str, Any]:
        ctx = _log_context()
        # The case id travels as "case_id": LiteLLM rejects a request whose body
        # or metadata has a "workspace_id" key (it is an Anthropic/Bedrock
        # workspace selector on its client-credential ban list), which failed
        # every case-scoped call with a 401. The tag keeps the
        # "workspace_id:<id>" form that scripts/reconcile_gate0_spend.py reads.
        fields = {
            "app": settings.gate0_app_name,
            "feature": request_tags.get("feature"),
            "kind": request_tags.get("kind"),
            "case_id": ctx.get("workspace_id"),
            "request_id": ctx.get("request_id"),
            "event": ctx.get("event"),
        }
        fields = {k: str(v) for k, v in fields.items() if v not in (None, "", "-")}
        tags = [f"{k}:{fields[k]}" for k in ("app", "feature") if k in fields]
        if "case_id" in fields:
            tags.append(f"workspace_id:{fields['case_id']}")
        return {**fields, "tags": tags}

    def build_params(self, request: LLMRequest, model: str) -> Dict[str, Any]:
        params = super().build_params(request, model)
        extra: Dict[str, Any] = dict(params.pop("extra_body", None) or {})
        opts = model_options(model)
        family = model_family(model)
        cap = opts.get("max_output_tokens")
        if family == "gemini":
            # The Gemini thinking floor (as on direct Gemini), within the
            # model's configured ceiling.
            floor = max(int(request.max_tokens), _GEMINI_OUTPUT_FLOOR)
            params["max_tokens"] = min(floor, int(cap)) if cap else floor
            params["temperature"] = request.temperature
            params["reasoning_effort"] = _reasoning_level(request)
            if settings.gate0_gemini_block_none:
                extra["safety_settings"] = [
                    {"category": c, "threshold": "BLOCK_NONE"} for c in _SAFETY_CATEGORIES
                ]
            if request.grounding:
                # The search tool cannot be combined with JSON mode on Gemini.
                params["web_search_options"] = {}
                params.pop("response_format", None)
        else:
            thinking = _thinking_enabled(model)
            if thinking and family == "anthropic" and request.json_mode:
                # JSON on Claude is a forced tool call, which thinking forbids.
                thinking = False
            if thinking:
                params["reasoning_effort"] = _reasoning_level(request)
                # Reasoning backends (Claude thinking, OpenAI o-series) take
                # only their default temperature.
                params.pop("temperature", None)
                if family == "anthropic":
                    # The thinking budget shares max_tokens with the answer.
                    floor = max(int(request.max_tokens), _ANTHROPIC_THINKING_FLOOR)
                    params["max_tokens"] = min(floor, int(cap)) if cap else floor
            elif "reasoning_effort" in params:
                # thinking: false overrides the reasoning-model name rule.
                params.pop("reasoning_effort")
                params["temperature"] = request.temperature
            if request.grounding and opts.get("grounding"):
                params["web_search_options"] = {}
                params.pop("response_format", None)
        model_extra = opts.get("extra_body")
        if isinstance(model_extra, dict):
            # Backend-specific parameters from configuration (e.g. a vLLM
            # chat_template_kwargs); the adapter's own keys take precedence.
            extra = {**model_extra, **extra}
        if settings.gate0_tag_requests:
            extra["metadata"] = self._metadata(request.tags)
        if extra:
            params["extra_body"] = extra
        return params

    async def _create(self, client: Any, params: Dict[str, Any]) -> Any:
        raw = await client.chat.completions.with_raw_response.create(**params)
        return raw.parse(), raw.headers

    # -- response -------------------------------------------------------------------
    @staticmethod
    def _cost(headers: Any) -> Optional[float]:
        try:
            value = headers.get("x-litellm-response-cost") if headers is not None else None
            cost = float(value) if value not in (None, "", "None") else None
        except (TypeError, ValueError):
            return None
        return cost if cost is not None and cost >= 0 else None

    @staticmethod
    def _call_id(headers: Any) -> Optional[str]:
        try:
            value = headers.get("x-litellm-call-id") if headers is not None else None
        except Exception:
            return None
        value = str(value or "").strip()
        # An opaque gateway id; bounded so a misbehaving proxy cannot flood logs.
        return value[:64] or None

    @classmethod
    def _gateway_fields(cls, headers: Any) -> "tuple[Optional[float], Optional[str]]":
        return cls._cost(headers), cls._call_id(headers)

    @staticmethod
    def _sources(response: Any) -> List[Dict[str, Any]]:
        """Grounding sources from OpenAI-style ``url_citation`` annotations,
        grouped per URL with the answer segments each one supports."""
        try:
            message = response.choices[0].message
        except Exception:
            return []
        annotations = getattr(message, "annotations", None) or []
        text = str(getattr(message, "content", "") or "")
        by_url: Dict[str, Dict[str, Any]] = {}
        for ann in annotations:
            cite = ann.get("url_citation") if isinstance(ann, dict) else getattr(ann, "url_citation", None)
            if cite is None:
                continue
            get = cite.get if isinstance(cite, dict) else (lambda k, c=cite: getattr(c, k, None))
            url = str(get("url") or "").strip()
            if not url:
                continue
            entry = by_url.setdefault(url, {"title": str(get("title") or "").strip(), "url": url})
            start, end = get("start_index"), get("end_index")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                supports = entry.setdefault("supports", [])
                if len(supports) < 5:
                    supports.append(text[start:end].strip())
        return list(by_url.values())

    def _result(self, response: Any, model: str, headers: Any = None) -> LLMResult:
        result = super()._result(response, model, headers)
        result.cost_usd = self._cost(headers)
        result.gateway_call_id = self._call_id(headers)
        result.sources = self._sources(response)
        if result.sources and result.cost_usd is None:
            # No gateway cost: bill the search request locally, as direct Gemini does.
            record_grounding_requests(1)
        return result

    @staticmethod
    def _to_error(exc: Exception) -> ProviderError:
        import openai

        status = getattr(exc, "status_code", None)
        message = str(exc)
        if status == 429 and "budget" in message.lower():
            # Spent budget: final. The caller reports the failure; retrying or
            # spreading the load over the fallback chain would not help.
            logger.warning("gate0 budget exhausted for this key")
            return ProviderError(f"gate0 budget exceeded: {message[:200]}", transient=False)
        transient = is_transient_status(status) or isinstance(
            exc, (openai.APIConnectionError, openai.APITimeoutError)
        )
        return ProviderError(f"{type(exc).__name__}: {message[:300]}", transient=transient)

    # -- embeddings -------------------------------------------------------------------
    def _sends_dimensions(self, model: str) -> bool:
        return bool(model_options(model).get("send_dimensions", True))

    def _embed_extra_body(self, model: str, task: EmbedTask) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if is_gemini_model(model):
            extra["task_type"] = "RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT"
        elif "cohere" in (model or "").lower() or model_options(model).get("cohere_input_type"):
            # Cohere embeds queries and documents differently; LiteLLM's
            # default treats everything as a document (as aws.py does directly).
            extra["input_type"] = "search_query" if task == "query" else "search_document"
        if settings.gate0_tag_requests:
            extra["metadata"] = self._metadata({"feature": "embeddings", "kind": "embed"})
        return extra

    async def _embed_create(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        raw = await client.embeddings.with_raw_response.create(**kwargs)
        return raw.parse(), raw.headers

    # -- usage meter --------------------------------------------------------------------
    async def submit_usage_records(self, records: List[Dict[str, Any]]) -> None:
        """POST service-side usage records (OCR, STT, search) to gate0's meter,
        authenticated with this service's gate0 key. gate0 resolves the team
        and key from that key and dedupes by call id. Raises ``ProviderError``."""
        import httpx

        client = self._client.get()
        try:
            await client.post(
                settings.gate0_meter_path,
                cast_to=httpx.Response,
                body={"records": records},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._to_error(exc) from exc

    # -- governance ---------------------------------------------------------------------
    async def served_models(self) -> List[str]:
        """Model names gate0 serves to this key (``GET /v1/models``).

        Used once at startup to report configured models gate0 cannot serve.
        Raises ``ProviderError`` when the gateway cannot be asked."""
        client = self._client.get()
        try:
            page = await asyncio.wait_for(client.models.list(), timeout=_SERVED_MODELS_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._to_error(exc) from exc
        return sorted({str(getattr(m, "id", "") or "") for m in (getattr(page, "data", None) or [])} - {""})
