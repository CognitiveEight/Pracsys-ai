"""What the agent stages hand back, checked before anything is published (SIM-153, SIM-71 tasks 17-18).

    python scripts/delivery/stage_io.py outcome --stage spec|implement --issue SIM-<n> --file <outcome.json>
    python scripts/delivery/stage_io.py cost --file <claude-execution-output.json>
    git diff --name-only origin/development...<branch> | \\
        python scripts/delivery/stage_io.py spec-paths --issue SIM-<n>
    git show <branch>:<design.md> | python scripts/delivery/stage_io.py design-status
    python scripts/delivery/stage_io.py design-gate --branch <ref name> --before <sha> --after <sha> \\
        --base origin/development [--repo .]
    python scripts/delivery/stage_io.py decide --outcome <normalised.json> \\
        --lint-rc <n> --test-rc <n> --commits <n>
    git diff --name-only origin/development...<branch> | \\
        python scripts/delivery/stage_io.py pr-comment --issue SIM-<n> --outcome <normalised.json> \\
        --lint-rc <n> --test-rc <n> --commits <n> [--agent-flag] [--cost <usd>]

The agent's outcome file is untrusted: ``outcome`` keeps only the fields the
design names, each checked, and turns anything unexpected into
``blocked`` / ``workflow-failed``. ``cost`` reads only the result message's
cost from the action's execution log (which also holds everything the agent
read, so the log itself is never published). ``spec-paths`` confines the spec
stage to its own three spec files. ``design-gate`` decides whether a push is
the G2 approval: the ticket's technical design flipped to ``Status: approved``
in this push, by a commit the agent did not author, on a branch that does not
change ``.claude/`` or ``.github/``. ``decide`` and ``pr-comment`` give the
implement stage's verdict and PR comment from those fields only. Standard
library only; prints JSON (``pr-comment`` prints markdown).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import guard_branch  # noqa: E402  (sibling script)
import notify_slack  # noqa: E402  (sibling script)
import review_labels  # noqa: E402  (sibling script)

_MAX_OUTCOME_BYTES = 64_000
_MAX_AC = 100
_AC_ID = re.compile(r"^AC-[0-9]{1,4}[a-z]?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ZERO_SHA = "0" * 40
_SPEC_PATH = r"^specs/(requirements|technical-design|acceptance-criteria)/{key}-[a-z0-9][a-z0-9-]*\.md$"
_STATUS_LINE = re.compile(r"^\s*(?:[-*]\s+)?Status\s*:\s*(.*)$")
#: Identities the agent commits under: the workflow's git identity, and the
#: action's default bot identity in case the environment override is lost.
AGENT_IDENTITIES = (
    "pracsys-agent@users.noreply.github.com",
    "41898282+claude[bot]@users.noreply.github.com",
)
#: A ticket branch touching these is not run by the agent: they hold the
#: instructions, hooks and workflows the next agent run would execute under.
PROTECTED_PREFIXES = (".claude/", ".github/")
VERDICTS = ("approve", "approve-with-follow-ups", "request-changes")
STATUSES = ("ready", "blocked")
STAGES = ("spec", "implement")


class StageRefused(Exception):
    """Input the stage helpers will not use."""


# ----------------------------------------------------------------- outcome


def _failed(stage: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"valid": False, "status": "blocked", "reasonCategory": "workflow-failed",
                           "needsHumanReview": True}
    if stage == "spec":
        out.update(branch=None, openQuestions=0)
    else:
        out.update(verdict=None, e2eRequired=False, acResults=[])
    return out


def _bool(data: Dict[str, Any], name: str, default: bool) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise StageRefused(f"{name} must be true or false")
    return value


def _category(data: Dict[str, Any], status: str) -> Optional[str]:
    value = data.get("reasonCategory")
    if value is None:
        return "dor-gap" if status == "blocked" else None
    if value not in notify_slack.REASON_CATEGORIES:
        raise StageRefused("unknown reason category")
    return value


def _validate(stage: str, data: Any, issue: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise StageRefused("outcome must be a JSON object")
    status = data.get("status")
    if status not in STATUSES:
        raise StageRefused("status must be ready or blocked")
    out: Dict[str, Any] = {"valid": True, "status": status}
    if stage == "spec":
        branch = data.get("branch")
        if status == "ready" or branch is not None:
            try:
                branch = guard_branch.check(branch if isinstance(branch, str) else "", issue)
            except guard_branch.GuardRefused as refused:
                raise StageRefused(str(refused)) from None
        questions = data.get("openQuestions", 0)
        if isinstance(questions, bool) or not isinstance(questions, int) or not 0 <= questions <= 999:
            raise StageRefused("openQuestions must be a count")
        out.update(branch=branch, reasonCategory=_category(data, status), openQuestions=questions,
                   needsHumanReview=_bool(data, "needsHumanReview", False))
        return out
    verdict = data.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        raise StageRefused("unknown verdict")
    if status == "ready" and verdict is None:
        raise StageRefused("a ready outcome needs a verdict")
    results = data.get("acResults", [])
    if not isinstance(results, list) or len(results) > _MAX_AC:
        raise StageRefused("acResults must be a short list")
    clean = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) \
                or not _AC_ID.fullmatch(item["id"]) or not isinstance(item.get("pass"), bool):
            raise StageRefused("each acResult is {id: AC-<n>, pass: bool}")
        clean.append({"id": item["id"], "pass": item["pass"]})
    out.update(verdict=verdict, needsHumanReview=_bool(data, "needsHumanReview", False),
               e2eRequired=_bool(data, "e2eRequired", False), reasonCategory=_category(data, status),
               acResults=clean)
    return out


def read_outcome(stage: str, path: str, issue: str) -> Dict[str, Any]:
    """The outcome file at ``path``, normalised; a blocked/workflow-failed outcome if it is
    missing, too big or not exactly as the design describes."""
    if stage not in STAGES:
        raise StageRefused("stage must be spec or implement")
    guard_branch.validate_key(issue)
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_OUTCOME_BYTES:
            raise StageRefused("outcome file too large")
        return _validate(stage, json.loads(raw.decode("utf-8")), issue)
    except (OSError, ValueError, StageRefused):
        return _failed(stage)


# -------------------------------------------------------------------- cost


def run_cost(path: str) -> Optional[float]:
    """USD cost from the last result message of the action's execution log, or None."""
    try:
        messages = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("type") == "result":
            for name in ("total_cost_usd", "cost_usd"):
                value = message.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and math.isfinite(value) and 0 <= value < 10_000:
                    return round(float(value), 4)
            return None
    return None


# --------------------------------------------------------------- spec paths


def check_spec_paths(issue: str, paths: Iterable[str]) -> Tuple[bool, List[str], Optional[str]]:
    """Whether a spec-stage diff touches only ``issue``'s own spec files."""
    key = guard_branch.validate_key(issue)
    pattern = re.compile(_SPEC_PATH.format(key=re.escape(key)))
    clean = [p.strip() for p in paths if p and p.strip()]
    if not clean:
        return False, [], "no spec files changed"
    if any(not pattern.fullmatch(p) or ".." in p for p in clean):
        return False, [], "the spec stage may only change its own files under specs/"
    return True, clean, None


# ----------------------------------------------------------- design status


def design_approved(text: str) -> bool:
    """True if the design's first ``Status:`` line (in its header) starts with ``approved``."""
    for line in text.splitlines()[:30]:
        match = _STATUS_LINE.match(line.replace("*", "").replace("_", ""))
        if match:
            return match.group(1).strip().lower().startswith("approved")
    return False


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout


def _exists(repo: str, rev: str) -> bool:
    return subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", rev], capture_output=True
    ).returncode == 0


def design_gate(branch: str, before: str, after: str, base: str, repo: str = ".") -> Dict[str, Any]:
    """Whether the push ``before``..``after`` to ``branch`` is the G2 approval."""
    match = re.fullmatch(r"(?:feature|fix)/(SIM-[0-9]+)-[a-z0-9][a-z0-9-]*", branch or "")
    if not match:
        return {"go": False, "reason": "not-ticket-branch", "issue": None, "branch": None}
    issue = match.group(1)
    for sha in (before, after):
        if not _SHA.fullmatch(sha or ""):
            raise StageRefused("before and after must be 40-hex commit ids")
    result = {"go": False, "issue": issue, "branch": guard_branch.check(branch, issue)}

    prefix = f"specs/technical-design/{issue}-"
    listing = _git(repo, "ls-tree", "--name-only", after, "specs/technical-design/").splitlines()
    designs = [p for p in listing if p.startswith(prefix) and p.endswith(".md")]
    if len(designs) != 1:
        return {**result, "reason": "no-design"}
    design = designs[0]
    if not design_approved(_git(repo, "show", f"{after}:{design}")):
        return {**result, "reason": "not-approved"}
    if before != _ZERO_SHA:
        if not _exists(repo, before):
            return {**result, "reason": "before-unavailable"}
        if _exists(repo, f"{before}:{design}") and design_approved(_git(repo, "show", f"{before}:{design}")):
            return {**result, "reason": "already-approved"}
    author = _git(repo, "log", "-1", "--format=%ae%n%ce", after).split()
    if any(identity in AGENT_IDENTITIES for identity in author):
        return {**result, "reason": "agent-commit"}
    changed = _git(repo, "diff", "--name-only", f"{base}...{after}").splitlines()
    if any(p.startswith(PROTECTED_PREFIXES) for p in changed):
        return {**result, "reason": "protected-path"}
    return {**result, "go": True, "reason": "approved", "design": design}


# ---------------------------------------------------------------- decision


def implement_decision(outcome: Dict[str, Any], lint_rc: Optional[int], test_rc: Optional[int],
                       commits: int) -> Dict[str, Any]:
    """Ready for review only with green gates, an approving verdict and every AC passing."""
    if not outcome.get("valid"):
        return {"ready": False, "reasonCategory": "workflow-failed"}
    if outcome.get("status") != "ready":
        return {"ready": False, "reasonCategory": outcome.get("reasonCategory") or "dor-gap"}
    if lint_rc != 0 or test_rc != 0 or not all(r["pass"] for r in outcome.get("acResults", [])):
        return {"ready": False, "reasonCategory": "tests-red"}
    if outcome.get("verdict") not in ("approve", "approve-with-follow-ups"):
        return {"ready": False, "reasonCategory": "review-changes"}
    if commits <= 0:
        return {"ready": False, "reasonCategory": "workflow-failed"}
    return {"ready": True, "reasonCategory": None}


def _gate(rc: Optional[int]) -> str:
    return "green" if rc == 0 else "red"


def pr_comment(*, issue: str, outcome: Dict[str, Any], decision: Dict[str, Any],
               lint_rc: Optional[int], test_rc: Optional[int], needs_human_review: bool,
               matched: List[str], cost: Optional[float]) -> str:
    """The implement stage's one PR comment, from validated fields only."""
    guard_branch.validate_key(issue)
    for path in matched:
        if path not in review_labels.SENSITIVE_FILES and not path.startswith(review_labels.SENSITIVE_DIRS):
            raise StageRefused("matched paths must be on the sensitive-path list")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", path):
            raise StageRefused("unexpected characters in a path")
    category = decision.get("reasonCategory")
    if category is not None and category not in notify_slack.REASON_CATEGORIES:
        raise StageRefused("unknown reason category")
    yes = {True: "yes", False: "no"}
    lines = [
        f"### Agent pipeline: {issue} implement stage",
        "",
        f"- Outcome: {'ready for review' if decision.get('ready') else 'blocked, kept as draft'}"
        + (f" (reason: {category})" if category else ""),
        f"- Review verdict: {outcome.get('verdict') or 'none'}",
        f"- `make lint`: {_gate(lint_rc)}; `make test`: {_gate(test_rc)}",
        f"- needs-human-review: {yes[bool(needs_human_review)]}",
        f"- `make e2e` required before deploy: {yes[bool(outcome.get('e2eRequired'))]}"
        " (a human runs it; it is never run in CI)",
    ]
    if cost is not None:
        lines.append(f"- Agent cost: ${cost:.2f}")
    if matched:
        lines.append("- Sensitive paths touched: " + ", ".join(f"`{p}`" for p in matched))
    results = outcome.get("acResults") or []
    if results:
        lines += ["", "| AC | Result |", "|---|---|"]
        lines += [f"| {r['id']} | {'pass' if r['pass'] else 'fail'} |" for r in results]
    lines += ["", "Merging stays with a human reviewer (G3)."]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- CLI


def _rc(value: str) -> Optional[int]:
    return int(value) if re.fullmatch(r"[0-9]{1,3}", value or "") else None


def _load_outcome(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StageRefused("normalised outcome must be an object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("outcome")
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--issue", required=True)
    p.add_argument("--file", required=True)
    p = sub.add_parser("cost")
    p.add_argument("--file", required=True)
    p = sub.add_parser("spec-paths")
    p.add_argument("--issue", required=True)
    sub.add_parser("design-status")
    p = sub.add_parser("design-gate")
    p.add_argument("--branch", required=True)
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--repo", default=".")
    for name in ("decide", "pr-comment"):
        p = sub.add_parser(name)
        p.add_argument("--outcome", required=True)
        p.add_argument("--lint-rc", default="")
        p.add_argument("--test-rc", default="")
        p.add_argument("--commits", default="0")
        if name == "pr-comment":
            p.add_argument("--issue", required=True)
            p.add_argument("--agent-flag", action="store_true")
            p.add_argument("--cost", type=float)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "outcome":
            result: Any = read_outcome(args.stage, args.file, args.issue)
        elif args.command == "cost":
            result = {"cost": run_cost(args.file)}
        elif args.command == "spec-paths":
            ok, paths, reason = check_spec_paths(args.issue, sys.stdin.read().splitlines())
            result = {"ok": ok, "paths": paths, "reason": reason}
        elif args.command == "design-status":
            result = {"approved": design_approved(sys.stdin.read())}
        elif args.command == "design-gate":
            result = design_gate(args.branch, args.before, args.after, args.base, args.repo)
        else:
            outcome = _load_outcome(args.outcome)
            commits = int(args.commits) if re.fullmatch(r"[0-9]{1,6}", args.commits) else 0
            decision = implement_decision(outcome, _rc(args.lint_rc), _rc(args.test_rc), commits)
            if args.command == "decide":
                result = decision
            else:
                needed, matched = review_labels.needs_human_review(
                    sys.stdin.read().splitlines(),
                    agent_flag=args.agent_flag or bool(outcome.get("needsHumanReview")),
                )
                print(pr_comment(issue=args.issue, outcome=outcome, decision=decision,
                                 lint_rc=_rc(args.lint_rc), test_rc=_rc(args.test_rc),
                                 needs_human_review=needed, matched=matched, cost=args.cost), end="")
                return 0
    except (StageRefused, guard_branch.GuardRefused, subprocess.CalledProcessError, OSError,
            ValueError) as refused:
        print(f"refused: {type(refused).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
