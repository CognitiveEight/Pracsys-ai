"""
Google Gemini AI client for SimplerToday.
Handles all interactions with Gemini 2.0 Flash model.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import google.generativeai as genai
from google.generativeai.types import HarmBlockThreshold, HarmCategory
from loguru import logger

# New unified SDK (google-genai), used ONLY for long-form draft rendering so we
# can control the Gemini 3 thinking budget (thinking_level) — reasoning tokens
# are otherwise billed against max_output_tokens and can starve a long draft.
# Guarded so a missing/old package can never break the legacy generate() path.
try:
    from google import genai as genai_ng
    from google.genai import types as genai_ng_types

    _NG_SDK_AVAILABLE = True
except Exception:  # pragma: no cover - depends on installed deps
    genai_ng = None  # type: ignore
    genai_ng_types = None  # type: ignore
    _NG_SDK_AVAILABLE = False

from app.ai.usage import (
    current_feature,
    record_embed_tokens,
    record_grounding_requests,
    record_usage,
)
from app.core.config import settings


# Permissive safety settings for legal document analysis.
# Legal documents routinely contain content that trips Gemini's default safety
# filters (allegations, threats of legal action, references to violence, medical
# content, etc.). Without relaxing these thresholds, ~40-60% of legal-notice,
# court-order, and consumer-complaint documents return finish_reason=2 (SAFETY).
# We process legal content only — not user-facing generation — so BLOCK_NONE is
# appropriate here.
_PERMISSIVE_SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}


# generate() reports failure by RETURNING one of these strings rather than raising,
# so that a caller expecting prose still gets something printable. The cost is that
# a 429 / 5xx / safety block is indistinguishable from real content unless the
# caller checks — a structured (JSON-mode) caller then just fails to parse it and
# reports "no JSON", hiding the actual cause. Every caller that branches on failure
# must use is_generation_error() so the real reason reaches the log.
GENERATION_ERROR_PREFIXES = (
    "Error generating response:",
    "Error: Gemini model not available",
    "Error generating long-form response:",
)


def is_generation_error(text: Optional[str]) -> bool:
    """True when ``text`` is a generate*() failure sentinel rather than model output."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in GENERATION_ERROR_PREFIXES)


def _request_timeout_seconds() -> Optional[float]:
    """Configured per-request ceiling, or None when disabled (<= 0)."""
    try:
        value = float(
            getattr(settings, "gemini_request_timeout_seconds", 300.0) or 0.0
        )
    except (TypeError, ValueError):
        return 300.0
    return value if value > 0 else None


async def _with_timeout(coro: Any, *, label: str) -> Any:
    """Await ``coro`` under the configured ceiling.

    A stalled generation is the worst failure mode this client has: the long
    feature generations are awaited inline by socket handlers, so an unbounded
    request hangs the whole turn with no output and no error. Raises
    asyncio.TimeoutError so the caller's failure path runs.
    """
    timeout = _request_timeout_seconds()
    if timeout is None:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Gemini request timed out after {:.0f}s ({})", timeout, label)
        raise


#: Texts per embed_content request.
_EMBED_BATCH = 100


class EmbeddingError(RuntimeError):
    """An embedding could not be produced. Callers decide the fallback."""



def _calling_feature() -> str:
    """Label for an LLM call made outside any ``@meter_usage`` feature: the
    nearest app function up the stack (e.g. ``strategies_from_documents``),
    so the ``llm_call`` line never says ``feature=-``."""
    import sys

    from app.ai.usage import _feature_label

    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("app.") and module not in ("app.ai.gemini_client", "app.ai.usage"):
            return _feature_label(frame.f_code.co_name)
        frame = frame.f_back
    return "-"


def _reraise_if_cancelled() -> None:
    """Re-raise a cancellation that arrived disguised as an ordinary error.

    Called first in every ``except Exception`` around a model call. A stop
    (``workspace_stop_request``) or shutdown cancels the task, but the SDK can
    surface that as another exception (google-genai raised ``AttributeError``
    against aiohttp 3.9). Caught as a plain failure, the cancellation was lost:
    the client retried on the legacy SDK and a stopped pipeline kept running.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


class GeminiClient:
    """
    Client for Google Gemini AI services.

    Features:
    - Text generation with Gemini 2.0 Flash
    - Embeddings for vector storage
    - Streaming support
    - Role-based conditioning
    """

    def __init__(self):
        self.model_name = settings.gemini_model  # e.g. gemini-3-flash-preview
        self.embedding_model_name = "models/gemini-embedding-001"
        self.model = None
        self.embedding_model = None
        self._initialized = False
        # Lazily-built google-genai (new SDK) client for long-form drafting.
        self._ng_client = None

    def initialize(self):
        """Initialize Gemini client."""
        if self._initialized:
            return

        try:
            # Configure API key
            if not settings.google_api_key:
                logger.warning("GOOGLE_API_KEY not set - Gemini features disabled")
                return

            genai.configure(api_key=settings.google_api_key)

            # Initialize generation model
            self.model = genai.GenerativeModel(self.model_name)

            logger.info("Gemini client initialised (model={})", self.model_name)
            self._initialized = True

        except Exception as e:
            logger.error("Failed to initialize Gemini client: {}", str(e))
            raise

    @staticmethod
    def _extract_balanced_json_fragment(text: str) -> Optional[str]:
        if not text:
            return None
        start_obj = text.find("{")
        start_arr = text.find("[")
        starts = [x for x in (start_obj, start_arr) if x >= 0]
        if not starts:
            return None
        start = min(starts)
        open_char = text[start]
        close_char = "}" if open_char == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        quote = '"'
        i = start
        while i < len(text):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                i += 1
                continue
            if ch in ('"', "'"):
                in_string = True
                quote = ch
                i += 1
                continue
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1].strip()
            i += 1
        return text[start:].strip()

    def _log_llm_call(
        self,
        response: Any,
        *,
        kind: str,
        started: float,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        """The ONE log line per model call: who called (feature), what
        (kind/model), how it ended (status + finish reason), how long, and the
        token spend. Never logs prompt or output text.

        WARNING when the call failed, was blocked or was truncated; INFO
        otherwise. Request/event/workspace ids come from the bound context.
        """
        try:
            finish = "-"
            prompt_t = out_t = think_t = None
            if response is not None:
                candidates = list(getattr(response, "candidates", None) or [])
                if candidates:
                    fr = getattr(candidates[0], "finish_reason", None)
                    finish = getattr(fr, "name", None) or str(fr)
                usage = getattr(response, "usage_metadata", None)
                prompt_t = getattr(usage, "prompt_token_count", None)
                out_t = getattr(usage, "candidates_token_count", None)
                think_t = getattr(usage, "thoughts_token_count", None)
                if status == "ok" and finish not in ("STOP", "1", "-", "None"):
                    status = "blocked" if "SAFETY" in finish or "BLOCK" in finish else "truncated"
            duration_ms = round((time.perf_counter() - started) * 1000)
            feature = current_feature() or _calling_feature()
            fields = dict(
                llm=True,
                llm_kind=kind,
                llm_feature=feature,
                llm_model=self.model_name,
                llm_status=status,
                llm_finish=finish,
                llm_ms=duration_ms,
                llm_in=prompt_t,
                llm_out=out_t,
                llm_think=think_t,
            )
            log = logger.bind(**fields)
            emit = log.info if status == "ok" else log.warning
            emit(
                "llm_call {} feature={} model={} status={} finish={} ms={} "
                "in={} out={} think={}{}",
                kind,
                feature,
                self.model_name,
                status,
                finish,
                duration_ms,
                prompt_t,
                out_t,
                think_t,
                f" error={error}" if error else "",
            )
        except Exception as exc:  # logging must never break generation
            logger.debug("llm_call logging failed: {}", exc)

    @staticmethod
    def _record_usage(response: Any) -> None:
        """Report this response's token usage to the active capture scope.

        Reads ``usage_metadata`` defensively (field names vary by SDK version /
        model, mirroring ``_log_generation_diagnostics``) and folds thinking
        tokens (``thoughts_token_count``) into output because Gemini bills them
        as output. No-op when no ``capture_usage`` scope is open. Best-effort:
        never raises, so it can be dropped into every generate path.
        """
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage is None:
                return
            prompt_t = int(getattr(usage, "prompt_token_count", 0) or 0)
            out_t = int(getattr(usage, "candidates_token_count", 0) or 0)
            think_t = int(getattr(usage, "thoughts_token_count", 0) or 0)
            record_usage(prompt_t, out_t + think_t)
        except Exception as exc:  # usage tracking must never break generation
            logger.debug("Gemini usage recording failed: {}", exc)

    @staticmethod
    def _safe_extract_text(response: Any) -> str:
        """Extract text from a Gemini response without ever raising.

        `response.text` raises when the candidate has no Part (e.g. when
        `finish_reason` is MAX_TOKENS=2, RECITATION=3, SAFETY=4, BLOCKLIST=5).
        Treat all such "no usable text" cases as an empty string so callers
        can detect them via `bool(result)` instead of trying to parse an
        error message that just happens to start with "Error".
        """
        if response is None:
            return ""
        # Try the fast path; if it raises (no Parts), fall back to candidate scan.
        try:
            text = response.text
            return text or ""
        except Exception:
            pass

        try:
            candidates = list(getattr(response, "candidates", None) or [])
        except Exception:
            candidates = []

        # Inspect the first finished candidate for any inline text Parts.
        chunks: List[str] = []
        finish_reason: Any = None
        for cand in candidates:
            if finish_reason is None:
                finish_reason = getattr(cand, "finish_reason", None)
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) if content is not None else None
            for part in parts or []:
                piece = getattr(part, "text", None)
                if piece:
                    chunks.append(str(piece))
            if chunks:
                break

        if chunks:
            return "".join(chunks)

        # No text recovered. Log finish reason at debug level so prod logs do
        # not get spammed with the same "finish_reason is 2" stack trace, but
        # operators can still inspect what happened when needed.
        try:
            fr_repr = getattr(finish_reason, "name", None) or str(finish_reason)
        except Exception:
            fr_repr = "unknown"
        logger.debug("Gemini returned no usable text (finish_reason={})", fr_repr)
        return ""

    @staticmethod
    def _coerce_valid_json_text(raw: str) -> Optional[str]:
        text = (raw or "").strip()
        if not text:
            return None
        if "```" in text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
            if match:
                text = match.group(1).strip()
            else:
                text = re.sub(r"^```\w*\s*", "", text)
                text = re.sub(r"\s*```\s*$", "", text).strip()
        candidate = GeminiClient._extract_balanced_json_fragment(text) or text
        candidate = re.sub(r",\s*([\]}])", r"\1", candidate)
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return None

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 12000,
        thinking_level: str = "LOW",
        **kwargs,
    ) -> str:
        """
        Generate text using Gemini.

        Args:
            prompt: User prompt
            system_instruction: System instruction for model
            temperature: Temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            thinking_level: Gemini-3 reasoning budget ("MINIMAL"/"LOW"/"MEDIUM"/
                "HIGH"). Defaults to LOW so the hidden reasoning pass stays cheap
                and responses are fast; pass "MINIMAL" for the smallest
                classifier-style calls.
            **kwargs: Additional generation parameters

        Returns:
            Generated text
        """
        if not self._initialized:
            self.initialize()

        if not self.model:
            logger.error("Gemini model not initialized")
            return "Error: Gemini model not available"

        started = time.perf_counter()
        try:
            # gemini-3 is a thinking model: its hidden reasoning tokens are billed
            # against max_output_tokens, and with an uncapped budget it reasons
            # for tens of thousands of tokens on EVERY call (slow). We instead
            # CAP the reasoning effort via thinking_level (new SDK) and keep a
            # generous-but-moderate output budget so the answer always completes
            # without truncation. max_output_tokens is only a ceiling (billed for
            # tokens actually generated), and with thinking capped it no longer
            # drives latency. Callers' own max_tokens is honoured as a floor.
            effective_max_tokens = max(int(max_tokens or 0), 16000)
            is_json_mode = kwargs.get("response_mime_type") == "application/json"

            result = None

            # Primary path: new google-genai SDK with an explicit, low thinking
            # budget so the reasoning pass stays cheap (fast) — same mechanism
            # generate_longform uses. Falls back to the legacy SDK on any error.
            ng_client = self._get_ng_client()
            if ng_client is not None:
                try:
                    ng_safety = [
                        genai_ng_types.SafetySetting(
                            category=cat,
                            threshold=genai_ng_types.HarmBlockThreshold.BLOCK_NONE,
                        )
                        for cat in (
                            genai_ng_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                            genai_ng_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                            genai_ng_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                            genai_ng_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                        )
                    ]
                    ng_cfg_kwargs: Dict[str, Any] = dict(
                        temperature=temperature,
                        max_output_tokens=effective_max_tokens,
                        safety_settings=ng_safety,
                        thinking_config=genai_ng_types.ThinkingConfig(
                            thinking_level=self._ng_thinking_level(thinking_level),
                        ),
                    )
                    if system_instruction:
                        ng_cfg_kwargs["system_instruction"] = system_instruction
                    if is_json_mode:
                        ng_cfg_kwargs["response_mime_type"] = "application/json"
                    ng_response = await _with_timeout(
                        ng_client.aio.models.generate_content(
                            model=self.model_name,
                            contents=prompt,
                            config=genai_ng_types.GenerateContentConfig(**ng_cfg_kwargs),
                        ),
                        label=f"generate/{self.model_name}/ng",
                    )
                    self._log_llm_call(ng_response, kind="generate", started=started)
                    self._record_usage(ng_response)
                    result = self._safe_extract_text(ng_response)
                except asyncio.TimeoutError:
                    # Do NOT fall back to legacy on a timeout: the caller has
                    # already waited the full ceiling, and a second full-length
                    # request would double the stall it was meant to bound.
                    raise
                except Exception as ng_err:
                    _reraise_if_cancelled()
                    self._log_llm_call(
                        None, kind="generate", started=started, status="error",
                        error=f"{type(ng_err).__name__}: {str(ng_err)[:160]}; falling back to legacy SDK",
                    )
                    result = None

            if not result:
                # Legacy fallback (new SDK unavailable / errored). Cannot cap
                # thinking here, so the moderate effective_max_tokens also keeps
                # the reasoning pass bounded.
                generation_config = genai.types.GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=effective_max_tokens,
                    **kwargs,
                )

                # Build full prompt with system instruction
                if system_instruction:
                    full_prompt = f"{system_instruction}\n\n{prompt}"
                else:
                    full_prompt = prompt

                # Run sync SDK in a thread so long strategy/analysis calls do not
                # block the asyncio loop (intelligence chat must stay responsive).
                def _sync_generate():
                    return self.model.generate_content(
                        full_prompt,
                        generation_config=generation_config,
                        safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
                    )

                legacy_started = time.perf_counter()
                response = await _with_timeout(
                    asyncio.to_thread(_sync_generate), label="generate/legacy"
                )
                self._log_llm_call(response, kind="generate_legacy", started=legacy_started)
                self._record_usage(response)
                result = self._safe_extract_text(response)

            # Global guardrail: when caller requests JSON mode, ensure we return valid JSON.
            # If first pass is malformed, ask Gemini to repair the payload only.
            is_json_mode = kwargs.get("response_mime_type") == "application/json"
            if is_json_mode:
                valid = self._coerce_valid_json_text(result)
                if valid is None:
                    original_preview = (result or "")[:1800]
                    for attempt in range(2):
                        repair_prompt = (
                            "Return ONLY valid JSON with no markdown and no extra text.\n"
                            "Repair the JSON below while preserving meaning.\n\n"
                            f"{original_preview}"
                        )

                        repair_config = genai.types.GenerationConfig(
                            temperature=0.0,
                            max_output_tokens=effective_max_tokens,
                            response_mime_type="application/json",
                        )

                        def _sync_repair():
                            return self.model.generate_content(
                                repair_prompt,
                                generation_config=repair_config,
                                safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
                            )

                        repair_started = time.perf_counter()
                        repair_response = await _with_timeout(
                            asyncio.to_thread(_sync_repair),
                            label="generate/json-repair",
                        )
                        self._log_llm_call(
                            repair_response, kind="json_repair", started=repair_started
                        )
                        # A repair round is a full billable generation — record it
                        # like the first pass above, or JSON-mode features
                        # under-report every time their output needs repairing.
                        self._record_usage(repair_response)
                        repaired_text = self._safe_extract_text(repair_response)
                        valid = self._coerce_valid_json_text(repaired_text)
                        if valid is not None:
                            logger.info(
                                "Gemini JSON repair succeeded on attempt {}",
                                attempt + 1,
                            )
                            result = valid
                            break
                    if valid is None:
                        logger.warning(
                            "Gemini JSON remained malformed after repair attempts"
                        )
                else:
                    result = valid

            return result

        except asyncio.TimeoutError:
            # Return the sentinel so is_generation_error() callers report a
            # real cause instead of the misleading "model returned no JSON".
            self._log_llm_call(None, kind="generate", started=started, status="timeout")
            return (
                "Error generating response: timed out after "
                f"{_request_timeout_seconds()}s"
            )
        except Exception as e:
            _reraise_if_cancelled()
            # Returned as a sentinel, not raised — so it must be logged as a
            # failure here, or the call reads as a success everywhere else.
            self._log_llm_call(None, kind="generate", started=started, status="error", error=str(e)[:300])
            return f"Error generating response: {str(e)}"

    def _get_ng_client(self):
        """Lazily build the google-genai (new SDK) async-capable client.

        Returns None when the SDK is unavailable or no API key is set, so the
        caller can transparently fall back to the legacy generate() path."""
        if not _NG_SDK_AVAILABLE:
            return None
        if not settings.google_api_key:
            return None
        if self._ng_client is None:
            try:
                self._ng_client = genai_ng.Client(api_key=settings.google_api_key)
            except Exception as exc:
                logger.warning("Failed to init google-genai client: {}", exc)
                self._ng_client = None
        return self._ng_client

    @staticmethod
    def _minimal_thinking_supported() -> bool:
        """Whether the configured model accepts ``thinking_level=MINIMAL``.

        Gemini 3.8 Flash rejects MINIMAL with a hard error, and the long-form
        continuation rounds ask for MINIMAL — so on that model every truncated
        long answer would fail instead of continuing.
        """
        override = getattr(settings, "gemini_minimal_thinking_supported", None)
        if override is not None:
            return bool(override)
        model = (getattr(settings, "gemini_model", "") or "").lower()
        return not model.startswith("gemini-3.8")

    @staticmethod
    def _ng_thinking_level(level: str):
        """Map a string thinking level to the SDK enum, defaulting to LOW.

        LOW keeps the Gemini 3 reasoning pass cheap so the bulk of
        max_output_tokens is spent on the actual draft, not on hidden thoughts.
        MINIMAL degrades to LOW on models that reject it."""
        mapping = {
            "MINIMAL": genai_ng_types.ThinkingLevel.MINIMAL,
            "LOW": genai_ng_types.ThinkingLevel.LOW,
            "MEDIUM": genai_ng_types.ThinkingLevel.MEDIUM,
            "HIGH": genai_ng_types.ThinkingLevel.HIGH,
        }
        key = (level or "LOW").upper()
        if key == "MINIMAL" and not GeminiClient._minimal_thinking_supported():
            key = "LOW"
        return mapping.get(key, genai_ng_types.ThinkingLevel.LOW)

    @staticmethod
    def _ng_finish_reason(response: Any) -> str:
        """Return the first candidate's finish_reason name (e.g. 'MAX_TOKENS',
        'STOP'), or '' if it cannot be read. Used to detect truncation so the
        long-form path can auto-continue."""
        try:
            candidates = list(getattr(response, "candidates", None) or [])
            if not candidates:
                return ""
            fr = getattr(candidates[0], "finish_reason", None)
            return getattr(fr, "name", None) or str(fr or "")
        except Exception:
            return ""

    async def generate_longform(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.35,
        max_tokens: int = 65536,
        thinking_level: str = "LOW",
        max_continuations: int = 3,
    ) -> str:
        """Generate a long-form document (legal draft / translation) via the
        new google-genai SDK, with explicit thinking control AND automatic
        continuation so the document always finishes.

        Why a separate path from generate(): Gemini 3 is a thinking model and
        its reasoning tokens are billed against max_output_tokens. The legacy
        google-generativeai SDK gives us no handle on that budget, so a long
        draft could be starved (or truncated mid-sentence) by an unbounded
        thinking pass. Two mechanisms keep drafts complete:

          1. thinking_level (drafting uses LOW): keeps the reasoning pass cheap
             so the bulk of the 64k output budget is spent on the draft itself,
             not on hidden thoughts. At MEDIUM/HIGH the thinking pass was eating
             ~60k tokens and the visible draft truncated after only ~2-3k.
          2. Continuation loop: if a response still comes back with
             finish_reason=MAX_TOKENS, we re-prompt with the draft-so-far and
             ask the model to continue from exactly where it stopped, then
             concatenate — up to `max_continuations` rounds. Continuation calls
             use MINIMAL thinking to maximise output per round. This guarantees
             completion for drafts longer than a single 64k response (20+ pages).

        finish_reason + token usage are logged each round so a thinking-vs-output
        squeeze stays visible. On ANY failure (SDK missing, network, parsing)
        this falls back to the legacy generate() so drafting never hard-fails.
        """
        client = self._get_ng_client()
        if client is None:
            logger.info(
                "google-genai unavailable; long-form draft falling back to legacy generate()"
            )
            return await self.generate(
                prompt,
                system_instruction=system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        try:
            safety = [
                genai_ng_types.SafetySetting(
                    category=cat,
                    threshold=genai_ng_types.HarmBlockThreshold.BLOCK_NONE,
                )
                for cat in (
                    genai_ng_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                )
            ]

            def _build_config(level: str):
                return genai_ng_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                    safety_settings=safety,
                    thinking_config=genai_ng_types.ThinkingConfig(
                        thinking_level=self._ng_thinking_level(level),
                    ),
                )

            started = time.perf_counter()
            response = await _with_timeout(
                client.aio.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=_build_config(thinking_level),
                ),
                label=f"generate_longform/{self.model_name}",
            )
            self._log_llm_call(response, kind="longform", started=started)
            self._record_usage(response)
            result = self._safe_extract_text(response)
            if not result:
                # Empty (blocked / truncated-to-nothing) — fall back so the
                # user still gets a draft from the legacy path.
                logger.warning(
                    "Long-form generation returned no text; falling back to legacy generate()"
                )
                return await self.generate(
                    prompt,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

            # Continuation loop: while the model stopped because it ran out of
            # output budget (MAX_TOKENS), ask it to pick up exactly where it
            # left off and stitch the pieces together. MINIMAL thinking on the
            # continuation rounds leaves nearly the whole budget for text.
            rounds = 0
            while (
                self._ng_finish_reason(response) == "MAX_TOKENS"
                and rounds < max_continuations
            ):
                rounds += 1
                logger.warning(
                    "Long-form hit MAX_TOKENS; auto-continuation round {}/{}",
                    rounds,
                    max_continuations,
                )
                continuation_prompt = (
                    f"{prompt}\n\n"
                    "--- DOCUMENT GENERATED SO FAR (do NOT repeat any of it) ---\n"
                    f"{result}\n\n"
                    "--- INSTRUCTION ---\n"
                    "Continue the document from EXACTLY where it stops above, "
                    "picking up mid-sentence or mid-word if necessary. Do NOT "
                    "repeat, summarise, or restart any text already written. "
                    "Do NOT add a preamble. Output ONLY the remaining text, and "
                    "carry on until the document is fully complete (including any "
                    "signature, verification, or annexure blocks the template "
                    "requires)."
                )
                started = time.perf_counter()
                response = await _with_timeout(
                    client.aio.models.generate_content(
                        model=self.model_name,
                        contents=continuation_prompt,
                        config=_build_config("MINIMAL"),
                    ),
                    label=f"generate_longform/{self.model_name}/cont",
                )
                self._log_llm_call(response, kind=f"longform_cont{rounds}", started=started)
                self._record_usage(response)
                piece = self._safe_extract_text(response)
                if not piece:
                    logger.warning(
                        "Continuation round {} returned no text; stopping.", rounds
                    )
                    break
                result += piece

            if self._ng_finish_reason(response) == "MAX_TOKENS":
                logger.warning(
                    "Long-form still truncated after {} continuation rounds; "
                    "returning best-effort {} chars.",
                    max_continuations,
                    len(result),
                )

            return result
        except Exception as exc:
            _reraise_if_cancelled()
            logger.error(
                "Long-form generation failed ({}); falling back to legacy generate()",
                exc,
            )
            return await self.generate(
                prompt,
                system_instruction=system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
            )

    @staticmethod
    def _extract_grounding_sources(response: Any) -> List[Dict[str, str]]:
        """Pull web sources out of a grounded response's grounding metadata.

        Returns a list of ``{"title", "url"}`` dicts (deduped by URL). The
        grounding metadata shape varies across SDK versions and is frequently
        absent (model answered without searching), so every access is guarded
        and an empty list is the safe default.
        """
        sources: List[Dict[str, str]] = []
        seen: set = set()
        try:
            candidates = list(getattr(response, "candidates", None) or [])
            if not candidates:
                return sources
            meta = getattr(candidates[0], "grounding_metadata", None)
            chunks = list(getattr(meta, "grounding_chunks", None) or [])
            # grounding_supports map answer segments to the chunks backing them;
            # keep that, so a claim can be traced to the page that supports it
            # rather than only to "one of these URLs".
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

    async def generate_grounded(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 12000,
        thinking_level: str = "LOW",
    ) -> tuple[str, List[Dict[str, str]]]:
        """Generate text grounded in real-time Google Search results.

        Uses the new google-genai SDK's ``google_search`` tool so the model
        searches the live web and returns both prose AND the source URLs it
        relied on (via ``grounding_metadata``). This is the "latest case law"
        channel for the Case Laws feature.

        Returns ``(text, [{"title", "url"}, ...])``. Grounding is only
        available on the new SDK, so when it is unavailable (or any error
        occurs) this returns ``("", [])`` and callers fall back to other
        search sources. ``response_mime_type=application/json`` is intentionally
        NOT set — the search tool returns prose plus grounding metadata, and the
        two cannot be combined.
        """
        client = self._get_ng_client()
        if client is None:
            logger.info("google-genai unavailable; grounded generation skipped")
            return "", []
        try:
            safety = [
                genai_ng_types.SafetySetting(
                    category=cat,
                    threshold=genai_ng_types.HarmBlockThreshold.BLOCK_NONE,
                )
                for cat in (
                    genai_ng_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    genai_ng_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                )
            ]
            cfg_kwargs: Dict[str, Any] = dict(
                temperature=temperature,
                max_output_tokens=max(int(max_tokens or 0), 16000),
                safety_settings=safety,
                tools=[
                    genai_ng_types.Tool(google_search=genai_ng_types.GoogleSearch())
                ],
                thinking_config=genai_ng_types.ThinkingConfig(
                    thinking_level=self._ng_thinking_level(thinking_level),
                ),
            )
            if system_instruction:
                cfg_kwargs["system_instruction"] = system_instruction
            config = genai_ng_types.GenerateContentConfig(**cfg_kwargs)

            # Use the SYNCHRONOUS client in a worker thread rather than
            # ``client.aio`` deliberately: the google-genai async transport runs
            # on aiohttp, and with the search tool enabled it references
            # ``aiohttp.ClientConnectorDNSError`` (added in aiohttp 3.10+). This
            # service pins aiohttp 3.9.5 (for python-socketio), so the async path
            # raises AttributeError before the request is even sent. The sync
            # client uses httpx and is unaffected; running it via to_thread keeps
            # the event loop responsive.
            def _sync_grounded():
                return client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config,
                )

            started = time.perf_counter()
            response = await _with_timeout(
                asyncio.to_thread(_sync_grounded), label="generate_grounded"
            )
            self._log_llm_call(response, kind="grounded", started=started)
            self._record_usage(response)
            # Grounding with Google Search is billed PER REQUEST on top of the
            # tokens recorded above, so both are counted here.
            record_grounding_requests(1)
            text = self._safe_extract_text(response) or ""
            sources = self._extract_grounding_sources(response)
            logger.debug("Grounded generation returned {} source(s)", len(sources))
            return text, sources
        except Exception as exc:
            _reraise_if_cancelled()
            logger.warning("Grounded generation failed: {}", exc)
            return "", []

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 12000,
        **kwargs,
    ) -> str:
        """
        Chat with Gemini using conversation history.

        Args:
            messages: List of message dicts with 'role' and 'content'
            system_instruction: System instruction
            temperature: Temperature
            max_tokens: Max tokens
            **kwargs: Additional parameters

        Returns:
            Generated response
        """
        if not self._initialized:
            self.initialize()

        if not self.model:
            logger.error("Gemini model not initialized")
            return "Error: Gemini model not available"

        try:
            generation_config = genai.types.GenerationConfig(
                temperature=temperature, max_output_tokens=max_tokens, **kwargs
            )

            conversation_text = ""

            if system_instruction:
                conversation_text += f"System: {system_instruction}\n\n"

            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")

                if role == "system":
                    conversation_text += f"System: {content}\n\n"
                elif role == "user":
                    conversation_text += f"User: {content}\n\n"
                elif role == "assistant":
                    conversation_text += f"Assistant: {content}\n\n"

            conversation_text += "Assistant:"

            def _sync_chat():
                chat = self.model.start_chat(history=[])
                return chat.send_message(
                    conversation_text,
                    generation_config=generation_config,
                    safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
                )

            started = time.perf_counter()
            response = await asyncio.to_thread(_sync_chat)
            self._log_llm_call(response, kind="chat", started=started)
            self._record_usage(response)
            return self._safe_extract_text(response)

        except Exception as e:
            _reraise_if_cancelled()
            self._log_llm_call(None, kind="chat", started=started if "started" in locals() else time.perf_counter(), status="error", error=str(e)[:300])
            return f"Error in chat: {str(e)}"

    @staticmethod
    def _l2_normalise(vector: List[float]) -> List[float]:
        """Unit-length copy. A reduced-dimension embedding is not unit length."""
        norm = sum(v * v for v in vector) ** 0.5
        if not norm:
            return list(vector)
        return [v / norm for v in vector]

    def _embedding_model_id(self) -> str:
        name = (getattr(settings, "embedding_model", "") or "gemini-embedding-001").strip()
        return name[len("models/"):] if name.startswith("models/") else name

    async def _embed_batch_once(
        self, texts: List[str], task_type: str
    ) -> List[List[float]]:
        """One embedding request for up to ``_EMBED_BATCH`` texts. Raises on failure."""
        dims = int(getattr(settings, "embedding_dimensions", 768) or 768)
        started = time.perf_counter()
        ng_client = self._get_ng_client()
        if ng_client is not None:
            response = await _with_timeout(
                ng_client.aio.models.embed_content(
                    model=self._embedding_model_id(),
                    contents=list(texts),
                    config=genai_ng_types.EmbedContentConfig(
                        task_type=task_type, output_dimensionality=dims
                    ),
                ),
                label="embed_content",
            )
            vectors = [list(e.values or []) for e in (response.embeddings or [])]
        else:
            # Legacy SDK fallback: one text per call.
            def _sync_embed(text: str):
                return genai.embed_content(
                    model=f"models/{self._embedding_model_id()}",
                    content=text,
                    task_type=task_type.lower(),
                    output_dimensionality=dims,
                )

            vectors = []
            for text in texts:
                result = await asyncio.to_thread(_sync_embed, text)
                vectors.append(list(result["embedding"]))
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for v in vectors:
            if len(v) != dims:
                raise EmbeddingError(
                    f"embedding has {len(v)} dimensions, expected {dims} — the "
                    f"model does not honour output_dimensionality"
                )
            if not any(v):
                raise EmbeddingError("embedding came back all zeros")
        # Billed per input token; the SDK gives no usage block, so estimate
        # (~4 chars per token) rather than leave the spend invisible.
        record_embed_tokens(sum(len(t or "") for t in texts) // 4)
        logger.debug(
            "embed_call texts={} task={} dims={} ms={}",
            len(texts), task_type, dims, round((time.perf_counter() - started) * 1000),
        )
        return [self._l2_normalise(v) for v in vectors]

    async def _embed_with_retry(
        self, texts: List[str], task_type: str
    ) -> List[List[float]]:
        attempts = max(1, int(getattr(settings, "embedding_max_attempts", 3) or 3))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._embed_batch_once(texts, task_type)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _reraise_if_cancelled()
                last_error = exc
                logger.warning(
                    "Embedding attempt {}/{} failed ({} text(s), {}): {}",
                    attempt,
                    attempts,
                    len(texts),
                    task_type,
                    exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        raise EmbeddingError(f"embedding failed after {attempts} attempts: {last_error}")

    async def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[float]:
        """
        Embed one text. ``task_type`` is RETRIEVAL_DOCUMENT for stored chunks and
        RETRIEVAL_QUERY for search queries — using the document type for queries
        measurably lowers retrieval quality.

        Raises:
            EmbeddingError: after ``embedding_max_attempts`` failures. Never
                returns a zero vector: a zero vector matches nothing and hides
                the failure.
        """
        if not self._initialized:
            self.initialize()
        vectors = await self._embed_with_retry([text or " "], task_type)
        return vectors[0]

    async def embed_query(self, text: str) -> List[float]:
        """Embed a search query (RETRIEVAL_QUERY task type)."""
        return await self.embed_text(text, task_type="RETRIEVAL_QUERY")

    async def embed_texts(
        self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> List[List[float]]:
        """Embed many texts, batched. Raises EmbeddingError like embed_text."""
        if not self._initialized:
            self.initialize()
        out: List[List[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = [t or " " for t in texts[i : i + _EMBED_BATCH]]
            out.extend(await self._embed_with_retry(batch, task_type))
        return out

    def is_available(self) -> bool:
        """Check if Gemini is available."""
        return self._initialized and self.model is not None


# Global Gemini client instance
gemini_client = GeminiClient()
