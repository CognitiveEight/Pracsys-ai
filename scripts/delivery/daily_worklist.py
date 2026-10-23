"""Daily PracSys worklist for Slack (SIM-151). No AI model; Jira read-only.

    python3 scripts/delivery/daily_worklist.py post [--dry-run]

Reads Jira SIM (all components) and posts one message to ``DAILY_CHANNEL_ID``:
work in progress by person, the next ten To Do tickets by priority, and what
is blocked or overdue. Credentials: ``SLACK_BOT_TOKEN``, ``JIRA_EMAIL``,
``JIRA_API_TOKEN``. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

_MAX_TEXT = 3900
_TODO_SHOWN = 10
_IST = timezone(timedelta(hours=5, minutes=30))
_FIELDS = [
    "summary", "status", "priority", "assignee", "updated", "duedate",
    "customfield_10075", "customfield_10021",
]
_PRIORITY = {"Highest": "P1", "High": "P2", "Medium": "P3", "Low": "P4", "Lowest": "P4"}

#: Active work: the team's workflow is To Do → Spec → In Progress → In Review →
#: QA → Done. Jira files Spec and QA under the "To Do" category, so the
#: queries name statuses, not categories.
ACTIVE_STATUSES = ("Spec", "In Progress", "In Review", "QA")
JQL_IN_PROGRESS = (
    "project = SIM AND status in ("
    + ", ".join(f'"{s}"' for s in ACTIVE_STATUSES)
    + ") AND issuetype != Epic ORDER BY updated ASC"
)
JQL_TODO = 'project = SIM AND status = "To Do" AND issuetype != Epic ORDER BY priority DESC, created ASC'
JQL_BLOCKED = (
    "project = SIM AND statusCategory != Done "
    "AND (Flagged is not EMPTY OR duedate < startOfDay()) ORDER BY priority DESC"
)


def _site() -> str:
    return os.environ.get("JIRA_SITE", "simplertoday.atlassian.net").strip()


# ------------------------------------------------------------------ I/O


def _jira_search(jql: str, limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Issues for ``jql`` (up to ``limit``) and whether more exist."""
    email = os.environ["JIRA_EMAIL"].strip()
    token = os.environ["JIRA_API_TOKEN"].strip()
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    body = json.dumps({"jql": jql, "fields": _FIELDS, "maxResults": limit}).encode()
    req = urllib.request.Request(
        f"https://{_site()}/rest/api/3/search/jql",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Jira host
        data = json.loads(resp.read().decode())
    return data.get("issues", []), not data.get("isLast", True)


def _slack_post(channel: str, text: str) -> None:
    token = os.environ["SLACK_BOT_TOKEN"].strip()
    data = urllib.parse.urlencode(
        {"channel": channel, "text": text, "unfurl_links": "false", "unfurl_media": "false"}
    ).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Slack host
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {body.get('error', 'unknown_error')}")


# ------------------------------------------------------------- formatting


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _f(issue: Dict[str, Any], name: str) -> Any:
    return (issue.get("fields") or {}).get(name)


def _priority(issue: Dict[str, Any]) -> str:
    return _PRIORITY.get((_f(issue, "priority") or {}).get("name", ""), "P3")


def _component(issue: Dict[str, Any]) -> str:
    value = _f(issue, "customfield_10075")
    if isinstance(value, list):
        value = value[0] if value else None
    return (value or {}).get("value", "—") if isinstance(value, dict) else "—"


def _head(issue: Dict[str, Any]) -> str:
    key = issue.get("key", "")
    return f"<https://{_site()}/browse/{key}|{key}> {_esc(_f(issue, 'summary') or '')}"


def _days_since(stamp: str, today: date) -> int:
    try:
        updated = datetime.strptime(stamp[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    return max(0, (today - updated).days)


def _in_progress_lines(issues: List[Dict[str, Any]], today: date) -> List[Tuple[str, List[str]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        who = (_f(issue, "assignee") or {}).get("displayName") or "Unassigned"
        groups.setdefault(who, []).append(issue)
    ordered = sorted(groups, key=lambda name: (name == "Unassigned", name.lower()))
    out = []
    for name in ordered:
        items = sorted(groups[name], key=lambda i: _f(i, "updated") or "")
        lines = []
        for issue in items:
            days = _days_since(_f(issue, "updated") or "", today)
            status = (_f(issue, "status") or {}).get("name", "")
            unit = "day" if days == 1 else "days"
            lines.append(
                f"  – {_head(issue)} · {_component(issue)} · {_priority(issue)} · "
                f"{status} · {days} {unit} since update"
            )
        out.append((name, lines))
    return out


def _todo_lines(issues: List[Dict[str, Any]]) -> List[str]:
    ranked = sorted(issues, key=_priority)  # stable: keeps Jira's oldest-first within a priority
    return [f"  – {_head(i)} · {_component(i)} · {_priority(i)}" for i in ranked[:_TODO_SHOWN]]


def _blocked_lines(issues: List[Dict[str, Any]], today: date) -> List[str]:
    lines = []
    for issue in issues:
        reasons = []
        if _f(issue, "customfield_10021"):
            reasons.append("flagged")
        due = _f(issue, "duedate")
        if due and due < today.isoformat():
            reasons.append(f"due {due}")
        lines.append(f"  – {_head(issue)} · {_component(issue)} · {', '.join(reasons) or 'blocked'}")
    return lines


def _blocks(
    *,
    in_progress: List[Dict[str, Any]],
    todo: List[Dict[str, Any]],
    todo_more: bool,
    blocked: List[Dict[str, Any]],
    today: date,
) -> List[str]:
    """The worklist as whole blocks: the header, each section title, and each
    person's group. A message may be split between blocks, never inside one
    (unless a single block is itself too long)."""
    header = f"*PracSys worklist — {today.strftime('%A')}, {today.day} {today.strftime('%b %Y')}*"
    blocks = [header]

    blocks.append(f"\n*In progress: spec, build, review, QA ({len(in_progress)})*")
    groups = _in_progress_lines(in_progress, today)
    for name, lines in groups:
        blocks.append("\n".join([f"• *{_esc(name)}*", *lines]))
    if not groups:
        blocks.append("  None")

    todo_total = f"{len(todo)}+" if todo_more else str(len(todo))
    todo_lines = _todo_lines(todo)
    blocks.append(
        "\n".join(
            [f"\n*Next up (top {min(len(todo), _TODO_SHOWN)} of {todo_total})*", *(todo_lines or ["  None"])]
        )
    )
    blocked_lines = _blocked_lines(blocked, today)
    blocks.append("\n".join([f"\n*Blocked and overdue ({len(blocked)})*", *(blocked_lines or ["  None"])]))
    return blocks


def _pack(blocks: List[str]) -> List[str]:
    """Pack blocks into as few messages as fit, each at most _MAX_TEXT long."""
    messages: List[str] = []
    current = ""
    for block in blocks:
        pieces = [block]
        if len(block) > _MAX_TEXT:  # one person with a very long list: split by line
            pieces, chunk = [], ""
            for line in block.split("\n"):
                if chunk and len(chunk) + 1 + len(line) > _MAX_TEXT:
                    pieces.append(chunk)
                    chunk = line if line.startswith(("*", "• *")) else f"• *(continued)*\n{line}"
                else:
                    chunk = f"{chunk}\n{line}" if chunk else line
            pieces.append(chunk)
        for piece in pieces:
            if current and len(current) + 1 + len(piece) > _MAX_TEXT:
                messages.append(current)
                current = piece.lstrip("\n")
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages


def build_messages(
    *,
    in_progress: List[Dict[str, Any]],
    todo: List[Dict[str, Any]],
    todo_more: bool,
    blocked: List[Dict[str, Any]],
    today: date,
) -> List[str]:
    """The whole worklist, split into Slack-sized messages; nothing is dropped."""
    return _pack(
        _blocks(in_progress=in_progress, todo=todo, todo_more=todo_more, blocked=blocked, today=today)
    )


def build_message(**kwargs: Any) -> str:
    """The whole worklist as one text (used for --dry-run and tests)."""
    return "\n".join(_blocks(**kwargs))


# -------------------------------------------------------------------- CLI


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    post_p = sub.add_parser("post", help="build the worklist and post it")
    post_p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = parser.parse_args(argv)

    channel = os.environ.get("DAILY_CHANNEL_ID", "").strip()
    if not channel and not args.dry_run:
        print("refused: DAILY_CHANNEL_ID must be set", file=sys.stderr)
        return 2

    in_progress, _ = _jira_search(JQL_IN_PROGRESS, 100)
    todo, todo_more = _jira_search(JQL_TODO, 100)
    blocked, _ = _jira_search(JQL_BLOCKED, 50)
    messages = build_messages(
        in_progress=in_progress,
        todo=todo,
        todo_more=todo_more,
        blocked=blocked,
        today=datetime.now(_IST).date(),
    )
    if args.dry_run:
        print("\n\n---\n\n".join(messages))
        return 0
    for text in messages:
        _slack_post(channel, text)
    print(
        f"posted worklist: messages={len(messages)} in_progress={len(in_progress)} "
        f"todo={len(todo)} blocked={len(blocked)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
