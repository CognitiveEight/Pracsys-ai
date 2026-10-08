"""
AWS: Bedrock (Converse API) and SageMaker real-time endpoints, via aioboto3.

Credentials come ONLY from the standard AWS chain — environment, shared
profile, or the instance/task/pod role — never from this service's settings.
Grant the role ``bedrock:InvokeModel`` (Converse uses it) on the specific
model ARNs, and ``sagemaker:InvokeEndpoint`` on the specific endpoints.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from app.ai.usage import record_embed_tokens
from app.core.config import settings
from app.llm.base import Provider, missing_module, text_of
from app.llm.config import sagemaker_endpoints
from app.llm.errors import ProviderError, is_transient_status
from app.llm.json_utils import JSON_ONLY_INSTRUCTION, strip_reasoning
from app.llm.types import (
    Capabilities,
    DocumentPart,
    EmbedTask,
    ImagePart,
    LLMRequest,
    LLMResult,
    TextPart,
)

_TRANSIENT_AWS_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "TooManyRequestsException",
    "InternalFailure",
    "ServiceUnavailable",
}
_IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
_DOC_FORMATS = {
    "application/pdf": "pdf",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/html": "html",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

_session = None


def _aws_session():
    global _session
    if _session is None:
        import aioboto3

        _session = aioboto3.Session()
    return _session


def _to_error(exc: Exception) -> ProviderError:
    code = ""
    status = None
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    transient = (
        code in _TRANSIENT_AWS_CODES
        or is_transient_status(status)
        or type(exc).__name__ in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError")
    )
    label = code or type(exc).__name__
    return ProviderError(f"{label}: {str(exc)[:300]}", transient=transient)


class BedrockProvider(Provider):
    """Any Bedrock chat model through the model-agnostic Converse API: Anthropic
    Claude, Amazon Nova, Meta Llama, Mistral, DeepSeek, Qwen, … Use the model id
    or a cross-region inference profile id (``us.…`` / ``global.…``) as the
    profile's model."""

    name = "bedrock"
    embed_batch_size = 96  # Cohere's per-request ceiling; Titan is per-text anyway.

    def _region(self) -> Optional[str]:
        return settings.bedrock_region or settings.aws_region or None

    def unavailable_reason(self) -> Optional[str]:
        return missing_module("aioboto3")

    def capabilities(self, model: str) -> Capabilities:
        m = (model or "").lower()
        vision = any(
            k in m
            for k in ("anthropic.", "amazon.nova-pro", "amazon.nova-lite", "amazon.nova-premier",
                      "llama3-2-11b", "llama3-2-90b", "llama4", "pixtral")
        )
        return Capabilities(
            json_mode=True,  # prompt-instructed; repaired by the router
            json_schema=self._tool_json(m),
            images=vision,
            documents=vision,
            audio=False,
            grounding=False,
            thinking=False,
        )

    @staticmethod
    def _tool_json(model_lower: str) -> bool:
        """Models whose Converse tool use supports forcing one specific tool,
        which is how a JSON schema is enforced on Bedrock."""
        return any(k in model_lower for k in ("anthropic.", "amazon.nova", "mistral-large"))

    @staticmethod
    def _doc_name(name: str) -> str:
        # Converse allows letters, digits, single spaces, hyphens, parens, brackets.
        cleaned = re.sub(r"[^A-Za-z0-9\-\(\)\[\] ]+", " ", name or "document")
        return re.sub(r"\s+", " ", cleaned).strip()[:200] or "document"

    def build_params(self, request: LLMRequest, model: str) -> Dict[str, Any]:
        messages = []
        for msg in request.messages:
            blocks: List[Dict[str, Any]] = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    blocks.append({"text": part.text})
                elif isinstance(part, ImagePart):
                    blocks.append({"image": {"format": _IMAGE_FORMATS.get(part.mime_type, "png"),
                                             "source": {"bytes": part.data}}})
                elif isinstance(part, DocumentPart):
                    blocks.append({"document": {"format": _DOC_FORMATS.get(part.mime_type, "txt"),
                                                "name": self._doc_name(part.name),
                                                "source": {"bytes": part.data}}})
            messages.append({"role": msg.role, "content": blocks})

        use_tool = bool(
            request.json_mode
            and request.json_schema
            and request.json_schema.get("type") == "object"
            and self._tool_json(model.lower())
        )
        system = request.system or ""
        if request.json_mode and not use_tool:
            system = f"{system}\n\n{JSON_ONLY_INSTRUCTION}".strip()
        params: Dict[str, Any] = {
            "modelId": model,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": int(request.max_tokens),
                "temperature": float(request.temperature),
            },
        }
        if system:
            params["system"] = [{"text": system}]
        if use_tool:
            params["toolConfig"] = {
                "tools": [{"toolSpec": {
                    "name": "respond",
                    "description": "Return the answer as JSON matching the schema.",
                    "inputSchema": {"json": request.json_schema},
                }}],
                "toolChoice": {"tool": {"name": "respond"}},
            }
        if settings.bedrock_guardrail_id and settings.bedrock_guardrail_version:
            params["guardrailConfig"] = {
                "guardrailIdentifier": settings.bedrock_guardrail_id,
                "guardrailVersion": settings.bedrock_guardrail_version,
            }
        return params

    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        params = self.build_params(request, model)
        try:
            async with _aws_session().client("bedrock-runtime", region_name=self._region()) as client:
                response = await client.converse(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _to_error(exc) from exc
        return self.parse(response, model)

    def parse(self, response: Dict[str, Any], model: str) -> LLMResult:
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        texts: List[str] = []
        for block in content:
            if "toolUse" in block:
                texts = [json.dumps((block["toolUse"] or {}).get("input") or {})]
                break
            if block.get("text"):
                texts.append(block["text"])  # reasoningContent blocks are skipped
        raw_finish = str(response.get("stopReason") or "-")
        finish = {
            "end_turn": "ok",
            "stop_sequence": "ok",
            "tool_use": "ok",
            "max_tokens": "truncated",
            "model_context_window_exceeded": "truncated",
            "guardrail_intervened": "blocked",
            "content_filtered": "blocked",
        }.get(raw_finish, "ok")
        text = "" if finish == "blocked" else strip_reasoning("".join(texts))
        usage = response.get("usage") or {}
        return LLMResult(
            text=text,
            finish=finish,  # type: ignore[arg-type]
            provider=self.name,
            model=model,
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
            raw_finish=raw_finish,
        )

    async def embed(
        self, texts: List[str], model: str, task: EmbedTask, dimensions: int
    ) -> List[List[float]]:
        m = model.lower()
        vectors: List[List[float]] = []
        tokens = 0
        try:
            async with _aws_session().client("bedrock-runtime", region_name=self._region()) as client:
                if "cohere.embed" in m:
                    body: Dict[str, Any] = {
                        "texts": list(texts),
                        "input_type": "search_query" if task == "query" else "search_document",
                        "embedding_types": ["float"],
                    }
                    if "v4" in m:
                        body["output_dimension"] = dimensions
                    resp = await client.invoke_model(modelId=model, body=json.dumps(body))
                    payload = json.loads(await resp["body"].read())
                    emb = payload.get("embeddings")
                    vectors = list(emb.get("float") if isinstance(emb, dict) else emb or [])
                elif "titan-embed" in m:
                    for text in texts:
                        body = {"inputText": text, "normalize": True}
                        if "v2" in m:
                            body["dimensions"] = dimensions
                        resp = await client.invoke_model(modelId=model, body=json.dumps(body))
                        payload = json.loads(await resp["body"].read())
                        vectors.append(list(payload.get("embedding") or []))
                        tokens += int(payload.get("inputTextTokenCount") or 0)
                else:
                    raise ProviderError(
                        f"unsupported Bedrock embedding model {model!r} "
                        "(expected amazon.titan-embed-* or cohere.embed-*)"
                    )
        except (asyncio.CancelledError, ProviderError):
            raise
        except Exception as exc:
            raise _to_error(exc) from exc
        record_embed_tokens(tokens or sum(len(t or "") for t in texts) // 4)
        return vectors


class SageMakerProvider(Provider):
    """A model on a SageMaker real-time endpoint. The profile's model is a name
    in SAGEMAKER_ENDPOINTS::

        {"qwen-72b": {"endpoint": "qwen25-72b-prod", "payload_format": "openai_messages",
                      "max_output_tokens": 4096, "json_mode": false},
         "llama-tgi": {"endpoint": "llama-tgi", "payload_format": "tgi_generate"}}

    ``openai_messages`` — LMI / vLLM / TGI containers serving the OpenAI
    Messages API (``{"messages": [...]}`` in, chat.completion out).
    ``tgi_generate``  — the classic TGI ``{"inputs", "parameters"}`` contract.
    Bespoke containers need their own format added here.
    """

    name = "sagemaker"

    def _endpoint(self, model: str) -> Dict[str, Any]:
        cfg = sagemaker_endpoints().get(model)
        if cfg is None:
            return {"endpoint": model, "payload_format": "openai_messages"}
        return dict(cfg)

    def unavailable_reason(self) -> Optional[str]:
        return missing_module("aioboto3")

    def capabilities(self, model: str) -> Capabilities:
        cfg = self._endpoint(model)
        return Capabilities(
            json_mode=True,
            images=False,
            documents=False,
            audio=False,
            grounding=False,
            max_output_tokens=cfg.get("max_output_tokens"),
        )

    def build_body(self, request: LLMRequest, model: str) -> Dict[str, Any]:
        cfg = self._endpoint(model)
        system = request.system or ""
        if request.json_mode:
            system = f"{system}\n\n{JSON_ONLY_INSTRUCTION}".strip()
        if cfg.get("payload_format", "openai_messages") == "tgi_generate":
            prompt = text_of(request)
            params: Dict[str, Any] = {
                "max_new_tokens": int(request.max_tokens),
                "return_full_text": False,
                "details": True,
            }
            # TGI requires temperature > 0; 0 means greedy decoding.
            if request.temperature and request.temperature > 0:
                params.update(temperature=float(request.temperature), do_sample=True)
            else:
                params["do_sample"] = False
            return {"inputs": f"{system}\n\n{prompt}" if system else prompt, "parameters": params}
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for msg in request.messages:
            messages.append({
                "role": msg.role,
                "content": "\n".join(p.text for p in msg.parts if isinstance(p, TextPart)),
            })
        body: Dict[str, Any] = {
            "messages": messages,
            "max_tokens": int(request.max_tokens),
            "temperature": float(request.temperature),
        }
        if cfg.get("model"):
            body["model"] = cfg["model"]
        if request.json_mode and cfg.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
        return body

    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        cfg = self._endpoint(model)
        body = self.build_body(request, model)
        try:
            async with _aws_session().client(
                "sagemaker-runtime", region_name=cfg.get("region") or settings.aws_region or None
            ) as client:
                resp = await client.invoke_endpoint(
                    EndpointName=cfg["endpoint"],
                    ContentType="application/json",
                    Accept="application/json",
                    Body=json.dumps(body),
                )
                payload = json.loads(await resp["Body"].read())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _to_error(exc) from exc
        return self.parse(payload, body, model)

    def parse(self, payload: Any, body: Dict[str, Any], model: str) -> LLMResult:
        if isinstance(payload, dict) and "choices" in payload:
            choice = (payload.get("choices") or [{}])[0]
            raw_finish = str(choice.get("finish_reason") or "-")
            text = strip_reasoning(str((choice.get("message") or {}).get("content") or ""))
            usage = payload.get("usage") or {}
            in_t = int(usage.get("prompt_tokens") or 0)
            out_t = int(usage.get("completion_tokens") or 0)
        else:
            item = payload[0] if isinstance(payload, list) and payload else payload or {}
            details = item.get("details") or {}
            raw_finish = str(details.get("finish_reason") or "-")
            text = strip_reasoning(str(item.get("generated_text") or ""))
            # TGI reports generated tokens only; estimate the prompt (~4 chars/token).
            in_t = len(str(body.get("inputs") or "")) // 4
            out_t = int(details.get("generated_tokens") or 0)
        finish = {"length": "truncated", "content_filter": "blocked"}.get(
            raw_finish, "ok" if text else "error"
        )
        return LLMResult(
            text="" if finish == "blocked" else text,
            finish=finish,  # type: ignore[arg-type]
            provider=self.name,
            model=model,
            input_tokens=in_t,
            output_tokens=out_t,
            raw_finish=raw_finish,
        )
