"""Gate G2 for the agent pipeline: the ``design-approved`` label (SIM-153, SIM-71 Phase 3).

    python scripts/delivery/g2_gate.py inputs --issue SIM-<n> --pr <n> --sha <40-hex>
    gh pr list ... --json number,headRefName,headRefOid,baseRefName,labels,isCrossRepository | \\
        python scripts/delivery/g2_gate.py candidates
    python scripts/delivery/g2_gate.py check --issue SIM-<n> --pr <n> --sha <40-hex> \\
        --pr-file <pull.json> --timeline-file <timeline.json> --activity-file <activity.json> \\
        --approvers "$DESIGN_APPROVERS" --repo <owner/name>
    python scripts/delivery/g2_gate.py git-check --issue SIM-<n> --sha <40-hex> \\
        --base origin/development [--repo .]

G2 is the label ``design-approved`` on the agent's spec PR, added by a login in
``DESIGN_APPROVERS`` (comma list, trimmed, case-insensitive), on exactly the
head the implement stage builds on. ``check`` decides it from the GitHub REST
replies only: the pull (open, base ``development``, head the issue's own
ticket branch in this repository, label ``agent-pr``, not yet
``agent-implemented``, head SHA equal to the input), the issue timeline (the
LAST ``labeled design-approved`` event, by an approver who is a User, with no
later ``unlabeled design-approved``, ``committed``, ``head_ref_force_pushed``
or ``head_ref_deleted``) and the repository activity (the branch's latest push
is the input SHA and happened strictly before the label, in an earlier second:
a ``committed`` event sorts by the commit's own date, which the pusher
controls). It always prints a JSON verdict and exits 0; ``notify`` says
whether the refusal is reported in Jira and Slack (everything except "not
approved yet", and nothing at all when the PR's head is not the issue's own
ticket branch in this repository, so a hand dispatch cannot flag an unrelated
issue). ``git-check`` is defence in depth on ``base..sha``: an agent-authored
or agent-committed commit may only touch the issue's own spec files (the spec
stage's commit), the branch may not change the pipeline or the gates
(``protected-path``), and every protected path at ``sha`` must be
byte-identical to ``base`` itself, the current development tip
(``protected-path-stale`` when development changed one since the branch was
cut). ``candidates`` lists the PRs the
dispatcher should check; ``inputs`` checks the three workflow inputs before any
API call. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import guard_branch  # noqa: E402  (sibling script)
import stage_io  # noqa: E402  (sibling script)

LABEL = "design-approved"
AGENT_PR = "agent-pr"
IMPLEMENTED = "agent-implemented"
BASE = "development"
#: Events after the approval that make it stale.
_STALE_EVENTS = ("committed", "head_ref_force_pushed", "head_ref_deleted")
#: Refusals that are only "not approved (yet)" or a transient read: a job-summary line, no notice.
QUIET_REASONS = ("not-approved", "already-implemented", "bad-input", "bad-data")
_PR_NUMBER = re.compile(r"^[0-9]{1,7}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class G2Refused(Exception):
    """A request the gate will not carry out."""


def _verdict(reason: str, owned: bool = True, **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"go": reason == "approved", "reason": reason,
                           "notify": owned and reason != "approved" and reason not in QUIET_REASONS}
    out.update(fields)
    return out


def _time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _labels(pr: Dict[str, Any]) -> set:
    return {lab.get("name") for lab in pr.get("labels") or [] if isinstance(lab, dict)}


def _approvers(raw: Optional[str]) -> set:
    return {login.strip().casefold() for login in (raw or "").split(",") if login.strip()}


def _is_label_event(event: Dict[str, Any], kind: str) -> bool:
    return event.get("event") == kind and (event.get("label") or {}).get("name") == LABEL


def valid_inputs(issue: Any, pr_number: Any, sha: Any) -> bool:
    """The implement workflow's inputs: ``SIM-<n>``, a PR number, a 40-hex commit id."""
    return bool(isinstance(issue, str) and guard_branch._KEY.fullmatch(issue)
                and isinstance(pr_number, str) and _PR_NUMBER.fullmatch(pr_number)
                and isinstance(sha, str) and _SHA.fullmatch(sha))


def check(*, issue: Any, pr_number: Any, sha: Any, pr: Any, timeline: Any, activity: Any,
          approvers: Optional[str], repo: str) -> Dict[str, Any]:
    """Whether PR ``pr_number`` carries a valid G2 approval for head ``sha`` of ``issue``."""
    if not valid_inputs(issue, pr_number, sha):
        return _verdict("bad-input")
    fields: Dict[str, Any] = {"issue": issue, "pr": int(pr_number), "sha": sha}
    if not isinstance(pr, dict) or not isinstance(timeline, list) or not isinstance(activity, list):
        return _verdict("bad-data", **fields)
    head = pr.get("head") or {}
    branch = head.get("ref") if isinstance(head.get("ref"), str) else ""
    try:
        guard_branch.check(branch, issue)
        owned = ((head.get("repo") or {}).get("full_name")) == repo
    except guard_branch.GuardRefused:
        owned = False
    # A PR that is not the issue's own is refused before anything else, and quietly.
    if not owned:
        return _verdict("wrong-branch", owned=False, **fields)
    if pr.get("number") != int(pr_number):
        return _verdict("pr-mismatch", **fields)
    if pr.get("state") != "open":
        return _verdict("pr-not-open", **fields)
    if (pr.get("base") or {}).get("ref") != BASE:
        return _verdict("wrong-base", **fields)
    fields["branch"] = branch
    labels = _labels(pr)
    if AGENT_PR not in labels:
        return _verdict("not-agent-pr", **fields)
    if IMPLEMENTED in labels:
        return _verdict("already-implemented", **fields)

    events = [e for e in timeline if isinstance(e, dict)]
    labelled = [i for i, e in enumerate(events) if _is_label_event(e, "labeled")]
    if LABEL not in labels or not labelled:
        return _verdict("not-approved", **fields)
    allowed = _approvers(approvers)
    if not allowed:
        return _verdict("no-approvers", **fields)
    index = labelled[-1]
    event = events[index]
    actor = event.get("actor") or {}
    login = actor.get("login") if isinstance(actor.get("login"), str) else ""
    label_time = _time(event.get("created_at"))
    if login.casefold() not in allowed or actor.get("type") != "User" or label_time is None:
        return _verdict("approver-not-allowed", **fields)
    fields.update(approver=login, labelledAt=event.get("created_at"))

    for position, later in enumerate(events):
        stale_kind = later.get("event") in _STALE_EVENTS or _is_label_event(later, "unlabeled")
        if not stale_kind:
            continue
        when = _time(later.get("created_at"))
        if position > index or (when is not None and when > label_time):
            return _verdict("approval-stale", **fields)

    if head.get("sha") != sha:
        return _verdict("sha-mismatch", **fields)

    pushes = []
    for entry in activity:
        if isinstance(entry, dict) and entry.get("ref") == f"refs/heads/{branch}":
            when = _time(entry.get("timestamp"))
            if when is not None:
                pushes.append((when, entry))
    if not pushes:
        return _verdict("push-unverified", **fields)
    pushed_at, last = max(pushes, key=lambda item: item[0])
    # Same second counts as after: the timestamps cannot show which came first.
    if last.get("after") != sha or pushed_at >= label_time:
        return _verdict("approval-stale", **fields)
    return _verdict("approved", **fields)


def approval_record(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """The verified approval the agent reads in CI instead of a ``Status: approved`` line."""
    if verdict.get("go") is not True:
        raise G2Refused("no G2 approval to record")
    return {"gate": "G2", "label": LABEL, "issue": verdict["issue"], "pr": verdict["pr"],
            "sha": verdict["sha"], "approver": verdict["approver"], "labelledAt": verdict["labelledAt"]}


# ------------------------------------------------------------- candidates


def candidates(prs: Any) -> List[Dict[str, Any]]:
    """Open agent PRs labelled ``design-approved`` and not yet implemented, in list order."""
    out: List[Dict[str, Any]] = []
    if not isinstance(prs, list):
        return out
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        labels = _labels(pr)
        branch = pr.get("headRefName")
        sha = pr.get("headRefOid")
        number = pr.get("number")
        if not ({AGENT_PR, LABEL} <= labels) or IMPLEMENTED in labels:
            continue
        if pr.get("baseRefName") != BASE or pr.get("isCrossRepository") is not False:
            continue
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(sha, str) \
                or not _SHA.fullmatch(sha):
            continue
        match = guard_branch._BRANCH.fullmatch(branch if isinstance(branch, str) else "")
        if not match:
            continue
        out.append({"issue": f"SIM-{match.group(2)}", "pr": number, "sha": sha, "branch": branch})
    return out


# -------------------------------------------------------------- git check


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout


def git_check(issue: str, sha: str, base: str, repo: str = ".") -> Dict[str, Any]:
    """Defence in depth on ``base..sha``: agent commits only as the spec stage's own spec files,
    no change to the pipeline or the gates anywhere on the branch, and the pipeline and gates
    at ``sha`` exactly as they are at ``base`` (the development tip) now."""
    key = guard_branch.validate_key(issue)
    if not _SHA.fullmatch(sha or ""):
        raise G2Refused("sha must be a 40-hex commit id")
    spec_path = re.compile(stage_io._SPEC_PATH.format(key=re.escape(key)))
    for commit in _git(repo, "rev-list", f"{base}..{sha}").split():
        identities = _git(repo, "log", "-1", "--format=%ae%n%ce", commit).split()
        if not any(identity.lower() in stage_io.AGENT_IDENTITIES for identity in identities):
            continue
        files = _git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r",
                     "--root", commit).split()
        if not files or any(not spec_path.fullmatch(p) or ".." in p for p in files):
            return {"ok": False, "reason": "agent-commit"}
    changed = _git(repo, "diff", "--name-only", "--no-renames", f"{base}...{sha}").splitlines()
    if any(stage_io.is_protected(p) for p in changed):
        return {"ok": False, "reason": "protected-path"}
    # Two-dot: the trees themselves. A branch cut before a protected change landed on
    # development would otherwise run the gates and pipeline files as they were then.
    differ = _git(repo, "diff", "--name-only", "--no-renames", base, sha).splitlines()
    if any(stage_io.is_protected(p) for p in differ):
        return {"ok": False, "reason": "protected-path-stale"}
    return {"ok": True, "reason": None}


# -------------------------------------------------------------------- CLI


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("candidates", help="gh pr list JSON on stdin")
    p = sub.add_parser("inputs")
    for name in ("--issue", "--pr", "--sha"):
        p.add_argument(name, required=True)
    p = sub.add_parser("check")
    for name in ("--issue", "--pr", "--sha", "--pr-file", "--timeline-file", "--activity-file", "--repo"):
        p.add_argument(name, required=True)
    p.add_argument("--approvers", default="")
    p.add_argument("--record", help="also write the approval record here when G2 passes")
    p = sub.add_parser("git-check")
    p.add_argument("--issue", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--repo", default=".")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "candidates":
            try:
                prs = json.loads(sys.stdin.read() or "[]")
            except ValueError:
                prs = []
            result: Any = candidates(prs)
        elif args.command == "inputs":
            result = {"ok": valid_inputs(args.issue, args.pr, args.sha)}
        elif args.command == "check":
            result = check(issue=args.issue, pr_number=args.pr, sha=args.sha,
                           pr=_read_json(args.pr_file), timeline=_read_json(args.timeline_file),
                           activity=_read_json(args.activity_file), approvers=args.approvers,
                           repo=args.repo)
            if args.record and result["go"]:
                Path(args.record).write_text(json.dumps(approval_record(result)) + "\n", encoding="utf-8")
        else:
            result = git_check(args.issue, args.sha, args.base, args.repo)
    except (G2Refused, guard_branch.GuardRefused, subprocess.CalledProcessError, OSError) as refused:
        print(f"refused: {type(refused).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
