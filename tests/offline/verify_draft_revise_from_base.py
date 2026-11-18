#!/usr/bin/env python3
"""
verify_draft_revise_from_base.py — Offline checks for SIM-219 at the handler.

A DRAFTING_ASSISTANT `user_message_request` that carries `baseDocumentUrl`
must revise the backend's latest .docx (the lawyer's edited version), or
refuse with `draftRefusal` and make no model call at all, chips included.
Exercises the real `_handle_drafting_assistant_message` and the real drafting
agent; only the download, the model, the session store, the .docx export and
the socket are stubbed. No API key, no backend, no network.

Covers:
  1. schema      — the new request fields parse (camelCase and snake_case)
  2. revised     — ai_response carries draftDocument with draftId/baseVersion,
                   built from the lawyer's text; no draftRefusal
  3. refusal     — open changes: draftRefusal with counts, no draftDocument,
                   draftStatus ready, no model call, no chips call
  4. unreadable  — base_unreadable refusal, no model call
  5. legacy      — no baseDocumentUrl: nothing downloaded, no new fields
  6. ai_response — every emitted payload validates against AIResponsePayload

USAGE
    python tests/offline/verify_draft_revise_from_base.py
    python tests/offline/verify_draft_revise_from_base.py --list
"""

import argparse
import asyncio
import importlib
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root

from app.socket.events import event_handler as H  # noqa: E402
from app.socket.schemas import AIResponsePayload, UserMessageRequestPayload  # noqa: E402
from app.processing.docx_generator import render_markdown_to_docx  # noqa: E402
import app.processing.draft_revision_base as B  # noqa: E402
from app.langgraph.drafting_agent import drafting_agent as DA  # noqa: E402

_gc = importlib.import_module("app.ai.gemini_client")

RESULTS = []
EMITS = []
MODEL_CALLS = []
CHIP_CALLS = []
DOWNLOADS = []

WS = "ws_fictional_sim219"
OWNER = "user_fictional_owner"
URL = "https://storage.example.invalid/drafts/ws_fictional/v5.docx?sig=fictional"
LAWYER_SENTENCE = "The sender also demands the return of the fictional delivery challans."
AI_SENTENCE = "Only the assistant's own copy has this sentence."
LAWYER_MD = f"# LEGAL NOTICE\n\n## Facts\n\n{LAWYER_SENTENCE}\n\n| Item | Amount |\n| --- | --- |\n| Principal | 50,000 |\n"
AI_MD = f"# LEGAL NOTICE\n\n## Facts\n\n{AI_SENTENCE}\n"

SESSION = {}


class _FakeSocket:
    # Must return True: SocketClient.emit reports DELIVERY.
    async def emit(self, event, payload):
        EMITS.append((event, payload))
        return True

    async def emit_ai_response(self, payload):
        EMITS.append(("ai_response", payload))
        return True


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (f"\n        {detail}" if detail and not ok else ""))


def _docx(md=LAWYER_MD, *, tracked=False, comment=False):
    data = render_markdown_to_docx(md, template_docx_bytes=None, title="Legal Notice")
    if not (tracked or comment):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}
    if tracked:
        doc = parts["word/document.xml"].decode("utf-8")
        i = doc.index("<w:sectPr")
        parts["word/document.xml"] = (
            doc[:i] + '<w:p><w:del w:id="9" w:author="Fictional" w:date="2026-11-01T00:00:00Z">'
            "<w:r><w:delText>old</w:delText></w:r></w:del></w:p>" + doc[i:]).encode("utf-8")
    if comment:
        parts["word/comments.xml"] = (
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:comment w:id="0" w:author="Fictional"><w:p><w:r><w:t>Check</w:t></w:r></w:p></w:comment>'
            "</w:comments>").encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in parts.items():
            zf.writestr(n, b)
    return out.getvalue()


def _install(base_bytes):
    SESSION.clear()
    SESSION.update({
        "draft_status": "ready", "draft_title": "Legal Notice", "source": "blank",
        "current_draft_version": 4, "last_draft_markdown": AI_MD,
        "last_draft_document": {"title": "Legal Notice", "format": "markdown",
                                "content": AI_MD, "version": 4},
    })

    async def _load(**_kw):
        return SESSION

    async def _save(**_kw):
        return None

    async def _export(**_kw):
        return None

    async def _router(prompt="", *_a, **_kw):
        MODEL_CALLS.append("router")
        return json.dumps({"action": "revise_draft",
                           "params": {"revision_instruction": "Add a demand."},
                           "assistant_message": "", "reasoning": "edit"})

    async def _render(prompt="", *_a, **_kw):
        MODEL_CALLS.append("render")
        kept = LAWYER_SENTENCE if LAWYER_SENTENCE in prompt else AI_SENTENCE
        return f"# LEGAL NOTICE\n\n## Facts\n\n{kept}\n\n## Demand\n\nPay in fifteen days.\n"

    async def _fetch(url, **_kw):
        DOWNLOADS.append(url)
        return base_bytes

    async def _chips(**_kw):
        CHIP_CALLS.append(True)
        return []

    DA._load_session_state = _load
    DA._save_session_state = _save
    DA._export_draft_docx = _export
    _gc.gemini_client.generate = _router
    _gc.gemini_client.generate_longform = _render
    B.fetch_base_docx = _fetch
    H._generate_drafting_chips = _chips
    EMITS.clear()
    MODEL_CALLS.clear()
    CHIP_CALLS.clear()
    DOWNLOADS.clear()


def _payload(**extra):
    body = {
        "content": "Add a demand paragraph.",
        "chatType": "DRAFTING_ASSISTANT",
        "workspaceId": WS,
        "userId": OWNER,
        "chatId": "chat_fictional_sim219",
        "timestamp": "2026-11-17T10:00:00Z",
        "actionableId": "draft_session_fictional",
    }
    body.update(extra)
    return UserMessageRequestPayload(**body)


async def _run(payload):
    await H._handle_drafting_assistant_message(
        payload=payload, workspace_id=WS, user_id=OWNER, chat_id=payload.chatId)
    out = [p for e, p in EMITS if e == "ai_response"]
    return out[-1] if out else None


def check_schema():
    print("\n[1] Schema")
    p = _payload(draftId="d1", baseVersion=5, baseDocumentUrl=URL)
    check("camelCase fields parse", (p.draftId, p.baseVersion, p.baseDocumentUrl) == ("d1", 5, URL))
    s = UserMessageRequestPayload(**{
        "content": "x", "chat_type": "DRAFTING_ASSISTANT", "timestamp": "t",
        "draft_id": "d2", "base_version": 6, "base_document_url": URL})
    check("snake_case aliases parse", (s.draftId, s.baseVersion, s.baseDocumentUrl) == ("d2", 6, URL))
    legacy = _payload()
    check("fields optional", legacy.draftId is None and legacy.baseDocumentUrl is None)
    check("absent fields are None (all three)",
          (legacy.draftId, legacy.baseVersion, legacy.baseDocumentUrl) == (None, None, None))


async def check_revised():
    print("\n[2] Revised from the lawyer's version")
    _install(_docx())
    resp = await _run(_payload(draftId="draft_fictional_7", baseVersion=5, baseDocumentUrl=URL))
    doc = (resp or {}).get("draftDocument") or {}
    check("downloaded once", DOWNLOADS == [URL])
    check("draftDocument built from the lawyer's text", LAWYER_SENTENCE in (doc.get("content") or ""))
    check("draftId echoed", doc.get("draftId") == "draft_fictional_7")
    check("baseVersion echoed", doc.get("baseVersion") == 5)
    check("version is base + 1", doc.get("version") == 6, str(doc.get("version")))
    check("no draftRefusal", "draftRefusal" not in (resp or {}))
    check("payload validates", _validates(resp))


async def check_refusal():
    print("\n[3] Open changes")
    _install(_docx(tracked=True, comment=True))
    resp = await _run(_payload(draftId="draft_fictional_7", baseVersion=5, baseDocumentUrl=URL))
    check("draftRefusal open_changes with counts",
          (resp or {}).get("draftRefusal") == {"code": "open_changes", "trackedChanges": 1, "comments": 1},
          json.dumps((resp or {}).get("draftRefusal")))
    check("no draftDocument", "draftDocument" not in (resp or {}))
    check("draftStatus ready", (resp or {}).get("draftStatus") == "ready")
    check("lawyer-facing message", "tracked changes or comments" in ((resp or {}).get("content") or ""))
    check("no model call", MODEL_CALLS == [], str(MODEL_CALLS))
    check("no chips call", CHIP_CALLS == [])
    check("session draft untouched", SESSION.get("last_draft_markdown") == AI_MD)
    check("payload validates", _validates(resp))


async def check_unreadable():
    print("\n[4] Unreadable base")
    _install(b"<html>expired link</html>")
    resp = await _run(_payload(baseVersion=5, baseDocumentUrl=URL))
    check("draftRefusal base_unreadable", (resp or {}).get("draftRefusal") == {"code": "base_unreadable"})
    check("no draftDocument", "draftDocument" not in (resp or {}))
    check("no model call", MODEL_CALLS == [] and CHIP_CALLS == [])
    check("payload validates", _validates(resp))


async def check_legacy():
    print("\n[5] No baseDocumentUrl")
    _install(_docx())
    resp = await _run(_payload())
    doc = (resp or {}).get("draftDocument") or {}
    check("nothing downloaded", DOWNLOADS == [])
    check("revised from the AI's own copy", AI_SENTENCE in (doc.get("content") or ""))
    check("no new fields", "draftId" not in doc and "baseVersion" not in doc
          and "draftRefusal" not in (resp or {}))
    check("model and chips called as before", "render" in MODEL_CALLS and CHIP_CALLS)
    check("payload validates", _validates(resp))


def _validates(resp):
    if not resp:
        return False
    try:
        AIResponsePayload(**resp)
        return True
    except Exception as e:  # noqa: BLE001
        print("        validation:", type(e).__name__)
        return False


CHECKS = [
    ("schema", check_schema),
    ("revised", check_revised),
    ("refusal", check_refusal),
    ("unreadable", check_unreadable),
    ("legacy", check_legacy),
]


async def main():
    H._socket_client = _FakeSocket()
    for _, fn in CHECKS:
        if asyncio.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
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
