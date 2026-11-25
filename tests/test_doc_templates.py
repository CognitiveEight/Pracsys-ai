"""SIM-228: legacy .doc templates are converted to .docx at ingest.

Converter tests run a fake ``soffice`` (a shell script on PATH) so CI needs no
LibreOffice; one real round trip runs only when ``soffice`` is installed.
Ingest tests drive ``DraftingAgent.ingest_template`` with the download,
storage and model calls stubbed. Fictional data only; no network.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from io import BytesIO

import pytest
from docx import Document

import app.langgraph.drafting_agent as drafting_module
from app.core.config import settings
from app.processing import doc_to_docx
from app.processing.doc_to_docx import convert_doc_to_docx

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FAKE_DOC = OLE2 + b"\x00" * 512


def _docx_bytes(*paragraphs: str) -> bytes:
    doc = Document()
    for text in paragraphs or ("Fictional clause one.",):
        doc.add_paragraph(text)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def _fake_soffice(tmp_path, monkeypatch, body: str) -> None:
    """Put an executable ``soffice`` running ``body`` first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "soffice"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")


# The fake writes $FAKE_DOCX to <outdir>/input.docx, as LibreOffice would.
_WRITES_DOCX = (
    'while [ "$1" != "--outdir" ]; do shift; done; '
    'cp "$FAKE_DOCX" "$2/input.docx"'
)


@pytest.fixture
def ready_docx(tmp_path, monkeypatch):
    path = tmp_path / "ready.docx"
    path.write_bytes(_docx_bytes("Fictional heading", "Fictional clause one."))
    monkeypatch.setenv("FAKE_DOCX", str(path))
    return path.read_bytes()


# ------------------------------------------------------------------ converter


def test_converts_with_soffice(tmp_path, monkeypatch, ready_docx):
    _fake_soffice(tmp_path, monkeypatch, _WRITES_DOCX)
    assert convert_doc_to_docx(FAKE_DOC) == ready_docx


def test_a_renamed_docx_is_returned_unchanged(monkeypatch):
    monkeypatch.setattr(doc_to_docx, "_soffice_path", lambda: pytest.fail("not needed"))
    data = _docx_bytes()
    assert convert_doc_to_docx(data) == data


def test_no_soffice_returns_none(monkeypatch):
    monkeypatch.setattr(doc_to_docx, "_soffice_path", lambda: None)
    assert doc_to_docx.is_available() is False
    assert convert_doc_to_docx(FAKE_DOC) is None


def test_disabled_returns_none(tmp_path, monkeypatch, ready_docx):
    _fake_soffice(tmp_path, monkeypatch, _WRITES_DOCX)
    monkeypatch.setattr(settings, "template_doc_convert_enabled", False)
    assert convert_doc_to_docx(FAKE_DOC) is None


@pytest.mark.parametrize(
    "body",
    [
        "exit 1",  # LibreOffice failed
        "exit 0",  # claimed success, wrote nothing
        'while [ "$1" != "--outdir" ]; do shift; done; echo junk > "$2/input.docx"',
    ],
    ids=["exit-code", "no-output", "not-a-word-file"],
)
def test_failed_conversion_returns_none(tmp_path, monkeypatch, body):
    _fake_soffice(tmp_path, monkeypatch, body)
    assert convert_doc_to_docx(FAKE_DOC) is None


def test_timeout_kills_the_process_group(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    # The script starts a child (as soffice starts soffice.bin) and hangs.
    _fake_soffice(tmp_path, monkeypatch, f"sleep 30 & echo $! > {marker}; wait")
    monkeypatch.setattr(settings, "template_doc_convert_timeout_seconds", 1)
    started = time.monotonic()
    assert convert_doc_to_docx(FAKE_DOC) is None
    assert time.monotonic() - started < 10
    child = int(marker.read_text().strip())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_empty_or_oversized_input_returns_none(monkeypatch):
    monkeypatch.setattr(doc_to_docx, "_MAX_INPUT_BYTES", 10)
    assert convert_doc_to_docx(b"") is None
    assert convert_doc_to_docx(FAKE_DOC) is None


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_real_round_trip(tmp_path):
    src = tmp_path / "fictional.docx"
    src.write_bytes(_docx_bytes("Fictional heading", "Fictional clause one."))
    subprocess.run(
        ["soffice", "--headless", f"-env:UserInstallation=file://{tmp_path}/p",
         "--convert-to", "doc", "--outdir", str(tmp_path), str(src)],
        check=True, timeout=120, capture_output=True,
    )
    out = convert_doc_to_docx((tmp_path / "fictional.doc").read_bytes())
    assert out is not None
    texts = [p.text for p in Document(BytesIO(out)).paragraphs]
    assert "Fictional clause one." in texts


# ------------------------------------------------------------------- ingest


class _Store:
    def __init__(self):
        self.puts = []

    async def put(self, *, user_id, template_id, payload):
        self.puts.append(payload)
        return True


@pytest.fixture
def ingest_env(monkeypatch):
    """Stub everything ingest_template reaches outside this process."""
    from app.langgraph import template_store as ts
    from app.processing import file_handlers
    from app.processing.document_processor import document_processor
    from app.storage import gcs

    seen = {"downloads": 0, "extract_names": [], "uploads": [], "profiles": 0}

    async def download(url, filename):
        seen["downloads"] += 1
        return FAKE_DOC, len(FAKE_DOC)

    def extract(content, filename):
        seen["extract_names"].append(filename)
        return "FICTIONAL HEADING\nFictional clause one." if filename.endswith(".docx") \
            else "FICTIONAL SCRAPE WITH NO BREAKS"

    def upload(bucket, name, data, mime, public=False):
        seen["uploads"].append(data)

    async def profile(self, **kw):
        seen["profiles"] += 1
        return {}

    async def none(self, **kw):
        return None

    async def passthrough(self, *, name, extracted_text):
        return extracted_text

    monkeypatch.setattr(document_processor, "http_client", object(), raising=False)
    monkeypatch.setattr(document_processor, "_download_file", download)
    monkeypatch.setattr(file_handlers.file_processor, "process_file_by_extension", extract)
    monkeypatch.setattr(gcs, "upload_bytes", upload)
    monkeypatch.setattr(settings, "gcs_docx_bucket_name", "fictional-bucket", raising=False)
    store = _Store()
    monkeypatch.setattr(ts, "template_store", store)
    monkeypatch.setattr(drafting_module, "template_store", store, raising=False)
    A = drafting_module.DraftingAgent
    monkeypatch.setattr(A, "_extract_template_profile", profile)
    monkeypatch.setattr(A, "_classify_document_type", none)
    monkeypatch.setattr(A, "_clean_template_ocr_text", passthrough)
    seen["store"] = store
    return seen


def _agent():
    return drafting_module.DraftingAgent.__new__(drafting_module.DraftingAgent)


async def _ingest(previous=None, file_hash="h1"):
    return await _agent().ingest_template(
        user_id="user_fictional", template_id="tpl_fictional", name="Fictional deed",
        category="Document Draft", extension="doc", mime_type="application/msword",
        file_url="https://storage.example/tpl", path="templates/tpl", file_hash=file_hash,
        previous_payload=previous,
    )


async def test_doc_template_is_converted_and_stored_as_docx(monkeypatch, ingest_env):
    converted = _docx_bytes("FICTIONAL HEADING", "Fictional clause one.")

    async def convert(data):
        assert data == FAKE_DOC
        return converted

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    payload = await _ingest()
    assert payload is not None
    # Text came from the .docx, so it keeps its paragraph breaks.
    assert ingest_env["extract_names"][-1].endswith(".docx")
    assert "\n" in payload["extracted_text"]
    # The converted file is the stored skeleton.
    assert ingest_env["uploads"] == [converted]
    assert payload["template_docx_gcs_uri"].startswith("gs://fictional-bucket/")
    # The payload keeps the source type and records the conversion.
    assert payload["extension"] == "doc"
    assert payload["doc_conversion"] == "ok"


async def test_failed_conversion_keeps_todays_behaviour(monkeypatch, ingest_env):
    async def convert(data):
        return None

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    payload = await _ingest()
    assert ingest_env["extract_names"][-1].endswith(".doc")
    assert ingest_env["uploads"] == []
    assert payload["template_docx_gcs_uri"] is None
    assert payload["doc_conversion"] == "failed"


def _cached(doc_conversion=None):
    payload = {
        "file_hash": "h1", "extracted_text": "FICTIONAL SCRAPE WITH NO BREAKS",
        "extraction_status": "ready", "extension": "doc", "document_type": "Deed",
        "interactive_variables": [{"key": "fictional"}],
    }
    if doc_conversion:
        payload["doc_conversion"] = doc_conversion
    return payload


async def test_cached_doc_without_skeleton_is_reingested_once(monkeypatch, ingest_env):
    converted = _docx_bytes("FICTIONAL HEADING", "Fictional clause one.")

    async def convert(data):
        return converted

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    monkeypatch.setattr(drafting_module, "_doc_converter_available", lambda: True)
    payload = await _ingest(previous=_cached())
    assert ingest_env["downloads"] == 1
    assert payload["doc_conversion"] == "ok"
    assert ingest_env["uploads"] == [converted]
    assert ingest_env["profiles"] == 1  # re-profiled from the structured text


@pytest.mark.parametrize(
    "available,previous_state",
    [(False, None), (True, "failed"), (True, "ok")],
    ids=["no-converter", "already-failed", "already-converted"],
)
async def test_cached_doc_is_not_reingested(monkeypatch, ingest_env, available, previous_state):
    async def convert(data):
        pytest.fail("must not convert")

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    monkeypatch.setattr(drafting_module, "_doc_converter_available", lambda: available)
    payload = await _ingest(previous=_cached(previous_state))
    assert ingest_env["downloads"] == 0
    assert payload["extracted_text"] == "FICTIONAL SCRAPE WITH NO BREAKS"


async def test_lazy_backfill_converts_a_doc(monkeypatch, ingest_env):
    converted = _docx_bytes()

    async def convert(data):
        return converted

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    out = await _agent()._lazy_backfill_template_skeleton(
        tpl={"extension": "doc", "source_url": "https://storage.example/tpl", "name": "Fictional"},
        user_id="user_fictional", template_id="tpl_fictional", bucket="fictional-bucket",
    )
    assert out == converted
    assert ingest_env["uploads"] == [converted]


async def test_lazy_backfill_gives_up_when_conversion_fails(monkeypatch, ingest_env):
    async def convert(data):
        return None

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    out = await _agent()._lazy_backfill_template_skeleton(
        tpl={"extension": "doc", "source_url": "https://storage.example/tpl", "name": "Fictional"},
        user_id="user_fictional", template_id="tpl_fictional", bucket="fictional-bucket",
    )
    assert out is None
    assert ingest_env["uploads"] == []


# ------------------------------------------------ added in verification


def test_soffice_gets_the_input_and_an_isolated_profile(tmp_path, monkeypatch, ready_docx):
    """AC-1: the bytes handed to LibreOffice are the .doc, converted to docx,
    with a profile and $HOME inside the call's own temp dir."""
    record = tmp_path / "record"
    record.mkdir()
    body = (
        f'printf "%s\\n" "$@" > {record}/args; echo "$HOME" > {record}/home; '
        f'for a; do last="$a"; done; cp "$last" {record}/input; ' + _WRITES_DOCX
    )
    _fake_soffice(tmp_path, monkeypatch, body)
    assert convert_doc_to_docx(FAKE_DOC) == ready_docx
    assert (record / "input").read_bytes() == FAKE_DOC
    args = (record / "args").read_text().splitlines()
    assert "--headless" in args
    assert args[args.index("--convert-to") + 1].startswith("docx")
    outdir = args[args.index("--outdir") + 1]
    profile = next(a for a in args if a.startswith("-env:UserInstallation=file://"))
    assert profile.startswith(f"-env:UserInstallation=file://{outdir}/")
    assert (record / "home").read_text().strip() == outdir


def _zip_bytes(**members: bytes) -> bytes:
    import zipfile

    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name.replace("__", "/"), data)
    return out.getvalue()


@pytest.mark.parametrize(
    "output",
    [
        _zip_bytes(other__file_txt=b"fictional"),  # a zip, but not Word
        _zip_bytes(word__document_xml=b"<not-xml"),  # Word-shaped, unreadable
        b"",  # an empty file
    ],
    ids=["zip-not-word", "broken-word-zip", "empty-file"],
)
def test_unreadable_output_returns_none(tmp_path, monkeypatch, output):
    """AC-4: output is accepted only when python-docx can open it."""
    bad = tmp_path / "bad.docx"
    bad.write_bytes(output)
    monkeypatch.setenv("FAKE_DOCX", str(bad))
    _fake_soffice(tmp_path, monkeypatch, _WRITES_DOCX)
    assert convert_doc_to_docx(FAKE_DOC) is None


@pytest.mark.parametrize(
    "body,timeout",
    [(_WRITES_DOCX, 30), ("exit 1", 30), ("sleep 30 & wait", 1)],
    ids=["ok", "failed", "timeout"],
)
def test_temp_dirs_are_removed(tmp_path, monkeypatch, ready_docx, body, timeout):
    """Each call's temp dir (input, output, LibreOffice profile) is removed."""
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(settings, "template_doc_convert_timeout_seconds", timeout)
    _fake_soffice(tmp_path, monkeypatch, "mkdir -p \"$HOME/profile\"; " + body)
    convert_doc_to_docx(FAKE_DOC)
    assert list(scratch.iterdir()) == []


def test_conversions_run_one_at_a_time(tmp_path, monkeypatch, ready_docx):
    """Concurrent callers never run two LibreOffice processes at once, and
    each gets its own result."""
    from concurrent.futures import ThreadPoolExecutor

    busy = tmp_path / "busy"
    overlap = tmp_path / "overlap"
    body = (
        f'if ! mkdir {busy} 2>/dev/null; then echo x >> {overlap}; fi; '
        f"sleep 0.3; rmdir {busy} 2>/dev/null; " + _WRITES_DOCX
    )
    _fake_soffice(tmp_path, monkeypatch, body)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(convert_doc_to_docx, [FAKE_DOC] * 3))
    assert results == [ready_docx] * 3
    assert not overlap.exists()


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_real_junk_ole2_returns_none():
    """AC-4 against real LibreOffice: an OLE2 header with nothing behind it."""
    started = time.monotonic()
    assert convert_doc_to_docx(FAKE_DOC) is None
    assert time.monotonic() - started < 60


async def test_reingested_doc_is_not_reingested_again(monkeypatch, ingest_env):
    """AC-8 'once': the payload written by the re-ingest stops the next sync."""
    calls = []

    async def convert(data):
        calls.append(data)
        return _docx_bytes("FICTIONAL HEADING", "Fictional clause one.")

    async def profile(self, **kw):
        ingest_env["profiles"] += 1
        return {
            "interactive_variables": [{"key": "fictional_party"}],
            "profile_schema_version": drafting_module.PROFILE_SCHEMA_VERSION,
        }

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    monkeypatch.setattr(drafting_module, "_doc_converter_available", lambda: True)
    monkeypatch.setattr(drafting_module.DraftingAgent, "_extract_template_profile", profile)
    first = await _ingest(previous=_cached())
    second = await _ingest(previous=first)
    assert ingest_env["downloads"] == 1
    assert len(calls) == 1
    assert ingest_env["profiles"] == 1
    assert second["doc_conversion"] == "ok"
    assert second["template_docx_gcs_uri"] == first["template_docx_gcs_uri"]


async def test_lazy_backfill_records_the_skeleton(monkeypatch, ingest_env):
    """AC-10: the converted .docx's URI is written back to the template cache."""
    seen = []

    async def convert(data):
        seen.append(data)
        return _docx_bytes()

    monkeypatch.setattr(drafting_module, "_convert_doc_template", convert)
    await _agent()._lazy_backfill_template_skeleton(
        tpl={"extension": "doc", "source_url": "https://storage.example/tpl", "name": "Fictional"},
        user_id="user_fictional", template_id="tpl_fictional", bucket="fictional-bucket",
    )
    assert seen == [FAKE_DOC]
    puts = ingest_env["store"].puts
    assert len(puts) == 1
    assert puts[0]["template_docx_gcs_uri"].startswith("gs://fictional-bucket/")
    assert puts[0]["extension"] == "doc"


# ------------------------------------------- AC-8 through the sync handler


async def test_global_sync_reingests_a_cached_doc_once(monkeypatch):
    """AC-8 end to end: the connect-time snapshot re-sends the same version
    and file hash of a .doc cached before SIM-228; with a converter installed
    that sync must reach ingest_template once (and not again afterwards)."""
    from app.langgraph import drafting_agent as da
    from app.langgraph.template_store import (
        GLOBAL_OWNER, GLOBAL_SCOPE, STATUS_PUBLISHED, template_store,
    )
    from app.socket.events import event_handler as h

    class _Socket:
        async def emit(self, event, payload):
            return True

    monkeypatch.setattr(template_store, "_store", {})
    monkeypatch.setattr(template_store, "_global_versions", {})
    monkeypatch.setattr(h, "_socket_client", _Socket())
    monkeypatch.setattr(h, "_drafting_meta_by_workspace", {})
    monkeypatch.setattr(drafting_module, "_doc_converter_available", lambda: True)

    tid = "6720a1b2c3d4e5f6a7b8c9ff"
    cached = _cached()
    cached.update({"template_id": tid, "scope": GLOBAL_SCOPE, "version": 1})
    await template_store.put(user_id=GLOBAL_OWNER, template_id=tid, payload=cached)
    await template_store.set_global_status(template_id=tid, status=STATUS_PUBLISHED)

    ingests = []

    async def ingest(*, user_id, template_id, previous_payload=None, **kw):
        ingests.append(template_id)
        payload = dict(previous_payload or {})
        payload["doc_conversion"] = "ok"
        await template_store.put(user_id=user_id, template_id=template_id, payload=payload)
        return payload

    monkeypatch.setattr(da.drafting_agent, "ingest_template", ingest)
    item = {
        "id": tid, "version": 1, "name": "Fictional deed", "originalName": "deed.doc",
        "category": "Document Draft", "kind": "file", "extension": "doc",
        "fileHash": "h1", "fileUrl": "https://storage.example.invalid/signed",
    }
    for _ in range(2):
        await h.on_global_template_sync({"action": "published", "templates": [item]})
    assert ingests == [tid]


# ------------------------------------------------- fixes after verification


def test_leftover_child_is_killed_after_a_normal_exit(tmp_path, monkeypatch, ready_docx):
    marker = tmp_path / "child.pid"
    # Converts, then leaves a child running (as a stray soffice.bin would).
    _fake_soffice(
        tmp_path, monkeypatch,
        f"sleep 30 & echo $! > {marker}; " + _WRITES_DOCX,
    )
    assert convert_doc_to_docx(FAKE_DOC) == ready_docx
    child = int(marker.read_text().strip())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_busy_converter_gives_up_after_the_timeout(tmp_path, monkeypatch, ready_docx):
    _fake_soffice(tmp_path, monkeypatch, _WRITES_DOCX)
    monkeypatch.setattr(settings, "template_doc_convert_timeout_seconds", 1)
    assert doc_to_docx._lock.acquire(timeout=1)
    try:
        started = time.monotonic()
        assert convert_doc_to_docx(FAKE_DOC) is None
        assert time.monotonic() - started < 5
    finally:
        doc_to_docx._lock.release()


@pytest.mark.parametrize(
    "payload,available,expected",
    [
        ({"extension": "doc"}, True, True),
        ({"extension": "doc"}, False, False),
        ({"extension": "doc", "doc_conversion": "failed"}, True, False),
        ({"extension": "doc", "doc_conversion": "ok"}, True, False),
        ({"extension": "docx"}, True, False),
        (None, True, False),
    ],
)
def test_needs_doc_conversion(monkeypatch, payload, available, expected):
    monkeypatch.setattr(drafting_module, "_doc_converter_available", lambda: available)
    assert drafting_module.needs_doc_conversion(payload) is expected
