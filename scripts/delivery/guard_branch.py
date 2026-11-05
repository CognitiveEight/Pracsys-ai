"""Branch guard for the agent publish steps (SIM-153, SIM-71 AC-21/23/27).

    python scripts/delivery/guard_branch.py check --branch <name> --issue SIM-<n>
    python scripts/delivery/guard_branch.py key --issue SIM-<n>
    python scripts/delivery/guard_branch.py already-ran --issue SIM-<n>
        # stdin: {"branches": [...remote branch names], "prs": [...open PR heads or titles],
        #         "agentPrs": [...heads or titles of open PRs labelled agent-pr]}

``check`` allows a push only to the run's own ticket branch,
``feature/SIM-<n>-<slug>`` or ``fix/SIM-<n>-<slug>``, and never to
``development``, ``staging`` or ``main``. ``already-ran`` tells the spec stage
whether a branch or open PR for the key exists, so a second run changes
nothing, and whether one of those is an open ``agent-pr`` PR (then the
earlier run is still in hand and the issue is not flagged Blocked). ``key`` only checks an issue key (a workflow input) and echoes it. No network and no credentials. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List

_KEY = re.compile(r"^SIM-([0-9]+)$")
_BRANCH = re.compile(r"^(feature|fix)/SIM-([0-9]+)-[a-z0-9][a-z0-9-]*$")
PROTECTED = ("development", "staging", "main")


class GuardRefused(Exception):
    """A branch or key the publish steps will not use."""


def validate_key(key: Any) -> str:
    """``key`` if it is exactly ``SIM-<n>``; refused otherwise."""
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise GuardRefused("issue key must match ^SIM-[0-9]+$")
    return key


def _number(key: str) -> str:
    return _KEY.fullmatch(validate_key(key)).group(1)


def check(branch: str, issue: str) -> str:
    """``branch`` if it is ``issue``'s own ticket branch; refused otherwise."""
    number = _number(issue)
    if branch in PROTECTED:
        raise GuardRefused(f"'{branch}' is protected; the pipeline never pushes to it")
    match = _BRANCH.fullmatch(branch or "")
    if not match:
        raise GuardRefused("branch must be feature/SIM-<n>-<slug> or fix/SIM-<n>-<slug>")
    if match.group(2) != number:
        raise GuardRefused("branch belongs to another issue")
    return branch


def _branch_name(ref: str) -> str:
    ref = (ref or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def already_ran(issue: str, remote_branches: Iterable[str], open_prs: Iterable[str]) -> bool:
    """True if a ticket branch for ``issue`` exists, or an open PR head or
    title carries its key."""
    number = _number(issue)
    prefixes = (f"feature/SIM-{number}-", f"fix/SIM-{number}-")
    if any(_branch_name(b).startswith(prefixes) for b in remote_branches):
        return True
    carries_key = re.compile(rf"(?<![A-Za-z0-9])SIM-{number}(?![0-9])")
    return any(carries_key.search(pr or "") for pr in open_prs)


def open_agent_pr(issue: str, agent_prs: Iterable[str]) -> bool:
    """True if an open PR labelled ``agent-pr`` carries ``issue``'s key in its head or title."""
    number = _number(issue)
    carries_key = re.compile(rf"(?<![A-Za-z0-9])SIM-{number}(?![0-9])")
    return any(carries_key.search(pr or "") for pr in agent_prs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_p = sub.add_parser("check", help="refuse anything but the issue's own ticket branch")
    check_p.add_argument("--branch", required=True)
    check_p.add_argument("--issue", required=True)
    key_p = sub.add_parser("key", help="check an issue key and echo it")
    key_p.add_argument("--issue", required=True)
    ran_p = sub.add_parser("already-ran", help="branches and PRs as JSON on stdin")
    ran_p.add_argument("--issue", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            result: Dict[str, Any] = {"ok": True, "branch": check(args.branch, args.issue)}
        elif args.command == "key":
            result = {"issue": validate_key(args.issue)}
        else:
            validate_key(args.issue)
            data = json.loads(sys.stdin.read() or "{}")
            agent_prs = [str(p) for p in data.get("agentPrs", [])]
            result = {
                "alreadyRan": already_ran(
                    args.issue,
                    [str(b) for b in data.get("branches", [])],
                    [str(p) for p in data.get("prs", [])] + agent_prs,
                ),
                "openAgentPr": open_agent_pr(args.issue, agent_prs),
            }
    except GuardRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
