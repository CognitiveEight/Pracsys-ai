"""
Provider-neutral request / response shapes for the model layer.

Callers never build these directly — ``gemini_client`` (the facade every
feature already imports) translates its keyword arguments into an
:class:`LLMRequest`, and each adapter in ``app/llm/providers/`` translates that
into its own SDK's request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

Finish = Literal["ok", "truncated", "blocked", "error", "timeout"]
EmbedTask = Literal["query", "document"]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    mime_type: str  # image/png, image/jpeg, image/webp, image/gif


@dataclass(frozen=True)
class DocumentPart:
    data: bytes
    mime_type: str  # application/pdf, text/plain, ...
    name: str = "document"


@dataclass(frozen=True)
class AudioPart:
    data: bytes
    mime_type: str  # audio/wav, audio/mp3, ...


Part = Union[TextPart, ImagePart, DocumentPart, AudioPart]


@dataclass
class Message:
    role: Literal["user", "assistant"]
    parts: List[Part]


@dataclass
class LLMRequest:
    """One generation request.

    ``messages`` is the conversation; a plain prompt is one user message with
    one :class:`TextPart`. ``thinking`` is the Gemini-style reasoning budget
    (MINIMAL/LOW/MEDIUM/HIGH); adapters map it to their own knob or ignore it.
    """

    messages: List[Message]
    system: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 12000
    json_mode: bool = False
    json_schema: Optional[Dict[str, Any]] = None
    thinking: str = "LOW"
    grounding: bool = False
    #: Attribution set by the router (feature / call kind), for gateways that
    #: track spend per tag. Never prompt content.
    tags: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_prompt(cls, prompt: str, **kwargs: Any) -> "LLMRequest":
        return cls(messages=[Message("user", [TextPart(prompt or "")])], **kwargs)

    def needs(self) -> set:
        """Capabilities this request requires beyond plain text."""
        req = set()
        for msg in self.messages:
            for part in msg.parts:
                if isinstance(part, ImagePart):
                    req.add("images")
                elif isinstance(part, DocumentPart):
                    req.add("documents")
                elif isinstance(part, AudioPart):
                    req.add("audio")
        if self.grounding:
            req.add("grounding")
        return req


@dataclass
class LLMResult:
    text: str
    finish: Finish
    provider: str
    model: str
    input_tokens: int = 0
    # Includes reasoning/thinking tokens: every provider here bills them as output.
    output_tokens: int = 0
    thinking_tokens: int = 0
    raw_finish: str = "-"
    sources: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    #: Authoritative USD cost reported by a gateway (gate0/LiteLLM), when known.
    #: Used instead of the local price table so both sides bill the same figure.
    cost_usd: Optional[float] = None
    #: The gateway's own id for this call (gate0's ``x-litellm-call-id``), so the
    #: service's ``llm_call`` line joins one-to-one with gate0's spend record.
    gateway_call_id: Optional[str] = None


class EmbeddingBatch(list):
    """The vectors of one embedding call, plus what a gateway reported about it.

    A plain ``list`` to every caller; providers that know more (gate0) fill the
    attributes so the router can log and bill the call like a generation.
    """

    def __init__(
        self,
        vectors: Any = (),
        *,
        input_tokens: int = 0,
        cost_usd: Optional[float] = None,
        gateway_call_id: Optional[str] = None,
    ) -> None:
        super().__init__(vectors)
        self.input_tokens = input_tokens
        self.cost_usd = cost_usd
        self.gateway_call_id = gateway_call_id


@dataclass(frozen=True)
class Capabilities:
    json_mode: bool = True
    json_schema: bool = False
    images: bool = False
    documents: bool = False
    audio: bool = False
    grounding: bool = False
    thinking: bool = False
    # Hard ceiling on output tokens for this model, or None when unknown/unbounded.
    max_output_tokens: Optional[int] = None

    def supports(self, need: str) -> bool:
        return bool(getattr(self, need, False))
