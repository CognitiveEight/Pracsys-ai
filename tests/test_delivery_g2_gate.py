"""Offline tests for ``scripts/delivery/g2_gate.py`` (SIM-153; SIM-71 gate G2 as a PR label).

G2 is the label ``design-approved`` on the agent's spec PR, added by a login in
``DESIGN_APPROVERS``, on exactly the head the implement stage will build on.
Every rule is checked against fixture JSON shaped like the GitHub REST
responses (pull, issue timeline, repository activity); git rules run in a
throwaway repository under ``tmp_path``. Fictional data only.
"""

import copy
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "CognitiveEight/Pracsys-ai"
BRANCH = "feature/SIM-13-export"
SHA = "a" * 40
OLD = "b" * 40
APPROVER = "approver-one"
LABEL_TIME = "2026-10-23T10:00:00Z"


def _load(name: str):
    path = ROOT / "scripts" / "delivery" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"delivery_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def g2():
    return _load("g2_gate")


def _pr(**over):
    pr = {
        "number": 7, "state": "open",
        "base": {"ref": "development"},
        "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": REPO}},
        "labels": [{"name": "agent-pr"}, {"name": "design-approved"}],
    }
    pr.update(over)
    return pr


def _label(name="design-approved", login=APPROVER, at=LABEL_TIME, kind="labeled", actor_type="User"):
    return {"event": kind, "label": {"name": name}, "actor": {"login": login, "type": actor_type},
            "created_at": at}


def _timeline():
    return [
        _label("agent-pr", login="token-owner", at="2026-10-22T08:00:00Z"),
        {"event": "committed", "sha": SHA},
        _label(),
    ]


def _activity(after=SHA, at="2026-10-23T09:00:00Z", ref=f"refs/heads/{BRANCH}"):
    return [{"ref": ref, "after": after, "before": OLD, "timestamp": at, "activity_type": "push"}]


def _check(g2, *, pr=None, timeline=None, activity=None, approvers=f" {APPROVER} , other-approver ",
           issue="SIM-13", number="7", sha=SHA):
    return g2.check(
        issue=issue, pr_number=number, sha=sha,
        pr=_pr() if pr is None else pr,
        timeline=_timeline() if timeline is None else timeline,
        activity=_activity() if activity is None else activity,
        approvers=approvers, repo=REPO,
    )


# ------------------------------------------------------------------- go


def test_label_by_an_approver_on_the_dispatched_head_is_g2(g2):
    out = _check(g2)
    assert out["go"] is True and out["reason"] == "approved" and out["notify"] is False
    assert out["issue"] == "SIM-13" and out["branch"] == BRANCH and out["pr"] == 7 and out["sha"] == SHA
    assert out["approver"] == APPROVER and out["labelledAt"] == LABEL_TIME


def test_approver_logins_are_trimmed_and_case_insensitive(g2):
    assert _check(g2, approvers="Other ,  APPROVER-ONE ")["go"] is True


# --------------------------------------------------------- not approved yet (quiet)


def test_no_label_yet_is_quiet(g2):
    pr = _pr(labels=[{"name": "agent-pr"}])
    out = _check(g2, pr=pr, timeline=_timeline()[:2])
    assert out["go"] is False and out["reason"] == "not-approved" and out["notify"] is False


def test_label_on_pr_but_no_labeled_event_is_not_approved(g2):
    out = _check(g2, timeline=_timeline()[:2])
    assert out["go"] is False and out["reason"] == "not-approved" and out["notify"] is False


def test_already_implemented_is_quiet(g2):
    pr = _pr(labels=[{"name": "agent-pr"}, {"name": "design-approved"}, {"name": "agent-implemented"}])
    out = _check(g2, pr=pr)
    assert out["go"] is False and out["reason"] == "already-implemented" and out["notify"] is False


# ------------------------------------------------------------ who labelled


@pytest.mark.parametrize("login,actor_type", [
    ("someone-else", "User"),
    (APPROVER, "Bot"),
    ("github-actions[bot]", "Bot"),
])
def test_label_by_anyone_but_an_approver_is_refused_and_reported(g2, login, actor_type):
    timeline = _timeline()[:2] + [_label(login=login, actor_type=actor_type)]
    out = _check(g2, timeline=timeline)
    assert out["go"] is False and out["reason"] == "approver-not-allowed" and out["notify"] is True


def test_no_approvers_configured_refuses(g2):
    for approvers in ("", " , ", None):
        out = _check(g2, approvers=approvers)
        assert out["go"] is False and out["reason"] == "no-approvers" and out["notify"] is True


def test_the_last_labeled_event_decides(g2):
    # an approver labelled, it was removed, and a non-approver labelled again
    timeline = _timeline() + [
        _label(kind="unlabeled", at="2026-10-23T10:05:00Z"),
        _label(login="someone-else", at="2026-10-23T10:06:00Z"),
    ]
    out = _check(g2, timeline=timeline)
    assert out["go"] is False and out["reason"] == "approver-not-allowed"
    # and the other way round: a non-approver first, then an approver
    timeline = _timeline()[:2] + [_label(login="someone-else", at="2026-10-23T09:30:00Z"), _label()]
    assert _check(g2, timeline=timeline)["go"] is True


# ----------------------------------------------------- nothing after the label


@pytest.mark.parametrize("event", [
    _label(kind="unlabeled", at="2026-10-23T10:05:00Z"),
    {"event": "committed", "sha": "c" * 40},
    {"event": "head_ref_force_pushed", "actor": {"login": "x"}, "created_at": "2026-10-23T10:05:00Z"},
    {"event": "head_ref_deleted", "actor": {"login": "x"}, "created_at": "2026-10-23T10:05:00Z"},
])
def test_any_change_after_the_label_makes_it_stale(g2, event):
    out = _check(g2, timeline=_timeline() + [event])
    assert out["go"] is False and out["reason"] == "approval-stale" and out["notify"] is True


def test_other_labels_after_the_approval_do_not_matter(g2):
    timeline = _timeline() + [_label("needs-human-review", login="someone-else", at="2026-10-23T11:00:00Z")]
    assert _check(g2, timeline=timeline)["go"] is True


def test_a_timed_change_listed_before_the_label_but_made_after_it_is_stale(g2):
    forced = {"event": "head_ref_force_pushed", "actor": {"login": "x"}, "created_at": "2026-10-23T10:30:00Z"}
    timeline = _timeline()[:2] + [forced, _label()]
    assert _check(g2, timeline=timeline)["reason"] == "approval-stale"


def test_a_backdated_commit_is_caught_by_the_push_time(g2):
    """A `committed` event sorts by the commit's own (forgeable) date, so a commit pushed after
    the label can appear before it in the timeline. The repository activity records when the
    branch was actually pushed."""
    out = _check(g2, activity=_activity(at="2026-10-23T10:30:00Z"))
    assert out["go"] is False and out["reason"] == "approval-stale" and out["notify"] is True


def test_the_last_push_must_be_the_dispatched_head(g2):
    activity = _activity(after=OLD, at="2026-10-23T09:00:00Z") + _activity(at="2026-10-23T08:00:00Z")
    assert _check(g2, activity=activity)["reason"] == "approval-stale"
    # the latest push (by time, not list order) is the one compared
    activity = _activity(after=OLD, at="2026-10-23T08:00:00Z") + _activity(at="2026-10-23T09:00:00Z")
    assert _check(g2, activity=activity)["go"] is True


def test_no_push_record_for_the_branch_refuses(g2):
    for activity in ([], _activity(ref="refs/heads/feature/SIM-14-other"), [{"ref": f"refs/heads/{BRANCH}"}]):
        out = _check(g2, activity=activity)
        assert out["go"] is False and out["reason"] == "push-unverified" and out["notify"] is True


# ------------------------------------------------------------- the PR itself


def test_head_must_equal_the_dispatched_sha(g2):
    out = _check(g2, pr=_pr(head={"ref": BRANCH, "sha": OLD, "repo": {"full_name": REPO}}))
    assert out["go"] is False and out["reason"] == "sha-mismatch" and out["notify"] is True


@pytest.mark.parametrize("over,reason", [
    ({"state": "closed"}, "pr-not-open"),
    ({"base": {"ref": "main"}}, "wrong-base"),
    ({"head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": "someone/fork"}}}, "wrong-branch"),
    ({"head": {"ref": "feature/SIM-14-other", "sha": SHA, "repo": {"full_name": REPO}}}, "wrong-branch"),
    ({"head": {"ref": "development", "sha": SHA, "repo": {"full_name": REPO}}}, "wrong-branch"),
    ({"labels": [{"name": "design-approved"}]}, "not-agent-pr"),
    ({"number": 8}, "pr-mismatch"),
])
def test_pr_shape_is_checked(g2, over, reason):
    out = _check(g2, pr=_pr(**over))
    assert out["go"] is False and out["reason"] == reason and out["notify"] is True


@pytest.mark.parametrize("kw", [
    {"issue": "SIM-13; rm"}, {"issue": "sim-13"}, {"number": "7; rm"}, {"number": ""},
    {"sha": "abc"}, {"sha": "A" * 40}, {"sha": SHA + "\n"},
])
def test_bad_inputs_are_refused_quietly(g2, kw):
    out = _check(g2, **kw)
    assert out["go"] is False and out["reason"] == "bad-input" and out["notify"] is False


def test_inputs_cli(g2, capsys):
    assert g2.main(["inputs", "--issue", "SIM-13", "--pr", "7", "--sha", SHA]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    for issue, pr, sha in (("SIM-13 ", "7", SHA), ("SIM-13", "7 ", SHA), ("SIM-13", "7", SHA[:-1]),
                           ("SIM-13", "12345678", SHA), ("SIM-13", "-1", SHA)):
        assert g2.main(["inputs", "--issue", issue, "--pr", pr, "--sha", sha]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": False}


def test_malformed_api_data_refuses(g2):
    for pr, timeline, activity in (("x", [], []), (_pr(), "x", []), (_pr(), _timeline(), {"a": 1})):
        out = g2.check(issue="SIM-13", pr_number="7", sha=SHA, pr=pr, timeline=timeline,
                       activity=activity, approvers=APPROVER, repo=REPO)
        assert out["go"] is False and out["reason"] == "bad-data" and out["notify"] is False


def test_quiet_reasons_are_only_not_yet_approved_ones(g2):
    """Everything else is reported in Jira and Slack. ``bad-data`` (an API reply that is not
    the expected shape) is retried by the next dispatcher tick instead of flagging the issue."""
    assert set(g2.QUIET_REASONS) == {"not-approved", "already-implemented", "bad-input", "bad-data"}


# ------------------------------------------------------------------ CLI


def test_check_cli_writes_json_and_never_fails_on_a_refusal(g2, tmp_path, capsys):
    files = {}
    for name, data in (("pr", _pr()), ("timeline", _timeline()), ("activity", _activity())):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(data))
    args = ["check", "--issue", "SIM-13", "--pr", "7", "--sha", SHA, "--pr-file", str(files["pr"]),
            "--timeline-file", str(files["timeline"]), "--activity-file", str(files["activity"]),
            "--approvers", APPROVER, "--repo", REPO]
    assert g2.main(args) == 0
    assert json.loads(capsys.readouterr().out)["go"] is True
    assert g2.main(args[:-4] + ["--approvers", "nobody", "--repo", REPO]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "approver-not-allowed"
    missing = args.copy()
    missing[missing.index("--timeline-file") + 1] = str(tmp_path / "absent.json")
    assert g2.main(missing) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "bad-data"


def test_record_is_what_the_agent_reads_as_the_approval(g2):
    record = g2.approval_record(_check(g2))
    assert record == {"gate": "G2", "label": "design-approved", "issue": "SIM-13", "pr": 7,
                      "sha": SHA, "approver": APPROVER, "labelledAt": LABEL_TIME}
    with pytest.raises(g2.G2Refused):
        g2.approval_record(_check(g2, approvers="nobody"))


# ----------------------------------------------------------- candidates


def _listed(number, branch, labels, sha=SHA, base="development", cross=False):
    return {"number": number, "headRefName": branch, "headRefOid": sha, "baseRefName": base,
            "isCrossRepository": cross, "labels": [{"name": n} for n in labels]}


def test_candidates_are_open_agent_prs_labelled_design_approved(g2):
    prs = [
        _listed(7, BRANCH, ["agent-pr", "design-approved"]),
        _listed(8, "fix/SIM-20-bug", ["design-approved", "agent-pr", "needs-human-review"]),
        _listed(9, "feature/SIM-21-x", ["agent-pr"]),  # not approved
        _listed(10, "feature/SIM-22-x", ["agent-pr", "design-approved", "agent-implemented"]),
        _listed(11, "feature/SIM-23-x", ["design-approved"]),  # not an agent PR
        _listed(12, "development", ["agent-pr", "design-approved"]),
        _listed(13, "feature/SIM-24-x", ["agent-pr", "design-approved"], base="main"),
        _listed(14, "feature/SIM-25-x", ["agent-pr", "design-approved"], cross=True),
        _listed(15, "feature/SIM-26-x", ["agent-pr", "design-approved"], sha="nope"),
        _listed(16, "feature/SIM-27-$(id)", ["agent-pr", "design-approved"]),
    ]
    assert g2.candidates(prs) == [
        {"issue": "SIM-13", "pr": 7, "sha": SHA, "branch": BRANCH},
        {"issue": "SIM-20", "pr": 8, "sha": SHA, "branch": "fix/SIM-20-bug"},
    ]
    assert g2.candidates("garbage") == []


def test_candidates_cli(g2, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_listed(7, BRANCH, ["agent-pr", "design-approved"])])))
    assert g2.main(["candidates"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"issue": "SIM-13", "pr": 7, "sha": SHA, "branch": BRANCH}]


# ----------------------------------------------------------- git checks


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


AGENT = "pracsys-agent <pracsys-agent@users.noreply.github.com>"
DESIGN = "specs/technical-design/SIM-13-export.md"


def _commit(repo, path, text, author="Test Approver <approver@example.com>"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", path)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--author", author, "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "development")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "user.email", "test@example.com")
    base = _commit(r, "README.md", "x\n")
    _git(r, "branch", "base-ref", base)
    _git(r, "switch", "-q", "-c", BRANCH)
    return r


def _spec_commit(repo):
    for kind in ("requirements", "technical-design", "acceptance-criteria"):
        target = repo / f"specs/{kind}/SIM-13-export.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# D\n- **Status:** draft\n")
        _git(repo, "add", str(target.relative_to(repo)))
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--author", AGENT, "-m", "docs(SIM-13): specs")
    return _git(repo, "rev-parse", "HEAD")


def test_git_check_accepts_the_spec_stage_commit_and_human_edits(g2, repo):
    _spec_commit(repo)
    head = _commit(repo, DESIGN, "# D\n- **Status:** draft\n\nreviewed\n")
    assert g2.git_check("SIM-13", head, "base-ref", str(repo)) == {"ok": True, "reason": None}


def test_git_check_refuses_an_agent_commit_outside_its_own_specs(g2, repo):
    _spec_commit(repo)
    _commit(repo, "app/x.py", "x = 1\n", author=AGENT)
    head = _commit(repo, DESIGN, "# D\n- **Status:** draft\n\nreviewed\n")
    assert g2.git_check("SIM-13", head, "base-ref", str(repo)) == {"ok": False, "reason": "agent-commit"}


def test_git_check_refuses_an_agent_committer_with_a_human_author(g2, repo):
    target = repo / "app/y.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("y = 1\n")
    _git(repo, "add", "app/y.py")
    env = {**__import__("os").environ, "GIT_COMMITTER_NAME": "claude[bot]",
           "GIT_COMMITTER_EMAIL": "41898282+claude[bot]@users.noreply.github.com"}
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--author",
         "Test Approver <approver@example.com>", "-m", "c", env=env)
    head = _git(repo, "rev-parse", "HEAD")
    assert g2.git_check("SIM-13", head, "base-ref", str(repo))["reason"] == "agent-commit"


def test_git_check_refuses_an_agent_spec_commit_for_another_issue(g2, repo):
    head = _commit(repo, "specs/technical-design/SIM-14-other.md", "x\n", author=AGENT)
    assert g2.git_check("SIM-13", head, "base-ref", str(repo))["reason"] == "agent-commit"


@pytest.mark.parametrize("path", [
    ".github/workflows/agent-implement.yml", ".claude/settings.json", "scripts/delivery/g2_gate.py",
    "Makefile", "pyproject.toml", "requirements-dev.txt", ".gitleaks.toml", ".gitleaksignore",
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", "conftest.py", "tests/conftest.py",
])
def test_git_check_refuses_a_branch_that_changes_the_pipeline_or_gates(g2, repo, path):
    _spec_commit(repo)
    head = _commit(repo, path, "changed\n")
    assert g2.git_check("SIM-13", head, "base-ref", str(repo)) == {"ok": False, "reason": "protected-path"}


def test_git_check_cli(g2, repo, capsys):
    head = _spec_commit(repo)
    assert g2.main(["git-check", "--issue", "SIM-13", "--sha", head, "--base", "base-ref",
                    "--repo", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "reason": None}
    assert g2.main(["git-check", "--issue", "SIM-13", "--sha", "zz", "--base", "base-ref",
                    "--repo", str(repo)]) == 2


def test_fixtures_are_not_mutated(g2):
    pr, timeline, activity = _pr(), _timeline(), _activity()
    before = copy.deepcopy((pr, timeline, activity))
    _check(g2, pr=pr, timeline=timeline, activity=activity)
    assert (pr, timeline, activity) == before
