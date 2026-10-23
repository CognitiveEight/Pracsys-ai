"""What the agent stages hand back, checked before anything is published (SIM-153, SIM-71 tasks 17-18).

    python scripts/delivery/stage_io.py outcome --stage spec|implement --issue SIM-<n> --file <outcome.json>
    python scripts/delivery/stage_io.py cost --file <result-summary.json>
    git diff --name-only origin/development...<branch> | \\
        python scripts/delivery/stage_io.py spec-paths --issue SIM-<n>
    git show <branch>:<design.md> | python scripts/delivery/stage_io.py design-status
    git log --no-renames --name-only --format= <range> | \\
        python scripts/delivery/stage_io.py agent-paths --stage spec|implement --issue SIM-<n>
    git log -p --no-renames --format= <range> | python scripts/delivery/stage_io.py allow-markers
    python scripts/delivery/stage_io.py decide --outcome <normalised.json> \\
        --lint-rc <n> --test-rc <n> --commits <n> [--budget] [--protected] [--secret] [--gates-mismatch]
    git diff --name-only origin/development...<branch> | \\
        python scripts/delivery/stage_io.py pr-comment --issue SIM-<n> --outcome <normalised.json> \\
        --ready true|false --reason <category> --lint-rc <n> --test-rc <n> [--agent-flag] [--cost <usd>]

The agent's outcome file is untrusted: ``outcome`` keeps only the fields the
design names, each checked, and turns anything unexpected into
``blocked`` / ``workflow-failed``. ``cost`` reads only the result message's
cost, and whether the run hit a spend limit, from the action's result summary
(the execution log holds everything the agent read, so it is never
published). ``spec-paths`` confines the spec stage to its own three spec
files; ``agent-paths`` is the publish jobs' refusal list (the implement stage
may not change the pipeline, the gates or any spec); ``allow-markers`` refuses
any agent-added ``gitleaks:allow``. ``design-status`` reads a design's header
``Status:`` line (the spec publish refuses a design the agent marked
approved). ``decide`` gives the implement stage's final verdict, reason and
whether to push; ``pr-comment`` renders that verdict (it never recomputes it).
G2 itself is ``g2_gate.py``. Standard library only; prints JSON
(``pr-comment`` prints markdown).
"""

from __future__ import annotations

import argparse
import json
import math
import re
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
_SPEC_PATH = r"^specs/(requirements|technical-design|acceptance-criteria)/{key}-[a-z0-9][a-z0-9-]*\.md$"
_STATUS_LINE = re.compile(r"^\s*(?:[-*]\s+)?Status\s*:\s*(.*)$")
_LIMIT_SUBTYPES = ("error_max_budget_usd",)
_ALLOW_MARKER = re.compile(r"gitleaks\s*:\s*allow", re.IGNORECASE)
#: Reasons on which publish pushes nothing (AC-39 for ``budget``).
NO_PUSH_REASONS = ("protected-path", "secret-found", "budget")
#: Identities the agent commits under: the workflow's git identity, and the
#: action's default bot identity in case the environment override is lost.
AGENT_IDENTITIES = (
    "pracsys-agent@users.noreply.github.com",
    "41898282+claude[bot]@users.noreply.github.com",
)
#: A ticket branch touching these is not run by the agent, and no agent commit
#: may touch them: they hold the instructions, hooks and workflows the next
#: agent run would execute under (``CLAUDE.md``, ``AGENTS.md``, ``.mcp.json``),
#: the delivery scripts the jobs holding secrets run, the secret scan's own
#: configuration, and the gates (``make lint`` / ``make test`` and what they
#: read, including the conftest files pytest loads first). Prefixes, so
#: ``requirements`` covers every ``requirements*.txt``.
PROTECTED_PREFIXES = (
    ".claude/",
    ".github/",
    "scripts/delivery/",
    "Makefile",
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "pytest.ini",
    "requirements",
    ".gitleaks.toml",
    ".gitleaksignore",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    ".mcp.json",
    "conftest.py",
    "tests/conftest.py",
)
#: What an implement-stage agent may never change (publish refuses, nothing is
#: pushed). The specs are the human-approved input.
IMPLEMENT_PROTECTED_PREFIXES = PROTECTED_PREFIXES + (
    "specs/technical-design/",
    "specs/requirements/",
    "specs/acceptance-criteria/",
    ".gitignore",
)
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


def run_budget(path: str) -> bool:
    """True if the action's result summary shows the run stopped on a spend limit.

    The summary is a list holding the last result message reduced to ``type``, ``subtype``,
    ``is_error``, ``num_turns``, ``total_cost_usd`` and ``limit`` (a bool the agent job
    derives from the result text: credit balance, spend or usage limit). The turn limit
    (``error_max_turns``) is not a budget stop; it ends in the gates' verdict."""
    try:
        messages = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("type") == "result":
            if message.get("subtype") in _LIMIT_SUBTYPES:
                return True
            return message.get("is_error") is True and message.get("limit") is True
    return False


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


# -------------------------------------------------------------- agent paths


def _clean_path(path: str) -> str:
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_protected(path: str) -> bool:
    """Whether ``path`` is on the pipeline/gates list (``PROTECTED_PREFIXES``)."""
    return _clean_path(path).startswith(PROTECTED_PREFIXES)


def check_agent_paths(stage: str, issue: str, paths: Iterable[str]) -> Tuple[bool, List[str], Optional[str]]:
    """Whether a publish job may push an agent change touching ``paths``.

    Returns ``(ok, matched, reason)``; ``reason`` is ``protected-path`` when refused. The spec
    stage may change only its own three spec files; the implement stage anything except
    ``IMPLEMENT_PROTECTED_PREFIXES``. An empty list is allowed (nothing to push)."""
    if stage not in STAGES:
        raise StageRefused("stage must be spec or implement")
    key = guard_branch.validate_key(issue)
    clean = sorted({_clean_path(p) for p in paths if p and p.strip()})
    if stage == "spec":
        pattern = re.compile(_SPEC_PATH.format(key=re.escape(key)))
        matched = [p for p in clean if not pattern.fullmatch(p) or ".." in p or is_protected(p)]
    else:
        matched = [p for p in clean if p.startswith(IMPLEMENT_PROTECTED_PREFIXES) or ".." in p]
    if matched:
        return False, matched, "protected-path"
    return True, [], None


# ----------------------------------------------------------- design status


def design_approved(text: str) -> bool:
    """True if the first ``Status:`` line in the design's header block (everything before
    the first ``## `` heading) starts with ``approved``."""
    for line in text.splitlines():
        if line.startswith("## "):
            return False
        match = _STATUS_LINE.match(line.replace("*", "").replace("_", ""))
        if match:
            return match.group(1).strip().lower().startswith("approved")
    return False


# ---------------------------------------------------------- allow markers


def allow_markers(patch_lines: Iterable[str]) -> int:
    """How many lines a patch ADDS that carry a ``gitleaks:allow`` marker (any spacing or case).
    An agent may never add one: it would silence the secret scan for that line."""
    count = 0
    for line in patch_lines:
        if line.startswith("+") and not line.startswith("+++") and _ALLOW_MARKER.search(line):
            count += 1
    return count


# ---------------------------------------------------------------- decision


def implement_decision(outcome: Dict[str, Any], lint_rc: Optional[int], test_rc: Optional[int],
                       commits: int, run_budget: bool = False, *, protected: bool = False,
                       secret: bool = False, gates_mismatch: bool = False) -> Dict[str, Any]:
    """The implement stage's final verdict: ``{ready, reasonCategory, push}``.

    Ready for review only with green gates on exactly the tree to be pushed, an approving
    verdict, at least one AC result and every AC passing. Publish refusals come first
    (``protected-path``, ``secret-found``: nothing pushed), then a run stopped by a spend
    limit (``budget``: nothing pushed, AC-39), then gate codes from another tree
    (``gates-mismatch``). Any other blocked run is pushed as a draft for a human (AC-34)."""
    def verdict(ready: bool, reason: Optional[str]) -> Dict[str, Any]:
        push = commits > 0 and reason not in NO_PUSH_REASONS
        return {"ready": ready and push, "reasonCategory": reason, "push": push}

    if protected:
        return verdict(False, "protected-path")
    if secret:
        return verdict(False, "secret-found")
    green = lint_rc == 0 and test_rc == 0
    if not outcome.get("valid"):
        if run_budget:
            return verdict(False, "budget")
        if gates_mismatch:
            return verdict(False, "gates-mismatch")
        return verdict(False, "workflow-failed" if green else "tests-red")
    if outcome.get("status") != "ready":
        return verdict(False, outcome.get("reasonCategory") or "dor-gap")
    if gates_mismatch:
        return verdict(False, "gates-mismatch")
    results = outcome.get("acResults") or []
    if not green or not all(r["pass"] for r in results):
        return verdict(False, "tests-red")
    if not results:
        return verdict(False, "dor-gap")
    if outcome.get("verdict") not in ("approve", "approve-with-follow-ups"):
        return verdict(False, "review-changes")
    if commits <= 0:
        return verdict(False, "workflow-failed")
    return verdict(True, None)


def _gate(rc: Optional[int]) -> str:
    if rc is None:
        return "no result for this tree"
    return "green" if rc == 0 else "red"


def pr_comment(*, issue: str, outcome: Dict[str, Any], decision: Dict[str, Any],
               lint_rc: Optional[int], test_rc: Optional[int], needs_human_review: bool,
               matched: List[str], cost: Optional[float]) -> str:
    """The implement stage's one PR comment, from validated fields only. ``decision`` is the
    final verdict publish already settled (``decide``); it is rendered, never recomputed."""
    guard_branch.validate_key(issue)
    for path in matched:
        if path not in review_labels.SENSITIVE_FILES and not path.startswith(review_labels.SENSITIVE_DIRS):
            raise StageRefused("matched paths must be on the sensitive-path list")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", path):
            raise StageRefused("unexpected characters in a path")
    category = decision.get("reasonCategory")
    if category is not None and category not in notify_slack.REASON_CATEGORIES:
        raise StageRefused("unknown reason category")
    if not isinstance(decision.get("ready"), bool) or (decision["ready"] and category is not None):
        raise StageRefused("ready must be a bool, with no reason when ready")
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
    p = sub.add_parser("agent-paths")
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--issue", required=True)
    sub.add_parser("design-status")
    sub.add_parser("allow-markers", help="a patch on stdin")
    for name in ("decide", "pr-comment"):
        p = sub.add_parser(name)
        p.add_argument("--outcome", required=True)
        p.add_argument("--lint-rc", default="")
        p.add_argument("--test-rc", default="")
    p = sub.choices["decide"]
    p.add_argument("--commits", default="0")
    p.add_argument("--budget", action="store_true", help="the run stopped on a spend limit")
    p.add_argument("--protected", action="store_true", help="publish refused a protected path")
    p.add_argument("--secret", action="store_true", help="gitleaks or an allow marker refused it")
    p.add_argument("--gates-mismatch", action="store_true",
                   help="the gates job tested another tree than the one to be pushed")
    p = sub.choices["pr-comment"]
    p.add_argument("--issue", required=True)
    p.add_argument("--ready", required=True, choices=("true", "false"))
    p.add_argument("--reason", default="")
    p.add_argument("--agent-flag", action="store_true")
    p.add_argument("--cost", type=float)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "outcome":
            result: Any = read_outcome(args.stage, args.file, args.issue)
        elif args.command == "cost":
            result = {"cost": run_cost(args.file), "budget": run_budget(args.file)}
        elif args.command == "spec-paths":
            ok, paths, reason = check_spec_paths(args.issue, sys.stdin.read().splitlines())
            result = {"ok": ok, "paths": paths, "reason": reason}
        elif args.command == "agent-paths":
            ok, matched, reason = check_agent_paths(args.stage, args.issue, sys.stdin.read().splitlines())
            result = {"ok": ok, "matched": matched, "reason": reason}
        elif args.command == "design-status":
            result = {"approved": design_approved(sys.stdin.read())}
        elif args.command == "allow-markers":
            count = allow_markers(sys.stdin.read().splitlines())
            result = {"ok": count == 0, "count": count}
        elif args.command == "decide":
            outcome = _load_outcome(args.outcome)
            commits = int(args.commits) if re.fullmatch(r"[0-9]{1,6}", args.commits) else 0
            result = implement_decision(outcome, _rc(args.lint_rc), _rc(args.test_rc), commits,
                                        run_budget=args.budget, protected=args.protected,
                                        secret=args.secret, gates_mismatch=args.gates_mismatch)
        else:
            outcome = _load_outcome(args.outcome)
            decision = {"ready": args.ready == "true", "reasonCategory": args.reason or None}
            needed, matched = review_labels.needs_human_review(
                sys.stdin.read().splitlines(),
                agent_flag=args.agent_flag or bool(outcome.get("needsHumanReview")),
            )
            print(pr_comment(issue=args.issue, outcome=outcome, decision=decision,
                             lint_rc=_rc(args.lint_rc), test_rc=_rc(args.test_rc),
                             needs_human_review=needed, matched=matched, cost=args.cost), end="")
            return 0
    except (StageRefused, guard_branch.GuardRefused, OSError, ValueError) as refused:
        print(f"refused: {type(refused).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
