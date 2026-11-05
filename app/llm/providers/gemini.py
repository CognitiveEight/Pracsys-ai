"""
Google Gemini (AI Studio API key) via the google-genai SDK.

Behaviour carried over unchanged from the pre-portability ``GeminiClient``:
BLOCK_NONE safety (legal documents routinely trip the default filters), a
capped ``thinking_level`` (Gemini 3 bills reasoning against
max_output_tokens), the 16k output floor, and Google Search grounding.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from app.ai.usage import record_embed_tokens, record_grounding_requests
from app.core.config import settings
from app.llm.base import LoopLocal, Provider, missing_module
from app.llm.errors import ProviderError, is_transient_status
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

try:  # Guarded so a missing package surfaces as "not configured", not ImportError.
    from google import genai as genai_ng
    from google.genai import types as gtypes
except Exception:  # pragma: no cover - depends on installed deps
    genai_ng = None  # type: ignore
    gtypes = None  # type: ignore

_BLOCKED_FINISH = ("SAFETY", "BLOCKLIST", "PROHIBITED", "SPII", "RECITATION", "IMAGE_SAFETY")
#: Output floor: thinking tokens are billed against max_output_tokens, so a
#: small caller budget would be eaten by reasoning and truncate the answer.
_OUTPUT_FLOOR = 16000


def minimal_thinking_supported() -> bool:
    """Whether the configured model accepts ``thinking_level=MINIMAL``.

    Gemini 3.8 Flash rejects MINIMAL with a hard error, and the long-form
    continuation rounds ask for MINIMAL — so on that model every truncated long
    answer would fail instead of continuing.
    """
    override = getattr(settings, "gemini_minimal_thinking_supported", None)
    if override is not None:
        return bool(override)
    model = (getattr(settings, "gemini_model", "") or "").lower()
    return not model.startswith("gemini-3.8")


def thinking_level(level: str):
    """Map a string thinking level to the SDK enum, defaulting to LOW.
    MINIMAL degrades to LOW on models that reject it."""
    mapping = {
        "MINIMAL": gtypes.ThinkingLevel.MINIMAL,
        "LOW": gtypes.ThinkingLevel.LOW,
        "MEDIUM": gtypes.ThinkingLevel.MEDIUM,
        "HIGH": gtypes.ThinkingLevel.HIGH,
    }
    key = (level or "LOW").upper()
    if key == "MINIMAL" and not minimal_thinking_supported():
        key = "LOW"
    return mapping.get(key, gtypes.ThinkingLevel.LOW)


def _safety_settings() -> list:
    return [
        gtypes.SafetySetting(category=cat, threshold=gtypes.HarmBlockThreshold.BLOCK_NONE)
        for cat in (
            gtypes.HarmCategory.HARM_CATEGORY_HARASSMENT,
            gtypes.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            gtypes.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            gtypes.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        )
    ]


def safe_extract_text(response: Any) -> str:
    """Text of a Gemini response without ever raising.

    ``response.text`` raises when the candidate has no Part (MAX_TOKENS with
    nothing emitted, RECITATION, SAFETY, …); all such cases become "".
    """
    if response is None:
        return ""
    try:
        return response.text or ""
    except Exception:
        pass
    chunks: List[str] = []
    for cand in list(getattr(response, "candidates", None) or []):
        content = getattr(cand, "content", None)
        for part in list(getattr(content, "parts", None) or []):
            piece = getattr(part, "text", None)
            if piece:
                chunks.append(str(piece))
        if chunks:
            break
    return "".join(chunks)


def finish_reason_name(response: Any) -> str:
    try:
        candidates = list(getattr(response, "candidates", None) or [])
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            if block:
                return f"PROMPT_{getattr(block, 'name', None) or block}"
            return "-"
        fr = getattr(candidates[0], "finish_reason", None)
        return getattr(fr, "name", None) or str(fr or "-")
    except Exception:
        return "-"


def extract_grounding_sources(response: Any) -> List[Dict[str, Any]]:
    """Web sources from a grounded response, deduped by URL. Each is
    ``{"title", "url"[, "supports"]}``; ``supports`` are the answer segments
    that source backs. Empty when the model answered without searching."""
    sources: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        candidates = list(getattr(response, "candidates", None) or [])
        if not candidates:
            return sources
        meta = getattr(candidates[0], "grounding_metadata", None)
        chunks = list(getattr(meta, "grounding_chunks", None) or [])
        supports_by_chunk: Dict[int, List[str]] = {}
        for support in list(getattr(meta, "grounding_supports", None) or []):
            segment = getattr(support, "segment", None)
            seg_text = str(getattr(segment, "text", "") or "").strip()
            if not seg_text:
                continue
            for idx in list(getattr(support, "grounding_chunk_indices", None) or []):
                supports_by_chunk.setdefault(int(idx), []).append(seg_text)
        for idx, chunk in enumerate(chunks):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            url = str(getattr(web, "uri", "") or "").strip()
            title = str(getattr(web, "title", "") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            entry: Dict[str, Any] = {"title": title, "url": url}
            if supports_by_chunk.get(idx):
                entry["supports"] = supports_by_chunk[idx][:5]
            sources.append(entry)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to extract grounding sources: {}", exc)
    return sources


class GeminiProvider(Provider):
    name = "gemini"
    embed_batch_size = 100

    def __init__(self, account: str = "") -> None:
        super().__init__(account)
        # One client serves both the async (``.aio``) and sync surfaces; the
        # google-genai client manages its own per-loop sessions.
        self._client = LoopLocal(lambda: genai_ng.Client(api_key=settings.google_api_key))

    def unavailable_reason(self) -> Optional[str]:
        if genai_ng is None:
            return missing_module("google.genai") or "google-genai failed to import"
        if not settings.google_api_key:
            return "GOOGLE_API_KEY is not set"
        return None

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            json_mode=True,
            json_schema=True,
            images=True,
            documents=True,
            audio=True,
            grounding=True,
            thinking=True,
            max_output_tokens=65536,
        )

    # -- request mapping ------------------------------------------------------
    @staticmethod
    def _contents(request: LLMRequest) -> Any:
        msgs = request.messages
        # A plain prompt goes as a bare string, exactly as before portability.
        if len(msgs) == 1 and all(isinstance(p, TextPart) for p in msgs[0].parts):
            return "\n".join(p.text for p in msgs[0].parts)
        contents = []
        for msg in msgs:
            parts = []
            for part in msg.parts:
                if isinstance(part, TextPart):
                    parts.append(gtypes.Part.from_text(text=part.text))
                elif isinstance(part, (ImagePart, DocumentPart, AudioPart)):
                    parts.append(gtypes.Part.from_bytes(data=part.data, mime_type=part.mime_type))
            contents.append(
                gtypes.Content(role="user" if msg.role == "user" else "model", parts=parts)
            )
        return contents

    def _config(self, request: LLMRequest) -> Any:
        cfg: Dict[str, Any] = dict(
            temperature=request.temperature,
            max_output_tokens=max(int(request.max_tokens or 0), _OUTPUT_FLOOR),
            safety_settings=_safety_settings(),
            thinking_config=gtypes.ThinkingConfig(thinking_level=thinking_level(request.thinking)),
        )
        if request.system:
            cfg["system_instruction"] = request.system
        if request.grounding:
            # The search tool returns prose plus grounding metadata; it cannot
            # be combined with JSON mode.
            cfg["tools"] = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
        elif request.json_mode:
            cfg["response_mime_type"] = "application/json"
            if request.json_schema:
                cfg["response_json_schema"] = request.json_schema
        return gtypes.GenerateContentConfig(**cfg)

    @staticmethod
    def _to_error(exc: Exception) -> ProviderError:
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        transient = is_transient_status(status) or isinstance(
            exc, (ConnectionError, OSError)
        )
        return ProviderError(f"{type(exc).__name__}: {str(exc)[:300]}", transient=transient)

    # -- calls ------------------------------------------------------------------
    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        client = self._client.get()
        contents = self._contents(request)
        config = self._config(request)
        try:
            if request.grounding:
                # The SYNCHRONOUS client in a worker thread, deliberately: the
                # async transport's search-tool path has broken on aiohttp
                # upgrades before (AttributeError before the request is sent).
                response = await asyncio.to_thread(
                    client.models.generate_content, model=model, contents=contents, config=config
                )
            else:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._to_error(exc) from exc

        if request.grounding:
            # Billed PER REQUEST on top of the tokens.
            record_grounding_requests(1)

        raw_finish = finish_reason_name(response)
        text = safe_extract_text(response)
        if raw_finish in ("STOP", "-"):
            finish = "ok"
        elif raw_finish == "MAX_TOKENS":
            finish = "truncated"
        elif any(b in raw_finish for b in _BLOCKED_FINISH) or raw_finish.startswith("PROMPT_"):
            finish = "blocked"
            text = ""
        else:
            finish = "ok" if text else "error"
        usage = getattr(response, "usage_metadata", None)
        out_t = int(getattr(usage, "candidates_token_count", 0) or 0)
        think_t = int(getattr(usage, "thoughts_token_count", 0) or 0)
        return LLMResult(
            text=text,
            finish=finish,  # type: ignore[arg-type]
            provider=self.name,
            model=model,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=out_t + think_t,
            thinking_tokens=think_t,
            raw_finish=raw_finish,
            sources=extract_grounding_sources(response) if request.grounding else [],
        )

    async def embed(
        self, texts: List[str], model: str, task: EmbedTask, dimensions: int
    ) -> List[List[float]]:
        model_id = model[len("models/"):] if model.startswith("models/") else model
        try:
            response = await self._client.get().aio.models.embed_content(
                model=model_id,
                contents=list(texts),
                config=gtypes.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT",
                    output_dimensionality=dimensions,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._to_error(exc) from exc
        # The SDK gives no usage block for embeddings: estimate (~4 chars/token)
        # rather than leave the spend invisible.
        record_embed_tokens(sum(len(t or "") for t in texts) // 4)
        return [list(e.values or []) for e in (response.embeddings or [])]
