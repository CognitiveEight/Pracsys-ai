"""
ModelRouter: picks a provider profile for each call and applies, identically
for every provider, everything that is not request translation:

- the route: the task's chain from LLM_ROUTES (else LLM_DEFAULT), then
  LLM_FALLBACKS; grounded calls use LLM_GROUNDING_PROFILES;
- capability checks — a request with images/audio/grounding skips a profile
  that cannot take it, and the output budget is clamped to the model's ceiling;
- the per-request timeout, one retry of a transient error on the same profile,
  then the next profile on error/timeout. A ``blocked`` result is final: a
  content-policy decision is not routed around;
- the single ``llm_call`` log line and token-usage recording (priced per model).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from loguru import logger

from app.core.config import settings
from app.llm.base import Provider
from app.llm.config import (
    Profile,
    Routing,
    load_routing,
    model_options,
    parse_profile,
)
from app.llm.errors import (
    EmbeddingError,
    ProviderError,
    reraise_if_cancelled,
    with_timeout,
)
from app.llm.providers import provider_classes
from app.llm.types import Capabilities, EmbedTask, LLMRequest, LLMResult


class ModelRouter:
    def __init__(self) -> None:
        self._providers: Dict[str, Provider] = {}
        self._routing: Optional[Routing] = None
        self._warned_unavailable: set = set()

    # -- configuration ---------------------------------------------------------
    @property
    def routing(self) -> Routing:
        if self._routing is None:
            self._routing = load_routing()
        return self._routing

    def reload(self) -> None:
        """Re-read LLM_* settings and drop cached adapters (tests, hot config)."""
        from app.llm.config import clear_caches

        clear_caches()
        self._routing = None
        self._providers.clear()
        self._warned_unavailable.clear()

    def provider_for(self, profile: Profile) -> Provider:
        provider = self._providers.get(profile.key)
        if provider is None:
            cls = provider_classes()[profile.provider]
            provider = cls(profile.account)
            self._providers[profile.key] = provider
        return provider

    def embedding_profile(self) -> Profile:
        spec = (settings.embedding_provider or "gemini").strip()
        return parse_profile(f"{spec}:{settings.embedding_model}")

    def capabilities(self, provider: Provider, model: str) -> Capabilities:
        caps = provider.capabilities(model)
        opts = model_options(model)
        if not opts:
            return caps
        return replace(
            caps,
            max_output_tokens=opts.get("max_output_tokens", caps.max_output_tokens),
            images=bool(opts.get("supports_images", caps.images)),
            documents=bool(opts.get("supports_documents", caps.documents)),
            audio=bool(opts.get("supports_audio", caps.audio)),
            json_mode=bool(opts.get("json_mode", caps.json_mode)),
        )

    async def gate0_unserved_models(self) -> List[str]:
        """Configured ``gate0:`` models the gateway does not list for this key.

        Covers routes, fallbacks, grounding, user-selectable models and the
        embedding profile. Raises ``ProviderError`` when gate0 cannot be asked."""
        from app.llm.config import selectable_models

        profiles = [
            p
            for p in (
                self.routing.all_profiles()
                + list(selectable_models().values())
                + [self.embedding_profile()]
            )
            if p.provider == "gate0"
        ]
        if not profiles:
            return []
        provider = self.provider_for(profiles[0])
        if provider.unavailable_reason():
            return []
        served = set(await provider.served_models())  # type: ignore[attr-defined]
        return sorted({p.model for p in profiles if p.model not in served})

    def problems(self) -> List[Tuple[str, str]]:
        """(profile, reason) for every configured profile that cannot serve calls."""
        from app.llm.config import selectable_models

        out, seen = [], set()
        profiles = (
            self.routing.all_profiles()
            + list(selectable_models().values())
            + [self.embedding_profile()]
        )
        for profile in profiles:
            if str(profile) in seen:
                continue
            seen.add(str(profile))
            reason = self.blocked_by_governance(profile) or self.provider_for(
                profile
            ).unavailable_reason()
            if reason:
                out.append((str(profile), reason))
        return out

    @staticmethod
    def blocked_by_governance(profile: Profile) -> Optional[str]:
        """Under GATE0_ENFORCE every call must go through gate0."""
        if getattr(settings, "gate0_enforce", False) and profile.provider != "gate0":
            return "GATE0_ENFORCE is on: direct provider calls are not allowed (use gate0:<model>)"
        return None

    def chain_for(self, task: Optional[str]) -> List[Profile]:
        """The task's chain, headed by the model the user chose for this case
        (or this chat turn), unless LLM_ROUTES pins the task. The configured
        chain stays behind it as the fallback."""
        chain = self.routing.chain(task)
        if task and task in self.routing.routes:
            return chain
        from app.workspace.model_selection import selected_profile

        chosen = selected_profile()
        if chosen is None:
            return chain
        return [chosen] + [p for p in chain if str(p) != str(chosen)]

    def default_available(self) -> bool:
        return any(
            self.blocked_by_governance(p) is None
            and self.provider_for(p).unavailable_reason() is None
            for p in self.routing.chain(None)
        )

    def primary_profile(self, task: Optional[str] = None) -> Profile:
        return self.routing.chain(task)[0]

    # -- generation --------------------------------------------------------------
    async def complete(self, request: LLMRequest, *, task: str, kind: str) -> LLMResult:
        """Run ``request`` down the task's chain. Never raises except
        ``asyncio.CancelledError``; failure is ``finish`` error/timeout."""
        chain = self.routing.grounding if request.grounding else self.chain_for(task)
        needs = request.needs()
        retries = max(0, int(getattr(settings, "llm_transient_retries", 1) or 0))
        last_finish, last_error, attempted = "error", "", False

        for profile in chain:
            provider = self.provider_for(profile)
            reason = self.blocked_by_governance(profile) or provider.unavailable_reason()
            if reason:
                if str(profile) not in self._warned_unavailable:
                    self._warned_unavailable.add(str(profile))
                    logger.warning("Model profile {} unavailable: {}", profile, reason)
                last_error = last_error or f"{profile} unavailable: {reason}"
                continue
            caps = self.capabilities(provider, profile.model)
            missing = sorted(n for n in needs if not caps.supports(n))
            if missing:
                logger.info("Model profile {} skipped for {}: no {}", profile, task, ",".join(missing))
                continue
            req = replace(request, tags={**request.tags, "feature": task, "kind": kind})
            if caps.max_output_tokens and req.max_tokens > caps.max_output_tokens:
                req = replace(req, max_tokens=int(caps.max_output_tokens))
            if req.json_schema and not caps.json_schema:
                req = replace(req, json_schema=None)

            attempted = True
            for attempt in range(retries + 1):
                started = time.perf_counter()
                try:
                    result = await with_timeout(
                        provider.generate(req, profile.model), label=f"{kind}/{profile}"
                    )
                except asyncio.TimeoutError:
                    self._log(kind, task, profile, started, status="timeout")
                    last_finish, last_error = "timeout", "timed out"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reraise_if_cancelled()
                    transient = isinstance(exc, ProviderError) and exc.transient
                    retrying = transient and attempt < retries
                    self._log(
                        kind, task, profile, started, status="error",
                        error=f"{str(exc)[:200]}{'; retrying' if retrying else ''}",
                    )
                    last_finish, last_error = "error", str(exc)[:300]
                    if retrying:
                        await asyncio.sleep(min(8.0, 1.0 * (2 ** attempt)))
                        continue
                    break
                self._log(kind, task, profile, started, result=result)
                self._record(result)
                if result.finish == "error":
                    last_finish, last_error = "error", "empty response"
                    break
                return result

        if not attempted and not last_error:
            last_error = "no configured model profile supports this request"
        return LLMResult(
            text="",
            finish=last_finish if attempted else "error",  # type: ignore[arg-type]
            provider="-",
            model="-",
            error=last_error if attempted else f"not available: {last_error}",
        )

    @staticmethod
    def _record(result: LLMResult) -> None:
        # Imported here: app.ai imports this module (via gemini_client), so a
        # module-level import would be circular when app.llm is imported first.
        from app.ai.usage import record_usage

        try:
            record_usage(
                result.input_tokens,
                result.output_tokens,
                model=result.model,
                provider=result.provider,
                cost=result.cost_usd,
            )
        except Exception as exc:  # usage tracking must never break generation
            logger.debug("usage recording failed: {}", exc)

    @staticmethod
    def _log(
        kind: str,
        feature: str,
        profile: Profile,
        started: float,
        *,
        result: Optional[LLMResult] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """The ONE log line per model call: who called (feature), what
        (kind/provider/model), how it ended, how long, and the token spend.
        Never logs prompt or output text. WARNING unless status is ok."""
        try:
            if result is not None:
                status = result.finish
                finish = result.raw_finish
                in_t, out_t, think_t = result.input_tokens, result.output_tokens, result.thinking_tokens
                cost, call_id = result.cost_usd, result.gateway_call_id
            else:
                finish, in_t, out_t, think_t = "-", None, None, None
                cost, call_id = None, None
            duration_ms = round((time.perf_counter() - started) * 1000)
            log = logger.bind(
                llm=True,
                llm_kind=kind,
                llm_feature=feature,
                llm_provider=profile.provider,
                llm_model=profile.model,
                llm_status=status,
                llm_finish=finish,
                llm_ms=duration_ms,
                llm_in=in_t,
                llm_out=out_t,
                llm_think=think_t,
                llm_cost=cost,
                llm_gateway_call_id=call_id,
            )
            emit = log.info if status == "ok" else log.warning
            emit(
                "llm_call {} feature={} provider={} model={} status={} finish={} ms={} "
                "in={} out={} think={} cost={} gateway_call_id={}{}",
                kind, feature, profile.provider, profile.model, status, finish,
                duration_ms, in_t, out_t, think_t,
                "-" if cost is None else f"{cost:.10g}", call_id or "-",
                f" error={error}" if error else "",
            )
        except Exception as exc:  # logging must never break generation
            logger.debug("llm_call logging failed: {}", exc)

    # -- embeddings ----------------------------------------------------------------
    async def embed(self, texts: List[str], task: EmbedTask) -> List[List[float]]:
        """Embed ``texts`` with the configured embedding profile.

        Never pads, truncates or zero-fills: a wrong count, wrong dimension or
        all-zero vector raises :class:`EmbeddingError` (after retries), and the
        caller stores the chunk as ``embedding_failed``.
        """
        profile = self.embedding_profile()
        provider = self.provider_for(profile)
        reason = self.blocked_by_governance(profile) or provider.unavailable_reason()
        if reason:
            raise EmbeddingError(f"embedding profile {profile} unavailable: {reason}")
        out: List[List[float]] = []
        size = max(1, int(provider.embed_batch_size or 1))
        for i in range(0, len(texts), size):
            out.extend(await self._embed_with_retry(provider, profile, texts[i : i + size], task))
        return out

    async def _embed_with_retry(
        self, provider: Provider, profile: Profile, texts: List[str], task: EmbedTask
    ) -> List[List[float]]:
        attempts = max(1, int(getattr(settings, "embedding_max_attempts", 3) or 3))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._embed_once(provider, profile, texts, task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reraise_if_cancelled()
                last_error = exc
                logger.warning(
                    "Embedding attempt {}/{} failed ({} text(s), {}, {}): {}",
                    attempt, attempts, len(texts), task, profile, exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        raise EmbeddingError(f"embedding failed after {attempts} attempts: {last_error}")

    @staticmethod
    async def _embed_once(
        provider: Provider, profile: Profile, texts: List[str], task: EmbedTask
    ) -> List[List[float]]:
        dims = int(getattr(settings, "embedding_dimensions", 768) or 768)
        started = time.perf_counter()
        vectors = await with_timeout(
            provider.embed(list(texts), profile.model, task, dims), label=f"embed/{profile}"
        )
        if len(vectors) != len(texts):
            raise EmbeddingError(f"embedding returned {len(vectors)} vectors for {len(texts)} inputs")
        for v in vectors:
            if len(v) != dims:
                raise EmbeddingError(
                    f"embedding has {len(v)} dimensions, expected {dims} — the "
                    f"model does not honour the requested dimensionality"
                )
            if not any(v):
                raise EmbeddingError("embedding came back all zeros")
        # One llm_call line per embedding call, like a generation: the gateway's
        # call id and cost let it be matched to gate0's spend record.
        ModelRouter._log(
            "embed",
            "embeddings",
            profile,
            started,
            result=LLMResult(
                text="",
                finish="ok",
                provider=profile.provider,
                model=profile.model,
                input_tokens=int(getattr(vectors, "input_tokens", 0) or 0),
                raw_finish=task,
                cost_usd=getattr(vectors, "cost_usd", None),
                gateway_call_id=getattr(vectors, "gateway_call_id", None),
            ),
        )
        return [l2_normalise(v) for v in vectors]


def l2_normalise(vector: List[float]) -> List[float]:
    """Unit-length copy. A reduced-dimension embedding is not unit length."""
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return list(vector)
    return [v / norm for v in vector]
