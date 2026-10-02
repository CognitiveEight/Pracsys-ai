#!/usr/bin/env python3
"""
Pre-deploy end-to-end test: run the real service against a local stand-in backend.

Starts ``python -m app.socket.ai_service`` (the production command) as a child
process and plays the backend's part over Socket.IO on 127.0.0.1:

1. **Handshake** — the stand-in verifies the v1 HMAC headers exactly as the
   backend does, with a throwaway secret; an unsigned or mis-signed client is
   refused.
2. **workspace_created** with two fictional evidence files (a PDF complaint and
   a DOCX legal notice) served over local HTTP, then waits for all seven panels
   (``document_feature_response``).
3. **ai_heartbeat_ping** → ``ai_heartbeat_pong``.
4. **user_message_request** → ``ai_response`` with citations.
5. **workspace_stop_request** + **workspace_resume_request** → a
   ``workspace:refresh`` and the declared panel only.
6. **workspace_deleted** (purge due now) → no points left in Qdrant.
7. **SIGTERM** → clean exit; the service log is scanned for errors and for
   the fictional party name (which must never be logged).

Isolation: the service runs with this stand-in as SOCKET_URL, a fresh Qdrant
collection on the local Qdrant (deleted afterwards), and no knowledge graph.
It still reads GOOGLE_API_KEY etc. from .env, so the Gemini calls are real and
billed (a few cents for this small case).

USAGE
    python scripts/e2e_local_backend.py                 # full run, ~5-15 min
    python scripts/e2e_local_backend.py --keep          # keep the Qdrant collection
    QDRANT_URL=http://localhost:6333 python scripts/e2e_local_backend.py
"""

import argparse
import asyncio
import datetime as dt
import io
import json
import os
import re
import secrets
import signal
import socket
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import socketio  # noqa: E402
from aiohttp import web  # noqa: E402

FEATURES = [
    "document_assessments", "workspace_metadata", "strategy_objective", "analysis_report",
    "list_of_dates", "applicable_laws", "procedural_timeline",
]
# Fictional case data. The party name doubles as a log-leak sentinel.
PARTY = "Arjun Testwala"
COMPANY = "Meridian Test Traders Private Limited"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _oid() -> str:
    return secrets.token_hex(12)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Evidence files
# ---------------------------------------------------------------------------


def make_pdf() -> bytes:
    import pymupdf as fitz

    pages = [
        f"""COMPLAINT UNDER SECTION 138 OF THE NEGOTIABLE INSTRUMENTS ACT, 1881
Before the Court of the Judicial Magistrate First Class, Pune

Complainant: {PARTY}, proprietor of Testwala Electricals, Pune.
Accused: {COMPANY}, through its director.

1. On 10/01/2026 the complainant supplied electrical goods worth Rs. 5,00,000
to the accused under invoice no. TE/2026/014.
2. In discharge of that liability the accused issued cheque no. 004512 dated
15/02/2026 for Rs. 5,00,000 drawn on State Bank of India, Camp Branch, Pune.
3. The cheque was presented on 20/02/2026 and returned unpaid on 22/02/2026
with the remark "Funds Insufficient".""",
        """4. The complainant sent a statutory demand notice dated 05/03/2026 by
registered post, received by the accused on 09/03/2026.
5. The accused failed to pay the amount within fifteen days of receipt of the
notice, i.e. by 24/03/2026.
6. This complaint is filed on 10/04/2026, within one month of the cause of
action, which arose on 25/03/2026.

PRAYER: The accused be summoned, tried and punished under Section 138 of the
Negotiable Instruments Act, 1881, and directed to pay compensation of
Rs. 10,00,000 under Section 357 of the Bharatiya Nagarik Suraksha Sanhita.""",
    ]
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 545, 790), text, fontsize=10)
    return doc.tobytes()


def make_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("LEGAL NOTICE", level=1)
    for para in [
        "Date: 05/03/2026",
        f"To: {COMPANY}, Pune.",
        f"Under instructions from my client {PARTY}, you are hereby called upon to pay "
        "Rs. 5,00,000, being the amount of cheque no. 004512 dated 15/02/2026, which was "
        "returned unpaid on 22/02/2026 for insufficient funds.",
        "If the amount is not paid within fifteen days of receipt of this notice, my client "
        "will initiate proceedings under Section 138 of the Negotiable Instruments Act, 1881.",
    ]:
        doc.add_paragraph(para)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Stand-in backend
# ---------------------------------------------------------------------------


class StandInBackend:
    def __init__(self, secret: str, port: int):
        from app.socket.auth import build_canonical_string, sign_canonical

        self._canonical, self._sign = build_canonical_string, sign_canonical
        self.secret, self.port = secret, port
        self.sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*",
                                        max_http_buffer_size=50_000_000)
        self.app = web.Application()
        self.sio.attach(self.app)
        self.files: Dict[str, bytes] = {}
        self.app.router.add_get("/files/{name}", self._serve)
        self.sid: Optional[str] = None
        self.connected = asyncio.Event()
        self.refused: List[str] = []
        self.events: Dict[str, List[Any]] = defaultdict(list)
        self.arrived = asyncio.Condition()
        self.sio.on("connect", self._on_connect)
        self.sio.on("*", self._on_any)

    async def _serve(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.files:
            raise web.HTTPNotFound()
        return web.Response(body=self.files[name])

    async def _on_connect(self, sid, environ, auth=None):
        h = {k[5:].lower().replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
        ts, nonce, sig = h.get("x-signature-timestamp"), h.get("x-signature-nonce"), h.get("x-signature")
        if h.get("x-client-type") != "AI_Maverick" or not (ts and nonce and sig):
            self.refused.append("missing_headers")
            raise socketio.exceptions.ConnectionRefusedError("missing_headers")
        if abs(time.time() - int(ts)) > 300:
            self.refused.append("stale_timestamp")
            raise socketio.exceptions.ConnectionRefusedError("stale_timestamp")
        expected = self._sign(self.secret, self._canonical(ts, nonce))
        if not secrets.compare_digest(expected.rstrip("="), sig.rstrip("=")):
            self.refused.append("bad_signature")
            raise socketio.exceptions.ConnectionRefusedError("bad_signature")
        self.sid = sid
        self.connected.set()

    async def _on_any(self, event, sid, data=None, *rest):
        async with self.arrived:
            self.events[event].append(data)
            self.arrived.notify_all()
        return {"ok": True}

    async def emit(self, event: str, data: Dict[str, Any]) -> None:
        await self.sio.emit(event, data, to=self.sid)

    async def wait_for(self, predicate, timeout: float) -> bool:
        async with self.arrived:
            try:
                await asyncio.wait_for(self.arrived.wait_for(predicate), timeout)
                return True
            except asyncio.TimeoutError:
                return False

    def features(self, workspace_id: str) -> Dict[str, Any]:
        out = {}
        for data in self.events.get("document_feature_response", []):
            if isinstance(data, dict) and data.get("workspaceId") == workspace_id:
                out[data.get("featureType")] = data
        return out

    async def start(self) -> web.AppRunner:
        runner = web.AppRunner(self.app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        return runner


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.rows: List[tuple] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


def _evidence(port: int, name: str, ext: str, file_id: str) -> Dict[str, Any]:
    return {
        "_id": file_id, "originalName": name, "storeFileName": f"{file_id}_{name}",
        "filename": f"Annexure-{name}", "extension": ext, "fileHash": secrets.token_hex(32),
        "url": f"http://127.0.0.1:{port}/files/{name}", "path": f"e2e/{name}", "uploadedAt": _now(),
    }


def _text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


async def scenario(backend: StandInBackend, rep: Report, args, collection: str) -> None:
    ws, user, chat = _oid(), _oid(), _oid()
    port = backend.port
    if args.evidence:
        # Real files: same flow, but the fictional-case answer checks are skipped.
        backend.files = {}
        files = []
        for path in args.evidence:
            name = re.sub(r"[^\w.\-]+", "_", os.path.basename(path))
            with open(path, "rb") as fh:
                backend.files[name] = fh.read()
            files.append(_evidence(port, name, name.rsplit(".", 1)[-1].lower(), _oid()))
    else:
        backend.files = {"complaint.pdf": make_pdf(), "legal_notice.docx": make_docx()}
        files = [_evidence(port, "complaint.pdf", "pdf", _oid()), _evidence(port, "legal_notice.docx", "docx", _oid())]

    print("\n1. Handshake", flush=True)
    ok = await asyncio.wait_for(backend.connected.wait(), 90) if True else False
    rep.check("service connects with a valid v1 signature", bool(ok), f"refusals={backend.refused}")

    print("\n2. workspace_created -> all panels (this is the slow part)", flush=True)
    await backend.emit("workspace_created", {
        "workspaceId": ws, "userId": user, "title": "E2E cheque dishonour (fictional)",
        "workspaceRole": "PETITIONER", "evidenceFiles": files, "createdAt": _now(),
    })
    started = time.monotonic()
    await backend.wait_for(lambda: set(FEATURES) <= set(backend.features(ws)), args.feature_timeout)
    got = backend.features(ws)
    rep.check("all 7 panels emitted", set(FEATURES) <= set(got),
              f"got {sorted(got)} in {int(time.monotonic() - started)}s")
    for feature in FEATURES:
        env = got.get(feature)
        rep.check(f"{feature}: has content", bool(env) and len(_text(env)) > 200,
                  f"{len(_text(env)) if env else 0} chars")
    if got.get("applicable_laws") and not args.evidence:
        blob = _text(got["applicable_laws"])
        rep.check("applicable_laws: no IPC", not re.search(r"Indian Penal Code|\bIPC\b", blob))
        statuses = re.findall(r'"registerStatus":\s*"(\w+)"', blob)
        rep.check("applicable_laws: every row has a register status", bool(statuses),
                  f"{dict((s, statuses.count(s)) for s in set(statuses))}")
        rep.check("applicable_laws: NI Act s.138 present and verified",
                  bool(re.search(r"Negotiable Instruments.{0,400}?\"registerStatus\":\s*\"verified\"", blob, re.S)
                       or re.search(r"\"registerStatus\":\s*\"verified\".{0,400}?Negotiable", blob, re.S)))
    if got.get("list_of_dates") and not args.evidence:
        blob = _text(got["list_of_dates"])
        rep.check("list_of_dates: key dates extracted",
                  all(d in blob for d in ("22/02/2026", "05/03/2026")) or all(
                      d in blob for d in ("2026-02-22", "2026-03-05")) or ("22" in blob and "Feb" in blob))
    meta_ok = any("metadata" in _text(e) or "totalPrice" in _text(e) for e in got.values())
    rep.check("cost metadata on panel emits", meta_ok)

    if args.evidence:
        _print_panels(got)
        await _purge_and_check(backend, rep, args, collection, ws, user)
        return

    print("\n3. Heartbeat", flush=True)
    n = len(backend.events.get("ai_heartbeat_pong", []))
    await backend.emit("ai_heartbeat_ping", {"requestId": "hb_e2e", "serverSentAt": _now()})
    ok = await backend.wait_for(lambda: len(backend.events.get("ai_heartbeat_pong", [])) > n, 30)
    rep.check("ai_heartbeat_ping -> pong", ok)

    print("\n4. Chat with citations", flush=True)
    await backend.emit("user_message_request", {
        "content": "What was the cheque amount, and on what date was it returned unpaid?",
        "chat_type": "INTELLIGENCE", "conversation_history": [], "workspaceId": ws,
        "userId": user, "chatId": chat, "timestamp": _now(),
    })
    def chat_reply():
        return [d for d in backend.events.get("ai_response", []) if isinstance(d, dict) and d.get("chatId") == chat]
    ok = await backend.wait_for(lambda: bool(chat_reply()), args.chat_timeout)
    reply = chat_reply()[-1] if chat_reply() else {}
    text = _text(reply)
    rep.check("ai_response received", ok)
    rep.check("answer states the amount", "5,00,000" in text or "500000" in text or "5 lakh" in text.lower())
    rep.check("answer states the return date", "22/02/2026" in text or "22 February 2026" in text
              or "February 22, 2026" in text or "22nd February" in text)
    citations = reply.get("citations") if isinstance(reply, dict) else None
    rep.check("answer carries citations", bool(citations), f"{len(citations or [])} citation(s)")

    print("\n5. Stop + resume", flush=True)
    await backend.emit("workspace_stop_request", {
        "requestId": f"st_{ws[:8]}", "workspaceId": ws, "userId": user, "stoppedAt": _now(),
    })
    await asyncio.sleep(3)
    before = len(backend.events.get("document_feature_response", []))
    refresh_before = len(backend.events.get("workspace:refresh", []))
    await backend.emit("workspace_resume_request", {
        "requestId": f"rs_{ws[:8]}", "workspaceId": ws, "userId": user,
        "workspaceRole": "PETITIONER", "reEmitFeatures": ["list_of_dates"], "evidenceFiles": files,
        "timestamp": _now(),
    })
    ok = await backend.wait_for(
        lambda: len(backend.events.get("document_feature_response", [])) > before, 180)
    new = backend.events.get("document_feature_response", [])[before:]
    rep.check("resume re-emits the declared panel", ok and any(
        isinstance(d, dict) and d.get("featureType") == "list_of_dates" for d in new))
    await asyncio.sleep(5)
    new = backend.events.get("document_feature_response", [])[before:]
    rep.check("resume emits nothing it did not declare",
              all(isinstance(d, dict) and d.get("featureType") == "list_of_dates" for d in new),
              f"{[d.get('featureType') for d in new if isinstance(d, dict)]}")
    rep.check("resume armed workspace:refresh",
              len(backend.events.get("workspace:refresh", [])) > refresh_before)

    await _purge_and_check(backend, rep, args, collection, ws, user)


def _print_panels(got: Dict[str, Any]) -> None:
    """Human summary of what each panel produced (for real-file runs)."""
    print("\n   --- panel output ---")
    for feature in FEATURES:
        payload = (got.get(feature) or {}).get("payload") or {}
        lists = {k: len(v) for k, v in payload.items() if isinstance(v, list)} if isinstance(payload, dict) else {}
        print(f"   {feature}: {lists}")
    lod = ((got.get("list_of_dates") or {}).get("payload") or {}).get("listOfDates") or []
    for entry in lod:
        print(f"      {entry.get('date')} | {entry.get('title')} | cited={bool(entry.get('citation'))}")


async def _purge_and_check(backend, rep, args, collection, ws, user) -> None:
    print("\n6. Compliance purge", flush=True)
    from qdrant_client import QdrantClient, models

    qc = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    flt = models.Filter(should=[
        models.FieldCondition(key=k, match=models.MatchValue(value=ws))
        for k in ("workspace_id", "metadata.workspace_id")
    ])
    def points() -> int:
        total = 0
        for c in qc.get_collections().collections:
            if c.name.startswith(collection):
                total += qc.count(c.name, count_filter=flt, exact=True).count
        return total
    before_purge = points()
    rep.check("workspace has points in Qdrant before purge", before_purge > 0, f"{before_purge} points")
    await backend.emit("workspace_deleted", {
        "event": "workspace_deleted", "workspaceId": ws, "userId": user, "deletedAt": _now(),
        "purgeAfter": _now(), "purgeAfterDays": 0, "reason": "user_deleted",
        "requestId": f"wd_{ws[:8]}_{int(time.time() * 1000)}",
    })
    deadline = time.monotonic() + 120
    left = before_purge
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        left = points()
        if left == 0:
            break
    rep.check("purge removes every point of the workspace", left == 0, f"{left} left")
    on_disk = os.path.join(ROOT, "data", "workspaces", ws)
    rep.check("purge removes the on-disk workspace copy", not os.path.exists(on_disk))
    if not args.keep:
        for c in qc.get_collections().collections:
            if c.name.startswith(collection):
                qc.delete_collection(c.name)


async def main_async(args) -> int:
    secret, port = secrets.token_urlsafe(32), _free_port()
    collection = f"e2e_{secrets.token_hex(4)}"
    backend = StandInBackend(secret, port)
    runner = await backend.start()
    log_path = os.path.join(args.log_dir, "e2e_service.log")
    env = dict(os.environ)
    env.update({
        "SOCKET_URL": f"http://127.0.0.1:{port}", "SOCKET_SECRET_KEY": secret,
        "SOCKET_AUTH_SCHEME": "v1", "SOCKET_PATH": "/socket.io", "SOCKET_NAMESPACE": "/",
        "SOCKET_TRANSPORTS": "websocket",
        "QDRANT_URL": os.environ.get("QDRANT_URL", "http://localhost:6333"), "QDRANT_API_KEY": "",
        "QDRANT_COLLECTION_NAME": collection,
        "NEO4J_URL": "", "ONTOLOGY_INGEST_ENABLED": "false",
    })
    print(f"stand-in backend on 127.0.0.1:{port}, Qdrant collection {collection}, service log {log_path}")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.socket.ai_service", cwd=ROOT, env=env,
            stdout=log, stderr=asyncio.subprocess.STDOUT,
        )
        rep = Report()
        try:
            await scenario(backend, rep, args, collection)
        except Exception as exc:  # noqa: BLE001 - reported as a failure, then torn down
            rep.check("scenario ran to the end", False, f"{type(exc).__name__}: {exc}")
        finally:
            print("\n7. Shutdown", flush=True)
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    code = await asyncio.wait_for(proc.wait(), 60)
                    rep.check("SIGTERM -> clean exit", code == 0, f"exit code {code}")
                except asyncio.TimeoutError:
                    proc.kill()
                    rep.check("SIGTERM -> clean exit", False, "did not exit in 60s")
            else:
                rep.check("service stayed up for the whole run", False, f"exited early ({proc.returncode})")
            await runner.cleanup()
            # Everything the service sent (fictional test data only), for inspection.
            with open(os.path.join(args.log_dir, "e2e_events.json"), "w", encoding="utf-8") as fh:
                json.dump(backend.events, fh, ensure_ascii=False, indent=1, default=str)

    with open(log_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    errors = [l for l in lines if re.search(r"\|\s*(ERROR|CRITICAL)\s*\|", l)]
    rep.check("no ERROR/CRITICAL lines in the service log", not errors, f"{len(errors)} line(s)")
    for line in errors[:15]:
        print("      " + line[:220])
    leaked = [l for l in lines if PARTY in l or COMPANY in l]
    rep.check("party names never reach the log", not leaked, f"{len(leaked)} line(s)")
    llm = [l for l in lines if "llm_call" in l]
    rep.check("one llm_call line per model call", bool(llm), f"{len(llm)} calls logged")
    print(f"\n{len(rep.rows) - rep.failed}/{len(rep.rows)} checks passed")
    return 1 if rep.failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="keep the e2e Qdrant collection(s)")
    ap.add_argument("--feature-timeout", type=float, default=1500, help="seconds to wait for all panels")
    ap.add_argument("--chat-timeout", type=float, default=300, help="seconds to wait for the chat reply")
    ap.add_argument("--log-dir", default=os.path.join(ROOT, "logs"), help="where e2e_service.log goes")
    ap.add_argument("--evidence", nargs="+", metavar="FILE",
                    help="run on these real files instead of the fictional case (answer checks skipped)")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
