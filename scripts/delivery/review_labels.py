"""Decide ``needs-human-review`` for an agent PR (SIM-153, SIM-71 AC-33).

    git diff --name-only origin/development...HEAD | \\
        python scripts/delivery/review_labels.py [--agent-flag]

The label is forced when the diff touches a path on the design's list
(``.claude/rules/pr.md``: socket payloads, role gate, purge, PII, billing,
model routing, plus the pipeline itself), OR'd with the agent's own flag.
Prints ``{"needsHumanReview": bool, "matched": [...]}``. No network.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, List, Tuple

#: Exact files.
SENSITIVE_FILES = (
    "app/socket/schemas.py",
    "app/langgraph/role_agent.py",
    "app/socket/handlers/workspace_purge.py",
    "app/workspace/persistence.py",
    "app/workspace/participants.py",
    "app/processing/pii_detector.py",
    "app/utils/redact.py",
    "app/ai/usage.py",
    "app/ai/pricing.py",
)
#: Whole trees (``<dir>/**``).
SENSITIVE_DIRS = ("app/llm/", ".claude/", ".github/", "scripts/deploy/")


def _normalise(path: str) -> str:
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def needs_human_review(paths: Iterable[str], agent_flag: bool = False) -> Tuple[bool, List[str]]:
    """Whether the PR needs a human reviewer, and the sensitive paths it touches."""
    matched = sorted(
        {
            p
            for p in (_normalise(raw) for raw in paths)
            if p and (p in SENSITIVE_FILES or p.startswith(SENSITIVE_DIRS))
        }
    )
    return bool(agent_flag or matched), matched


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-flag", action="store_true", help="the agent asked for a human review")
    args = parser.parse_args(argv)
    needed, matched = needs_human_review(sys.stdin.read().splitlines(), agent_flag=args.agent_flag)
    print(json.dumps({"needsHumanReview": needed, "matched": matched}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
