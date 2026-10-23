"""Fixed-format delivery notices in Slack (SIM-153, SIM-71 AC-37).

    python scripts/delivery/notify_slack.py post --issue SIM-<n> \\
        --stage spec|implement|review --outcome ready|blocked|failed \\
        [--pr-url https://github.com/CognitiveEight/Pracsys-ai/pull/<n>] \\
        [--run-url https://github.com/CognitiveEight/Pracsys-ai/actions/runs/<id>] \\
        [--count name=<int> ...] [--cost <usd>] [--reason-category <category>] [--dry-run]

A notice is built only from those structured fields, each validated, so no
text from a ticket, Slack or the model can reach it. It is posted top-level to
``DELIVERY_CHANNEL_ID`` with ``SLACK_BOT_TOKEN``; there is no channel or text
argument. ``jira_ops.py comment`` uses the same field validation. Standard
library only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

_API = "https://slack.com/api/"
_KEY = re.compile(r"^SIM-[0-9]+$")
_PR_URL = re.compile(r"^https://github\.com/CognitiveEight/Pracsys-ai/pull/([0-9]+)$")
_RUN_URL = re.compile(r"^https://github\.com/CognitiveEight/Pracsys-ai/actions/runs/[0-9]+(/job/[0-9]+)?$")
_COUNT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_COUNTS = 10
_MAX_COST = 10_000.0

STAGES = ("spec", "implement", "review")
OUTCOMES = ("ready", "blocked", "failed")
REASON_CATEGORIES = (
    "dor-gap",
    "tests-red",
    "review-changes",
    "injection",
    "workflow-failed",
    "budget",
    "already-ran",
    "protected-path",
    "secret-found",
)


class DeliveryRefused(Exception):
    """A request the delivery helpers will not carry out."""


def validate_fields(
    *,
    issue: Any,
    stage: Any,
    outcome: Any,
    pr_url: Any = None,
    run_url: Any = None,
    counts: Optional[Mapping[str, Any]] = None,
    cost: Any = None,
    reason_category: Any = None,
) -> Dict[str, Any]:
    """The fields of a notice or comment, checked; refused if any is not as expected."""
    if not isinstance(issue, str) or not _KEY.fullmatch(issue):
        raise DeliveryRefused("issue key must match ^SIM-[0-9]+$")
    if stage not in STAGES:
        raise DeliveryRefused(f"stage must be one of {list(STAGES)}")
    if outcome not in OUTCOMES:
        raise DeliveryRefused(f"outcome must be one of {list(OUTCOMES)}")
    pr_number = None
    if pr_url is not None:
        match = _PR_URL.fullmatch(pr_url) if isinstance(pr_url, str) else None
        if not match:
            raise DeliveryRefused("PR URL must be https://github.com/CognitiveEight/Pracsys-ai/pull/<n>")
        pr_number = match.group(1)
    if run_url is not None and not (isinstance(run_url, str) and _RUN_URL.fullmatch(run_url)):
        raise DeliveryRefused("run URL must be a GitHub Actions run of CognitiveEight/Pracsys-ai")
    clean_counts: Dict[str, int] = {}
    for name, value in (counts or {}).items():
        if not isinstance(name, str) or not _COUNT_NAME.fullmatch(name):
            raise DeliveryRefused("count names are short identifiers")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DeliveryRefused("counts are non-negative integers")
        clean_counts[name] = value
    if len(clean_counts) > _MAX_COUNTS:
        raise DeliveryRefused(f"at most {_MAX_COUNTS} counts")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise DeliveryRefused("cost must be a number (USD)")
        if not math.isfinite(cost) or not 0 <= cost < _MAX_COST:
            raise DeliveryRefused("cost out of range")
    if reason_category is not None and reason_category not in REASON_CATEGORIES:
        raise DeliveryRefused(f"reason category must be one of {list(REASON_CATEGORIES)}")
    return {
        "issue": issue,
        "stage": stage,
        "outcome": outcome,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "run_url": run_url,
        "counts": clean_counts,
        "cost": None if cost is None else float(cost),
        "reason_category": reason_category,
    }


def parse_counts(pairs: Optional[List[str]]) -> Dict[str, int]:
    """``["name=3", ...]`` from the command line as a dict; refused if malformed."""
    out: Dict[str, int] = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[0-9]{1,9}", value):
            raise DeliveryRefused("--count takes name=<non-negative integer>")
        out[name] = int(value)
    return out


def build_notice(
    *,
    issue: str,
    stage: str,
    outcome: str,
    pr_url: Optional[str] = None,
    run_url: Optional[str] = None,
    counts: Optional[Mapping[str, int]] = None,
    cost: Optional[float] = None,
    reason_category: Optional[str] = None,
) -> str:
    """One line of Slack text from validated fields only."""
    f = validate_fields(
        issue=issue, stage=stage, outcome=outcome, pr_url=pr_url, run_url=run_url,
        counts=counts, cost=cost, reason_category=reason_category,
    )
    parts = [f["issue"], f["stage"], f["outcome"]]
    if f["reason_category"]:
        parts.append(f"reason: {f['reason_category']}")
    if f["pr_url"]:
        parts.append(f"<{f['pr_url']}|PR #{f['pr_number']}>")
    if f["run_url"]:
        parts.append(f"<{f['run_url']}|run>")
    parts.extend(f"{name}={value}" for name, value in f["counts"].items())
    if f["cost"] is not None:
        parts.append(f"cost ${f['cost']:.2f}")
    return " · ".join(parts)


def _config() -> Dict[str, str]:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("DELIVERY_CHANNEL_ID", "").strip()
    if not token or not channel:
        raise DeliveryRefused("SLACK_BOT_TOKEN and DELIVERY_CHANNEL_ID must be set")
    return {"token": token, "channel": channel}


def _request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    token = _config()["token"]
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(
        _API + method,
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Slack host
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {body.get('error', 'unknown_error')}")
    return body


def post(**fields: Any) -> Dict[str, Any]:
    """Post the notice for ``fields`` (see ``build_notice``) top-level in the delivery channel."""
    text = build_notice(**fields)
    channel = _config()["channel"]
    body = _request(
        "chat.postMessage",
        {"channel": channel, "text": text, "unfurl_links": "false", "unfurl_media": "false"},
    )
    return {"ts": body.get("ts", "")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    post_p = sub.add_parser("post", help="post one fixed-format notice")
    post_p.add_argument("--issue", required=True)
    post_p.add_argument("--stage", required=True)
    post_p.add_argument("--outcome", required=True)
    post_p.add_argument("--pr-url")
    post_p.add_argument("--run-url")
    post_p.add_argument("--count", action="append", default=[], metavar="NAME=INT")
    post_p.add_argument("--cost", type=float)
    post_p.add_argument("--reason-category")
    post_p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fields = dict(
            issue=args.issue, stage=args.stage, outcome=args.outcome, pr_url=args.pr_url,
            run_url=args.run_url, counts=parse_counts(args.count), cost=args.cost,
            reason_category=args.reason_category,
        )
        result = {"text": build_notice(**fields)} if args.dry_run else post(**fields)
    except DeliveryRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
