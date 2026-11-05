#!/usr/bin/env python3
"""
Live smoke test of every configured model profile. BILLED: makes one small
JSON generation per profile (and one embedding with --embed) against the real
providers configured in .env.

For each profile in LLM_DEFAULT / LLM_ROUTES / LLM_FALLBACKS (or the ones given
with --profile) it prints: status, finish, latency, tokens and price — never
prompt or output text. Exit code is non-zero if any profile fails, so it can gate
a deploy that switches providers.

USAGE
    python scripts/smoke_llm_providers.py                       # all configured profiles
    python scripts/smoke_llm_providers.py --profile openai_compat:deepseek:deepseek-chat
    python scripts/smoke_llm_providers.py --embed               # also the embedding profile
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROMPT = (
    "Return a JSON object with keys \"court\" and \"section\" for this sentence: "
    "The Delhi High Court considered Section 318 of the Bharatiya Nyaya Sanhita."
)


async def _one(router, profile) -> bool:
    from app.ai.pricing import price_for
    from app.llm.errors import with_timeout
    from app.llm.json_utils import coerce_valid_json_text
    from app.llm.types import LLMRequest

    provider = router.provider_for(profile)
    reason = provider.unavailable_reason()
    if reason:
        print(f"  {profile}: NOT CONFIGURED — {reason}")
        return False
    request = LLMRequest.from_prompt(_PROMPT, temperature=0.0, max_tokens=512, json_mode=True)
    started = time.perf_counter()
    try:
        result = await with_timeout(provider.generate(request, profile.model), label=str(profile))
    except Exception as exc:  # report and keep going
        print(f"  {profile}: FAILED — {type(exc).__name__}: {str(exc)[:200]}")
        return False
    ms = round((time.perf_counter() - started) * 1000)
    valid = coerce_valid_json_text(result.text) is not None
    # A gateway's own cost (gate0) is what the service bills; else the local table.
    price = round(result.cost_usd, 6) if result.cost_usd is not None else price_for(
        profile.model, result.input_tokens, result.output_tokens
    )["price"]
    source = "gateway" if result.cost_usd is not None else "table"
    ok = result.finish == "ok" and valid
    print(
        f"  {profile}: {'ok' if ok else 'PROBLEM'} finish={result.finish}({result.raw_finish}) "
        f"json={'valid' if valid else 'INVALID'} ms={ms} in={result.input_tokens} "
        f"out={result.output_tokens} price=${price} ({source})"
    )
    return ok


async def _embed(router) -> bool:
    profile = router.embedding_profile()
    started = time.perf_counter()
    try:
        vectors = await router.embed(["Section 318 BNS cheating"], "query")
    except Exception as exc:
        print(f"  {profile}: FAILED — {str(exc)[:200]}")
        return False
    print(f"  {profile}: ok dims={len(vectors[0])} ms={round((time.perf_counter() - started) * 1000)}")
    return True


async def _run(args) -> int:
    from app.ai.usage import capture_usage
    from app.llm.config import parse_profile
    from app.llm.router import ModelRouter

    router = ModelRouter()
    if args.profile:
        profiles = [parse_profile(p) for p in args.profile]
    else:
        seen, profiles = set(), []
        for p in router.routing.all_profiles():
            if str(p) not in seen:
                seen.add(str(p))
                profiles.append(p)
    print(f"Generation ({len(profiles)} profile(s)):")
    ok = True
    with capture_usage():
        for profile in profiles:
            ok &= await _one(router, profile)
        if args.embed:
            print("Embedding:")
            ok &= await _embed(router)
    print("All profiles OK." if ok else "Some profiles FAILED.")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", action="append", help="profile string; repeatable")
    ap.add_argument("--embed", action="store_true", help="also test the embedding profile")
    sys.exit(asyncio.run(_run(ap.parse_args())))


if __name__ == "__main__":
    main()
