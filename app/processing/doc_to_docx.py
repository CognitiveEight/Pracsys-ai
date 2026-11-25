"""Convert a legacy Word 97-2003 ``.doc`` into ``.docx`` (SIM-228).

Template ingest stores a Word skeleton only for ``.docx`` (and PDFs/images it
can rebuild). A ``.doc`` template had none, so every draft from it took the
``defaults`` route with generic styling, and its text came from ``antiword``
or a raw scrape that loses even the line breaks. Converting it to ``.docx``
first lets it follow the ``.docx`` path end to end: structured text
extraction, the stored skeleton, and the template routes that keep its layout.

LibreOffice, run headless, is the only converter that keeps fonts, numbering,
indents and tables. Each call:

- runs in its own temporary directory with its own LibreOffice profile, so
  calls never share state and nothing is left behind;
- is bounded by ``TEMPLATE_DOC_CONVERT_TIMEOUT_SECONDS``; on timeout the whole
  process group is killed (``soffice`` starts a child ``soffice.bin``);
- runs one at a time (a module lock): LibreOffice is memory-hungry, and
  template ingest is rare;
- returns ``None`` on any failure, so the caller keeps today's behaviour.

Nothing here logs a filename or document text: sizes, durations and a status.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from typing import Optional

from loguru import logger

# The first bytes of an OLE2 compound file (every real .doc).
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_MAX_INPUT_BYTES = 50 * 1024 * 1024
_MAX_OUTPUT_BYTES = 100 * 1024 * 1024

_lock = threading.Lock()


def _soffice_path() -> Optional[str]:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def is_available() -> bool:
    """True when a LibreOffice binary is on PATH."""
    return _soffice_path() is not None


def _settings():
    from app.core.config import settings

    return settings


def _is_docx(data: bytes) -> bool:
    if not data.startswith(_ZIP_MAGIC):
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            return "word/document.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def _opens_as_word(data: bytes) -> bool:
    if not _is_docx(data):
        return False
    try:
        from docx import Document

        Document(BytesIO(data))
        return True
    except Exception:
        return False


def _run(cmd: list, cwd: str, timeout: float) -> int:
    """Run LibreOffice in its own process group; kill the group on timeout."""
    env = dict(os.environ)
    env["HOME"] = cwd  # LibreOffice writes caches under $HOME
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        proc.wait()
        raise
    finally:
        # A child LibreOffice left behind after a normal exit must not keep
        # running (or writing into the temp dir being removed).
        _kill_group(proc.pid)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def convert_doc_to_docx(data: bytes) -> Optional[bytes]:
    """``.doc`` bytes → ``.docx`` bytes, or ``None`` when it cannot be done.

    A file that is already a ``.docx`` (renamed) is returned unchanged.
    Blocking: call it through ``asyncio.to_thread``.
    """
    if not data or len(data) > _MAX_INPUT_BYTES:
        return None
    if _is_docx(data):
        return data
    settings = _settings()
    if not getattr(settings, "template_doc_convert_enabled", True):
        return None
    soffice = _soffice_path()
    if soffice is None:
        logger.warning(
            "doc_to_docx unavailable: LibreOffice (soffice) is not on PATH; "
            "the .doc template keeps generic styling"
        )
        return None
    if not data.startswith(_OLE2_MAGIC):
        # RTF or HTML saved as .doc: LibreOffice reads those too.
        logger.debug("doc_to_docx input is not OLE2; converting anyway")

    timeout = float(getattr(settings, "template_doc_convert_timeout_seconds", 120) or 120)
    started = time.monotonic()
    # Bounded wait: templates queued behind a hung conversion give up rather
    # than hold executor threads for N x timeout.
    if not _lock.acquire(timeout=timeout):
        logger.warning(
            "doc_to_docx failed status=busy in_bytes={}; the .doc template keeps "
            "generic styling",
            len(data),
        )
        return None
    try:
        out, status = _convert_locked(soffice, data, timeout)
    finally:
        _lock.release()
    elapsed = time.monotonic() - started
    if out is None:
        logger.warning(
            "doc_to_docx failed status={} in_bytes={} duration={:.1f}s; "
            "the .doc template keeps generic styling",
            status,
            len(data),
            elapsed,
        )
    else:
        logger.info(
            "doc_to_docx ok in_bytes={} out_bytes={} duration={:.1f}s",
            len(data),
            len(out),
            elapsed,
        )
    return out


def _convert_locked(soffice: str, data: bytes, timeout: float):
    """One conversion in a fresh temp dir; returns ``(bytes or None, status)``."""
    status = "error"
    out: Optional[bytes] = None
    with tempfile.TemporaryDirectory(prefix="doc2docx-", ignore_cleanup_errors=True) as tmp:
        src = os.path.join(tmp, "input.doc")
        with open(src, "wb") as fh:
            fh.write(data)
        profile = "file://" + os.path.join(tmp, "profile")
        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            f"-env:UserInstallation={profile}",
            "--convert-to",
            "docx:MS Word 2007 XML",
            "--outdir",
            tmp,
            src,
        ]
        try:
            code = _run(cmd, tmp, timeout)
            dst = os.path.join(tmp, "input.docx")
            if code == 0 and os.path.isfile(dst):
                size = os.path.getsize(dst)
                if 0 < size <= _MAX_OUTPUT_BYTES:
                    with open(dst, "rb") as fh:
                        candidate = fh.read()
                    if _opens_as_word(candidate):
                        out = candidate
                        status = "ok"
                    else:
                        status = "invalid_output"
                else:
                    status = "bad_size"
            else:
                status = f"exit_{code}"
        except subprocess.TimeoutExpired:
            status = "timeout"
        except Exception as exc:  # never fail ingest because of the converter
            logger.opt(exception=exc).warning("doc_to_docx raised")
            status = "error"
    return out, status
