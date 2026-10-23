"""PracSys Engineering access for the triage agent (SIM-133). Read-only.

    python scripts/delivery/confluence_docs.py search --query '<words>'
    python scripts/delivery/confluence_docs.py read --page-id <id>

Only the PracSys Engineering space (``PRACENG``) can be searched or read: it
holds the features, flows, contracts, runbooks, policies and decisions of all
four PracSys apps, so the one requests channel can answer questions about any
of them. Uses the Atlassian service account (``JIRA_EMAIL`` /
``JIRA_API_TOKEN``), never shown to the model. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List

_SPACE = "PRACENG"
_PAGE_ID = re.compile(r"^\d{1,15}$")
_MAX_TEXT = 12000


class TriageRefused(Exception):
    """A request the triage helpers will not carry out."""


def _site() -> str:
    return os.environ.get("JIRA_SITE", "simplertoday.atlassian.net").strip()


def _request(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        raise TriageRefused("JIRA_EMAIL and JIRA_API_TOKEN must be set")
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    url = f"https://{_site()}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Atlassian host
        return json.loads(resp.read().decode())


def _page_url(page_id: str) -> str:
    return f"https://{_site()}/wiki/spaces/{_SPACE}/pages/{page_id}"


def _to_text(storage: str) -> str:
    """Confluence storage XHTML → plain text, one block per line."""
    text = re.sub(r"(?i)<br\s*/?>|</(p|h[1-6]|li|tr|div|blockquote|pre)>", "\n", storage)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def search(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Pages in PRACENG matching ``query`` (free text, never raw CQL)."""
    words = (query or "").strip()
    if not words:
        raise TriageRefused("empty search")
    escaped = words.replace("\\", " ").replace('"', '\\"')
    cql = f'space = "{_SPACE}" AND type = page AND text ~ "{escaped}"'
    body = _request("/wiki/rest/api/search", {"cql": cql, "limit": max(1, min(limit, 15))})
    hits = []
    for result in body.get("results", []):
        content = result.get("content") or {}
        page_id = str(content.get("id", ""))
        if not page_id:
            continue
        hits.append(
            {
                "id": page_id,
                "title": content.get("title", ""),
                "excerpt": _to_text(result.get("excerpt", ""))[:300],
                "url": _page_url(page_id),
            }
        )
    return hits


def read(page_id: str) -> Dict[str, Any]:
    """One PRACENG page as plain text; pages in any other space are refused."""
    if not _PAGE_ID.match(page_id or ""):
        raise TriageRefused("page id must be a number")
    page = _request(f"/wiki/rest/api/content/{page_id}", {"expand": "body.storage,space"})
    if ((page.get("space") or {}).get("key")) != _SPACE:
        raise TriageRefused("only PracSys Engineering pages can be read")
    storage = ((page.get("body") or {}).get("storage") or {}).get("value", "")
    return {
        "id": str(page.get("id", page_id)),
        "title": page.get("title", ""),
        "url": _page_url(page_id),
        "text": _to_text(storage)[:_MAX_TEXT],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    search_p = sub.add_parser("search", help="search PracSys Engineering pages")
    search_p.add_argument("--query", required=True)
    read_p = sub.add_parser("read", help="read one PracSys Engineering page")
    read_p.add_argument("--page-id", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: Any = search(args.query) if args.command == "search" else read(args.page_id)
    except TriageRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
