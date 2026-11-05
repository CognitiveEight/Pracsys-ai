"""Offline tests for the Slack-events relay (SIM-152). No network: the GitHub
dispatch is replaced. Fictional ids and text only."""

import hashlib
import hmac
import importlib.util
import json
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-signing-secret"
CHANNEL = "C0TRIAGE001"
NOW = 1_792_700_000


def _load():
    path = ROOT / "relay" / "slack_events" / "main.py"
    spec = importlib.util.spec_from_file_location("slack_events_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signed(payload, ts=NOW, secret=SECRET, extra=None):
    body = json.dumps(payload).encode()
    base = f"v0:{ts}:{body.decode()}".encode()
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    headers = {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}
    headers.update(extra or {})
    return headers, body


def _message(**over):
    event = {"type": "message", "channel": CHANNEL, "user": "U0PERSON01",
             "text": "Question: confidential matter of Ramesh Testwala", "ts": "1792700000.000100"}
    event.update(over)
    return {"type": "event_callback", "event": event}


@pytest.fixture
def relay(monkeypatch):
    mod = _load()
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "gh-test-token")
    monkeypatch.setenv("GITHUB_REPO", "ExampleOrg/example-repo")
    monkeypatch.setenv("TRIAGE_CHANNEL_ID", CHANNEL)
    calls = []
    monkeypatch.setattr(mod, "_dispatch", lambda: calls.append("dispatch") or 204)
    return mod, calls


def _run(mod, payload, **kw):
    headers, body = _signed(payload, **kw)
    return mod.process(headers, body, now=NOW)


def test_valid_signature_accepted(relay):
    mod, calls = relay
    status, _ = _run(mod, _message())
    assert status == 200 and calls == ["dispatch"]


def test_bad_or_stale_signature_rejected(relay):
    mod, calls = relay
    assert _run(mod, _message(), secret="wrong")[0] == 401
    assert _run(mod, _message(), ts=NOW - 301)[0] == 401
    headers, body = _signed(_message())
    assert mod.process({}, body, now=NOW)[0] == 401
    headers["X-Slack-Request-Timestamp"] = "not-a-number"
    assert mod.process(headers, body, now=NOW)[0] == 401
    assert calls == []


def test_url_verification(relay):
    mod, calls = relay
    status, body = _run(mod, {"type": "url_verification", "challenge": "abc123"})
    assert status == 200 and body == "abc123" and calls == []


def test_person_message_dispatches(relay):
    mod, calls = relay
    _run(mod, _message())
    _run(mod, _message(thread_ts="1792699000.000100"))  # a reply such as "approve"
    _run(mod, _message(subtype="thread_broadcast"))
    assert calls == ["dispatch"] * 3


def test_ignored_events(relay):
    mod, calls = relay
    _run(mod, _message(bot_id="B0BOT00001"))
    _run(mod, _message(channel="C0OTHER0001"))
    for subtype in ("channel_join", "message_changed", "message_deleted", "bot_message"):
        _run(mod, _message(subtype=subtype))
    _run(mod, {"type": "event_callback", "event": {"type": "app_mention", "channel": CHANNEL}})
    assert calls == []


def test_reactions(relay):
    mod, calls = relay

    def reaction(name, channel=CHANNEL):
        return {"type": "event_callback", "event": {
            "type": "reaction_added", "user": "U0PERSON01", "reaction": name,
            "item": {"type": "message", "channel": channel, "ts": "1792700000.000200"}}}

    for name in ("white_check_mark", "heavy_check_mark", "x", "negative_squared_cross_mark"):
        _run(mod, reaction(name))
    _run(mod, reaction("thumbsup"))
    _run(mod, reaction("white_check_mark", channel="C0OTHER0001"))
    assert calls == ["dispatch"] * 4


def test_retry_ignored(relay):
    mod, calls = relay
    status, _ = _run(mod, _message(), extra={"X-Slack-Retry-Num": "1"})
    assert status == 200 and calls == []


def test_dispatch_failure_still_200(relay, monkeypatch):
    mod, _ = relay

    def boom():
        raise TimeoutError("github slow")

    monkeypatch.setattr(mod, "_dispatch", boom)
    assert _run(mod, _message())[0] == 200


def test_logs_carry_no_message_content(relay, caplog):
    mod, _ = relay
    with caplog.at_level(logging.INFO):
        _run(mod, _message())
        _run(mod, _message(channel="C0OTHER0001"))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged  # one line per request
    for secret_bit in ("Ramesh", "confidential", "U0PERSON01", "1792700000.000100", "gh-test-token"):
        assert secret_bit not in logged


def test_missing_configuration_fails_closed(monkeypatch):
    mod = _load()
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    headers, body = _signed(_message())
    assert mod.process(headers, body, now=NOW)[0] == 401


def test_dispatch_failure_logs_github_status(relay, monkeypatch, caplog):
    import urllib.error

    mod, _ = relay

    def forbidden():
        raise urllib.error.HTTPError("https://api.github.test", 403, "Forbidden", {}, None)

    monkeypatch.setattr(mod, "_dispatch", forbidden)
    with caplog.at_level(logging.INFO):
        assert _run(mod, _message())[0] == 200
    assert "workflow dispatch failed (HTTP 403)" in caplog.text
