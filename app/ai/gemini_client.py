"""
Google Gemini AI client for SimplerToday.
Handles all interactions with Gemini 2.0 Flash model.
"""

import asyncio
import json
import re
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

from app.core.config import settings
from app.observability.trace import tracer


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

            logger.success(
                "✅ Gemini client initialized with model: {}", self.model_name
            )
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

    @staticmethod
    def _log_generation_diagnostics(response: Any, label: str = "") -> None:
        """Surface *why* a generation stopped and how the token budget was spent.

        Without this, a response with finish_reason=MAX_TOKENS (or SAFETY /
        RECITATION) still returns its partial text via _safe_extract_text and
        ships silently — a draft cut off mid-sentence with no trace of the
        cause. This logs the finish_reason and usage_metadata so we can tell a
        genuine token-budget exhaustion (candidates+thoughts ≈ requested cap)
        apart from an early STOP or a content-filter cut. Best-effort only:
        never raises, and field names are read defensively because the
        google-generativeai 0.8.x usage_metadata shape varies by version /
        model (Gemini 3 thinking models add thoughts_token_count)."""
        try:
            candidates = list(getattr(response, "candidates", None) or [])
            fr_repr = "none"
            if candidates:
                fr = getattr(candidates[0], "finish_reason", None)
                fr_repr = getattr(fr, "name", None) or str(fr)
            usage = getattr(response, "usage_metadata", None)
            prompt_t = getattr(usage, "prompt_token_count", None)
            out_t = getattr(usage, "candidates_token_count", None)
            think_t = getattr(usage, "thoughts_token_count", None)
            total_t = getattr(usage, "total_token_count", None)
            # WARNING when the stop reason indicates truncation/blocking, INFO
            # otherwise — so normal STOP completions don't spam prod logs but a
            # truncated draft is loud and greppable.
            truncated = fr_repr not in ("STOP", "1", "none")
            log = logger.warning if truncated else logger.info
            log(
                "Gemini generation diagnostics [{}]: finish_reason={} "
                "prompt_tokens={} output_tokens={} thinking_tokens={} total_tokens={}",
                label or "generate",
                fr_repr,
                prompt_t,
                out_t,
                think_t,
                total_t,
            )
        except Exception as exc:  # diagnostics must never break generation
            logger.debug("Gemini diagnostics logging failed: {}", exc)

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
        logger.debug(
            "Gemini returned no usable text (finish_reason={})", fr_repr
        )
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

    @tracer.trace("gemini_generate")
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 12000,
        **kwargs,
    ) -> str:
        """
        Generate text using Gemini.

        Args:
            prompt: User prompt
            system_instruction: System instruction for model
            temperature: Temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional generation parameters

        Returns:
            Generated text
        """
        if not self._initialized:
            self.initialize()

        if not self.model:
            logger.error("Gemini model not initialized")
            return "Error: Gemini model not available"

        try:
            logger.debug("Generating with Gemini 2.0 Flash...")

            # Configure generation
            generation_config = genai.types.GenerationConfig(
                temperature=temperature, max_output_tokens=max_tokens, **kwargs
            )

            # Build full prompt with system instruction
            if system_instruction:
                full_prompt = f"{system_instruction}\n\n{prompt}"
            else:
                full_prompt = prompt

            # Run sync SDK in a thread so long strategy/analysis calls do not block
            # the asyncio loop (intelligence chat must stay responsive).
            def _sync_generate():
                return self.model.generate_content(
                    full_prompt,
                    generation_config=generation_config,
                    safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
                )

            response = await asyncio.to_thread(_sync_generate)
            self._log_generation_diagnostics(response, label=self.model_name)
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
                            max_output_tokens=max_tokens,
                            response_mime_type="application/json",
                        )

                        def _sync_repair():
                            return self.model.generate_content(
                                repair_prompt,
                                generation_config=repair_config,
                                safety_settings=_PERMISSIVE_SAFETY_SETTINGS,
                            )

                        repair_response = await asyncio.to_thread(_sync_repair)
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

            logger.success("✅ Generated {} characters with Gemini", len(result))
            return result

        except Exception as e:
            logger.error("Gemini generation error: {}", str(e))
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
    def _ng_thinking_level(level: str):
        """Map a string thinking level to the SDK enum, defaulting to LOW.

        LOW keeps the Gemini 3 reasoning pass cheap so the bulk of
        max_output_tokens is spent on the actual draft, not on hidden thoughts."""
        mapping = {
            "MINIMAL": genai_ng_types.ThinkingLevel.MINIMAL,
            "LOW": genai_ng_types.ThinkingLevel.LOW,
            "MEDIUM": genai_ng_types.ThinkingLevel.MEDIUM,
            "HIGH": genai_ng_types.ThinkingLevel.HIGH,
        }
        return mapping.get((level or "LOW").upper(), genai_ng_types.ThinkingLevel.LOW)

    @tracer.trace("gemini_generate_longform")
    async def generate_longform(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.35,
        max_tokens: int = 65536,
        thinking_level: str = "LOW",
    ) -> str:
        """Generate a long-form document (legal draft / translation) via the
        new google-genai SDK with explicit thinking control.

        Why a separate path from generate(): Gemini 3 is a thinking model and
        its reasoning tokens are billed against max_output_tokens. The legacy
        google-generativeai SDK gives us no handle on that budget, so a long
        draft could be starved (or truncated mid-sentence) by an unbounded
        thinking pass. Here the caller chooses thinking_level explicitly
        (drafting uses HIGH for the deepest reasoning), we request the full
        64k output ceiling, and log finish_reason + token usage so a
        thinking-vs-output budget squeeze is visible. NOTE: thinking tokens are
        billed against max_output_tokens, so HIGH thinking reduces the room
        left for the draft — step the level down if long drafts truncate.

        On ANY failure (SDK missing, network, parsing) this falls back to the
        legacy generate() so drafting never hard-fails because of the migration.
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
            config = genai_ng_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=temperature,
                max_output_tokens=max_tokens,
                safety_settings=safety,
                thinking_config=genai_ng_types.ThinkingConfig(
                    thinking_level=self._ng_thinking_level(thinking_level),
                ),
            )
            response = await client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            self._log_generation_diagnostics(
                response, label=f"{self.model_name}/longform"
            )
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
            logger.success(
                "✅ Generated {} characters via google-genai long-form path",
                len(result),
            )
            return result
        except Exception as exc:
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

    @tracer.trace("gemini_chat")
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

            response = await asyncio.to_thread(_sync_chat)

            result = self._safe_extract_text(response)

            logger.success("✅ Chat response: {} characters", len(result))
            return result

        except Exception as e:
            logger.error("Gemini chat error: {}", str(e))
            return f"Error in chat: {str(e)}"

    @tracer.trace("gemini_embed")
    async def embed_text(self, text: str) -> List[float]:
        """
        Generate embeddings for text using Google's embedding model.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (768 dimensions)
        """
        if not self._initialized:
            self.initialize()

        try:

            def _sync_embed():
                return genai.embed_content(
                    model=self.embedding_model_name,
                    content=text,
                    task_type="retrieval_document",
                )

            result = await asyncio.to_thread(_sync_embed)

            embedding = result["embedding"]

            logger.debug("✅ Generated embedding: {} dimensions", len(embedding))
            return embedding

        except Exception as e:
            logger.error("Gemini embedding error: {}", str(e))
            # Return zero vector on error
            return [0.0] * 768

    @tracer.trace("gemini_embed_batch")
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        embeddings = []

        for text in texts:
            embedding = await self.embed_text(text)
            embeddings.append(embedding)

        logger.debug("✅ Generated {} embeddings", len(embeddings))
        return embeddings

    def is_available(self) -> bool:
        """Check if Gemini is available."""
        return self._initialized and self.model is not None


# Global Gemini client instance
gemini_client = GeminiClient()
