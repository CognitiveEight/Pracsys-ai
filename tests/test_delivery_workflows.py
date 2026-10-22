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
