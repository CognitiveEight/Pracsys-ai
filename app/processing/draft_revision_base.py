"""Load the lawyer's latest draft version as the base of a revision (SIM-219).

On a drafting revision turn the backend sends ``baseDocumentUrl``: a
short-lived link to the latest stored .docx of the draft, which includes the
lawyer's own edits from the editor. This module reads that file once and
decides what the turn may do with it:

- ``base_unreadable``: the link failed or expired, the file is too large, or
  it is not a valid .docx;
- ``open_changes``: the file still has tracked changes or comments
  (``docx_review_state``); a revision would silently accept or drop them;
- ``ok``: the body converted to drafting Markdown (``docx_markdown``).

No model is called here. The URL is never stored and is logged only through
``safe_url``; the file's text is never logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from app.processing.docx_review_state import scan_review_state
from app.utils.redact import safe_url

OUTCOME_OK = "ok"
OUTCOME_OPEN_CHANGES = "open_changes"
OUTCOME_UNREADABLE = "base_unreadable"

OPEN_CHANGES_MESSAGE = (
    "This draft has tracked changes or comments that are still open, "
    "including any changes suggested by the AI. Accept or reject them in the "
    "editor, then ask again."
)
UNREADABLE_MESSAGE = (
    "I could not open the latest version of this draft, so I have not "
    "changed it. Please try again in a moment."
)


class BaseUnreadable(Exception):
    """The base file could not be fetched or is not usable. ``reason`` is a
    fixed code for logs, never content or a URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RevisionBase:
    outcome: str
    markdown: str = ""
    tracked_changes: int = 0
    comments: int = 0
    reason: Optional[str] = None  # set when outcome is base_unreadable
    # SIM-220: the file itself, kept for this turn only (the tracked-changes
    # path edits it). Never stored in the session or logged.
    docx_bytes: bytes = field(default=b"", repr=False)

    def refusal(self) -> Optional[dict]:
        """The ``draftRefusal`` object for the ai_response, or None."""
        if self.outcome == OUTCOME_OPEN_CHANGES:
            return {
                "code": OUTCOME_OPEN_CHANGES,
                "trackedChanges": self.tracked_changes,
                "comments": self.comments,
            }
        if self.outcome == OUTCOME_UNREADABLE:
            return {"code": OUTCOME_UNREADABLE}
        return None

    def message(self) -> str:
        if self.outcome == OUTCOME_OPEN_CHANGES:
            return OPEN_CHANGES_MESSAGE
        if self.outcome == OUTCOME_UNREADABLE:
            return UNREADABLE_MESSAGE
        return ""


async def fetch_base_docx(
    url: str, *, timeout_seconds: float, max_bytes: int
) -> bytes:
    """Download the base file once, streamed, bounded by time and size."""
    import httpx

    clean = (url or "").strip()
    if not clean.lower().startswith(("https://", "http://")):
        raise BaseUnreadable("bad_url")
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=True
            ) as client:
                async with client.stream("GET", clean) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "draft base download failed: status={} url={}",
                            response.status_code,
                            safe_url(clean),
                        )
                        raise BaseUnreadable(f"http_{response.status_code}")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise BaseUnreadable("too_large")
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise BaseUnreadable("too_large")
                    return bytes(buf)
    except BaseUnreadable:
        raise
    except asyncio.CancelledError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("draft base download timed out: url={}", safe_url(clean))
        raise BaseUnreadable("timeout")
    except Exception as exc:
        logger.warning(
            "draft base download failed: {} url={}", type(exc).__name__, safe_url(clean)
        )
        raise BaseUnreadable("download_error")


def _inspect(docx_bytes: bytes) -> RevisionBase:
    """Scan, then convert. Runs in a worker thread (zip + XML parsing)."""
    from app.processing.docx_markdown import docx_to_draft_markdown

    state = scan_review_state(docx_bytes)
    if not state.readable:
        return RevisionBase(outcome=OUTCOME_UNREADABLE, reason=state.reason)
    if state.has_open_changes:
        return RevisionBase(
            outcome=OUTCOME_OPEN_CHANGES,
            tracked_changes=state.tracked_changes,
            comments=state.comments,
        )
    try:
        markdown = docx_to_draft_markdown(docx_bytes)
    except Exception:
        return RevisionBase(outcome=OUTCOME_UNREADABLE, reason="convert_failed")
    if not markdown.strip():
        return RevisionBase(outcome=OUTCOME_UNREADABLE, reason="empty_body")
    return RevisionBase(outcome=OUTCOME_OK, markdown=markdown, docx_bytes=docx_bytes)


async def load_revision_base(url: str) -> RevisionBase:
    """Fetch and inspect the base file. Never raises except on cancellation."""
    from app.core.config import settings

    try:
        data = await fetch_base_docx(
            url,
            timeout_seconds=float(settings.draft_base_download_timeout_seconds),
            max_bytes=int(settings.draft_base_max_bytes),
        )
    except BaseUnreadable as exc:
        return RevisionBase(outcome=OUTCOME_UNREADABLE, reason=exc.reason)
    return await asyncio.to_thread(_inspect, data)
