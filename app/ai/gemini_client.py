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
        self.model_name = settings.gemini_model  # gemini-2.5-flash
        self.embedding_model_name = "models/gemini-embedding-001"
        self.model = None
        self.embedding_model = None
        self._initialized = False

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
            result = response.text

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
                        repaired_text = repair_response.text
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

            result = response.text

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
