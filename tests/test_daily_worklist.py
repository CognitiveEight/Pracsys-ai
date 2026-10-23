"""Offline tests for the daily PracSys worklist (SIM-151). No network: the Jira
search and the Slack post are replaced. Fictional ticket data only."""

import importlib.util
import io
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 24)


def _load():
    path = ROOT / "scripts" / "delivery" / "daily_worklist.py"
    spec = importlib.util.spec_from_file_location("daily_worklist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue(key, summary="Fix the thing", status="In Progress", priority="High",
           assignee="Asha Test", updated="2026-10-22T10:00:00.000+0530",
           component="AI", flagged=False, due=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "priority": {"name": priority},
            "assignee": {"displayName": assignee} if assignee else None,
            "updated": updated,
            "duedate": due,
            "customfield_10075": {"value": component} if component else None,
            "customfield_10021": [{"value": "Impediment"}] if flagged else None,
        },
    }


@pytest.fixture
def wl():
    return _load()


def test_in_progress_grouped_by_assignee(wl):
    issues = [
        _issue("SIM-3", assignee="Ravi Test", updated="2026-10-23T09:00:00.000+0530"),
        _issue("SIM-1", assignee=None, status="In Review"),
        _issue("SIM-2", assignee="Asha Test", updated="2026-10-20T09:00:00.000+0530", component="FE", priority="Medium"),
        _issue("SIM-4", assignee="Asha Test", updated="2026-10-22T09:00:00.000+0530"),
    ]
    text = wl.build_message(in_progress=issues, todo=[], todo_more=False, blocked=[], today=TODAY)
    section = text.split("*In progress (4)*")[1].split("*Next up")[0]
    assert section.index("Asha Test") < section.index("Ravi Test") < section.index("Unassigned")
    asha = section.split("Asha Test")[1].split("Ravi Test")[0]
    assert asha.index("SIM-2") < asha.index("SIM-4")  # oldest update first
    assert "· FE · P3 · In Progress · 4 days since update" in asha


def test_next_up_top_ten(wl):
    todo = [_issue(f"SIM-{i}", status="To Do", priority=p)
            for i, p in enumerate(["Low", "Highest", "Medium", "High"] * 3 + ["Medium", "Lowest"], start=10)]
    text = wl.build_message(in_progress=[], todo=todo, todo_more=False, blocked=[], today=TODAY)
    section = text.split("*Next up")[1].split("*Blocked")[0]
    assert section.startswith(" (top 10 of 14)*")
    keys = [line.split("|")[1].split(">")[0] for line in section.splitlines() if "browse/" in line]
    assert len(keys) == 10
    priorities = [line.split(" · ")[2] for line in section.splitlines() if "browse/" in line]
    assert priorities == sorted(priorities)  # P1 before P4


def test_blocked_and_overdue(wl):
    blocked = [
        _issue("SIM-20", flagged=True, status="To Do"),
        _issue("SIM-21", due="2026-10-20", status="In Progress", component="BE"),
    ]
    text = wl.build_message(in_progress=[], todo=[], todo_more=False, blocked=blocked, today=TODAY)
    section = text.split("*Blocked and overdue (2)*")[1]
    assert "SIM-20" in section and "flagged" in section
    assert "SIM-21" in section and "due 2026-10-20" in section and "· BE ·" in section


def test_empty_sections(wl):
    text = wl.build_message(in_progress=[], todo=[], todo_more=False, blocked=[], today=TODAY)
    assert text.startswith("*PracSys worklist — Saturday, 24 Oct 2026*")
    assert text.count("None") == 3


def test_escapes_slack_characters(wl):
    text = wl.build_message(
        in_progress=[_issue("SIM-5", summary="Fix <b> & > tags")], todo=[], todo_more=False, blocked=[], today=TODAY
    )
    assert "Fix &lt;b&gt; &amp; &gt; tags" in text


def test_length_cap(wl):
    many = [_issue(f"SIM-{i}", summary="A long ticket summary " * 4, assignee=f"Person {i % 7}") for i in range(200)]
    text = wl.build_message(in_progress=many, todo=many[:10], todo_more=True, blocked=many, today=TODAY)
    assert len(text) <= 3900
    assert "… and " in text and " more" in text


def test_posts_only_to_daily_channel(wl, monkeypatch):
    posted = []
    monkeypatch.setenv("DAILY_CHANNEL_ID", "C0DAILY0001")
    monkeypatch.setattr(wl, "_jira_search", lambda jql, limit: ([], False))
    monkeypatch.setattr(wl, "_slack_post", lambda channel, text: posted.append((channel, text)))
    assert wl.main(["post"]) == 0
    assert len(posted) == 1 and posted[0][0] == "C0DAILY0001"


def test_dry_run_does_not_post(wl, monkeypatch, capsys):
    monkeypatch.setenv("DAILY_CHANNEL_ID", "C0DAILY0001")
    monkeypatch.setattr(wl, "_jira_search", lambda jql, limit: ([], False))
    monkeypatch.setattr(wl, "_slack_post", lambda channel, text: pytest.fail("posted in dry run"))
    assert wl.main(["post", "--dry-run"]) == 0
    assert "PracSys worklist" in capsys.readouterr().out


def test_missing_channel_refused(wl, monkeypatch):
    monkeypatch.delenv("DAILY_CHANNEL_ID", raising=False)
    monkeypatch.setattr(wl, "_jira_search", lambda jql, limit: ([], False))
    monkeypatch.setattr("sys.stderr", io.StringIO())
    assert wl.main(["post"]) != 0
