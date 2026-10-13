"""
Per-turn usage accumulation (LLM tokens + paid non-LLM API units).

A single user turn fans out into many ``GeminiClient.generate*`` calls (the
main answer, suggestion chips, strategy refresh, …), and ``gemini_client`` is a
module-level singleton shared across concurrent requests. To attribute token
usage to the right turn without threading a counter through every call site, we
use a :class:`contextvars.ContextVar`: each turn opens a :func:`capture_usage`
scope, every generate call inside it reports via :func:`record_usage`, and the
scope reads the aggregated totals at the end.

The same scope also carries the **paid non-LLM** work a turn triggers — Vision
OCR units, Speech-to-Text seconds, Vertex AI Search queries, Google Search
grounding requests, embedding tokens — reported via the ``record_*`` helpers
below and priced by :func:`app.ai.pricing.service_price_for`. Without this those
costs were billed to nobody, so ``price``/``totalPrice`` understated real spend.

``contextvars`` are copied per ``asyncio`` task, so two workspaces generating
concurrently never see each other's counts. They are also copied into
``asyncio.to_thread`` workers, and since the helpers mutate the *object* the
ContextVar points to (not the var), OCR/STT running in a worker thread still
reports into the scope that opened it — hence the lock below.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Optional

from loguru import logger


@dataclass
class UsageAccumulator:
    """Running usage totals for one captured scope (one user turn / feature)."""

    input_tokens: int = 0
    output_tokens: int = 0
    # LLM cost, priced per call at record time by the model that served it — a
    # scope can mix models once tasks are routed to different providers, so
    # pricing the token totals by one model at the end would be wrong.
    input_price: float = 0.0
    output_price: float = 0.0
    # model -> tokens (in + out) it consumed in this scope.
    models: dict = field(default_factory=dict)
    # The part of this scope recorded with NO workspace in context, so it never
    # reached the workspace ledger; the scope's own emit reports it instead
    # (see drain_for_emit). Everything else is reported from the ledger.
    unattributed: Optional["UsageAccumulator"] = field(default=None, repr=False, compare=False)
    # Set once the turn's totals have been billed into the workspace cumulative,
    # so a second emit in the same turn (e.g. an error path after the main
    # response) does not double-count the same tokens.
    counted: bool = False

    # --- Paid non-LLM units consumed inside this scope ---
    # Vision text_detection units: 1 per image AND 1 per OCR'd PDF page.
    ocr_units: int = 0
    # Speech-to-Text V2 billed audio seconds (sum over jobs/chunks, including
    # retries and chunk overlap — that is what Google bills).
    stt_seconds: float = 0.0
    # Vertex AI Search queries issued.
    search_queries: int = 0
    # Gemini google_search grounding requests (billed per request on top of tokens).
    grounding_requests: int = 0
    # Embedding input tokens (ESTIMATED — see record_embed_tokens).
    embed_tokens: int = 0

    # OCR and STT report from asyncio.to_thread workers, and process_batch may
    # process several documents concurrently against one accumulator, so every
    # mutation is guarded.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def add(
        self,
        prompt_tokens: int,
        output_tokens: int,
        model: Optional[str] = None,
        cost: Optional[float] = None,
    ) -> None:
        """Add one generation's tokens, priced by ``model`` (default: the
        configured Gemini model). ``cost`` — an authoritative USD figure from a
        gateway — replaces the table price; it is split into input/output in
        the table's proportion so the per-side fields still add up to it.
        Negative/None values are treated as 0."""
        from app.ai.pricing import price_for
        from app.core.config import settings

        in_tok = max(int(prompt_tokens or 0), 0)
        out_tok = max(int(output_tokens or 0), 0)
        name = model or settings.gemini_model
        price = price_for(name, in_tok, out_tok)
        if cost is not None and cost >= 0:
            table_total = price["inputPrice"] + price["outputPrice"]
            in_share = price["inputPrice"] / table_total if table_total else 0.0
            price = {
                "inputPrice": round(cost * in_share, 6),
                "outputPrice": round(cost * (1 - in_share), 6),
            }
        with self._lock:
            self.input_tokens += in_tok
            self.output_tokens += out_tok
            self.input_price += price["inputPrice"]
            self.output_price += price["outputPrice"]
            self.models[name] = self.models.get(name, 0) + in_tok + out_tok

    def add_ocr_units(self, units: int) -> None:
        """Add Vision OCR units. Negative/None values are treated as 0."""
        with self._lock:
            self.ocr_units += max(int(units or 0), 0)

    def add_stt_seconds(self, seconds: float) -> None:
        """Add Speech-to-Text billed seconds. Negative/None values are treated as 0."""
        with self._lock:
            self.stt_seconds += max(float(seconds or 0.0), 0.0)

    def add_search_queries(self, queries: int) -> None:
        """Add Vertex AI Search queries. Negative/None values are treated as 0."""
        with self._lock:
            self.search_queries += max(int(queries or 0), 0)

    def add_grounding_requests(self, requests: int) -> None:
        """Add Google Search grounding requests. Negative/None values are treated as 0."""
        with self._lock:
            self.grounding_requests += max(int(requests or 0), 0)

    def add_embed_tokens(self, tokens: int) -> None:
        """Add embedding input tokens. Negative/None values are treated as 0."""
        with self._lock:
            self.embed_tokens += max(int(tokens or 0), 0)


# The active accumulator for the current async task, or None when no scope is
# open (in which case record_usage is a harmless no-op).
_current_usage: ContextVar[Optional[UsageAccumulator]] = ContextVar(
    "current_usage", default=None
)


#: Name of the feature whose work is running (the @meter_usage-decorated
#: function), so every model call can say who made it without each of the
#: ~80 call sites passing a label.
_current_feature: ContextVar[str] = ContextVar("current_feature", default="")


def current_feature() -> str:
    """The feature label for the running work, or "" outside any feature."""
    return _current_feature.get()


def _feature_label(func_name: str) -> str:
    name = func_name.lstrip("_")
    for prefix in ("generate_and_emit_", "generate_", "regenerate_", "run_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


@contextlib.contextmanager
def capture_usage() -> Iterator[UsageAccumulator]:
    """Open a usage-capture scope and yield its accumulator.

    All :func:`record_usage` calls made (directly or transitively) within the
    ``with`` block add to the yielded accumulator. The previous scope, if any,
    is restored on exit so nested scopes do not leak.
    """
    accumulator = UsageAccumulator()
    token = _current_usage.set(accumulator)
    try:
        yield accumulator
    finally:
        _current_usage.reset(token)


def current_usage() -> Optional[UsageAccumulator]:
    """Return the accumulator for the active scope, or None if none is open."""
    return _current_usage.get()


def meter_usage(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorate an async method so its whole body runs inside a fresh
    :func:`capture_usage` scope.

    Used on feature generate-and-emit handlers: every ``record_usage`` made while
    the method runs is attributed to a per-call accumulator that the method's own
    emit reads via :func:`current_usage`. The scope is NESTED — if an outer scope
    is open (e.g. a chat turn), that outer scope is restored on return and does
    NOT receive this feature's tokens, so each feature is billed exactly once to
    the workspace cumulative and the chat ``ai_response`` is not double-charged.
    """

    label = _feature_label(func.__name__)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _current_feature.set(label)
        try:
            with capture_usage():
                return await func(*args, **kwargs)
        finally:
            _current_feature.reset(token)

    return wrapper


def llm_price(usage: Any) -> "dict[str, float]":
    """LLM cost of a scope: ``{inputPrice, outputPrice, price}``.

    Uses the per-call prices accumulated at record time. An object without
    them (a test double, or a scope from before per-model pricing) is priced by
    the configured Gemini model, which is what every scope used to be.
    """
    from app.ai.pricing import price_for
    from app.core.config import settings

    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    models = getattr(usage, "models", None)
    if isinstance(models, dict) and models:
        in_price = round(float(getattr(usage, "input_price", 0.0) or 0.0), 6)
        out_price = round(float(getattr(usage, "output_price", 0.0) or 0.0), 6)
        return {
            "inputPrice": in_price,
            "outputPrice": out_price,
            "price": round(in_price + out_price, 6),
        }
    return price_for(settings.gemini_model, in_tok, out_tok)


def primary_model(usage: Any) -> str:
    """The model that did most of a scope's work (for ``aiModel`` labels)."""
    from app.core.config import settings

    models = getattr(usage, "models", None)
    if isinstance(models, dict) and models:
        return max(models.items(), key=lambda kv: kv[1])[0]
    return settings.gemini_model


def begin_usage() -> "tuple[object, UsageAccumulator]":
    """Open a usage-capture scope imperatively (for try/finally call sites).

    Returns ``(token, accumulator)``. The caller MUST pass ``token`` to
    :func:`reset_usage` in a ``finally`` block to restore the previous scope.
    Prefer the :func:`capture_usage` context manager where a ``with`` block
    fits; use this only when re-indenting the whole body is impractical.
    """
    accumulator = UsageAccumulator()
    token = _current_usage.set(accumulator)
    return token, accumulator


def reset_usage(token: object) -> None:
    """Restore the scope captured before a matching :func:`begin_usage`."""
    try:
        _current_usage.reset(token)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("reset_usage failed: {}", exc)


def record_usage(
    prompt_tokens: int,
    output_tokens: int,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    cost: Optional[float] = None,
) -> None:
    """Report one generation's token usage to the active scope, if any.

    Safe to call from anywhere. The call is billed to the active scope and to
    the running event's workspace (see the workspace ledger below); with
    neither, it warns once (``_warned_unscoped_kinds``) — tokens spent with no
    scope and no workspace are billed to nobody, and that must be visible. ``output_tokens`` should already
    include thinking tokens (every supported provider bills them as output).
    ``model`` prices the call; ``cost`` (a gateway's own USD figure) overrides
    the table price. ``provider`` is accepted for symmetry with the
    ``llm_call`` log line and is not needed for pricing.
    """
    _record(
        "LLM tokens",
        (prompt_tokens or 0) + (output_tokens or 0),
        lambda acc: acc.add(prompt_tokens, output_tokens, model, cost),
    )


# --- Paid non-LLM API recorders -------------------------------------------
#
# Same contract as record_usage: safe from any thread, a no-op when no scope is
# open, and never raises — cost tracking must never break the work it measures.
# Every recorder (LLM tokens included) warns on a dropped non-zero quantity: a
# paid unit consumed outside a scope is money billed to nobody, which is exactly
# the bug these counters exist to fix, so it must be visible in the logs.


_warned_unscoped_kinds: set[str] = set()


def _warn_unscoped_once(kind: str, quantity: float) -> None:
    """Warn once per kind per process that paid usage was billed to nobody.

    Once per kind is enough to surface an uninstrumented path, without spamming
    when it is hit in a loop (e.g. per-chunk embeddings). A zero quantity is not
    worth a warning — nothing was spent.
    """
    if not quantity or kind in _warned_unscoped_kinds:
        return
    _warned_unscoped_kinds.add(kind)
    logger.warning(
        "Paid {} usage ({}) consumed outside any capture_usage scope — "
        "cost not billed to a workspace (warning shown once per kind)",
        kind,
        quantity,
    )


def _record_service_unit(
    kind: str, quantity: float, apply: Callable[[UsageAccumulator], None]
) -> None:
    """Shared plumbing for the paid-unit recorders below."""
    _record(kind, quantity, apply)


# --- Workspace ledger -------------------------------------------------------
#
# Every paid unit is charged to the workspace it was spent for AT RECORD TIME,
# keyed by the workspace_id bound in the logging context of the running event
# (the same id gate0/LiteLLM tags each request with, so per-case totals
# reconcile with the gateway's spend logs). Emits then report — and add to the
# workspace's cumulative total — whatever the ledger holds since the last emit
# (drain_for_emit). A cost spent in a scope that is never emitted (knowledge
# graph ingestion, background re-evaluations, …) therefore still reaches the
# case on its next emit instead of being billed to nobody.

_ledger: dict = {}
_ledger_lock = threading.Lock()


def current_workspace_id() -> Optional[str]:
    """workspace_id of the inbound event this code runs for, from the logging
    context (``logger.contextualize``); None outside a workspace event."""
    try:
        import loguru._logger as _loguru

        value = (_loguru.context.get() or {}).get("workspace_id")
    except Exception:  # internal API; attribution degrades to the scope
        return None
    value = str(value or "").strip()
    return value if value and value not in ("-", "unknown", "None") else None


def _record(kind: str, quantity: float, apply: Callable[[UsageAccumulator], None]) -> None:
    """Apply one paid quantity to the active scope and to the workspace ledger."""
    accumulator = _current_usage.get()
    workspace_id = current_workspace_id()
    if accumulator is None and workspace_id is None:
        _warn_unscoped_once(kind, quantity)
        return
    try:
        if accumulator is not None:
            apply(accumulator)
            if workspace_id is None:
                with accumulator._lock:
                    if accumulator.unattributed is None:
                        accumulator.unattributed = UsageAccumulator()
                apply(accumulator.unattributed)
        if workspace_id is not None:
            with _ledger_lock:
                apply(_ledger.setdefault(workspace_id, UsageAccumulator()))
    except Exception as exc:  # cost tracking must never break the work it measures
        logger.debug("record {} usage failed: {}", kind, exc)


def merge_usage(*parts: Optional[UsageAccumulator]) -> UsageAccumulator:
    """One accumulator holding the sum of ``parts`` (None entries skipped)."""
    out = UsageAccumulator()
    for part in parts:
        if part is None:
            continue
        out.input_tokens += part.input_tokens
        out.output_tokens += part.output_tokens
        out.input_price += part.input_price
        out.output_price += part.output_price
        for name, tokens in part.models.items():
            out.models[name] = out.models.get(name, 0) + tokens
        out.ocr_units += part.ocr_units
        out.stt_seconds += part.stt_seconds
        out.search_queries += part.search_queries
        out.grounding_requests += part.grounding_requests
        out.embed_tokens += part.embed_tokens
    return out


def drain_for_emit(workspace_id: Optional[str], usage: Any = None) -> UsageAccumulator:
    """The spend an emit for ``workspace_id`` must report, removed so no other
    emit reports it again: everything charged to the workspace since its last
    emit, plus the part of ``usage`` (the emitting scope) that had no workspace
    in context when it was recorded."""
    drained = None
    if workspace_id:
        with _ledger_lock:
            drained = _ledger.pop(str(workspace_id), None)
    unattributed = None
    if isinstance(usage, UsageAccumulator):
        with usage._lock:
            unattributed, usage.unattributed = usage.unattributed, None
    return merge_usage(drained, unattributed)


def pending_workspace_usage(workspace_id: str) -> Optional[UsageAccumulator]:
    """Copy of the not-yet-reported spend for a workspace (diagnostics/tests)."""
    with _ledger_lock:
        acc = _ledger.get(str(workspace_id))
        return merge_usage(acc) if acc is not None else None


def forget_workspace_usage(workspace_id: str) -> None:
    """Drop a workspace's unreported spend (workspace purge)."""
    with _ledger_lock:
        _ledger.pop(str(workspace_id), None)


def record_ocr_units(units: int = 1) -> None:
    """Report Vision OCR units (1 per image, 1 per OCR'd PDF page)."""
    _record_service_unit("OCR", units, lambda acc: acc.add_ocr_units(units))


def record_stt_seconds(seconds: float) -> None:
    """Report Speech-to-Text billed audio seconds."""
    _record_service_unit("STT", seconds, lambda acc: acc.add_stt_seconds(seconds))


def record_search_queries(queries: int = 1) -> None:
    """Report Vertex AI Search queries issued."""
    _record_service_unit(
        "Vertex AI Search", queries, lambda acc: acc.add_search_queries(queries)
    )


def record_grounding_requests(requests: int = 1) -> None:
    """Report Google Search grounding requests (billed on top of tokens)."""
    _record_service_unit(
        "Google Search grounding",
        requests,
        lambda acc: acc.add_grounding_requests(requests),
    )


def log_unbilled_usage(label: str, usage: Optional[UsageAccumulator]) -> None:
    """Log the priced spend of a scope that has no workspace to bill to.

    Some paid work is genuinely user-scoped rather than workspace-scoped (e.g.
    template-library ingestion + profiling), so there is no ``token_usage``
    cumulative to add it to and no emit to hang a ``metadata`` block on. Opening
    a scope and logging it here keeps that cost visible and stops the
    :func:`_warn_unscoped_once` alarm from firing on an intentional path.

    Best-effort and silent when nothing was spent.
    """
    if usage is None:
        return
    try:
        from app.ai.pricing import service_price_for

        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        service = service_price_for(
            ocr_units=int(getattr(usage, "ocr_units", 0) or 0),
            stt_seconds=float(getattr(usage, "stt_seconds", 0.0) or 0.0),
            search_queries=int(getattr(usage, "search_queries", 0) or 0),
            grounding_requests=int(getattr(usage, "grounding_requests", 0) or 0),
            embed_tokens=int(getattr(usage, "embed_tokens", 0) or 0),
        )
        llm = llm_price(usage)
        total = round(llm["price"] + service["servicePrice"], 6)
        if not (in_tok or out_tok or service["servicePrice"]):
            return
        logger.info(
            "Usage ({}): {} in / {} out tokens, ocr={} stt={}s embed={} — "
            "${} (not billed to a workspace: no workspace in scope)",
            label,
            in_tok,
            out_tok,
            int(getattr(usage, "ocr_units", 0) or 0),
            round(float(getattr(usage, "stt_seconds", 0.0) or 0.0), 1),
            int(getattr(usage, "embed_tokens", 0) or 0),
            total,
        )
    except Exception as exc:  # logging cost must never break the caller
        logger.debug("log_unbilled_usage failed ({}): {}", label, exc)


def record_embed_tokens(tokens: int) -> None:
    """Report embedding input tokens.

    The legacy ``genai.embed_content`` response carries no ``usage_metadata``, so
    callers pass an ESTIMATE (~4 chars/token). ``embedPrice`` is therefore
    approximate; at $0.15/1M tokens the error is negligible next to OCR/STT.
    """
    _record_service_unit(
        "embedding", tokens, lambda acc: acc.add_embed_tokens(tokens)
    )
