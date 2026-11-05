"""
Model token pricing and paid non-LLM API pricing.

Central source of truth for how many USD a Gemini generation costs, used to
attach a per-response price and a running per-workspace total to every
``ai_response`` payload. :func:`service_price_for` does the same for the paid
non-LLM APIs the pipeline uses (Vision OCR, Speech-to-Text, Vertex AI Search,
Google Search grounding, embeddings) — see the ``SERVICE PRICING`` section.

Prices are USD per 1,000,000 tokens (paid tier, text input) as published on
https://ai.google.dev/gemini-api/docs/pricing. Gemini bills *thinking* tokens
(``thoughts_token_count``) as output, so callers should fold thinking tokens
into the output count before calling :func:`price_for`.

The table is intentionally small and easy to update. When the active model is
not listed we fall back — in order — to a longest-prefix match, then to the
configurable default rate in ``settings`` — so an unknown/renamed model never
crashes a response, it just prices at the default with a warning.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from loguru import logger

from app.core.config import settings

# model name -> (input_usd_per_million, output_usd_per_million)
# Keep in sync with https://ai.google.dev/gemini-api/docs/pricing
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    # Launch rate, valid through 2026-12-31. Google has announced it doubles to
    # (1.50, 7.50) from 2027-01-01 — update this row (or set
    # TOKEN_PRICE_INPUT/OUTPUT_PER_MILLION) before then or costs under-report.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# Fallback when a model is not found in the table and no override is set.
_DEFAULT_RATES: Tuple[float, float] = (0.50, 3.00)

_PER_MILLION = 1_000_000


def _rates_for(model: str) -> Tuple[float, float]:
    """Resolve (input_rate, output_rate) per 1M tokens for ``model``.

    Resolution order: explicit config override -> exact table match ->
    longest-prefix table match -> module default. Never raises.
    """
    # 1) Explicit operator override (both must be set to take effect).
    cfg_in = getattr(settings, "token_price_input_per_million", None)
    cfg_out = getattr(settings, "token_price_output_per_million", None)
    if cfg_in is not None and cfg_out is not None:
        return float(cfg_in), float(cfg_out)

    if not model:
        return _DEFAULT_RATES

    # 1b) Per-model operator rates (LLM_PRICING_OVERRIDES). This is how
    #     non-Gemini models are priced: their rates are deliberately not
    #     hard-coded here, because they change and must be taken from each
    #     provider's own price page (OpenAI, Azure, AWS, DeepSeek, …), or for
    #     self-hosted models computed from the serving cost.
    override = _override_rates(model)
    if override is not None:
        return override

    # 2) Exact match.
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # 3) Longest-prefix match (handles versioned suffixes like
    #    "gemini-2.5-flash-002" or "-latest").
    best_key = ""
    for key in MODEL_PRICING:
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return MODEL_PRICING[best_key]

    # 4) Default — pricing is best-effort, never a hard failure. Every call is
    #    priced, so warn once per model rather than on each call.
    if model not in _warned_unpriced:
        _warned_unpriced.add(model)
        logger.warning(
            "No pricing entry for model '{}'; using default rates {} per 1M tokens "
            "(set LLM_PRICING_OVERRIDES)",
            model,
            _DEFAULT_RATES,
        )
    return _DEFAULT_RATES


_warned_unpriced: set = set()


def _override_rates(model: str) -> Optional[Tuple[float, float]]:
    """``LLM_PRICING_OVERRIDES`` rate for ``model`` (exact, else longest prefix)."""
    try:
        from app.llm.config import pricing_overrides

        table = pricing_overrides()
    except Exception as exc:  # malformed JSON is reported at startup validation
        logger.debug("LLM_PRICING_OVERRIDES unreadable: {}", exc)
        return None
    key = model if model in table else max(
        (k for k in table if model.startswith(k)), key=len, default=""
    )
    if not key:
        return None
    try:
        in_rate, out_rate = table[key]
        return float(in_rate), float(out_rate)
    except (TypeError, ValueError):
        logger.warning("LLM_PRICING_OVERRIDES['{}'] must be [input, output]", key)
        return None


def price_for(model: str, input_tokens: int, output_tokens: int) -> Dict[str, float]:
    """Return the USD cost of a single generation.

    ``output_tokens`` should already include any thinking tokens (Gemini bills
    them as output). Returns ``inputPrice``, ``outputPrice`` and their sum
    ``price``, each rounded to 6 decimal places (sub-cent precision).
    """
    in_rate, out_rate = _rates_for(model)
    input_tokens = max(int(input_tokens or 0), 0)
    output_tokens = max(int(output_tokens or 0), 0)

    input_price = round(input_tokens / _PER_MILLION * in_rate, 6)
    output_price = round(output_tokens / _PER_MILLION * out_rate, 6)
    return {
        "inputPrice": input_price,
        "outputPrice": output_price,
        "price": round(input_price + output_price, 6),
    }


# ==========================================================================
# SERVICE PRICING — paid non-LLM APIs
# ==========================================================================
#
# The pipeline spends money outside Gemini generation: Vision OCR on scanned
# PDFs/images, Speech-to-Text on audio evidence, Vertex AI Search and Google
# Search grounding on legal lookups, and embeddings on vector storage. These
# are counted by app.ai.usage (record_ocr_units, record_stt_seconds, …) and
# priced here, then folded into the ``price`` / ``totalPrice`` a workspace
# reports.
#
# Rates below were verified against the cited pages in July 2026. Each is
# overridable from the environment (see app/core/config.py) so ops can correct a
# rate without a deploy — re-check the pages when an upstream price moves.
#
# NEITHER free tiers NOR volume discounts are modelled, because both are monthly
# and account-wide and a per-workspace counter cannot see them: Vision's first
# 1,000 units/month (and its $0.60/1k tier above 5M units), Speech-to-Text's
# volume tiers (down to $0.004/min) and Dynamic Batch discount, Vertex AI
# Search's 10,000 free queries/month, and grounding's monthly free prompts.
# Emitted cost is therefore an UPPER BOUND on the true invoice.

# Vision TEXT_DETECTION, units 1,001–5,000,000/month. Each page of a multi-page
# file (e.g. a PDF) bills as its own image, which is why units are counted per
# page in app/processing/file_handlers.py.
# https://cloud.google.com/vision/pricing
OCR_PRICE_PER_1K_UNITS: float = 1.50

# Speech-to-Text V2 (Chirp 3) recognition, per minute of audio. $0.016/min is the
# base rate for BOTH real-time and batch (BatchRecognize); the cheaper Dynamic
# Batch tier is a different API mode this service does not use.
# https://cloud.google.com/speech-to-text/pricing
STT_PRICE_PER_MINUTE: float = 0.016

# Vertex AI Search, Standard Edition, per 1,000 queries.
# https://cloud.google.com/generative-ai-app-builder/pricing
SEARCH_PRICE_PER_1K_QUERIES: float = 1.50

# Grounding with Google Search via the Gemini API, per 1,000 requests. The rate
# depends on the model generation: Gemini 2.5 bills $35 per 1,000 grounded
# prompts, Gemini 3 bills $14 per 1,000 search queries — so this resolves against
# the active model rather than being a single constant.
# https://ai.google.dev/gemini-api/docs/pricing
GROUNDING_PRICE_PER_1K_REQUESTS: float = 35.00
GROUNDING_PRICE_PER_1K_REQUESTS_GEMINI_3: float = 14.00

# Gemini embeddings, per 1M input tokens.
# https://ai.google.dev/gemini-api/docs/pricing
EMBED_PRICE_PER_MILLION_TOKENS: float = 0.15

_PER_THOUSAND = 1_000


def _grounding_rate_default() -> float:
    """Default grounding rate for the active model generation.

    Gemini 3 grounding is materially cheaper than Gemini 2.5's, so pricing it
    with one flat constant would over-report by 2.5x the moment ``gemini_model``
    is switched to a Gemini 3 model. Prefix-matched in the same spirit as
    :func:`_rates_for`.
    """
    model = getattr(settings, "gemini_model", "") or ""
    if model.startswith("gemini-3"):
        return GROUNDING_PRICE_PER_1K_REQUESTS_GEMINI_3
    return GROUNDING_PRICE_PER_1K_REQUESTS


def _service_rate(setting_name: str, default: float) -> float:
    """Resolve one service rate: settings override -> module default.

    Mirrors :func:`_rates_for`'s ``getattr`` style so a missing/unset setting is
    never a hard failure.
    """
    override = getattr(settings, setting_name, None)
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid {} override ({!r}); using default {}",
                setting_name,
                override,
                default,
            )
    return default


def service_price_for(
    *,
    ocr_units: int = 0,
    stt_seconds: float = 0.0,
    search_queries: int = 0,
    grounding_requests: int = 0,
    embed_tokens: int = 0,
    embed_gateway_tokens: int = 0,
    embed_gateway_cost: float = 0.0,
) -> Dict[str, float]:
    """Return the USD cost of the paid non-LLM work in one scope.

    Keys: ``ocrPrice``, ``sttPrice``, ``searchPrice``, ``groundingPrice``,
    ``embedPrice`` and their sum ``servicePrice`` — each rounded to 6 decimal
    places (sub-cent precision), matching :func:`price_for`. Negative/None
    quantities are treated as 0. Never raises.

    ``embed_gateway_tokens`` of the ``embed_tokens`` were priced by a gateway at
    ``embed_gateway_cost``; only the rest are priced at the local rate.
    """
    ocr_units = max(int(ocr_units or 0), 0)
    stt_seconds = max(float(stt_seconds or 0.0), 0.0)
    search_queries = max(int(search_queries or 0), 0)
    grounding_requests = max(int(grounding_requests or 0), 0)
    embed_tokens = max(int(embed_tokens or 0), 0)

    ocr_price = round(
        ocr_units / _PER_THOUSAND * _service_rate(
            "ocr_price_per_1k_units", OCR_PRICE_PER_1K_UNITS
        ),
        6,
    )
    stt_price = round(
        stt_seconds / 60.0 * _service_rate(
            "stt_price_per_minute", STT_PRICE_PER_MINUTE
        ),
        6,
    )
    search_price = round(
        search_queries / _PER_THOUSAND * _service_rate(
            "search_price_per_1k_queries", SEARCH_PRICE_PER_1K_QUERIES
        ),
        6,
    )
    grounding_price = round(
        grounding_requests / _PER_THOUSAND * _service_rate(
            "grounding_price_per_1k_requests", _grounding_rate_default()
        ),
        6,
    )
    gateway_tokens = min(max(int(embed_gateway_tokens or 0), 0), embed_tokens)
    gateway_cost = max(float(embed_gateway_cost or 0.0), 0.0)
    embed_price = round(
        (embed_tokens - gateway_tokens) / _PER_MILLION * _service_rate(
            "embed_price_per_million_tokens", EMBED_PRICE_PER_MILLION_TOKENS
        )
        + gateway_cost,
        6,
    )

    return {
        "ocrPrice": ocr_price,
        "sttPrice": stt_price,
        "searchPrice": search_price,
        "groundingPrice": grounding_price,
        "embedPrice": embed_price,
        "servicePrice": round(
            ocr_price + stt_price + search_price + grounding_price + embed_price, 6
        ),
    }
