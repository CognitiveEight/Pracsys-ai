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
    assert job["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "read"}
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
    assert set(triggers) == {"push"}
    assert triggers["push"]["branches"] == ["feature/SIM-*", "fix/SIM-*"]
    assert triggers["push"]["paths"] == ["specs/technical-design/**"]
    gate = doc["jobs"]["prepare"]
    cond = _flat(gate["if"])
    assert "contains(format(',{0},',vars.DESIGN_APPROVERS),format(',{0},',github.event.sender.login))" in cond
    assert "github.event.sender.type!='Bot'" in cond
    text = path.read_text()
    for needed in ("stage_io.py design-gate", "--to \"In Progress\"", "--add agent-running",
                   "qdrant/qdrant:v1.18.3", "requirements.lock.txt", "make lint", "make test",
                   "stage_io.py outcome --stage implement", "stage_io.py decide", "review_labels.py",
                   "gh pr ready", "needs-human-review", "gh pr comment", "--to \"In Review\"",
                   "--to Blocked", "guard_branch.py check", "notify_slack.py post"):
        assert needed in text, needed
    (_, _, step), = _agent_steps(doc)
    assert ".github/agent-prompts/implement.md" in step["with"]["prompt"]
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


@pytest.mark.parametrize("path", [p for p in AGENT_FILES if p.name != "agent-dispatch.yml"],
                         ids=lambda p: p.name)
def test_agent_step_runs_bounded(path):
    doc, _ = _wf(path)
    (_, job, step), = _agent_steps(doc)
    args = step["with"]["claude_args"]
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
