#!/usr/bin/env python3
"""
verify_docx_build.py — Offline checks for `draft_file_build_request` (SIM-216).

The backend asks for a Word file built from a stored draft's text ("Rebuild
Word file", the first Word file of an older draft). The AI answers with
`draft_file_build_response`, always, success or failure.

Exercises the real handler through `event_handler.handle(...)` and the real
renderer; only the socket and the draft-file upload are stubbed. No API key,
no backend, no bucket, no model call. Fictional text only.

Covers:
  1. ready_markdown — Markdown draft → ok, link, echo, placeholders, no metadata
  2. ready_html     — editor HTML → same table and placeholders as Markdown
  3. in_flight      — a re-send while the build runs is swallowed (one build)
  4. cached         — a re-send after completion gets the same answer, no rebuild
  5. invalid        — bad payloads answered `invalid_request`; no requestId → silent
  6. empty          — empty draft text → `empty_content`
  7. failures       — upload error → `upload_failed`; render error → `render_failed`
  8. timeout        — a build past DRAFT_DOCX_BUILD_TIMEOUT_SECONDS → `timeout`
  9. template       — templateId not held → standard layout, still ok
 10. metadata       — the cost block appears only when a model was called
 11. purge          — pending purge refused (not revoked); purge mid-build
                      deletes the file; the ledger is swept by the purge
 12. stop           — the build is not part of a run a stop can cancel
 13. logs           — no title, draft text or link in any log line
 14. layout         — formatLayout (SIM-217) applied; a missing image or a null
                      layout still builds
 15. title          — no title in the contract: inferred from the draft

USAGE
    python tests/offline/verify_docx_build.py            # run everything
    python tests/offline/verify_docx_build.py --list     # show the check names
"""

import argparse
import asyncio
import io
import os
import sys
import threading
import time
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

from loguru import logger  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.processing import docx_generator  # noqa: E402
from app.socket.events import event_handler as H  # noqa: E402
from app.socket.handlers import run_control  # noqa: E402
from app.storage import draft_files  # noqa: E402

EVENT = "draft_file_build_request"
RESPONSE = "draft_file_build_response"

RESULTS = []
EMITS = []
UPLOADS = []
DELETES = []
LOG_LINES = []

RUN_ID = uuid.uuid4().hex[:10]
OWNER = "68b0e1f8d3a9c4b2e1f0aaaa"
TITLE = "Legal Notice for Recovery of Dues Zephyrine"
SECRET_WORD = "Quillfeather"  # appears only in the draft text
FAKE_LINK = "https://storage.example.invalid/drafts/x.docx?X-Goog-Signature=SIGNATUREVALUE"

MARKDOWN = f"""# {TITLE}

To,
<Bank Name>

My client {SECRET_WORD} states:

1. The account was opened in 2019.
   - Statement A
2. The dues remain unpaid.

| Item | Amount |
| --- | --- |
| Principal | 1,00,000 |
| Total | 1,00,000 |

Pay to <Account Number>.
"""

HTML = f"""<h1>{TITLE}</h1><p>To,<br>&lt;Bank Name&gt;</p>
<p>My client {SECRET_WORD} states:</p>
<ol><li>The account was opened in 2019.<ul><li>Statement A</li></ul></li>
<li>The dues remain unpaid.</li></ol>
<table><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody><tr><td>Principal</td><td>1,00,000</td></tr><tr><td>Total</td><td>1,00,000</td></tr></tbody></table>
<script>alert(1)</script><p>Pay to &lt;Account Number&gt;.</p>"""


LETTERHEAD_TEXT = "Example Chambers Zephyrine"
LETTERHEAD = {
    "id": "fmt_lh",
    "name": "Example letterhead",
    "type": "letterhead",
    "version": 4,
    "formatting": {
        "page": {"font": "Calibri", "fontSize": "11pt", "watermark": "None"},
        "letterhead": {
            "headerType": "text", "headerTextLeft": LETTERHEAD_TEXT,
            "footerType": "text", "footerTextCenter": "1 Sample Road, Example City",
            "pageApplication": "all",
        },
    },
}
LETTERHEAD_WITH_IMAGE = {
    **LETTERHEAD,
    "formatting": {
        **LETTERHEAD["formatting"],
        "letterhead": {
            **LETTERHEAD["formatting"]["letterhead"],
            "headerType": "image",
            "headerImageUrl": FAKE_LINK,
        },
    },
}


class _FakeSocket:
    # Must return True: SocketClient.emit reports DELIVERY.
    async def emit(self, event, payload):
        EMITS.append((event, payload))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", payload))
        return True


H.set_socket_client(_FakeSocket())

# Upload stub: records the call; can be gated (in-flight) or made slow/failing.
UPLOAD_GATE = threading.Event()
UPLOAD_GATE.set()
UPLOAD_MODE = {"mode": "ok", "sleep": 0.0}


def _fake_upload(workspace_id, data):
    UPLOAD_GATE.wait(10)
    if UPLOAD_MODE["sleep"]:
        time.sleep(UPLOAD_MODE["sleep"])
    if UPLOAD_MODE["mode"] == "fail":
        raise RuntimeError("storage unavailable")
    UPLOADS.append((workspace_id, data))
    return FAKE_LINK


def _fake_delete(workspace_id):
    DELETES.append(workspace_id)
    return 1


draft_files.upload_draft_docx = _fake_upload
draft_files.delete_workspace_draft_files = _fake_delete
draft_files.draft_bucket = lambda: "bucket.example.invalid"


async def _fixed_role(*_a, **_k):
    return "PETITIONER"


H._resolve_workspace_role = _fixed_role


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _ws(name):
    return f"{name}{RUN_ID}"


def _req(ws, rid=None, **over):
    body = {
        "requestId": rid or f"ddb_{uuid.uuid4().hex[:8]}",
        "workspaceId": ws,
        "userId": OWNER,
        "purpose": "rebuild",
        "draftId": "user_draft_0001",
        "content": MARKDOWN,
        "format": "markdown",
        "formatLayout": None,
        "timestamp": "2026-11-19T10:00:00.000Z",
    }
    body.update(over)
    return body


def _responses(rid=None):
    return [p for e, p in EMITS if e == RESPONSE and (rid is None or p.get("requestId") == rid)]


async def _settle(timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pending = [t for t in list(H._background_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=0.2)


def _tables_in(data):
    return docx_generator.docx_structure(data)["tables"]


def _reset():
    EMITS.clear()
    UPLOADS.clear()
    DELETES.clear()
    UPLOAD_MODE.update(mode="ok", sleep=0.0)
    UPLOAD_GATE.set()


# ---------------------------------------------------------------------------


async def check_ready_markdown():
    print("\n[1] Ready (Markdown)")
    _reset()
    ws = _ws("ddbmd")
    body = _req(ws)
    logs_before = len(LOG_LINES)
    await H.handle(EVENT, body)
    await _settle()
    summary = [ln for ln in LOG_LINES[logs_before:] if "draft_file_build status=" in ln]
    check("exactly one summary log line for the build", len(summary) == 1, str(len(summary)))
    res = _responses(body["requestId"])
    check("exactly one response", len(res) == 1, str(res))
    r = res[0] if res else {}
    check("status ok, no old-contract fields",
          r.get("status") == "ok" and "docxStatus" not in r and "reason" not in r
          and "version" not in r and "userId" not in r, str(r))
    check("downloadUrl is the uploaded link", r.get("downloadUrl") == FAKE_LINK)
    check("no errorCode on success", "errorCode" not in r)
    check("ids echoed",
          all(r.get(k) == body[k] for k in ("requestId", "workspaceId", "draftId")))
    check("placeholderCount counts <Label> fill-ins", r.get("placeholderCount") == 2, str(r.get("placeholderCount")))
    check("no metadata when no model was called", "metadata" not in r)
    check("uploaded once, under the workspace", len(UPLOADS) == 1 and UPLOADS[0][0] == ws)
    data = UPLOADS[0][1] if UPLOADS else b""
    check("upload is a .docx", zipfile.is_zipfile(io.BytesIO(data)))
    check("the table reached the Word file", data and _tables_in(data) == 1)


async def check_ready_html():
    print("\n[2] Ready (HTML)")
    _reset()
    ws = _ws("ddbhtml")
    body = _req(ws, content=HTML, format="html")
    await H.handle(EVENT, body)
    await _settle()
    r = (_responses(body["requestId"]) or [{}])[0]
    check("status ready", r.get("status") == "ok", str(r))
    check("placeholders survive the HTML", r.get("placeholderCount") == 2, str(r.get("placeholderCount")))
    data = UPLOADS[0][1] if UPLOADS else b""
    check("the table reached the Word file", data and _tables_in(data) == 1)
    text = docx_generator.docx_structure(data)["text"] if data else ""
    check("script content dropped", "alert" not in text)
    # Same Word text as the Markdown build.
    _reset()
    body_md = _req(ws)
    await H.handle(EVENT, body_md)
    await _settle()
    md_text = docx_generator.docx_structure(UPLOADS[0][1])["text"] if UPLOADS else ""
    check("same Word text as the equivalent Markdown", text and text == md_text)


async def check_in_flight():
    print("\n[3] Re-send while in flight")
    _reset()
    ws = _ws("ddbinflight")
    body = _req(ws)
    UPLOAD_GATE.clear()
    await H.handle(EVENT, body)
    await asyncio.sleep(0.3)
    await H.handle(EVENT, dict(body))
    check("nothing answered yet", not _responses(body["requestId"]))
    UPLOAD_GATE.set()
    await _settle()
    check("one build, one answer",
          len(UPLOADS) == 1 and len(_responses(body["requestId"])) == 1,
          f"uploads={len(UPLOADS)} responses={len(_responses(body['requestId']))}")


async def check_cached():
    print("\n[4] Re-send after completion")
    _reset()
    ws = _ws("ddbcached")
    body = _req(ws)
    await H.handle(EVENT, body)
    await _settle()
    await H.handle(EVENT, dict(body))
    await _settle()
    res = _responses(body["requestId"])
    check("answered twice", len(res) == 2, str(len(res)))
    check("same answer both times", len(res) == 2 and res[0] == res[1])
    check("built once", len(UPLOADS) == 1)
    # A failed answer is replayed the same way (the contract caches every answer).
    _reset()
    UPLOAD_MODE["mode"] = "fail"
    body2 = _req(ws)
    await H.handle(EVENT, body2)
    await _settle()
    UPLOAD_MODE["mode"] = "ok"
    await H.handle(EVENT, dict(body2))
    await _settle()
    res2 = _responses(body2["requestId"])
    check("a failed answer is replayed, not rebuilt",
          len(res2) == 2 and all(r.get("errorCode") == "upload_failed" for r in res2) and not UPLOADS,
          str(res2))


async def check_invalid():
    print("\n[5] Invalid request")
    _reset()
    ws = _ws("ddbinvalid")
    cases = {
        "missing content": {k: v for k, v in _req(ws).items() if k != "content"},
        "unknown format": _req(ws, format="rtf"),
        "missing draftId": {k: v for k, v in _req(ws).items() if k != "draftId"},
        "missing workspaceId": {k: v for k, v in _req(ws).items() if k != "workspaceId"},
        "content too long": _req(ws, content="x" * 2_000_001),
    }
    for label, body in cases.items():
        EMITS.clear()
        await H.handle(EVENT, body)
        await _settle()
        r = (_responses(body["requestId"]) or [{}])[0]
        check(f"{label}: answered error/invalid_request",
              r.get("status") == "error" and r.get("errorCode") == "invalid_request", str(r))
    check("ids echoed when readable",
          (_responses() or [{}])[-1].get("workspaceId") == ws)
    EMITS.clear()
    body = cases["missing content"]
    await H.handle(EVENT, body)
    await _settle()
    r = (_responses(body["requestId"]) or [{}])[0]
    check("invalid_request echoes every readable id",
          all(r.get(k) == body[k] for k in ("requestId", "workspaceId", "draftId")),
          str(r))
    EMITS.clear()
    body = _req(ws)
    body.pop("requestId")
    body.pop("content")  # invalid AND unanswerable
    logs_before = len(LOG_LINES)
    await H.handle(EVENT, body)
    await _settle()
    check("no requestId: nothing to answer, nothing emitted", not _responses())
    check("no requestId: logged",
          any("has no requestId" in ln for ln in LOG_LINES[logs_before:]))
    check("nothing uploaded for invalid requests", not UPLOADS)


async def check_empty():
    print("\n[6] Empty content")
    _reset()
    ws = _ws("ddbempty")
    for fmt, content in (("markdown", "   \n "), ("html", "<p> </p><script>x</script>")):
        body = _req(ws, content=content, format=fmt)
        await H.handle(EVENT, body)
        await _settle()
        r = (_responses(body["requestId"]) or [{}])[0]
        check(f"{fmt}: error/empty_content", r.get("errorCode") == "empty_content", str(r))
    check("nothing uploaded", not UPLOADS)


async def check_failures():
    print("\n[7] Failures")
    _reset()
    ws = _ws("ddbfail")
    UPLOAD_MODE["mode"] = "fail"
    body = _req(ws)
    await H.handle(EVENT, body)
    await _settle()
    r = (_responses(body["requestId"]) or [{}])[0]
    check("upload error → upload_failed", r.get("errorCode") == "upload_failed", str(r))
    check("no link on failure", "downloadUrl" not in r and r.get("status") == "error")
    check("placeholderCount still reported", r.get("placeholderCount") == 2)
    UPLOAD_MODE["mode"] = "ok"

    original = docx_generator.render_markdown_to_docx

    def _boom(*_a, **_k):
        raise ValueError("render broke")

    docx_generator.render_markdown_to_docx = _boom
    try:
        body = _req(ws)
        await H.handle(EVENT, body)
        await _settle()
    finally:
        docx_generator.render_markdown_to_docx = original
    r = (_responses(body["requestId"]) or [{}])[0]
    check("render error → render_failed", r.get("errorCode") == "render_failed", str(r))
    check("render_failed: no link, placeholderCount still reported",
          "downloadUrl" not in r and r.get("status") == "error"
          and r.get("placeholderCount") == 2, str(r))

    old_bucket = draft_files.draft_bucket
    draft_files.draft_bucket = lambda: None
    try:
        body = _req(ws)
        await H.handle(EVENT, body)
        await _settle()
    finally:
        draft_files.draft_bucket = old_bucket
    r = (_responses(body["requestId"]) or [{}])[0]
    check("no bucket configured → upload_failed", r.get("errorCode") == "upload_failed", str(r))
    check("no bucket: no link, placeholderCount still reported",
          "downloadUrl" not in r and r.get("status") == "error"
          and r.get("placeholderCount") == 2, str(r))


async def check_timeout():
    print("\n[8] Timeout")
    _reset()
    ws = _ws("ddbtimeout")
    old = settings.draft_docx_build_timeout_seconds
    settings.draft_docx_build_timeout_seconds = 0.3
    UPLOAD_MODE["sleep"] = 1.0
    try:
        body = _req(ws)
        started = time.monotonic()
        await H.handle(EVENT, body)
        await _settle()
        elapsed = time.monotonic() - started
    finally:
        settings.draft_docx_build_timeout_seconds = old
    r = (_responses(body["requestId"]) or [{}])[0]
    check("answered error/timeout", r.get("errorCode") == "timeout", str(r))
    check("answered at the cap, not after the slow upload", elapsed < 0.95, f"{elapsed:.2f}s")
    await asyncio.sleep(0.9)  # let the abandoned upload thread finish


async def check_template_missing():
    print("\n[9] Template not held")
    _reset()
    ws = _ws("ddbtpl")
    from app.ai.gemini_client import gemini_client

    model_calls = []
    patched = {}
    for name in ("generate", "generate_with_images", "generate_longform",
                 "generate_grounded", "chat"):
        patched[name] = getattr(gemini_client, name)

        async def _counting(*_a, _n=name, **_k):
            model_calls.append(_n)
            return ""

        setattr(gemini_client, name, _counting)
    patched["generate_sync"] = gemini_client.generate_sync
    gemini_client.generate_sync = lambda *_a, **_k: model_calls.append("generate_sync") or ""
    try:
        body = _req(ws, templateId="tpl_not_held_0001", templateVersion=2, templateScope="user")
        await H.handle(EVENT, body)
        await _settle()
    finally:
        for name in patched:
            gemini_client.__dict__.pop(name, None)  # back to the class methods
    check("no model call for a template the AI does not hold", not model_calls, str(model_calls))
    check("upload carries the standard-layout file", len(UPLOADS) == 1)
    r = (_responses(body["requestId"]) or [{}])[0]
    check("built with the standard layout, ok", r.get("status") == "ok", str(r))
    check("no metadata (no model called)", "metadata" not in r)


async def check_metadata():
    print("\n[10] Metadata only when a model was called")
    _reset()
    ws = _ws("ddbmeta")
    from app.ai.usage import record_usage
    from app.langgraph import drafting_agent as da

    original = da.drafting_agent.build_docx_from_text

    async def _with_model(**kwargs):
        record_usage(1200, 300, model=settings.gemini_model)
        return da.DocxBuildResult(FAKE_LINK, "llm_diff", None, "found")

    da.drafting_agent.build_docx_from_text = _with_model
    try:
        body = _req(ws, templateId="tpl_held_0001")
        await H.handle(EVENT, body)
        await _settle()
    finally:
        da.drafting_agent.build_docx_from_text = original
    r = (_responses(body["requestId"]) or [{}])[0]
    meta = r.get("metadata") or {}
    check("metadata present", bool(meta), str(r))
    check("metadata carries the tokens and a price",
          meta.get("inputTokens") == 1200 and meta.get("outputTokens") == 300
          and "price" in meta and meta.get("messageId"), str(meta))


async def check_purge():
    print("\n[11] Purge")
    _reset()
    ws = _ws("ddbpurge")
    H._pending_purges[ws] = {"requestId": "wd_x", "purgeAfter": "2099-01-01T00:00:00Z"}
    try:
        body = _req(ws)
        await H.handle(EVENT, body)
        await _settle()
        r = (_responses(body["requestId"]) or [{}])[0]
        check("pending purge → invalid_request", r.get("errorCode") == "invalid_request", str(r))
        check("the pending purge is NOT revoked", ws in H._pending_purges)
        check("nothing built", not UPLOADS)
    finally:
        H._pending_purges.pop(ws, None)

    # Purge lands while the build runs: the uploaded file is deleted again.
    _reset()
    ws2 = _ws("ddbpurgemid")
    UPLOAD_GATE.clear()
    body = _req(ws2)
    await H.handle(EVENT, body)
    await asyncio.sleep(0.3)
    H._purged_workspaces[ws2] = {"purgedAt": "now"}
    UPLOAD_GATE.set()
    await _settle()
    try:
        r = (_responses(body["requestId"]) or [{}])[0]
        check("purge mid-build → invalid_request, no link",
              r.get("errorCode") == "invalid_request" and "downloadUrl" not in r, str(r))
        check("the workspace's draft files were deleted again", DELETES == [ws2], str(DELETES))
        check("no cached answer kept for a purged workspace",
              body["requestId"] not in (H._draft_docx_build_requests.get(ws2) or {}))
    finally:
        H._purged_workspaces.pop(ws2, None)

    _reset()
    ws3 = _ws("ddbsweep")
    body = _req(ws3)
    await H.handle(EVENT, body)
    await _settle()
    check("ledger holds the answer", body["requestId"] in (H._draft_docx_build_requests.get(ws3) or {}))
    H._clear_workspace_memory(ws3)
    check("purge sweep erases the ledger (and the cached link)", ws3 not in H._draft_docx_build_requests)


async def check_stop():
    print("\n[12] Stop does not cancel a build")
    _reset()
    ws = _ws("ddbstop")
    check("event runs unscoped", EVENT in run_control._UNSCOPED_EVENTS)
    UPLOAD_GATE.clear()
    body = _req(ws)
    await H.handle(EVENT, body)
    await asyncio.sleep(0.3)
    check("build not registered as a run task", not H._workspace_run_tasks.get(ws))
    H._cancel_workspace_run_tasks(ws)
    UPLOAD_GATE.set()
    await _settle()
    r = (_responses(body["requestId"]) or [{}])[0]
    check("still answered ok", r.get("status") == "ok", str(r))


async def check_layout():
    print("\n[14] formatLayout applied (SIM-217 shape)")
    _reset()
    ws = _ws("ddblayout")
    body = _req(ws, purpose="first_file", formatLayout=LETTERHEAD)
    r = await _one(body)
    check("status ok with a letterhead", r.get("status") == "ok", str(r))
    data = UPLOADS[0][1] if UPLOADS else b""
    check("letterhead text is in the Word file's header",
          bool(data) and LETTERHEAD_TEXT.encode() in _header_xml(data))
    # A letterhead image that cannot be fetched never fails the build.
    _reset()
    from app.processing import format_images

    original = format_images.fetch_format_image
    format_images.fetch_format_image = lambda *_a, **_k: None
    try:
        r = await _one(_req(ws, formatLayout=LETTERHEAD_WITH_IMAGE))
    finally:
        format_images.fetch_format_image = original
    check("missing letterhead image: still ok", r.get("status") == "ok", str(r))
    r = await _one(_req(ws, formatLayout=None))
    check("formatLayout null: still ok", r.get("status") == "ok", str(r))


async def check_title():
    print("\n[15] Standard layout without a title in the payload")
    from app.socket.handlers.draft_docx_build import _draft_title

    check("first heading is the title", _draft_title(None, MARKDOWN) == TITLE)
    check("an explicit title wins",
          _draft_title("Bail Application", MARKDOWN) == "Bail Application")
    court = ("IN THE COURT OF THE SESSIONS JUDGE\n\n"
             "BAIL APPLICATION UNDER SECTION 483 BNSS\n\n1. That.")
    check("no heading: the title block is read", "BAIL APPLICATION" in _draft_title("", court))
    check("empty draft: a neutral title", _draft_title(None, "") == "Draft")


async def _one(body):
    await H.handle(EVENT, body)
    await _settle()
    return (_responses(body["requestId"]) or [{}])[0]


def _header_xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return b"".join(zf.read(n) for n in zf.namelist() if n.startswith("word/header"))


async def check_logs():
    print("\n[13] Log hygiene")
    joined = "\n".join(LOG_LINES)
    check("summary line written", "draft_file_build status=" in joined)
    check("no title in logs", TITLE not in joined)
    check("no draft text in logs", SECRET_WORD not in joined)
    check("no link or signature in logs",
          FAKE_LINK not in joined and "SIGNATUREVALUE" not in joined and "storage.example.invalid" not in joined)


def _cleanup():
    for ws in list(H._draft_docx_build_requests):
        if ws.endswith(RUN_ID):
            H._draft_docx_build_requests.pop(ws, None)


CHECKS = [
    ("ready_markdown", check_ready_markdown),
    ("ready_html", check_ready_html),
    ("in_flight", check_in_flight),
    ("cached", check_cached),
    ("invalid", check_invalid),
    ("empty", check_empty),
    ("failures", check_failures),
    ("timeout", check_timeout),
    ("template", check_template_missing),
    ("metadata", check_metadata),
    ("purge", check_purge),
    ("stop", check_stop),
    ("layout", check_layout),
    ("title", check_title),
    ("logs", check_logs),
]


async def main():
    sink = logger.add(lambda m: LOG_LINES.append(str(m)), level="DEBUG")
    try:
        for _, fn in CHECKS:
            await fn()
    finally:
        logger.remove(sink)
        _cleanup()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list the check groups and exit")
    args = ap.parse_args()
    if args.list:
        for name, _ in CHECKS:
            print(name)
        sys.exit(0)
    sys.exit(asyncio.run(main()))
