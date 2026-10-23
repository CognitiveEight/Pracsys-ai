"""Pick the next SIM tickets for the agent pipeline (SIM-153, SIM-71 AC-16/17/18).

    AGENTS_ENABLED=true AGENT_MAX_CONCURRENT=1 AGENT_RUNNING=<n> \\
    OPEN_SPEC_PR_KEYS=SIM-1,SIM-2 EXISTING_TICKET_BRANCH_KEYS=SIM-3 \\
    python scripts/delivery/agent_queue.py next

Prints ``{"start": ["SIM-<n>", ...]}``: the tickets labelled ``agent-ready``
and ``ai-repo`` to start the spec stage for, P1 → P4 then oldest first, at
most ``AGENT_MAX_CONCURRENT - AGENT_RUNNING`` of them. Nothing when
``AGENTS_ENABLED`` is not exactly ``true`` (Jira is not even queried). An
issue in Spec with an open spec PR is waiting for G2 and is skipped, and so is
any issue that already has a ``feature/SIM-<n>-*`` / ``fix/SIM-<n>-*`` branch
(``EXISTING_TICKET_BRANCH_KEYS``): the spec stage would only report
``already-ran`` for it. It starts
no workflow itself; ``agent-dispatch.yml`` does. Credentials:
``JIRA_EMAIL`` / ``JIRA_API_TOKEN``. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

JQL = (
    "project = SIM AND labels = agent-ready AND labels = ai-repo AND labels not in "
    '(agent-running, agent-blocked) AND status in ("To Do", Spec) ORDER BY priority DESC, created ASC'
)
_KEY = re.compile(r"^SIM-[0-9]+$")
_FIELDS = ["priority", "created", "labels", "status"]
_RANK = {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5}
_REQUIRED_LABELS = {"agent-ready", "ai-repo"}
_SKIP_LABELS = {"agent-running", "agent-blocked"}
_STATUSES = {"To Do", "Spec"}
_LATEST = datetime.max.replace(tzinfo=timezone.utc)


class QueueRefused(Exception):
    """A queue request the helper will not carry out."""


def _site() -> str:
    return os.environ.get("JIRA_SITE", "simplertoday.atlassian.net").strip()


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        raise QueueRefused("JIRA_EMAIL and JIRA_API_TOKEN must be set")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(
        f"https://{_site()}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Jira host
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def _created(stamp: Any) -> datetime:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return _LATEST


def _order(issue: Dict[str, Any]) -> Tuple[int, datetime]:
    fields = issue.get("fields") or {}
    rank = _RANK.get((fields.get("priority") or {}).get("name", ""), _RANK["Medium"])
    return rank, _created(fields.get("created"))


def _eligible(issue: Dict[str, Any], open_spec_pr_keys: Iterable[str]) -> bool:
    key = issue.get("key", "")
    fields = issue.get("fields") or {}
    labels = set(fields.get("labels") or [])
    status = (fields.get("status") or {}).get("name", "")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return False
    if not _REQUIRED_LABELS <= labels or labels & _SKIP_LABELS:
        return False
    if status not in _STATUSES:
        return False
    return not (status == "Spec" and key in open_spec_pr_keys)


def select(
    issues: List[Dict[str, Any]],
    *,
    running: int,
    max_concurrent: int,
    enabled: bool,
    open_spec_pr_keys: Iterable[str],
    branch_keys: Iterable[str] = (),
) -> List[str]:
    """Keys to start now, in queue order; empty when disabled or at the limit."""
    if enabled is not True:
        return []
    room = max_concurrent - running
    if room <= 0:
        return []
    waiting = set(open_spec_pr_keys)
    has_branch = set(branch_keys)
    ready = sorted(
        (i for i in issues if _eligible(i, waiting) and i.get("key") not in has_branch), key=_order
    )
    return [i["key"] for i in ready[:room]]


def _env_int(name: str, default: Optional[int]) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is None:
            raise QueueRefused(f"{name} must be set")
        return default
    if not re.fullmatch(r"[0-9]{1,4}", raw):
        raise QueueRefused(f"{name} must be a non-negative integer")
    return int(raw)


def _env_keys(name: str) -> Set[str]:
    return {k.strip() for k in os.environ.get(name, "").split(",") if _KEY.fullmatch(k.strip())}


def next_keys() -> List[str]:
    """Read the settings from the environment, query Jira, and select."""
    if os.environ.get("AGENTS_ENABLED") != "true":
        return []
    max_concurrent = _env_int("AGENT_MAX_CONCURRENT", 1)
    running = _env_int("AGENT_RUNNING", None)
    if running >= max_concurrent:
        return []
    open_keys = _env_keys("OPEN_SPEC_PR_KEYS")
    branch_keys = _env_keys("EXISTING_TICKET_BRANCH_KEYS")
    body = {"jql": JQL, "fields": _FIELDS, "maxResults": 50}
    issues = _request("POST", "/rest/api/3/search/jql", body).get("issues", [])
    return select(
        issues, running=running, max_concurrent=max_concurrent, enabled=True, open_spec_pr_keys=open_keys,
        branch_keys=branch_keys,
    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next", help="print the keys to start")
    parser.parse_args(argv)
    try:
        start = next_keys()
    except QueueRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps({"start": start}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
