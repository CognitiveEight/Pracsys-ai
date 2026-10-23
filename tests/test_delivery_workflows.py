"""Static checks on the delivery workflows (SIM-71 AC-15f and the triage guardrails).

Parsed as YAML; nothing runs. These pin the properties that keep the triage
agent contained, so a later edit cannot quietly widen them.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "slack-triage.yml"


def _load():
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the bare key `on` as boolean True.
    return doc, doc.get("on", doc.get(True))


def test_triage_job_gated_on_variable():
    doc, _ = _load()
    assert doc["jobs"]["triage"]["if"].replace(" ", "") == "vars.TRIAGE_ENABLED=='true'"


def test_triage_runs_on_schedule_and_manual_only_with_read_permissions():
    doc, triggers = _load()
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_actions_are_pinned_by_sha():
    doc, _ = _load()
    for step in doc["jobs"]["triage"]["steps"]:
        ref = step["uses"].split("@", 1)[1]
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]


def test_agent_tools_are_read_only_plus_the_two_helpers():
    doc, _ = _load()
    step = next(s for s in doc["jobs"]["triage"]["steps"] if "claude-code-action" in s["uses"])
    args = step["with"]["claude_args"]
    allowed = args.split("--allowedTools", 1)[1].strip().strip('"').split(",")
    assert allowed == [
        "Read",
        "Grep",
        "Glob",
        "Bash(python3 scripts/delivery/slack_io.py:*)",
        "Bash(python3 scripts/delivery/jira_triage.py:*)",
        "Bash(python3 scripts/delivery/confluence_docs.py:*)",
    ]
    for banned in ("Edit", "Write", "WebFetch", "git push", "make e2e", "dangerously"):
        assert banned not in args


def test_checkout_does_not_keep_credentials():
    doc, _ = _load()
    checkout = doc["jobs"]["triage"]["steps"][0]
    assert checkout["with"]["persist-credentials"] is False


def test_action_uses_the_read_only_job_token_not_oidc():
    doc, _ = _load()
    step = next(s for s in doc["jobs"]["triage"]["steps"] if "claude-code-action" in s["uses"])
    assert step["with"]["github_token"] == "${{ github.token }}"
    assert "id-token" not in doc["permissions"]
    assert doc["permissions"] == {"contents": "read"}


DAILY = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-worklist.yml"


def _load_daily():
    doc = yaml.safe_load(DAILY.read_text())
    return doc, doc.get("on", doc.get(True))


def test_daily_job_gated_on_variable():
    doc, _ = _load_daily()
    assert doc["jobs"]["worklist"]["if"].replace(" ", "") == "vars.DAILY_ENABLED=='true'"


def test_daily_schedule_read_only_and_no_model():
    doc, triggers = _load_daily()
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "30 3 * * 1-6"}]  # 09:00 IST, Mon–Sat
    assert doc["permissions"] == {"contents": "read"}
    steps = doc["jobs"]["worklist"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert len(uses) == 1 and uses[0].startswith("actions/checkout@")
    assert len(uses[0].split("@", 1)[1]) == 40  # pinned by SHA
    assert steps[0]["with"]["persist-credentials"] is False
    assert "ANTHROPIC_API_KEY" not in DAILY.read_text()


# --------------------------------------------------------------------------
# Phase 3 agent workflows (SIM-153, SIM-71 AC-19, AC-26, AC-29, AC-35 and
# the publish-token and permission rules of the design). Every rule below is
# checked over every `.github/workflows/agent-*.yml`, so a new agent
# workflow inherits them.

import re  # noqa: E402

import pytest  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PROMPTS = Path(__file__).resolve().parents[1] / ".github" / "agent-prompts"
AGENT_FILES = sorted(WORKFLOWS.glob("agent-*.yml"))
ALL_FILES = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
KILL_SWITCH = "vars.AGENTS_ENABLED=='true'"
AGENT_ACTION = "anthropics/claude-code-action@"
SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
#: `${{ }}` expressions that carry text someone outside the team controls.
UNTRUSTED_EXPR = re.compile(
    r"\$\{\{[^}]*("
    r"github\.event\.(head_commit|commits|pull_request|issue|comment|review|review_comment|"
    r"discussion|pages|workflow_run|inputs|client_payload|release|sender)"
    r"|github\.head_ref|inputs\."
    r")"
)
FILE_TOOLS = {"Read", "Grep", "Glob", "Write", "Edit", "Task"}
BASH_PREFIX = re.compile(r"^Bash\([^()*]+:\*\)$")


def _wf(path):
    doc = yaml.safe_load(path.read_text())
    return doc, doc.get("on", doc.get(True))


def _steps(doc):
    for job_id, job in doc["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, job, step


def _agent_steps(doc):
    return [(j, job, s) for j, job, s in _steps(doc) if AGENT_ACTION in s.get("uses", "")]


def _flat(expr):
    return re.sub(r"\s+", "", expr or "")


def _allowed_tools(step):
    args = step["with"]["claude_args"]
    match = re.search(r'--allowedTools\s+"([^"]*)"', args)
    assert match, "the agent step must list its tools explicitly"
    return args, match.group(1).split(",")


def test_agent_workflows_exist():
    assert {p.name for p in AGENT_FILES} == {"agent-dispatch.yml", "agent-spec.yml", "agent-implement.yml"}


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_every_agent_job_has_kill_switch(path):
    doc, _ = _wf(path)
    for job_id, job in doc["jobs"].items():
        assert KILL_SWITCH in _flat(job.get("if")), f"{path.name}:{job_id} lacks the kill switch"


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_agent_step_holds_no_other_credentials_or_network_tools(path):
    doc, _ = _wf(path)
    # Nothing secret is inherited from workflow- or job-level env.
    assert "secrets." not in yaml.safe_dump(doc.get("env") or {})
    for job in doc["jobs"].values():
        assert "secrets." not in yaml.safe_dump(job.get("env") or {})
    for _, _, step in _agent_steps(doc):
        for name, value in (step.get("env") or {}).items():
            assert not name.startswith(("JIRA_", "SLACK_", "GCP_", "DELIVERY_APP_", "GOOGLE_")), name
            assert name != "AGENT_PUSH_TOKEN"
            assert "secrets." not in str(value), name
        secrets_used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", yaml.safe_dump(step["with"])))
        assert secrets_used == {"ANTHROPIC_API_KEY"}
        assert step["with"]["github_token"] == "${{ github.token }}"
        args, tools = _allowed_tools(step)
        for tool in tools:
            assert tool in FILE_TOOLS or BASH_PREFIX.fullmatch(tool), f"not allowed: {tool}"
            low = tool.lower()
            for banned in ("git push", "bash(gh", "curl", "wget", "webfetch", "websearch",
                           "make e2e", "e2e_local_backend", "git config", "git remote"):
                assert banned not in low, tool
        for banned in ("WebFetch", "WebSearch", "dangerously", "bypassPermissions", "mcp__"):
            assert banned not in args


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_untrusted_interpolation_and_actions_pinned(path):
    doc, triggers = _wf(path)
    assert "pull_request_target" not in triggers
    assert "pull_request_target" not in path.read_text()
    for job_id, job in doc["jobs"].items():
        if "uses" in job:  # reusable workflow
            assert job["uses"].startswith("./") or SHA_PIN.fullmatch(job["uses"]), job["uses"]
    for job_id, _, step in _steps(doc):
        run = step.get("run", "")
        assert not UNTRUSTED_EXPR.search(run), f"{path.name}:{job_id}: event text in run:"
        if path.name.startswith("agent-"):
            # Stricter for agent workflows: values reach scripts only through env.
            assert "${{" not in run, f"{path.name}:{job_id}: expression in run:"
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert SHA_PIN.fullmatch(uses.split()[0]), f"{path.name}: {uses} is not pinned by SHA"


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_push_token_only_in_publish_steps(path):
    doc, _ = _wf(path)
    assert "AGENT_PUSH_TOKEN" not in yaml.safe_dump(doc.get("env") or {})
    for job_id, job in doc["jobs"].items():
        assert "AGENT_PUSH_TOKEN" not in yaml.safe_dump(job.get("env") or {})
        holders = [s for s in job.get("steps", []) if "AGENT_PUSH_TOKEN" in yaml.safe_dump(s)]
        for step in holders:
            assert step.get("name", "").startswith(("Publish", "Push")), f"{job_id}: {step.get('name')}"
            assert "uses" not in step  # only our own shell, no third-party code
        if holders:
            # The push token never shares a job (a runner) with the agent.
            assert not any(AGENT_ACTION in s.get("uses", "") for s in job["steps"]), job_id


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_agent_permissions_least_privilege(path):
    doc, _ = _wf(path)
    text = path.read_text()
    assert "write-all" not in text and "read-all" not in text
    assert doc["permissions"] == {"contents": "read"}
    for job_id, job in doc["jobs"].items():
        perms = job.get("permissions", doc["permissions"])
        assert isinstance(perms, dict), job_id
        assert perms.get("contents") == "read", job_id
        assert "id-token" not in perms
        for scope, level in perms.items():
            if path.name == "agent-dispatch.yml" and scope == "actions":
                assert level == "write"
            else:
                assert level in ("read", "none"), f"{path.name}:{job_id} {scope}: {level}"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_merge_step_anywhere(path):
    text = path.read_text()
    for banned in ("gh pr merge", "--auto", "enable-auto-merge", "merge_method", "--force", "push -f"):
        assert banned not in text, f"{path.name}: {banned}"
    assert not re.search(r"git merge(\s|$)", text), f"{path.name}: git merge"


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_agent_checkouts_never_persist_credentials(path):
    doc, _ = _wf(path)
    for job_id, _, step in _steps(doc):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False, job_id


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_execution_log_and_ticket_are_never_uploaded(path):
    doc, _ = _wf(path)
    for job_id, _, step in _steps(doc):
        if step.get("uses", "").startswith("actions/upload-artifact@"):
            upload = str(step["with"]["path"]).rstrip("/")
            assert "claude-execution-output" not in upload
            # The ticket goes only to the agent's job; the hand-off back holds
            # no ticket text (outcome, result summary, gate codes, bundle).
            expected = "/ticket" if step["with"]["name"].startswith("ticket-") else "/handoff"
            assert upload.endswith(expected), (job_id, upload)
            assert step["with"]["retention-days"] <= 3


def test_dispatch_schedule_queue_and_start():
    doc, triggers = _wf(WORKFLOWS / "agent-dispatch.yml")
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "*/15 3-15 * * 1-6"}]
    job = doc["jobs"]["dispatch"]
    assert job["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "read",
                                  "issues": "read"}
    text = (WORKFLOWS / "agent-dispatch.yml").read_text()
    assert "ANTHROPIC_API_KEY" not in text and "AGENT_PUSH_TOKEN" not in text
    assert "scripts/delivery/agent_queue.py next" in text
    assert "gh workflow run agent-spec.yml --ref main" in text
    assert "--label agent-pr" in text
    pick = next(s for s in job["steps"] if "agent_queue.py" in s.get("run", ""))
    assert {"AGENTS_ENABLED", "AGENT_MAX_CONCURRENT", "AGENT_RUNNING", "OPEN_SPEC_PR_KEYS",
            "JIRA_EMAIL", "JIRA_API_TOKEN"} <= set(pick["env"])
    start = next(s for s in job["steps"] if "gh workflow run" in s.get("run", ""))
    assert start["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "secrets." not in yaml.safe_dump(start)


def test_spec_stage_shape():
    path = WORKFLOWS / "agent-spec.yml"
    doc, triggers = _wf(path)
    assert set(triggers) == {"workflow_dispatch"}
    issue = triggers["workflow_dispatch"]["inputs"]["issue"]
    assert issue["required"] is True
    assert doc["concurrency"] == {"group": "agent-${{ inputs.issue }}", "cancel-in-progress": False}
    for job_id, job in doc["jobs"].items():
        assert job["timeout-minutes"] <= 30, job_id
    text = path.read_text()
    # The key is checked before any API call, and reaches scripts via env only.
    first = doc["jobs"]["prepare"]["steps"]
    validate = next(s for s in first if "guard_branch.py key" in s.get("run", ""))
    assert validate["env"]["ISSUE"] == "${{ inputs.issue }}"
    assert first.index(validate) < min(
        i for i, s in enumerate(first) if "jira_ops.py" in s.get("run", "")
    )
    for needed in ("guard_branch.py already-ran", "jira_ops.py fetch", "--to Spec", "--add agent-running",
                   "--reason-category already-ran", "stage_io.py outcome --stage spec",
                   "stage_io.py spec-paths", "guard_branch.py check", "gh pr create --draft",
                   "--base development", "--label agent-pr", "--remove agent-running",
                   "notify_slack.py post", "--to Blocked", "pre-commit install"):
        assert needed in text, needed
    agent = doc["jobs"]["agent"]
    (_, _, step), = _agent_steps(doc)
    assert ".github/agent-prompts/spec.md" in step["with"]["prompt"]
    _, tools = _allowed_tools(step)
    assert "Bash(make test:*)" not in tools
    assert agent["permissions"] == {"contents": "read"}
    cleanup = doc["jobs"]["cleanup"]
    assert "failure()||cancelled()" in _flat(cleanup["if"])
    body = "\n".join(step.get("run", "") for step in cleanup["steps"])
    assert "workflow-failed" in body and "--remove agent-running" in body and "--to Blocked" in body


def test_implement_stage_shape():
    path = WORKFLOWS / "agent-implement.yml"
    doc, triggers = _wf(path)
    # Runs from `main` only: workflow_dispatch, never a push (any writer could push a modified
    # copy of this file and read the secrets).
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"issue", "pr", "sha"}
    assert all(i["required"] is True for i in inputs.values())
    assert doc["concurrency"] == {"group": "agent-${{ inputs.issue }}", "cancel-in-progress": False}
    text = path.read_text()
    for needed in ("g2_gate.py check", "g2_gate.py git-check", "--to \"In Progress\"", "--add agent-running",
                   "qdrant/qdrant:v1.18.3", "requirements.lock.txt", "make lint", "make test",
                   "stage_io.py outcome --stage implement", "stage_io.py decide", "review_labels.py",
                   "gh pr ready", "needs-human-review", "gh pr comment", "--to \"In Review\"",
                   "--to Blocked", "guard_branch.py check", "notify_slack.py post",
                   "--add-label agent-implemented", "--reason-category g2-refused"):
        assert needed in text, needed
    (_, _, step), = _agent_steps(doc)
    assert ".github/agent-prompts/implement.md" in step["with"]["prompt"]
    assert "g2.json" in step["with"]["prompt"]
    args, tools = _allowed_tools(step)
    for tool in ("Bash(make test:*)", "Bash(python -m pytest:*)", "Bash(ruff check:*)"):
        assert tool in tools
    assert "e2e" not in args
    # `gh pr ready` only when the publish decision says ready.
    assert re.search(r'if \[ "\$READY" = "true" \]; then\s+gh pr ready', text)
    cleanup = doc["jobs"]["cleanup"]
    assert "failure()||cancelled()" in _flat(cleanup["if"])
    body = "\n".join(step.get("run", "") for step in cleanup["steps"])
    assert "workflow-failed" in body and "--remove agent-running" in body and "--to Blocked" in body


def test_implement_inputs_are_checked_before_any_api_call_and_reach_scripts_via_env():
    doc, _ = _wf(WORKFLOWS / "agent-implement.yml")
    steps = doc["jobs"]["prepare"]["steps"]
    validate = next(i for i, s in enumerate(steps) if "g2_gate.py inputs" in s.get("run", ""))
    first_api = min(i for i, s in enumerate(steps) if re.search(r"gh api|jira_ops\.py", s.get("run", "")))
    assert validate < first_api
    env = steps[validate]["env"]
    assert env == {"ISSUE": "${{ inputs.issue }}", "PR": "${{ inputs.pr }}", "SHA": "${{ inputs.sha }}"}
    # every later step uses the checked outputs, never the raw inputs
    for step in steps[validate + 1:]:
        assert "inputs." not in yaml.safe_dump(step), step.get("name")
    for job_id, job in doc["jobs"].items():
        if job_id != "prepare":
            assert "inputs." not in _job_text(job), job_id


def test_implement_prepare_decides_g2_from_the_pr_label():
    doc, _ = _wf(WORKFLOWS / "agent-implement.yml")
    prepare = doc["jobs"]["prepare"]
    assert _flat(prepare["if"]) == KILL_SWITCH  # no sender check: G2 is the label, read from the timeline
    assert prepare["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "read"}
    body = _run(prepare)
    for needed in ("issues/${PR}/timeline", "--paginate", "pulls/${PR}", "activity?ref=refs/heads/",
                   '--approvers "$DESIGN_APPROVERS"', "--record", "g2.json"):
        assert needed in body, needed
    gate = next(s for s in prepare["steps"] if "g2_gate.py check" in s.get("run", ""))
    assert gate["env"]["DESIGN_APPROVERS"] == "${{ vars.DESIGN_APPROVERS }}"
    assert "secrets." not in yaml.safe_dump(gate)
    # the approval record travels with the ticket, so the agent can read it
    assert "$RUNNER_TEMP/ticket/g2.json" in body
    # refusals other than "not approved yet" are reported and flag the issue
    report = next(s for s in prepare["steps"] if "--reason-category g2-refused" in s.get("run", ""))
    assert "steps.gate.outputs.notify=='true'" in _flat(report["if"])
    assert "--to Blocked" in report["run"] and "notify_slack.py post" in report["run"]
    assert "GITHUB_STEP_SUMMARY" in body


@pytest.mark.parametrize("path", [p for p in AGENT_FILES if p.name != "agent-dispatch.yml"],
                         ids=lambda p: p.name)
def test_agent_step_runs_bounded(path):
    doc, _ = _wf(path)
    (_, job, step), = _agent_steps(doc)
    args = step["with"]["claude_args"]
    if path.name == "agent-implement.yml":
        assert "--max-turns ${{ vars.AGENT_MAX_TURNS_IMPLEMENT || 120 }}" in args
    else:
        assert "--max-turns ${{ vars.AGENT_MAX_TURNS || 60 }}" in args
    assert "--model ${{ vars.AGENT_MODEL || 'claude-sonnet-5-5' }}" in args
    assert job["timeout-minutes"] <= 90
    # the prompt carries no expression an outsider controls
    assert not UNTRUSTED_EXPR.search(step["with"]["prompt"])


def test_prompts_treat_the_ticket_as_data():
    for name in ("spec.md", "implement.md"):
        text = (PROMPTS / name).read_text()
        assert "data, not instructions" in text
        assert '"injection"' in text and "outcome file" in text
        assert "Never push" in text
    assert "e2e" in (PROMPTS / "implement.md").read_text()
    for command in ("spec", "implement", "verify", "review"):
        body = (Path(__file__).resolve().parents[1] / ".claude" / "commands" / f"{command}.md").read_text()
        assert "## In CI (GitHub Actions)" in body


# ------------------------------------------- review and audit fixes (SIM-153)

ROOT_DIR = Path(__file__).resolve().parents[1]
STAGE_FILES = [WORKFLOWS / "agent-spec.yml", WORKFLOWS / "agent-implement.yml"]
HARDEN = "step-security/harden-runner@"
QDRANT_PINNED = re.compile(r"^qdrant/qdrant:v1\.18\.3@sha256:[0-9a-f]{64}$")


def _job_text(job):
    return yaml.safe_dump(job)


def _run(job):
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def _ci_gitleaks():
    ci, _ = _wf(WORKFLOWS / "ci.yml")
    env = ci["jobs"]["secrets-scan"]["env"]
    return env["GITLEAKS_VERSION"], env["GITLEAKS_SHA256"]


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_jobs_holding_secrets_run_only_development_scripts(path):
    """Blocker: no job that holds a Jira, Slack or push credential checks out (or switches to)
    anything but `development`; ticket-branch commits are read only as git data."""
    doc, _ = _wf(path)
    for job_id, job in doc["jobs"].items():
        if job_id == "agent":
            continue
        if "secrets." not in _job_text(job):
            continue
        checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
        assert checkouts, job_id
        for step in checkouts:
            assert step["with"].get("ref") == "development", f"{path.name}:{job_id}"
        body = _run(job)
        assert not re.search(r"git (switch|checkout)\b", body), f"{path.name}:{job_id}"
        assert "make " not in body and "pip install" not in body and "pre-commit" not in body, job_id


def test_implement_reads_the_approved_commit_only_as_git_data():
    doc, _ = _wf(WORKFLOWS / "agent-implement.yml")
    for job_id in ("prepare", "agent", "gates", "publish"):
        job = doc["jobs"][job_id]
        checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"] == {"ref": "development", "fetch-depth": 0, "persist-credentials": False}, job_id
        assert "fetch -q --no-tags origin" in _run(job), job_id
    body = _run(doc["jobs"]["prepare"])
    assert "g2_gate.py git-check" in body and "--base origin/development" in body


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_publish_refuses_protected_paths_and_secrets_before_pushing(path):
    doc, _ = _wf(path)
    publish = doc["jobs"]["publish"]
    stage = "implement" if "implement" in path.name else "spec"
    steps = publish["steps"]
    names = [s.get("name", "") for s in steps]
    check = next(i for i, s in enumerate(steps) if f"stage_io.py agent-paths --stage {stage}" in s.get("run", ""))
    scan = next(i for i, s in enumerate(steps) if "gitleaks git" in s.get("run", ""))
    push = next(i for i, s in enumerate(steps) if "git push" in s.get("run", ""))
    assert check < push and scan < push, names
    body = steps[scan]["run"]
    assert "--redact" in body and "secret-found" in body
    assert "--no-renames --name-only" in steps[check]["run"]
    install = next(s for s in steps if "sha256sum --check --strict" in s.get("run", ""))
    version, digest = _ci_gitleaks()
    assert publish["env"]["GITLEAKS_VERSION"] == version
    assert publish["env"]["GITLEAKS_SHA256"] == digest
    assert "secrets." not in yaml.safe_dump(install)


def test_gates_run_on_a_fresh_runner_without_secrets():
    doc, _ = _wf(WORKFLOWS / "agent-implement.yml")
    gates = doc["jobs"]["gates"]
    assert set(gates["needs"]) == {"prepare", "agent"}
    assert "secrets." not in _job_text(gates)
    assert gates["permissions"] == {"contents": "read"}
    assert QDRANT_PINNED.fullmatch(gates["services"]["qdrant"]["image"])
    checkout = next(s for s in gates["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "development"
    body = _run(gates)
    assert "make lint" in body and "make test" in body
    assert "git bundle verify" in body and "merge-base --is-ancestor" in body
    # deps are installed from development before the agent's tree is checked out
    assert body.index("pip install") < body.index("git switch -q --detach")
    assert {"lint", "test", "head"} <= set(gates["outputs"])
    # the agent job no longer runs the gates itself
    agent_body = _run(doc["jobs"]["agent"])
    assert "make test" not in agent_body and "make lint" not in agent_body
    publish = doc["jobs"]["publish"]
    assert "gates" in publish["needs"]
    text = _job_text(publish)
    for needed in ("needs.gates.result", "needs.gates.outputs.lint", "needs.gates.outputs.test",
                   "needs.gates.outputs.head"):
        assert needed in text, needed


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_no_log_tails_and_only_normalised_outcomes_leave_the_agent_job(path):
    doc, _ = _wf(path)
    text = path.read_text()
    assert "tail -" not in text and "cat \"$RUNNER_TEMP/test.log\"" not in text
    agent = doc["jobs"]["agent"]
    collect = next(s for s in agent["steps"] if s.get("name") == "Collect the agent's work")
    assert "stage_io.py\" outcome --stage" in collect["run"]
    assert 'cp "$outcome"' not in collect["run"]
    # the trusted copy of the scripts is taken before the agent runs
    steps = agent["steps"]
    snapshot = next(i for i, s in enumerate(steps) if "git archive" in s.get("run", ""))
    claude = next(i for i, s in enumerate(steps) if AGENT_ACTION in s.get("uses", ""))
    assert snapshot < claude
    for step in steps:
        if step.get("uses", "").startswith("actions/upload-artifact@"):
            assert step["with"]["retention-days"] == 1


@pytest.mark.parametrize("path,jobs", [
    (WORKFLOWS / "agent-spec.yml", ("agent",)),
    (WORKFLOWS / "agent-implement.yml", ("agent", "gates")),
], ids=["spec", "implement"])
def test_harden_runner_is_the_first_step(path, jobs):
    doc, _ = _wf(path)
    for job_id in jobs:
        first = doc["jobs"][job_id]["steps"][0]
        assert first["uses"].startswith(HARDEN) and SHA_PIN.fullmatch(first["uses"].split()[0]), job_id
        assert first["with"]["egress-policy"] == "block", job_id
        allowed = first["with"]["allowed-endpoints"].split()
        assert "pypi.org:443" in allowed and "files.pythonhosted.org:443" in allowed
        if job_id == "agent":
            assert "api.anthropic.com:443" in allowed
        else:
            assert "api.anthropic.com:443" not in allowed
        # no sudo and no container access for code the agent wrote (input name read from
        # action.yml at the pinned SHA; `disable-sudo` alone is deprecated there)
        assert first["with"]["disable-sudo-and-containers"] == "true", job_id


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_qdrant_pinned_by_digest_and_pre_commit_exact(path):
    doc, _ = _wf(path)
    for job_id, job in doc["jobs"].items():
        for service in (job.get("services") or {}).values():
            assert QDRANT_PINNED.fullmatch(service["image"]), job_id
    text = path.read_text()
    assert "|| echo pre-commit" not in text
    if "pre-commit install" in text and "requirements-dev.txt" in text:
        assert re.search(r"pre-commit==", (ROOT_DIR / "requirements-dev.txt").read_text())


def test_implement_publish_titles_unblocks_and_maps_budget():
    path = WORKFLOWS / "agent-implement.yml"
    doc, _ = _wf(path)
    body = _run(doc["jobs"]["publish"])
    assert 'gh pr edit "$PR" --title "${ISSUE}: implementation"' in body
    assert "jira_ops.py unblock" in body
    assert "--budget" in body


def test_spec_stage_scope_check_comes_before_the_fetch():
    path = WORKFLOWS / "agent-spec.yml"
    doc, _ = _wf(path)
    steps = doc["jobs"]["prepare"]["steps"]
    scope = next(i for i, s in enumerate(steps) if "jira_ops.py check-scope" in s.get("run", ""))
    fetch = next(i for i, s in enumerate(steps) if "jira_ops.py fetch" in s.get("run", ""))
    ran = next(i for i, s in enumerate(steps) if "guard_branch.py already-ran" in s.get("run", ""))
    assert scope < ran < fetch
    assert "steps.scope.outputs.inscope=='true'" in _flat(steps[fetch]["if"])
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert "steps.ticket.outputs.go=='true'" in _flat(upload["if"])
    # already ran: flagged and labelled so the queue stops re-dispatching it, unless an open
    # agent PR for the key shows the earlier run is in hand (concurrency > 1)
    report = next(s for s in steps if "--reason-category already-ran" in s.get("run", ""))
    assert "--to Blocked" in report["run"]
    assert re.search(r'if \[ "\$OPEN_AGENT_PR" != "true" \]; then\s+python3 scripts/delivery/jira_ops.py '
                     r'transition --issue "\$ISSUE" --to Blocked', report["run"])
    assert report["env"]["OPEN_AGENT_PR"] == "${{ steps.ran.outputs.open_agent_pr }}"
    ran_step = steps[ran]["run"]
    assert "agentPrs" in ran_step and "labels" in ran_step
    publish = _run(doc["jobs"]["publish"])
    assert "needs-human-review" in publish and "jira_ops.py unblock" in publish
    assert "budget" in publish


def test_dispatch_skips_keys_with_a_ticket_branch():
    doc, _ = _wf(WORKFLOWS / "agent-dispatch.yml")
    job = doc["jobs"]["dispatch"]
    pick = next(s for s in job["steps"] if "agent_queue.py" in s.get("run", ""))
    assert pick["env"]["EXISTING_TICKET_BRANCH_KEYS"] == "${{ steps.count.outputs.branch_keys }}"
    body = _run(job)
    assert "matching-refs/heads/feature/SIM-" in body and "matching-refs/heads/fix/SIM-" in body


def test_push_token_owner_is_never_trusted_as_an_approver_by_default():
    """The push token belongs to an approver (until a machine user exists). G2 is the label
    event's actor, so the pipeline must never add `design-approved` itself, there is no
    built-in approver fallback, and git-check refuses agent commits outside the spec files.
    The design records the machine user as REQUIRED before AGENTS_ENABLED goes beyond the pilot."""
    for path in ALL_FILES:
        text = path.read_text()
        assert "st-heyabhishek" not in text, path.name
        assert not re.search(r"(add-label|--label)[ =]+\"?design-approved", text.replace(
            "gh pr list --state open --label agent-pr --label design-approved", "")), path.name
        assert "labels/design-approved" not in text and '"design-approved"' not in text, path.name
    design = (ROOT_DIR / "specs" / "technical-design" / "SIM-71-automated-delivery-pipeline.md").read_text()
    assert "REQUIRED before `AGENTS_ENABLED=true` beyond the pilot" in design


# ------------------------------------------------ second fix round (SIM-153)


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_workflow_holding_a_secret_runs_on_push_or_pull_request(path):
    """A push- or pull_request-triggered workflow runs the YAML from the pushed branch, so any
    writer could change it and read whatever secrets it references."""
    _, triggers = _wf(path)
    text = path.read_text()
    if "secrets." in text:
        assert not ({"push", "pull_request"} & set(triggers)), path.name
    if path.name == "ci.yml":
        assert {"push", "pull_request"} & set(triggers)
        assert "secrets." not in text


def test_dispatch_starts_implement_runs_for_g2_approved_prs():
    doc, _ = _wf(WORKFLOWS / "agent-dispatch.yml")
    job = doc["jobs"]["dispatch"]
    body = _run(job)
    assert "gh pr list --state open --label agent-pr --label design-approved" in body
    assert "g2_gate.py candidates" in body and "g2_gate.py check" in body
    assert ('gh workflow run agent-implement.yml --ref main -f issue="$issue" -f pr="$pr" -f sha="$sha"'
            in body)
    # the G2 step reads GitHub only; Jira is checked in a separate step with no GitHub write token
    g2_step = next(s for s in job["steps"] if "g2_gate.py check" in s.get("run", ""))
    assert "secrets." not in yaml.safe_dump(g2_step)
    assert g2_step["env"]["DESIGN_APPROVERS"] == "${{ vars.DESIGN_APPROVERS }}"
    scope = next(s for s in job["steps"] if "jira_ops.py check-scope" in s.get("run", ""))
    assert "GH_TOKEN" not in scope["env"]
    # implement runs count against AGENT_MAX_CONCURRENT before any spec run is picked
    pick = next(s for s in job["steps"] if "agent_queue.py" in s.get("run", ""))
    assert "implement_started" in yaml.safe_dump(pick) or "implement_started" in pick["run"]
    start = next(s for s in job["steps"] if "gh workflow run agent-implement.yml" in s.get("run", ""))
    assert "secrets." not in yaml.safe_dump(start)


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_agent_step_scrubs_subprocess_env_and_allows_the_dispatch_bot(path):
    doc, _ = _wf(path)
    (_, job, step), = _agent_steps(doc)
    assert step["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert step["with"]["allowed_bots"] == "github-actions"
    # no pip cache on the agent job: a cache the agent's runner restores is shared state
    for s in job["steps"]:
        if s.get("uses", "").startswith("actions/setup-python@"):
            assert "cache" not in (s.get("with") or {}), path.name


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_publish_refuses_agent_added_gitleaks_allow_before_pushing(path):
    doc, _ = _wf(path)
    steps = doc["jobs"]["publish"]["steps"]
    check = next(i for i, s in enumerate(steps) if "stage_io.py allow-markers" in s.get("run", ""))
    push = next(i for i, s in enumerate(steps) if "git push" in s.get("run", ""))
    assert check < push
    assert "git log -p --no-renames --format=" in steps[check]["run"]
    assert "secret" in steps[check]["run"]


@pytest.mark.parametrize("path", STAGE_FILES, ids=lambda p: p.name)
def test_comments_say_agent_side_normalisation_is_not_a_control(path):
    assert "not a control" in path.read_text()


def test_implement_publish_pushes_only_on_the_decided_verdict():
    doc, _ = _wf(WORKFLOWS / "agent-implement.yml")
    publish = doc["jobs"]["publish"]
    check = next(s for s in publish["steps"] if "stage_io.py decide" in s.get("run", ""))
    body = check["run"]
    for flag in ("--protected", "--secret", "--gates-mismatch", "--budget"):
        assert flag in body, flag
    # pr-comment renders the final verdict; it does not get the inputs to recompute it
    comment = body[body.index("stage_io.py pr-comment"):]
    assert '--ready "$ready"' in comment and '--reason "$reason"' in comment
    assert "--commits" not in comment.split("\n\n")[0]
    push = next(s for s in publish["steps"] if "git push" in s.get("run", ""))
    assert push["env"]["PUSH"] == "${{ steps.check.outputs.push }}"
    assert re.search(r'if \[ "\$PUSH" = "true" \]; then\s+git push', push["run"])
    # the PR is the dispatched one, re-checked against the branch before any edit
    assert 'gh pr view "$PR" --json headRefName' in push["run"]
    assert "gh pr create" not in push["run"]
