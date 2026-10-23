"""Offline tests for the Slack → Jira triage helpers (SIM-133, SIM-71 AC-15d/e).

The helpers are the only way the triage agent touches Slack and Jira, so their
guards are the hard controls: replies only in a thread of the configured
channel; issues only in SIM, only Bug/Task, labels forced, never twice for the
same Slack message. No network: every HTTP call goes through ``_request``,
which the tests replace.
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "scripts" / "delivery" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"delivery_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def slack(monkeypatch):
    mod = _load("slack_io")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("TRIAGE_CHANNEL_ID", "C0TEST0001")
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        if method == "auth.test":
            return {"ok": True, "url": "https://example-team.slack.com/"}
        if method == "conversations.history":
            return {
                "ok": True,
                "messages": [
                    {"ts": "1792600200.000002", "user": "U1", "text": "Bug: dates missing", "reply_count": 1},
                    {"ts": "1792600100.000001", "bot_id": "B9", "text": "Jira: SIM-1 created"},
                ],
                "has_more": False,
            }
        if method == "conversations.replies":
            return {
                "ok": True,
                "messages": [
                    {"ts": "1792600200.000002", "user": "U1", "text": "Bug: dates missing"},
                    {
                        "ts": "1792600201.000003",
                        "user": "U2",
                        "text": "Draft SIM ticket — awaiting approval",
                        "reactions": [{"name": "white_check_mark", "users": ["U1"], "count": 1}],
                    },
                ],
                "has_more": False,
            }
        if method == "chat.postMessage":
            return {"ok": True, "ts": "1792600300.000004"}
        raise AssertionError(f"unexpected Slack method {method}")

    monkeypatch.setattr(mod, "_request", fake_request)
    return mod, calls


@pytest.fixture
def jira(monkeypatch):
    mod = _load("jira_triage")
    monkeypatch.setenv("JIRA_COMPONENT_FIELD", "customfield_10075")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "token-test")
    state = {"existing": [], "calls": []}

    def fake_request(method, path, body=None):
        state["calls"].append((method, path, body))
        if path.startswith("/rest/api/3/search/jql"):
            return {"issues": state["existing"]}
        if method == "POST" and path == "/rest/api/2/issue":
            return {"key": "SIM-999"}
        raise AssertionError(f"unexpected Jira call {method} {path}")

    monkeypatch.setattr(mod, "_request", fake_request)
    return mod, state


# ---------------------------------------------------------------- Slack


def test_read_returns_threads_reactions_links_and_flags_bots(slack):
    mod, _ = slack
    out = mod.read_channel(days=7)
    assert [m["ts"] for m in out["messages"]] == ["1792600100.000001", "1792600200.000002"]
    bot, human = out["messages"]
    assert bot["is_bot"] is True and human["is_bot"] is False
    assert human["link"] == "https://example-team.slack.com/archives/C0TEST0001/p1792600200000002"
    reply = human["replies"][0]
    assert reply["text"].startswith("Draft SIM ticket")
    assert reply["reactions"] == [{"name": "white_check_mark", "users": ["U1"]}]


def test_reply_refuses_top_level_and_other_channels(slack):
    mod, calls = slack
    with pytest.raises(mod.TriageRefused):
        mod.reply(thread_ts="", text="hello")
    with pytest.raises(mod.TriageRefused):
        mod.reply(thread_ts="not-a-ts", text="hello")
    with pytest.raises(mod.TriageRefused):
        mod.reply(thread_ts="1792600200.000002", text="   ")
    # There is no channel parameter at all: the only possible target is the
    # configured channel, in a thread.
    assert "channel" not in mod.reply.__code__.co_varnames[: mod.reply.__code__.co_argcount]
    mod.reply(thread_ts="1792600200.000002", text="Not a ticket\n\nSee the docs.")
    method, params = calls[-1]
    assert method == "chat.postMessage"
    assert params["channel"] == "C0TEST0001"
    assert params["thread_ts"] == "1792600200.000002"
    assert params.get("reply_broadcast") in (None, False)


def test_slack_cli_reply_reads_text_from_stdin(slack, monkeypatch, capsys):
    mod, calls = slack
    monkeypatch.setattr("sys.stdin", io.StringIO("Draft SIM ticket — awaiting approval\nbody"))
    assert mod.main(["reply", "--thread-ts", "1792600200.000002"]) == 0
    assert calls[-1][1]["text"].startswith("Draft SIM ticket")
    monkeypatch.setattr("sys.stdin", io.StringIO("x"))
    assert mod.main(["reply", "--thread-ts", "bad"]) != 0


def test_slack_requires_configuration(monkeypatch):
    mod = _load("slack_io")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TRIAGE_CHANNEL_ID", raising=False)
    with pytest.raises(mod.TriageRefused):
        mod.reply(thread_ts="1792600200.000002", text="hi")


# ---------------------------------------------------------------- Jira


def test_search_is_always_scoped_to_sim(jira):
    mod, state = jira
    mod.search('labels = from-slack OR project = OTHER')
    _, path, body = state["calls"][-1]
    assert body["jql"] == "project = SIM AND (labels = from-slack OR project = OTHER)"


def test_create_refuses_out_of_scope_and_duplicates(jira):
    mod, state = jira
    link = "https://example-team.slack.com/archives/C0TEST0001/p1792600200000002"
    with pytest.raises(mod.TriageRefused):
        mod.create(component="AI", issue_type="Epic", summary="x", priority="P2", source_link=link, description="d")
    with pytest.raises(mod.TriageRefused):
        mod.create(component="AI", issue_type="Bug", summary="x", priority="P9", source_link=link, description="d")
    with pytest.raises(mod.TriageRefused):
        mod.create(component="AI", issue_type="Bug", summary="x", priority="P2", source_link="https://elsewhere.test/x", description="d")
    with pytest.raises(mod.TriageRefused):
        mod.create(component="AI", issue_type="Bug", summary=" ", priority="P2", source_link=link, description="d")

    state["existing"] = [{"key": "SIM-5", "fields": {"summary": "already there"}}]
    assert mod.create(component="AI", issue_type="Bug", summary="x", priority="P2", source_link=link, description="d") == {
        "key": "SIM-5",
        "created": False,
    }
    assert not [c for c in state["calls"] if c[1] == "/rest/api/2/issue"]

    state["existing"] = []
    out = mod.create(component="AI", issue_type="Task", summary="Export case laws", priority="P3", source_link=link, description="Problem")
    assert out == {"key": "SIM-999", "created": True}
    _, _, body = state["calls"][-1]
    fields = body["fields"]
    assert fields["project"] == {"key": "SIM"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["priority"] == {"name": "Medium"}
    assert sorted(fields["labels"]) == ["ai-repo", "from-slack"]
    assert fields["customfield_10075"] == {"value": "AI"}
    assert fields["description"].rstrip().endswith(f"Source: {link}")


def test_jira_cli_has_no_edit_transition_or_delete():
    mod = _load("jira_triage")
    parser = mod.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001
    assert commands == {"search", "create"}


def test_jira_cli_create_reads_description_from_stdin(jira, monkeypatch, capsys):
    mod, state = jira
    monkeypatch.setattr("sys.stdin", io.StringIO("Problem: dates missing"))
    rc = mod.main([
        "create", "--component", "FE", "--type", "Bug", "--summary", "Dates missing", "--priority", "P2",
        "--source-link", "https://example-team.slack.com/archives/C0TEST0001/p1792600200000002",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["key"] == "SIM-999"


def test_create_component_and_labels(jira):
    mod, state = jira
    link = "https://example-team.slack.com/archives/C0TEST0001/p1792600200000002"
    with pytest.raises(mod.TriageRefused):
        mod.create(component="Marketing", issue_type="Bug", summary="x", priority="P2", source_link=link, description="d")
    expected = {"AI": "ai-repo", "BE": "be-repo", "FE": "fe-repo", "Admin": "admin-repo", "DevOps": "devops", "QA": "qa"}
    for component, label in expected.items():
        mod.create(component=component, issue_type="Task", summary="x", priority="P3", source_link=link, description="d")
        fields = state["calls"][-1][2]["fields"]
        assert fields["customfield_10075"] == {"value": component}
        assert sorted(fields["labels"]) == sorted(["from-slack", label])


# ---------------------------------------------------------------- Confluence


@pytest.fixture
def confluence(monkeypatch):
    mod = _load("confluence_docs")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "token-test")
    calls = []

    def fake_request(path, params):
        calls.append((path, params))
        if path == "/wiki/rest/api/search":
            return {"results": [{
                "content": {"id": "4489221", "title": "Sharing, transfer and revoking access"},
                "excerpt": "Lawyers can share a case",
                "resultGlobalContainer": {"title": "PracSys Engineering"},
            }]}
        if path == "/wiki/rest/api/content/5390000":
            return {"id": "5390000", "title": "Other", "space": {"key": "OTHER"},
                    "body": {"storage": {"value": "<p>secret</p>"}}}
        if path == "/wiki/rest/api/content/4489221":
            return {"id": "4489221", "title": "Sharing", "space": {"key": "PRACENG"},
                    "body": {"storage": {"value": "<h2>In plain words</h2><p>Share &amp; revoke.</p>"}}}
        raise AssertionError(path)

    monkeypatch.setattr(mod, "_request", fake_request)
    return mod, calls


def test_confluence_scoped_to_praceng(confluence):
    mod, calls = confluence
    hits = mod.search('sharing" OR space = "OTHER')
    cql = calls[-1][1]["cql"]
    assert cql.startswith('space = "PRACENG" AND type = page AND text ~ ')
    assert 'OR space = "OTHER"' not in cql.replace('\\"', "")  # quotes in the query are escaped
    assert hits[0]["id"] == "4489221"
    assert hits[0]["url"].endswith("/wiki/spaces/PRACENG/pages/4489221")
    with pytest.raises(mod.TriageRefused):
        mod.read("5390000")
    with pytest.raises(mod.TriageRefused):
        mod.read("not-an-id")
    page = mod.read("4489221")
    assert page["text"] == "In plain words\nShare & revoke."
