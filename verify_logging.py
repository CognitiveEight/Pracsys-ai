#!/usr/bin/env python3
"""
verify_logging.py — guards the logging rules in CLAUDE.md ("Logging").

Static checks over every log call in app/ (AST, not grep):

  * no f-string messages (Loguru placeholders keep the text greppable and
    the arguments structured);
  * no payload / model-output dumps: json.dumps(...) of a payload, repr(data),
    "RAW" dumps;
  * no URLs unless wrapped in safe_url(): signed URLs carry credentials;
  * no slices of content variables (raw[:200], prompt[:500]...) — log the
    size with chars() instead;
  * no emoji in messages.

Functional checks (in a temporary directory, with dummy settings):

  * setup_logging() is idempotent and keeps the audit sink (audit.log had
    been dead since 2026-04-02 because a second setup removed it);
  * every line inside an event carries request_id / event / workspace_id;
  * audit and trace records stay out of app.log.

USAGE
    python verify_logging.py
"""

import ast
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
LEVELS = {"debug", "info", "success", "warning", "error", "exception", "critical"}
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⬆⬇✅❌]")
CONTENT = re.compile(
    r"^(raw|raw_text|raw_response|response_text|user_feedback|instruction|summary|"
    r"combined_text|json_str|ai_response|prompt|answer|user_message|text|content|"
    r"output|reply|message|query|document_text|combined_document_content)$"
)
# Identifiers that hold personal data (party/participant names, contact details,
# the case title, which names the parties). Error, id and count variables are fine.
PERSONAL = re.compile(
    r"(^|_)(part(y|ies)|party_names|case_?title|casetitle|phone|mobile|email|client_name|"
    r"full_name|person_name|participants?)(_|$)", re.I)
PERSONAL_OK = re.compile(r"(_err|_error|_id|_ids|_count)$", re.I)
SAFE_WRAPPERS = ("chars", "safe_url", "len", "bool")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"\n        {detail}" if detail and not ok else ""))


def _is_logger_call(node):
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr not in LEVELS:
        return False
    v = f.value
    while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in ("opt", "bind"):
        v = v.func.value
    return isinstance(v, ast.Name) and v.id == "logger"


def _violations(node):
    out = []
    first = node.args[0] if node.args else None
    if isinstance(first, ast.JoinedStr):
        out.append("f-string message")
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        if EMOJI.search(first.value):
            out.append("emoji in message")
        if re.search(r"\bRAW\b", first.value):
            out.append("RAW dump")
    for arg in node.args[1:]:
        # Anything wrapped in chars() / safe_url() is already redacted.
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id in ("chars", "safe_url"):
            continue
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "dumps":
                out.append("json.dumps argument")
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "repr":
                if any(isinstance(a, ast.Name) and a.id in ("data", "payload") for a in sub.args):
                    out.append("repr(payload)")
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
                base = sub.value
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if CONTENT.match(name or ""):
                    out.append(f"content slice {name}[...]")
        # Values only counted (len), redacted (chars/safe_url) or tested
        # (the condition of an `x if y else z`) never reach the log.
        skip = set()
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in SAFE_WRAPPERS:
                skip.update(id(n) for n in ast.walk(sub))
            if isinstance(sub, ast.IfExp):
                skip.update(id(n) for n in ast.walk(sub.test))
        for sub in ast.walk(arg):
            if id(sub) in skip:
                continue
            ident = sub.id if isinstance(sub, ast.Name) else (
                sub.value if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and re.fullmatch(r"[A-Za-z_]+", sub.value) else None)
            if ident and PERSONAL.search(ident) and not PERSONAL_OK.search(ident):
                out.append(f"personal data {ident}")
                break
        if isinstance(arg, ast.Name) and re.search(r"url$", arg.id, re.I):
            out.append(f"bare URL argument {arg.id}")
        if isinstance(arg, ast.Name) and arg.id in ("data", "payload", "envelope", "response_body"):
            out.append(f"whole payload argument {arg.id}")
    return out


def check_static():
    print("\n[static rules]")
    found = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            found.append(f"{path}: does not parse ({e})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_logger_call(node):
                for v in _violations(node):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {v}")
    kinds = {}
    for f in found:
        kinds.setdefault(f.rsplit(": ", 1)[-1].split(" ")[0], []).append(f)
    for rule in ("f-string", "emoji", "RAW", "json.dumps", "repr(payload)", "content", "bare", "whole", "personal"):
        hits = [f for k, v in kinds.items() if k.startswith(rule.split()[0]) for f in v]
        check(f"no log call with {rule}", not hits, "\n        ".join(hits[:8]))


SCRIPT = r'''
import asyncio
from loguru import logger
from app.observability import audit_logger, tracer, setup_logging
setup_logging()   # second call must be a no-op
@tracer.trace("verify_op")
async def op(): return 1
async def main():
    with logger.contextualize(request_id="rq-verify", event="workspace_sync", workspace_id="ws-verify"):
        logger.info("inside event")
        audit_logger.log_event("verify_event", {"k": 1}, workspace_id="ws-verify")
        await op()
asyncio.run(main())
logger.complete()
'''


def check_runtime():
    print("\n[runtime setup]")
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, SOCKET_URL="http://dummy", SECRET_KEY="dummy",
                   PYTHONPATH=str(ROOT), ENABLE_AUDIT_LOG="true", ENABLE_TRACE="true",
                   LOG_LEVEL="INFO")
        proc = subprocess.run([sys.executable, "-c", SCRIPT], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            check("logging setup runs", False, proc.stderr[-800:])
            return
        logs = pathlib.Path(tmp, "logs")
        app_log = (logs / "app.log").read_text() if (logs / "app.log").exists() else ""
        audit = (logs / "audit" / "audit.log").read_text() if (logs / "audit" / "audit.log").exists() else ""
        structured = (logs / "structured.log").read_text() if (logs / "structured.log").exists() else ""
        check("audit.log receives audit events after setup ran twice", "verify_event" in audit)
        check("lines inside an event carry request_id, event and workspace_id",
              "rq-verify workspace_sync ws=ws-verify" in app_log, app_log[-300:])
        check("audit records stay out of app.log", "verify_event" not in app_log)
        check("trace records go to structured.log, not app.log",
              '"operation": "verify_op"' in structured and "trace verify_op" not in app_log)


def main():
    check_static()
    check_runtime()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
