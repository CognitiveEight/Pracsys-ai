"""Slack events → GitHub workflow relay (SIM-152). Deployed as a GCP Cloud Function.

Slack sends every message and reaction in the triage channel here. The relay
checks Slack's signature, keeps only a person's message or a ✅/❌ reaction in
that channel, and starts the "Slack triage" workflow in GitHub, which does the
actual work. It never reads, logs or forwards message text.

Environment (secrets from Secret Manager):
    SLACK_SIGNING_SECRET   Slack app → Basic Information → Signing Secret
    GITHUB_DISPATCH_TOKEN  fine-grained token, this repo only, Actions: read and write
    GITHUB_REPO            owner/name, e.g. CognitiveEight/Pracsys-ai
    TRIAGE_CHANNEL_ID      the requests channel id
    GITHUB_WORKFLOW        optional, default slack-triage.yml
    GITHUB_REF             optional, default main

Entry point: ``handle``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

log = logging.getLogger("slack_triage_relay")
logging.basicConfig(level=logging.INFO)

_MAX_AGE_SECONDS = 300
_DECISION_REACTIONS = {"white_check_mark", "heavy_check_mark", "x", "negative_squared_cross_mark"}
_ALLOWED_SUBTYPES = {None, "thread_broadcast"}


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _signature_ok(headers: Mapping[str, str], body: bytes, now: float) -> bool:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    stamp = _header(headers, "X-Slack-Request-Timestamp")
    given = _header(headers, "X-Slack-Signature")
    if not secret or not stamp or not given:
        return False
    try:
        if abs(now - int(stamp)) > _MAX_AGE_SECONDS:
            return False
    except ValueError:
        return False
    base = b"v0:" + stamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _matches(event: Dict[str, Any]) -> bool:
    """A person's message, or an approval reaction, in the triage channel."""
    channel = os.environ.get("TRIAGE_CHANNEL_ID", "")
    kind = event.get("type")
    if kind == "message":
        return (
            event.get("channel") == channel
            and not event.get("bot_id")
            and event.get("subtype") in _ALLOWED_SUBTYPES
        )
    if kind == "reaction_added":
        item = event.get("item") or {}
        return item.get("channel") == channel and event.get("reaction") in _DECISION_REACTIONS
    return False


def _dispatch() -> int:
    """Start the triage workflow; returns GitHub's HTTP status (204 on success)."""
    repo = os.environ["GITHUB_REPO"]
    workflow = os.environ.get("GITHUB_WORKFLOW", "slack-triage.yml")
    ref = os.environ.get("GITHUB_REF", "main")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": ref}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_DISPATCH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - fixed GitHub host
        return resp.status


def process(
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: Optional[float] = None,
    dispatch: Optional[Callable[[], int]] = None,
) -> Tuple[int, str]:
    """Return (HTTP status, response body) for one Slack request."""
    if not _signature_ok(headers, body, time.time() if now is None else now):
        log.warning("rejected: bad or missing Slack signature")
        return 401, "invalid signature"
    try:
        payload = json.loads(body.decode() or "{}")
    except ValueError:
        log.warning("rejected: body is not JSON")
        return 400, "bad request"

    if payload.get("type") == "url_verification":
        log.info("url_verification answered")
        return 200, str(payload.get("challenge", ""))

    event = payload.get("event") or {}
    kind = event.get("type", "unknown")
    if _header(headers, "X-Slack-Retry-Num"):
        log.info("event=%s ignored: Slack retry", kind)
        return 200, "ok"
    if payload.get("type") != "event_callback" or not _matches(event):
        log.info("event=%s ignored", kind)
        return 200, "ok"

    try:
        status = (dispatch or _dispatch)()
        log.info("event=%s matched: workflow dispatch status=%s", kind, status)
    except Exception as err:  # noqa: BLE001 - Slack must always get its 200
        code = getattr(err, "code", None)  # HTTPError carries GitHub's status code
        detail = f"HTTP {code}" if isinstance(code, int) else type(err).__name__
        log.error("event=%s matched: workflow dispatch failed (%s)", kind, detail)
    return 200, "ok"


def handle(request: Any) -> Tuple[str, int, Dict[str, str]]:
    """Cloud Functions (functions-framework) entry point."""
    status, text = process(request.headers, request.get_data())
    return text, status, {"Content-Type": "text/plain"}
