"""Slack access for the triage agent (SIM-133). The only way it reads or posts.

Two commands, both on the one channel in ``TRIAGE_CHANNEL_ID``:

    python scripts/delivery/slack_io.py read [--days 7]
    python scripts/delivery/slack_io.py reply --thread-ts <ts>   # text on stdin

``reply`` has no channel argument and refuses anything but a thread reply, so
whatever the model writes, it can only answer inside a thread of the triage
channel. The bot token is read from ``SLACK_BOT_TOKEN`` here and never shown
to the model. Standard library only, so the workflow needs no extra install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List

_API = "https://slack.com/api/"
_TS = re.compile(r"^\d{9,11}\.\d{6}$")
_MAX_TEXT = 3900  # Slack's limit is 4000 characters per message text


class TriageRefused(Exception):
    """A request the triage helpers will not carry out."""


def _config() -> Dict[str, str]:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("TRIAGE_CHANNEL_ID", "").strip()
    if not token or not channel:
        raise TriageRefused("SLACK_BOT_TOKEN and TRIAGE_CHANNEL_ID must be set")
    return {"token": token, "channel": channel}


def _request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    token = _config()["token"]
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(
        _API + method,
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Slack host
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {body.get('error', 'unknown_error')}")
    return body


def _paged(method: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor = None
    while True:
        body = _request(method, {**params, "cursor": cursor, "limit": 200})
        out.extend(body.get("messages", []))
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not body.get("has_more") or not cursor:
            return out


def _slim(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts": message.get("ts", ""),
        "user": message.get("user", ""),
        "is_bot": bool(message.get("bot_id")) or message.get("subtype") == "bot_message",
        "subtype": message.get("subtype", ""),
        "text": message.get("text", ""),
        "reactions": [
            {"name": r.get("name", ""), "users": list(r.get("users", []))}
            for r in message.get("reactions", [])
        ],
    }


def read_channel(days: int = 7) -> Dict[str, Any]:
    """Top-level messages of the last ``days`` days, oldest first, with threads."""
    channel = _config()["channel"]
    team_url = _request("auth.test", {}).get("url", "").rstrip("/")
    oldest = f"{time.time() - days * 86400:.6f}"
    top = _paged("conversations.history", {"channel": channel, "oldest": oldest})
    messages = []
    for message in sorted(top, key=lambda m: float(m.get("ts", 0))):
        item = _slim(message)
        item["link"] = f"{team_url}/archives/{channel}/p{item['ts'].replace('.', '')}"
        item["replies"] = []
        if message.get("reply_count"):
            thread = _paged("conversations.replies", {"channel": channel, "ts": item["ts"]})
            item["replies"] = [_slim(r) for r in thread if r.get("ts") != item["ts"]]
        messages.append(item)
    return {"channel": channel, "messages": messages}


def reply(thread_ts: str, text: str) -> Dict[str, Any]:
    """Post ``text`` as a reply in the thread ``thread_ts`` of the triage channel."""
    channel = _config()["channel"]
    if not _TS.match(thread_ts or ""):
        raise TriageRefused("a reply needs the thread timestamp of a message in the triage channel")
    if not (text or "").strip():
        raise TriageRefused("empty reply")
    body = _request(
        "chat.postMessage",
        {"channel": channel, "thread_ts": thread_ts, "text": text.strip()[:_MAX_TEXT]},
    )
    return {"ts": body.get("ts", "")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    read_p = sub.add_parser("read", help="messages of the triage channel, with threads")
    read_p.add_argument("--days", type=int, default=7)
    reply_p = sub.add_parser("reply", help="reply in a thread; text on stdin")
    reply_p.add_argument("--thread-ts", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "read":
            result = read_channel(days=max(1, min(args.days, 14)))
        else:
            result = reply(thread_ts=args.thread_ts, text=sys.stdin.read())
    except TriageRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
