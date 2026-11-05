#!/usr/bin/env python3
"""
Reconcile each case's model cost in SimplerToday with gate0 (LiteLLM).

Reads gate0's per-request spend logs, keeps the requests tagged
``app:<GATE0_APP_NAME>``, sums them per ``workspace_id:<id>`` tag, and compares
each sum with the workspace's model-cost total in SimplerToday
(``totalInputPrice + totalOutputPrice`` in the persisted WORKSPACE_META cache).
A mismatch means a model call whose cost never reached its case.

Only MODEL cost is compared: OCR, Speech-to-Text, Vertex AI Search and
non-gate0 embeddings never pass through gate0, so they appear only in
SimplerToday's ``totalServicePrice``. Totals that started before gate0 was in
use also include direct-Gemini spend, which gate0 never saw; compare those
workspaces from a fresh start, or read the "SimplerToday" column as an upper
bound for them.

Needs a gate0 key that may read spend logs (the master key, or an admin key),
read from the environment variable named by --key-env (never a CLI argument,
so it does not land in shell history). Read-only on both sides.

USAGE
    GATE0_ADMIN_KEY=... python scripts/reconcile_gate0_spend.py
    python scripts/reconcile_gate0_spend.py --since 2026-10-01 --tolerance 0.001
    python scripts/reconcile_gate0_spend.py --workspace <id> --by-feature
"""

import argparse
import asyncio
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _gateway_root(base_url: str) -> str:
    root = (base_url or "").rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _spend_rows(root: str, key: str, since: str, until: str) -> list:
    query = urllib.parse.urlencode(
        {"start_date": since, "end_date": until, "summarize": "false"}
    )
    req = urllib.request.Request(
        f"{root}/spend/logs?{query}", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosec: operator-configured URL
        data = json.loads(resp.read())
    return data if isinstance(data, list) else data.get("data", [])


def _tags(row: dict) -> list:
    tags = row.get("request_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = [tags]
    return [str(t) for t in tags]


def _tag_value(tags: list, prefix: str) -> str:
    return next((t.split(":", 1)[1] for t in tags if t.startswith(prefix + ":")), "")


async def _simplertoday_totals(workspace_ids) -> dict:
    from app.workspace.persistence import CacheKinds, restore_state

    out = {}
    for ws in workspace_ids:
        data = await restore_state(ws, CacheKinds.WORKSPACE_META)
        meta = (data or {}).get("metadata") if isinstance(data, dict) else None
        totals = (meta or {}).get("token_usage") or {}
        out[ws] = {
            "model": float(totals.get("totalInputPrice", 0.0) or 0.0)
            + float(totals.get("totalOutputPrice", 0.0) or 0.0),
            "service": float(totals.get("totalServicePrice", 0.0) or 0.0),
            "found": bool(totals),
        }
    return out


def main() -> int:
    from app.core.config import settings

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=str(date.today() - timedelta(days=30)))
    ap.add_argument("--until", default=str(date.today() + timedelta(days=1)))
    ap.add_argument("--key-env", default="GATE0_ADMIN_KEY",
                    help="environment variable holding a gate0 key that can read spend logs")
    ap.add_argument("--base-url", default=settings.gate0_base_url or "")
    ap.add_argument("--workspace", help="only this workspace id")
    ap.add_argument("--by-feature", action="store_true", help="break gate0 spend down by feature")
    ap.add_argument("--tolerance", type=float, default=0.0005, help="USD difference treated as equal")
    args = ap.parse_args()

    key = os.environ.get(args.key_env)
    if not key:
        print(f"Set {args.key_env} to a gate0 key that can read /spend/logs.")
        return 2
    if not args.base_url:
        print("Set GATE0_BASE_URL (or pass --base-url).")
        return 2

    rows = _spend_rows(_gateway_root(args.base_url), key, args.since, args.until)
    app_tag = f"app:{settings.gate0_app_name}"
    spend = collections.defaultdict(float)
    calls = collections.Counter()
    features = collections.defaultdict(lambda: collections.defaultdict(float))
    untagged = 0.0
    for row in rows:
        tags = _tags(row)
        if app_tag not in tags:
            continue
        cost = float(row.get("spend") or 0.0)
        ws = _tag_value(tags, "workspace_id")
        if not ws:
            untagged += cost
            continue
        if args.workspace and ws != args.workspace:
            continue
        spend[ws] += cost
        calls[ws] += 1
        features[ws][_tag_value(tags, "feature") or "-"] += cost

    ours = asyncio.run(_simplertoday_totals(sorted(spend)))
    print(f"gate0 spend {args.since} .. {args.until}, tagged {app_tag}\n")
    print(f"{'workspace':40s} {'calls':>5s} {'gate0 $':>10s} {'SimplerToday $':>15s} {'diff $':>9s}  status")
    mismatches = 0
    for ws in sorted(spend, key=lambda w: -spend[w]):
        mine = ours.get(ws, {})
        diff = mine.get("model", 0.0) - spend[ws]
        if not mine.get("found"):
            status = "no SimplerToday totals"
        elif abs(diff) <= args.tolerance:
            status = "OK"
        else:
            status = "MISMATCH"
            mismatches += 1
        print(f"{ws:40s} {calls[ws]:5d} {spend[ws]:10.4f} {mine.get('model', 0.0):15.4f} {diff:9.4f}  {status}")
        if args.by_feature:
            for feat, cost in sorted(features[ws].items(), key=lambda kv: -kv[1]):
                print(f"    {feat:44s} {cost:10.4f}")
    if untagged:
        print(f"\n{app_tag} spend with no workspace tag (scripts, startup work): ${untagged:.4f}")
    print(f"\n{mismatches} mismatch(es). Non-model services (OCR, STT, search) are not in gate0.")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
