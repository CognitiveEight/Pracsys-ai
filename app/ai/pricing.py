"""
Model token pricing.

Central source of truth for how many USD a Gemini generation costs, used to
attach a per-response price and a running per-workspace total to every
``ai_response`` payload.

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

from typing import Dict, Tuple

from loguru import logger

from app.core.config import settings

# model name -> (input_usd_per_million, output_usd_per_million)
# Keep in sync with https://ai.google.dev/gemini-api/docs/pricing
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
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

    # 4) Default — pricing is best-effort, never a hard failure.
    logger.warning(
        "No pricing entry for model '{}'; using default rates {} per 1M tokens",
        model,
        _DEFAULT_RATES,
    )
    return _DEFAULT_RATES


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
