"""
The model client every feature calls — now provider-neutral.

The module and singleton keep their historical names (``gemini_client``,
``GeminiClient``) because ~40 modules import them and the offline suites
monkeypatch methods on the instance. Behind the same methods, each call is
routed by :class:`app.llm.router.ModelRouter` to the provider profile
configured for its task (LLM_DEFAULT / LLM_ROUTES / LLM_FALLBACKS): Gemini,
OpenAI, Azure OpenAI, Azure AI Foundry, AWS Bedrock, SageMaker, or any
OpenAI-compatible server (DeepSeek, Qwen, Llama, … on vLLM/Ollama/hosted
APIs). With no LLM_* settings the route is ``gemini:<GEMINI_MODEL>``, i.e.
exactly the pre-portability behaviour. See docs/MODEL_PORTABILITY.md.

Contract kept for callers:
- ``generate*`` never raise (except ``asyncio.CancelledError``); failure is a
  returned sentinel — check it with :func:`is_generation_error`;
- JSON mode (``response_mime_type="application/json"``) returns valid JSON or,
  after two repair rounds, the best effort text;
- a safety/content-filter block returns "" (never routed to another provider).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.ai.usage import current_feature
from app.core.config import settings
from app.llm.errors import (  # noqa: F401  (re-exported for callers)
    GENERATION_ERROR_PREFIXES,
    EmbeddingError,
    is_generation_error,
    request_timeout_seconds,
)
from app.llm.json_utils import coerce_valid_json_text
from app.llm.router import ModelRouter
from app.llm.types import LLMRequest, LLMResult, Message, TextPart

#: Texts per embedding batch handed to the router (which splits further to the
#: provider's own per-request ceiling).
_EMBED_BATCH = 100

#: Modules skipped when naming the feature that made a call.
_INTERNAL_MODULES = ("app.ai.gemini_client", "app.ai.usage", "app.llm")


def _calling_feature() -> str:
    """Label for a model call made outside any ``@meter_usage`` feature: the
    nearest app function up the stack (e.g. ``strategies_from_documents``).

    This is both the ``feature=`` on the ``llm_call`` log line and the task
    key LLM_ROUTES matches, so a route can be written straight from the logs.
    """
    from app.ai.usage import _feature_label

    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("app.") and not module.startswith(_INTERNAL_MODULES):
            return _feature_label(frame.f_code.co_name)
        frame = frame.f_back
    return "-"


def _task_label(task: Optional[str]) -> str:
    return task or current_feature() or _calling_feature()


def _sentinel(result: LLMResult, prefix: str = "Error generating response:") -> str:
    error = result.error or "unknown error"
    if error.startswith("not available"):
        return f"Error: model not available ({error[len('not available: '):]})"
    if result.finish == "timeout":
        return f"{prefix} timed out after {request_timeout_seconds()}s"
    return f"{prefix} {error}"


class ModelClient:
    """Provider-neutral client behind the ``gemini_client`` singleton."""

    def __init__(self) -> None:
        self.router = ModelRouter()
        self._initialized = False
        # The service's event loop, so worker threads (the audio pipeline) can
        # run a generation on it via generate_sync().
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    # -- lifecycle ----------------------------------------------------------------
    def initialize(self) -> None:
        """Validate the routing configuration once.

        Explicitly configured profiles (LLM_DEFAULT / LLM_ROUTES / …) that
        cannot be served fail startup loudly. The implicit default (Gemini with
        no GOOGLE_API_KEY) only warns, as it always has.
        """
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if self._initialized:
            return
        # Validated once; handlers call initialize() before every use, and the
        # live availability check is is_available().
        self._initialized = True
        problems = self.router.problems()
        explicit = any(
            getattr(settings, name, None)
            for name in ("llm_default", "llm_routes", "llm_fallbacks", "llm_grounding_profiles")
        ) or (settings.embedding_provider or "gemini") != "gemini" or bool(
            getattr(settings, "gate0_enforce", False)
        )
        if problems:
            detail = "; ".join(f"{p}: {r}" for p, r in problems)
            if explicit:
                self._initialized = False
                raise RuntimeError(f"Model routing is misconfigured — {detail}")
            logger.warning("Model features disabled — {}", detail)
            return
        logger.info(
            "Model client initialised (default={}, routes={}, fallbacks={}, embeddings={})",
            self.router.primary_profile(),
            len(self.router.routing.routes),
            len(self.router.routing.fallbacks),
            self.router.embedding_profile(),
        )

    def is_available(self) -> bool:
        """Whether the default route has a provider that can serve calls."""
        try:
            return self.router.default_available()
        except Exception as exc:
            logger.warning("Model routing unreadable: {}", exc)
            return False

    @property
    def model_name(self) -> str:
        """Model of the default profile (labels and diagnostics only)."""
        try:
            return self.router.primary_profile().model
        except Exception:
            return settings.gemini_model

    @property
    def model(self) -> Optional[str]:
        """Back-compat truthiness check (``if not gemini_client.model``)."""
        return self.model_name if self.is_available() else None

    # -- generation -----------------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 12000,
        thinking_level: str = "LOW",
        task: Optional[str] = None,
        repair_json: bool = True,
        **kwargs: Any,
    ) -> str:
        """Generate text with the model routed for ``task``.

        Args:
            prompt: User prompt.
            system_instruction: System instruction.
            temperature: Sampling temperature.
            max_tokens: Output budget (clamped to the model's ceiling).
            thinking_level: Reasoning budget MINIMAL/LOW/MEDIUM/HIGH, for the
                models that have one.
            task: Route key; defaults to the calling feature's name.
            repair_json: In JSON mode, run up to two model repair rounds on
                malformed output. False returns the raw text for callers that
                recover truncated JSON themselves (repair only sees a preview).
            **kwargs: ``response_mime_type="application/json"`` for JSON mode,
                ``response_schema`` (a JSON-schema dict) to constrain it.

        Returns:
            The text, or an error sentinel (see :func:`is_generation_error`).
        """
        label = _task_label(task)
        json_mode = kwargs.get("response_mime_type") == "application/json"
        schema = kwargs.get("response_schema")
        request = LLMRequest.from_prompt(
            prompt,
            system=system_instruction,
            temperature=temperature,
            max_tokens=int(max_tokens or 0) or 12000,
            json_mode=json_mode,
            json_schema=schema if isinstance(schema, dict) else None,
            thinking=thinking_level,
        )
        result = await self.router.complete(request, task=label, kind="generate")
        if result.finish in ("error", "timeout"):
            return _sentinel(result)
        text = result.text
        if json_mode and text:
            valid = coerce_valid_json_text(text)
            if valid is not None:
                text = valid
            elif repair_json:
                text = await self._ensure_json(text, label, request.max_tokens)
        return text

    async def _ensure_json(self, text: str, task: str, max_tokens: int) -> str:
        """Malformed ``text`` repaired by the model, or after two failed rounds
        the best-effort original."""
        preview = text[:1800]
        for attempt in range(2):
            repair = LLMRequest.from_prompt(
                "Return ONLY valid JSON with no markdown and no extra text.\n"
                "Repair the JSON below while preserving meaning.\n\n"
                f"{preview}",
                temperature=0.0,
                max_tokens=max_tokens,
                json_mode=True,
            )
            # A repair round is a full billable generation; the router records it.
            result = await self.router.complete(repair, task=task, kind="json_repair")
            valid = coerce_valid_json_text(result.text) if result.text else None
            if valid is not None:
                logger.info("JSON repair succeeded on attempt {}", attempt + 1)
                return valid
        logger.warning("Model JSON remained malformed after repair attempts")
        return text

    async def generate_longform(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.35,
        max_tokens: int = 65536,
        thinking_level: str = "LOW",
        max_continuations: int = 3,
        task: Optional[str] = None,
    ) -> str:
        """Generate a long document (legal draft / translation) that always
        finishes: while a response stops on its output ceiling, re-prompt with
        the text so far and stitch the continuation on, up to
        ``max_continuations`` rounds. Continuations use MINIMAL thinking so
        nearly the whole budget goes to text. An empty first answer falls back
        to :meth:`generate`.
        """
        label = _task_label(task)

        def _request(text: str, level: str) -> LLMRequest:
            return LLMRequest.from_prompt(
                text,
                system=system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=level,
            )

        result = await self.router.complete(_request(prompt, thinking_level), task=label, kind="longform")
        if result.finish == "timeout":
            return _sentinel(result)
        if not result.text:
            logger.warning("Long-form generation returned no text; falling back to generate()")
            return await self.generate(
                prompt,
                system_instruction=system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
                task=label,
            )
        text = result.text
        rounds = 0
        while result.finish == "truncated" and rounds < max_continuations:
            rounds += 1
            logger.warning(
                "Long-form hit its output ceiling; auto-continuation round {}/{}",
                rounds,
                max_continuations,
            )
            continuation = (
                f"{prompt}\n\n"
                "--- DOCUMENT GENERATED SO FAR (do NOT repeat any of it) ---\n"
                f"{text}\n\n"
                "--- INSTRUCTION ---\n"
                "Continue the document from EXACTLY where it stops above, "
                "picking up mid-sentence or mid-word if necessary. Do NOT "
                "repeat, summarise, or restart any text already written. "
                "Do NOT add a preamble. Output ONLY the remaining text, and "
                "carry on until the document is fully complete (including any "
                "signature, verification, or annexure blocks the template "
                "requires)."
            )
            result = await self.router.complete(
                _request(continuation, "MINIMAL"), task=label, kind=f"longform_cont{rounds}"
            )
            if not result.text:
                logger.warning("Continuation round {} returned no text; stopping.", rounds)
                break
            text += result.text
        if result.finish == "truncated":
            logger.warning(
                "Long-form still truncated after {} continuation rounds; returning "
                "best-effort {} chars.",
                max_continuations,
                len(text),
            )
        return text

    async def generate_grounded(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 12000,
        thinking_level: str = "LOW",
        task: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Generate text grounded in live Google Search results.

        Served only by profiles in LLM_GROUNDING_PROFILES (Gemini). Returns
        ``(text, [{"title", "url"[, "supports"]}, ...])``, or ``("", [])`` when
        no grounding profile is available or the call fails — callers fall
        back to their other search sources. JSON mode cannot be combined with
        the search tool, so it is never set here.
        """
        request = LLMRequest.from_prompt(
            prompt,
            system=system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking=thinking_level,
            grounding=True,
        )
        result = await self.router.complete(request, task=_task_label(task), kind="grounded")
        if result.finish in ("error", "timeout"):
            logger.warning("Grounded generation unavailable: {}", result.error)
            return "", []
        logger.debug("Grounded generation returned {} source(s)", len(result.sources))
        return result.text or "", list(result.sources)

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 12000,
        task: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Multi-turn generation over ``[{"role", "content"}, ...]``. System
        messages are folded into the system instruction."""
        system_parts = [system_instruction] if system_instruction else []
        turns: List[Message] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "") or ""
            if role == "system":
                system_parts.append(content)
            else:
                turns.append(Message("assistant" if role == "assistant" else "user", [TextPart(content)]))
        if not turns:
            turns = [Message("user", [TextPart("")])]
        request = LLMRequest(
            messages=turns,
            system="\n\n".join(system_parts) or None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        result = await self.router.complete(request, task=_task_label(task), kind="chat")
        if result.finish in ("error", "timeout"):
            return _sentinel(result, "Error in chat:")
        return result.text

    def generate_sync(self, prompt: str, **kwargs: Any) -> str:
        """Blocking :meth:`generate` for code running in a worker thread (the
        audio pipeline runs under ``asyncio.to_thread``).

        Runs on the service's loop so adapters reuse their clients, and in a
        copy of the calling thread's context so usage is billed to the scope
        that started the work. Never call it on the event-loop thread itself:
        it would block the loop it waits on.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            logger.error("generate_sync called on the event loop thread; use await generate()")
            return "Error generating response: generate_sync called on the event loop thread"
        loop = self._main_loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.generate(prompt, **kwargs), loop)
            timeout = request_timeout_seconds()
            try:
                # Generous: JSON repair rounds can follow the main call.
                return future.result(timeout=(timeout * 4 + 30) if timeout else None)
            except Exception as exc:
                future.cancel()
                return f"Error generating response: {type(exc).__name__}: {exc}"
        return asyncio.run(self.generate(prompt, **kwargs))

    # -- embeddings -------------------------------------------------------------------
    async def embed_text(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        """Embed one text. ``task_type`` is RETRIEVAL_DOCUMENT for stored chunks
        and RETRIEVAL_QUERY for search queries — using the document type for
        queries measurably lowers retrieval quality.

        Raises:
            EmbeddingError: after ``embedding_max_attempts`` failures. Never
                returns a zero vector: a zero vector matches nothing and hides
                the failure.
        """
        vectors = await self.router.embed([text or " "], _embed_task(task_type))
        return vectors[0]

    async def embed_query(self, text: str) -> List[float]:
        """Embed a search query (RETRIEVAL_QUERY task type)."""
        return await self.embed_text(text, task_type="RETRIEVAL_QUERY")

    async def embed_texts(
        self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[List[float]]:
        """Embed many texts, batched. Raises EmbeddingError like embed_text."""
        out: List[List[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = [t or " " for t in texts[i : i + _EMBED_BATCH]]
            out.extend(await self.router.embed(batch, _embed_task(task_type)))
        return out


def _embed_task(task_type: str) -> str:
    return "query" if "QUERY" in (task_type or "").upper() else "document"


#: Historical name, kept for imports and tests.
GeminiClient = ModelClient

# Global model client instance
gemini_client = ModelClient()
