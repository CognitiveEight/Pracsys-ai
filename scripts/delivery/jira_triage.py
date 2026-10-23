"""Jira access for the triage agent (SIM-133). The only way it reads or writes SIM.

    python scripts/delivery/jira_triage.py search --jql '<jql>'
    python scripts/delivery/jira_triage.py create --component AI|BE|FE|Admin|DevOps|QA \\
        --type Bug|Task --summary '<one line>' --priority P1|P2|P3|P4 \\
        --source-link '<Slack message link>'          # description on stdin

``search`` is always scoped to project SIM. ``create`` makes one Bug or Task
in SIM for one PracSys component, with labels forced to ``from-slack`` and
the component's label,
and refuses if an issue already exists for the same Slack message. There is no
edit, comment, transition or delete command. Credentials come from
``JIRA_EMAIL`` / ``JIRA_API_TOKEN`` and are never shown to the model.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from typing import Any, Dict, List, Optional

_PROJECT = "SIM"
_TYPES = {"Bug", "Task"}
_PRIORITIES = {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"}
#: SIM "Components" values and the label each one forces (one requests
#: channel serves every PracSys app).
_COMPONENT_LABELS = {
    "AI": "ai-repo",
    "BE": "be-repo",
    "FE": "fe-repo",
    "Admin": "admin-repo",
    "DevOps": "devops",
    "QA": "qa",
}
_SLACK_LINK = re.compile(r"^https://[a-z0-9-]+\.slack\.com/archives/C[A-Z0-9]+/p(\d{16})$")


class TriageRefused(Exception):
    """A request the triage helpers will not carry out."""


def _site() -> str:
    return os.environ.get("JIRA_SITE", "simplertoday.atlassian.net").strip()


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        raise TriageRefused("JIRA_EMAIL and JIRA_API_TOKEN must be set")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(
        f"https://{_site()}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Jira host
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def search(jql: str, max_results: int = 20) -> List[Dict[str, Any]]:
    """Issues in SIM matching ``jql``; the project scope cannot be widened."""
    body = {
        "jql": f"project = {_PROJECT} AND ({jql})",
        "fields": ["summary", "status", "labels"],
        "maxResults": max(1, min(max_results, 50)),
    }
    issues = _request("POST", "/rest/api/3/search/jql", body).get("issues", [])
    return [
        {
            "key": i.get("key", ""),
            "summary": (i.get("fields") or {}).get("summary", ""),
            "status": ((i.get("fields") or {}).get("status") or {}).get("name", ""),
        }
        for i in issues
    ]


def create(
    *,
    component: str,
    issue_type: str,
    summary: str,
    priority: str,
    source_link: str,
    description: str,
) -> Dict[str, Any]:
    """Create one SIM issue for a Slack message, or return the one that exists."""
    if component not in _COMPONENT_LABELS:
        raise TriageRefused(f"component must be one of {sorted(_COMPONENT_LABELS)}")
    if issue_type not in _TYPES:
        raise TriageRefused(f"issue type must be one of {sorted(_TYPES)}")
    if priority not in _PRIORITIES:
        raise TriageRefused(f"priority must be one of {sorted(_PRIORITIES)}")
    match = _SLACK_LINK.match(source_link or "")
    if not match:
        raise TriageRefused("source link must be a Slack message link")
    if not (summary or "").strip():
        raise TriageRefused("empty summary")

    existing = search(f'labels = "from-slack" AND text ~ "p{match.group(1)}"', max_results=1)
    if existing:
        return {"key": existing[0]["key"], "created": False}

    fields: Dict[str, Any] = {
        "project": {"key": _PROJECT},
        "issuetype": {"name": issue_type},
        "summary": summary.strip()[:250],
        "priority": {"name": _PRIORITIES[priority]},
        "labels": ["from-slack", _COMPONENT_LABELS[component]],
        "description": f"{(description or '').strip()}\n\nSource: {source_link}",
    }
    component_field = os.environ.get("JIRA_COMPONENT_FIELD", "").strip()
    if component_field:
        fields[component_field] = {"value": component}
    created = _request("POST", "/rest/api/2/issue", {"fields": fields})
    return {"key": created.get("key", ""), "created": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    search_p = sub.add_parser("search", help="read-only JQL search inside SIM")
    search_p.add_argument("--jql", required=True)
    create_p = sub.add_parser("create", help="create one Bug or Task; description on stdin")
    create_p.add_argument("--component", required=True)
    create_p.add_argument("--type", dest="issue_type", required=True)
    create_p.add_argument("--summary", required=True)
    create_p.add_argument("--priority", required=True)
    create_p.add_argument("--source-link", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "search":
            result: Any = search(args.jql)
        else:
            result = create(
                component=args.component,
                issue_type=args.issue_type,
                summary=args.summary,
                priority=args.priority,
                source_link=args.source_link,
                description=sys.stdin.read(),
            )
    except TriageRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
