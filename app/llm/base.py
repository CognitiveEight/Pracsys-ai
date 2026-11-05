"""
The contract every model provider adapter implements.

An adapter translates an :class:`LLMRequest` into one SDK call and the SDK's
response into an :class:`LLMResult`. It does NOT time out, retry, fall back,
repair JSON, log the ``llm_call`` line or record token usage — the router does
all of that once, identically for every provider. An adapter:

- returns an ``LLMResult`` for any response the provider produced, including
  a truncated or safety-blocked one (``finish`` says which);
- raises :class:`ProviderError` (``transient=True`` for 429/5xx/network) when
  the call itself failed;
- never swallows ``asyncio.CancelledError``.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Callable, List, Optional

from app.llm.types import Capabilities, EmbedTask, LLMRequest, LLMResult


class Provider:
    #: Registry name (the first segment of a profile string).
    name: str = ""
    #: Max texts per embedding request; the router splits larger batches.
    embed_batch_size: int = 100

    def __init__(self, account: str = "") -> None:
        self.account = account

    def unavailable_reason(self) -> Optional[str]:
        """Why this provider cannot serve calls (missing SDK / credentials),
        or None when it can. Must not log or return any secret value."""
        return None

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities()

    async def generate(self, request: LLMRequest, model: str) -> LLMResult:
        raise NotImplementedError

    async def embed(
        self, texts: List[str], model: str, task: EmbedTask, dimensions: int
    ) -> List[List[float]]:
        raise NotImplementedError(f"{self.name} does not provide embeddings")


class LoopLocal:
    """One lazily-built object per running event loop.

    Async SDK clients (httpx / aiohttp sessions) bind to the loop that first
    uses them. The service runs one loop, but scripts and the sync bridge used
    by the audio pipeline can run others, and a client reused across loops
    fails with "Event loop is closed".
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = (
            weakref.WeakKeyDictionary()
        )
        self._no_loop: Optional[Any] = None

    def get(self) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._no_loop is None:
                self._no_loop = self._factory()
            return self._no_loop
        client = self._by_loop.get(loop)
        if client is None:
            client = self._factory()
            self._by_loop[loop] = client
        return client

    def set(self, client: Any) -> None:
        """Pin one client for every loop (tests inject fakes this way)."""
        self._factory = lambda: client
        self._by_loop = weakref.WeakKeyDictionary()
        self._no_loop = None


def text_of(request: LLMRequest) -> str:
    """The request flattened to text, for providers that only take a prompt."""
    from app.llm.types import TextPart

    chunks: List[str] = []
    for msg in request.messages:
        body = "\n".join(p.text for p in msg.parts if isinstance(p, TextPart))
        if len(request.messages) > 1:
            chunks.append(f"{'User' if msg.role == 'user' else 'Assistant'}: {body}")
        else:
            chunks.append(body)
    return "\n\n".join(chunks)


def missing_module(module: str) -> Optional[str]:
    """None when ``module`` imports, else an install hint."""
    import importlib.util

    try:
        found = importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a dotted name whose parent package is absent
        found = False
    if not found:
        return f"python package for '{module}' is not installed (see requirements.txt)"
    return None
