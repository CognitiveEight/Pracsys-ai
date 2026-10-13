"""
Gate0 — ViitorCloud's governed AI gateway (a LiteLLM proxy).

Gate0 speaks the OpenAI API, so this adapter is the OpenAI mapping plus what a
governed gateway adds:

- **Attribution.** Every request carries LiteLLM ``metadata`` and ``tags``
  (app, feature, workspace, request id) so gate0 can attribute spend per
  workspace. Ids only — never names or prompt content.
- **Billing.** The cost gate0 computed is read from the ``x-litellm-response-cost``
  header and billed as-is, so SimplerToday and gate0 report the same figure.
- **Gemini features through the gateway.** For Gemini-backed models (name
  contains "gemini", or ``LLM_MODEL_OPTIONS[model].gemini = true`` for a task
  class such as ``gate0/lite``), LiteLLM translates the OpenAI-style
  parameters:
  - ``reasoning_effort`` → thinking level;
  - ``web_search_options`` → Google Search grounding, whose sources come back
    as ``url_citation`` annotations;
  - ``safety_settings`` BLOCK_NONE, passed through;
  - embedding ``task_type``.
- **Budgets.** A 429 that is a budget block is final (non-transient): retrying
  a spent budget only adds load. A plain rate limit stays transient.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from app.ai.usage import record_grounding_requests
from app.core.config import settings
from app.llm.base import missing_module
from app.llm.config import model_options, secret
from app.llm.errors import ProviderError, is_transient_status
from app.llm.providers.openai_compat import OpenAIChatProvider
from app.llm.types import Capabilities, EmbedTask, LLMRequest, LLMResult

_GEMINI_OUTPUT_FLOOR = 16000  # thinking tokens share the output budget
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
    opt = model_options(model).get("gemini")
    if opt is not None:
        return bool(opt)
    return "gemini" in (model or "").lower()


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
        gemini = is_gemini_model(model)
        return Capabilities(
            json_mode=True,
            json_schema=True,
            images=True,
            documents=gemini,
            audio=False,
            grounding=gemini,
            thinking=gemini,
            max_output_tokens=None,  # set per model in LLM_MODEL_OPTIONS
        )

    # -- request ------------------------------------------------------------------
    def _metadata(self, request_tags: Dict[str, str]) -> Dict[str, Any]:
        ctx = _log_context()
        fields = {
            "app": settings.gate0_app_name,
            "feature": request_tags.get("feature"),
            "kind": request_tags.get("kind"),
            "workspace_id": ctx.get("workspace_id"),
            "request_id": ctx.get("request_id"),
            "event": ctx.get("event"),
        }
        fields = {k: str(v) for k, v in fields.items() if v not in (None, "", "-")}
        tags = [f"{k}:{fields[k]}" for k in ("app", "feature", "workspace_id") if k in fields]
        return {**fields, "tags": tags}

    def build_params(self, request: LLMRequest, model: str) -> Dict[str, Any]:
        params = super().build_params(request, model)
        extra: Dict[str, Any] = dict(params.pop("extra_body", None) or {})
        if is_gemini_model(model):
            # The Gemini thinking floor (as on direct Gemini), within the
            # model's configured ceiling.
            floor = max(int(request.max_tokens), _GEMINI_OUTPUT_FLOOR)
            cap = model_options(model).get("max_output_tokens")
            params["max_tokens"] = min(floor, int(cap)) if cap else floor
            params["temperature"] = request.temperature
            level = (request.thinking or "LOW").lower()
            # Gemini 3.8 rejects MINIMAL; LOW is the safe floor everywhere.
            params["reasoning_effort"] = "low" if level == "minimal" else level
            if settings.gate0_gemini_block_none:
                extra["safety_settings"] = [
                    {"category": c, "threshold": "BLOCK_NONE"} for c in _SAFETY_CATEGORIES
                ]
            if request.grounding:
                # The search tool cannot be combined with JSON mode on Gemini.
                params["web_search_options"] = {}
                params.pop("response_format", None)
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
            return float(value) if value not in (None, "", "None") else None
        except (TypeError, ValueError):
            return None

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
        if settings.gate0_tag_requests:
            extra["metadata"] = self._metadata({"feature": "embeddings", "kind": "embed"})
        return extra
